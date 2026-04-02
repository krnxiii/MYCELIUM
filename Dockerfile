FROM python:3.12-slim AS base

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl nodejs npm && rm -rf /var/lib/apt/lists/*
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
