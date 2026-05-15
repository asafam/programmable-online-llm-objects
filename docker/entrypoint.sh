#!/bin/bash
# Entrypoint for the LNL OpenClaw worker container.
# Starts the OpenClaw gateway and the LNL mock server as sibling processes.
#
# The gateway performs a full process restart when its config is patched by
# evaluate_baseline.py (agents.list, agentToAgent, etc.). The monitor loop
# below detects this and tracks the new PID so the container stays alive
# across gateway self-restarts. The container only exits if the mock server
# dies or the gateway dies without spawning a successor.
set -euo pipefail

# ── Copy plugin into the bind-mount dir (hidden at image build time) ─────────
# The bind-mount over /home/node/.openclaw hides any files baked into the image
# at that path.  Restore the plugin from its staging location on every start.
mkdir -p "${HOME}/.openclaw/extensions/lnl-mock-external"
cp -f "${HOME}/openclaw-extensions/lnl-mock-external/index.js" \
      "${HOME}/.openclaw/extensions/lnl-mock-external/index.js"
cp -f "${HOME}/openclaw-extensions/lnl-mock-external/openclaw.plugin.json" \
      "${HOME}/.openclaw/extensions/lnl-mock-external/openclaw.plugin.json"

# ── Start the Azure usage proxy ──────────────────────────────────────────────
# Injects stream_options:{include_usage:true} into Azure streaming chat completions
# so the gateway receives token counts.
#
# ORDER MATTERS: we must start the proxy and redirect AZURE_OPENAI_ENDPOINT
# *before* running envsubst, so that the gateway config's baseUrl expands to
# the proxy URL rather than the real Azure endpoint. The gateway then routes all
# model API calls through the proxy.
#
# Upstream = the real Azure base URL (no /openai/v1 suffix). The gateway calls
# the proxy at /openai/v1/chat/completions; that path is forwarded verbatim to
# the upstream, producing the correct full Azure URL.
REAL_AZURE_ENDPOINT="${AZURE_OPENAI_ENDPOINT}"
echo "[entrypoint] Starting Azure usage proxy on port 18800..."
cd /app && python3 docker/azure_usage_proxy.py \
    --upstream "${REAL_AZURE_ENDPOINT}" \
    --port 18800 \
    --host 127.0.0.1 &
PROXY_PID=$!
sleep 1
echo "[entrypoint] Azure proxy started (PID ${PROXY_PID})."

# Redirect AZURE_OPENAI_ENDPOINT to the proxy so the envsubst below bakes the
# proxy URL into the gateway's baseUrl config field.
export AZURE_OPENAI_ENDPOINT="http://127.0.0.1:18800/"

# ── Write gateway config ──────────────────────────────────────────────────────
CONFIG_DIR="${HOME}/.openclaw"
mkdir -p "${CONFIG_DIR}"
# Write config AFTER the env override so baseUrl expands to the proxy URL.
# Template lives outside .openclaw so a pool bind-mount doesn't hide it.
envsubst '${OPENCLAW_GATEWAY_TOKEN} ${AZURE_OPENAI_ENDPOINT}' \
    < "${HOME}/openclaw.json.tpl" \
    > "${CONFIG_DIR}/openclaw.json"

# Clear the gateway's own device identity so it generates a fresh unpaired
# identity on every start.  Workers 3/4 had their internal device pre-paired
# with limited scopes ("operator.read") from a previous manual operation.
# A fresh identity has never been paired, so shouldSkipLocalBackendSelfPairing
# bypasses the pairing check for all backend loopback connections.
# paired.json is intentionally kept so the host CLI device retains its full
# operator-scope pairing and the Python SDK can connect without re-pairing.
rm -f "${CONFIG_DIR}/identity/device.json"
rm -f "${CONFIG_DIR}/identity/device-auth.json"

# ── Start the LNL mock server ─────────────────────────────────────────────────
echo "[entrypoint] Starting mock server on port 18888..."
cd /app && python3 -m src.data.mock_server \
    --port 18888 \
    --openclaw-url "http://localhost:18789" &
MOCK_PID=$!

# Wait for the mock server to be ready before starting the gateway
for i in $(seq 1 30); do
    if curl -sf http://localhost:18888/health > /dev/null 2>&1; then
        echo "[entrypoint] Mock server ready."
        break
    fi
    sleep 0.5
done

# ── Start the OpenClaw gateway ────────────────────────────────────────────────
echo "[entrypoint] Starting OpenClaw gateway..."
openclaw gateway run --auth token --token "${OPENCLAW_GATEWAY_TOKEN}" --allow-unconfigured &
OC_PID=$!

# ── Monitor loop ──────────────────────────────────────────────────────────────
# The gateway self-restarts (full process fork+exec) when evaluate_baseline.py
# patches the agent config. The old PID exits with code 0; a new PID takes over.
# We poll every 2 seconds: if the mock server dies we abort; if the gateway PID
# is gone we look for a successor and track it.
echo "[entrypoint] Monitoring mock server (PID ${MOCK_PID}) and gateway (PID ${OC_PID})..."
while true; do
    sleep 2

    # Mock server exit → unrecoverable; shut everything down
    if ! kill -0 "${MOCK_PID}" 2>/dev/null; then
        wait "${MOCK_PID}" 2>/dev/null || true
        echo "[entrypoint] Mock server (PID ${MOCK_PID}) exited. Shutting down."
        kill "${OC_PID}" 2>/dev/null || true
        exit 1
    fi

    # Gateway exit → check whether it self-restarted.
    # Full-process restarts spawn a new "openclaw" parent which then
    # launches an "openclaw-gateway" child; the original OC_PID is the
    # "openclaw" launcher (not "openclaw-gateway"), so pgrep for the
    # launcher by exact name and fall back to the gateway child name.
    if ! kill -0 "${OC_PID}" 2>/dev/null; then
        NEW_PID=""
        for _i in 1 2 3 4 5; do
            sleep 2
            NEW_PID=$(pgrep -x "openclaw" 2>/dev/null | head -1 || true)
            [ -n "${NEW_PID}" ] && break
            NEW_PID=$(pgrep -x "openclaw-gateway" 2>/dev/null | head -1 || true)
            [ -n "${NEW_PID}" ] && break
        done
        if [ -n "${NEW_PID}" ]; then
            echo "[entrypoint] Gateway restarted as PID ${NEW_PID} (was ${OC_PID})."
            OC_PID="${NEW_PID}"
        else
            echo "[entrypoint] Gateway (PID ${OC_PID}) exited without restart. Shutting down."
            kill "${MOCK_PID}" 2>/dev/null || true
            exit 1
        fi
    fi
done
