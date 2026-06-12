"""
MockServer — generic mock for external system tool calls.

Runs as a FastAPI app. Any gateway plugin (e.g. lnl-mock-external for OpenClaw)
forwards tool calls (slack_send_message, email_send, jira_create_issue, …) to this
server instead of the real external APIs. The server responds with scripted or
LLM-generated responses and optionally injects callbacks back into the agent session
via OpenClaw's /hooks/wake endpoint.

Routes:
    POST /tool/{method}   — receive a tool invocation from the plugin
    POST /configure       — set active MockScript and session key before a run
    GET  /log             — retrieve recorded tool call log for the current session
    GET  /health          — readiness probe

Zero-config usage:
    python server.py                      # listens on :18888
    python server.py --port 9000          # custom port
    python server.py --openclaw-url http://host:18789  # enable wake callbacks
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

# Ensure this directory is on sys.path so `from schema import ...` always resolves
# to mock/schema.py regardless of how this file is run (script or module).
_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import httpx
import uvicorn
import yaml
from fastapi import FastAPI, Request

from schema import (
    MockCallback,
    MockImmediateResponse,
    MockMethodDef,
    MockScript,
    MockSystemDef,
    OrchestratorReaction,
    OrchestratorScript,
    OrchestratorTrigger,
)

logger = logging.getLogger(__name__)

# ── Config discovery ──────────────────────────────────────────────────────────
# Resolved relative to this file — works from any working directory.

_MOCKS_DIR = _HERE / "config"
_ORCHESTRATION_DIR = _MOCKS_DIR / "orchestration"


# ── Template interpolation ────────────────────────────────────────────────────

def _interpolate(template: str, args: dict[str, Any], tool_call_id: str) -> str:
    """Fill {placeholders} in a template from tool call args."""
    ctx = {**args, "tool_call_id": tool_call_id, "timestamp": time.strftime("%H:%M:%S")}
    try:
        return template.format_map(ctx)
    except KeyError:
        return template


# ── LLM helper ───────────────────────────────────────────────────────────────

def _llm_chat(system_prompt: str, user_message: str, model: str = "gpt-4o-mini") -> str:
    """Simple single-turn LLM call using the OpenAI SDK."""
    from openai import OpenAI
    client = OpenAI()
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        max_tokens=256,
    )
    return resp.choices[0].message.content or ""


# ── FastAPI app factory ───────────────────────────────────────────────────────

def _make_app(state: "_ServerState") -> FastAPI:
    app = FastAPI(title="LNL MockServer")

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/configure")
    async def configure(request: Request):
        body = await request.json()
        slot_id = body.get("slot_id", "default")
        slot = state.get_slot(slot_id)
        slot.session_key = body.get("session_key", "default")
        if "mock_script" in body:
            slot.mock_script = MockScript(**body["mock_script"])
        if "orchestration_script" in body:
            slot.orchestration_script = OrchestratorScript(**body["orchestration_script"])
        slot.call_log.clear()
        slot.fired_triggers.clear()
        return {"status": "configured", "session_key": slot.session_key, "slot_id": slot_id}

    @app.get("/log")
    def get_log(slot_id: str = "default"):
        slot = state.get_slot(slot_id)
        return {"calls": slot.call_log}

    @app.post("/tool/{method}")
    async def handle_tool(method: str, request: Request):
        body = await request.json()
        slot_id = body.pop("__slot_id__", "default")
        slot = state.get_slot(slot_id)
        session_key = body.pop("__session_key__", slot.session_key)
        tool_call_id = uuid.uuid4().hex[:8]

        method_def: Optional[MockMethodDef] = None
        if slot.mock_script:
            method_def = slot.mock_script.get_method(method)

        # Build immediate response
        if method_def and method_def.llm_persona and slot.llm_mode:
            system_prompt = (
                f"You are an external system API with this persona:\n{method_def.llm_persona}\n\n"
                f"Respond ONLY with a realistic API response string (no JSON wrapper). Be brief."
            )
            user_msg = f"Tool call: {method}({json.dumps(body)})"
            result = _llm_chat(system_prompt, user_msg, model=slot.llm_model)
        elif method_def:
            result = _interpolate(method_def.immediate.template, body, tool_call_id)
        else:
            # Verb-aware fallback: "no script configured" reads as a broken tool and
            # makes cautious agents refuse to act. Reads get empty data (the component
            # state lives in the agent's own state, not behind a tool); writes get a
            # success ack — the call and its args are the evidence that matters.
            _low = method.lower()
            if any(v in _low for v in ("get", "list", "read", "fetch", "search", "lookup", "query")):
                result = "{}"
            else:
                result = json.dumps({"status": "success", "tool": method, "id": tool_call_id})

        record = {
            "method": method,
            "args": body,
            "result": result,
            "tool_call_id": tool_call_id,
            "session_key": session_key,
        }
        slot.call_log.append(record)
        logger.debug("MockServer: %s(%s) → %s", method, body, result)

        if method_def and method_def.callback:
            asyncio.create_task(
                _inject_callback(method_def.callback, body, tool_call_id, session_key, slot)
            )

        if slot.orchestration_script:
            asyncio.create_task(
                _fire_reactions(
                    slot.orchestration_script.triggers,
                    slot.orchestration_script.time_scale,
                    method, body, tool_call_id, session_key, slot,
                )
            )

        return {"status": "ok", "result": result}

    return app


async def _inject_callback(
    cb: MockCallback,
    args: dict[str, Any],
    tool_call_id: str,
    session_key: str,
    state: "_ServerState",
) -> None:
    """Wait delay_seconds then POST to OpenClaw /hooks/wake."""
    await asyncio.sleep(cb.delay_seconds)

    if state.llm_mode and state.mock_script:
        msg_text = cb.message_template
    else:
        msg_text = _interpolate(cb.message_template, args, tool_call_id)

    state.call_log.append({
        "method": f"{cb.source}:callback",
        "args": args,
        "result": msg_text,
        "tool_call_id": tool_call_id,
        "session_key": session_key,
        "is_callback": True,
    })

    wake_url = f"{state.openclaw_url}/hooks/wake"
    payload = {"text": msg_text, "sessionKey": session_key}
    headers = {}
    if state.openclaw_hook_token:
        headers["Authorization"] = f"Bearer {state.openclaw_hook_token}"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(wake_url, json=payload, headers=headers)
            if resp.status_code != 200:
                logger.debug("MockServer callback to %s returned %d", wake_url, resp.status_code)
    except Exception as e:
        logger.debug("MockServer callback failed: %s", e)


# ── Orchestration ─────────────────────────────────────────────────────────────

def _trigger_matches(trigger: OrchestratorTrigger, method: str, args: dict[str, Any]) -> bool:
    """Return True if a tool call matches a trigger's `tool` method and `match` patterns."""
    if trigger.tool != method:
        return False
    for key, pattern in trigger.match.items():
        value = str(args.get(key, ""))
        if not re.search(pattern, value, re.IGNORECASE):
            return False
    return True


