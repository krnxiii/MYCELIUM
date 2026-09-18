FROM python:3.12-slim AS base

WORKDIR /app

# Node is deliberately absent: the Claude Code native binary does not use it at
# runtime, and installing the CLI from npm on the distro node made the build
# unreproducible — an unpinned global package, and npm >=2.1.198 declares
# node>=22 while Debian ships 20 (EBADENGINE on every build).
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Claude Code CLI (cc-cli LLM provider shells out to `claude -p`), installed with
# Anthropic's official installer and pinned so a rebuild yields the same CLI.
# Bump deliberately: docker compose build --build-arg CLAUDE_CODE_VERSION=x.y.z
ARG CLAUDE_CODE_VERSION=2.1.267
RUN curl -fsSL https://claude.ai/install.sh | bash -s "${CLAUDE_CODE_VERSION}"
ENV PATH="/root/.local/bin:${PATH}"

# The installer self-updates in the background by default. In a container that
# writes into an image layer: the update is lost on every restart, re-downloaded
# on every start, and the running CLI silently diverges from the built image.
# The image is the source of truth for the version, so the updater is off.
ENV DISABLE_AUTOUPDATER=1

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
