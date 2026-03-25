FROM python:3.12-slim AS base

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --extra mcp --frozen --no-install-project

COPY mycelium/ mycelium/
RUN uv sync --no-dev --extra mcp --frozen

EXPOSE 8000

ENV MYCELIUM_MCP__TRANSPORT=streamable-http
ENV MYCELIUM_MCP__HOST=0.0.0.0
ENV MYCELIUM_MCP__PORT=8000

CMD ["uv", "run", "mycelium", "serve"]
