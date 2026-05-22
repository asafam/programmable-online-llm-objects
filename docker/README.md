# LNL OpenClaw Worker — Docker Pool

Run parallel baseline evaluations without OpenClaw label collisions. Each container bundles an isolated OpenClaw gateway + mock server. Two named worker types keep single-agent and multi-agent evals completely separate — each type has its own port range and can scale independently to 2, 4, 8, 16, or 24 workers.

## What's inside each container

| Component | Container port | Purpose |
|---|---|---|
| OpenClaw gateway | `18789` | WebSocket + HTTP gateway |
| LNL mock server | `18888` | Mock tool execution + callbacks |
| Azure usage proxy | `18800` | Proxies Azure OpenAI calls (not exposed to host) |
| `lnl-mock-external` plugin | — | Wired into gateway; forwards tool calls to mock server |

---

## Port layout

Three non-overlapping zones on the host:

| Zone | Gateway host ports | Mock host ports |
|---|---|---|
| **Local LNL** (reserved — do not bind) | `18789` | `18888` |
| **single-agent workers** 1–24 | `19789`–`19812` | `19888`–`19911` |
| **multi-agent workers** 1–24 | `20789`–`20812` | `20888`–`20911` |

Formula: `single-worker-N` → gateway `19788+N`, mock `19887+N`. `multi-worker-N` → gateway `20788+N`, mock `20887+N`.

This design lets you run a local non-Docker evaluation (on `18789`/`18888`) alongside two fully loaded Docker pools simultaneously without any port conflicts.

---

## Prerequisites

- Docker and Docker Compose installed
- OpenClaw installed locally and authenticated (`openclaw auth login`) — the container gateway uses the same operator token as your local install
- `.env` in the repo root with `OPENAI_API_KEY` and/or `ANTHROPIC_API_KEY`

---

## Build

From the repo root (one time, or after changing the plugin or Python source):

```bash
docker build -f docker/Dockerfile -t lnl-openclaw-worker .
```

The plugin is copied from `plugins/openclaw-mock-external/dist/index.js` (committed to the repo). Only re-run `npm --prefix plugins/openclaw-mock-external run build` before `docker build` if you've changed the plugin TypeScript source.

Takes ~2–3 minutes on first build; subsequent builds use the Docker layer cache.

---

## Start the pool

Use `./docker/start-pool.sh` — it reads your local OpenClaw operator token automatically and starts the containers with the correct auth:

```bash
# Single-agent workers (ports 19789+/19888+)
./docker/start-pool.sh --type single --workers 8

# Multi-agent workers (ports 20789+/20888+) — can run at the same time
./docker/start-pool.sh --type multi --workers 8

# Stop a specific type
./docker/start-pool.sh --type single down
./docker/start-pool.sh --type multi  down

# Stop ALL workers
./docker/start-pool.sh down

# Restart a pool
./docker/start-pool.sh --type single --workers 8 restart

# Tail logs
./docker/start-pool.sh logs
```

`--workers` accepts any value from 1 to 24. Matching pool YAMLs exist for `2`, `4`, `8`, `16`, and `24`.

The script reads the operator token from `~/.openclaw/identity/device-auth.json` and exports it as `OPENCLAW_GATEWAY_TOKEN_1..N` before calling `docker compose up`. This token must match your local OpenClaw install — the containers authenticate as the same device.

> **Why the operator token?** The OpenClaw SDK sends your local device's operator token when connecting to the gateway. The gateway only accepts connections that present the same token it was started with. Using a random secret would cause every SDK call to fail with an auth error.

---

## Run an evaluation against the pool

```bash
# Single-agent evaluation
python -m src.data.evaluate_baseline \
  -i outputs/.../workflows-mods.jsonl \
  --pool docker/worker-pool-single-8.yaml \
  --single-agent \
  --model gpt-4o \
  --runs 3

# Multi-agent evaluation
python -m src.data.evaluate_baseline \
  -i outputs/.../workflows-mods.jsonl \
  --pool docker/worker-pool-multi-8.yaml \
  --model gpt-4o \
  --runs 3
```

