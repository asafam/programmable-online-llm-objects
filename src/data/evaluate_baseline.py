"""
Baseline evaluation runner — OpenClaw single/multi-agent comparison for the LNL experiment.

Runs the same Samples as evaluate.py but uses an OpenClaw agent instead of the
LNL runtime. By default, uses a single combined agent that receives all object
definitions and handles all messages. With --multi-agent, uses one agent per LNL-object.

Requires:
    - OpenClaw daemon running (openclaw gateway status)
    - openclaw-sdk installed (pip install openclaw-sdk)

Usage:
    python -m src.data.evaluate_baseline \\
        -i outputs/data/zapier/20260322_010211/samples.jsonl \\
        --runs 3

    # Multi-agent mode (one agent per object):
    python -m src.data.evaluate_baseline \\
        -i outputs/data/zapier/20260322_010211/samples.jsonl \\
        --multi-agent --runs 3
"""
from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import dataclasses
import json
import random
import re
import statistics
import sys
import time

import httpx
from collections import defaultdict
from tqdm import tqdm
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

load_dotenv()

from src.data.schema import (
    EvalSummary,
    EventResult,
    ModificationResult,
    ObjectDef,
    Sample,
    SampleResult,
)
from src.data.mock_server import MockServer, merge_tc_mock_tools, resolve_mock_configs
from src.data.evaluate import INTER_EVENT_TIMEOUT_S
from src.data.utils import (
    format_tc_event_detail,
    infer_provider,
    load_jsonl,
)
from src.lnl.openclaw_export import (
    _bootstrap_stub_md,
    _identity_md,
    export_single_agent_workspace,
    export_workflow_from_objects,
    reset_agent_state,
    reset_single_agent_state,
)

try:
    from openclaw_sdk.core.exceptions import TimeoutError as OcTimeoutError
except ImportError:
    OcTimeoutError = None  # type: ignore[assignment,misc]

try:
    from openclaw_sdk.core.exceptions import GatewayError as OcGatewayError
except ImportError:
    OcGatewayError = None  # type: ignore[assignment,misc]

# ── Remote mock server client ─────────────────────────────────────────────────

class RemoteMockServer:
    """Thin HTTP client for a mock server running inside a Docker container.

    Drop-in replacement for MockServer when the server is already running
    remotely. Pass its URL via --mock-server-url and this class handles
    /configure, /log, and /health calls without starting a local process.
    """

    def __init__(self, url: str):
        self._url = url.rstrip("/")
        # Set before configure() to include mock script in the /configure payload.
        self._pending_mock_script: Any = None

    def start(self) -> None:
        pass  # server already running in the container

    def wait_ready(self, timeout: float = 30.0) -> None:
        import time
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                resp = httpx.get(f"{self._url}/health", timeout=2.0)
                if resp.status_code == 200:
                    return
            except Exception:
                pass
            time.sleep(0.5)
        raise RuntimeError(f"Remote mock server at {self._url} not ready after {timeout}s")

    def configure(self, session_key: str, slot_id: str = "default") -> None:
        payload: dict[str, Any] = {"session_key": session_key, "slot_id": slot_id}
        if self._pending_mock_script is not None:
            payload["mock_script"] = self._pending_mock_script.model_dump()
            self._pending_mock_script = None
        httpx.post(f"{self._url}/configure", json=payload, timeout=10.0).raise_for_status()

    def get_log(self, slot_id: str = "default") -> list[dict]:
        resp = httpx.get(f"{self._url}/log", params={"slot_id": slot_id}, timeout=10.0)
        resp.raise_for_status()
        return resp.json().get("calls", [])

    def stop(self) -> None:
        pass  # container lifecycle is managed externally


# ── Docker worker pool ────────────────────────────────────────────────────────

@dataclasses.dataclass
class WorkerConfig:
    """Connection details for a single OpenClaw Docker worker container.

    data_dir      — host path that is bind-mounted into the container as
                    /home/node/.openclaw.  evaluate_baseline.py writes workspace
                    and agent files here; the gateway inside the container reads
                    them via the mount.
    container_home — the path the gateway sees for those same files (default:
                    /home/node/.openclaw).  This is substituted into the agent
                    workspace/agentDir entries that are written to the gateway
                    config API, so they resolve correctly inside the container.
    """
    name: str
    gateway_url: str
    mock_server_url: str
    data_dir: Path
    container_home: str = "/home/node/.openclaw"
    container_name: Optional[str] = None  # Docker container name for auto-restart

    @property
    def token(self) -> Optional[str]:
        return _load_openclaw_token(self.data_dir)


def _load_pool_config(path: Path) -> list[WorkerConfig]:
    """Load a worker-pool YAML file and return a list of WorkerConfig objects."""
    import yaml
    raw = yaml.safe_load(path.read_text())
    workers = []
    for entry in raw.get("workers", []):
        workers.append(WorkerConfig(
            name=entry["name"],
            gateway_url=entry["gateway_url"],
            mock_server_url=entry["mock_server_url"],
            data_dir=Path(entry["data_dir"]).expanduser(),
            container_home=entry.get("container_home", "/home/node/.openclaw"),
            container_name=entry.get("container_name"),
        ))
    if not workers:
        raise ValueError(f"No workers defined in pool config: {path}")
    return workers


def _clean_pool_worker_dirs(workers: list[WorkerConfig]) -> None:
    """Wipe accumulated workspace/config/log files from each worker data_dir.

    Preserves identity/, devices/ (auth files written by start-pool.sh),
    openclaw.json (gateway config containing the auth token + agent list),
    extensions/ (plugin JS files copied into the bind-mount by the entrypoint),
    and workspace-*/ dirs (agent workspaces written by pre-registration or prior
    TCs — content is refreshed per-TC by export_workflow_from_objects with
    force=True, so the dirs themselves must survive to avoid evicting agents that
    were pre-started with skipBootstrap=False and are currently paired).
    Called once at the start of every pool-mode eval run so workers start clean.
    """
    import shutil
    _KEEP = {"identity", "devices", "openclaw.json", "extensions"}
    for w in workers:
        if not w.data_dir.is_dir():
            continue
        removed = []
        skipped = []
        for child in w.data_dir.iterdir():
            # Keep workspace dirs — pre-started agents read them; per-TC export
            # rewrites their content with force=True so there is no state bleed.
            if child.name in _KEEP or child.name.startswith("workspace-"):
                continue
            try:
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink(missing_ok=True)
                removed.append(child.name)
            except PermissionError:
                # Container-owned dirs (e.g. logs/, tmp/) may be unreadable/undeleteable.
                # Skip them — they don't affect the eval.
                skipped.append(child.name)
        if removed:
            print(f"  [{w.name}] cleaned {len(removed)} entries from {w.data_dir}")
        if skipped:
            print(f"  [{w.name}] skipped (permission denied): {skipped}")


def _load_device_operator_token() -> Optional[str]:
    """Read the local SDK device operator token from ~/.openclaw/identity/device-auth.json.

    This is the token the SDK sends in connect.params.auth.token during the
    connect handshake.  The container gateway must be configured with this same
    value as its auth token, otherwise the handshake fails with 'token mismatch'.
    """
    auth_path = Path.home() / ".openclaw" / "identity" / "device-auth.json"
    if not auth_path.exists():
        return None
    try:
        data = json.loads(auth_path.read_text())
        return data.get("tokens", {}).get("operator", {}).get("token") or None
    except Exception:
        return None


def _write_worker_gateway_config(data_dir: Path, operator_token: str) -> None:
    """Write (or patch) data_dir/openclaw.json so the gateway uses operator_token for auth.

    Preserves any extra fields already present in the file (e.g. agent list,
    plugin config the gateway wrote back).  Falls back to writing a fresh
    minimal config if the existing file is unreadable.
    """
    config_path = data_dir / "openclaw.json"
    cfg: dict = {}
    if config_path.exists():
        try:
            cfg = json.loads(config_path.read_text())
        except Exception:
            pass
    cfg.setdefault("gateway", {}).setdefault("auth", {})
    cfg["gateway"]["auth"]["mode"] = "token"
    cfg["gateway"]["auth"]["token"] = operator_token
    cfg["gateway"].setdefault("mode", "local")
    # Trust loopback + Docker bridge so inter-agent sessions_send routing isn't
    # blocked by the WS handshake hardening introduced in 2026.2.19-2 (issue #21236).
    cfg["gateway"]["trustedProxies"] = ["127.0.0.1", "::1", "172.16.0.0/12"]
    cfg.setdefault("plugins", {"allow": ["lnl-mock-external"]})
    cfg.setdefault("tools", {"sessions": {"visibility": "all"}})
    cfg.setdefault("commands", {"native": "auto", "nativeSkills": "auto", "restart": True})
    cfg.setdefault("agents", {"list": []})
    # Delete before writing: openclaw.json may be owned by the container's node user
    # (mode 600).  We own the parent directory, so unlink() succeeds even without
    # file ownership; write_text() then creates a fresh file we own.
    try:
        config_path.unlink(missing_ok=True)
    except Exception:
        pass
    config_path.write_text(json.dumps(cfg, indent=2))


def _ensure_worker_gateway_token(worker: "WorkerConfig", operator_token: str) -> bool:
    """Ensure the worker's gateway is configured with operator_token.

    Returns True if the container was restarted (caller should re-wait for health).
    """
    import subprocess as _sp
    worker.data_dir.mkdir(parents=True, exist_ok=True)

    # Read openclaw.json directly, catching PermissionError explicitly.
    # If the file is container-owned (mode 600), a PermissionError means the
    # gateway was already configured by pre-registration (smoke-B / start-pool.sh).
    # Restarting in that case would wipe the pre-registered agent list and undo
    # config_mgr.patch effects (skipBootstrap, agentToAgent, etc.).
    config_path = worker.data_dir / "openclaw.json"
    current_token: Optional[str] = None
    if config_path.exists():
        try:
            raw = config_path.read_text()
            current_token = json.loads(raw).get("gateway", {}).get("auth", {}).get("token") or None
        except PermissionError:
            # File is container-owned (mode 600): pre-registration configured the
            # container correctly. The gateway uses its --token CLI arg for auth, not
            # the file. Skip restart to preserve the pre-registered config.
            return False
        except Exception:
            pass  # readable but malformed — fall through to restart

    if current_token == operator_token:
        return False  # already correct

    print(f"  [{worker.name}] Gateway token mismatch — updating config and restarting container...")
    _write_worker_gateway_config(worker.data_dir, operator_token)

    if worker.container_name:
        try:
            _sp.run(["docker", "restart", worker.container_name],
                    check=True, capture_output=True, timeout=30)
        except Exception as exc:
            print(f"  [{worker.name}] WARNING: docker restart failed: {exc}. "
                  "Container may still use the old token.")
            return False
    else:
        print(f"  [{worker.name}] WARNING: no container_name set; cannot auto-restart. "
              "Restart the container manually for the new token to take effect.")
        return False
    return True


# ── Version ───────────────────────────────────────────────────────────────────

def _build_version() -> str:
    """Dynamic version string: git commit timestamp + short hash + '+dirty' if uncommitted changes."""
    import subprocess as _sp
    try:
        ts = _sp.check_output(
            ["git", "log", "-1", "--format=%cd", "--date=format:%Y%m%d_%H%M%S"],
            stderr=_sp.DEVNULL, text=True
        ).strip()
        sha = _sp.check_output(
            ["git", "log", "-1", "--format=%h"],
            stderr=_sp.DEVNULL, text=True
        ).strip()
        dirty = _sp.call(
            ["git", "diff", "--quiet", "HEAD"],
            stderr=_sp.DEVNULL
        ) != 0
        base = f"{ts}_{sha}" if ts and sha else "unknown"
        return f"{base}+dirty" if dirty else base
    except Exception:
        import os
        mtime = os.path.getmtime(__file__)
        from datetime import datetime
        return datetime.fromtimestamp(mtime).strftime("%Y%m%d_%H%M%S")

_VERSION: str = _build_version()  # bumped 2026-05-24 (v45): classify all HTTP 4xx/5xx as infra_provider — covers auth (401), quota (429), schema (400), server errors (500/503) and any future provider HTTP errors without needing per-code additions.

# ── Infrastructure failure detection ─────────────────────────────────────────

_INFRA_ERROR_PATTERNS: list[str] = [
    "pairing required",
    "network connection error",
    ": terminated",
    # Gateway / connection failures that indicate infra problems, not eval failures
    "gateway did not become ready",
    "not connected. call await gw.connect",
    "timed out connecting to ws://",
    "gateway became unstable after config write",
    "websocket disconnected",
    "keepalive ping timeout",
    "gatewaydisconnected",
    # TCP-level transport errors from Docker gateway hot-reload / container restart
    "connection reset by peer",  # errno 54 (macOS) / 104 (Linux) — gateway mid-restart
    "connection refused",        # gateway not yet up after restart
    "broken pipe",               # write to a closed socket during hot-reload
    # Generic timeout strings from websockets (TimeoutError("timed out")) and
    # subprocess.TimeoutExpired ("Command '...' timed out after 60 seconds")
    "timed out",
    # Azure / OpenAI provider 5xx — the LLM provider is the one failing, not the
    # agent. Keep parity with _INFRA_PROVIDER_PATTERNS below so TC-level
    # catch-all classification matches per-event classification.
    "http 500", "http 503", "5xx", "internal server error", "service unavailable",
]

def _classify_error_type(reasoning_texts: list[str]) -> Optional[str]:
    """Return 'infra' when reasoning texts match known infra failure patterns."""
    combined = " ".join(t for t in reasoning_texts if t).lower()
    if any(p in combined for p in _INFRA_ERROR_PATTERNS):
        return "infra"
    return None


# --- Per-event 3-way failure classification ----------------------------------

# Substrings that indicate Azure/LLM-provider failures (NOT our integration's fault).
# Sourced from observed error_message texts, judge reasoning patterns, and known
# Azure error codes. Pattern match is case-insensitive.
_INFRA_PROVIDER_PATTERNS: list[str] = [
    # Rate-limiting / throttling
    "rate-limit", "rate limit", "rate-limited", "rate limited", "throttle", "429",
    # SDK's standard "no response" message — explicitly says rate-limit or unavailable
    "the llm may be rate-limited or unavailable",
    "agent completed with no response",
    # Azure content filter / responsible-AI policy
    "content_filter", "content filter", "jailbreak", "responsibleaipolicy",
    "responsible ai policy", "responsibleaiservice",
    # Provider schema rejection (the agent's tool list / payload made Azure 400)
    # — these are baseline-OC fault in spirit, but Azure is the one rejecting,
    # and the rejection happens BEFORE the LLM runs, so we can't measure agent
    # reasoning. Classified as infra_provider so it doesn't pollute behavioral.
    "rejected the request schema", "provider rejected the request",
    "llm request failed: provider rejected",
    # Any HTTP 4xx / 5xx from the provider — auth failures (401), quota (429),
    # schema rejection (400), server errors (500/503), etc. All indicate the
    # provider rejected the request before the agent could reason, so they
    # don't reflect agent quality and should be excluded from pass-rate scoring.
    "http 4", "http 5", "5xx", "azure openai response truncated",
    # OpenAI/Azure-specific error envelopes
    "openai_error", "azureopenai", "api error", "service unavailable",
]

# Substrings that indicate OpenClaw integration / our framework failures.
# These are things WE could fix (gateway setup, timeouts, session management).
_OC_EVAL_PATTERNS: list[str] = [
    # Gateway lifecycle / connection
    "gateway did not become ready", "openclaw gateway",
    "not connected", "call await gw.connect", "gateway became unstable",
    "websocket disconnected", "gatewaydisconnected", "keepalive ping timeout",
    "timed out connecting to ws://", "network connection error", ": terminated",
    # Session / pairing
    "pairing required", "pairing-required", "session not found", "session expired",
    # Timeouts (our 90s sessions_send / 180s event / 900-2400s TC settings)
    "timeout after", "timed out after", "wall-clock timeout", "deadline exceeded",
    # Generic "timed out" — but only here, after we've ruled out provider rate-limits
    # (which would have matched _INFRA_PROVIDER_PATTERNS already).
    "timed out", "timeout",
    # OpenClaw container / runtime aborts (our infra)
    "container restarted", "container died", "worker restart",
]


def _classify_failure(
    success: bool,
    passed: bool,
    error_message: str = "",
    stop_reason: str = "",
    reasoning: str = "",
) -> Optional[str]:
    """Three-way classify a failed event.

    Returns one of:
      None              — event passed (no failure to classify)
      'infra_provider'  — Azure/LLM-provider failure (rate-limit, content filter,
                          schema reject, HTTP 5xx). Not on us. Factored out of pass-rate.
      'oc_eval'         — OpenClaw integration / our framework failure (gateway not
                          ready, sessions_send timeout, TC wall-clock timeout, runtime
                          aborted, pairing errors). On us to fix. Factored out of pass-rate.
      'behavioral'      — Agent's reasoning produced a real failure (wrong tool args,
                          missing field, missing dispatch, wrong value, wrong branch,
                          lied about completion). Kept IN pass-rate.

    Heuristics (applied in priority order, infra first to avoid behavioral over-counting):
      1. If passed → None
      2. If error_message / reasoning matches a known PROVIDER pattern → 'infra_provider'
      3. If error_message / reasoning matches a known OC pattern → 'oc_eval'
      4. If stop_reason == 'aborted' → 'oc_eval' (runtime aborted execution)
      5. If success=False with no recognized signal → 'oc_eval' (SDK reported
         failure but didn't surface a provider error, so likely OC plumbing).
      6. Otherwise (success=True but judge graded failure) → 'behavioral'.
    """
    if passed:
        return None
    haystack = " ".join([error_message or "", stop_reason or "", reasoning or ""]).lower()
    # Provider first (priority over generic 'timed out' that OC also matches)
    if any(p in haystack for p in _INFRA_PROVIDER_PATTERNS):
        return "infra_provider"
    if any(p in haystack for p in _OC_EVAL_PATTERNS):
        return "oc_eval"
    if (stop_reason or "").lower() == "aborted":
        return "oc_eval"
    if not success:
        # SDK reported failure but no recognized signal — conservatively call it OC
        # (better than mis-tagging as behavioral).
        return "oc_eval"
    return "behavioral"


# ── OpenClaw agent configuration ─────────────────────────────────────────────

def _load_openclaw_token(openclaw_home: Optional[Path] = None) -> Optional[str]:
    """Read the gateway auth token from openclaw.json, if present."""
    config_path = (openclaw_home or Path.home() / ".openclaw") / "openclaw.json"
    if config_path.exists():
        try:
            cfg = json.loads(config_path.read_text())
            return cfg.get("gateway", {}).get("auth", {}).get("token") or None
        except Exception:
            pass
    return None


_DEFAULT_GATEWAY_WS_URL = "ws://127.0.0.1:18789"


def _reset_agent_session(object_id: str, openclaw_home: Path) -> None:
    """Delete the 'main' session transcript for an agent so the next run starts fresh.

    In multi-agent mode all agents use session_name="main". Without resetting,
    conversation history from a previous run bleeds into the next one.
    """
    sessions_dir = openclaw_home / "agents" / object_id / "sessions"
    sessions_json = sessions_dir / "sessions.json"
    try:
        if not sessions_json.exists():
            return
    except PermissionError:
        return  # container owns agents/; gateway API handles cleanup
    try:
        store = json.loads(sessions_json.read_text())
    except Exception:
        return

    main_key = f"agent:{object_id}:main"
    entry = store.pop(main_key, None)
    if entry:
        # Delete the JSONL transcript file so the gateway starts fresh
        transcript = Path(entry.get("sessionFile", ""))
        try:
            if transcript.exists():
                transcript.unlink(missing_ok=True)
            sessions_json.write_text(json.dumps(store, indent=2))
        except PermissionError:
            pass