def _reaction_delay(reaction: OrchestratorReaction, time_scale: float) -> float:
    """Compute real-time delay in seconds, applying time_scale to simulated minutes."""
    return reaction.after_seconds * time_scale + reaction.after_minutes * 60.0 * time_scale


async def _fire_reactions(
    triggers: list[OrchestratorTrigger],
    time_scale: float,
    method: str,
    args: dict[str, Any],
    tool_call_id: str,
    session_key: str,
    state: "_ServerState",
) -> None:
    """Check all orchestration triggers and fire matching reactions."""
    for idx, trigger in enumerate(triggers):
        if not _trigger_matches(trigger, method, args):
            continue
        if trigger.fire_once and idx in state.fired_triggers:
            continue
        if trigger.fire_once:
            state.fired_triggers.add(idx)
        for reaction in trigger.reactions:
            delay = _reaction_delay(reaction, time_scale)
            asyncio.create_task(
                _inject_reaction(reaction, delay, args, tool_call_id, session_key, state)
            )


async def _inject_reaction(
    reaction: OrchestratorReaction,
    delay: float,
    args: dict[str, Any],
    tool_call_id: str,
    session_key: str,
    state: "_ServerState",
) -> None:
    """Wait `delay` seconds then inject the reaction message into OpenClaw."""
    if delay > 0:
        await asyncio.sleep(delay)

    msg_text = _interpolate(reaction.message, args, tool_call_id)

    state.call_log.append({
        "method": f"{reaction.source}:orchestration",
        "args": args,
        "result": msg_text,
        "tool_call_id": tool_call_id,
        "session_key": session_key,
        "is_callback": True,
        "is_orchestration": True,
    })

    wake_url = f"{state.openclaw_url}/hooks/wake"
    payload = {"text": msg_text, "sessionKey": session_key}
    headers = {}
    if state.openclaw_hook_token:
        headers["Authorization"] = f"Bearer {state.openclaw_hook_token}"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(wake_url, json=payload, headers=headers)
            if resp.status_code != 200:
                logger.debug("Orchestration injection to %s returned %d", wake_url, resp.status_code)
    except Exception as e:
        logger.debug("Orchestration injection failed: %s", e)


def load_orchestration_file(path: Path) -> OrchestratorScript:
    """Load an OrchestratorScript from a YAML file."""
    with open(path) as f:
        data = yaml.safe_load(f)
    return OrchestratorScript(**data)


# ── Server state ──────────────────────────────────────────────────────────────

