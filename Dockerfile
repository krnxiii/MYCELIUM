FROM python:3.12-slim AS base

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl git docker.io nodejs npm \
    && rm -rf /var/lib/apt/lists/*

# Docker Compose v2 plugin (for /update self-update from telegram bot)
RUN mkdir -p /usr/local/lib/docker/cli-plugins \
    && curl -fsSL -o /usr/local/lib/docker/cli-plugins/docker-compose \
       "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-$(uname -m)" \
    && chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
RUN npm install -g @anthropic-ai/claude-code
RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --extra mcp --extra telegram --frozen --no-install-project

COPY mycelium/ mycelium/
RUN uv sync --no-dev --extra mcp --extra telegram --frozen

EXPOSE 9631

ENV MYCELIUM_MCP__TRANSPORT=streamable-http
ENV MYCELIUM_MCP__HOST=0.0.0.0
ENV MYCELIUM_MCP__PORT=9631

CMD ["uv", "run", "mycelium", "serve"]
