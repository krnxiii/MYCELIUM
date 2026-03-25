.PHONY: install install-full up down reset quickstart quickstart-app quickstart-docker test test-unit test-semantic bench lint mcp-server mcp-install mcp-install-http mcp-gate-init mcp-skills-install mcp-rules-install serve render clean uninstall

# ── Installation ────────────────────────────────────────────────
#
#                 │ Embeddings: API      │ Embeddings: TEI (Docker)
#   ──────────────┼──────────────────────┼──────────────────────────
#   App: local    │ make quickstart      │ —
#   App: Docker   │ make quickstart-app  │ make quickstart-docker
#

quickstart:
	$(MAKE) up
	uv sync
	uv run python -c "from mycelium.driver.neo4j_driver import Neo4jDriver; from mycelium.config import load_settings; import asyncio; asyncio.run(Neo4jDriver(load_settings().neo4j).build_indices())"
	$(MAKE) mcp-install
	$(MAKE) mcp-gate-init
	$(MAKE) mcp-skills-install
	@echo "Ready. Run: make serve"

quickstart-app:
	docker compose --profile app up -d --build
	@bash scripts/wait-healthy.sh mycelium-neo4j mycelium-app
	$(MAKE) mcp-install-http
	$(MAKE) mcp-gate-init
	$(MAKE) mcp-skills-install
	@echo "Stack ready: Neo4j :7474 | MCP http://localhost:8000/mcp"

quickstart-docker:
	docker compose -f docker-compose.yml -f docker-compose.full.yml --profile full up -d --build
	@bash scripts/wait-healthy.sh mycelium-neo4j mycelium-app
	$(MAKE) mcp-install-http
	$(MAKE) mcp-gate-init
	$(MAKE) mcp-skills-install
	@echo "Stack ready: Neo4j :7474 | TEI :8090 | MCP http://localhost:8000/mcp"

# ── Granular install ────────────────────────────────────────────

install:
	uv sync

install-full:
	uv sync --all-extras

# ── Infrastructure ──────────────────────────────────────────────

up:
	docker compose up -d
	@bash scripts/wait-healthy.sh mycelium-neo4j

down:
	docker compose down

reset:
	docker compose down -v
	rm -rf $${MYCELIUM_DATA_DIR:-$$HOME/.mycelium}/neo4j
	$(MAKE) up

# ── Testing ─────────────────────────────────────────────────────

test:
	uv run pytest -m "not semantic and not extraction" --cov=mycelium -x -v

test-unit:
	uv run pytest -m "not integration and not semantic and not extraction" --cov=mycelium -x -v

test-semantic:
	uv run pytest -m "semantic" --cov=mycelium -x -v

bench:
	uv run pytest tests/test_benchmark.py -v

lint:
	uv run ruff check mycelium/ tests/
	uv run mypy mycelium/

# ── Runtime ─────────────────────────────────────────────────────

mcp-server:
	uv run python -m mycelium.mcp.server

mcp-install:
	@if command -v claude >/dev/null 2>&1; then \
		claude mcp remove mycelium -s user 2>/dev/null; \
		claude mcp add -t stdio -s user mycelium -- uv run --project $(CURDIR) --extra mcp python -m mycelium.mcp.server; \
		echo "MCP server registered globally (claude mcp list to verify)"; \
	else \
		echo "claude CLI not found — skipping MCP registration"; \
	fi

mcp-install-http:
	@if command -v claude >/dev/null 2>&1; then \
		claude mcp remove mycelium -s user 2>/dev/null; \
		claude mcp add -t http -s user mycelium http://localhost:8000/mcp; \
		echo "MCP server registered via HTTP (http://localhost:8000/mcp)"; \
	else \
		echo "claude CLI not found — skipping MCP registration"; \
	fi

mcp-gate-init:
	@mkdir -p ~/.mycelium
	@touch ~/.mycelium/.read_enabled
	@echo "Gate init: ~/.mycelium/.read_enabled created (read=on, write=off)"

mcp-skills-install:
	@for skill in mycelium-on mycelium-off mycelium-ingest mycelium-recall mycelium-reflect mycelium-distill mycelium-discover; do \
		mkdir -p ~/.claude/skills/$$skill; \
		cp $(CURDIR)/.claude/skills/$$skill/SKILL.md ~/.claude/skills/$$skill/SKILL.md; \
	done
	@echo "Skills installed: /mycelium-on, /mycelium-off, /mycelium-ingest, /mycelium-recall, /mycelium-reflect, /mycelium-distill, /mycelium-discover"

mcp-rules-install:
	@mkdir -p ~/.claude
	@if [ -f ~/.claude/CLAUDE.md ] && grep -qF "MYCELIUM MCP Access Control" ~/.claude/CLAUDE.md; then \
		echo "Access rules already in ~/.claude/CLAUDE.md"; \
	else \
		printf '\n## MYCELIUM MCP Access Control\n- NEVER create `~/.mycelium/.write_enabled` yourself\n- NEVER delete `~/.mycelium/.read_enabled` yourself\n- Use `/mycelium-on` and `/mycelium-off` skills to toggle access\n- If a tool returns "disabled", tell the user to run the skill\n' >> ~/.claude/CLAUDE.md; \
		echo "Access rules added to ~/.claude/CLAUDE.md"; \
	fi

serve:
	uv run mycelium serve

render:
	uv run --extra render mycelium render

clean:
	rm -rf .mypy_cache .pytest_cache .ruff_cache .coverage htmlcov

uninstall:
	@bash scripts/uninstall.sh
