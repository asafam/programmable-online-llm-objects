"""
Baseline evaluation runner — OpenClaw single/multi-agent comparison for the LNL experiment.

Runs the same TestCases as evaluate.py but uses an OpenClaw agent instead of the
LNL runtime. By default, uses a single combined agent that receives all object
definitions and handles all messages. With --multi-agent, uses one agent per LNL-object.

Requires:
    - OpenClaw daemon running (openclaw gateway status)
    - openclaw-sdk installed (pip install openclaw-sdk)

Usage:
    python -m src.data.evaluate_baseline \\
        -i outputs/data/zapier/20260322_010211/test_cases.jsonl \\
        --runs 3

    # Multi-agent mode (one agent per object):
    python -m src.data.evaluate_baseline \\
        -i outputs/data/zapier/20260322_010211/test_cases.jsonl \\
        --multi-agent --runs 3
"""
from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import dataclasses
import json
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
    TestCase,
    TestCaseResult,
)
from src.data.mock_server import MockServer, merge_tc_mock_tools, resolve_mock_configs
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
    config_path.write_text(json.dumps(cfg, indent=2))


def _ensure_worker_gateway_token(worker: "WorkerConfig", operator_token: str) -> bool:
    """Ensure the worker's gateway is configured with operator_token.

    Returns True if the container was restarted (caller should re-wait for health).
    """
    import subprocess as _sp
    worker.data_dir.mkdir(parents=True, exist_ok=True)
    current_token = _load_openclaw_token(worker.data_dir)
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

_VERSION: str = _build_version()

# ── Infrastructure failure detection ─────────────────────────────────────────

_INFRA_ERROR_PATTERNS: list[str] = [
    "pairing required",
    "network connection error",
    ": terminated",
]

