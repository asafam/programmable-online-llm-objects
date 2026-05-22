#!/bin/bash
# Start the LNL OpenClaw worker pool.
#
# Reads the operator token from ~/.openclaw/identity/device-auth.json and
# exports it as OPENCLAW_GATEWAY_TOKEN_1..N before bringing up docker-compose.
#
# Usage:
#   ./docker/start-pool.sh                              # start 4 single-agent workers (default)
#   ./docker/start-pool.sh --type single --workers 8   # start 8 single-agent workers
#   ./docker/start-pool.sh --type multi --workers 8    # start 8 multi-agent workers
#   ./docker/start-pool.sh --type single down          # stop single-agent workers
#   ./docker/start-pool.sh --type multi down           # stop multi-agent workers
#   ./docker/start-pool.sh down                        # stop ALL workers (both types)
#   ./docker/start-pool.sh restart                     # restart (default: single, 4 workers)
#   ./docker/start-pool.sh logs                        # tail all container logs
#
# Port layout:
#   single-worker-N : gateway 19788+N  /  mock 19887+N   (e.g. worker-1 → 19789 / 19888)
#   multi-worker-N  : gateway 20788+N  /  mock 20887+N   (e.g. worker-1 → 20789 / 20888)
#
# Run two evals in parallel (two terminal windows):
#   ./docker/start-pool.sh --type single --workers 8
#   ./docker/start-pool.sh --type multi  --workers 8
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${SCRIPT_DIR}/docker-compose.yml"

# ── Parse --type / --workers flags ───────────────────────────────────────────
NUM_WORKERS=4
WORKER_TYPE="single"
POSITIONAL=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --workers)
            NUM_WORKERS="$2"
            shift 2
            ;;
        --type)
            WORKER_TYPE="$2"
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

if [[ "${WORKER_TYPE}" != "single" && "${WORKER_TYPE}" != "multi" ]]; then
    echo "ERROR: --type must be 'single' or 'multi'" >&2
    exit 1
fi

# ── Type-specific settings ────────────────────────────────────────────────────
if [[ "${WORKER_TYPE}" == "single" ]]; then
    GW_BASE=19788
    MOCK_BASE=19887
    SERVICE_PREFIX="single-worker"
    DATA_PREFIX="single-worker"
else
    GW_BASE=20788
    MOCK_BASE=20887
    SERVICE_PREFIX="multi-worker"
    DATA_PREFIX="multi-worker"
fi

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

python3 - "${POOL_DATA_DIR}" "${DEVICE_AUTH}" "${HOST_DEVICE_JSON}" "${NUM_WORKERS}" "${DATA_PREFIX}" <<'PYEOF'
import json, sys, base64, time, os
from pathlib import Path

pool_dir = Path(sys.argv[1])
device_auth = json.loads(Path(sys.argv[2]).read_text())
host_device = json.loads(Path(sys.argv[3]).read_text())
num_workers = int(sys.argv[4])
data_prefix = sys.argv[5]

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
    worker_dir = pool_dir / f"{data_prefix}-{n}"
    (worker_dir / "identity").mkdir(parents=True, exist_ok=True)
    (worker_dir / "devices").mkdir(parents=True, exist_ok=True)
    (worker_dir / "identity" / "device-auth.json").write_text(json.dumps(device_auth, indent=2))
    (worker_dir / "devices" / "paired.json").write_text(paired_json)

print(f"Seeded identity/device-auth.json and devices/paired.json for {data_prefix}-1..{num_workers}")
PYEOF

# Build list of service names for this type/size
WORKER_SERVICES=""
for n in $(seq 1 "${NUM_WORKERS}"); do
    WORKER_SERVICES="${WORKER_SERVICES} ${SERVICE_PREFIX}-${n}"
done

# ── Dispatch subcommand ───────────────────────────────────────────────────────
case "${CMD}" in
    up)
        echo "Cleaning worker data directories (${SERVICE_PREFIX} 1-${NUM_WORKERS})..."
        for n in $(seq 1 "${NUM_WORKERS}"); do
            worker_dir="${POOL_DATA_DIR:-/tmp/lnl-pool}/${DATA_PREFIX}-${n}"
            if [ -d "${worker_dir}" ]; then
                find "${worker_dir}" -mindepth 1 -maxdepth 1 \
                    ! -name "identity" ! -name "devices" \
                    -exec rm -rf {} +
            fi
        done
        echo "Starting LNL OpenClaw worker pool (${WORKER_TYPE}-agent, ${NUM_WORKERS} workers)..."
        docker compose -f "${COMPOSE_FILE}" up -d --remove-orphans ${WORKER_SERVICES}
        echo ""
        echo "Workers ready (${WORKER_TYPE}-agent, ${NUM_WORKERS}):"
        for n in $(seq 1 "${NUM_WORKERS}"); do
            gw_port=$((GW_BASE + n))
            mock_port=$((MOCK_BASE + n))
            echo "  ${SERVICE_PREFIX}-${n}  gateway=ws://localhost:${gw_port}  mock=http://localhost:${mock_port}"
        done
        echo ""
        echo "Run evaluation with matching pool config, e.g.:"
        echo "  ./scripts/run-eval-baseline.sh -i <input.jsonl> --pool docker/worker-pool-${WORKER_TYPE}-${NUM_WORKERS}.yaml"
        ;;
    down)
        if [ "${CMD}" = "down" ] && [ -z "${WORKER_TYPE+unset}" ]; then
            echo "Stopping ALL LNL OpenClaw workers..."
            docker compose -f "${COMPOSE_FILE}" down
        else
            echo "Stopping ${WORKER_TYPE}-agent workers (1-${NUM_WORKERS})..."
            docker compose -f "${COMPOSE_FILE}" stop ${WORKER_SERVICES}
            docker compose -f "${COMPOSE_FILE}" rm -f ${WORKER_SERVICES}
        fi
        ;;
    restart)
        echo "Restarting LNL OpenClaw worker pool (${WORKER_TYPE}-agent, ${NUM_WORKERS} workers)..."
        docker compose -f "${COMPOSE_FILE}" stop ${WORKER_SERVICES}
        docker compose -f "${COMPOSE_FILE}" rm -f ${WORKER_SERVICES}
        for n in $(seq 1 "${NUM_WORKERS}"); do
            worker_dir="${POOL_DATA_DIR:-/tmp/lnl-pool}/${DATA_PREFIX}-${n}"
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
        echo "Usage: $0 [--type single|multi] [--workers N] [up|down|restart|logs]" >&2
        exit 1
        ;;
esac
