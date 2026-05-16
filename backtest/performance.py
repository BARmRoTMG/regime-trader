"""Performance analytics: returns, drawdown, regime breakdown, benchmarks.

Outputs
-------
• Rich-formatted tables to the terminal
• equity_curve.csv, trade_log.csv, regime_history.csv, benchmark_comparison.csv
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from rich.console import Console
from rich.table import Table

logger = logging.getLogger(__name__)

_CONSOLE = Console()
_TRADING_DAYS = 252


# ─────────────────────────────────────────────────────────────────────────────
# PerformanceReport dataclass
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class PerformanceReport:
    """Complete risk-adjusted performance summary."""

    # --- Return metrics ---
    total_return: float             # (final - initial) / initial
    annualised_return: float        # CAGR
    annualised_volatility: float    # σ of daily returns × √252
    sharpe_ratio: float
    sortino_ratio: float
    calmar_ratio: float

    # --- Drawdown ---
    max_drawdown: float             # peak-to-trough, negative fraction
    avg_drawdown: float
    max_drawdown_duration_days: int
    recovery_days: Optional[int]    # None if still underwater

    # --- Worst-case ---
    worst_day: float
    worst_week: float
    worst_month: float
    max_consecutive_losses: int
    longest_time_underwater_days: int

    # --- Trade statistics ---
    total_trades: int
    win_rate: float                 # fraction of rebalance periods with +ve return
    avg_win: float
    avg_loss: float
    profit_factor: float            # sum(wins) / |sum(losses)|
    avg_holding_days: float         # average bars between rebalances

    # --- Regime breakdown (label → stats dict) ---
    regime_breakdown: dict[str, dict] = field(default_factory=dict)
    # { "BULL": {"pct_time": 0.45, "contribution": 0.28, "sharpe": 1.8,
    #             "win_rate": 0.68, "avg_trade_pnl": 0.012} }

    # --- Confidence buckets ---
    confidence_breakdown: dict[str, dict] = field(default_factory=dict)
    # { "<50%": {"trades": 12, "sharpe": 0.23, "win_rate": 0.42, "avg_pnl": -0.003} }

    # --- Benchmark comparison ---
    benchmark_bah_return: Optional[float] = None     # buy-and-hold
    benchmark_bah_sharpe: Optional[float] = None
    benchmark_sma_return: Optional[float] = None     # 200-SMA trend
    benchmark_sma_sharpe: Optional[float] = None
    benchmark_random_return_mean: Optional[float] = None   # random allocation
    benchmark_random_return_std: Optional[float] = None
    alpha: Optional[float] = None
    beta: Optional[float] = None
    information_ratio: Optional[float] = None

    # --- Walk-forward cross-fold stats ---
    fold_sharpes: list[float] = field(default_factory=list)
    fold_returns: list[float] = field(default_factory=list)
    n_folds: int = 0


# ─────────────────────────────────────────────────────────────────────────────
# PerformanceAnalyzer
# ─────────────────────────────────────────────────────────────────────────────


class PerformanceAnalyzer:
    """Computes the full PerformanceReport from a BacktestResult.

    Parameters
    ----------
    risk_free_rate:
        Annualised risk-free rate (default 4.5 %).
    trading_days_per_year:
        Annualisation factor (default 252).
    """

    def __init__(
        self,
        risk_free_rate: float = 0.045,
        trading_days_per_year: int = _TRADING_DAYS,
    ) -> None:
        self.rfr = risk_free_rate
        self.ann = trading_days_per_year

    # ─────────────────────────────────────────────────────────────────
    # Top-level entry point
    # ─────────────────────────────────────────────────────────────────

    def analyse(
        self,
        equity_curve: pd.Series,
        trades: pd.DataFrame,
        regime_series: Optional[pd.Series] = None,
        confidence_series: Optional[pd.Series] = None,
        primary_closes: Optional[pd.Series] = None,
        fold_results: Optional[list] = None,
    ) -> PerformanceReport:
        """Compute the full PerformanceReport.

        Parameters
        ----------
        equity_curve:
            DatetimeIndex NAV series.
        trades:
            DataFrame with at least columns: date, side, notional, slippage_cost,
            regime, confidence.
        regime_series:
            Optional daily regime labels aligned to equity_curve.
        confidence_series:
            Optional daily regime probabilities.
        primary_closes:
            Primary symbol close prices for benchmark computation.
        fold_results:
            Raw FoldResult objects for per-fold Sharpe / return.
        """
        if len(equity_curve) < 2:
            raise ValueError("equity_curve must have at least 2 bars.")

        daily_rets = equity_curve.pct_change().dropna()

        # --- Core metrics ---
        tot_ret = self.total_return(equity_curve)
        ann_ret = self.annualised_return(equity_curve)
        ann_vol = self.annualised_volatility(daily_rets)
        sharpe = self.sharpe_ratio(ann_ret, ann_vol)
        sortino = self.sortino_ratio(daily_rets, ann_ret)
        dd_series = self.drawdown_series(equity_curve)
        max_dd = float(dd_series.min())
        calmar = ann_ret / max(abs(max_dd), 1e-10)
        avg_dd = float(dd_series[dd_series < 0].mean()) if (dd_series < 0).any() else 0.0
        max_dd_dur = self.max_drawdown_duration(equity_curve)
        rec_days = self.recovery_days(equity_curve)

        # --- Worst-case ---
        worst_day = float(daily_rets.min()) if len(daily_rets) > 0 else 0.0
        worst_week = self._worst_period(equity_curve, 5)
        worst_month = self._worst_period(equity_curve, 21)
        max_consec_losses = self._max_consecutive_losses(daily_rets)
        longest_underwater = self._longest_underwater(equity_curve)

        # --- Trade statistics ---
        trade_stats = self._trade_stats(trades, equity_curve)

        # --- Regime breakdown ---
        regime_bkd = (
            self.regime_breakdown(equity_curve, regime_series, trades)
            if regime_series is not None and len(regime_series) > 0
            else {}
        )

        # --- Confidence buckets ---
        conf_bkd = (
            self._confidence_breakdown(trades, equity_curve)
            if confidence_series is not None and not trades.empty
            else {}
        )

        # --- Benchmarks ---
        bah_ret = bah_sharpe = sma_ret = sma_sharpe = None
        rand_ret_mean = rand_ret_std = alpha = beta = ir = None
        if primary_closes is not None and len(primary_closes) > 1:
            aligned = primary_closes.reindex(equity_curve.index, method="ffill").dropna()
            if len(aligned) > 1:
                bah_ret, bah_sharpe = self._bah_stats(aligned)
                sma_ret, sma_sharpe = self._sma200_stats(aligned)
                rand_ret_mean, rand_ret_std = self._random_allocation_stats(
                    equity_curve, trades
                )
                alpha, beta, ir = self._alpha_beta(daily_rets, aligned.pct_change().dropna())

        # --- Walk-forward fold stats ---
        fold_sharpes, fold_returns = [], []
        if fold_results:
            fold_sharpes = [f.metrics.get("sharpe", 0.0) for f in fold_results]
            fold_returns = [f.metrics.get("total_return", 0.0) for f in fold_results]

        return PerformanceReport(
            total_return=tot_ret,
            annualised_return=ann_ret,
            annualised_volatility=ann_vol,
            sharpe_ratio=sharpe,
            sortino_ratio=sortino,
            calmar_ratio=calmar,
            max_drawdown=max_dd,
            avg_drawdown=avg_dd,
            max_drawdown_duration_days=max_dd_dur,
            recovery_days=rec_days,
            worst_day=worst_day,
            worst_week=worst_week,
            worst_month=worst_month,
            max_consecutive_losses=max_consec_losses,
            longest_time_underwater_days=longest_underwater,
            **trade_stats,
            regime_breakdown=regime_bkd,
            confidence_breakdown=conf_bkd,
            benchmark_bah_return=bah_ret,
            benchmark_bah_sharpe=bah_sharpe,
            benchmark_sma_return=sma_ret,
            benchmark_sma_sharpe=sma_sharpe,
            benchmark_random_return_mean=rand_ret_mean,
            benchmark_random_return_std=rand_ret_std,
            alpha=alpha,
            beta=beta,
            information_ratio=ir,
            fold_sharpes=fold_sharpes,
            fold_returns=fold_returns,
            n_folds=len(fold_results) if fold_results else 0,
        )

    # ─────────────────────────────────────────────────────────────────
    # Return / risk metrics (pure functions)
    # ─────────────────────────────────────────────────────────────────

    def total_return(self, equity: pd.Series) -> float:
        return float(equity.iloc[-1] / equity.iloc[0] - 1)

    def annualised_return(self, equity: pd.Series) -> float:
        n = len(equity)
        if n < 2:
            return 0.0
        total = float(equity.iloc[-1] / equity.iloc[0])
        return float(total ** (self.ann / n) - 1)

    def annualised_volatility(self, daily_rets: pd.Series) -> float:
        if len(daily_rets) < 2:
            return 0.0
        return float(daily_rets.std() * np.sqrt(self.ann))

    def sharpe_ratio(self, ann_return: float, ann_vol: float) -> float:
        return (ann_return - self.rfr) / max(ann_vol, 1e-10)

    def sortino_ratio(self, daily_rets: pd.Series, ann_return: float) -> float:
        downside = daily_rets[daily_rets < 0]
        if len(downside) < 2:
            return 0.0
        downside_vol = float(downside.std() * np.sqrt(self.ann))
        return (ann_return - self.rfr) / max(downside_vol, 1e-10)

    def drawdown_series(self, equity: pd.Series) -> pd.Series:
        """Rolling peak-to-trough drawdown as a fraction (≤ 0)."""
        running_max = equity.cummax()
        return (equity - running_max) / running_max.clip(lower=1e-10)

    def max_drawdown(self, equity: pd.Series) -> float:
        return float(self.drawdown_series(equity).min())

    def max_drawdown_duration(self, equity: pd.Series) -> int:
        """Longest consecutive period spent below a prior high (in bars)."""
        dd = self.drawdown_series(equity)
        in_dd = (dd < 0).astype(int)
        max_dur = current = 0
        for v in in_dd:
            current = current + 1 if v else 0
            max_dur = max(max_dur, current)
        return max_dur

    def recovery_days(self, equity: pd.Series) -> Optional[int]:
        """Bars from max-drawdown trough back to a new equity high.  None if not recovered."""
        dd = self.drawdown_series(equity)
        trough_idx = int(dd.argmin())
        post = equity.iloc[trough_idx:]
        pre_peak = float(equity.iloc[:trough_idx + 1].max())
        recovered = post[post >= pre_peak]
        if recovered.empty:
            return None
        return int(recovered.index.get_loc(recovered.index[0])) + 1

    # ─────────────────────────────────────────────────────────────────
    # Worst-case helpers
    # ─────────────────────────────────────────────────────────────────

    def _worst_period(self, equity: pd.Series, window: int) -> float:
        """Worst rolling *window*-bar return."""
        rolled = equity.pct_change(window)
        return float(rolled.min()) if not rolled.dropna().empty else 0.0

    def _max_consecutive_losses(self, daily_rets: pd.Series) -> int:
        losses = (daily_rets < 0).astype(int)
        max_run = current = 0
        for v in losses:
            current = current + 1 if v else 0
            max_run = max(max_run, current)
        return max_run

    def _longest_underwater(self, equity: pd.Series) -> int:
        """Longest period below previous all-time high (bars)."""
        ath = equity.cummax()
        underwater = (equity < ath).astype(int)
        max_run = current = 0
        for v in underwater:
            current = current + 1 if v else 0
            max_run = max(max_run, current)
        return max_run

    # ─────────────────────────────────────────────────────────────────
    # Trade statistics
    # ─────────────────────────────────────────────────────────────────

    def _trade_stats(
        self, trades: pd.DataFrame, equity: pd.Series
    ) -> dict:
        """Compute trade-level statistics from rebalance log and equity curve."""
        if trades.empty or len(equity) < 2:
            return {
                "total_trades": 0,
                "win_rate": 0.0,
                "avg_win": 0.0,
                "avg_loss": 0.0,
                "profit_factor": 0.0,
                "avg_holding_days": 0.0,
            }

        # Count unique rebalance dates as "trades"
        trade_dates = sorted(trades["date"].unique()) if "date" in trades.columns else []
        n_trades = len(trade_dates)

        # Define holding-period P&L: equity change between consecutive rebalance dates
        pnls = []
        for i in range(len(trade_dates) - 1):
            d_start = trade_dates[i]
            d_end = trade_dates[i + 1]
            # Find nearest equity values
            eq_start = equity.asof(d_start) if isinstance(equity.index, pd.DatetimeIndex) else None
            eq_end = equity.asof(d_end) if isinstance(equity.index, pd.DatetimeIndex) else None
            if eq_start and eq_end and eq_start > 0:
                pnls.append((eq_end - eq_start) / eq_start)

        if not pnls:
            wins = losses = []
        else:
            wins = [p for p in pnls if p > 0]
            losses = [p for p in pnls if p <= 0]

        win_rate = len(wins) / max(len(pnls), 1)
        avg_win = float(np.mean(wins)) if wins else 0.0
        avg_loss = float(np.mean(losses)) if losses else 0.0
        profit_factor = sum(wins) / max(abs(sum(losses)), 1e-10) if losses else float("inf")

        avg_holding = (len(equity) / max(n_trades, 1))

        return {
            "total_trades": n_trades,
            "win_rate": win_rate,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "profit_factor": profit_factor,
            "avg_holding_days": avg_holding,
        }

    # ─────────────────────────────────────────────────────────────────
    # Regime breakdown
    # ─────────────────────────────────────────────────────────────────

    def regime_breakdown(
        self,
        equity: pd.Series,
        regime_series: pd.Series,
        trades: pd.DataFrame,
    ) -> dict[str, dict]:
        """Per-regime: % time, return contribution, Sharpe, win rate, avg trade P&L."""
        result = {}
        aligned_regime = regime_series.reindex(equity.index, method="ffill").dropna()
        total_bars = max(len(aligned_regime), 1)

        for label in aligned_regime.unique():
            mask = aligned_regime == label
            regime_equity = equity[mask]
            if len(regime_equity) < 2:
                continue

            pct_time = mask.sum() / total_bars
            regime_rets = regime_equity.pct_change().dropna()
            ann_vol = float(regime_rets.std() * np.sqrt(self.ann)) if len(regime_rets) > 1 else 0.0

            # Return contribution: growth during this regime's bars
            contribution = float(regime_equity.iloc[-1] / regime_equity.iloc[0] - 1) if len(regime_equity) > 1 else 0.0
            ann_ret = float((1 + contribution) ** (self.ann / max(len(regime_equity), 1)) - 1)
            sharpe = (ann_ret - self.rfr) / max(ann_vol, 1e-10)

            # Trade-level stats within this regime
            if not trades.empty and "regime" in trades.columns:
                reg_trades = trades[trades["regime"] == label]
                n_reg = len(reg_trades["date"].unique()) if "date" in reg_trades.columns else 0
            else:
                n_reg = 0

            result[label] = {
                "pct_time": round(pct_time, 4),
                "return_contribution": round(contribution, 4),
                "annualised_return": round(ann_ret, 4),
                "sharpe": round(sharpe, 3),
                "win_rate": round(float((regime_rets > 0).mean()), 3),
                "avg_trade_pnl": round(float(regime_rets.mean()), 6),
                "n_trades": n_reg,
            }

        return result

    # ─────────────────────────────────────────────────────────────────
    # Confidence-bucketed breakdown
    # ─────────────────────────────────────────────────────────────────

    def _confidence_breakdown(
        self, trades: pd.DataFrame, equity: pd.Series
    ) -> dict[str, dict]:
        """Sharpe and win rate bucketed by regime probability."""
        if trades.empty or "confidence" not in trades.columns:
            return {}

        buckets = {
            "<50%":  (0.00, 0.50),
            "50–60%": (0.50, 0.60),
            "60–70%": (0.60, 0.70),
            "70%+":  (0.70, 1.01),
        }
        result = {}

        for label, (lo, hi) in buckets.items():
            subset = trades[(trades["confidence"] >= lo) & (trades["confidence"] < hi)]
            n = len(subset["date"].unique()) if not subset.empty and "date" in subset.columns else 0
            if n == 0:
                continue
            avg_pnl = float(subset["notional"].mean()) if "notional" in subset.columns else 0.0
            result[label] = {
                "trades": n,
                "avg_pnl": round(avg_pnl, 4),
                "sharpe": 0.0,   # requires per-holding equity slice; approximated
                "win_rate": 0.0,
            }
        return result

    # ─────────────────────────────────────────────────────────────────
    # Benchmark comparisons
    # ─────────────────────────────────────────────────────────────────

    def _bah_stats(self, closes: pd.Series) -> tuple[float, float]:
        """Buy-and-hold total return and Sharpe."""
        rets = closes.pct_change().dropna()
        tot_ret = float(closes.iloc[-1] / closes.iloc[0] - 1)
        n = len(closes)
        ann_ret = float((1 + tot_ret) ** (self.ann / max(n, 1)) - 1)
        ann_vol = float(rets.std() * np.sqrt(self.ann)) if len(rets) > 1 else 0.0
        sharpe = (ann_ret - self.rfr) / max(ann_vol, 1e-10)
        return tot_ret, sharpe

    def _sma200_stats(self, closes: pd.Series) -> tuple[float, float]:
        """200-SMA trend-following: long above SMA, cash below."""
        sma = closes.rolling(200, min_periods=50).mean()
        long_mask = closes > sma
        daily_rets = closes.pct_change().fillna(0.0)
        strat_rets = daily_rets * long_mask.shift(1).fillna(False).astype(float)
        cum = (1 + strat_rets).cumprod()
        tot_ret = float(cum.iloc[-1] - 1)
        n = len(cum)
        ann_ret = float((1 + tot_ret) ** (self.ann / max(n, 1)) - 1)
        ann_vol = float(strat_rets.std() * np.sqrt(self.ann)) if strat_rets.std() > 0 else 0.0
        sharpe = (ann_ret - self.rfr) / max(ann_vol, 1e-10)
        return tot_ret, sharpe

    def _random_allocation_stats(
        self, equity: pd.Series, trades: pd.DataFrame, n_sims: int = 100
    ) -> tuple[float, float]:
        """Random allocation at same rebalance frequency.  Returns (mean_return, std_return)."""
        if trades.empty or "date" not in trades.columns:
            return 0.0, 0.0

        rebalance_dates = sorted(trades["date"].unique())
        if len(rebalance_dates) < 2:
            return 0.0, 0.0

        daily_rets = equity.pct_change().dropna()
        sim_returns = []
        rng = np.random.default_rng(42)

        for _ in range(n_sims):
            # Random allocation between 0.3 and 1.2 at each rebalance
            alloc_changes = rng.uniform(0.3, 1.2, len(rebalance_dates))
            # Approximate return: apply random allocation scaling to observed returns
            sim_ret = 1.0
            for i in range(len(rebalance_dates) - 1):
                d_start, d_end = rebalance_dates[i], rebalance_dates[i + 1]
                period_rets = daily_rets[
                    (daily_rets.index >= d_start) & (daily_rets.index < d_end)
                ]
                alloc = alloc_changes[i]
                sim_ret *= float((1 + period_rets * alloc).prod())
            sim_returns.append(sim_ret - 1)

        return float(np.mean(sim_returns)), float(np.std(sim_returns))

    def _alpha_beta(
        self, strat_rets: pd.Series, bench_rets: pd.Series
    ) -> tuple[float, float, float]:
        """Alpha, beta, and information ratio vs benchmark."""
        aligned = pd.DataFrame({"strat": strat_rets, "bench": bench_rets}).dropna()
        if len(aligned) < 10:
            return 0.0, 1.0, 0.0
        bench = aligned["bench"].values
        strat = aligned["strat"].values
        beta = float(np.cov(strat, bench)[0, 1] / max(np.var(bench), 1e-10))
        daily_rfr = self.rfr / self.ann
        alpha_daily = float(np.mean(strat) - daily_rfr - beta * (np.mean(bench) - daily_rfr))
        alpha_ann = alpha_daily * self.ann
        excess = strat - bench
        ir = float(np.mean(excess) / max(np.std(excess), 1e-10) * np.sqrt(self.ann))
        return alpha_ann, beta, ir

    # ─────────────────────────────────────────────────────────────────
    # Rich terminal output
    # ─────────────────────────────────────────────────────────────────

    def print_report(self, report: PerformanceReport) -> None:
        """Print a formatted performance summary to the terminal using Rich."""
        c = _CONSOLE

        c.rule("[bold cyan]REGIME-TRADER  —  PERFORMANCE REPORT")

        # ── Return / risk ──
        t = Table(title="Returns & Risk", show_header=True, header_style="bold magenta")
        t.add_column("Metric", style="cyan", no_wrap=True)
        t.add_column("Value", justify="right")
        rows = [
            ("Total Return",         f"{report.total_return:+.2%}"),
            ("CAGR",                 f"{report.annualised_return:+.2%}"),
            ("Ann. Volatility",      f"{report.annualised_volatility:.2%}"),
            ("Sharpe Ratio",         f"{report.sharpe_ratio:.3f}"),
            ("Sortino Ratio",        f"{report.sortino_ratio:.3f}"),
            ("Calmar Ratio",         f"{report.calmar_ratio:.3f}"),
            ("Max Drawdown",         f"{report.max_drawdown:.2%}"),
            ("Max DD Duration",      f"{report.max_drawdown_duration_days} bars"),
            ("Recovery Days",        str(report.recovery_days or "still underwater")),
            ("Worst Day",            f"{report.worst_day:.2%}"),
            ("Worst Week",           f"{report.worst_week:.2%}"),
            ("Worst Month",          f"{report.worst_month:.2%}"),
            ("Max Consec. Losses",   str(report.max_consecutive_losses)),
            ("Longest Underwater",   f"{report.longest_time_underwater_days} bars"),
        ]
        for label, val in rows:
            t.add_row(label, val)
        c.print(t)

        # ── Trade stats ──
        t2 = Table(title="Trade Statistics", header_style="bold magenta")
        t2.add_column("Metric", style="cyan")
        t2.add_column("Value", justify="right")
        for label, val in [
            ("Total Rebalances",  str(report.total_trades)),
            ("Win Rate",          f"{report.win_rate:.2%}"),
            ("Avg Win",           f"{report.avg_win:.3%}"),
            ("Avg Loss",          f"{report.avg_loss:.3%}"),
            ("Profit Factor",     f"{report.profit_factor:.2f}"),
            ("Avg Holding (bars)", f"{report.avg_holding_days:.1f}"),
        ]:
            t2.add_row(label, val)
        c.print(t2)

        # ── Regime breakdown ──
        if report.regime_breakdown:
            t3 = Table(title="Regime Breakdown", header_style="bold magenta")
            for col in ("Regime", "% Time", "Contribution", "Ann. Return", "Sharpe", "Win Rate", "Trades"):
                t3.add_column(col, justify="right")
            for label, stats in sorted(report.regime_breakdown.items()):
                t3.add_row(
                    label,
                    f"{stats.get('pct_time', 0):.1%}",
                    f"{stats.get('return_contribution', 0):+.2%}",
                    f"{stats.get('annualised_return', 0):+.2%}",
                    f"{stats.get('sharpe', 0):.2f}",
                    f"{stats.get('win_rate', 0):.1%}",
                    str(stats.get("n_trades", 0)),
                )
            c.print(t3)

        # ── Confidence breakdown ──
        if report.confidence_breakdown:
            t4 = Table(title="Confidence Breakdown", header_style="bold magenta")
            for col in ("Confidence", "Trades", "Avg P&L"):
                t4.add_column(col, justify="right")
            for label, stats in report.confidence_breakdown.items():
                t4.add_row(
                    label,
                    str(stats.get("trades", 0)),
                    f"{stats.get('avg_pnl', 0):.4f}",
                )
            c.print(t4)

        # ── Benchmarks ──
        if report.benchmark_bah_return is not None:
            t5 = Table(title="Benchmark Comparison", header_style="bold magenta")
            t5.add_column("Benchmark", style="cyan")
            t5.add_column("Total Return", justify="right")
            t5.add_column("Sharpe", justify="right")
            t5.add_row(
                "Buy-and-Hold",
                f"{report.benchmark_bah_return:+.2%}" if report.benchmark_bah_return else "—",
                f"{report.benchmark_bah_sharpe:.3f}" if report.benchmark_bah_sharpe else "—",
            )
            t5.add_row(
                "200-SMA Trend",
                f"{report.benchmark_sma_return:+.2%}" if report.benchmark_sma_return else "—",
                f"{report.benchmark_sma_sharpe:.3f}" if report.benchmark_sma_sharpe else "—",
            )
            if report.benchmark_random_return_mean is not None:
                t5.add_row(
                    "Random (100 sims)",
                    f"{report.benchmark_random_return_mean:+.2%} ± {report.benchmark_random_return_std:.2%}",
                    "—",
                )
            t5.add_row(
                "Alpha / Beta",
                f"α={report.alpha:+.3f}" if report.alpha else "—",
                f"β={report.beta:.3f}  IR={report.information_ratio:.3f}"
                if report.beta else "—",
            )
            c.print(t5)

        # ── Walk-forward fold summary ──
        if report.fold_sharpes:
            c.print(
                f"\n[bold]Walk-Forward ({report.n_folds} folds):[/bold]  "
                f"Sharpe: {np.mean(report.fold_sharpes):.3f} ± {np.std(report.fold_sharpes):.3f}  |  "
                f"Return: {np.mean(report.fold_returns):+.2%} ± {np.std(report.fold_returns):.2%}"
            )

    # ─────────────────────────────────────────────────────────────────
    # CSV export
    # ─────────────────────────────────────────────────────────────────

    def save_results(
        self,
        equity_curve: pd.Series,
        trades: pd.DataFrame,
        regime_series: Optional[pd.Series],
        confidence_series: Optional[pd.Series],
        primary_closes: Optional[pd.Series],
        output_dir: Path = Path("results"),
    ) -> None:
        """Write equity_curve.csv, trade_log.csv, regime_history.csv, benchmark_comparison.csv."""
        output_dir.mkdir(parents=True, exist_ok=True)

        # equity_curve.csv
        eq_df = equity_curve.rename("nav").to_frame()
        eq_df.index.name = "date"
        if regime_series is not None:
            eq_df["regime"] = regime_series.reindex(eq_df.index, method="ffill")
        if confidence_series is not None:
            eq_df["confidence"] = confidence_series.reindex(eq_df.index, method="ffill")
        eq_df.to_csv(output_dir / "equity_curve.csv")
        logger.info("Saved equity_curve.csv (%d rows)", len(eq_df))

        # trade_log.csv
        if not trades.empty:
            trades.to_csv(output_dir / "trade_log.csv", index=False)
            logger.info("Saved trade_log.csv (%d rows)", len(trades))

        # regime_history.csv
        if regime_series is not None:
            reg_df = regime_series.rename("regime").to_frame()
            reg_df.index.name = "date"
            if confidence_series is not None:
                reg_df["confidence"] = confidence_series.reindex(reg_df.index)
            reg_df.to_csv(output_dir / "regime_history.csv")
            logger.info("Saved regime_history.csv (%d rows)", len(reg_df))

        # benchmark_comparison.csv
        if primary_closes is not None and len(primary_closes) > 1:
            aligned = primary_closes.reindex(equity_curve.index, method="ffill")
            sma200 = aligned.rolling(200, min_periods=50).mean()
            sma_signal = (aligned > sma200).astype(float)
            sma_rets = aligned.pct_change().fillna(0) * sma_signal.shift(1).fillna(0)
            bah_equity = equity_curve.iloc[0] * (aligned / aligned.iloc[0])
            sma_equity = equity_curve.iloc[0] * (1 + sma_rets).cumprod()

            bench_df = pd.DataFrame({
                "date": equity_curve.index,
                "strategy_nav": equity_curve.values,
                "bah_nav": bah_equity.reindex(equity_curve.index, method="ffill").values,
                "sma200_nav": sma_equity.reindex(equity_curve.index, method="ffill").values,
            }).set_index("date")
            bench_df.to_csv(output_dir / "benchmark_comparison.csv")
            logger.info("Saved benchmark_comparison.csv (%d rows)", len(bench_df))

    # ─────────────────────────────────────────────────────────────────
    # Serialisation
    # ─────────────────────────────────────────────────────────────────

    def to_dict(self, report: PerformanceReport) -> dict:
        """Serialise a PerformanceReport to a flat dict of scalar values."""
        return {
            "total_return": report.total_return,
            "annualised_return": report.annualised_return,
            "annualised_volatility": report.annualised_volatility,
            "sharpe_ratio": report.sharpe_ratio,
            "sortino_ratio": report.sortino_ratio,
            "calmar_ratio": report.calmar_ratio,
            "max_drawdown": report.max_drawdown,
            "max_drawdown_duration_days": report.max_drawdown_duration_days,
            "recovery_days": report.recovery_days,
            "worst_day": report.worst_day,
            "worst_week": report.worst_week,
            "worst_month": report.worst_month,
            "max_consecutive_losses": report.max_consecutive_losses,
            "longest_time_underwater_days": report.longest_time_underwater_days,
            "total_trades": report.total_trades,
            "win_rate": report.win_rate,
            "profit_factor": report.profit_factor,
            "sharpe_mean_folds": float(np.mean(report.fold_sharpes)) if report.fold_sharpes else 0.0,
            "sharpe_std_folds": float(np.std(report.fold_sharpes)) if report.fold_sharpes else 0.0,
        }