def _classify_error_type(reasoning_texts: list[str]) -> Optional[str]:
    """Return 'infra' when reasoning texts match known infra failure patterns."""
    combined = " ".join(t for t in reasoning_texts if t).lower()
    if any(p in combined for p in _INFRA_ERROR_PATTERNS):
        return "infra"
    return None


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
    if not sessions_json.exists():
        return
    try:
        store = json.loads(sessions_json.read_text())
    except Exception:
        return

    main_key = f"agent:{object_id}:main"
    entry = store.pop(main_key, None)
    if entry:
        # Delete the JSONL transcript file so the gateway starts fresh
        transcript = Path(entry.get("sessionFile", ""))
        if transcript.exists():
            transcript.unlink(missing_ok=True)
        sessions_json.write_text(json.dumps(store, indent=2))


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
    if not sessions_dir.exists():
        return
    sessions_json = sessions_dir / "sessions.json"
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

    # Quick health check — if the gateway responds, nothing to do
    ws_url = worker.gateway_url or "ws://127.0.0.1:18789"
    http_url = ws_url.replace("ws://", "http://").replace("wss://", "https://")
    health_url = f"{http_url}/health"
    try:
        r = _httpx.get(health_url, timeout=2.0)
        if r.status_code == 200:
            return False  # healthy — no restart needed
    except Exception:
        pass

    # Gateway is down — restart the container (serialised to avoid Docker races)
    if not worker.container_name:
        return False
    async with _restart_lock:
        # Re-check after acquiring lock (another coroutine may have fixed it)
        try:
            r = _httpx.get(health_url, timeout=2.0)
            if r.status_code == 200:
                return False
        except Exception:
            pass
        tqdm.write(f"  [{worker.name}] Gateway is down — restarting container...")
        await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: _sp.run(
                ["docker", "restart", worker.container_name],
                check=True, capture_output=True, timeout=60,
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
    # config on startup, which triggers config overwrites over ~5s)
    await _wait_for_gateway(
        worker.gateway_url, None,
        timeout_s=45.0, stable_for_s=5.0,
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
    token = os.environ.get("OPENCLAW_GATEWAY_TOKEN") or _load_openclaw_token(openclaw_home)
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
    """
    if config.get("gateway", {}).get("auth"):
        return  # already present — nothing to do
    token = _load_openclaw_token(openclaw_home)
    if not token:
        import os
        token = os.environ.get("OPENCLAW_GATEWAY_TOKEN")
    if token:
        config.setdefault("gateway", {})["auth"] = {"mode": "token", "token": token}


async def _configure_openclaw_agents(
    objects: list[ObjectDef],
    provider: str,
    model: str,
    openclaw_home: Path,
    gateway_url: Optional[str],
    path_prefix: Optional[Path] = None,
) -> str:
    """Register all objects as agents in the OpenClaw daemon config.

    Args:
        path_prefix: When set, use this path for workspace/agentDir entries in
            the gateway config instead of openclaw_home.  See
            _configure_single_openclaw_agent for details.

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

        # Check if config actually changed — skip patch (and the reload it
        # triggers) when the agent list and tool settings are already correct.
        old_ids = sorted(a.get("id") for a in lst)
        new_ids = sorted(a.get("id") for a in new_lst)
        config_changed = (
            old_ids != new_ids
            or tools_cfg.get("agentToAgent") != new_a2a
            or tools_cfg.get("sessions", {}).get("visibility") != new_vis
        )

        if config_changed:
            agents_cfg["list"] = new_lst
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
) -> int:
    """Write the agent config to a single worker's bind-mount.

    Returns the number of agents registered.  The gateway's file watcher
    detects the change and applies an in-process hot-reload — no SDK
    config.patch needed (which would trigger a full process restart).
    """
    import json

    config_home = Path(worker.container_home)
    oc_home = worker.data_dir

    # Create minimal workspace/agent dirs on the host bind-mount.
    for oid in all_object_ids:
        (oc_home / f"workspace-{oid}").mkdir(parents=True, exist_ok=True)
        (oc_home / "agents" / oid / "agent").mkdir(parents=True, exist_ok=True)
    if single_agent_id:
        (oc_home / f"workspace-{single_agent_id}").mkdir(parents=True, exist_ok=True)
        (oc_home / "agents" / single_agent_id / "agent").mkdir(parents=True, exist_ok=True)

    # Read the existing config as base (preserves gateway.auth etc.).
    config_file = oc_home / "openclaw.json"
    if config_file.exists():
        import json5
        config = json5.loads(config_file.read_text())
    else:
        config = {}
    _ensure_config_auth(config, oc_home)

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
    tools_cfg["agentToAgent"] = {
        "enabled": True,
        "allow": sorted(registered_ids),
    }
    tools_cfg.setdefault("sessions", {})["visibility"] = "all"

    # Write directly to the bind-mount — the gateway's file watcher
    # detects the change and applies an in-process hot-reload.
    config_file.write_text(json.dumps(config, indent=2) + "\n")
    n = len(registered_ids)
    if verbose:
        print(f"  [{worker.name}] Config written ({n} agents).", flush=True)
    return n


def _preregister_agents_on_workers(
    workers: list["WorkerConfig"],
    all_object_ids: set[str],
    provider: str,
    model: str,
    single_agent_id: Optional[str],
) -> None:
    """Pre-register ALL agents on every worker by writing the config file
    to the bind-mounted data directory.

    Also cleans up stale ``.bak`` / ``.clobbered`` files that cause the
    gateway's "Config observe anomaly" warning (it compares the new config
    size against the largest backup it finds on disk).
    """
    import time as _time
    import httpx as _httpx

    for w in workers:
        # Remove stale backup files to prevent "size-drop-vs-last-good" anomaly
        for bak in w.data_dir.glob("openclaw.json.bak*"):
            bak.unlink(missing_ok=True)
        for clob in w.data_dir.glob("openclaw.json.clobbered.*"):
            clob.unlink(missing_ok=True)

        _write_worker_config(w, all_object_ids, provider, model, single_agent_id)

    # Wait for all gateways to pick up the new config (file watcher).
    _time.sleep(3)
    for w in workers:
        ws_url = w.gateway_url or "ws://127.0.0.1:18789"
        health_url = ws_url.replace("ws://", "http://") + "/health"
        deadline = _time.monotonic() + 30.0
        while _time.monotonic() < deadline:
            try:
                r = _httpx.get(health_url, timeout=1.0)
                if r.status_code == 200:
                    break
            except Exception:
                pass
            _time.sleep(0.5)
        print(f"  [{w.name}] Ready.", flush=True)


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
    await _wait_for_gateway(gateway_url, openclaw_home, timeout_s=ready_timeout_s, stable_for_s=stable_for_s)


async def _wait_for_gateway(
    gateway_url: Optional[str] = None,
    openclaw_home: Optional[Path] = None,
    timeout_s: float = 30.0,
    stable_for_s: float = 0.0,
) -> None:
    """Poll until the gateway accepts a connection, then return.

    If stable_for_s > 0, the gateway must remain reachable for that many
    consecutive seconds before this function returns.  This prevents declaring
    the gateway "ready" during a brief lull between two hot-reload cycles.

    Uses a simple HTTP health check instead of the SDK's WebSocket connection.
    The SDK's ``_connect_with_backoff`` retries indefinitely with exponential
    backoff, which masks brief connection drops — the stability timer never
    resets because the SDK reconnects internally before raising an exception.
    A raw HTTP GET to ``/health`` fails fast (1s timeout), so each probe
    accurately reflects whether the gateway is up *at that instant*.

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
        try:
            r = await asyncio.get_event_loop().run_in_executor(
                None, lambda: _httpx.get(health_url, timeout=1.0)
            )
            if r.status_code == 200:
                if stable_since is None:
                    stable_since = _time.monotonic()
                if stable_for_s <= 0.0 or (_time.monotonic() - stable_since) >= stable_for_s:
                    return  # gateway is up (and stable long enough)
            else:
                stable_since = None
        except Exception:
            stable_since = None  # reset stability timer on any connection failure
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
    tc: TestCase,
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


async def _snapshot_session_tokens(
    gateway: Any,
    session_keys: list[str],
) -> dict[str, tuple[int, int]]:
    """Call sessions.list and return {key: (inputTokens, outputTokens)} for given keys."""
    try:
        result = await gateway.call("sessions.list", {})
        sess_map = {s.get("key", ""): s for s in result.get("sessions", [])}
        return {
            k: (sess_map.get(k, {}).get("inputTokens", 0),
                sess_map.get(k, {}).get("outputTokens", 0))
            for k in session_keys
        }
    except Exception:
        return {k: (0, 0) for k in session_keys}


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
    tc: TestCase,
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
    from src.lnl.openclaw_export import rewrite_agents_md

    # Reset state before run.
    if single_agent_id:
        reset_single_agent_state(tc.objects, openclaw_home, single_agent_id)
    else:
        for obj in tc.objects:
            reset_agent_state(f"{obj.object_id}{slot_suffix}", obj.state_description, openclaw_home)

    # Multi-agent: session names are generated PER-EVENT (not per-run) so each
    # webhook trigger starts with a clean session.  State persists across events
    # via state.md files; session conversation history does NOT carry over.
    # run_session_name is unused in multi-agent mode — see _make_event_handles().
    run_session_name = None

    # Build trigger map: event_id → list of triggered events (test-case schema)
    trigger_map: dict[str, list[Any]] = {}
    for evt in tc.events:
        if evt.triggered_by:
            trigger_map.setdefault(evt.triggered_by, []).append(evt)

    # Build tool trigger map for in-process trigger dispatch (mirrors evaluate.py)
    tool_trigger_map = {t.tool_name: t for t in tc.mock_tools if t.triggers}

    # Build ordered message list (identical logic to the old sync version)
    messages: list[dict[str, Any]] = []
    for i, step in enumerate(tc.steps):
        messages.append({
            "kind": "step",
            "index": i,
            "target": step.target,
            "content": f"[Event from {step.source}]: {step.text}",
            "expect": step.expect,
        })

    active_mods = tc.modifications[:max_modifications] if max_modifications is not None else tc.modifications
    allowed_mod_ids: set[str] = {m.id for m in active_mods}

    # Build concurrent group map (event_concurrency > 0 only).
    # Concurrent events are dispatched as batches around mods, not in the main timeline.
    group_map: dict[str, list] = {}
    if event_concurrency > 0:
        for evt in tc.events:
            if evt.concurrent_group:
                group_map.setdefault(evt.concurrent_group, []).append(evt)

    timeline: list[tuple[int, str, Any]] = []
    for mod in active_mods:
        timeline.append((parse_when(mod.when), "mod", mod))
    for evt in tc.events:
        if evt.triggered_by is None:
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
            for triggered in trigger_map.get(item.id, []):
                messages.append({
                    "kind": "event",
                    "item": triggered,
                    "target": triggered.recipient,
                    "content": f"[Event from {triggered.source} (triggered by {item.id})]: {triggered.input}",
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
        # No rewrite_agents_md — peer session keys are hardcoded to "main" in
        # AGENTS.md (set during export). Only the entry agent uses sname, and
        # that's managed by our Python client handle, not AGENTS.md.

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
    async with await OpenClawClient.connect(**_openclaw_connect_kwargs(gateway_url, openclaw_home)) as client:

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
                # Each event gets its own independent session so OpenClaw receives
                # N truly concurrent requests rather than serializing them within
                # a shared session.
                if not single_agent_id:
                    evt_handles, _ = await _make_event_handles(client)
                else:
                    evt_handles = handles
                lookup = single_agent_id or evt.recipient
                h = evt_handles.get(lookup)
                if h is None:
                    return evt, None, 0.0
                content = f"[Event from {evt.source} at {evt.when}]: {evt.input}"
                t_evt = time.time()
                try:
                    res = await h.execute(content)
                    return evt, res, (time.time() - t_evt) * 1000
                except Exception as exc:
                    return evt, exc, (time.time() - t_evt) * 1000

            t0 = time.time()
            if single_agent_id:
                # Single-agent uses one shared session — concurrent calls race on it
                # and drop the connection. Run sequentially instead.
                pairs = [await _one(e) for e in batch]
            else:
                pairs = await asyncio.gather(*[_one(e) for e in batch])
            lat_ms = (time.time() - t0) * 1000

            if mock_server:
                await _wait_mock_quiescence(mock_server, max_wait_s=60.0, quiet_s=3.0,
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
                evidence = gather_evidence(
                    agent_out,
                    tool_calls=batch_tool_calls if mock_server else None,
                    state_content=batch_state,
                )
                passed, reasoning, _votes, _in_tok, _out_tok = harness.evaluate_assertion(
                    evt.expect.action, evidence, ctx)
                tqdm.write(f"    {evt.id} {'✓' if passed else '✗'} {display_lat/1000:.1f}s [conc]  {reasoning[:100]}")
                event_results.append(EventResult(
                    event_id=evt.id, passed=passed, reasoning=reasoning,
                    expected=evt.expect.action, evidence=evidence,
                    prior_context=ctx, latency_ms=display_lat,
                    role=evt.role,
                    judge_input_tokens=_in_tok, judge_output_tokens=_out_tok,
                    judge_votes=_votes,
                ))

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

            # Build session keys for token tracking (entry + all peer "main" sessions).
            _entry_agent_id = lookup_id if single_agent_id else f"{lookup_id}{slot_suffix}"
            _entry_sess_key = f"agent:{_entry_agent_id}:{session_names[lookup_id]}"
            _peer_sess_keys = (
                [] if single_agent_id else
                [f"agent:{obj.object_id}{slot_suffix}:main" for obj in tc.objects]
            )
            _all_sess_keys = [_entry_sess_key] + _peer_sess_keys
            _tok_before = await _snapshot_session_tokens(client.gateway, _all_sess_keys)

            t0 = time.time()
            result = await handle.execute(msg["content"])
            latency_ms = (time.time() - t0) * 1000
            if not result.success:
                tqdm.write(f"  [AGENT ERROR] {target_id}: {result.content}", file=sys.stderr)
            content = result.content if result.success else f"(error: {result.content})"

            # Entry-agent chattiness metrics
            _oc_tool_calls = result.tool_calls  # list[ToolCall] from ExecutionResult
            _agent_tool_calls = len(_oc_tool_calls)
            _a2a_calls = sum(1 for c in _oc_tool_calls if c.tool == "sessions_send")
            _mock_tool_calls = 0  # updated below if mock_server is active

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
                    await _wait_mock_quiescence(mock_server, max_wait_s=30.0, quiet_s=3.0,
                                                slot_id=slot_suffix or "default")
                event_tool_calls = mock_server.get_log(slot_id=slot_suffix or "default")
                _mock_tool_calls = len(event_tool_calls)
            else:
                # No mock server: add a brief delay so the gateway can flush session
                # token counts to sessions.list before we snapshot.
                await asyncio.sleep(0.5)

            # Snapshot session tokens AFTER cascade completes (gateway updates sessions.list
            # asynchronously — ~0.5s after execute() returns).
            _tok_after = await _snapshot_session_tokens(client.gateway, _all_sess_keys)
            _agent_in_tok, _agent_out_tok = _delta_tokens(_tok_before, _tok_after)

            if mock_server:
                # In-process triggers: dispatch tool-triggered messages directly
                # (mirrors MockInProcessExecutor in evaluate.py)
                for call in list(event_tool_calls):
                    if call.get("is_callback") or call.get("is_orchestration"):
                        continue
                    tool_def = tool_trigger_map.get(call["method"])
                    if tool_def is None:
                        continue
                    if not _tool_call_matches(tool_def.match, call.get("args", {})):
                        continue
                    for trigger in tool_def.triggers:
                        tgt_id = trigger.target_object_id
                        tgt_lookup = single_agent_id if single_agent_id else tgt_id
                        tgt_handle = handles.get(tgt_lookup)
                        if tgt_handle is None:
                            if verbose:
                                tqdm.write(f"  Warning: trigger target {tgt_id!r} not in handles, skipping")
                            continue
                        if mock_server:
                            tgt_sname = session_names[tgt_lookup]
                            tgt_agent_id_for_key = f"{tgt_lookup}{slot_suffix}" if not single_agent_id else tgt_lookup
                            tgt_mock_key = f"agent:{tgt_agent_id_for_key}:{tgt_sname}"
                            mock_server.configure(tgt_mock_key, slot_id=slot_suffix or "default")
                        try:
                            trigger_msg = trigger.message_template.format(**call.get("args", {}))
                        except KeyError as _ke:
                            import logging as _logging
                            _logging.getLogger(__name__).warning(
                                "Tool trigger key missing: %s  tool=%s  template=%r  args=%s",
                                _ke, call["method"], trigger.message_template, call.get("args", {})
                            )
                            trigger_msg = trigger.message_template
                        triggered_content = f"[Event from {trigger.source}]: {trigger_msg}"
                        if verbose:
                            tqdm.write(f"  [TRIGGER→{tgt_id}] {triggered_content[:120]}")
                        await tgt_handle.execute(triggered_content)
                        if single_agent_id:
                            await asyncio.sleep(0.3)
                        else:
                            await _wait_mock_quiescence(mock_server, max_wait_s=5.0, quiet_s=0.5,
                                                        slot_id=slot_suffix or "default")
                        trigger_calls = mock_server.get_log(slot_id=slot_suffix or "default")
                        event_tool_calls.extend(trigger_calls)

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
                    tqdm.write(f"    {step_id} {'✓' if passed else '✗'} {latency_ms/1000:.1f}s  {reasoning[:120]}")
                    if verbose:
                        tqdm.write(f"  Expected: {expect.action}")
                        tqdm.write(f"  {'✓ PASS' if passed else '✗ FAIL'}: {reasoning[:200]}")
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
                        mock_tool_calls=_mock_tool_calls,
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
                    evidence = gather_evidence(
                        content,
                        tool_calls=event_tool_calls if mock_server else None,
                        state_content=post_event_state,
                    )
                    passed, reasoning, _votes, _in_tok, _out_tok = harness.evaluate_assertion(
                        item.expect.action, evidence, prior_context)
                    tqdm.write(f"    {item.id} {'✓' if passed else '✗'} {latency_ms/1000:.1f}s  {reasoning[:120]}")
                    if verbose:
                        tqdm.write(f"  Expected: {item.expect.action}")
                        tqdm.write(f"  {'✓ PASS' if passed else '✗ FAIL'}: {reasoning[:200]}")
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
                        mock_tool_calls=_mock_tool_calls,
                    ))
                prior_context = _read_prior_context(tc, openclaw_home, single_agent_id)

    return event_results, mod_results


def _execute_test_case_inner(
    tc: TestCase,
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
    ))


def execute_test_case(
    tc: TestCase,
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
) -> tuple[list[EventResult], list[ModificationResult]]:
    """Run a single TestCase with an optional wall-clock timeout."""
    if timeout_s is None:
        return _execute_test_case_inner(tc, agents, openclaw_home, harness,
                                        mock_server=mock_server, verbose=verbose,
                                        steps_only=steps_only,
                                        single_agent_id=single_agent_id,
                                        slot_suffix=slot_suffix,
                                        max_modifications=max_modifications,
                                        event_concurrency=event_concurrency,
                                        concurrency_seed=concurrency_seed)

    partial_events: list[EventResult] = []
    partial_mods: list[ModificationResult] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            _execute_test_case_inner, tc, agents, openclaw_home,
            harness, mock_server, verbose, steps_only, single_agent_id,
            partial_events, partial_mods, slot_suffix, max_modifications,
            event_concurrency, concurrency_seed,
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
            for i, step in enumerate(tc.steps):
                eid = f"S{i+1:03d}"
                if eid not in collected_event_ids and step.expect is not None:
                    partial_events.append(EventResult(
                        event_id=eid, passed=False,
                        reasoning=f"Timeout after {timeout_s}s",
                    ))
            _active_mod_ids = {m.id for m in (tc.modifications[:max_modifications] if max_modifications else tc.modifications)}
            for evt in tc.events:
                if evt.id not in collected_event_ids and evt.expect is not None:
                    if all(mid in _active_mod_ids for mid in (evt.after_mod_ids or [])):
                        partial_events.append(EventResult(
                            event_id=evt.id, passed=False,
                            reasoning=f"Timeout after {timeout_s}s",
                            role=getattr(evt, "role", None),
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


# ── Summary ──────────────────────────────────────────────────────────────────

_STEP_EVENT_ID = re.compile(r"^S\d+$")


def _running_metrics(results: "list[TestCaseResult]") -> tuple[Optional[float], Optional[float]]:
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


def _pbar_postfix(pbar, results) -> None:
    """Update pbar postfix with running mean + sample pass rates."""
    if pbar is None:
        return
    mean_pr, sample_pr = _running_metrics(results)
    fields: dict[str, str] = {}
    if mean_pr is not None:
        fields["mean"] = f"{mean_pr:.1%}"
    if sample_pr is not None:
        fields["sample"] = f"{sample_pr:.1%}"
    if fields:
        pbar.set_postfix(refresh=False, **fields)


def _compute_summary(results: list[TestCaseResult]) -> EvalSummary:
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

    all_events: list[EventResult] = []
    pass_rates: list[float] = []
    for r in results:
        is_base = r.tc_id in base_tc_ids
        effective = [
            e for e in r.events
            if is_base or not _STEP_EVENT_ID.match(e.event_id)
        ]
        all_events.extend(effective)
        if effective:
            pass_rates.append(sum(1 for e in effective if e.passed) / len(effective))

    mean_pass_rate = mean(pass_rates) if pass_rates else 0.0

    by_tc: dict[str, list[float]] = defaultdict(list)
    for r in results:
        if r.pass_rate is not None:
            by_tc[r.tc_id].append(r.pass_rate)
    per_tc_stds = [statistics.stdev(rates) for rates in by_tc.values() if len(rates) > 1]
    pass_rate_std = mean(per_tc_stds) if per_tc_stds else None

    def _per_tc_std(by_tc_rates: dict) -> Optional[float]:
        """Mean of per-TC stdevs across runs — same pattern as pass_rate_std."""
        stdevs = [statistics.stdev(v) for v in by_tc_rates.values() if len(v) > 1]
        return mean(stdevs) if stdevs else None

    # Steps pass rate + std (base TCs only, mean fraction of steps passed per TC)
    by_tc_step: dict[str, list[float]] = defaultdict(list)
    # Samples completion + std (fraction of TCs where ALL step events passed)
    by_tc_completion: dict[str, list[float]] = defaultdict(list)
    for r in results:
        if r.tc_id not in base_tc_ids:
            continue
        step_evts = [e for e in r.events if _STEP_EVENT_ID.match(e.event_id)]
        if step_evts:
            by_tc_step[r.tc_id].append(sum(1 for e in step_evts if e.passed) / len(step_evts))
            by_tc_completion[r.tc_id].append(1.0 if all(e.passed for e in step_evts) else 0.0)
    steps_pass_rate = mean([mean(v) for v in by_tc_step.values()]) if by_tc_step else None
    steps_pass_rate_std = _per_tc_std(by_tc_step)
    samples_completion = mean([mean(v) for v in by_tc_completion.values()]) if by_tc_completion else None
    samples_completion_std = _per_tc_std(by_tc_completion)

    inconclusive_tc_ids: set[str] = set()
    for r in results:
        if any(_STEP_EVENT_ID.match(e.event_id) and not e.passed for e in r.events):
            inconclusive_tc_ids.add(r.tc_id)

    # Role-based pass rates + std: exclude inconclusive TCs, grouped by TC across runs
    def _role_pass_rate_and_std(role_val, exclude_inconclusive=True) -> tuple[Optional[float], Optional[float]]:
        by_tc: dict[str, list[float]] = defaultdict(list)
        for r in results:
            if exclude_inconclusive and r.tc_id in inconclusive_tc_ids:
                continue
            evts = [e for e in r.events if e.role == role_val]
            if evts:
                by_tc[r.tc_id].append(sum(1 for e in evts if e.passed) / len(evts))
        rate = mean([mean(v) for v in by_tc.values()]) if by_tc else None
        return rate, _per_tc_std(by_tc)

    conclusive_events = [
        e for r in results if r.tc_id not in inconclusive_tc_ids
        for e in r.events
    ]
    mod_events = [e for e in conclusive_events if e.role in ("pre_mod", "post_mod", "irrelevant")]
    mod_pass_rate = (sum(1 for e in mod_events if e.passed) / len(mod_events)) if mod_events else None

    by_tc_mod: dict[str, list[float]] = defaultdict(list)
    for r in results:
        if r.tc_id in inconclusive_tc_ids:
            continue
        evts = [e for e in r.events if e.role in ("pre_mod", "post_mod", "irrelevant")]
        if evts:
            by_tc_mod[r.tc_id].append(sum(1 for e in evts if e.passed) / len(evts))
    mod_pass_rate_std = _per_tc_std(by_tc_mod)

    pre_mod_pass_rate, pre_mod_pass_rate_std = _role_pass_rate_and_std("pre_mod")
    post_mod_pass_rate, post_mod_pass_rate_std = _role_pass_rate_and_std("post_mod")
    irrelevant_pass_rate, irrelevant_pass_rate_std = _role_pass_rate_and_std("irrelevant")

    # Role-based pass rates including inconclusive TCs (indicative)
    all_mod_events = [
        e for r in results for e in r.events
        if e.role in ("pre_mod", "post_mod", "irrelevant")
    ]
    mod_pass_rate_all = (sum(1 for e in all_mod_events if e.passed) / len(all_mod_events)) if all_mod_events else None

    by_tc_mod_all: dict[str, list[float]] = defaultdict(list)
    for r in results:
        evts = [e for e in r.events if e.role in ("pre_mod", "post_mod", "irrelevant")]
        if evts:
            by_tc_mod_all[r.tc_id].append(sum(1 for e in evts if e.passed) / len(evts))
    mod_pass_rate_all_std = _per_tc_std(by_tc_mod_all)

    pre_mod_pass_rate_all, pre_mod_pass_rate_all_std = _role_pass_rate_and_std("pre_mod", exclude_inconclusive=False)
    post_mod_pass_rate_all, post_mod_pass_rate_all_std = _role_pass_rate_and_std("post_mod", exclude_inconclusive=False)
    irrelevant_pass_rate_all, irrelevant_pass_rate_all_std = _role_pass_rate_and_std("irrelevant", exclude_inconclusive=False)

    return EvalSummary(
        total_test_cases=total_test_cases,
        total_runs=total_runs,
        total_events=len(all_events),
        mean_pass_rate=mean_pass_rate,
        pass_rate_std=pass_rate_std,
        steps_pass_rate=steps_pass_rate,
        steps_pass_rate_std=steps_pass_rate_std,
        samples_completion=samples_completion,
        samples_completion_std=samples_completion_std,
        mod_pass_rate=mod_pass_rate,
        mod_pass_rate_std=mod_pass_rate_std,
        mod_pass_rate_all=mod_pass_rate_all,
        mod_pass_rate_all_std=mod_pass_rate_all_std,
        pre_mod_pass_rate=pre_mod_pass_rate,
        pre_mod_pass_rate_std=pre_mod_pass_rate_std,
        pre_mod_pass_rate_all=pre_mod_pass_rate_all,
        pre_mod_pass_rate_all_std=pre_mod_pass_rate_all_std,
        post_mod_pass_rate=post_mod_pass_rate,
        post_mod_pass_rate_std=post_mod_pass_rate_std,
        post_mod_pass_rate_all=post_mod_pass_rate_all,
        post_mod_pass_rate_all_std=post_mod_pass_rate_all_std,
        irrelevant_pass_rate=irrelevant_pass_rate,
        irrelevant_pass_rate_std=irrelevant_pass_rate_std,
        irrelevant_pass_rate_all=irrelevant_pass_rate_all,
        irrelevant_pass_rate_all_std=irrelevant_pass_rate_all_std,
        inconclusive_tcs=len(inconclusive_tc_ids),
        mean_event_input_tokens=mean([e.input_tokens for e in all_events]),
        mean_event_output_tokens=mean([e.output_tokens for e in all_events]),
        mean_event_latency_ms=mean([e.latency_ms for e in all_events]),
        mean_mod_input_tokens=mean([m.input_tokens for m in all_mods]),
        mean_mod_output_tokens=mean([m.output_tokens for m in all_mods]),
        mean_mod_latency_ms=mean([m.latency_ms for m in all_mods]),
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
                                seen_exports_per_slot[slot].clear()
                                # Container restart wipes agent config — the export
                                # block below will re-write it for the current sample.
                            _clear_worker_state(tc.objects, slot_openclaw_home, single_agent_id)

                        # ── Export + configure (pool mode: per-sample lazy) ──
                        # When the sample changes, export workspace files AND
                        # write a fresh config with only this sample's agents.
                        # Keeping the agent list small avoids gateway slowdowns
                        # from routing across hundreds of registered agents.
                        if workers and tc.sample_id not in seen_exports_per_slot[slot]:
                            if single_agent_id:
                                export_single_agent_workspace(tc.objects, slot_openclaw_home,
                                                              agent_id=single_agent_id, force=True)
                            else:
                                export_workflow_from_objects(tc.objects, slot_openclaw_home,
                                                             force=True, write_config=False)
                            # Write config with just this sample's agents
                            tc_agent_ids = {obj.object_id for obj in tc.objects}
                            if single_agent_id:
                                tc_agent_ids = {single_agent_id}
                            _model = getattr(args, "model", None) or "gpt-4o"
                            _provider = getattr(args, "provider", None) or infer_provider(_model)
                            _write_worker_config(
                                worker, tc_agent_ids, _provider, _model, single_agent_id,
                                verbose=False,
                            )
                            await asyncio.sleep(3)  # file watcher pickup
                            # Delete BOOTSTRAP.md so the gateway doesn't run its
                            # onboarding flow (which overrides SOUL.md and makes
                            # agents respond "Hey, who am I?" instead of executing).
                            # The gateway auto-creates this file for new workspaces;
                            # our SOUL.md already encodes identity and behavior.
                            for _obj in tc.objects:
                                _bs = slot_openclaw_home / f"workspace-{_obj.object_id}{slot_suffix}" / "BOOTSTRAP.md"
                                _bs.unlink(missing_ok=True)
                            seen_exports_per_slot[slot].add(tc.sample_id)

                        if slot_mock_server is not None:
                            tc_mock_script = resolve_mock_configs(tc)
                            tc_mock_script = merge_tc_mock_tools(tc_mock_script, tc.mock_tools)
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

                        run_idx = 0  # default for exception reporting before the loop starts
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
                                    event_concurrency=getattr(args, "concurrency", 0),
                                    concurrency_seed=getattr(args, "seed", None) or 42,
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
                                for i, step in enumerate(tc.steps):
                                    eid = f"S{i+1:03d}"
                                    if eid not in collected_ev_ids and step.expect is not None:
                                        _partial_ev.append(EventResult(
                                            event_id=eid, passed=False,
                                            reasoning=_timeout_label,
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
                                tc_result = TestCaseResult(
                                    tc_id=tc.id, sample_id=tc.sample_id, tc_index=tc_idx,
                                    name=tc.name, domain=tc.domain, run_index=run_idx,
                                    events=event_results, modifications=mod_results,
                                    pass_rate=sum(1 for e in event_results if e.passed) / len(event_results) if event_results else None,
                                    elapsed_ms=tc_elapsed_ms,
                                    error_type="timeout",
                                )
                                async with results_lock:
                                    all_tc_results.append(tc_result)
                                    new_tc_results.append(tc_result)
                                    output_file.write(tc_result.model_dump_json() + "\n")
                                    output_file.flush()
                                    tqdm.write(f"\n  → TIMEOUT ({_timeout_label})  pass={n_pass}/{len(event_results)}")
                                    _pbar_postfix(pbar, all_tc_results)
                                    if pbar is not None:
                                        pbar.update(1)
                                continue
                            tc_elapsed_ms = (time.time() - tc_t0) * 1000
                            pass_rate = (
                                sum(1 for e in event_results if e.passed) / len(event_results)
                                if event_results else None
                            )
                            tc_result = TestCaseResult(
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
                                tqdm.write(f"\n  → pass={passed_n}/{total_n} ({rate_str})  elapsed={_elapsed_str}")
                                _pbar_postfix(pbar, all_tc_results)
                                if pbar is not None:
                                    pbar.update(1)
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
                err_results: list[EventResult] = []
                for i, step in enumerate(tc.steps):
                    if step.expect is not None:
                        err_results.append(EventResult(
                            event_id=f"S{i+1:03d}", passed=False, reasoning=_err_label,
                        ))
                _max_mods_err = getattr(args, "modifications", None)
                _active_mod_ids_err = {m.id for m in (tc.modifications[:_max_mods_err] if _max_mods_err else tc.modifications)}
                for evt in tc.events:
                    if evt.expect is not None:
                        if all(mid in _active_mod_ids_err for mid in (evt.after_mod_ids or [])):
                            err_results.append(EventResult(
                                event_id=evt.id, passed=False, reasoning=_err_label,
                                role=getattr(evt, "role", None),
                            ))
                err_mod_results = [ModificationResult(mod_id=m.id) for m in (tc.modifications[:_max_mods_err] if _max_mods_err else tc.modifications)]
                tc_result = TestCaseResult(
                    tc_id=tc.id, sample_id=tc.sample_id, tc_index=tc_idx,
                    name=tc.name, domain=tc.domain, run_index=run_idx,
                    events=err_results, modifications=err_mod_results,
                    pass_rate=0.0 if err_results else None,
                    elapsed_ms=_err_elapsed_ms,
                    error_type=_classify_error_type([_err_label]),
                )
                async with results_lock:
                    all_tc_results.append(tc_result)
                    new_tc_results.append(tc_result)
                    output_file.write(tc_result.model_dump_json() + "\n")
                    output_file.flush()
                    tqdm.write(f"  TC {tc.id} [slot={slot}] FAILED: {_err_label}", file=sys.stderr)
                    _pbar_postfix(pbar, all_tc_results)
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


# ── Main runner ──────────────────────────────────────────────────────────────

def _print_summary(summary, output_path: Optional[Path] = None, elapsed_s: Optional[float] = None) -> None:
    """Print a human-readable summary of evaluation results."""
    def _fmt(v):
        return f"{v:.3f}" if v is not None else "N/A"

    def _fmts(v, s) -> str:
        return f"{_fmt(v)}  std: {_fmt(s)}"

    has_inconclusive = summary.inconclusive_tcs > 0

    def _fmt_mod(conclusive, conclusive_std, all_val, all_std) -> str:
        if not has_inconclusive:
            return _fmts(conclusive, conclusive_std)
        return f"{_fmts(conclusive, conclusive_std)}  ({summary.inconclusive_tcs} inconclusive TCs excluded; all: {_fmts(all_val, all_std)})"

    if output_path:
        print(f"Complete. Output: {output_path}")
    if elapsed_s is not None:
        h = int(elapsed_s) // 3600
        m = (int(elapsed_s) % 3600) // 60
        s = int(elapsed_s) % 60
        ms = int((elapsed_s % 1) * 1000)
        elapsed_str = f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}" if h else f"{m:02d}:{s:02d}.{ms:03d}"
        print(f"Elapsed:             {elapsed_str}")
    print(f"Mean pass rate:      {_fmts(summary.mean_pass_rate, summary.pass_rate_std)}")
    print(f"Steps pass rate:     {_fmts(summary.steps_pass_rate, summary.steps_pass_rate_std)}")
    print(f"Samples completion:  {_fmts(summary.samples_completion, summary.samples_completion_std)}")
    print(f"Mod pass rate:       {_fmt_mod(summary.mod_pass_rate, summary.mod_pass_rate_std, summary.mod_pass_rate_all, summary.mod_pass_rate_all_std)}  (pre+post+irrelevant)")
    print(f"  Pre-mod:           {_fmt_mod(summary.pre_mod_pass_rate, summary.pre_mod_pass_rate_std, summary.pre_mod_pass_rate_all, summary.pre_mod_pass_rate_all_std)}")
    print(f"  Post-mod:          {_fmt_mod(summary.post_mod_pass_rate, summary.post_mod_pass_rate_std, summary.post_mod_pass_rate_all, summary.post_mod_pass_rate_all_std)}")
    print(f"  Irrelevant:        {_fmt_mod(summary.irrelevant_pass_rate, summary.irrelevant_pass_rate_std, summary.irrelevant_pass_rate_all, summary.irrelevant_pass_rate_all_std)}")
    print(f"Inconclusive TCs:    {summary.inconclusive_tcs}")
    n_events = summary.total_events or 1
    print(f"Agent tokens:        {summary.total_agent_input_tokens:,} in / {summary.total_agent_output_tokens:,} out"
          f"  (mean/event: {summary.mean_event_input_tokens:.0f} in / {summary.mean_event_output_tokens:.0f} out)")
    print(f"Judge tokens:        {summary.total_judge_input_tokens:,} in / {summary.total_judge_output_tokens:,} out"
          f"  (mean/event: {summary.total_judge_input_tokens/n_events:.0f} in / {summary.total_judge_output_tokens/n_events:.0f} out)")


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

    test_cases = load_jsonl(args.input, TestCase)

    if getattr(args, "tc", None):
        selected: list[TestCase] = []
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
    elif args.limit:
        test_cases = test_cases[: args.limit]

    # When running steps-only, deduplicate by sample_id (same as evaluate.py)
    if getattr(args, "steps_only", False):
        seen_step_samples: set[str] = set()
        deduped: list[TestCase] = []
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
                print("  Waiting for restarted containers to become ready...")
                for w in restarted_workers:
                    RemoteMockServer(w.mock_server_url).wait_ready(timeout=60.0)
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
    elif judge_provider == "google":
        from src.lnl.judge import GeminiJudge
        judge = GeminiJudge(model=judge_model)
    else:
        from src.lnl.judge import AnthropicJudge
        judge = AnthropicJudge(model=judge_model)

    from src.lnl.benchmark import BenchmarkHarness
    harness = BenchmarkHarness(judge=judge)

    args.output.parent.mkdir(parents=True, exist_ok=True)

    all_tc_results: list[TestCaseResult] = []
    seen_samples: set[str] = set()
    effective_provider = agent_provider or "openai"

    # Continuation: if output file already exists, load completed runs and skip them.
    # Infrastructure failures (pairing required, network error, terminated) are NOT
    # added to completed — they will be automatically re-run on resume.
    completed: set[tuple[int, int]] = set()  # (tc_index, run_index)
    infra_rerun_count = 0
    if args.output.exists():
        timeout_rerun_count = 0
        for r in _load_tc_results(args.output):
            if r.error_type == "infra":
                infra_rerun_count += 1
                continue  # exclude from completed → will be re-run
            if r.error_type == "timeout":
                timeout_rerun_count += 1
                continue  # exclude from completed → will be re-run with higher timeout
            completed.add((r.tc_index, r.run_index))
            all_tc_results.append(r)
        if completed:
            print(f"Resuming: {len(completed)} run(s) already done, skipping.")
        if infra_rerun_count:
            print(f"Re-running {infra_rerun_count} infra-failed TC(s) (pairing/network/terminated).")
        if timeout_rerun_count:
            print(f"Re-running {timeout_rerun_count} timed-out TC(s).")

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
              _pbar_postfix(pbar, all_tc_results)
          if workers:
            # ── Pool mode: clean stale backups, then dispatch ─────────────
            # Agent config is written per-sample in the TC loop (via
            # _write_worker_config) — not pre-registered upfront.
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
                    export_single_agent_workspace(tc.objects, openclaw_home, agent_id=single_agent_id, force=True)
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
                tc_mock_script = merge_tc_mock_tools(tc_mock_script, tc.mock_tools)
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
                    )
                    pass_rate = (
                        sum(1 for e in event_results if e.passed) / len(event_results)
                        if event_results else None
                    )
                    tc_result = TestCaseResult(
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
                    _pbar_postfix(pbar, all_tc_results)
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
                            )
                            pass_rate = (
                                sum(1 for e in event_results if e.passed) / len(event_results)
                                if event_results else None
                            )
                            tc_result = TestCaseResult(
                                tc_id=tc.id, sample_id=tc.sample_id, tc_index=tc_idx,
                                name=tc.name, domain=tc.domain, run_index=run_idx,
                                events=event_results, modifications=mod_results,
                                pass_rate=pass_rate,
                                error_type=_classify_error_type([e.reasoning or "" for e in event_results]),
                            )
                            f.write(tc_result.model_dump_json() + "\n")
                            f.flush()
                            all_tc_results.append(tc_result)
                            _pbar_postfix(pbar, all_tc_results)
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
    _print_summary(summary, output_path=args.output, elapsed_s=time.monotonic() - eval_start)
    return args.output


# ── CLI ──────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Baseline evaluation: OpenClaw multi-agent comparison for LNL experiment",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m src.data.evaluate_baseline -i outputs/data/zapier/20260322_010211/test_cases.jsonl
  python -m src.data.evaluate_baseline -i test_cases.jsonl --runs 3 --model gpt-4o
""",
    )
    parser.add_argument("--input", "-i", type=Path, default=None,
                        help="Path to test cases JSONL file")
    parser.add_argument("--output", "-o", type=Path, default=None,
                        help="Output JSONL path (default: {stem}_baseline.jsonl next to input)")
    parser.add_argument("--runs", type=int, default=1,
                        help="Number of runs per test case (default: 1)")
    parser.add_argument("--timeout", type=float, default=180.0, metavar="SECONDS",
                        help="Wall-clock timeout per test case run (default: 180)")
    parser.add_argument("--model", "-m", default="claude-sonnet-4-6", metavar="MODEL",
                        help="Model for OpenClaw agents (default: claude-sonnet-4-6). Provider inferred from name.")
    parser.add_argument("--provider", "-p", choices=["openai", "anthropic", "google"], default=None,
                        help="LLM provider (overrides inference from --model)")
    parser.add_argument("--verbose", "-v", action="store_true", default=False,
                        help="Print each message and agent response with per-event pass/fail")
    parser.add_argument("--gateway-url", default=None,
                        help="OpenClaw gateway WebSocket URL (default: auto-detect localhost:18789)")
    parser.add_argument("--openclaw-home", default="~/.openclaw",
                        help="Root OpenClaw directory for agent workspaces (default: ~/.openclaw)")
    parser.add_argument("--judge-model", default=None,
                        help="Model for LLM-as-judge (default: same as --model, matching evaluate.py behavior)")
    parser.add_argument("--judge-provider", choices=["openai", "anthropic", "google"], default=None,
                        help="Provider for judge model (inferred from --judge-model if not specified)")
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
    parser.add_argument("--concurrency", type=int, default=0, metavar="N",
                        help="Fire N events per concurrent group simultaneously (default: 0 = sequential). "
                             "Mirrors --concurrency in evaluate.py: same stress test, same semantics, "
                             "applied to OpenClaw. Requires TCs generated with --concurrent-events.")
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
    return parser


def _load_tc_results(path: Path) -> list[TestCaseResult]:
    """Load TestCaseResult lines from a results JSONL, skipping EvalSummary lines."""
    import json as _json
    results = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            data = _json.loads(line)
            if "tc_id" in data:
                results.append(TestCaseResult(**data))
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
