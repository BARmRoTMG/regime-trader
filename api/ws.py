"""WebSocket manager — pushes live updates to all connected dashboard tabs.

Usage (server side)
-------------------
    from api.ws import broadcast
    await broadcast({"type": "signal", ...})

Usage (client side — frontend)
-------------------------------
    const ws = new WebSocket("ws://localhost:8000/ws");
    ws.onmessage = (e) => { const msg = JSON.parse(e.data); ... };

Message types
-------------
    signal   — a new TradingView alert was received
    equity   — equity snapshot updated
    ping     — keepalive (sent every 30s)
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)
router = APIRouter(tags=["websocket"])

_connections: list[WebSocket] = []


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    _connections.append(ws)
    logger.info("WS client connected (%d total)", len(_connections))
    try:
        while True:
            # Keep connection alive; client messages are ignored
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _connections.remove(ws)
        logger.info("WS client disconnected (%d remaining)", len(_connections))


async def broadcast(data: dict[str, Any]) -> None:
    """Send *data* as JSON to every connected WebSocket client."""
    if not _connections:
        return
    message = json.dumps(data)
    dead: list[WebSocket] = []
    for ws in _connections:
        try:
            await ws.send_text(message)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _connections.remove(ws)


async def _keepalive_loop() -> None:
    """Ping all clients every 30 s to prevent idle disconnects."""
    while True:
        await asyncio.sleep(30)
        await broadcast({"type": "ping"})