def _clear_agent_sessions(object_id: str, openclaw_home: Path) -> None:
    """Delete ALL session transcripts for an agent so the next run starts with a clean history.

    _global_counter resets to 0 on each script invocation, so the first run always
    uses session name "eval-ma-1".  If a previous invocation left a transcript under
    that name, the gateway reuses it — injecting old conversation history.  This
    function wipes sessions.json and all transcript JSONL files so no name can collide.

    Transcript paths stored in sessions.json use the *container* path
    (/home/node/.openclaw/...). For pool mode the files live on the host under
    openclaw_home, so we translate by replacing the container home prefix.
    """
    sessions_dir = openclaw_home / "agents" / object_id / "sessions"
    try:
        if not sessions_dir.exists():
            return
    except PermissionError:
        return  # container's node user owns agents/; can't stat — skip silently
    sessions_json = sessions_dir / "sessions.json"
    try:
        # Delete every JSONL transcript file referenced in sessions.json
        if sessions_json.exists():
            try:
                store = json.loads(sessions_json.read_text())
                for entry in store.values():
                    raw_path = entry.get("sessionFile", "")
                    if not raw_path:
                        continue
                    transcript = Path(raw_path)
                    # Remap container-absolute path to host path via openclaw_home
                    if transcript.is_absolute() and not transcript.exists():
                        try:
                            # Container home is /home/node/.openclaw; strip and re-root
                            rel = transcript.relative_to(Path("/home/node/.openclaw"))
                            transcript = openclaw_home / rel
                        except ValueError:
                            pass
                    transcript.unlink(missing_ok=True)
            except Exception:
                pass
        # Also delete any orphaned JSONL files not referenced in sessions.json
        for f in sessions_dir.glob("*.jsonl"):
            f.unlink(missing_ok=True)
        # Reset the session index to empty
        sessions_json.write_text("{}\n")
    except PermissionError:
        pass  # container's node user owns agents/; gateway API sessions.delete handles actual cleanup


def _clear_worker_state(
    objects: list,
    openclaw_home: Path,
    single_agent_id: Optional[str],
) -> None:
    """Clear session transcripts and state files for all agents on a worker."""
    if single_agent_id:
        agent_ids = [single_agent_id]
    else:
        agent_ids = [obj.object_id for obj in objects]
    for aid in agent_ids:
        _clear_agent_sessions(aid, openclaw_home)
        state_file = openclaw_home / f"workspace-{aid}" / "state.md"
        state_file.unlink(missing_ok=True)


# Lock to prevent concurrent container restarts — Docker Desktop's port
# forwarding becomes unreliable when multiple containers restart at once.
_restart_lock = asyncio.Lock()


async def _ensure_worker_healthy(worker: "WorkerConfig") -> bool:
    """Check if a worker's gateway is reachable; restart container if not.

    The config.patch hot-reload can kill the gateway process in a way that
    the entrypoint can't recover from (the self-restarted PID isn't found by
    pgrep, so the container shuts down).  This function detects that and
    brings the container back up.

    Returns True if the container was restarted (caller should re-export/
    re-configure agents), False if the gateway was already healthy.
    """
    import subprocess as _sp
    import httpx as _httpx

    # Quick health check — HTTP *and* WebSocket must both be up.
    # HTTP /health can return 200 while the WS server is still restarting
    # (hot-reload cycle); if we skip the WS probe and return "healthy" here,
    # the caller connects, gets "Not connected", and retries forever.
    ws_url = worker.gateway_url or "ws://127.0.0.1:18789"
    http_url = ws_url.replace("ws://", "http://").replace("wss://", "https://")
    health_url = f"{http_url}/health"
    try:
        r = _httpx.get(health_url, timeout=2.0)
        if r.status_code == 200:
            ws_ok = await _probe_ws_connection(ws_url, timeout=3.0)
            if ws_ok:
                return False  # fully healthy — no restart needed
    except Exception:
        pass

    # Gateway is down (HTTP or WS) — restart the container (serialised to avoid Docker races)
    if not worker.container_name:
        return False
    async with _restart_lock:
        # Re-check after acquiring lock (another coroutine may have fixed it)
        try:
            r = _httpx.get(health_url, timeout=2.0)
            if r.status_code == 200 and await _probe_ws_connection(ws_url, timeout=3.0):
                return False
        except Exception:
            pass
        tqdm.write(f"  [{worker.name}] Gateway is down — restarting container...")
        try:
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: _sp.run(
                    ["docker", "restart", worker.container_name],
                    check=True, capture_output=True, timeout=120,
                ),
            )
        except _sp.TimeoutExpired:
            # restart command itself timed out — force-kill and try a fresh start
            tqdm.write(f"  [{worker.name}] docker restart timed out — force-stopping and restarting...")
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: _sp.run(
                    ["docker", "stop", "-t", "5", worker.container_name],
                    capture_output=True,
                ),
            )
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: _sp.run(
                    ["docker", "start", worker.container_name],
                    check=True, capture_output=True, timeout=30,
                ),
            )

    # Wait for mock server
    deadline = asyncio.get_event_loop().time() + 60.0
    while asyncio.get_event_loop().time() < deadline:
        try:
            r = await asyncio.get_event_loop().run_in_executor(
                None, lambda: _httpx.get(f"{worker.mock_server_url}/health", timeout=2)
            )
            if r.status_code == 200:
                break
        except Exception:
            pass
        await asyncio.sleep(1.0)
    else:
        raise RuntimeError(f"[{worker.name}] Mock server not ready after restart")

    # Wait for gateway to be stable (the entrypoint writes a fresh template
    # config on startup, which triggers config overwrites over ~5s).
    # 120s matches the per-TC config-patch wait (line 2504): under CPU
    # contention, gateway-ready can slip from ~12s to 80s+ (see commit
    # 2f17cbc), so the previous 45s budget fired before the gateway came
    # back and surfaced as "gateway did not become ready within 45s".
    await _wait_for_gateway(
        worker.gateway_url, None,
        timeout_s=120.0, stable_for_s=5.0,
    )
    tqdm.write(f"  [{worker.name}] Container recovered.")
    return True  # restarted — caller must re-configure agents


def _slot_objects(objects: list, slot_suffix: str) -> list:
    """Return copies of ObjectDef list with object_id and peer IDs suffixed for a concurrent slot.

    Slot 0 (slot_suffix="") returns the original list unchanged.
    Slots 1+ (slot_suffix="-c1", "-c2", ...) get distinct IDs so their gateway agent
    registrations and workspace dirs don't collide with each other.
    """
    if not slot_suffix:
        return objects
    from src.data.schema import ObjectDef, PeerDecl
    slotted = []
    for obj in objects:
        slotted_peers = [
            PeerDecl(object_id=f"{p.object_id}{slot_suffix}", relationship=p.relationship)
            for p in obj.peers
        ]
        slotted.append(obj.model_copy(update={
            "object_id": f"{obj.object_id}{slot_suffix}",
            "peers": slotted_peers,
        }))
    return slotted


def _openclaw_connect_kwargs(gateway_url: Optional[str] = None, openclaw_home: Optional[Path] = None) -> dict:
    """Build kwargs for OpenClawClient.connect(), including auth token if configured."""
    import os
    kwargs: dict[str, Any] = {
        "gateway_ws_url": gateway_url or _DEFAULT_GATEWAY_WS_URL,
    }
    # Fallback chain: env var → worker's openclaw.json → host device-auth.json.
    # The worker file may be mode 600 (container's node user owns it after the
    # gateway cascade-rewrites it), so we need the device-auth.json fallback.
    # device-auth.json is always readable and carries the same operator token
    # that start-pool.sh seeded into each container's OPENCLAW_GATEWAY_TOKEN.
    token = (
        os.environ.get("OPENCLAW_GATEWAY_TOKEN")
        or _load_openclaw_token(openclaw_home)
        or _load_device_operator_token()
    )
    if token:
        kwargs["api_key"] = token  # ClientConfig.api_key → ProtocolGateway(token=...)
    return kwargs




async def _configure_single_openclaw_agent(
    agent_id: str,
    provider: str,
    model: str,
    openclaw_home: Path,
    gateway_url: Optional[str],
    path_prefix: Optional[Path] = None,
) -> str:
    """Register a single combined agent in the OpenClaw daemon config.

    Args:
        path_prefix: When set, use this path for workspace/agentDir entries in
            the gateway config instead of openclaw_home.  Use this when the host
            path (openclaw_home) and the container-internal path differ — e.g.
            Docker pool mode where openclaw_home is the bind-mount source and
            path_prefix is /home/node/.openclaw inside the container.

    Returns the provider string used (passed through unchanged).
    """
    import json
    import json5
    from openclaw_sdk import OpenClawClient

    config_home = path_prefix or openclaw_home

    async with await OpenClawClient.connect(**_openclaw_connect_kwargs(gateway_url, openclaw_home)) as client:
        result = await client.config_mgr.get()
        raw = result.get("raw") or "{}"
        if not isinstance(raw, str):
            raw = "{}"
        base_hash = result.get("hash")
        config = json5.loads(raw)
        _ensure_config_auth(config, openclaw_home)
        agents_cfg = config.setdefault("agents", {})
        lst = agents_cfg.setdefault("list", [])

        new_entry = {
            "id": agent_id,
            "name": agent_id.replace("-", " ").title(),
            "workspace": str(config_home / f"workspace-{agent_id}"),
            "agentDir": str(config_home / "agents" / agent_id / "agent"),
            "model": {"primary": f"{provider}/{model}"},
        }

        # Check if this agent is already configured identically — skip patch
        # (and the reload it triggers) when nothing changed.
        existing = next((a for a in lst if a.get("id") == agent_id), None)
        config_changed = existing != new_entry

        if config_changed:
            lst = [a for a in lst if a.get("id") != agent_id]
            lst.append(new_entry)
            agents_cfg["list"] = lst

            # Clear stale sessions so the reload fires immediately.
            sessions = await client.gateway.sessions_list()
            for s in sessions:
                key = s.get("key")
                if key:
                    try:
                        await client.gateway.sessions_delete(key)
                    except Exception:
                        pass

            await client.config_mgr.patch(json.dumps(config, indent=2), base_hash=base_hash)

    if config_changed:
        await _wait_for_gateway_restart(
            gateway_url, openclaw_home,
            drop_timeout_s=10.0,
            ready_timeout_s=30.0,
            stable_for_s=5.0,
        )
    return provider



def _ensure_config_auth(config: dict, openclaw_home: Optional[Path]) -> None:
    """Inject gateway.auth into config if absent.

    When config_mgr.get() returns an empty raw config ('{}'), a plain patch
    would clear the gateway's in-memory auth, causing 'pairing required' on
    the next connection.  We restore the token from openclaw.json so the
    patched config always carries the auth section.

    After the gateway cascade-rewrites openclaw.json, it may omit gateway.auth
    (auth is managed by the --token CLI arg, not the file).  The fallback chain
    ensures the next per-TC write re-seeds the auth section.
    """
    if config.get("gateway", {}).get("auth"):
        return  # already present — nothing to do
    import os
    token = (
        _load_openclaw_token(openclaw_home)
        or os.environ.get("OPENCLAW_GATEWAY_TOKEN")
        or _load_device_operator_token()   # reliable: always on host filesystem
    )
    if token:
        config.setdefault("gateway", {})["auth"] = {"mode": "token", "token": token}


async def _configure_openclaw_agents(
    objects: list[ObjectDef],
    provider: str,
    model: str,
    openclaw_home: Path,
    gateway_url: Optional[str],
    path_prefix: Optional[Path] = None,
    skip_bootstrap: bool = True,
) -> str:
    """Register all objects as agents in the OpenClaw daemon config.

    Args:
        path_prefix: When set, use this path for workspace/agentDir entries in
            the gateway config instead of openclaw_home.  See
            _configure_single_openclaw_agent for details.
        skip_bootstrap: When False, agents auto-start on gateway startup and
            immediately pair (connect) with the gateway.  Required for
            agentToAgent sessions_send to succeed — the target agent must be
            paired before it can receive a session message.  Defaults to True
            (agents start on demand, i.e. when they receive their first message).

    Returns the provider string used (passed through unchanged).
    """
    import json
    import json5
    from openclaw_sdk import OpenClawClient

    config_home = path_prefix or openclaw_home
    object_ids = {obj.object_id for obj in objects}

    async with await OpenClawClient.connect(**_openclaw_connect_kwargs(gateway_url, openclaw_home)) as client:
        result = await client.config_mgr.get()
        raw = result.get("raw") or "{}"
        if not isinstance(raw, str):
            raw = "{}"
        base_hash = result.get("hash")
        config = json5.loads(raw)
        _ensure_config_auth(config, openclaw_home)
        agents_cfg = config.setdefault("agents", {})
        lst = agents_cfg.setdefault("list", [])

        # Build desired agent list: keep ALL existing agents (accumulate),
        # update/add only the current TC's agents.  Never remove old entries —
        # they sit idle and don't affect the TC.  This minimises config diffs
        # so config.patch (and the gateway reload it triggers) fires as rarely
        # as possible.
        existing_ids = {a.get("id") for a in lst}
        new_lst = [a for a in lst if a.get("id") not in object_ids]
        for obj in objects:
            new_lst.append({
                "id": obj.object_id,
                "name": obj.object_id.replace("-", " ").title(),
                "workspace": str(config_home / f"workspace-{obj.object_id}"),
                "agentDir": str(config_home / "agents" / obj.object_id / "agent"),
                "model": {"primary": f"{provider}/{model}"},
            })

        # agentToAgent: ACCUMULATE allow list (union with existing) so the
        # config stabilizes after all unique agent IDs are registered.
        tools_cfg = config.setdefault("tools", {})
        old_allow = set(tools_cfg.get("agentToAgent", {}).get("allow", []))
        merged_allow = sorted(old_allow | object_ids)
        new_a2a = {"enabled": True, "allow": merged_allow}
        new_vis = "all"

        # defaults.skipBootstrap drives gateway-level auto-start behaviour
        new_defaults = {"model": f"{provider}/{model}", "skipBootstrap": skip_bootstrap}

        # Check if config actually changed — skip patch (and the reload it
        # triggers) when the agent list and tool settings are already correct.
        old_ids = sorted(a.get("id") for a in lst)
        new_ids = sorted(a.get("id") for a in new_lst)
        old_defaults = agents_cfg.get("defaults", {})
        config_changed = (
            old_ids != new_ids
            or tools_cfg.get("agentToAgent") != new_a2a
            or tools_cfg.get("sessions", {}).get("visibility") != new_vis
            or old_defaults.get("skipBootstrap") != skip_bootstrap
        )

        if config_changed:
            agents_cfg["list"] = new_lst
            agents_cfg["defaults"] = new_defaults
            tools_cfg["agentToAgent"] = new_a2a
            tools_cfg.setdefault("sessions", {})["visibility"] = new_vis

            # Clear stale sessions so the reload fires immediately —
            # "running"/"unknown" sessions cause the gateway to defer.
            sessions = await client.gateway.sessions_list()
            for s in sessions:
                key = s.get("key")
                if key:
                    try:
                        await client.gateway.sessions_delete(key)
                    except Exception:
                        pass

            await client.config_mgr.patch(json.dumps(config, indent=2), base_hash=base_hash)

    if config_changed:
        # The gateway hot-reloads after a config patch.
        await _wait_for_gateway_restart(
            gateway_url, openclaw_home,
            drop_timeout_s=10.0,
            ready_timeout_s=30.0,
            stable_for_s=5.0,
        )
    return provider


def _write_worker_config(
    worker: "WorkerConfig",
    all_object_ids: set[str],
    provider: str,
    model: str,
    single_agent_id: Optional[str],
    *,
    verbose: bool = True,
    preserve_a2a_allow: bool = False,
) -> int:
    """Write the agent config to a single worker's bind-mount.

    Returns the number of agents registered.  The gateway's file watcher
    detects the change and applies an in-process hot-reload — no SDK
    config.patch needed (which would trigger a full process restart).

    preserve_a2a_allow: if True, keep the existing tools.agentToAgent.allow
    list unchanged (only set enabled=True).  Use this for per-TC writes so
    that in-flight cascades from a previous TC are not blocked by a narrowed
    allow list.
    """
    import json

    config_home = Path(worker.container_home)
    oc_home = worker.data_dir

    # Create minimal workspace/agent dirs on the host bind-mount.
    # agents/ tree may be owned by the container's node user; mkdir raises PermissionError
    # in that case — skip silently, the gateway will create agentDir on first use.
    for oid in all_object_ids:
        (oc_home / f"workspace-{oid}").mkdir(parents=True, exist_ok=True)
        try:
            (oc_home / "agents" / oid / "agent").mkdir(parents=True, exist_ok=True)
        except PermissionError:
            pass
    if single_agent_id:
        (oc_home / f"workspace-{single_agent_id}").mkdir(parents=True, exist_ok=True)
        try:
            (oc_home / "agents" / single_agent_id / "agent").mkdir(parents=True, exist_ok=True)
        except PermissionError:
            pass

    # Read the existing config as base (preserves gateway.auth etc.).
    config_file = oc_home / "openclaw.json"
    if config_file.exists():
        try:
            import json5
            config = json5.loads(config_file.read_text())
        except Exception:
            # File may be unreadable (container's node user wrote it with 600 perms).
            # Start from an empty config — auth will be re-seeded by _ensure_config_auth.
            config = {}
    else:
        config = {}
    _ensure_config_auth(config, oc_home)

    # Always ensure the gateway can start when --bind lan is active.
    # Without dangerouslyAllowHostHeaderOriginFallback the gateway refuses to
    # start if gateway.controlUi.allowedOrigins is absent (required for
    # non-loopback bind).  This setting is safe for our local/HPC environment.
    config.setdefault("gateway", {}).setdefault("controlUi", {})[
        "dangerouslyAllowHostHeaderOriginFallback"
    ] = True

    # Build clean agent list: chrome-extension + all eval agents.
    agents_cfg = config.setdefault("agents", {})
    old_lst = agents_cfg.get("list", [])
    new_lst = [a for a in old_lst if a.get("id") == "chrome-extension"]

    registered_ids: set[str] = set()
    if single_agent_id:
        new_lst.append({
            "id": single_agent_id,
            "name": single_agent_id.replace("-", " ").title(),
            "workspace": str(config_home / f"workspace-{single_agent_id}"),
            "agentDir": str(config_home / "agents" / single_agent_id / "agent"),
            "model": {"primary": f"{provider}/{model}"},
        })
        registered_ids.add(single_agent_id)
    else:
        for oid in sorted(all_object_ids):
            new_lst.append({
                "id": oid,
                "name": oid.replace("-", " ").title(),
                "workspace": str(config_home / f"workspace-{oid}"),
                "agentDir": str(config_home / "agents" / oid / "agent"),
                "model": {"primary": f"{provider}/{model}"},
            })
            registered_ids.add(oid)

    agents_cfg["list"] = new_lst
    tools_cfg = config.setdefault("tools", {})
    if preserve_a2a_allow:
        # Merge current TC's agent IDs into the existing allow list (union, never narrow).
        # Never overwrite with a smaller set — a cascade from a just-completed TC may still
        # be draining and needs its agents to remain routable. If the existing config has
        # no allow list (e.g. fresh container), this seeds it from the current TC's agents.
        old_allow = set(tools_cfg.get("agentToAgent", {}).get("allow", []))
        merged_allow = sorted(old_allow | registered_ids)
        tools_cfg["agentToAgent"] = {"enabled": True, "allow": merged_allow}
    else:
        tools_cfg["agentToAgent"] = {
            "enabled": True,
            "allow": sorted(registered_ids),
        }
    tools_cfg.setdefault("sessions", {})["visibility"] = "all"

    # Write directly to the bind-mount — the gateway's file watcher
    # detects the change and applies an in-process hot-reload.
    # Skip the write when content is unchanged: writing identical bytes still
    # updates mtime, which triggers a spurious hot-reload and drops connections.
    new_content = json.dumps(config, indent=2) + "\n"
    if config_file.exists():
        try:
            if config_file.read_text() == new_content:
                n = len(registered_ids)
                if verbose:
                    print(f"  [{worker.name}] Config unchanged ({n} agents), skipping write.", flush=True)
                return n, False  # type: ignore[return-value]
        except Exception:
            pass
    # Write the config.  If the gateway's startup cascade has already rewritten
    # openclaw.json (mode 600, owned by container's node user), the write will
    # raise PermissionError.  Treat that as "unchanged" — workspace files were
    # already exported above; skipping the write avoids a per-TC hot-reload
    # cascade that causes the gateway PID to change, which the entrypoint
    # misreads as a clean exit and shuts down the container.
    try:
        config_file.write_text(new_content)
    except PermissionError:
        if verbose:
            print(f"  [{worker.name}] openclaw.json not writable (container-owned) — skipping.", flush=True)
        return len(registered_ids), False  # type: ignore[return-value]
    n = len(registered_ids)
    if verbose:
        print(f"  [{worker.name}] Config written ({n} agents).", flush=True)
    return n, True  # type: ignore[return-value]


