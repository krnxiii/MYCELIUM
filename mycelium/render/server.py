"""Render server: FastAPI serves static + graph API on :8500."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from mycelium.config import load_settings
from mycelium.driver.neo4j_driver import Neo4jDriver
from mycelium.driver.neo4j_driver import MIGRATIONS
from mycelium.render.queries import (
    GRAPH_EDGES,
    GRAPH_NODES,
    GRAPH_STATS,
    NEIGHBOR_EDGES,
    NEIGHBOR_NODES,
    NEURON_SYNAPSES,
    SIGNALS_BY_IDS,
)

STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[arg-type]
    """Startup: connect Neo4j. Shutdown: close."""
    s = load_settings()
    app.state.drv = Neo4jDriver(s.neo4j)
    await app.state.drv.__aenter__()
    for stmt in MIGRATIONS:
        try:
            await app.state.drv.execute_query(stmt)
        except Exception:
            pass
    yield
    await app.state.drv.close()


app = FastAPI(title="MYCELIUM Graph Viewer", lifespan=lifespan)


def _drv() -> Neo4jDriver:
    return app.state.drv  # type: ignore[no-any-return]


# ── Routes ───────────────────────────────────────────────


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/graph")
async def graph() -> dict[str, Any]:
    drv   = _drv()
    nodes = await drv.execute_query(GRAPH_NODES)
    edges = await drv.execute_query(GRAPH_EDGES)
    stats = await drv.execute_query(GRAPH_STATS)
    s     = stats[0] if stats else {"neurons": 0, "synapses": 0}
    return {
        "nodes": nodes,
        "edges": edges,
        "stats": {
            "total_neurons":  s["neurons"],
            "total_synapses": s["synapses"],
            "shown_nodes":    len(nodes),
            "shown_edges":    len(edges),
        },
    }


@app.get("/api/neuron/{uuid}")
async def neuron(uuid: str) -> dict[str, Any]:
    drv      = _drv()
    synapses = await drv.execute_query(NEURON_SYNAPSES, {"uuid": uuid})
    uuids    = list({u for s in synapses for u in (s.get("episodes") or [])})
    sigs     = (
        await drv.execute_query(SIGNALS_BY_IDS, {"uuids": uuids})
        if uuids else []
    )
    return {"synapses": synapses, "signals": sigs}


@app.get("/api/neighbors/{uuid}")
async def neighbors(uuid: str) -> dict[str, Any]:
    drv   = _drv()
    nodes = await drv.execute_query(NEIGHBOR_NODES, {"uuid": uuid})
    edges = await drv.execute_query(NEIGHBOR_EDGES, {"uuid": uuid})
    return {"nodes": nodes, "edges": edges}


app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
