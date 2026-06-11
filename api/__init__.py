"""REST API and WebSocket server for the regime-trader dashboard.

The server has two responsibilities:
1. Receive TradingView webhook alerts (POST /webhook/alert) and persist them.
2. Serve the React dashboard with portfolio data, trade history, and live
   push updates via WebSocket.

Entry point
-----------
    uvicorn api.server:app --host 0.0.0.0 --port 8000 --reload

Environment variables (from .env)
----------------------------------
    WEBHOOK_SECRET   shared secret embedded in TradingView alert JSON
    DB_PATH          path to SQLite file (default: data/regime_trader.db)
    WEBHOOK_PORT     port for uvicorn (default: 8000)
"""
