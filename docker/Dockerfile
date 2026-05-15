# LNL OpenClaw Worker
#
# Bundles the OpenClaw gateway daemon + LNL mock-external plugin + mock server
# into a single container. Spinning up N containers = N isolated eval slots.
#
# Ports:
#   18789 — OpenClaw WebSocket/HTTP gateway  (--gateway-url / --openclaw-http-url)
#   18888 — LNL mock server HTTP             (--mock-server-url)
#
# Required env vars at runtime:
#   OPENCLAW_GATEWAY_TOKEN   — shared secret; set to any strong random string
#   ANTHROPIC_API_KEY        — or OPENAI_API_KEY, depending on the agent model
#
# Build (from repo root):
#   docker build -f docker/Dockerfile -t lnl-openclaw-worker .
#
# The plugin is copied from plugins/openclaw-mock-external/dist/index.js which
# is committed to the repo. Only re-run `npm run build` in that directory first
# if you have changed the plugin TypeScript source since the last commit.

FROM ghcr.io/openclaw/openclaw:latest

# ── Install Python 3 + pip (Debian/Ubuntu base) ───────────────────────────────
USER root

RUN apt-get update -qq && \
    apt-get install -y --no-install-recommends \
        python3 python3-pip python3-venv gettext-base curl && \
    rm -rf /var/lib/apt/lists/*

# ── Install Python dependencies ───────────────────────────────────────────────
COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --break-system-packages --no-cache-dir \
        -r /tmp/requirements.txt \
        openclaw-sdk

# ── Copy application source ───────────────────────────────────────────────────
WORKDIR /app

COPY src/   src/
COPY config/ config/

# ── Install the mock-external plugin ─────────────────────────────────────────
# Stored OUTSIDE /home/node/.openclaw so the pool bind-mount doesn't hide them.
# The entrypoint copies them into the bind-mount dir at container startup.
RUN mkdir -p /home/node/openclaw-extensions/lnl-mock-external
COPY plugins/openclaw-mock-external/dist/index.js \
     /home/node/openclaw-extensions/lnl-mock-external/index.js
COPY plugins/openclaw-mock-external/openclaw.plugin.json \
     /home/node/openclaw-extensions/lnl-mock-external/openclaw.plugin.json

# ── Copy Azure usage proxy ────────────────────────────────────────────────────
COPY docker/azure_usage_proxy.py /app/docker/azure_usage_proxy.py

# ── Bake in the OpenClaw config template ──────────────────────────────────────
# Stored outside .openclaw so a bind-mount over /home/node/.openclaw (pool mode)
# doesn't hide the template before the entrypoint can expand it.
COPY docker/openclaw-config.json /home/node/openclaw.json.tpl

# ── Entrypoint ────────────────────────────────────────────────────────────────
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Fix ownership so the node user can write its own home
RUN mkdir -p /home/node/.openclaw && chown -R node:node /home/node/.openclaw /home/node/openclaw-extensions /app

USER node

EXPOSE 18789 18888

ENTRYPOINT ["/entrypoint.sh"]
