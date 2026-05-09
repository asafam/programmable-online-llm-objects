#!/bin/bash
# Start the LNL OpenClaw worker pool.
#
# Reads the operator token from ~/.openclaw/identity/device-auth.json and
# exports it as OPENCLAW_GATEWAY_TOKEN_1 (and _2/_3/_4 if you have multiple
# workers) before bringing up docker-compose.
#
# Usage:
#   ./docker/start-pool.sh          # start (or restart) the pool
#   ./docker/start-pool.sh down     # stop and remove containers
#   ./docker/start-pool.sh logs     # tail container logs
#
# The pool uses host ports 19789/19888 (worker-1) so it won't collide with a
# locally-running OpenClaw instance on the default 18789/18888 ports.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${SCRIPT_DIR}/docker-compose.yml"

# ── Read operator token from local OpenClaw identity ─────────────────────────
DEVICE_AUTH="${HOME}/.openclaw/identity/device-auth.json"
if [ ! -f "${DEVICE_AUTH}" ]; then
    echo "ERROR: ${DEVICE_AUTH} not found." >&2
    echo "Make sure OpenClaw is installed and you have logged in (openclaw auth login)." >&2
    exit 1
fi

OPERATOR_TOKEN=$(python3 -c "
import json, sys
try:
    d = json.load(open('${DEVICE_AUTH}'))
    print(d['tokens']['operator']['token'])
except (KeyError, json.JSONDecodeError) as e:
    print(f'ERROR: could not read operator token: {e}', file=sys.stderr)
    sys.exit(1)
")

export OPENCLAW_GATEWAY_TOKEN_1="${OPERATOR_TOKEN}"
export OPENCLAW_GATEWAY_TOKEN_2="${OPERATOR_TOKEN}"
export OPENCLAW_GATEWAY_TOKEN_3="${OPERATOR_TOKEN}"
export OPENCLAW_GATEWAY_TOKEN_4="${OPERATOR_TOKEN}"

# ── Pre-seed worker identity dirs so gateways accept token connections ────────
# Each worker gateway needs:
#   identity/device-auth.json  — operator token credential
#   devices/paired.json        — host device pre-approved as operator
# Without these the gateway falls back to interactive "pairing required" mode.
POOL_DATA_DIR="${LNL_POOL_DATA_DIR:-/tmp/lnl-pool}"
HOST_DEVICE_JSON="${HOME}/.openclaw/identity/device.json"

python3 - "${POOL_DATA_DIR}" "${DEVICE_AUTH}" "${HOST_DEVICE_JSON}" <<'PYEOF'
import json, sys, base64, time, os
from pathlib import Path

pool_dir = Path(sys.argv[1])
device_auth = json.loads(Path(sys.argv[2]).read_text())
host_device = json.loads(Path(sys.argv[3]).read_text())

# Extract base64url public key from PEM (last 32 bytes of the SubjectPublicKeyInfo DER)
import base64 as _b64
pem = host_device["publicKeyPem"].strip()
der_b64 = "".join(pem.split("\n")[1:-1])
der = _b64.b64decode(der_b64)
raw_pub_key = _b64.urlsafe_b64encode(der[-32:]).rstrip(b"=").decode()

device_id = host_device["deviceId"]
op = device_auth["tokens"]["operator"]
now_ms = int(time.time() * 1000)

paired_entry = {
    "deviceId": device_id,
    "publicKey": raw_pub_key,
    "platform": "darwin",
    "clientId": "cli",
    "clientMode": "cli",
    "role": "operator",
    "roles": ["operator"],
    "scopes": op["scopes"],
    "approvedScopes": op["scopes"],
    "tokens": {
        "operator": {
            "token": op["token"],
            "role": "operator",
            "scopes": op["scopes"],
            "createdAtMs": now_ms,
            "lastUsedAtMs": now_ms,
        }
    },
    "createdAtMs": now_ms,
    "approvedAtMs": now_ms,
    "remoteIp": "192.168.65.1",
}
paired_json = json.dumps({device_id: paired_entry}, indent=2)

for n in range(1, 5):
    worker_dir = pool_dir / f"worker-{n}"
    (worker_dir / "identity").mkdir(parents=True, exist_ok=True)
    (worker_dir / "devices").mkdir(parents=True, exist_ok=True)
    (worker_dir / "identity" / "device-auth.json").write_text(json.dumps(device_auth, indent=2))
    (worker_dir / "devices" / "paired.json").write_text(paired_json)

print(f"Seeded identity/device-auth.json and devices/paired.json for workers 1-4")
PYEOF

# ── Dispatch subcommand ───────────────────────────────────────────────────────
CMD="${1:-up}"

case "${CMD}" in
    up)
        echo "Cleaning worker data directories..."
        for n in 1 2 3 4; do
            worker_dir="${POOL_DATA_DIR:-/tmp/lnl-pool}/worker-${n}"
            if [ -d "${worker_dir}" ]; then
                # Remove accumulated workspace dirs and gateway noise; preserve identity/devices
                find "${worker_dir}" -mindepth 1 -maxdepth 1 \
                    ! -name "identity" ! -name "devices" \
                    -exec rm -rf {} +
            fi
        done
        echo "Starting LNL OpenClaw worker pool..."
        docker compose -f "${COMPOSE_FILE}" up -d --remove-orphans
        echo ""
        echo "Workers ready:"
        echo "  worker-1  gateway=ws://localhost:19789  mock=http://localhost:19888"
        echo ""
        echo "Run evaluation:"
        echo "  python -m src.data.evaluate_baseline -i <test_cases.jsonl> --pool docker/worker-pool.yaml"
        ;;
    down)
        echo "Stopping LNL OpenClaw worker pool..."
        docker compose -f "${COMPOSE_FILE}" down
        ;;
    restart)
        echo "Restarting LNL OpenClaw worker pool..."
        docker compose -f "${COMPOSE_FILE}" down
        for n in 1 2 3 4; do
            worker_dir="${POOL_DATA_DIR:-/tmp/lnl-pool}/worker-${n}"
            if [ -d "${worker_dir}" ]; then
                find "${worker_dir}" -mindepth 1 -maxdepth 1 \
                    ! -name "identity" ! -name "devices" \
                    -exec rm -rf {} +
            fi
        done
        docker compose -f "${COMPOSE_FILE}" up -d --remove-orphans
        ;;
    logs)
        docker compose -f "${COMPOSE_FILE}" logs -f "${@:2}"
        ;;
    *)
        echo "Usage: $0 [up|down|restart|logs]" >&2
        exit 1
        ;;
esac