def _preregister_agents_on_workers(
    workers: list["WorkerConfig"],
    all_object_ids: set[str],
    provider: str,
    model: str,
    single_agent_id: Optional[str],
) -> None:
    """Pre-register ALL agents on every worker by writing the config file
    to the bind-mounted data directory.

    Writes one worker at a time and waits for each gateway to complete its
    hot-reload before writing the next.  This staggers the reload cycle so
    workers never crash simultaneously (the entrypoint PID-tracking bug
    kills the container if a hot-reload fires while the process is still
    settling from a previous one).

    Also cleans up stale ``.bak`` / ``.clobbered`` files that cause the
    gateway's "Config observe anomaly" warning.
    """
    import time as _time
    import httpx as _httpx

    n_agents = len(all_object_ids) + (1 if single_agent_id else 0)
    print(
        f"Pre-registering {n_agents} agents on {len(workers)} workers (staggered)...",
        flush=True,
    )

    for w in workers:
        # Remove stale backup files to prevent "size-drop-vs-last-good" anomaly
        for bak in w.data_dir.glob("openclaw.json.bak*"):
            bak.unlink(missing_ok=True)
        for clob in w.data_dir.glob("openclaw.json.clobbered.*"):
            clob.unlink(missing_ok=True)

        _write_worker_config(w, all_object_ids, provider, model, single_agent_id)  # type: ignore[misc]

        # Wait for this worker's gateway to complete its hot-reload (HTTP + WS
        # stable for 3s) before writing the next worker.  Prevents the
        # thundering-herd crash when all N containers reload simultaneously.
        ws_url = w.gateway_url or "ws://127.0.0.1:18789"
        http_url = ws_url.replace("ws://", "http://").replace("wss://", "https://")
        health_url = f"{http_url}/health"
        deadline = _time.monotonic() + 60.0
        stable_since: Optional[float] = None
        while _time.monotonic() < deadline:
            http_ok = False
            ws_ok = False
            try:
                r = _httpx.get(health_url, timeout=1.0)
                http_ok = r.status_code == 200
            except Exception:
                pass
            if http_ok:
                try:
                    from websockets.sync.client import connect as _ws_connect_sync
                    with _ws_connect_sync(ws_url, open_timeout=2.0):
                        pass
                    ws_ok = True
                except Exception:
                    pass
            if http_ok and ws_ok:
                if stable_since is None:
                    stable_since = _time.monotonic()
                if _time.monotonic() - stable_since >= 8.0:
                    break
            else:
                stable_since = None
            _time.sleep(0.5)
        else:
            raise RuntimeError(
                f"[{w.name}] Gateway did not stabilise within 60s after pre-registration"
            )
        print(f"  [{w.name}] Ready ({n_agents} agents).", flush=True)


def _pool_doctor_check(
    openclaw_home: Path,
    tc_objects: list,
    *,
    slot_suffix: str = "",
    worker_name: str = "worker",
    print_fn=None,
) -> bool:
    """Pre-execution health check for a pool worker's agent setup.

    Verifies that the gateway config and workspace files are correctly set up
    before a TC runs.  Called after _write_worker_config and after
    reset_agent_state/rewrite_agents_md so all files should be present.

    Checks:
      1. openclaw.json readable, agentToAgent.allow present and contains TC agents
      2. workspace-{id}/ directory exists for each agent
      3. AGENTS.md exists and contains 'sessions_send'
      4. state.md exists
      5. lnl-mock-external plugin directory exists

    Returns True if all checks pass.  Always prints a ✓/✗ report via print_fn
    (defaults to tqdm.write if available, else print).
    """
    import json5 as _json5  # already used elsewhere in this module

    if print_fn is None:
        try:
            from tqdm import tqdm as _tqdm
            print_fn = _tqdm.write
        except ImportError:
            print_fn = print

    label = f"[{worker_name}] doctor"
    all_ok = True

    # ── 1. openclaw.json ────────────────────────────────────────────────────
    config_file = openclaw_home / "openclaw.json"
    if config_file.exists():
        try:
            config = _json5.loads(config_file.read_text())
            agent_ids_in_config = {a.get("id") for a in config.get("agents", {}).get("list", [])}
            allow = config.get("tools", {}).get("agentToAgent", {}).get("allow")
            tc_ids = {f"{obj.object_id}{slot_suffix}" for obj in tc_objects}

            config_issues = []
            if allow is None:
                config_issues.append("agentToAgent.allow MISSING — A2A will be blocked")
                all_ok = False
            elif not allow:
                config_issues.append("agentToAgent.allow is empty — A2A will be blocked")
                all_ok = False
            else:
                missing_from_allow = tc_ids - set(allow)
                if missing_from_allow:
                    config_issues.append(
                        f"agents not in allow list: {sorted(missing_from_allow)}"
                    )
                    all_ok = False
            missing_from_list = tc_ids - agent_ids_in_config
            if missing_from_list:
                config_issues.append(
                    f"agents missing from agents.list: {sorted(missing_from_list)}"
                )
                all_ok = False

            if config_issues:
                for issue in config_issues:
                    print_fn(f"  {label} ✗ openclaw.json: {issue}")
            else:
                print_fn(
                    f"  {label} ✓ openclaw.json: {len(agent_ids_in_config)} agents"
                    f", allow={sorted(allow)}"
                )
        except PermissionError:
            print_fn(
                f"  {label} ⚠ openclaw.json: permission denied (container owns it) — cannot verify"
            )
        except Exception as exc:
            print_fn(f"  {label} ✗ openclaw.json: parse error: {exc}")
            all_ok = False
    else:
        print_fn(f"  {label} ✗ openclaw.json: NOT FOUND at {config_file}")
        all_ok = False

    # ── 2-4. Workspace files per agent ──────────────────────────────────────
    for obj in tc_objects:
        oid = f"{obj.object_id}{slot_suffix}"
        ws = openclaw_home / f"workspace-{oid}"
        if not ws.exists():
            print_fn(f"  {label} ✗ workspace-{oid}/: directory MISSING")
            all_ok = False
            continue

        agents_md = ws / "AGENTS.md"
        if not agents_md.exists():
            print_fn(f"  {label} ✗ workspace-{oid}/AGENTS.md: MISSING")
            all_ok = False
        else:
            try:
                content = agents_md.read_text()
                is_leaf = "(No peers defined.)" in content
                if "sessions_send" not in content:
                    if is_leaf:
                        # Leaf agents (no peers) call external tools directly — expected.
                        print_fn(f"  {label} ✓ workspace-{oid}/AGENTS.md: ok (leaf, no peers)")
                    else:
                        print_fn(
                            f"  {label} ✗ workspace-{oid}/AGENTS.md: 'sessions_send' not found"
                            " (peer communication not configured)"
                        )
                        all_ok = False
                else:
                    peer_count = content.count("sessions_send")
                    print_fn(
                        f"  {label} ✓ workspace-{oid}/AGENTS.md: ok"
                        f" (sessions_send ×{peer_count})"
                    )
            except Exception as exc:
                print_fn(f"  {label} ✗ workspace-{oid}/AGENTS.md: read error: {exc}")
                all_ok = False

        state_md = ws / "state.md"
        if not state_md.exists():
            print_fn(f"  {label} ✗ workspace-{oid}/state.md: MISSING")
            all_ok = False
        else:
            size = state_md.stat().st_size
            print_fn(f"  {label} ✓ workspace-{oid}/state.md: {size}B")

    # ── 5. Plugin directory ─────────────────────────────────────────────────
    # The container's entrypoint copies the plugin into the bind-mount at startup.
    # Absence means either the container hasn't finished initializing or the data_dir
    # path doesn't match the container bind-mount (path mismatch bug).
    plugin_dir = openclaw_home / "extensions" / "lnl-mock-external"
    if plugin_dir.exists():
        js_files = list(plugin_dir.glob("*.js"))
        print_fn(f"  {label} ✓ extensions/lnl-mock-external: {len(js_files)} JS file(s)")
    else:
        # Warning only (not a hard failure): plugin is seeded by container entrypoint.
        # If this persists, mock tools (zapier_*, slack_*, etc.) won't be available.
        print_fn(
            f"  {label} ⚠ extensions/lnl-mock-external: MISSING"
            " — mock tools may be unavailable (path mismatch or container still starting)"
        )

    return all_ok


async def _wait_for_gateway_restart(
    gateway_url: Optional[str] = None,
    openclaw_home: Optional[Path] = None,
    drop_timeout_s: float = 10.0,
    ready_timeout_s: float = 30.0,
    stable_for_s: float = 5.0,
) -> None:
    """Wait for a gateway restart to complete: first wait for the connection to
    drop (confirming the old process exited), then wait for the new process to
    accept connections.

    This avoids the race in a plain _wait_for_gateway call: without waiting for
    the drop, the poll immediately reconnects to the still-running old process,
    returns success, and the restart fires mid-run.

    Uses HTTP health checks (fast-fail, 1s timeout) instead of the SDK's
    WebSocket connection, whose internal ``_connect_with_backoff`` retry loop
    masks brief connection drops and defeats the stability detection.
    """
    import httpx as _httpx
    import time as _time

    # Derive HTTP URL from the ws:// gateway URL
    ws_url = gateway_url or "ws://127.0.0.1:18789"
    http_url = ws_url.replace("ws://", "http://").replace("wss://", "https://")
    health_url = f"{http_url}/health"

    # Phase 1 — wait for the connection to drop (restart began)
    deadline = _time.monotonic() + drop_timeout_s
    while _time.monotonic() < deadline:
        try:
            r = await asyncio.get_event_loop().run_in_executor(
                None, lambda: _httpx.get(health_url, timeout=1.0)
            )
            if r.status_code != 200:
                break  # unhealthy — restart in progress
            await asyncio.sleep(0.5)
        except Exception:
            # Connection refused / dropped — restart has begun
            break

    # Phase 2 — wait for the new process to be ready AND stable.
    # Requiring stability prevents returning during a brief lull between
    # back-to-back hot-reload cycles (e.g. startup reload + config-patch reload).
    # http_only=True: gateway requires auth for WS upgrades, so the unauthenticated
    # _probe_ws_connection always fails; HTTP health is sufficient here.
    await _wait_for_gateway(gateway_url, openclaw_home, timeout_s=ready_timeout_s,
                            stable_for_s=stable_for_s, http_only=True)


async def _probe_ws_connection(ws_url: str, timeout: float = 3.0) -> bool:
    """Return True if the WebSocket server is accepting connections right now.

    The HTTP /health endpoint and the WebSocket server can be out of sync during
    hot-reloads: HTTP returns 200 while the WS server is still restarting.  A
    raw WS connect (without auth) confirms the server is actually accepting TCP
    upgrade requests at this instant.
    """
    try:
        from websockets.asyncio.client import connect as _ws_connect
        async with asyncio.timeout(timeout):
            conn = await _ws_connect(ws_url)
            await conn.close()
        return True
    except Exception:
        return False


async def _wait_for_gateway(
    gateway_url: Optional[str] = None,
    openclaw_home: Optional[Path] = None,
    timeout_s: float = 30.0,
    stable_for_s: float = 0.0,
    http_only: bool = False,
) -> None:
    """Poll until the gateway accepts HTTP *and* WebSocket connections, then return.

    If stable_for_s > 0, both must remain reachable for that many consecutive
    seconds before this function returns.  This prevents declaring the gateway
    "ready" during a brief lull between two hot-reload cycles.

    HTTP /health confirms the process is up; a raw WebSocket probe confirms the
    WS server is actually accepting connections — the two can be out of sync
    during hot-reloads (HTTP stays up while WS server restarts).

    Pass http_only=True to skip the WS probe (use when the gateway requires token
    auth for WS upgrades, making unauthenticated _probe_ws_connection always fail).

    Raises:
        asyncio.TimeoutError: if the gateway does not respond within timeout_s.
    """
    import httpx as _httpx
    import time as _time

    # Derive HTTP URL from the ws:// gateway URL (same host/port)
    ws_url = gateway_url or "ws://127.0.0.1:18789"
    http_url = ws_url.replace("ws://", "http://").replace("wss://", "https://")
    health_url = f"{http_url}/health"

    deadline = _time.monotonic() + timeout_s
    stable_since: Optional[float] = None
    while _time.monotonic() < deadline:
        http_ok = False
        ws_ok = False
        try:
            r = await asyncio.get_event_loop().run_in_executor(
                None, lambda: _httpx.get(health_url, timeout=1.0)
            )
            http_ok = r.status_code == 200
        except Exception:
            pass
        if http_ok:
            ws_ok = http_only or await _probe_ws_connection(ws_url, timeout=2.0)
        if http_ok and ws_ok:
            if stable_since is None:
                stable_since = _time.monotonic()
            if stable_for_s <= 0.0 or (_time.monotonic() - stable_since) >= stable_for_s:
                return  # gateway is up (and stable long enough)
        else:
            stable_since = None
        await asyncio.sleep(0.5)
    raise asyncio.TimeoutError(
        f"OpenClaw gateway did not become ready within {timeout_s:.0f}s"
    )


# ── Timestamp parsing ────────────────────────────────────────────────────────

def parse_when(when: str) -> int:
    """Convert 'W02-1T10:30' → ordinal minutes for sorting."""
    week_part, time_part = when.split("T")
    w, d = week_part.lstrip("W").split("-")
    h, m = time_part.split(":")
    return (int(w) * 7 + int(d)) * 1440 + int(h) * 60 + int(m)


# ── Evidence gathering ───────────────────────────────────────────────────────

def gather_evidence(
    content: str,
    tool_calls: Optional[list[dict]] = None,
    state_content: str = "",
) -> str:
    """Collect observable evidence from an OpenClaw agent response.

    Args:
        content: The agent's text response (plain text from OpenClaw).
        tool_calls: Accumulated mock tool call log for this run window.
        state_content: Contents of the agent's state.md after the message,
                       reflecting decisions the agent persisted during execution.
    """
    parts: list[str] = []

    if content.strip():
        parts.append(f"Response:\n{content.strip()}")

    if state_content.strip():
        parts.append(f"Updated state:\n{state_content.strip()}")

    if tool_calls:
        lines = []
        for call in tool_calls:
            if call.get("is_callback"):
                lines.append(f"  - [{call['method']}] {call['result']}")
            else:
                lines.append(f"  - {call['method']}({json.dumps(call.get('args', {}))}) → {call['result']}")
        parts.append("Tool calls:\n" + "\n".join(lines))

    return "\n\n".join(parts) if parts else "(no observable output)"


def state_only_evidence(state_content: str) -> str:
    """Evidence for the memory-fidelity judge: only the persisted state, no tool calls or response."""
    s = state_content.strip()
    return f"Updated state:\n{s}" if s else "(no state recorded)"


# ── OpenClaw agent wrapper ───────────────────────────────────────────────────

class OpenClawAgent:
    """Holds metadata for an OpenClaw agent used during evaluation.

    Actual session management is done by _execute_tc_async, which opens
    persistent connections for all agents simultaneously. This class exists
    to carry agent_id and gateway_url into the execution path, and to maintain
    the global counter that ensures unique session names across TC runs.
    """

    # Global counter ensures session names are unique across all TCs in a run,
    # preventing the gateway from reusing a cached session from a previous TC.
    _global_counter: int = 0

    def __init__(self, agent_id: str, gateway_url: Optional[str] = None):
        self._agent_id = agent_id
        self._gateway_url = gateway_url


# ── Cascade wait helper ───────────────────────────────────────────────────────

