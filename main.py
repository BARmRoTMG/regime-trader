"""regime-trader – entry point.

Usage
-----
Live trading (paper mode):
    python main.py --mode live

Walk-forward backtest:
    python main.py --mode backtest

Stress test:
    python main.py --mode stress

Single-symbol debug run:
    python main.py --mode live --symbols SPY --log-level DEBUG
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

from backtest.backtester import BacktestConfig, WalkForwardBacktester
from backtest.performance import PerformanceAnalyzer
from backtest.stress_test import StressTester
from broker.alpaca_client import AlpacaClient, AlpacaConfig
from broker.order_executor import OrderExecutor
from broker.position_tracker import PositionTracker
from core.hmm_engine import HMMConfig, HMMEngine
from core.regime_strategies import RegimeStrategy, StrategyConfig
from core.risk_manager import RiskConfig, RiskManager
from core.signal_generator import SignalGenerator
from data.feature_engineering import FeatureEngineer
from data.market_data import MarketDataFeed
from monitoring.alerts import AlertConfig, AlertManager
from monitoring.dashboard import Dashboard
from monitoring.logger import setup_logging, get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Configuration loading
# ---------------------------------------------------------------------------


def load_settings(path: Path = Path("config/settings.yaml")) -> dict:
    """Load settings.yaml and return the parsed dict."""
    ...


def load_credentials(path: Path = Path("config/credentials.yaml")) -> dict:
    """Load credentials.yaml (optional; falls back to env vars)."""
    ...


def build_alpaca_config(creds: dict) -> AlpacaConfig:
    """Construct AlpacaConfig preferring env vars over credentials.yaml."""
    ...


def build_hmm_config(settings: dict) -> HMMConfig:
    """Parse settings['hmm'] into an HMMConfig."""
    ...


def build_strategy_config(settings: dict) -> StrategyConfig:
    """Parse settings['strategy'] into a StrategyConfig."""
    ...


def build_risk_config(settings: dict) -> RiskConfig:
    """Parse settings['risk'] into a RiskConfig."""
    ...


def build_backtest_config(settings: dict) -> BacktestConfig:
    """Parse settings['backtest'] into a BacktestConfig."""
    ...


def build_alert_config(creds: dict, settings: dict) -> AlertConfig:
    """Build AlertConfig from credentials and monitoring settings."""
    ...


# ---------------------------------------------------------------------------
# Run modes
# ---------------------------------------------------------------------------


def run_live(settings: dict, alpaca_cfg: AlpacaConfig) -> None:
    """Start the live (paper or real) trading loop.

    Steps
    -----
    1. Connect to Alpaca and load history.
    2. Fit initial HMM on min_train_bars of history.
    3. Start the dashboard and alert manager.
    4. Enter the main event loop:
       a. On each new bar, re-compute features.
       b. Generate a SignalPacket.
       c. Execute approved signals via OrderExecutor.
       d. Update PositionTracker and dashboard.
    """
    ...


def run_backtest(settings: dict, alpaca_cfg: AlpacaConfig) -> None:
    """Fetch historical data and run a walk-forward backtest.

    Steps
    -----
    1. Fetch full history from Alpaca REST.
    2. Build WalkForwardBacktester and run().
    3. Analyse results with PerformanceAnalyzer.
    4. Print the performance report.
    """
    ...


def run_stress(settings: dict, alpaca_cfg: AlpacaConfig) -> None:
    """Run the stress-test battery on historical data.

    Steps
    -----
    1. Fetch history and run a baseline backtest.
    2. Instantiate StressTester with the baseline equity curve.
    3. Run all scenarios and print summary table.
    """
    ...


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="regime-trader",
        description="HMM-based regime-detection trading system.",
    )
    parser.add_argument(
        "--mode",
        choices=["live", "backtest", "stress"],
        default="live",
        help="Execution mode (default: live).",
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=None,
        help="Override symbol universe from settings.yaml.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: INFO).",
    )
    parser.add_argument(
        "--settings",
        type=Path,
        default=Path("config/settings.yaml"),
        help="Path to settings.yaml.",
    )
    return parser.parse_args()


def main() -> int:
    load_dotenv()
    args = parse_args()
    setup_logging(level=args.log_level)

    settings = load_settings(args.settings)

    if args.symbols:
        settings["broker"]["symbols"] = args.symbols

    try:
        creds = load_credentials()
    except FileNotFoundError:
        creds = {}

    alpaca_cfg = build_alpaca_config(creds)

    mode_fn = {"live": run_live, "backtest": run_backtest, "stress": run_stress}
    mode_fn[args.mode](settings, alpaca_cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
