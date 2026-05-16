# regime-trader

An HMM-driven market-regime detection and allocation trading system that runs on Alpaca.

## How it works

1. **Regime detection** – A Gaussian HMM is trained on rolling windows of volatility and momentum features. It classifies each bar into one of three regimes: `low_vol`, `mid_vol`, or `high_vol`.
2. **Allocation strategy** – Each regime maps to a target equity fraction and leverage multiplier. A trend-confirmation signal further adjusts mid-vol allocations.
3. **Risk management** – Every order passes through a risk layer that enforces per-trade risk limits, position caps, leverage ceilings, and intraday / intraweek drawdown circuit-breakers.
4. **Walk-forward backtest** – The system validates itself with a strict walk-forward engine that prevents look-ahead bias.
5. **Stress testing** – Crash injection, gap simulation, and vol-spike scenarios verify robustness under tail conditions.

## Project structure

```
regime-trader/
├── config/              # YAML configuration and credential templates
├── core/                # HMM engine, strategies, risk manager, signal generator
├── broker/              # Alpaca API client, order executor, position tracker
├── data/                # Market data feed and feature engineering
├── monitoring/          # Structured logger, Rich dashboard, alerts
├── backtest/            # Walk-forward backtester, performance analytics, stress tests
├── tests/               # Unit tests (pytest)
└── main.py              # Entry point
```

## Quick start

```bash
# 1. Clone and create a virtual environment
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure credentials
cp .env.example .env
# edit .env with your Alpaca API keys

cp config/credentials.yaml.example config/credentials.yaml
# edit config/credentials.yaml (optional — .env takes precedence)

# 4. Review / adjust settings
# edit config/settings.yaml

# 5. Run a walk-forward backtest
python main.py --mode backtest

# 6. Run stress tests
python main.py --mode stress

# 7. Start live paper trading
python main.py --mode live
```

## Configuration

All parameters live in [config/settings.yaml](config/settings.yaml). Key sections:

| Section | Purpose |
|---------|---------|
| `broker` | Trading symbols, timeframe, paper vs. live |
| `hmm` | HMM state counts, convergence, stability/flicker guards |
| `strategy` | Regime-to-allocation mapping, leverage, rebalance threshold |
| `risk` | Per-trade risk, exposure caps, drawdown circuit-breakers |
| `backtest` | Walk-forward window sizes, slippage, initial capital |
| `monitoring` | Dashboard refresh interval, alert rate limits |

## Running tests

```bash
pytest tests/ -v
```

## Modes

| Flag | Description |
|------|-------------|
| `--mode live` | Connect to Alpaca and trade (respects `paper_trading` setting) |
| `--mode backtest` | Walk-forward backtest on historical data |
| `--mode stress` | Run crash / gap / vol-spike stress scenarios |
| `--symbols SPY QQQ` | Override symbol universe from the command line |
| `--log-level DEBUG` | Verbose logging |

## Security

- Never commit `.env` or `config/credentials.yaml` — both are git-ignored.
- Use Alpaca paper trading (`paper_trading: true`) until the strategy is validated.
- The risk manager enforces hard drawdown halts independent of strategy logic.

## Dependencies

See [requirements.txt](requirements.txt). Key libraries:

- `hmmlearn` – Gaussian HMM fitting
- `alpaca-py` – Alpaca REST and streaming API
- `ta` – Technical analysis indicators
- `rich` – Terminal dashboard
- `scikit-learn` – BIC model selection utilities