async def _wait_mock_quiescence(
    mock_server: "MockServer",
    max_wait_s: float = 10.0,
    quiet_s: float = 1.5,
    slot_id: str = "default",
) -> None:
    """Poll mock server log until no new tool calls arrive for quiet_s seconds.

    Used after execute() returns to wait for agentToAgent cascade completions.
    Since get_log() is non-destructive (configure() clears the log, not get_log()),
    we can poll repeatedly to detect when the call count stabilises.

    Args:
        slot_id: Concurrent slot to poll (default "default" for non-concurrent runs).
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + max_wait_s
    last_count = len(mock_server.get_log(slot_id=slot_id))
    quiet_since = loop.time()

    while True:
        await asyncio.sleep(0.3)
        now = loop.time()
        if now >= deadline:
            break
        current_count = len(mock_server.get_log(slot_id=slot_id))
        if current_count > last_count:
            last_count = current_count
            quiet_since = now
        elif now - quiet_since >= quiet_s:
            break  # no new calls for quiet_s — cascade complete


# ── Prior context ────────────────────────────────────────────────────────────

def _read_prior_context(
    tc: Sample,
    openclaw_home: Path,
    single_agent_id: Optional[str] = None,
) -> str:
    """Read state.md for all objects as prior-state context for the judge.

    Equivalent to evaluate.py's _format_prior_state(rt): gives the judge a
    snapshot of what each agent knew before the current event fired.
    In single-agent mode, reads from the combined workspace.
    """
    lines = ["=== PRIOR STATE ==="]
    if single_agent_id:
        state_file = openclaw_home / f"workspace-{single_agent_id}" / "state.md"
        if state_file.exists():
            text = state_file.read_text().strip()
            if text:
                lines.append(text)
    else:
        for obj in tc.objects:
            ws = openclaw_home / f"workspace-{obj.object_id}"
            state_file = ws / "state.md"
            if state_file.exists():
                text = state_file.read_text().strip()
                if text:
                    lines.append(f"[{obj.object_id}]:\n{text}")
    return "\n\n".join(lines)


# ── Tool trigger matching ────────────────────────────────────────────────────

def _tool_call_matches(match: dict[str, str], args: dict) -> bool:
    """Return True if all match conditions pass (empty dict always passes)."""
    for key, pattern in match.items():
        if not re.search(pattern, str(args.get(key, "")), re.IGNORECASE):
            return False
    return True


def _session_tok(s: dict) -> tuple[int, int]:
    """Extract (inputTokens, outputTokens) from a sessions.list entry.

    Sessions still in "running" state only have totalTokens (no inputTokens/
    outputTokens).  Fall back to totalTokens as an approximation so the delta
    is non-zero rather than silently 0.
    """
    in_tok = s.get("inputTokens")
    out_tok = s.get("outputTokens")
    if in_tok is None:
        in_tok = s.get("totalTokens", 0)
        out_tok = 0
    return (in_tok or 0, out_tok or 0)


async def _snapshot_session_tokens(
    gateway: Any,
    session_keys: Optional[list[str]] = None,
) -> dict[str, tuple[int, int]]:
    """Call sessions.list and return {key: (inputTokens, outputTokens)}.

    If session_keys is None, returns all live sessions (needed to capture downstream
    agentToAgent sessions whose names aren't known before execution).
    """
    try:
        result = await gateway.call("sessions.list", {})
        sessions = result.get("sessions", [])
        if session_keys is None:
            return {s.get("key", ""): _session_tok(s) for s in sessions}
        sess_map = {s.get("key", ""): s for s in sessions}
        return {k: _session_tok(sess_map[k]) if k in sess_map else (0, 0) for k in session_keys}
    except Exception:
        return {}


def _delta_tokens(
    before: dict[str, tuple[int, int]],
    after: dict[str, tuple[int, int]],
) -> tuple[int, int]:
    """Sum the token delta across all sessions."""
    total_in = total_out = 0
    for key, (after_in, after_out) in after.items():
        before_in, before_out = before.get(key, (0, 0))
        total_in += max(0, after_in - before_in)
        total_out += max(0, after_out - before_out)
    return total_in, total_out


# ── Core execution ───────────────────────────────────────────────────────────

async def _execute_tc_async(
    tc: Sample,
    gateway_url: Optional[str],
    openclaw_home: Path,
    harness,
    mock_server: Optional["MockServer"],
    verbose: bool,
    steps_only: bool,
    single_agent_id: Optional[str],
    partial_events: Optional[list],
    partial_mods: Optional[list],
    slot_suffix: str = "",
    max_modifications: Optional[int] = None,
    event_concurrency: int = 0,
    concurrency_seed: int = 42,
    tracked_harness=None,
    thinking: Optional[str] = None,
    sequential: bool = False,
    peer_message_timeout: float = 0.0,
    worker_name: Optional[str] = None,
    run_doctor: bool = False,
) -> tuple[list[EventResult], list[ModificationResult]]:
    """Async core: open persistent sessions for ALL agents simultaneously, then send messages.

    Persistent sessions fix the agentToAgent label-collision problem.
    Previously each agent.execute() call created a brand-new session that tried
    to claim label=agent_id. In sequential evaluation the target agent had no
    active session when the source forwarded via agentToAgent, and stale sessions
    from the previous message caused 'label already in use' errors.

    With all sessions open at once the gateway can route agentToAgent to any peer
    without collision — they're all live for the duration of this TC run.
    """
    from openclaw_sdk import OpenClawClient
    from openclaw_sdk.core.config import ExecutionOptions
    from openclaw_sdk.core.exceptions import GatewayError as _OcGatewayError
    from src.lnl.openclaw_export import rewrite_agents_md

    _oc_thinking = {"disabled": "off", "enabled": "enabled"}.get(thinking, thinking) if thinking else None
    _exec_opts = ExecutionOptions(thinking=_oc_thinking) if _oc_thinking is not None else None

    async def _extract_tool_calls_from_session(
        gateway: Any,
        session_key: str,
        timeout: float = 5.0,
    ) -> list[str]:
        """Return the ordered list of tool names invoked in this session.

        Source of truth: `sessions.get` returns the full message history;
        each assistant turn's content blocks include `{"type": "toolCall",
        "name": "<tool>", "arguments": {...}}` for every tool the LLM
        called. This is the only reliable inventory:

        - ExecutionResult.tool_calls misses sessions_send (the SDK pairs
          TOOL_CALL/TOOL_RESULT events, and sessions_send results are
          delivered via the peer session, not the entry's stream).
        - Callback handlers (on_tool_call, on_stream_event) similarly
          don't receive tool-call events from the real gateway path —
          verified empirically against a live worker: the gateway emits
          `agent` (assistant deltas) and `chat` events only, no
          `stream="tool"` payloads for sessions_send or file ops.

        The session-history API is the authoritative record (it's what
        the LLM provider returned), so we read from there.
        """
        try:
            sess = await asyncio.wait_for(
                gateway.call("sessions.get", {"key": session_key}),
                timeout=timeout,
            )
        except (asyncio.TimeoutError, _OcGatewayError, Exception):
            return []
        names: list[str] = []
        for msg in (sess.get("messages") or []):
            if msg.get("role") != "assistant":
                continue
            for block in (msg.get("content") or []):
                if isinstance(block, dict) and block.get("type") == "toolCall":
                    name = block.get("name") or ""
                    if name:
                        names.append(name)
        return names

    async def _extract_peer_tool_calls(
        gateway: Any,
        tc_objects: list,
        entry_object_id: str,
        slot_suffix: str,
        per_call_timeout: float = 3.0,
    ) -> dict[str, list[str]]:
        """Return {peer_object_id: [tool, tool, ...]} for every non-entry agent.

        Peers use `:main` session names (the gateway's mainKey, reset at the
        start of each event by _make_event_handles). Extraction happens after
        the entry's execute() returns and any cascade has settled. An empty
        list means the peer's :main session has no assistant tool-calls for
        this event — either it was never reached or it processed without
        emitting any tool calls.

        Skipped sessions (peer never existed, gateway error, timeout) are
        absent from the returned dict — distinguishable from "session existed
        but no tool calls" (present with empty list).
        """
        if not tc_objects:
            return {}
        peers = [o for o in tc_objects if o.object_id != entry_object_id]
        # Issue all sessions.get() calls concurrently — each is cheap (~5ms)
        # but doing them serially adds up across 5-10 peers per event.
        async def _one(peer):
            key = f"agent:{peer.object_id}{slot_suffix}:main"
            try:
                tools = await _extract_tool_calls_from_session(gateway, key, timeout=per_call_timeout)
                return peer.object_id, tools
            except Exception:
                return peer.object_id, None
        results = await asyncio.gather(*[_one(p) for p in peers], return_exceptions=False)
        return {pid: tools for pid, tools in results if tools is not None}

    # Reset state before run.
    if single_agent_id:
        # Steps-based single-agent mode: pass the Sample (uses tc.steps), not tc.objects.
        reset_single_agent_state(tc, openclaw_home, single_agent_id)
    else:
        for obj in tc.objects:
            reset_agent_state(f"{obj.object_id}{slot_suffix}", obj.state_description, openclaw_home)
        # Refresh AGENTS.md so the embedded sessions_send timeoutSeconds matches the
        # runtime --peer-message-timeout value. Peer session keys stay as :main
        # (unchanged from export time); only the prompt's timeoutSeconds literal
        # and surrounding fire-and-forget wording are rewritten.
        rewrite_agents_md(
            tc.objects, openclaw_home,
            session_name="main",
            slot_suffix=slot_suffix,
            peer_message_timeout=peer_message_timeout,
        )

    # Doctor check: verify agent files and gateway config are correctly set up.
    if run_doctor and not single_agent_id:
        _pool_doctor_check(
            openclaw_home, tc.objects,
            slot_suffix=slot_suffix,
            worker_name=worker_name or "worker",
        )

    # Multi-agent: session names are generated PER-EVENT (not per-run) so each
    # webhook trigger starts with a clean session.  State persists across events
    # via state.md files; session conversation history does NOT carry over.
    # run_session_name is unused in multi-agent mode — see _make_event_handles().
    run_session_name = None

    # Build ordered message list (identical logic to the old sync version)
    messages: list[dict[str, Any]] = []
    base_events = [e for e in tc.events if e.role == "base"]
    for i, step in enumerate(base_events):
        messages.append({
            "kind": "step",
            "index": i,
            "target": step.recipient,
            "content": f"[Event from {step.source}]: {step.input}",
            "expect": step.expect,
        })

    active_mods = tc.modifications[:max_modifications] if max_modifications is not None else tc.modifications
    allowed_mod_ids: set[str] = {m.id for m in active_mods}

    # Build concurrent group map (event_concurrency > 0 only).
    # Concurrent events are dispatched as batches around mods, not in the main timeline.
    group_map: dict[str, list] = {}
    if event_concurrency > 0:
        for evt in tc.events:
            if evt.role == "base":
                continue
            if evt.concurrent_group:
                group_map.setdefault(evt.concurrent_group, []).append(evt)

    timeline: list[tuple[int, str, Any]] = []
    for mod in active_mods:
        timeline.append((parse_when(mod.when), "mod", mod))
    for evt in tc.events:
        if evt.role == "base":
            continue
        if all(mid in allowed_mod_ids for mid in (evt.after_mod_ids or [])):
            if not evt.concurrent_group or event_concurrency == 0:
                timeline.append((parse_when(evt.when), "event", evt))
    timeline.sort(key=lambda x: x[0])

    for _, kind, item in timeline:
        if kind == "mod":
            messages.append({
                "kind": "mod",
                "item": item,
                "target": item.target,
                "content": f"[Administrative instruction at {item.when}]: {item.intent}",
            })
        else:
            messages.append({
                "kind": "event",
                "item": item,
                "target": item.recipient,
                "content": f"[Event from {item.source} at {item.when}]: {item.input}",
            })

    event_results: list[EventResult] = partial_events if partial_events is not None else []
    mod_results: list[ModificationResult] = partial_mods if partial_mods is not None else []
    prior_context: str = ""

    async def _make_event_handles(
        client: Any,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """Create fresh handles + session name for one event.

        Entry agent uses a unique session name per event to prevent conversation
        history from bleeding across events. Peer agents always use "main" — the
        gateway's default mainKey that it auto-creates on demand when sessions_send
        targets them. Custom session names (e.g. "eval-ma-6") do NOT get
        auto-created and cause "pairing required" errors.

        State persists across events via state.md; conversation history is reset
        per event for peers via sessions.reset on their "main" session key.
        """
        OpenClawAgent._global_counter += 1
        sname = f"eval-ma-{OpenClawAgent._global_counter}"
        # AGENTS.md was already rewritten once at the top of this TC run to refresh
        # the embedded sessions_send timeoutSeconds. Peer session keys are :main
        # and don't change per-event; only the entry agent uses sname, managed by
        # our Python client handle (not AGENTS.md).

        ev_handles: dict[str, Any] = {}
        ev_session_names: dict[str, str] = {}
        for obj in tc.objects:
            agent_id = f"{obj.object_id}{slot_suffix}"
            ev_session_names[obj.object_id] = sname
            ev_handles[obj.object_id] = client.get_agent(agent_id, session_name=sname)

        # Write stub BOOTSTRAP.md + populated IDENTITY.md so the gateway never
        # triggers its onboarding flow (empty IDENTITY.md Name → BOOTSTRAP.md).
        _stub = _bootstrap_stub_md()
        for obj in tc.objects:
            ws_path = openclaw_home / f"workspace-{obj.object_id}{slot_suffix}"
            (ws_path / "BOOTSTRAP.md").write_text(_stub)
            identity_path = ws_path / "IDENTITY.md"
            if not identity_path.exists() or "Name:" not in identity_path.read_text():
                identity_path.write_text(_identity_md(obj))

        # Reset peer agents' "main" sessions so each event starts with a clean
        # conversation history. sessions.reset clears history without deleting
        # the session key, keeping it routable for incoming sessions_send calls.
        for obj in tc.objects:
            peer_key = f"agent:{obj.object_id}{slot_suffix}:main"
            try:
                await client.gateway.call("sessions.reset", {"key": peer_key})
            except Exception:
                pass  # session may not exist yet — gateway creates on first sessions_send

        return ev_handles, ev_session_names

    # ── One persistent connection for the whole TC run ────────────────────────
    # Bound connect time: the SDK's _connect_with_backoff retries ConnectionRefusedError
    # indefinitely (up to 30s between attempts), so if the gateway is down it can hang
    # for hundreds of seconds before the 900s wall-clock timeout fires.  A 30s limit
    # here causes a fast GatewayError → caught by the gateway retry loop → _ensure_worker_healthy.
    _connect_timeout_s = 30.0
    try:
        _oc_client = await asyncio.wait_for(
            OpenClawClient.connect(**_openclaw_connect_kwargs(gateway_url, openclaw_home)),
            timeout=_connect_timeout_s,
        )
    except asyncio.TimeoutError:
        raise _OcGatewayError(
            f"OpenClaw client did not connect within {_connect_timeout_s:.0f}s (gateway may be reloading)"
        )
    async with _oc_client as client:

        # Single-agent: one session for the whole run (no per-event refresh needed).
        if single_agent_id:
            OpenClawAgent._global_counter += 1
            sname = f"eval-{single_agent_id}-{OpenClawAgent._global_counter}"
            handles: dict[str, Any] = {single_agent_id: client.get_agent(single_agent_id, session_name=sname)}
            session_names: dict[str, str] = {single_agent_id: sname}

        async def _dispatch_concurrent_group(group: list, ctx: str, group_key: str = "") -> None:
            """Fire events in `group` simultaneously; append EventResults to event_results."""
            if not group:
                return

            import random as _random
            if event_concurrency >= len(group):
                batch = list(group)
            else:
                rng = _random.Random(f"{concurrency_seed}:{tc.id}:{group_key}")
                batch = rng.sample(group, event_concurrency)

            async def _one(evt):
                if not single_agent_id:
                    # Multi-agent: fresh session per event for true concurrency.
                    evt_handles, _ = await _make_event_handles(client)
                else:
                    # Single-agent: fresh session per concurrent event so parallel
                    # calls don't race on a shared WebSocket connection.
                    OpenClawAgent._global_counter += 1
                    sname = f"eval-{single_agent_id}-{OpenClawAgent._global_counter}"
                    evt_handles = {single_agent_id: client.get_agent(single_agent_id, session_name=sname)}
                lookup = single_agent_id or evt.recipient
                h = evt_handles.get(lookup)
                if h is None:
                    return evt, None, 0.0
                content = f"[Event from {evt.source} at {evt.when}]: {evt.input}"
                t_evt = time.time()
                try:
                    res = await h.execute(content, options=_exec_opts)
                    return evt, res, (time.time() - t_evt) * 1000
                except Exception as exc:
                    return evt, exc, (time.time() - t_evt) * 1000

            t0 = time.time()
            if sequential:
                pairs = []
                for e in batch:
                    pairs.append(await _one(e))
            else:
                pairs = await asyncio.gather(*[_one(e) for e in batch])
            lat_ms = (time.time() - t0) * 1000

            if mock_server:
                await _wait_mock_quiescence(mock_server, max_wait_s=INTER_EVENT_TIMEOUT_S, quiet_s=3.0,
                                            slot_id=slot_suffix or "default")
                batch_tool_calls = mock_server.get_log(slot_id=slot_suffix or "default")
            else:
                await asyncio.sleep(0.5)
                batch_tool_calls = []

            if single_agent_id:
                sf = openclaw_home / f"workspace-{single_agent_id}" / "state.md"
                batch_state = sf.read_text().strip() if sf.exists() else ""
            else:
                parts = []
                for obj in tc.objects:
                    sf = openclaw_home / f"workspace-{obj.object_id}{slot_suffix}" / "state.md"
                    if sf.exists():
                        text = sf.read_text().strip()
                        if text:
                            parts.append(f"[{obj.object_id}]:\n{text}")
                batch_state = "\n\n".join(parts)

            for evt, res, evt_lat_ms in pairs:
                # In single-agent mode evt_lat_ms is the individual call time.
                # In multi-agent mode all calls ran simultaneously so use batch time.
                display_lat = evt_lat_ms if single_agent_id else lat_ms
                if evt.expect is None:
                    continue
                if isinstance(res, Exception) or res is None:
                    event_results.append(EventResult(
                        event_id=evt.id, passed=False, reasoning=f"Dispatch error: {res}",
                        role=evt.role, latency_ms=display_lat,
                    ))
                    continue
                agent_out = res.content if res.success else f"(error: {res.content})"
                _active_harness = (
                    tracked_harness
                    if tracked_harness is not None and evt.role == "irrelevant"
                    else harness
                )
                if _active_harness is tracked_harness:
                    evidence = state_only_evidence(batch_state)
                else:
                    evidence = gather_evidence(
                        agent_out,
                        tool_calls=batch_tool_calls if mock_server else None,
                        state_content=batch_state,
                    )
                passed, reasoning, _votes, _in_tok, _out_tok = _active_harness.evaluate_assertion(
                    evt.expect.action, evidence, ctx)
                tqdm.write(f"    {evt.id} {'✓' if passed else '✗'} {display_lat/1000:.1f}s [conc]  {reasoning[:100]}")
                # Entry-point token usage only — concurrent events run simultaneously so
                # session-delta tokens can't be attributed to individual events.
                _conc_in_tok = getattr(res.token_usage, "input", 0) or 0
                _conc_out_tok = getattr(res.token_usage, "output", 0) or 0
                event_results.append(EventResult(
                    event_id=evt.id, passed=passed, reasoning=reasoning,
                    expected=evt.expect.action, evidence=evidence,
                    prior_context=ctx, latency_ms=display_lat,
                    role=evt.role,
                    input_tokens=_conc_in_tok, output_tokens=_conc_out_tok,
                    judge_input_tokens=_in_tok, judge_output_tokens=_out_tok,
                    judge_votes=_votes,
                ))

        # ── Pre-warm peer agents (pool multi-agent only) ─────────────────────
        # Peer agents start on-demand (skipBootstrap=true default): they are
        # not yet running when the entry agent fires its first sessions_send.
        # The gateway returns "pairing required" because the peer process has
        # not yet connected.  Fix: send a brief warmup to every TC agent in
        # parallel so they're all paired before the entry agent starts.
        # Cost: one LLM call per agent per TC; session history is cleared by
        # _make_event_handles → sessions.reset("agent:{id}:main") per event.
        if not single_agent_id and run_doctor:
            # Serialize: parallel gather() overwhelms the gateway during the
            # first wake (each first-execute triggers an agent process spawn +
            # WS pairing; doing N concurrently was killing workers). One at a
            # time, short timeout, swallow errors — peer may still pair on the
            # real sessions_send if this warmup fails.
            for _obj in tc.objects:
                _agent_id = f"{_obj.object_id}{slot_suffix}"
                try:
                    _h = client.get_agent(_agent_id, session_name="main")
                    await asyncio.wait_for(
                        _h.execute("Initialization check. Reply with 'Ready' only."),
                        timeout=20.0,
                    )
                except Exception:
                    pass
                await asyncio.sleep(0.5)  # let the gateway settle between spawns

        for msg in messages:
            if steps_only and msg["kind"] != "step":
                break

            # Fire pre-mod concurrent group before dispatching the modification.
            if msg["kind"] == "mod" and event_concurrency > 0:
                mod_id = msg["item"].id
                pre_key = f"cgroup_pre_{mod_id}"
                await _dispatch_concurrent_group(group_map.get(pre_key, []), prior_context, pre_key)

            target_id = msg["target"]

            # Multi-agent: fresh handles per event so each trigger starts with a
            # clean session.  (Single-agent reuses the same handle throughout.)
            if not single_agent_id:
                handles, session_names = await _make_event_handles(client)

            lookup_id = single_agent_id if single_agent_id else target_id
            handle = handles.get(lookup_id)
            if handle is None:
                if verbose:
                    tqdm.write(f"  Warning: no handle for target {lookup_id!r}, skipping")
                continue

            # Configure mock server session key (also clears the per-message log).
            # The full OpenClaw session key is agent:{agent_id}:{session_name}.
            if mock_server:
                sname = session_names[lookup_id]
                agent_id_for_key = f"{lookup_id}{slot_suffix}" if not single_agent_id else lookup_id
                mock_key = f"agent:{agent_id_for_key}:{sname}"
                mock_server.configure(mock_key, slot_id=slot_suffix or "default")

            # Snapshot all live sessions before execution so the delta after
            # quiescence captures downstream agentToAgent tokens too.
            tok_snap_before = await _snapshot_session_tokens(client.gateway)

            t0 = time.time()
            result = await handle.execute(msg["content"], options=_exec_opts)
            latency_ms = (time.time() - t0) * 1000
            # Distinguish infra failure (SDK returned success=False — rate-limit,
            # aborted, no response) from real agent reasoning failure. The SDK's
            # error_message has the diagnostic text (e.g. "Agent completed with no
            # response — the LLM may be rate-limited"); stop_reason is one of
            # "complete" / "aborted" / "error" / "timeout". Both were previously
            # dropped, masking infra failures as regular pass-rate failures.
            _result_err_message = getattr(result, "error_message", None) or ""
            _result_stop_reason = getattr(result, "stop_reason", None) or ""
            _peer_infra_error = not result.success
            if _peer_infra_error:
                _err_label = (
                    _result_err_message
                    or f"agent execution failed (stop_reason={_result_stop_reason or 'unknown'})"
                )
                tqdm.write(f"  [AGENT INFRA ERROR] {target_id}: {_err_label}", file=sys.stderr)
                content = f"(infra_error: {_err_label})"
            else:
                content = result.content

            # Entry-agent chattiness metrics — sourced from sessions.get because
            # both ExecutionResult.tool_calls and SDK callbacks fail to surface
            # tool calls on the real-gateway path (verified empirically against a
            # live worker). The session-history API returns the complete
            # assistant-turn content blocks, including {type: "toolCall", name,
            # arguments} entries for every tool the LLM invoked. This is the
            # authoritative record.
            _entry_session_key = (
                f"agent:{single_agent_id}:{session_names[lookup_id]}"
                if single_agent_id else
                f"agent:{lookup_id}{slot_suffix}:{session_names[lookup_id]}"
            )
            _entry_tool_names = await _extract_tool_calls_from_session(
                client.gateway, _entry_session_key,
            )
            _agent_tool_calls = len(_entry_tool_names)
            _a2a_calls = sum(1 for name in _entry_tool_names if name == "sessions_send")
            # Per-peer tool capture: lets us see what each peer in the cascade
            # actually did (none, simple write, further sessions_send to deeper
            # peer, errored, etc). Skipped in single-agent mode (no peers).
            if single_agent_id:
                _peer_tool_names: dict[str, list[str]] = {}
            else:
                _peer_tool_names = await _extract_peer_tool_calls(
                    client.gateway, tc.objects, lookup_id, slot_suffix,
                )
            _mock_tool_calls = 0  # updated below if mock_server is active
            if verbose and _entry_tool_names:
                tqdm.write(f"  [tool-calls] {target_id}: {_entry_tool_names[:6]}{'...' if len(_entry_tool_names)>6 else ''}")
                if _peer_tool_names:
                    non_empty_peers = {k: v for k, v in _peer_tool_names.items() if v}
                    tqdm.write(f"  [peer-calls] {len(non_empty_peers)}/{len(_peer_tool_names)} peers acted: {dict(list(non_empty_peers.items())[:3])}")
            elif verbose and result.success:
                tqdm.write(f"  [tool-calls] {target_id}: (none captured — session history empty)")

            # Collect tool calls for this event only
            event_tool_calls: list[dict] = []

            if mock_server:
                if single_agent_id:
                    # Single-agent: no agentToAgent chains, short wait suffices
                    await asyncio.sleep(0.5)
                else:
                    # Multi-agent: wait for any agentToAgent cascade to complete.
                    # With sessions_send(timeout=300s) per hop, cascades can still
                    # be running after execute() returns — give them extra time.
                    await _wait_mock_quiescence(mock_server, max_wait_s=INTER_EVENT_TIMEOUT_S, quiet_s=3.0,
                                                slot_id=slot_suffix or "default")
                event_tool_calls = mock_server.get_log(slot_id=slot_suffix or "default")
                _mock_tool_calls = len(event_tool_calls)
            else:
                await asyncio.sleep(0.5)

            # Token usage: delta across all sessions captures both the entry-point agent
            # and any downstream agents invoked via agentToAgent. More accurate than
            # result.token_usage which only covers the entry-point session.
            tok_snap_after = await _snapshot_session_tokens(client.gateway)
            _agent_in_tok, _agent_out_tok = _delta_tokens(tok_snap_before, tok_snap_after)

            # Read post-execution state(s)
            # Quiescence ensures the cascade is complete; state.md is written synchronously
            # by the agent before execute() returns or before the cascade's execute() returns.
            if single_agent_id:
                sf = openclaw_home / f"workspace-{single_agent_id}" / "state.md"
                post_event_state = sf.read_text().strip() if sf.exists() else ""
            else:
                state_parts = []
                for obj in tc.objects:
                    sf = openclaw_home / f"workspace-{obj.object_id}{slot_suffix}" / "state.md"
                    if sf.exists():
                        text = sf.read_text().strip()
                        if text:
                            state_parts.append(f"[{obj.object_id}]:\n{text}")
                post_event_state = "\n\n".join(state_parts)

            if verbose:
                kind_label = {"step": "STEP", "mod": "MOD", "event": "EVENT"}.get(msg["kind"], "?")
                route_label = f"{target_id}→{single_agent_id}" if single_agent_id else target_id
                tqdm.write(f"\n{'─'*60}")
                tqdm.write(f"[{kind_label}→{route_label}] {msg['content'][:120]}")
                tqdm.write(f"  Agent: {content[:300]}")
                if post_event_state:
                    tqdm.write(f"  State: {post_event_state[:200]}")

            if msg["kind"] == "step":
                step_id = f"S{msg['index']+1:03d}"
                expect = msg["expect"]
                if expect is not None:
                    evidence = gather_evidence(
                        content,
                        tool_calls=event_tool_calls if mock_server else None,
                        state_content=post_event_state,
                    )
                    passed, reasoning, _votes, _in_tok, _out_tok = harness.evaluate_assertion(
                        expect.action, evidence, prior_context)
                    _failure_class = _classify_failure(
                        success=result.success, passed=passed,
                        error_message=_result_err_message,
                        stop_reason=_result_stop_reason,
                        reasoning=reasoning,
                    )
                    tqdm.write(f"    {step_id} {'✓' if passed else '✗'} {latency_ms/1000:.1f}s  {reasoning[:120]}")
                    if verbose:
                        tqdm.write(f"  Expected: {expect.action}")
                        tqdm.write(f"  {'✓ PASS' if passed else '✗ FAIL'}: {reasoning[:200]}")
                        if _failure_class:
                            tqdm.write(f"  [failure_class] {_failure_class}")
                    event_results.append(EventResult(
                        event_id=step_id,
                        passed=passed, reasoning=reasoning,
                        expected=expect.action, evidence=evidence,
                        prior_context=prior_context, latency_ms=latency_ms,
                        input_tokens=_agent_in_tok, output_tokens=_agent_out_tok,
                        judge_input_tokens=_in_tok, judge_output_tokens=_out_tok,
                        judge_votes=_votes,
                        agent_tool_calls=_agent_tool_calls,
                        a2a_calls=_a2a_calls,
                        entry_tool_names=_entry_tool_names,
                        peer_tool_names=_peer_tool_names,
                        mock_tool_calls=_mock_tool_calls,
                        infra_error=_failure_class in ("oc_eval", "infra_provider"),
                        failure_class=_failure_class,
                    ))
                prior_context = _read_prior_context(tc, openclaw_home, single_agent_id)

            elif msg["kind"] == "mod":
                mod = msg["item"]
                tag = f"{mod.mod_type.value}/{mod.ambiguity.value}"
                tqdm.write(f"    ── [{tag}] {mod.id} {latency_ms/1000:.1f}s  {mod.intent[:70]}")
                mod_results.append(ModificationResult(mod_id=mod.id, latency_ms=latency_ms))
                prior_context = _read_prior_context(tc, openclaw_home, single_agent_id)
                # Fire post-mod concurrent group after mod settles.
                if event_concurrency > 0:
                    post_key = f"cgroup_post_{mod.id}"
                    await _dispatch_concurrent_group(group_map.get(post_key, []), prior_context, post_key)

            else:  # event
                item = msg["item"]
                if item.expect is not None:
                    _active_harness = (
                        tracked_harness
                        if tracked_harness is not None and item.role == "irrelevant"
                        else harness
                    )
                    if _active_harness is tracked_harness:
                        evidence = state_only_evidence(post_event_state)
                    else:
                        evidence = gather_evidence(
                            content,
                            tool_calls=event_tool_calls if mock_server else None,
                            state_content=post_event_state,
                        )
                    passed, reasoning, _votes, _in_tok, _out_tok = _active_harness.evaluate_assertion(
                        item.expect.action, evidence, prior_context)
                    _failure_class = _classify_failure(
                        success=result.success, passed=passed,
                        error_message=_result_err_message,
                        stop_reason=_result_stop_reason,
                        reasoning=reasoning,
                    )
                    tqdm.write(f"    {item.id} {'✓' if passed else '✗'} {latency_ms/1000:.1f}s  {reasoning[:120]}")
                    if verbose:
                        tqdm.write(f"  Expected: {item.expect.action}")
                        tqdm.write(f"  {'✓ PASS' if passed else '✗ FAIL'}: {reasoning[:200]}")
                        if _failure_class:
                            tqdm.write(f"  [failure_class] {_failure_class}")
                    event_results.append(EventResult(
                        event_id=item.id, passed=passed, reasoning=reasoning,
                        expected=item.expect.action, evidence=evidence,
                        prior_context=prior_context, latency_ms=latency_ms,
                        role=item.role,
                        input_tokens=_agent_in_tok, output_tokens=_agent_out_tok,
                        judge_input_tokens=_in_tok, judge_output_tokens=_out_tok,
                        judge_votes=_votes,
                        agent_tool_calls=_agent_tool_calls,
                        a2a_calls=_a2a_calls,
                        entry_tool_names=_entry_tool_names,
                        peer_tool_names=_peer_tool_names,
                        mock_tool_calls=_mock_tool_calls,
                        infra_error=_failure_class in ("oc_eval", "infra_provider"),
                        failure_class=_failure_class,
                    ))
                prior_context = _read_prior_context(tc, openclaw_home, single_agent_id)

    return event_results, mod_results


def _execute_test_case_inner(
    tc: Sample,
    agents: dict[str, OpenClawAgent],
    openclaw_home: Path,
    harness,
    mock_server: Optional["MockServer"] = None,
    verbose: bool = False,
    steps_only: bool = False,
    single_agent_id: Optional[str] = None,
    _partial_events: Optional[list] = None,
    _partial_mods: Optional[list] = None,
    slot_suffix: str = "",
    max_modifications: Optional[int] = None,
    event_concurrency: int = 0,
    concurrency_seed: int = 42,
    tracked_harness=None,
    thinking: Optional[str] = None,
    sequential: bool = False,
    peer_message_timeout: float = 0.0,
) -> tuple[list[EventResult], list[ModificationResult]]:
    """Sync wrapper: delegate to _execute_tc_async with persistent multi-agent sessions."""
    gateway_url = next(iter(agents.values()))._gateway_url if agents else None
    return asyncio.run(_execute_tc_async(
        tc, gateway_url, openclaw_home, harness,
        mock_server, verbose, steps_only, single_agent_id,
        _partial_events, _partial_mods,
        slot_suffix=slot_suffix,
        max_modifications=max_modifications,
        event_concurrency=event_concurrency,
        concurrency_seed=concurrency_seed,
        tracked_harness=tracked_harness,
        thinking=thinking,
        sequential=sequential,
        peer_message_timeout=peer_message_timeout,
    ))


def execute_test_case(
    tc: Sample,
    agents: dict[str, OpenClawAgent],
    openclaw_home: Path,
    harness,
    timeout_s: Optional[float] = None,
    mock_server: Optional[MockServer] = None,
    verbose: bool = False,
    steps_only: bool = False,
    single_agent_id: Optional[str] = None,
    slot_suffix: str = "",
    max_modifications: Optional[int] = None,
    event_concurrency: int = 0,
    concurrency_seed: int = 42,
    tracked_harness=None,
    thinking: Optional[str] = None,
    sequential: bool = False,
    peer_message_timeout: float = 0.0,
) -> tuple[list[EventResult], list[ModificationResult]]:
    """Run a single Sample with an optional wall-clock timeout."""
    if timeout_s is None:
        return _execute_test_case_inner(tc, agents, openclaw_home, harness,
                                        mock_server=mock_server, verbose=verbose,
                                        steps_only=steps_only,
                                        single_agent_id=single_agent_id,
                                        slot_suffix=slot_suffix,
                                        max_modifications=max_modifications,
                                        event_concurrency=event_concurrency,
                                        concurrency_seed=concurrency_seed,
                                        tracked_harness=tracked_harness,
                                        thinking=thinking,
                                        sequential=sequential,
                                        peer_message_timeout=peer_message_timeout)

    partial_events: list[EventResult] = []
    partial_mods: list[ModificationResult] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            _execute_test_case_inner, tc, agents, openclaw_home,
            harness, mock_server, verbose, steps_only, single_agent_id,
            partial_events, partial_mods, slot_suffix, max_modifications,
            event_concurrency, concurrency_seed, tracked_harness, thinking,
            sequential, peer_message_timeout,
        )
        try:
            return future.result(timeout=timeout_s)
        except concurrent.futures.TimeoutError:
            future.cancel()
            # Preserve whatever was completed; mark the rest as timed-out failures.
            # Note: future.cancel() doesn't stop a running thread — it may continue
            # appending real results to partial_events after we add placeholders.
            # We snapshot collected IDs now, add placeholders for the rest, then
            # deduplicate keeping the last occurrence of each event_id so real
            # results (appended later by the thread) win over placeholders.
            collected_event_ids = {e.event_id for e in partial_events}
            # TC wall-clock timeout is OUR setting (--timeout, default 900s) so
            # classify these placeholders as oc_eval (factor out of pass-rate).
            for step in [e for e in tc.events if e.role == "base"]:
                eid = step.id
                if eid not in collected_event_ids and step.expect is not None:
                    partial_events.append(EventResult(
                        event_id=eid, passed=False,
                        reasoning=f"Timeout after {timeout_s}s",
                        infra_error=True, failure_class="oc_eval",
                    ))
            _active_mod_ids = {m.id for m in (tc.modifications[:max_modifications] if max_modifications else tc.modifications)}
            for evt in tc.events:
                if evt.id not in collected_event_ids and evt.expect is not None:
                    if all(mid in _active_mod_ids for mid in (evt.after_mod_ids or [])):
                        partial_events.append(EventResult(
                            event_id=evt.id, passed=False,
                            reasoning=f"Timeout after {timeout_s}s",
                            role=getattr(evt, "role", None),
                            infra_error=True, failure_class="oc_eval",
                        ))
            collected_mod_ids = {m.mod_id for m in partial_mods}
            for mod in (tc.modifications[:max_modifications] if max_modifications else tc.modifications):
                if mod.id not in collected_mod_ids:
                    partial_mods.append(ModificationResult(mod_id=mod.id))

            # Deduplicate: last occurrence of each id wins (real result > placeholder)
            seen: dict[str, int] = {}
            for i, e in enumerate(partial_events):
                seen[e.event_id] = i
            deduped = [partial_events[i] for i in sorted(seen.values())]

            seen_mods: dict[str, int] = {}
            for i, m in enumerate(partial_mods):
                seen_mods[m.mod_id] = i
            deduped_mods = [partial_mods[i] for i in sorted(seen_mods.values())]
            return deduped, deduped_mods


# ── Output path ──────────────────────────────────────────────────────────────

def default_output_path(input_path: Path) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    repo_root = Path(__file__).parent.parent.parent
    outputs_root = repo_root / "outputs"
    try:
        rel = input_path.resolve().relative_to(outputs_root.resolve())
        base = outputs_root / rel.parent
    except ValueError:
        try:
            rel = input_path.resolve().relative_to(repo_root.resolve())
            base = outputs_root / rel.parent
        except ValueError:
            base = input_path.parent
    return base / "runs" / f"{input_path.stem}_baseline_{ts}.jsonl"


def _role_elapsed_fields(events: list) -> dict:
    """Sum latency_ms per role from a list of EventResult objects."""
    def _sum(role):
        evts = [e for e in events if e.role == role]
        return sum(e.latency_ms for e in evts) if evts else None
    return dict(
        base_elapsed_ms=_sum(None),
        pre_mod_elapsed_ms=_sum("pre_mod"),
        post_mod_elapsed_ms=_sum("post_mod"),
        irrelevant_elapsed_ms=_sum("irrelevant"),
    )


# ── Summary ──────────────────────────────────────────────────────────────────

_STEP_EVENT_ID = re.compile(r"^S\d+$")


def _running_metrics(results: "list[SampleResult]") -> tuple[Optional[float], Optional[float]]:
    """Return (mean_pass_rate, sample_pass_rate) across accumulated results.

    sample_pass_rate: among base-TC runs (first tc_id per sample_id) that have
    step events, the fraction where ALL step events passed.
    """
    valid = [r.pass_rate for r in results if r.pass_rate is not None]
    mean_pr = sum(valid) / len(valid) if valid else None

    first_tc_per_sample: dict[str, str] = {}
    for r in results:
        sid = r.sample_id or r.tc_id
        if sid not in first_tc_per_sample:
            first_tc_per_sample[sid] = r.tc_id
    base_tc_ids = set(first_tc_per_sample.values())

    attempts = passes = 0
    for r in results:
        if r.tc_id not in base_tc_ids:
            continue
        step_evts = [e for e in r.events if _STEP_EVENT_ID.match(e.event_id)]
        if not step_evts:
            continue
        attempts += 1
        if all(e.passed for e in step_evts):
            passes += 1
    sample_pr = passes / attempts if attempts else None
    return mean_pr, sample_pr


def _pbar_postfix(pbar, results, agent_model: str = None) -> None:
    """Update pbar postfix with running mean + sample pass rates + token counters."""
    if pbar is None:
        return
    mean_pr, sample_pr = _running_metrics(results)
    in_tok  = sum(e.input_tokens  or 0 for r in results for e in r.events)
    out_tok = sum(e.output_tokens or 0 for r in results for e in r.events)
    fields: dict[str, str] = {}
    if mean_pr is not None:
        fields["mean"] = f"{mean_pr:.1%}"
    if sample_pr is not None:
        fields["sample"] = f"{sample_pr:.1%}"
    cost = _compute_cost(in_tok, out_tok, agent_model or "")
    cost_str = f" (${cost:.2f})" if cost is not None else ""
    fields["tok"] = f"{in_tok//1000}k↑{out_tok//1000}k↓{cost_str}"
    pbar.set_postfix(refresh=False, **fields)


def _compute_summary(results: list[SampleResult]) -> EvalSummary:
    """Compute aggregate metrics across all test case results.

    Step events (id matching S\\d+) are deduplicated by sample_id: only the first
    TC variant per sample contributes step results to the summary. All TC variants
    contribute their modification and timeline event results.
    """
    all_mods = [m for r in results for m in r.modifications]
    total_runs = len(results)
    total_test_cases = len({r.tc_id for r in results})

    def mean(vals):
        return sum(vals) / len(vals) if vals else 0.0

    # Identify the first TC seen per sample_id — that TC's step results are canonical.
    first_tc_per_sample: dict[str, str] = {}
    for r in results:
        sid = r.sample_id or r.tc_id
        if sid not in first_tc_per_sample:
            first_tc_per_sample[sid] = r.tc_id
    base_tc_ids = set(first_tc_per_sample.values())

    # TCs where every event hit an infra failure — excluded from all scoring.
    infra_error_tc_ids: set[str] = {
        r.tc_id for r in results
        if r.events and all(e.infra_error for e in r.events)
    }

    all_events: list[EventResult] = []
    pass_rates: list[float] = []
    for r in results:
        if r.tc_id in infra_error_tc_ids:
            continue
        is_base = r.tc_id in base_tc_ids
        effective = [
            e for e in r.events
            if (is_base or not _STEP_EVENT_ID.match(e.event_id))
            and not e.infra_error
        ]
        all_events.extend(effective)
        if effective:
            pass_rates.append(sum(1 for e in effective if e.passed) / len(effective))

    mean_pass_rate = mean(pass_rates) if pass_rates else 0.0

    by_tc: dict[str, list[float]] = defaultdict(list)
    for r in results:
        if r.pass_rate is not None:
            by_tc[r.tc_id].append(r.pass_rate)
    per_tc_means_pr = [mean(v) for v in by_tc.values() if v]
    n_pr = len(per_tc_means_pr)
    if n_pr >= 2:
        try:
            from scipy import stats as _scipy_stats
            _t_crit_pr = float(_scipy_stats.t.ppf(0.975, df=n_pr - 1))
        except ImportError:
            _t_crit_pr = 1.96
        pass_rate_ci95 = _t_crit_pr * statistics.stdev(per_tc_means_pr) / (n_pr ** 0.5)
    else:
        pass_rate_ci95 = None

    def _per_tc_ci95(by_tc_rates: dict) -> Optional[float]:
        """95% CI half-width on the mean, from across-TC variance (Student's t)."""
        tc_means = [mean(v) for v in by_tc_rates.values() if v]
        n = len(tc_means)
        if n < 2:
            return None
        try:
            from scipy import stats as _scipy_stats
            t_crit = float(_scipy_stats.t.ppf(0.975, df=n - 1))
        except ImportError:
            t_crit = 1.96
        return t_crit * statistics.stdev(tc_means) / (n ** 0.5)

    # Steps pass rate + std (base TCs only, mean fraction of steps passed per TC)
    by_tc_step: dict[str, list[float]] = defaultdict(list)
    # Workflows completion + std (fraction of TCs where ALL step events passed)
    by_tc_completion: dict[str, list[float]] = defaultdict(list)
    for r in results:
        if r.tc_id not in base_tc_ids or r.tc_id in infra_error_tc_ids:
            continue
        step_evts = [e for e in r.events if _STEP_EVENT_ID.match(e.event_id) and not e.infra_error]
        if step_evts:
            by_tc_step[r.tc_id].append(sum(1 for e in step_evts if e.passed) / len(step_evts))
            by_tc_completion[r.tc_id].append(1.0 if all(e.passed for e in step_evts) else 0.0)
    steps_pass_rate = mean([mean(v) for v in by_tc_step.values()]) if by_tc_step else None
    steps_pass_rate_ci95 = _per_tc_ci95(by_tc_step)
    samples_completion = mean([mean(v) for v in by_tc_completion.values()]) if by_tc_completion else None
    samples_completion_ci95 = _per_tc_ci95(by_tc_completion)

    # Probe TCs (no pre_mod events) are exempt: step failures there don't invalidate probe metrics.
    tcs_with_pre_mod = {r.tc_id for r in results if any(e.role == "pre_mod" for e in r.events)}
    inconclusive_tc_ids: set[str] = set()
    for r in results:
        if r.tc_id not in tcs_with_pre_mod or r.tc_id in infra_error_tc_ids:
            continue
        if any(_STEP_EVENT_ID.match(e.event_id) and not e.passed and not e.infra_error for e in r.events):
            inconclusive_tc_ids.add(r.tc_id)

    # Role-based pass rates + std: exclude inconclusive TCs, grouped by TC across runs
    def _role_pass_rate_and_ci95(role_val, exclude_inconclusive=True) -> tuple[Optional[float], Optional[float]]:
        by_tc: dict[str, list[float]] = defaultdict(list)
        for r in results:
            if r.tc_id in infra_error_tc_ids:
                continue
            if exclude_inconclusive and r.tc_id in inconclusive_tc_ids:
                continue
            evts = [e for e in r.events if e.role == role_val and not e.infra_error]
            if evts:
                by_tc[r.tc_id].append(sum(1 for e in evts if e.passed) / len(evts))
        rate = mean([mean(v) for v in by_tc.values()]) if by_tc else None
        return rate, _per_tc_ci95(by_tc)

    conclusive_events = [
        e for r in results if r.tc_id not in inconclusive_tc_ids and r.tc_id not in infra_error_tc_ids
        for e in r.events if not e.infra_error
    ]
    mod_events = [e for e in conclusive_events if e.role in ("pre_mod", "post_mod", "irrelevant")]
    mod_pass_rate = (sum(1 for e in mod_events if e.passed) / len(mod_events)) if mod_events else None

    by_tc_mod: dict[str, list[float]] = defaultdict(list)
    for r in results:
        if r.tc_id in inconclusive_tc_ids or r.tc_id in infra_error_tc_ids:
            continue
        evts = [e for e in r.events if e.role in ("pre_mod", "post_mod", "irrelevant") and not e.infra_error]
        if evts:
            by_tc_mod[r.tc_id].append(sum(1 for e in evts if e.passed) / len(evts))
    mod_pass_rate_ci95 = _per_tc_ci95(by_tc_mod)

    pre_mod_pass_rate, pre_mod_pass_rate_ci95 = _role_pass_rate_and_ci95("pre_mod")
    post_mod_pass_rate, post_mod_pass_rate_ci95 = _role_pass_rate_and_ci95("post_mod")
    irrelevant_pass_rate, irrelevant_pass_rate_ci95 = _role_pass_rate_and_ci95("irrelevant")

    # Role-based pass rates including inconclusive TCs (indicative)
    all_mod_events = [
        e for r in results if r.tc_id not in infra_error_tc_ids
        for e in r.events
        if e.role in ("pre_mod", "post_mod", "irrelevant") and not e.infra_error
    ]
    mod_pass_rate_all = (sum(1 for e in all_mod_events if e.passed) / len(all_mod_events)) if all_mod_events else None

    by_tc_mod_all: dict[str, list[float]] = defaultdict(list)
    for r in results:
        if r.tc_id in infra_error_tc_ids:
            continue
        evts = [e for e in r.events if e.role in ("pre_mod", "post_mod", "irrelevant") and not e.infra_error]
        if evts:
            by_tc_mod_all[r.tc_id].append(sum(1 for e in evts if e.passed) / len(evts))
    mod_pass_rate_all_ci95 = _per_tc_ci95(by_tc_mod_all)

    pre_mod_pass_rate_all, pre_mod_pass_rate_all_ci95 = _role_pass_rate_and_ci95("pre_mod", exclude_inconclusive=False)
    post_mod_pass_rate_all, post_mod_pass_rate_all_ci95 = _role_pass_rate_and_ci95("post_mod", exclude_inconclusive=False)
    irrelevant_pass_rate_all, irrelevant_pass_rate_all_ci95 = _role_pass_rate_and_ci95("irrelevant", exclude_inconclusive=False)

    return EvalSummary(
        total_test_cases=total_test_cases,
        total_runs=total_runs,
        total_events=len(all_events),
        mean_pass_rate=mean_pass_rate,
        pass_rate_ci95=pass_rate_ci95,
        steps_pass_rate=steps_pass_rate,
        steps_pass_rate_ci95=steps_pass_rate_ci95,
        samples_completion=samples_completion,
        samples_completion_ci95=samples_completion_ci95,
        mod_pass_rate=mod_pass_rate,
        mod_pass_rate_ci95=mod_pass_rate_ci95,
        mod_pass_rate_all=mod_pass_rate_all,
        mod_pass_rate_all_ci95=mod_pass_rate_all_ci95,
        pre_mod_pass_rate=pre_mod_pass_rate,
        pre_mod_pass_rate_ci95=pre_mod_pass_rate_ci95,
        pre_mod_pass_rate_all=pre_mod_pass_rate_all,
        pre_mod_pass_rate_all_ci95=pre_mod_pass_rate_all_ci95,
        post_mod_pass_rate=post_mod_pass_rate,
        post_mod_pass_rate_ci95=post_mod_pass_rate_ci95,
        post_mod_pass_rate_all=post_mod_pass_rate_all,
        post_mod_pass_rate_all_ci95=post_mod_pass_rate_all_ci95,
        irrelevant_pass_rate=irrelevant_pass_rate,
        irrelevant_pass_rate_ci95=irrelevant_pass_rate_ci95,
        irrelevant_pass_rate_all=irrelevant_pass_rate_all,
        irrelevant_pass_rate_all_ci95=irrelevant_pass_rate_all_ci95,
        inconclusive_tcs=len(inconclusive_tc_ids),
        infra_error_tcs=len(infra_error_tc_ids),
        mean_event_input_tokens=mean([e.input_tokens for e in all_events]),
        mean_event_output_tokens=mean([e.output_tokens for e in all_events]),
        mean_event_latency_ms=mean([e.latency_ms for e in all_events]),
        mean_mod_input_tokens=mean([m.input_tokens for m in all_mods]),
        mean_mod_output_tokens=mean([m.output_tokens for m in all_mods]),
        mean_mod_latency_ms=mean([m.latency_ms for m in all_mods]),
        mean_base_event_latency_ms=mean([e.latency_ms for e in all_events if e.role is None]) or None,
        mean_pre_mod_event_latency_ms=mean([e.latency_ms for e in all_events if e.role == "pre_mod"]) or None,
        mean_post_mod_event_latency_ms=mean([e.latency_ms for e in all_events if e.role == "post_mod"]) or None,
        mean_irrelevant_event_latency_ms=mean([e.latency_ms for e in all_events if e.role == "irrelevant"]) or None,
        total_agent_input_tokens=sum(e.input_tokens for e in all_events) + sum(m.input_tokens for m in all_mods),
        total_agent_output_tokens=sum(e.output_tokens for e in all_events) + sum(m.output_tokens for m in all_mods),
        total_judge_input_tokens=sum(e.judge_input_tokens for e in all_events),
        total_judge_output_tokens=sum(e.judge_output_tokens for e in all_events),
    )


# ── Concurrent TC runner ─────────────────────────────────────────────────────

async def _run_all_tcs_concurrent(
    test_cases: list,
    args: argparse.Namespace,
    openclaw_home: Path,
    harness,
    mock_server: Optional["MockServer"],
    single_agent_id: Optional[str],
    output_file,
    timeout_s: Optional[float],
    workers: Optional[list["WorkerConfig"]] = None,
    pbar=None,
    completed: "frozenset[tuple[int, int]]" = frozenset(),
    prior_results: Optional[list] = None,
    tracked_harness=None,
) -> list:
    """Run all TCs concurrently up to args.concurrency at a time.

    Standard mode: each slot uses isolated workspace dirs (workspace-{id}-cN)
    and unique session names on the shared gateway.

    Pool mode (workers != None): each slot is a dedicated Docker container with
    its own isolated gateway and mock server.  No slot_suffix needed — containers
    are already isolated.  The container is restarted before each TC to guarantee
    a clean gateway (no cached agent state), then agents are exported and
    configured fresh on every run.
    """
    concurrency = len(workers) if workers else getattr(args, "parallel", 1)
    sem = asyncio.Semaphore(concurrency)
    slot_queue: asyncio.Queue[int] = asyncio.Queue()
    for s in range(concurrency):
        slot_queue.put_nowait(s)

    results_lock = asyncio.Lock()
    prior: list = list(prior_results) if prior_results else []
    new_tc_results: list = []  # only results added during this run (returned to caller)
    all_tc_results: list = prior  # full set (prior + new) used for pbar metrics
    # Per-slot dedup: track which sample_ids have been exported for each slot
    seen_exports_per_slot: dict[int, set[str]] = {s: set() for s in range(concurrency)}

    async def _run_one(tc_idx: int, tc) -> None:
        async with sem:
            slot = await slot_queue.get()
            tc_t0 = time.time()  # fallback; overwritten per-run below
            run_idx = 0  # default for exception reporting if setup raises before the run loop
            try:
                # ── Resolve gateway / mock server / openclaw_home for this slot ──
                if workers:
                    worker = workers[slot]
                    slot_gateway_url = worker.gateway_url
                    slot_openclaw_home = worker.data_dir
                    slot_container_home = Path(worker.container_home)
                    slot_mock_server: Any = RemoteMockServer(worker.mock_server_url)
                    slot_suffix = ""  # container-isolated; no ID suffix needed
                else:
                    slot_gateway_url = args.gateway_url
                    slot_openclaw_home = openclaw_home
                    slot_container_home = None
                    slot_mock_server = mock_server
                    slot_suffix = "" if slot == 0 else f"-c{slot}"

                # ── Retry loop: configure + execute (pool mode retries on connection errors) ──
                # Connection resets happen when the gateway hot-reloads after a
                # config.patch.  On ConnectionResetError, wait for the gateway to
                # stabilize and retry the TC execution WITHOUT re-patching the config
                # (re-patching triggers another hot-reload → another reset → loop).
                for _gateway_attempt in range(3):  # attempt 0, 1, 2 (2 retries)
                    try:
                        # ── Pool mode: ensure gateway is alive, clear state ──
                        if workers:
                            was_restarted = await _ensure_worker_healthy(worker)
                            if was_restarted:
                                # Container restart wipes openclaw.json — clear the
                                # export cache so the per-TC config write below fires.
                                seen_exports_per_slot[slot].clear()
                            elif _gateway_attempt > 0:
                                # Retry after a connection error: wait for true stability.
                                await _wait_for_gateway(
                                    slot_gateway_url, None,
                                    timeout_s=30.0, stable_for_s=5.0,
                                )
                            _clear_worker_state(tc.objects, slot_openclaw_home, single_agent_id)

                        # ── Export workspace files + write config (pool mode: per-sample) ──
                        # When the sample changes, export workspace files and write a
                        # fresh config with only this sample's agents (2-3 IDs).  Keeping
                        # the agent list small ensures hot-reload restarts finish quickly
                        # (well within the entrypoint's 12s PID-tracking window).
                        if workers and tc.sample_id not in seen_exports_per_slot[slot]:
                            # Stagger first config write per slot so all N workers don't
                            # hot-reload simultaneously on TC001.  Only needed when the
                            # slot cache is empty (fresh start or post-restart).
                            if not seen_exports_per_slot[slot]:
                                await asyncio.sleep(slot * 2.0)
                            if single_agent_id:
                                # Steps-based single-agent mode: pass Sample (tc.steps used).
                                export_single_agent_workspace(tc, slot_openclaw_home,
                                                              agent_id=single_agent_id, force=True)
                            else:
                                export_workflow_from_objects(tc.objects, slot_openclaw_home,
                                                             force=True, write_config=False)
                            tc_agent_ids = {obj.object_id for obj in tc.objects}
                            if single_agent_id:
                                tc_agent_ids = {single_agent_id}
                            _model = getattr(args, "model", None) or "gpt-4o"
                            _provider = getattr(args, "provider", None) or infer_provider(_model)
                            _, _config_changed = _write_worker_config(
                                worker, tc_agent_ids, _provider, _model, single_agent_id,
                                verbose=False, preserve_a2a_allow=True,
                            )
                            if _config_changed:
                                # Hot-reload: the gateway self-cascades 3-4 config rewrites
                                # (~2s each via inotify) before settling — total ~8s.
                                # 12s gives a clear margin over the cascade; HTTP /health
                                # stays 200 throughout so no WS probe needed.
                                await asyncio.sleep(12.0)
                            else:
                                # Even without a config change, workspace file overwrites
                                # (force=True export) change AGENTS.md/SOUL.md mtimes,
                                # which the gateway sees via inotify and uses to restart
                                # the affected agents.  Wait for agents to re-pair before
                                # the entry agent sends its first message.
                                await asyncio.sleep(8.0)
                            # Delete BOOTSTRAP.md so the gateway doesn't run its
                            # onboarding flow (which overrides SOUL.md).
                            for _obj in tc.objects:
                                _bs = slot_openclaw_home / f"workspace-{_obj.object_id}{slot_suffix}" / "BOOTSTRAP.md"
                                _bs.unlink(missing_ok=True)
                            seen_exports_per_slot[slot].add(tc.sample_id)

                        if slot_mock_server is not None:
                            tc_mock_script = resolve_mock_configs(tc)
                            tc_mock_script = merge_tc_mock_tools(tc_mock_script, tc.tools)
                            if workers:
                                # RemoteMockServer: stash script so configure() sends it in the POST body
                                slot_mock_server._pending_mock_script = tc_mock_script
                            else:
                                mock_server._state.get_slot(slot_suffix or "default").mock_script = tc_mock_script  # type: ignore[union-attr]

                        if single_agent_id:
                            agents = {single_agent_id: OpenClawAgent(single_agent_id, gateway_url=slot_gateway_url)}
                        else:
                            agents = {
                                f"{obj.object_id}{slot_suffix}": OpenClawAgent(
                                    f"{obj.object_id}{slot_suffix}", gateway_url=slot_gateway_url
                                )
                                for obj in tc.objects
                            }

                        for run_idx in range(args.runs):
                            if (tc_idx, run_idx) in completed:
                                continue  # already done in a previous run — skip
                            # Clear session transcripts and state.md to prevent bleed
                            # from previous runs.
                            agent_ids = (
                                [single_agent_id] if single_agent_id
                                else [f"{obj.object_id}{slot_suffix}" for obj in tc.objects]
                            )
                            _bs_stub = _bootstrap_stub_md()
                            for aid in agent_ids:
                                _clear_agent_sessions(aid, slot_openclaw_home)
                                ws_dir = slot_openclaw_home / f"workspace-{aid}"
                                (ws_dir / "state.md").unlink(missing_ok=True)
                                # Overwrite (not delete) so gateway never recreates its onboarding version
                                _bsp = ws_dir / "BOOTSTRAP.md"
                                if _bsp.exists():
                                    _bsp.write_text(_bs_stub)
                                # Clear agent-written memory summaries to prevent cross-TC
                                # contamination: OpenClaw writes session summaries to memory/
                                # after each run; shared agent names (e.g. zapier-table used
                                # by 24 TCs) accumulate memories from earlier TCs and confuse
                                # the LLM about the current task.
                                mem_dir = ws_dir / "memory"
                                if mem_dir.is_dir():
                                    for mf in mem_dir.iterdir():
                                        if mf.is_file():
                                            mf.unlink(missing_ok=True)

                            mod_type_str = tc.modifications[0].mod_type.value if tc.modifications else "none"
                            tqdm.write(f"\n  {tc.id}[{mod_type_str}] run={run_idx} [slot={slot}]")
                            tc_t0 = time.time()
                            tc_timeout = timeout_s
                            _partial_ev: list[EventResult] = []
                            _partial_mod: list[ModificationResult] = []
                            try:
                                coro = _execute_tc_async(
                                    tc, slot_gateway_url, slot_openclaw_home, harness,
                                    slot_mock_server,
                                    getattr(args, "verbose", False),
                                    getattr(args, "steps_only", False),
                                    single_agent_id,
                                    _partial_ev, _partial_mod,
                                    slot_suffix=slot_suffix,
                                    max_modifications=getattr(args, "modifications", None),
                                    event_concurrency=getattr(args, "concurrency", None) or getattr(args, "sequential", None) or 0,
                                    concurrency_seed=getattr(args, "seed", None) or 42,
                                    tracked_harness=tracked_harness,
                                    thinking=getattr(args, "thinking", None),
                                    sequential=bool(getattr(args, "sequential", None)),
                                    peer_message_timeout=getattr(args, "peer_message_timeout", 0.0),
                                    worker_name=worker.name if workers else None,
                                    run_doctor=False,
                                )
                                if tc_timeout:
                                    event_results, mod_results = await asyncio.wait_for(coro, timeout=tc_timeout)
                                else:
                                    event_results, mod_results = await coro
                            except (asyncio.TimeoutError, *(
                                (OcTimeoutError,) if OcTimeoutError is not None else ()
                            )) as _timeout_exc:
                                # TC exceeded wall-clock timeout OR per-agent SDK timeout.
                                # Preserve events that completed; fill the rest with placeholders.
                                _timeout_label = str(_timeout_exc) or f"Timeout after {tc_timeout}s"
                                tc_elapsed_ms = (time.time() - tc_t0) * 1000
                                collected_ev_ids = {e.event_id for e in _partial_ev}
                                # TC wall-clock timeout = OUR --timeout setting; OcTimeoutError =
                                # OpenClaw SDK-level peer timeout (also our integration's 90s
                                # sessions_send setting). Both classify as oc_eval and are
                                # factored out of pass-rate.
                                for step in [e for e in tc.events if e.role == "base"]:
                                    eid = step.id
                                    if eid not in collected_ev_ids and step.expect is not None:
                                        _partial_ev.append(EventResult(
                                            event_id=eid, passed=False,
                                            reasoning=_timeout_label,
                                            infra_error=True, failure_class="oc_eval",
                                        ))
                                _max_mods = getattr(args, "modifications", None)
                                _active_mod_ids = {m.id for m in (tc.modifications[:_max_mods] if _max_mods else tc.modifications)}
                                for evt in tc.events:
                                    if evt.id not in collected_ev_ids and evt.expect is not None:
                                        if all(mid in _active_mod_ids for mid in (evt.after_mod_ids or [])):
                                            _partial_ev.append(EventResult(
                                                event_id=evt.id, passed=False,
                                                reasoning=_timeout_label,
                                                role=getattr(evt, "role", None),
                                                infra_error=True, failure_class="oc_eval",
                                            ))
                                collected_mod_ids = {m.mod_id for m in _partial_mod}
                                for mod in (tc.modifications[:_max_mods] if _max_mods else tc.modifications):
                                    if mod.id not in collected_mod_ids:
                                        _partial_mod.append(ModificationResult(mod_id=mod.id))
                                # Dedup: last occurrence wins (real result appended first, placeholder after)
                                seen_ev: dict[str, int] = {}
                                for i, e in enumerate(_partial_ev):
                                    seen_ev[e.event_id] = i
                                event_results = [_partial_ev[i] for i in sorted(seen_ev.values())]
                                seen_mod: dict[str, int] = {}
                                for i, m in enumerate(_partial_mod):
                                    seen_mod[m.mod_id] = i
                                mod_results = [_partial_mod[i] for i in sorted(seen_mod.values())]
                                n_timeout = sum(1 for e in event_results if not e.passed)
                                n_pass = sum(1 for e in event_results if e.passed)
                                tc_result = SampleResult(
                                    tc_id=tc.id, sample_id=tc.sample_id, tc_index=tc_idx,
                                    name=tc.name, domain=tc.domain, run_index=run_idx,
                                    events=event_results, modifications=mod_results,
                                    pass_rate=sum(1 for e in event_results if e.passed) / len(event_results) if event_results else None,
                                    elapsed_ms=tc_elapsed_ms,
                                    error_type="timeout",
                                    **_role_elapsed_fields(event_results),
                                )
                                async with results_lock:
                                    all_tc_results.append(tc_result)
                                    new_tc_results.append(tc_result)
                                    output_file.write(tc_result.model_dump_json() + "\n")
                                    output_file.flush()
                                    tqdm.write(f"\n  → TIMEOUT ({_timeout_label})  pass={n_pass}/{len(event_results)}")
                                    _pbar_postfix(pbar, all_tc_results, agent_model=args.model)
                                    if pbar is not None:
                                        pbar.update(1)
                                continue
                            tc_elapsed_ms = (time.time() - tc_t0) * 1000
                            pass_rate = (
                                sum(1 for e in event_results if e.passed) / len(event_results)
                                if event_results else None
                            )
                            tc_result = SampleResult(
                                tc_id=tc.id,
                                sample_id=tc.sample_id,
                                tc_index=tc_idx,
                                name=tc.name,
                                domain=tc.domain,
                                run_index=run_idx,
                                events=event_results,
                                modifications=mod_results,
                                pass_rate=pass_rate,
                                elapsed_ms=tc_elapsed_ms,
                                error_type=_classify_error_type([e.reasoning or "" for e in event_results]),
                                **_role_elapsed_fields(event_results),
                            )
                            async with results_lock:
                                all_tc_results.append(tc_result)
                                new_tc_results.append(tc_result)
                                output_file.write(tc_result.model_dump_json() + "\n")
                                output_file.flush()
                                passed_n = sum(1 for e in event_results if e.passed)
                                total_n = len(event_results)
                                rate_str = f"{pass_rate:.0%}" if pass_rate is not None else "N/A"
                                _tc_s = tc_elapsed_ms / 1000
                                _elapsed_str = f"{int(_tc_s) // 60:02d}:{int(_tc_s) % 60:02d}.{int((_tc_s % 1) * 1000):03d}"
                                _avg_evt_s = _tc_s / total_n if total_n else 0.0
                                tqdm.write(f"\n  → pass={passed_n}/{total_n} ({rate_str})  elapsed={_elapsed_str}  avg/evt={_avg_evt_s:.1f}s")
                                _pbar_postfix(pbar, all_tc_results, agent_model=args.model)
                                if pbar is not None:
                                    pbar.update(1)
                        # TC-boundary barrier: fire-and-forget peer cascades
                        # from the last event can still be writing after
                        # _execute_tc_async returns. Letting the next TC's
                        # _write_worker_config land mid-cascade triggers a
                        # gateway hot-reload that drops in-flight sessions and
                        # crashes the worker. Drain trailing mock activity
                        # before releasing the slot.
                        if workers and slot_mock_server is not None:
                            try:
                                await _wait_mock_quiescence(
                                    slot_mock_server,
                                    max_wait_s=INTER_EVENT_TIMEOUT_S,
                                    quiet_s=6.0,
                                    slot_id=slot_suffix or "default",
                                )
                            except Exception:
                                pass  # never block slot release on quiescence errors
                        break  # run_idx loop finished without connection error — exit retry loop
                    except (
                        ConnectionResetError, ConnectionRefusedError, BrokenPipeError,
                        *(  # GatewayError("WebSocket disconnected") — same root cause
                            (OcGatewayError,) if OcGatewayError is not None else ()
                        ),
                    ) as _conn_exc:
                        if not workers or _gateway_attempt >= 2:
                            raise  # non-pool mode or max retries exceeded: propagate to outer handler
                        tqdm.write(
                            f"  TC {tc.id} [slot={slot}] gateway connection error "
                            f"(attempt {_gateway_attempt + 1}/2), will recover and retry: {_conn_exc!r}",
                            file=sys.stderr,
                        )
                        # The hot-reload may have killed the container entirely.
                        # _ensure_worker_healthy on the next iteration will detect
                        # this and restart it.
            except (Exception, BaseException) as exc:
                if isinstance(exc, asyncio.CancelledError):
                    raise  # let cancellation propagate normally
                # Unexpected error — write a failed result so the TC isn't silently dropped
                _err_label = str(exc) or repr(exc)
                _err_elapsed_ms = (time.time() - tc_t0) * 1000
                _err_is_infra = bool(_classify_error_type([_err_label]))
                err_results: list[EventResult] = []
                for step in [e for e in tc.events if e.role == "base"]:
                    if step.expect is not None:
                        err_results.append(EventResult(
                            event_id=step.id, passed=False, reasoning=_err_label,
                            infra_error=_err_is_infra,
                        ))
                _max_mods_err = getattr(args, "modifications", None)
                _active_mod_ids_err = {m.id for m in (tc.modifications[:_max_mods_err] if _max_mods_err else tc.modifications)}
                for evt in tc.events:
                    if evt.expect is not None:
                        if all(mid in _active_mod_ids_err for mid in (evt.after_mod_ids or [])):
                            err_results.append(EventResult(
                                event_id=evt.id, passed=False, reasoning=_err_label,
                                role=getattr(evt, "role", None),
                                infra_error=_err_is_infra,
                            ))
                err_mod_results = [ModificationResult(mod_id=m.id) for m in (tc.modifications[:_max_mods_err] if _max_mods_err else tc.modifications)]
                tc_result = SampleResult(
                    tc_id=tc.id, sample_id=tc.sample_id, tc_index=tc_idx,
                    name=tc.name, domain=tc.domain, run_index=run_idx,
                    events=err_results, modifications=err_mod_results,
                    pass_rate=0.0 if err_results else None,
                    elapsed_ms=_err_elapsed_ms,
                    error_type=_classify_error_type([_err_label]),
                    **_role_elapsed_fields(err_results),
                )
                async with results_lock:
                    all_tc_results.append(tc_result)
                    new_tc_results.append(tc_result)
                    output_file.write(tc_result.model_dump_json() + "\n")
                    output_file.flush()
                    tqdm.write(f"  TC {tc.id} [slot={slot}] FAILED: {_err_label}", file=sys.stderr)
                    _pbar_postfix(pbar, all_tc_results, agent_model=args.model)
                    if pbar is not None:
                        pbar.update(1)
            finally:
                slot_queue.put_nowait(slot)

    results = await asyncio.gather(*[
        _run_one(i, tc) for i, tc in enumerate(test_cases)
        if any((i, r) not in completed for r in range(args.runs))
    ], return_exceptions=True)
    for exc in results:
        if isinstance(exc, BaseException):
            tqdm.write(f"  [gather] unhandled task exception: {exc!r}", file=sys.stderr)
    return new_tc_results


# ── Pricing ──────────────────────────────────────────────────────────────────

_MODEL_PRICES: dict[str, tuple[float, float]] = {
    "gpt-5.4-mini": (0.75, 1.5),
    "gpt-5.4":      (2.5,  5.0),
}


def _compute_cost(in_tok: int, out_tok: int, model: str) -> Optional[float]:
    prices = _MODEL_PRICES.get(model)
    if prices is None:
        return None
    return in_tok / 1_000_000 * prices[0] + out_tok / 1_000_000 * prices[1]


# ── Main runner ──────────────────────────────────────────────────────────────

def _print_summary(summary, output_path: Optional[Path] = None, elapsed_s: Optional[float] = None,
                   agent_model: str = None, judge_model: str = None) -> None:
    """Print a human-readable summary of evaluation results."""
    def _fmt(v):
        return f"{v:.4f}" if v is not None else "N/A"

    def _fmts(v, me) -> str:
        return f"{_fmt(v)}  ±ME: {_fmt(me)}"

    has_inconclusive = summary.inconclusive_tcs > 0

    def _fmt_mod(conclusive, conclusive_std, all_val, all_std) -> str:
        if not has_inconclusive:
            return _fmts(conclusive, conclusive_std)
        return f"{_fmts(all_val, all_std)}  (conclusive only: {_fmts(conclusive, conclusive_std)}, {summary.inconclusive_tcs} inconclusive excluded)"

    if output_path:
        print(f"Complete. Output: {output_path}")
    if elapsed_s is not None:
        h = int(elapsed_s) // 3600
        m = (int(elapsed_s) % 3600) // 60
        s = int(elapsed_s) % 60
        ms = int((elapsed_s % 1) * 1000)
        elapsed_str = f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}" if h else f"{m:02d}:{s:02d}.{ms:03d}"
        print(f"Elapsed:             {elapsed_str}")
    print(f"Mean pass rate:      {_fmts(summary.mean_pass_rate, summary.pass_rate_ci95)}")
    print(f"Steps pass rate:     {_fmts(summary.steps_pass_rate, summary.steps_pass_rate_ci95)}")
    print(f"Workflows completion:  {_fmts(summary.samples_completion, summary.samples_completion_ci95)}")
    print(f"Mod pass rate:       {_fmt_mod(summary.mod_pass_rate, summary.mod_pass_rate_ci95, summary.mod_pass_rate_all, summary.mod_pass_rate_all_ci95)}  (pre+post+irrelevant)")
    print(f"  Pre-mod:           {_fmt_mod(summary.pre_mod_pass_rate, summary.pre_mod_pass_rate_ci95, summary.pre_mod_pass_rate_all, summary.pre_mod_pass_rate_all_ci95)}")
    print(f"  Post-mod:          {_fmt_mod(summary.post_mod_pass_rate, summary.post_mod_pass_rate_ci95, summary.post_mod_pass_rate_all, summary.post_mod_pass_rate_all_ci95)}")
    print(f"  Irrelevant:        {_fmt_mod(summary.irrelevant_pass_rate, summary.irrelevant_pass_rate_ci95, summary.irrelevant_pass_rate_all, summary.irrelevant_pass_rate_all_ci95)}")
    print(f"Inconclusive TCs:    {summary.inconclusive_tcs}")
    def _fmt_ms(v) -> str:
        return f"{v:.0f}ms" if v is not None else "N/A"
    print(f"Mean event latency:  {_fmt_ms(summary.mean_event_latency_ms)}"
          f"  (base: {_fmt_ms(summary.mean_base_event_latency_ms)}"
          f"  pre: {_fmt_ms(summary.mean_pre_mod_event_latency_ms)}"
          f"  post: {_fmt_ms(summary.mean_post_mod_event_latency_ms)}"
          f"  irrel: {_fmt_ms(summary.mean_irrelevant_event_latency_ms)})")
    n_events = summary.total_events or 1
    print(f"Agent tokens:        {summary.total_agent_input_tokens:,} in / {summary.total_agent_output_tokens:,} out"
          f"  (mean/event: {summary.mean_event_input_tokens:.0f} in / {summary.mean_event_output_tokens:.0f} out)")
    print(f"Judge tokens:        {summary.total_judge_input_tokens:,} in / {summary.total_judge_output_tokens:,} out"
          f"  (mean/event: {summary.total_judge_input_tokens/n_events:.0f} in / {summary.total_judge_output_tokens/n_events:.0f} out)")
    agent_cost = _compute_cost(summary.total_agent_input_tokens, summary.total_agent_output_tokens, agent_model or "")
    judge_cost = _compute_cost(summary.total_judge_input_tokens, summary.total_judge_output_tokens, judge_model or "")
    if agent_cost is not None or judge_cost is not None:
        agent_str = f"${agent_cost:.2f}" if agent_cost is not None else "unknown model"
        judge_str = f"${judge_cost:.2f}" if judge_cost is not None else "unknown model"
        total = (agent_cost or 0) + (judge_cost or 0)
        print(f"Cost:                ${total:.2f} total  "
              f"(agent: {agent_str}  judge: {judge_str})")


def _warn_continuation_mismatch(output_path: Path, args: argparse.Namespace) -> None:
    GUARDED = [
        ("model",         lambda a: getattr(a, "model", None)),
        ("judge_model",   lambda a: getattr(a, "judge_model", None) or getattr(a, "model", None)),
        ("runs",          lambda a: getattr(a, "runs", None)),
        ("concurrency",   lambda a: getattr(a, "concurrency", None)),
        ("modifications", lambda a: getattr(a, "modifications", None)),
        ("seed",          lambda a: getattr(a, "seed", None)),
        ("limit",         lambda a: getattr(a, "limit", None)),
        ("timeout",       lambda a: getattr(a, "timeout", None)),
    ]
    try:
        with open(output_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                params = d.get("params") if d.get("type") == "meta" else None
                if params is None:
                    continue
                mismatches = []
                for field, get_current in GUARDED:
                    orig = params.get(field)
                    curr = get_current(args)
                    if orig is not None and curr is not None and orig != curr:
                        mismatches.append(f"  {field}: original={orig!r}  current={curr!r}")
                if mismatches:
                    print("\n⚠️  WARNING: continuation params differ from original run:")
                    for m in mismatches:
                        print(m)
                    ans = input("\nContinue anyway? [y/N] ").strip().lower()
                    if ans != "y":
                        raise SystemExit("Aborted by user.")
                    print()
                break
    except SystemExit:
        raise
    except Exception:
        pass


def run(args: argparse.Namespace) -> Path:
    """Run baseline evaluation. Returns the output path."""
    eval_start = time.monotonic()
    print(f"evaluate_baseline {_VERSION}")
    import os
    if args.output is None:
        args.output = default_output_path(args.input)
    print(f"Output: {args.output}")

    # Ensure the gateway token is available as an env var so the SDK picks it up
    # regardless of which code path triggers a connection.
    openclaw_home: Path = Path(getattr(args, "openclaw_home", "~/.openclaw")).expanduser()
    if not os.environ.get("OPENCLAW_GATEWAY_TOKEN"):
        token = _load_openclaw_token(openclaw_home)
        if token:
            os.environ["OPENCLAW_GATEWAY_TOKEN"] = token

    if not args.input.exists():
        print(f"Error: Input file not found: {args.input}", file=sys.stderr)
        sys.exit(1)

    test_cases = load_jsonl(args.input, Sample)

    if getattr(args, "tc", None):
        selected: list[Sample] = []
        for selector in args.tc:
            if selector.isdigit():
                idx = int(selector) - 1
                if idx < 0 or idx >= len(test_cases):
                    print(f"Error: --tc {selector} out of range", file=sys.stderr)
                    sys.exit(1)
                selected.append(test_cases[idx])
            else:
                # Match by TC ID, or by sample_id (selects all TCs sharing that sample)
                matched = [tc for tc in test_cases if tc.id == selector or tc.sample_id == selector]
                if not matched:
                    print(f"Error: --tc {selector!r} not found", file=sys.stderr)
                    sys.exit(1)
                selected.extend(matched)
        test_cases = selected
    elif getattr(args, "sample", None):
        import random as _random
        _rng = _random.Random(getattr(args, "sample_seed", None))
        test_cases = _rng.sample(test_cases, min(args.sample, len(test_cases)))
    elif args.limit:
        test_cases = test_cases[: args.limit]

    # When running steps-only, deduplicate by sample_id (same as evaluate.py)
    if getattr(args, "steps_only", False):
        seen_step_samples: set[str] = set()
        deduped: list[Sample] = []
        for tc in test_cases:
            key = tc.sample_id or tc.id
            if key not in seen_step_samples:
                seen_step_samples.add(key)
                deduped.append(tc)
        if len(deduped) < len(test_cases):
            print(
                f"  Steps-only mode: deduplicating by sample_id "
                f"({len(test_cases)} → {len(deduped)} test cases)"
            )
        test_cases = deduped

    timeout_s: Optional[float] = getattr(args, "timeout", None)

    agent_model: Optional[str] = getattr(args, "model", None)
    agent_provider: Optional[str] = getattr(args, "provider", None)
    if agent_model and not agent_provider:
        agent_provider = infer_provider(agent_model)

    judge_model = args.judge_model or agent_model or "gpt-4o"
    judge_provider = args.judge_provider or infer_provider(judge_model)

    multi_agent: bool = getattr(args, "multi_agent", False)
    single_agent_id: Optional[str] = None if multi_agent else "lnl-eval"

    # ── Docker worker pool ────────────────────────────────────────────────────
    pool_config_path = getattr(args, "pool", None)
    workers: Optional[list[WorkerConfig]] = None
    if pool_config_path:
        workers = _load_pool_config(Path(pool_config_path))
        print("Cleaning pool worker directories...")
        _clean_pool_worker_dirs(workers)
        # Pool mode: each worker authenticates with its own token from data_dir/openclaw.json.
        # Clear any global env var so _openclaw_connect_kwargs reads per-worker tokens instead.
        os.environ.pop("OPENCLAW_GATEWAY_TOKEN", None)

        # Sync gateway auth token: the SDK sends the local device operator token in
        # connect.params.auth.token (from ~/.openclaw/identity/device-auth.json).
        # The container gateway must be configured with this same value.
        operator_token = _load_device_operator_token()
        if operator_token:
            restarted_workers: list[WorkerConfig] = []
            for w in workers:
                if _ensure_worker_gateway_token(w, operator_token):
                    restarted_workers.append(w)
            if restarted_workers:
                # Wait for restarted containers to become healthy before proceeding.
                # Must wait for BOTH mock server AND gateway: the mock server (Flask) comes
                # up in ~1s, but the OpenClaw gateway (Node.js + plugin) takes longer.
                # Without waiting for gateway stability here, the first TC that hits this
                # worker runs into a mid-restart hot-reload and gets a connection reset.
                print("  Waiting for restarted containers to become ready...")
                for w in restarted_workers:
                    RemoteMockServer(w.mock_server_url).wait_ready(timeout=60.0)
                    asyncio.run(_wait_for_gateway(w.gateway_url, None, timeout_s=60.0, stable_for_s=5.0))
                    print(f"  [{w.name}] ready.")
        else:
            print("  WARNING: No local device operator token found. "
                  "Gateway auth may fail. Run 'openclaw login' or ensure "
                  "~/.openclaw/identity/device-auth.json exists.")

    print(f"Loaded {len(test_cases)} test cases from {args.input}")
    print(f"Mode: baseline (OpenClaw {'multi-agent' if multi_agent else 'single-agent'})")
    if workers:
        print(f"Pool: {len(workers)} workers")
        for w in workers:
            w.data_dir.mkdir(parents=True, exist_ok=True)
            print(f"  {w.name}: gateway={w.gateway_url}  mock={w.mock_server_url}  data={w.data_dir}")
    else:
        print(f"OpenClaw home: {openclaw_home}")
    if agent_model:
        print(f"Agent model: {agent_provider}/{agent_model}")
    print(f"Judge: {judge_provider}/{judge_model}")
    print(f"Runs per test case: {args.runs}")
    print(f"Timeout per run: {timeout_s}s" if timeout_s else "Timeout: none")
    if args.gateway_url and not workers:
        print(f"Gateway: {args.gateway_url}")
    print()

    # Build MockServer (optional) — local or remote (Docker container)
    # In pool mode each worker has its own RemoteMockServer created per-slot.
    mock_server: Optional[MockServer] = None
    if workers:
        print("Mock server: per-worker (pool mode)")
        for w in workers:
            rs = RemoteMockServer(w.mock_server_url)
            rs.wait_ready()
            print(f"  {w.name} mock server: ready")
    else:
        mock_server_url = getattr(args, "mock_server_url", None)
        if mock_server_url:
            print(f"Mock server: remote at {mock_server_url}")
            mock_server = RemoteMockServer(mock_server_url)  # type: ignore[assignment]
            mock_server.wait_ready()
            print("Mock server: ready")
        elif getattr(args, "mock_server", False):
            openclaw_http_url = getattr(args, "openclaw_http_url", "http://localhost:18789")
            mock_port = getattr(args, "mock_server_port", 18888)
            llm_mode = getattr(args, "mock_llm_mode", False)
            print(f"Mock server: enabled (port {mock_port}, {'LLM' if llm_mode else 'script'} mode)")
            mock_server = MockServer(
                openclaw_url=openclaw_http_url,
                openclaw_hook_token=_load_openclaw_token(openclaw_home),
                port=mock_port,
                llm_mode=llm_mode,
            )
            mock_server.start()
            mock_server.wait_ready()
            print("Mock server: ready")


    if judge_provider == "openai":
        from src.lnl.judge import OpenAIJudge
        judge = OpenAIJudge(model=judge_model)
    elif judge_provider == "azure":
        from src.lnl.judge import AzureJudge
        judge = AzureJudge(model=judge_model)
    elif judge_provider == "google":
        from src.lnl.judge import GeminiJudge
        judge = GeminiJudge(model=judge_model)
    else:
        from src.lnl.judge import AnthropicJudge
        judge = AnthropicJudge(model=judge_model)

    from src.lnl.benchmark import BenchmarkHarness
    harness = BenchmarkHarness(judge=judge)

    # Optional memory-fidelity judge for tracked events (probe-dataset TCs).
    tracked_harness: Optional["BenchmarkHarness"] = None
    tracked_judge_path = getattr(args, "tracked_judge", None)
    if tracked_judge_path:
        import yaml as _yaml
        tracked_prompt = _yaml.safe_load(Path(tracked_judge_path).read_text())["system_prompt"].strip()
        if judge_provider == "openai":
            from src.lnl.judge import OpenAIJudge
            tracked_judge_inst = OpenAIJudge(model=judge_model, system_prompt=tracked_prompt)
        elif judge_provider == "azure":
            from src.lnl.judge import AzureJudge
            tracked_judge_inst = AzureJudge(model=judge_model, system_prompt=tracked_prompt)
        elif judge_provider == "google":
            from src.lnl.judge import GeminiJudge
            tracked_judge_inst = GeminiJudge(model=judge_model, system_prompt=tracked_prompt)
        else:
            from src.lnl.judge import AnthropicJudge
            tracked_judge_inst = AnthropicJudge(model=judge_model, system_prompt=tracked_prompt)
        tracked_harness = BenchmarkHarness(judge=tracked_judge_inst)

    args.output.parent.mkdir(parents=True, exist_ok=True)

    all_tc_results: list[SampleResult] = []
    seen_samples: set[str] = set()
    effective_provider = agent_provider or "openai"

    # Continuation: if output file already exists, load completed runs and skip them.
    # When infra/timeout TCs are found, the user is prompted to choose whether to
    # skip them (continue with remaining TCs only) or retry them in order.
    completed: set[tuple[int, int]] = set()  # (tc_index, run_index)
    infra_results:   list[SampleResult] = []  # TCs with error_type=="infra"
    timeout_results: list[SampleResult] = []  # TCs with error_type=="timeout"
    if args.output.exists():
        for r in _load_tc_results(args.output):
            # Re-classify TCs saved with error_type=None but infra-matching reasoning.
            # Handles results written before the current _INFRA_ERROR_PATTERNS were in place.
            if r.error_type is None:
                _re = _classify_error_type([e.reasoning or "" for e in (r.events or [])])
                if _re == "infra":
                    r = r.model_copy(update={"error_type": "infra"})
            if r.error_type == "infra":
                infra_results.append(r)
            elif r.error_type == "timeout":
                timeout_results.append(r)
            else:
                # Behaviorally-complete results go straight into completed.
                completed.add((r.tc_index, r.run_index))
                all_tc_results.append(r)

        n_infra   = len(infra_results)
        n_timeout = len(timeout_results)

        if completed:
            print(f"Resuming: {len(completed)} run(s) already done, skipping.")

        if n_infra or n_timeout:
            parts = []
            if n_infra:
                parts.append(f"{n_infra} TC(s) with infra errors (gateway/pairing/network)")
            if n_timeout:
                parts.append(f"{n_timeout} TC(s) that timed out")
            print(f"Found {' and '.join(parts)} from the previous run.")

            # Ask the user what to do — default to skip when not interactive.
            retry_failed = False
            if sys.stdin.isatty():
                print("  [1] Skip them — continue with remaining TCs only  (default)")
                print("  [2] Retry them — re-run infra/timeout TCs in order")
                try:
                    choice = input("Choice [1/2, default=1]: ").strip()
                except EOFError:
                    choice = "1"
                retry_failed = choice == "2"
            else:
                print("  (Non-interactive: skipping infra/timeout TCs. Re-run manually to retry.)")

            if retry_failed:
                # Leave infra/timeout TCs out of completed → they will be re-run.
                print(f"  → Will retry {n_infra + n_timeout} TC(s).")
            else:
                # Mark them completed so they are skipped this run.
                for r in infra_results + timeout_results:
                    completed.add((r.tc_index, r.run_index))
                    all_tc_results.append(r)
                print(f"  → Skipping {n_infra + n_timeout} TC(s). Re-run on the same output file to retry.")

    # Serialize all runtime params (including defaults) for the metadata header
    def _serialize_arg(v: object) -> object:
        if isinstance(v, Path):
            return str(v)
        return v

    run_params = {k: _serialize_arg(v) for k, v in vars(args).items()}
    # Store the resolved judge model (args.judge_model may be None, defaulting to agent model)
    run_params["judge_model"] = judge_model
    run_params["judge_provider"] = judge_provider
    meta_record = {
        "type": "meta",
        "timestamp": datetime.now().isoformat(),
        "version": _VERSION,
        "params": run_params,
    }

    if completed:
        _warn_continuation_mismatch(args.output, args)
    concurrency = len(workers) if workers else getattr(args, "parallel", 1)
    file_mode = "a" if completed else "w"

    with open(args.output, file_mode) as f:
        if not completed:
            f.write(json.dumps(meta_record) + "\n")

        n_skipped = sum(
            1 for tc_idx, tc in enumerate(test_cases)
            for run_idx in range(args.runs)
            if (tc_idx, run_idx) in completed
        )
        # Filter test cases to only pending runs
        pending_tcs = [
            (tc_idx, tc)
            for tc_idx, tc in enumerate(test_cases)
            if any((tc_idx, run_idx) not in completed for run_idx in range(args.runs))
        ]

        total_runs = len(test_cases) * args.runs
        with tqdm(total=total_runs, initial=n_skipped, unit="run", desc="Evaluating") as pbar:
          if all_tc_results:  # continuation — show running metrics immediately
              _pbar_postfix(pbar, all_tc_results, agent_model=args.model)
          if workers:
            # ── Pool mode: clean stale backups, then dispatch ─────────────
            # Agent config is written per-sample in the TC loop (via
            # _write_worker_config) — not pre-registered upfront.  Writing
            # only this TC's 2-3 agents keeps the config small so hot-reload
            # restarts complete well within the entrypoint's 12s PID window.
            # Clean stale .bak files to avoid "Config observe anomaly".
            for w in workers:
                for bak in w.data_dir.glob("openclaw.json.bak*"):
                    bak.unlink(missing_ok=True)
                for clob in w.data_dir.glob("openclaw.json.clobbered.*"):
                    clob.unlink(missing_ok=True)

            n_pending = len(pending_tcs)
            tqdm.write(f"Pool: dispatching {n_pending} TCs across {len(workers)} workers ...")
            new_results = asyncio.run(_run_all_tcs_concurrent(
                test_cases, args, openclaw_home, harness, None,
                single_agent_id, f, timeout_s, workers=workers, pbar=pbar,
                completed=frozenset(completed),
                prior_results=all_tc_results,
                tracked_harness=tracked_harness,
            ))
            all_tc_results.extend(new_results)
          elif concurrency > 1 and not single_agent_id:
            # ── Concurrent multi-agent mode ───────────────────────────────────
            # Pre-export all unique samples for all concurrent slots, THEN dispatch.
            tqdm.write(f"Concurrency: {concurrency} slots (multi-agent)")
            seen_exports: set[str] = set()
            for _, tc in pending_tcs:
                if tc.sample_id in seen_exports:
                    continue
                seen_exports.add(tc.sample_id)
                tqdm.write(f"  Exporting slot-0 agents for sample {tc.sample_id!r} "
                      f"({[o.object_id for o in tc.objects]})")
                export_workflow_from_objects(tc.objects, openclaw_home, force=True, write_config=False)
                if agent_model or agent_provider:
                    effective_provider = asyncio.run(_configure_openclaw_agents(
                        tc.objects, agent_provider or "openai", agent_model or "gpt-4o",
                        openclaw_home, args.gateway_url,
                    ))
                for slot in range(1, concurrency):
                    slot_suffix = f"-c{slot}"
                    slotted_objs = _slot_objects(tc.objects, slot_suffix)
                    tqdm.write(f"  Exporting slot-{slot} agents ({[o.object_id for o in slotted_objs]})")
                    export_workflow_from_objects(slotted_objs, openclaw_home, force=True, write_config=False)
                    if agent_model or agent_provider:
                        asyncio.run(_configure_openclaw_agents(
                            slotted_objs, agent_provider or "openai", agent_model or "gpt-4o",
                            openclaw_home, args.gateway_url,
                        ))
            tqdm.write(f"  Running {len(pending_tcs)} TCs with concurrency={concurrency} ...")
            new_results = asyncio.run(_run_all_tcs_concurrent(
                test_cases, args, openclaw_home, harness, mock_server,
                single_agent_id, f, timeout_s, pbar=pbar,
                completed=frozenset(completed),
                prior_results=all_tc_results,
                tracked_harness=tracked_harness,
            ))
            all_tc_results.extend(new_results)
          # ── Sequential mode ─ runs when concurrency == 1 or single-agent ────────
          _ran_concurrent = workers is not None or (concurrency > 1 and not single_agent_id)
          for tc_idx, tc in pending_tcs:
            if _ran_concurrent:
                break  # skip sequential loop when concurrent mode already ran all TCs
            # Export + register agents when object structure is new (dedup by sample_id)
            if tc.sample_id not in seen_samples:

                if single_agent_id:
                    tqdm.write(f"  Exporting single agent '{single_agent_id}' for sample {tc.sample_id!r} "
                          f"({len(tc.objects)} objects: {[o.object_id for o in tc.objects]})")
                    # Steps-based single-agent mode: pass Sample (tc.steps used, not tc.objects).
                    export_single_agent_workspace(tc, openclaw_home, agent_id=single_agent_id, force=True)
                    if agent_model or agent_provider:
                        effective_provider = asyncio.run(_configure_single_openclaw_agent(
                            single_agent_id,
                            agent_provider or "openai",
                            agent_model or "gpt-4o",
                            openclaw_home,
                            args.gateway_url,
                        ))
                        tqdm.write(f"  Configured agent: {effective_provider}/{agent_model or 'gpt-4o'}")
                else:
                    tqdm.write(f"  Exporting agents for sample {tc.sample_id!r} "
                          f"({len(tc.objects)} objects: {[o.object_id for o in tc.objects]})")
                    export_workflow_from_objects(tc.objects, openclaw_home, force=True, write_config=False)
                    if agent_model or agent_provider:
                        effective_provider = asyncio.run(_configure_openclaw_agents(
                            tc.objects,
                            agent_provider or "openai",
                            agent_model or "gpt-4o",
                            openclaw_home,
                            args.gateway_url,
                        ))
                        tqdm.write(f"  Configured agents: {effective_provider}/{agent_model or 'gpt-4o'}")

                seen_samples.add(tc.sample_id)

            # Build agents dict (fresh per TC so session counters reset)
            if single_agent_id:
                agents = {single_agent_id: OpenClawAgent(single_agent_id, gateway_url=args.gateway_url)}
            else:
                agents = {
                    obj.object_id: OpenClawAgent(obj.object_id, gateway_url=args.gateway_url)
                    for obj in tc.objects
                }

            if mock_server is not None:
                tc_mock_script = resolve_mock_configs(tc)
                tc_mock_script = merge_tc_mock_tools(tc_mock_script, tc.tools)
                mock_server._state.mock_script = tc_mock_script

            for run_idx in range(args.runs):
                if (tc_idx, run_idx) in completed:
                    continue  # already done — skip
                mod_type_str = tc.modifications[0].mod_type.value if tc.modifications else "none"
                label = f"{tc.id}[{mod_type_str}] run={run_idx}"
                pbar.set_description(f"Eval {label}")
                try:
                    event_results, mod_results = execute_test_case(
                        tc, agents, openclaw_home, harness, timeout_s,
                        mock_server=mock_server,
                        verbose=getattr(args, "verbose", False),
                        steps_only=getattr(args, "steps_only", False),
                        single_agent_id=single_agent_id,
                        event_concurrency=getattr(args, "concurrency", 0),
                        concurrency_seed=getattr(args, "seed", None) or 42,
                        tracked_harness=tracked_harness,
                        thinking=getattr(args, "thinking", None),
                        peer_message_timeout=getattr(args, "peer_message_timeout", 0.0),
                    )
                    pass_rate = (
                        sum(1 for e in event_results if e.passed) / len(event_results)
                        if event_results else None
                    )
                    tc_result = SampleResult(
                        tc_id=tc.id,
                        sample_id=tc.sample_id,
                        tc_index=tc_idx,
                        name=tc.name,
                        domain=tc.domain,
                        run_index=run_idx,
                        events=event_results,
                        modifications=mod_results,
                        pass_rate=pass_rate,
                        error_type=_classify_error_type([e.reasoning or "" for e in event_results]),
                    )
                    f.write(tc_result.model_dump_json() + "\n")
                    f.flush()
                    all_tc_results.append(tc_result)
                    _pbar_postfix(pbar, all_tc_results, agent_model=args.model)
                    passed_n = sum(1 for e in event_results if e.passed)
                    total_n = len(event_results)
                    rate_str = f"{pass_rate:.0%}" if pass_rate is not None else "N/A"
                    detail = format_tc_event_detail(event_results)
                    suffix = f"  {detail}" if detail else ""
                    tqdm.write(f"  {label}: pass={passed_n}/{total_n} ({rate_str}){suffix}")
                    pbar.update(1)
                except Exception as e:
                    err_str = str(e)
                    # "Not connected" typically means the gateway restarted mid-run
                    # (config patch triggers a restart). Retry once after waiting.
                    if "not connected" in err_str.lower() or "connect" in err_str.lower():
                        tqdm.write(f"RETRYING (gateway reconnect): {e}", file=sys.stderr)
                        try:
                            asyncio.run(_wait_for_gateway(getattr(args, "gateway_url", None), openclaw_home))
                            event_results, mod_results = execute_test_case(
                                tc, agents, openclaw_home, harness, timeout_s,
                                mock_server=mock_server,
                                verbose=getattr(args, "verbose", False),
                                steps_only=getattr(args, "steps_only", False),
                                single_agent_id=single_agent_id,
                                event_concurrency=getattr(args, "concurrency", 0),
                                concurrency_seed=getattr(args, "seed", None) or 42,
                                tracked_harness=tracked_harness,
                                thinking=getattr(args, "thinking", None),
                                peer_message_timeout=getattr(args, "peer_message_timeout", 0.0),
                            )
                            pass_rate = (
                                sum(1 for e in event_results if e.passed) / len(event_results)
                                if event_results else None
                            )
                            tc_result = SampleResult(
                                tc_id=tc.id, sample_id=tc.sample_id, tc_index=tc_idx,
                                name=tc.name, domain=tc.domain, run_index=run_idx,
                                events=event_results, modifications=mod_results,
                                pass_rate=pass_rate,
                                error_type=_classify_error_type([e.reasoning or "" for e in event_results]),
                            )
                            f.write(tc_result.model_dump_json() + "\n")
                            f.flush()
                            all_tc_results.append(tc_result)
                            _pbar_postfix(pbar, all_tc_results, agent_model=args.model)
                            passed_n = sum(1 for e in event_results if e.passed)
                            total_n = len(event_results)
                            rate_str = f"{pass_rate:.0%}" if pass_rate is not None else "N/A"
                            detail = format_tc_event_detail(event_results)
                            suffix = f"  {detail}" if detail else ""
                            tqdm.write(f"  {label}: pass={passed_n}/{total_n} ({rate_str}){suffix}")
                        except Exception as e2:
                            tqdm.write(f"FAILED (retry): {e2}", file=sys.stderr)
                    else:
                        tqdm.write(f"FAILED: {e}", file=sys.stderr)
                    pbar.update(1)

    summary = _compute_summary(all_tc_results)
    with open(args.output, "a") as f:
        f.write(summary.model_dump_json() + "\n")

    if mock_server is not None:
        mock_server.stop()

    print()
    _print_summary(summary, output_path=args.output, elapsed_s=time.monotonic() - eval_start,
                   agent_model=args.model, judge_model=getattr(args, "judge_model", None))

    # Report non-behavioral failures that are excluded from the pass-rate above.
    # These are NOT re-run automatically — re-run the script to retry them.
    non_behavioral: list[str] = []
    if summary.infra_error_tcs:
        non_behavioral.append(f"{summary.infra_error_tcs} TC(s) with infra errors (gateway/pairing/network)")
    # Count timeout TCs from all_tc_results (not tracked in EvalSummary directly)
    n_timeout = sum(1 for r in all_tc_results if r.error_type == "timeout")
    if n_timeout:
        non_behavioral.append(f"{n_timeout} TC(s) that timed out")
    if non_behavioral:
        print()
        print("⚠  Non-behavioral failures excluded from pass-rate above:")
        for msg in non_behavioral:
            print(f"   • {msg}")
        print("   Re-run on the same output file to retry these.")

    return args.output


# ── CLI ──────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Baseline evaluation: OpenClaw multi-agent comparison for LNL experiment",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m src.data.evaluate_baseline -i outputs/data/zapier/20260322_010211/samples.jsonl
  python -m src.data.evaluate_baseline -i workflows-mods.jsonl --runs 3 --model gpt-4o
""",
    )
    parser.add_argument("--input", "-i", type=Path, default=None,
                        help="Path to test cases JSONL file")
    parser.add_argument("--output", "-o", type=Path, default=None,
                        help="Output JSONL path (default: {stem}_baseline.jsonl next to input)")
    parser.add_argument("--runs", type=int, default=1,
                        help="Number of runs per test case (default: 1)")
    parser.add_argument("--timeout", type=float, default=900.0, metavar="SECONDS",
                        help="Wall-clock timeout per test case run (default: 900)")
    parser.add_argument("--model", "-m", default="claude-sonnet-4-6", metavar="MODEL",
                        help="Model for OpenClaw agents (default: claude-sonnet-4-6). Provider inferred from name.")
    parser.add_argument("--provider", "-p", choices=["openai", "azure", "anthropic", "google"], default=None,
                        help="LLM provider (overrides inference from --model)")
    parser.add_argument("--verbose", "-v", action="store_true", default=False,
                        help="Print each message and agent response with per-event pass/fail")
    parser.add_argument("--gateway-url", default=None,
                        help="OpenClaw gateway WebSocket URL (default: auto-detect localhost:18789)")
    parser.add_argument("--openclaw-home", default="~/.openclaw",
                        help="Root OpenClaw directory for agent workspaces (default: ~/.openclaw)")
    parser.add_argument("--judge-model", default=None,
                        help="Model for LLM-as-judge (default: same as --model, matching evaluate.py behavior)")
    parser.add_argument("--judge-provider", choices=["openai", "azure", "anthropic", "google"], default=None,
                        help="Provider for judge model (inferred from --judge-model if not specified)")
    parser.add_argument("--tracked-judge", type=Path, default=None, metavar="YAML",
                        help=(
                            "Path to a judge YAML with a `system_prompt` key. When set, events with "
                            "role='irrelevant' and expect set (tracked events in probe-dataset TCs) "
                            "are judged using this prompt instead of the default judge prompt."
                        ))
    parser.add_argument("--limit", "-n", type=int, default=None,
                        help="Process only the first N test cases")
    parser.add_argument("--tc", nargs="+", metavar="INDEX_OR_ID",
                        help="Run specific test cases by 1-based index, ID, or sample_id (selects all TCs sharing that sample). Overrides --limit.")
    parser.add_argument("--mock-server", action=argparse.BooleanOptionalAction, default=True,
                        help="Enable mock external system integration (default: enabled). Use --no-mock-server to disable.")
    parser.add_argument("--mock-server-port", type=int, default=18888,
                        help="Port for the mock server (default: 18888)")
    parser.add_argument("--mock-llm-mode", action="store_true", default=False,
                        help="Use LLM to generate mock responses instead of YAML scripts")
    parser.add_argument("--thinking",
                        choices=["disabled", "enabled"],
                        default=None,
                        help="Set extended thinking mode for OpenClaw agent execute() calls (disabled/enabled). Default: not set (model default).")
    parser.add_argument("--peer-message-timeout", type=float, default=0.0, metavar="SECONDS",
                        help="timeoutSeconds for sessions_send peer messages in multi-agent runs. "
                             "0 = fire-and-forget (enqueue and return immediately, no reply awaited) — DEFAULT. "
                             "Pass >0 (e.g. 150) only if you want coordinator peers to wait for downstream "
                             "sub-cascade replies; sized for 3-hop A2A chains. "
                             "See https://docs.openclaw.ai/concepts/session-tool. Multi-agent only.")
    parser.add_argument("--steps-only", action="store_true", default=False,
                        help="Run only the steps phase (no modifications/events). "
                             "Deduplicates by sample_id.")
    parser.add_argument("--openclaw-http-url", default="http://localhost:18789",
                        help="OpenClaw gateway HTTP URL for callback injection (default: http://localhost:18789)")
    parser.add_argument("--mock-server-url", default=None, metavar="URL",
                        help="URL of a remote mock server already running (e.g. in a Docker container). "
                             "When set, skips starting a local mock server and connects to this URL instead.")
    parser.add_argument("--single-agent", dest="multi_agent", action="store_false",
                        help="Use a single combined agent for all LNL-objects instead of one agent per object.")
    parser.set_defaults(multi_agent=True)
    parser.add_argument("--seed", "-s", type=int, default=None, metavar="N",
                        help="Random seed for concurrent event group sampling (default: 42). "
                             "Mirrors --seed in evaluate.py so both evals pick the same subset "
                             "of concurrent events for a fair head-to-head comparison.")
    parser.add_argument("--parallel", "-j", type=int, default=1, metavar="N",
                        dest="parallel",
                        help="Number of TCs to run concurrently (default: 1). "
                             "N>1 requires --multi-agent. Each concurrent slot gets isolated "
                             "workspace dirs (workspace-{id}-cN) and session names.")
    conc_group = parser.add_mutually_exclusive_group()
    conc_group.add_argument("--concurrency", type=int, default=None, metavar="N",
                        help="Fire N events per concurrent group in parallel. "
                             "Mirrors --concurrency in evaluate.py: same stress test, same semantics, "
                             "applied to OpenClaw. Requires TCs generated with --concurrent-events.")
    conc_group.add_argument("--sequential", type=int, default=None, metavar="N",
                        help="Fire N events per concurrent group one at a time (sequential dispatch). "
                             "Mutually exclusive with --concurrency. "
                             "Requires TCs generated with --concurrent-events.")
    parser.add_argument("--modifications", type=int, default=None, metavar="N",
                        help="Limit evaluation to the first N modifications per test case (default: all). "
                             "Events whose after_mod_ids reference mods beyond N are skipped. "
                             "Useful for evaluating 3-mod test cases as if they were 1-mod or 2-mod.")
    parser.add_argument("--pool", default=None, metavar="YAML",
                        help="Path to a worker-pool YAML file (see docker/worker-pool.yaml). "
                             "Distributes TCs across Docker worker containers, each with its own "
                             "isolated OpenClaw gateway and mock server. Overrides --parallel, "
                             "--gateway-url, and --mock-server-url.")
    parser.add_argument("--stats", default=None, metavar="FILE", type=Path,
                        help="Recompute and reprint summary stats from an existing results JSONL "
                             "file without re-running evaluation. All other args are ignored.")
    parser.add_argument("--sample", type=int, default=None, metavar="N",
                        help="Randomly sample N test cases from the input (use with --sample-seed for reproducibility).")
    parser.add_argument("--sample-seed", type=int, default=None, metavar="S",
                        help="Random seed for --sample (default: None = non-reproducible).")
    return parser


def _load_tc_results(path: Path) -> list[SampleResult]:
    """Load SampleResult lines from a results JSONL, skipping EvalSummary lines."""
    import json as _json
    results = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            data = _json.loads(line)
            if "tc_id" in data:
                results.append(SampleResult(**data))
    return results


def main():
    args = build_parser().parse_args()
    if args.stats:
        results = _load_tc_results(args.stats)
        summary = _compute_summary(results)
        _print_summary(summary)
        return
    if args.input is None:
        build_parser().error("the following arguments are required: --input/-i")
    run(args)


if __name__ == "__main__":
    main()