`--pool` reads the YAML, distributes test cases across all workers using a work queue (each worker picks up the next TC as soon as it's free), and writes a single merged output file. No `--mock-server-url` or `--gateway-url` flags needed — the pool YAML supplies them.

Run a single test case for debugging:

```bash
python -m src.data.evaluate_baseline \
  -i outputs/.../workflows-mods.jsonl \
  --pool docker/worker-pool-single-8.yaml \
  --single-agent --model gpt-4o --tc 1 --verbose
```

### Running single-agent and multi-agent in parallel

Start each pool in its own terminal, then launch both evals simultaneously:

```bash
# Terminal 1
./docker/start-pool.sh --type single --workers 8
./scripts/run-eval-baseline.sh -i input.jsonl \
    --pool docker/worker-pool-single-8.yaml \
    --single-agent -o out_single.jsonl

# Terminal 2 (at the same time)
./docker/start-pool.sh --type multi --workers 8
./scripts/run-eval-baseline.sh -i input.jsonl \
    --pool docker/worker-pool-multi-8.yaml \
    -o out_multi.jsonl
```

---

## How it works

### Bind mount

Each container bind-mounts a host directory into `/home/node/.openclaw`:

```
/tmp/lnl-pool/single-worker-1  ←→  /home/node/.openclaw  (inside container)
/tmp/lnl-pool/multi-worker-1   ←→  /home/node/.openclaw  (inside container)
```

`evaluate_baseline.py` writes agent workspace files (SOUL.md, AGENTS.md, state.md, etc.) into the host `data_dir`. The running gateway reads them through the mount. The host directory is created automatically on first `docker compose up`.

### Plugin placement

The `lnl-mock-external` plugin is baked into the image at `/home/node/openclaw-extensions/` (deliberately outside the bind-mount path). The container entrypoint copies it into `/home/node/.openclaw/extensions/` on every startup, so the bind-mount can't hide it.

### Session and state isolation

Before each test case run, `evaluate_baseline.py` clears all session transcripts and `state.md` files for the involved agents. This prevents conversation history and state from a previous run bleeding into the next one — the gateway caches agent state in memory, so the disk files must be clean before each run.

### Config template

The gateway config is generated from `docker/openclaw-config.json` by the entrypoint at startup via `envsubst`. The config disables gateway self-restart (`commands.restart: false`) to prevent the entrypoint from re-running and wiping agent config patches that `evaluate_baseline.py` applies at runtime.

---

## Pool YAML files

Pre-built YAMLs for standard pool sizes:

| File | Type | Workers |
|---|---|---|
| `worker-pool-single-2.yaml` | single-agent | 2 |
| `worker-pool-single-4.yaml` | single-agent | 4 |
| `worker-pool-single-8.yaml` | single-agent | 8 |
| `worker-pool-single-16.yaml` | single-agent | 16 |
| `worker-pool-single-24.yaml` | single-agent | 24 |
| `worker-pool-multi-2.yaml` | multi-agent | 2 |
| `worker-pool-multi-4.yaml` | multi-agent | 4 |
| `worker-pool-multi-8.yaml` | multi-agent | 8 |
| `worker-pool-multi-16.yaml` | multi-agent | 16 |
| `worker-pool-multi-24.yaml` | multi-agent | 24 |

Pass any of these as `--pool docker/<file>` to `evaluate_baseline.py` or the convenience script.

---

## Passing API keys

`docker-compose.yml` loads `.env` from the repo root via `env_file`. Any key in `.env` is forwarded into the container:

```
OPENAI_API_KEY=...
ANTHROPIC_API_KEY=...
GOOGLE_API_KEY=...
```

---

## Teardown

```bash
# Stop one type
./docker/start-pool.sh --type single down
./docker/start-pool.sh --type multi  down

# Stop everything
./docker/start-pool.sh down

# Also remove the built image
docker rmi lnl-openclaw-worker
```
