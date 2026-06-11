"""Main FastAPI application.

Run
---
    uvicorn api.server:app --host 0.0.0.0 --port 8000 --reload

Endpoints
---------
    POST   /webhook/alert              TradingView alert receiver
    GET    /api/accounts               account CRUD
    GET    /api/portfolio/{id}         live portfolio snapshot
    GET    /api/trades/{id}            trade history + equity curve
    GET    /api/signals/{id}           signal log
    GET    /api/strategies             strategy list + enable/disable
    WS     /ws                         live push to dashboard
    GET    /health                     liveness check

Static files
------------
    The React build (frontend/dist/) is served at "/" so the single
    binary `uvicorn api.server:app` serves both the API and the UI.
    During development, Vite's dev server handles the frontend at :5173
    and proxies /api/* and /webhook/* to :8000.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from api.routes import accounts, portfolio, trades, signals, strategies, webhook
from api.ws import router as ws_router, _keepalive_loop
from db.database import get_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

_FRONTEND_DIST = Path(__file__).parent.parent / "frontend" / "dist"


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialise DB (applies migrations) and start WS keepalive
    get_db()
    keepalive = asyncio.create_task(_keepalive_loop())
    logger.info("regime-trader API server started")
    yield
    keepalive.cancel()
    logger.info("regime-trader API server stopped")


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Regime Trader",
    description="TradingView → Tradovate monitoring and dashboard API",
    version="2.0.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
)

# Allow Vite dev server (port 5173) to call this API during development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:8000"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(ws_router)
app.include_router(webhook.router)
app.include_router(accounts.router)
app.include_router(portfolio.router)
app.include_router(trades.router)
app.include_router(signals.router)
app.include_router(strategies.router)


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok", "version": app.version}


# ── Serve React build in production ──────────────────────────────────────────

if _FRONTEND_DIST.exists():
    app.mount("/", StaticFiles(directory=str(_FRONTEND_DIST), html=True), name="spa")


# ── Dev entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "api.server:app",
        host="0.0.0.0",
        port=int(os.getenv("WEBHOOK_PORT", "8000")),
        reload=True,
        log_level="info",
    )
