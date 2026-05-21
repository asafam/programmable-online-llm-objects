# LNL OpenClaw Worker — Docker Pool

Run parallel baseline evaluations without OpenClaw label collisions. Each container bundles an isolated OpenClaw gateway + mock server. Spinning up N containers = N independent evaluation slots.

## What's inside each container

| Component | Container port | Host port (worker-1) | Purpose |
|---|---|---|---|
| OpenClaw gateway | `18789` | `19789` | WebSocket + HTTP gateway |
| LNL mock server | `18888` | `19888` | Mock tool execution + callbacks |
| `lnl-mock-external` plugin | — | — | Wired into gateway; forwards tool calls to mock server |

Host ports start at `19789`/`19888` for worker-1 and increment per worker (+1 per gateway, +1 per mock server). This avoids collisions with a locally-running OpenClaw instance on the default `18789`/`18888` ports — both can run simultaneously.

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

Use the provided script — it reads your local OpenClaw operator token automatically and starts the containers with the correct auth:

```bash
./docker/start-pool.sh          # start (or recreate) the pool
./docker/start-pool.sh down     # stop and remove containers
./docker/start-pool.sh restart  # restart after config changes
./docker/start-pool.sh logs     # tail container logs
```

The script reads the operator token from `~/.openclaw/identity/device-auth.json` and exports it as `OPENCLAW_GATEWAY_TOKEN_1` (and `_2`/`_3`/`_4`) before calling `docker compose`. This token must match your local OpenClaw install — the containers authenticate as the same device.

> **Why the operator token?** The OpenClaw SDK sends your local device's operator token when connecting to the gateway. The gateway only accepts connections that present the same token it was started with. Using a random secret would cause every SDK call to fail with an auth error.

---

## Run an evaluation against the pool

```bash
python -m src.data.evaluate_baseline \
  -i outputs/.../workflows-mods.jsonl \
  --pool docker/worker-pool.yaml \
  --model gpt-4o \
  --runs 3
```

`--pool` reads `docker/worker-pool.yaml`, distributes test cases across all workers using a work queue (each worker picks up the next TC as soon as it's free), and writes a single merged output file. No `--mock-server-url` or `--gateway-url` flags needed — the pool YAML supplies them.

Run a single test case for debugging:

```bash
python -m src.data.evaluate_baseline \
  -i outputs/.../workflows-mods.jsonl \
  --pool docker/worker-pool.yaml \
  --model gpt-4o --tc 1 --verbose
```

---

## How it works

### Bind mount

Each container bind-mounts a host directory into `/home/node/.openclaw`:

```
/tmp/lnl-pool/worker-1  ←→  /home/node/.openclaw  (inside container)
```

`evaluate_baseline.py` writes agent workspace files (SOUL.md, AGENTS.md, state.md, etc.) into the host `data_dir`. The running gateway reads them through the mount. The host directory is created automatically on first `docker compose up`.

### Plugin placement

The `lnl-mock-external` plugin is baked into the image at `/home/node/openclaw-extensions/` (deliberately outside the bind-mount path). The container entrypoint copies it into `/home/node/.openclaw/extensions/` on every startup, so the bind-mount can't hide it.

### Session and state isolation

Before each test case run, `evaluate_baseline.py` clears all session transcripts and `state.md` files for the involved agents. This prevents conversation history and state from a previous run bleeding into the next one — the gateway caches agent state in memory, so the disk files must be clean before each run.

### Config template

The gateway config is generated from `docker/openclaw-config.json` by the entrypoint at startup via `envsubst`. The config disables gateway self-restart (`commands.restart: false`) to prevent the entrypoint from re-running and wiping agent config patches that `evaluate_baseline.py` applies at runtime.

---

## Port assignments

| Worker | Gateway (`--gateway-url`) | Mock server (`--mock-server-url`) |
|---|---|---|
| `worker-1` | `ws://localhost:19789` | `http://localhost:19888` |
| `worker-2` | `ws://localhost:19790` | `http://localhost:19889` |
| `worker-3` | `ws://localhost:19791` | `http://localhost:19890` |
| `worker-4` | `ws://localhost:19792` | `http://localhost:19891` |

---

## Enabling more workers

Workers 2–4 are commented out in `docker-compose.yml` and `worker-pool.yaml`. To enable them:

1. Uncomment the desired workers in both files
2. Run `./docker/start-pool.sh restart`

To add a fifth worker, duplicate a worker block in `docker-compose.yml` with the next port offset:

```yaml
worker-5:
  <<: *worker-base
  container_name: lnl-oc-worker-5
  environment:
    OPENCLAW_GATEWAY_TOKEN: "${OPENCLAW_GATEWAY_TOKEN_5:-changeme-worker-5}"
    LNL_MOCK_SERVER_URL: "http://localhost:18888"
  ports:
    - "19793:18789"
    - "19892:18888"
  volumes:
    - type: bind
      source: "${LNL_POOL_DATA_DIR:-/tmp/lnl-pool}/worker-5"
      target: /home/node/.openclaw
      bind:
        create_host_path: true
```

Add the corresponding entry to `docker/worker-pool.yaml` and export `OPENCLAW_GATEWAY_TOKEN_5` in `start-pool.sh`.

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
./docker/start-pool.sh down

# Also remove the built image
docker rmi lnl-openclaw-worker
```
