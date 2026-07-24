FROM python:3.12-slim AS base

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl git nodejs npm \
    && rm -rf /var/lib/apt/lists/*
RUN npm install -g @anthropic-ai/claude-code
RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --extra mcp --extra telegram --extra render --frozen --no-install-project

COPY mycelium/ mycelium/
RUN uv sync --no-dev --extra mcp --extra telegram --extra render --frozen

EXPOSE 9631

ENV MYCELIUM_MCP__TRANSPORT=streamable-http
ENV MYCELIUM_MCP__HOST=0.0.0.0
ENV MYCELIUM_MCP__PORT=9631
# Bind inside the container on all interfaces so the published port is
# reachable; host-side exposure is controlled by the compose port bind addr.
ENV MYCELIUM_RENDER__HOST=0.0.0.0

CMD ["uv", "run", "mycelium", "serve"]
