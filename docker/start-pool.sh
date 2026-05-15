#!/bin/bash
# Start the LNL OpenClaw worker pool.
#
# Reads the operator token from ~/.openclaw/identity/device-auth.json and
# exports it as OPENCLAW_GATEWAY_TOKEN_1..N before bringing up docker-compose.
#
# Usage:
#   ./docker/start-pool.sh                   # start 4 workers (default)
#   ./docker/start-pool.sh --workers 8       # start 8 workers
#   ./docker/start-pool.sh --workers 2       # start 2 workers
#   ./docker/start-pool.sh down              # stop and remove containers
#   ./docker/start-pool.sh restart           # restart (same worker count as last up)
#   ./docker/start-pool.sh logs              # tail container logs
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${SCRIPT_DIR}/docker-compose.yml"

# ── Parse --workers flag ──────────────────────────────────────────────────────
NUM_WORKERS=4
POSITIONAL=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --workers)
            NUM_WORKERS="$2"
            shift 2
            ;;
        *)
            POSITIONAL+=("$1")
            shift
            ;;
    esac
done
set -- "${POSITIONAL[@]+"${POSITIONAL[@]}"}"

CMD="${1:-up}"

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

for n in $(seq 1 "${NUM_WORKERS}"); do
    export "OPENCLAW_GATEWAY_TOKEN_${n}=${OPERATOR_TOKEN}"
done

# ── Pre-seed worker identity dirs so gateways accept token connections ────────
POOL_DATA_DIR="${LNL_POOL_DATA_DIR:-/tmp/lnl-pool}"
HOST_DEVICE_JSON="${HOME}/.openclaw/identity/device.json"

python3 - "${POOL_DATA_DIR}" "${DEVICE_AUTH}" "${HOST_DEVICE_JSON}" "${NUM_WORKERS}" <<'PYEOF'
import json, sys, base64, time, os
from pathlib import Path

pool_dir = Path(sys.argv[1])
device_auth = json.loads(Path(sys.argv[2]).read_text())
host_device = json.loads(Path(sys.argv[3]).read_text())
num_workers = int(sys.argv[4])

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

for n in range(1, num_workers + 1):
    worker_dir = pool_dir / f"worker-{n}"
    (worker_dir / "identity").mkdir(parents=True, exist_ok=True)
    (worker_dir / "devices").mkdir(parents=True, exist_ok=True)
    (worker_dir / "identity" / "device-auth.json").write_text(json.dumps(device_auth, indent=2))
    (worker_dir / "devices" / "paired.json").write_text(paired_json)

print(f"Seeded identity/device-auth.json and devices/paired.json for workers 1-{num_workers}")
PYEOF

# Build list of service names for this pool size
WORKER_SERVICES=""
for n in $(seq 1 "${NUM_WORKERS}"); do
    WORKER_SERVICES="${WORKER_SERVICES} worker-${n}"
done

# ── Dispatch subcommand ───────────────────────────────────────────────────────
case "${CMD}" in
    up)
        echo "Cleaning worker data directories..."
        for n in $(seq 1 "${NUM_WORKERS}"); do
            worker_dir="${POOL_DATA_DIR:-/tmp/lnl-pool}/worker-${n}"
            if [ -d "${worker_dir}" ]; then
                find "${worker_dir}" -mindepth 1 -maxdepth 1 \
                    ! -name "identity" ! -name "devices" \
                    -exec rm -rf {} +
            fi
        done
        echo "Starting LNL OpenClaw worker pool (${NUM_WORKERS} workers)..."
        docker compose -f "${COMPOSE_FILE}" up -d --remove-orphans ${WORKER_SERVICES}
        echo ""
        echo "Workers ready (${NUM_WORKERS}):"
        for n in $(seq 1 "${NUM_WORKERS}"); do
            gw_port=$((19788 + n))
            mock_port=$((19887 + n))
            echo "  worker-${n}  gateway=ws://localhost:${gw_port}  mock=http://localhost:${mock_port}"
        done
        echo ""
        echo "Run evaluation with matching pool config, e.g.:"
        echo "  python -m src.data.evaluate_baseline -i <test_cases.jsonl> --pool docker/worker-pool-${NUM_WORKERS}.yaml"
        ;;
    down)
        echo "Stopping LNL OpenClaw worker pool..."
        docker compose -f "${COMPOSE_FILE}" down
        ;;
    restart)
        echo "Restarting LNL OpenClaw worker pool (${NUM_WORKERS} workers)..."
        docker compose -f "${COMPOSE_FILE}" down
        for n in $(seq 1 "${NUM_WORKERS}"); do
            worker_dir="${POOL_DATA_DIR:-/tmp/lnl-pool}/worker-${n}"
            if [ -d "${worker_dir}" ]; then
                find "${worker_dir}" -mindepth 1 -maxdepth 1 \
                    ! -name "identity" ! -name "devices" \
                    -exec rm -rf {} +
            fi
        done
        docker compose -f "${COMPOSE_FILE}" up -d --remove-orphans ${WORKER_SERVICES}
        ;;
    logs)
        docker compose -f "${COMPOSE_FILE}" logs -f "${@:2}"
        ;;
    *)
        echo "Usage: $0 [--workers N] [up|down|restart|logs]" >&2
        exit 1
        ;;
esac