class _ServerState:
    def __init__(self):
        self.session_key: str = "default"
        self.mock_script: Optional[MockScript] = None
        self.orchestration_script: Optional[OrchestratorScript] = None
        self.call_log: list[dict] = []
        self.fired_triggers: set[int] = set()
        self.openclaw_url: str = "http://localhost:18789"
        self.openclaw_hook_token: Optional[str] = None
        self.llm_mode: bool = False
        self.llm_model: str = "gpt-4o-mini"
        self._slot_states: dict[str, "_ServerState"] = {}
        self._slot_lock = threading.Lock()

    def get_slot(self, slot_id: str) -> "_ServerState":
        """Return per-slot state for concurrent TC isolation.

        Slot "default" returns self (backward compatible).
        Other slots inherit global settings on first creation.
        """
        if slot_id == "default":
            return self
        with self._slot_lock:
            if slot_id not in self._slot_states:
                s = _ServerState.__new__(_ServerState)
                s.session_key = self.session_key
                s.mock_script = self.mock_script
                s.orchestration_script = self.orchestration_script
                s.call_log = []
                s.fired_triggers = set()
                s.openclaw_url = self.openclaw_url
                s.openclaw_hook_token = self.openclaw_hook_token
                s.llm_mode = self.llm_mode
                s.llm_model = self.llm_model
                s._slot_states = {}
                s._slot_lock = threading.Lock()
                self._slot_states[slot_id] = s
            return self._slot_states[slot_id]


# ── MockServer lifecycle class ────────────────────────────────────────────────

class MockServer:
    """Manages the lifecycle of the FastAPI mock server in a daemon thread."""

    def __init__(
        self,
        mock_script: Optional[MockScript] = None,
        openclaw_url: str = "http://localhost:18789",
        openclaw_hook_token: Optional[str] = None,
        port: int = 18888,
        llm_mode: bool = False,
        llm_model: str = "gpt-4o-mini",
    ):
        self._port = port
        self._state = _ServerState()
        self._state.mock_script = mock_script
        self._state.openclaw_url = openclaw_url
        self._state.openclaw_hook_token = openclaw_hook_token
        self._state.llm_mode = llm_mode
        self._state.llm_model = llm_model
        self._thread: Optional[threading.Thread] = None
        self._server: Optional[uvicorn.Server] = None

    def start(self) -> None:
        """Start the server in a background daemon thread."""
        app = _make_app(self._state)
        config = uvicorn.Config(
            app,
            host="0.0.0.0",
            port=self._port,
            log_level="warning",
            loop="asyncio",
        )
        self._server = uvicorn.Server(config)

        def _run():
            asyncio.run(self._server.serve())

        self._thread = threading.Thread(target=_run, daemon=True, name="mock-server")
        self._thread.start()

    def wait_ready(self, timeout: float = 10.0) -> None:
        """Poll /health until the server is up or timeout is reached."""
        deadline = time.time() + timeout
        url = f"http://localhost:{self._port}/health"
        while time.time() < deadline:
            try:
                resp = httpx.get(url, timeout=1.0)
                if resp.status_code == 200:
                    return
            except Exception:
                pass
            time.sleep(0.1)
        raise RuntimeError(f"MockServer did not start within {timeout}s")

    def load_orchestration(self, path: Path) -> None:
        """Load an OrchestratorScript from a YAML file and activate it."""
        self._state.orchestration_script = load_orchestration_file(path)

    def add_orchestration(self, script: OrchestratorScript) -> None:
        """Set an OrchestratorScript programmatically."""
        self._state.orchestration_script = script

    def configure(self, session_key: str, slot_id: str = "default") -> None:
        """Reset call log and set the active session key for the next run."""
        url = f"http://127.0.0.1:{self._port}/configure"
        slot = self._state.get_slot(slot_id)
        payload: dict[str, Any] = {"session_key": session_key, "slot_id": slot_id}
        if slot.mock_script:
            payload["mock_script"] = slot.mock_script.model_dump()
        if slot.orchestration_script:
            payload["orchestration_script"] = slot.orchestration_script.model_dump()
        httpx.post(url, json=payload, timeout=5.0)

    def get_log(self, slot_id: str = "default") -> list[dict]:
        """Retrieve the recorded tool call log for the given slot."""
        url = f"http://127.0.0.1:{self._port}/log"
        resp = httpx.get(url, params={"slot_id": slot_id}, timeout=5.0)
        return resp.json().get("calls", [])

    def stop(self) -> None:
        """Signal the server to shut down."""
        if self._server:
            self._server.should_exit = True
        if self._thread:
            self._thread.join(timeout=10.0)


if __name__ == "__main__":
    import argparse
    import signal

    p = argparse.ArgumentParser(description="Start the LNL mock server as a standalone process.")
    p.add_argument("--port", type=int, default=18888, help="Port to listen on (default: 18888)")
    p.add_argument("--openclaw-url", default="http://localhost:18789",
                   help="OpenClaw gateway URL for wake callbacks (default: http://localhost:18789)")
    cli_args = p.parse_args()

    server = MockServer(openclaw_url=cli_args.openclaw_url, port=cli_args.port)
    server.start()
    server.wait_ready()
    print(f"MockServer ready on port {cli_args.port}", flush=True)

    signal.pause()
