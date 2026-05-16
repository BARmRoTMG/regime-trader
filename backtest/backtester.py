"""Walk-forward allocation backtester.

DESIGN: Allocation-based, not individual-trade-based.
The backtester sets a TARGET PORTFOLIO ALLOCATION each bar based on the
detected volatility regime and rebalances only when that allocation changes
meaningfully (>10%).  This is how real systematic strategies work.

Allocation math (exact)
-----------------------
    equity = cash + sum(shares[sym] * price[sym])
    target_shares[sym] = int(equity * signal.effective_notional_pct() / price[sym])
    delta[sym] = target_shares[sym] - current_shares[sym]
    cash -= delta[sym] * exec_price[sym]          # exec_price includes slippage

When leverage > 1 (e.g., 1.25x in low-vol), total exposure > equity,
so cash turns negative (simulated margin).  equity = cash + share_value
is still correct because share value exceeds the margin debt.

Fill delay
----------
Signal detected at bar N close → executed at bar N+1 open.
This mirrors live trading where you see EOD regime and execute next morning.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd

from core.hmm_engine import HMMConfig, HMMEngine, RegimeState
from core.regime_strategies import Signal, StrategyConfig, StrategyOrchestrator
from core.risk_manager import RiskConfig
from data.feature_engineering import FEATURE_COLUMNS, build_features

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Config / result dataclasses
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class BacktestConfig:
    """Walk-forward simulation parameters."""

    slippage_pct: float = 0.0005        # one-way slippage fraction
    initial_capital: float = 100_000.0
    train_window: int = 252             # IS bars per fold
    test_window: int = 126              # OOS bars per fold
    step_size: int = 126               # bars to advance between folds
    risk_free_rate: float = 0.045
    warm_up_bars: int = 252             # extra history before IS (feature warm-up)
    rebalance_threshold: float = 0.10   # min drift to trigger rebalance
    commission_pct: float = 0.0         # Alpaca is commission-free
    fill_delay_bars: int = 1            # 1 = execute at next bar's open


@dataclass
class FoldResult:
    """Simulation results for one walk-forward fold."""

    fold_index: int
    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime
    equity_curve: pd.Series            # DatetimeIndex → NAV
    regime_series: pd.Series           # DatetimeIndex → regime label
    confidence_series: pd.Series       # DatetimeIndex → regime probability
    trades: pd.DataFrame               # one row per rebalance leg
    metrics: dict[str, float] = field(default_factory=dict)


@dataclass
class BacktestResult:
    """Aggregated results across all walk-forward folds."""

    config: BacktestConfig
    symbols: list[str]
    primary_symbol: str
    folds: list[FoldResult] = field(default_factory=list)
    combined_equity: pd.Series = field(default_factory=pd.Series)
    combined_regime: pd.Series = field(default_factory=pd.Series)
    combined_confidence: pd.Series = field(default_factory=pd.Series)
    combined_trades: pd.DataFrame = field(default_factory=pd.DataFrame)
    summary_metrics: dict[str, float] = field(default_factory=dict)

    # The raw primary-symbol close prices for benchmark comparisons
    primary_closes: pd.Series = field(default_factory=pd.Series)


# ─────────────────────────────────────────────────────────────────────────────
# Backtester
# ─────────────────────────────────────────────────────────────────────────────


class WalkForwardBacktester:
    """Rolling walk-forward regime-allocation backtester.

    Parameters
    ----------
    ohlcv:
        MultiIndex (symbol, timestamp) OHLCV DataFrame.  Must contain at
        least ``warm_up_bars + train_window + test_window`` rows per symbol.
    hmm_config, strategy_config, risk_config, backtest_config:
        Component configurations.
    symbols:
        Universe of symbols.  The first symbol (or SPY if present) is used
        as the *primary* for HMM regime detection; all symbols receive equal
        allocation within the target equity fraction.
    """

    def __init__(
        self,
        ohlcv: pd.DataFrame,
        hmm_config: HMMConfig,
        strategy_config: StrategyConfig,
        risk_config: RiskConfig,
        backtest_config: BacktestConfig,
        symbols: list[str],
    ) -> None:
        if not isinstance(ohlcv.index, pd.MultiIndex):
            raise ValueError("ohlcv must have a MultiIndex (symbol, date).")
        self.ohlcv = ohlcv
        self.hmm_config = hmm_config
        self.strategy_config = strategy_config
        self.risk_config = risk_config
        self.backtest_config = backtest_config
        self.symbols = symbols

        # Determine primary symbol for HMM regime detection
        self.primary = "SPY" if "SPY" in symbols else symbols[0]

        # Pre-split OHLCV by symbol for fast access
        self._by_sym: dict[str, pd.DataFrame] = {}
        all_syms_in_data = ohlcv.index.get_level_values(0).unique().tolist()
        for sym in symbols:
            if sym in all_syms_in_data:
                self._by_sym[sym] = ohlcv.loc[sym].sort_index()
        if self.primary not in self._by_sym:
            raise ValueError(f"Primary symbol '{self.primary}' not in ohlcv index.")

    # ─────────────────────────────────────────────────────────────────
    # Public entry point
    # ─────────────────────────────────────────────────────────────────

    def run(self) -> BacktestResult:
        """Execute the full walk-forward backtest.

        Returns
        -------
        BacktestResult
            Aggregated equity curve, regime history, trades, and summary metrics.
        """
        folds_data = self._generate_folds()
        if not folds_data:
            raise RuntimeError("No valid walk-forward folds found. Provide more history.")

        logger.info(
            "WalkForwardBacktester: %d folds  |  IS=%d  OOS=%d  step=%d  primary=%s",
            len(folds_data), self.backtest_config.train_window,
            self.backtest_config.test_window, self.backtest_config.step_size,
            self.primary,
        )

        fold_results: list[FoldResult] = []
        for fold_idx, (feature_dates, is_dates, oos_dates) in enumerate(folds_data):
            logger.info(
                "Fold %d/%d  IS=[%s → %s]  OOS=[%s → %s]",
                fold_idx + 1, len(folds_data),
                is_dates[0].date(), is_dates[-1].date(),
                oos_dates[0].date(), oos_dates[-1].date(),
            )
            try:
                fold = self._run_fold(fold_idx, feature_dates, is_dates, oos_dates)
                fold_results.append(fold)
            except Exception as exc:
                logger.error("Fold %d failed: %s", fold_idx, exc, exc_info=True)

        return self._aggregate_results(fold_results)

    # ─────────────────────────────────────────────────────────────────
    # Fold generation
    # ─────────────────────────────────────────────────────────────────

    def _generate_folds(self) -> list[tuple[pd.DatetimeIndex, pd.DatetimeIndex, pd.DatetimeIndex]]:
        """Build rolling (feature_dates, is_dates, oos_dates) triples.

        feature_dates = warm_up + IS + OOS (for causal feature computation).
        is_dates      = IS only (for HMM training).
        oos_dates     = OOS only (for simulation).

        Train / test windows never overlap; test_start is always > train_end.
        """
        dates = self._get_dates()
        n = len(dates)
        cfg = self.backtest_config

        required = cfg.warm_up_bars + cfg.train_window + cfg.test_window
        if n < required:
            logger.warning(
                "Only %d dates available; need %d for at least one fold.", n, required
            )
            return []

        folds = []
        # IS starts after the warm-up block
        is_start_idx = cfg.warm_up_bars

        while is_start_idx + cfg.train_window + cfg.test_window <= n:
            is_end_idx = is_start_idx + cfg.train_window
            oos_end_idx = is_end_idx + cfg.test_window

            feat_start_idx = is_start_idx - cfg.warm_up_bars   # always ≥ 0
            feature_dates = dates[feat_start_idx:oos_end_idx]
            is_dates = dates[is_start_idx:is_end_idx]
            oos_dates = dates[is_end_idx:oos_end_idx]

            # Sanity: OOS must start strictly after IS ends
            assert is_dates[-1] < oos_dates[0], "Fold overlap detected!"

            folds.append((feature_dates, is_dates, oos_dates))
            is_start_idx += cfg.step_size

        return folds

    def _get_dates(self) -> pd.DatetimeIndex:
        """Sorted unique dates from the primary symbol."""
        return self._by_sym[self.primary].index.sort_values().unique()

    # ─────────────────────────────────────────────────────────────────
    # Fold execution
    # ─────────────────────────────────────────────────────────────────

    def _run_fold(
        self,
        fold_idx: int,
        feature_dates: pd.DatetimeIndex,
        is_dates: pd.DatetimeIndex,
        oos_dates: pd.DatetimeIndex,
    ) -> FoldResult:
        """Train on IS dates; simulate bar-by-bar on OOS dates.

        No information from OOS leaks into IS:
        • HMM is trained exclusively on IS features.
        • OOS regime states are computed via the forward algorithm run from
          the IS start through each OOS bar (forward algorithm is causal).
        """
        cfg = self.backtest_config
        prim_ohlcv = self._by_sym[self.primary]

        # ── 1. Compute features on full feature window (warm-up + IS + OOS) ──
        prim_feature_ohlcv = prim_ohlcv[prim_ohlcv.index.isin(feature_dates)]
        features_full = build_features(prim_feature_ohlcv, normalise=True, dropna=True)

        # IS portion of features (for HMM training)
        features_is = features_full[features_full.index <= is_dates[-1]]

        if len(features_is) < max(self.hmm_config.min_train_bars // 2, 50):
            raise RuntimeError(
                f"Fold {fold_idx}: only {len(features_is)} IS feature rows — "
                "not enough to train HMM. Increase warm_up_bars or train_window."
            )

        # ── 2. Train HMM on IS features ──
        is_ohlcv_for_fit = prim_ohlcv[prim_ohlcv.index <= is_dates[-1]]
        engine = HMMEngine(config=self.hmm_config)
        # Temporarily lower min_train_bars to whatever IS gives us
        engine.config.min_train_bars = max(len(features_is) - 20, 50)
        engine.fit(is_ohlcv_for_fit)

        regime_infos = engine.get_all_regime_infos()
        orchestrator = StrategyOrchestrator(self.strategy_config, regime_infos)

        # ── 3. Forward algorithm on full feature sequence (IS + OOS) ──
        features_combined = features_full[features_full.index >= feature_dates[0]]
        X_all = features_combined.values.astype(float)
        all_proba = engine.predict_regime_filtered(X_all)   # (T_full, n_states)

        # Map feature dates to probability index
        feat_date_to_idx = {d: i for i, d in enumerate(features_combined.index)}

        # ── 4. Apply stability filter bar-by-bar over OOS ──
        engine._reset_stability_state()
        oos_feat_dates = [d for d in features_combined.index if d in set(oos_dates)]
        regime_states_oos: dict[pd.Timestamp, RegimeState] = {}
        for feat_date in oos_feat_dates:
            idx = feat_date_to_idx[feat_date]
            proba = all_proba[idx]
            state_id = int(np.argmax(proba))
            prob = float(proba[state_id])
            rs = engine._apply_stability_filter(state_id, prob, proba, feat_date)
            regime_states_oos[feat_date] = rs

        # ── 5. Bar-by-bar OOS simulation ──
        equity_curve, regime_series, confidence_series, trades = self._simulate_oos(
            oos_dates=oos_dates,
            regime_states=regime_states_oos,
            engine=engine,
            orchestrator=orchestrator,
            prim_ohlcv=prim_ohlcv,
        )

        metrics = self._fold_metrics(equity_curve)
        metrics["n_trades"] = float(len(trades))

        return FoldResult(
            fold_index=fold_idx,
            train_start=is_dates[0].to_pydatetime(),
            train_end=is_dates[-1].to_pydatetime(),
            test_start=oos_dates[0].to_pydatetime(),
            test_end=oos_dates[-1].to_pydatetime(),
            equity_curve=equity_curve,
            regime_series=regime_series,
            confidence_series=confidence_series,
            trades=trades,
            metrics=metrics,
        )

    # ─────────────────────────────────────────────────────────────────
    # OOS simulation
    # ─────────────────────────────────────────────────────────────────

    def _simulate_oos(
        self,
        oos_dates: pd.DatetimeIndex,
        regime_states: dict[pd.Timestamp, RegimeState],
        engine: HMMEngine,
        orchestrator: StrategyOrchestrator,
        prim_ohlcv: pd.DataFrame,
    ) -> tuple[pd.Series, pd.Series, pd.Series, pd.DataFrame]:
        """Bar-by-bar allocation simulation on the OOS period.

        Returns (equity_curve, regime_series, confidence_series, trades_df).
        """
        cfg = self.backtest_config
        cash = cfg.initial_capital
        shares: dict[str, float] = {sym: 0.0 for sym in self.symbols}

        eq_curve: dict[pd.Timestamp, float] = {}
        reg_series: dict[pd.Timestamp, str] = {}
        conf_series: dict[pd.Timestamp, float] = {}
        trade_rows: list[dict] = []

        pending_signals: Optional[list[Signal]] = None
        pending_regime: Optional[str] = None
        prev_weights: dict[str, float] = {}

        for date in oos_dates:
            open_px = self._prices_on(date, "open")
            close_px = self._prices_on(date, "close")

            # ── Execute pending rebalance at today's OPEN (fill delay = 1 bar) ──
            if pending_signals is not None:
                nav_open = self._nav(cash, shares, open_px)
                cash, new_trades = self._execute_rebalance(
                    pending_signals, shares, open_px, nav_open,
                    date, pending_regime or "UNKNOWN",
                )
                shares = {sig.symbol: int(nav_open * sig.effective_notional_pct() / open_px[sig.symbol])
                          for sig in pending_signals if sig.symbol in open_px and open_px[sig.symbol] > 0}
                trade_rows.extend(new_trades)
                pending_signals = None
                pending_regime = None

            # ── Mark to market at close ──
            nav = self._nav(cash, shares, close_px)
            eq_curve[date] = nav

            # ── Regime state for this bar ──
            rs = regime_states.get(date)
            if rs is None:
                # No feature row for this date (e.g., first OOS bars still in warm-up)
                reg_series[date] = "UNKNOWN"
                conf_series[date] = 0.0
                continue
            reg_series[date] = rs.label
            conf_series[date] = rs.probability

            # ── Strategy signals ──
            bars_dict = self._bars_to_date(date, prim_ohlcv)
            signals = orchestrator.generate_signals(
                self.symbols, bars_dict, rs, is_flickering=engine.is_flickering()
            )
            if not signals:
                continue

            # ── Check rebalance trigger ──
            target_w = {s.symbol: s.effective_notional_pct() for s in signals}
            current_w = {sym: shares[sym] * close_px.get(sym, 0.0) / max(nav, 1.0)
                         for sym in self.symbols}
            if orchestrator.needs_rebalance(target_w, current_w):
                pending_signals = signals
                pending_regime = rs.label

        equity_s = pd.Series(eq_curve, name="nav").sort_index()
        regime_s = pd.Series(reg_series, name="regime").sort_index()
        conf_s = pd.Series(conf_series, name="confidence").sort_index()
        trades_df = pd.DataFrame(trade_rows) if trade_rows else pd.DataFrame(
            columns=["date", "symbol", "side", "shares", "price", "notional",
                     "slippage_cost", "regime", "confidence"]
        )
        return equity_s, regime_s, conf_s, trades_df

    def _execute_rebalance(
        self,
        signals: list[Signal],
        shares: dict[str, float],
        open_px: dict[str, float],
        nav: float,
        date: pd.Timestamp,
        regime_label: str,
    ) -> tuple[float, list[dict]]:
        """Execute a rebalance at open prices; return updated cash and trade records."""
        cfg = self.backtest_config
        trade_rows: list[dict] = []

        # Compute target shares for every signal
        target: dict[str, int] = {}
        for sig in signals:
            px = open_px.get(sig.symbol, 0.0)
            if px > 0:
                target[sig.symbol] = int(nav * sig.effective_notional_pct() / px)
            else:
                target[sig.symbol] = 0

        # Flat any symbol not in signals
        for sym in list(shares.keys()):
            if sym not in target:
                target[sym] = 0

        # Sort: sells first (free up cash), then buys
        deltas = {sym: target.get(sym, 0) - int(shares[sym]) for sym in target}
        ordered = sorted(deltas.keys(), key=lambda s: deltas[s])  # negatives first

        cash_delta = 0.0
        for sym in ordered:
            delta = deltas[sym]
            if delta == 0:
                continue
            px = open_px.get(sym, 0.0)
            if px <= 0:
                continue

            side = "buy" if delta > 0 else "sell"
            slip = cfg.slippage_pct if delta > 0 else -cfg.slippage_pct
            exec_price = px * (1 + slip)
            cost = delta * exec_price                # negative for sells (cash inflow)
            slip_cost = abs(delta) * px * cfg.slippage_pct
            commission = self._compute_commission(abs(delta), exec_price)

            cash_delta -= cost + commission
            sig_match = next((s for s in signals if s.symbol == sym), None)
            confidence = sig_match.confidence if sig_match else 0.0

            trade_rows.append({
                "date": date,
                "symbol": sym,
                "side": side,
                "shares": abs(delta),
                "price": exec_price,
                "notional": abs(delta) * exec_price,
                "slippage_cost": slip_cost,
                "commission": commission,
                "regime": regime_label,
                "confidence": confidence,
            })

        return cash_delta, trade_rows

    # ─────────────────────────────────────────────────────────────────
    # Aggregation
    # ─────────────────────────────────────────────────────────────────

    def _aggregate_results(self, folds: list[FoldResult]) -> BacktestResult:
        """Concatenate fold equity curves; compute summary metrics."""
        if not folds:
            return BacktestResult(
                config=self.backtest_config, symbols=self.symbols,
                primary_symbol=self.primary,
            )

        combined_equity = pd.concat(
            [f.equity_curve for f in folds]
        ).sort_index()
        combined_regime = pd.concat(
            [f.regime_series for f in folds]
        ).sort_index()
        combined_conf = pd.concat(
            [f.confidence_series for f in folds]
        ).sort_index()

        trade_dfs = [f.trades for f in folds if len(f.trades) > 0]
        combined_trades = (
            pd.concat(trade_dfs, ignore_index=True)
            if trade_dfs
            else pd.DataFrame()
        )

        fold_metrics = {
            "n_folds": len(folds),
            "mean_fold_return": float(np.mean([f.metrics.get("total_return", 0) for f in folds])),
            "mean_fold_sharpe": float(np.mean([f.metrics.get("sharpe", 0) for f in folds])),
            "worst_fold_return": float(np.min([f.metrics.get("total_return", 0) for f in folds])),
            "best_fold_return": float(np.max([f.metrics.get("total_return", 0) for f in folds])),
        }

        # Primary symbol close prices for benchmark builder
        prim_closes = self._by_sym[self.primary]["close"]
        oos_start = folds[0].test_start
        oos_end = folds[-1].test_end
        primary_closes = prim_closes[
            (prim_closes.index >= pd.Timestamp(oos_start))
            & (prim_closes.index <= pd.Timestamp(oos_end))
        ]

        return BacktestResult(
            config=self.backtest_config,
            symbols=self.symbols,
            primary_symbol=self.primary,
            folds=folds,
            combined_equity=combined_equity,
            combined_regime=combined_regime,
            combined_confidence=combined_conf,
            combined_trades=combined_trades,
            summary_metrics=fold_metrics,
            primary_closes=primary_closes,
        )

    # ─────────────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────────────

    def _prices_on(self, date: pd.Timestamp, col: str) -> dict[str, float]:
        """Return {symbol: price} for all symbols on *date*."""
        prices = {}
        for sym in self.symbols:
            sym_data = self._by_sym.get(sym)
            if sym_data is None:
                continue
            if date in sym_data.index:
                prices[sym] = float(sym_data.loc[date, col])
            elif not sym_data.empty:
                # Forward-fill: use most recent available price
                prior = sym_data.index[sym_data.index <= date]
                if len(prior) > 0:
                    prices[sym] = float(sym_data.loc[prior[-1], col])
        return prices

    def _nav(
        self, cash: float, shares: dict[str, float], prices: dict[str, float]
    ) -> float:
        return cash + sum(shares.get(sym, 0.0) * prices.get(sym, 0.0) for sym in shares)

    def _bars_to_date(
        self, date: pd.Timestamp, prim_ohlcv: pd.DataFrame
    ) -> dict[str, pd.DataFrame]:
        """Return bars dict (symbol → OHLCV up to date) for strategy's EMA/ATR calcs."""
        cutoff = prim_ohlcv[prim_ohlcv.index <= date]
        result = {}
        for sym in self.symbols:
            sym_data = self._by_sym.get(sym)
            if sym_data is not None:
                result[sym] = sym_data[sym_data.index <= date]
            else:
                result[sym] = cutoff   # fall back to primary
        return result

    def _apply_slippage(self, price: float, side: str) -> float:
        slip = self.backtest_config.slippage_pct
        return price * (1 + slip if side == "buy" else 1 - slip)

    def _compute_commission(self, shares: float, price: float) -> float:
        return shares * price * self.backtest_config.commission_pct

    def _fold_metrics(self, equity: pd.Series) -> dict[str, float]:
        """Compute lightweight fold-level metrics (subset of full PerformanceReport)."""
        if len(equity) < 2:
            return {"total_return": 0.0, "sharpe": 0.0, "max_dd": 0.0}
        rets = equity.pct_change().dropna()
        total_ret = float(equity.iloc[-1] / equity.iloc[0] - 1)
        ann_ret = float((1 + total_ret) ** (252 / max(len(equity), 1)) - 1)
        ann_vol = float(rets.std() * np.sqrt(252)) if len(rets) > 1 else 0.0
        sharpe = (ann_ret - self.backtest_config.risk_free_rate) / max(ann_vol, 1e-10)
        rolling_max = equity.cummax()
        dd = (equity - rolling_max) / rolling_max.clip(lower=1e-10)
        max_dd = float(dd.min())
        return {"total_return": total_ret, "sharpe": sharpe, "max_dd": max_dd}
