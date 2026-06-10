"""
Lossless backend.

FastAPI app that exposes the agent (POST /api/chat), the store dashboard
(GET /api/dashboard), demo controls (POST /api/demo/*), and the human
approval endpoint (POST /api/actions/{id}/approve). Also serves the
single-page UI from ./static.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .agent import agent
from .config import get_settings
from .mcp_bridge import mcp_bridge
from .store import store
from .telemetry import get_backend

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("lossless")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await mcp_bridge.start()
    log.info("Lossless ready. MCP mode: %s", mcp_bridge.mode)
    yield
    await mcp_bridge.stop()


app = FastAPI(
    title="Lossless — AI Store Ops Agent",
    version="0.1.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ----- schemas -----

class ChatRequest(BaseModel):
    message: str
    reset: bool = False


class ChatResponse(BaseModel):
    reply: str
    tool_calls: List[Dict[str, Any]] = []
    proposed_action_id: Optional[str] = None
    mode: str


class InjectRequest(BaseModel):
    kind: str = "payments_timeout"  # payments_timeout | cart_slow | search_outage


# ----- endpoints -----

@app.get("/api/status")
def status():
    s = get_settings()
    return {
        "name": "Lossless",
        "tagline": "AI Store Ops Agent for brick-and-mortar retail",
        "gemini_configured": s.gemini_configured,
        "gemini_model": s.gemini_model,
        "dynatrace_configured": s.dynatrace_configured,
        "mcp_mode": mcp_bridge.mode,
        "demo_mode": s.demo_mode,
    }


@app.get("/api/dashboard")
def dashboard():
    backend = get_backend()
    return {
        "backend": backend.name,
        "metrics": backend.health(),
        "problems": backend.problems(),
        "revenue_impact_30m": backend.revenue_impact(30),
    }


@app.get("/api/incidents")
def incidents():
    return {
        "open": [i.to_summary() for i in store.open_incidents()],
        "all": [i.to_summary() for i in store.all_incidents()],
    }


@app.get("/api/actions/recent")
def recent_actions(limit: int = 25):
    return {"actions": store.recent_actions(limit)}


@app.post("/api/actions/{action_id}/approve")
def approve_action(action_id: str):
    entry = store.approve_action(action_id)
    if not entry:
        raise HTTPException(404, f"no such action {action_id}")
    return {"action_id": action_id, "approved": entry["approved"], "entry": entry}


@app.post("/api/demo/inject")
def demo_inject(req: InjectRequest):
    inc = store.inject_incident(req.kind)
    return {"injected": inc.to_summary()}


@app.post("/api/demo/reset")
def demo_reset():
    n = store.clear_incidents()
    agent.reset()
    return {"cleared_incidents": n}


@app.post("/api/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    if req.reset:
        agent.reset()
    try:
        turn = await agent.chat(req.message)
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    return ChatResponse(
        reply=turn.final_text,
        tool_calls=turn.tool_calls,
        proposed_action_id=turn.proposed_action_id,
        mode=turn.mode,
    )


# ----- static UI -----

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
def index():
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        return HTMLResponse("<h1>Lossless</h1><p>UI not built.</p>")
    return HTMLResponse(index_path.read_text(encoding="utf-8"))


@app.get("/healthz")
def healthz():
    return JSONResponse({"ok": True})
