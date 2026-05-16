"""Technical indicators and feature computation for the HMM regime detector.

All features are computed from historical OHLCV data with NO look-ahead bias.
Every rolling operation uses only data up to and including bar T when computing
the value at T.  The same pipeline is used identically in live trading and
backtesting.
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd
import ta
from ta.momentum import RSIIndicator, ROCIndicator
from ta.trend import ADXIndicator, SMAIndicator
from ta.volatility import AverageTrueRange

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Feature column name constants (ordered: index 0 = log_return_1 used for labeling)
# ---------------------------------------------------------------------------

F_LOG_RETURN_1 = "log_return_1"
F_LOG_RETURN_5 = "log_return_5"
F_LOG_RETURN_20 = "log_return_20"
F_REALIZED_VOL_20 = "realized_vol_20"
F_VOL_RATIO = "vol_ratio"
F_VOLUME_ZSCORE = "volume_zscore"
F_VOLUME_TREND = "volume_trend"
F_ADX_14 = "adx_14"
F_SMA50_SLOPE = "sma50_slope"
F_RSI14_ZSCORE = "rsi14_zscore"
F_DIST_SMA200 = "dist_sma200"
F_ROC_10 = "roc_10"
F_ROC_20 = "roc_20"
F_ATR_NORM = "atr_norm"

# Canonical column order used everywhere (HMM, tests, serialisation)
FEATURE_COLUMNS: list[str] = [
    F_LOG_RETURN_1,
    F_LOG_RETURN_5,
    F_LOG_RETURN_20,
    F_REALIZED_VOL_20,
    F_VOL_RATIO,
    F_VOLUME_ZSCORE,
    F_VOLUME_TREND,
    F_ADX_14,
    F_SMA50_SLOPE,
    F_RSI14_ZSCORE,
    F_DIST_SMA200,
    F_ROC_10,
    F_ROC_20,
    F_ATR_NORM,
]

_ZSCORE_WINDOW = 252   # rolling z-score look-back (1 trading year)

# ---------------------------------------------------------------------------
# Low-level pure helpers
# ---------------------------------------------------------------------------


def rolling_zscore(series: pd.Series, window: int = _ZSCORE_WINDOW) -> pd.Series:
    """Causal rolling z-score: (x_t - mean(x_{t-window+1:t})) / std(...)."""
    min_periods = max(window // 4, 20)   # allow partial warm-up
    mu = series.rolling(window, min_periods=min_periods).mean()
    sigma = series.rolling(window, min_periods=min_periods).std()
    return (series - mu) / sigma.clip(lower=1e-10)


def _rolling_linear_slope(series: pd.Series, window: int) -> pd.Series:
    """Rolling OLS slope over *window* bars, normalised by |mean| of the window.

    Normalisation makes the output scale-free so volume and price slopes are
    comparable after z-scoring.
    """
    x = np.arange(window, dtype=float)
    x_c = x - x.mean()
    x_ss = float(np.dot(x_c, x_c))   # sum-of-squares

    def _slope(y: np.ndarray) -> float:
        if np.isnan(y).any():
            return np.nan
        y_mean = float(y.mean())
        if abs(y_mean) < 1e-10:
            return 0.0
        return float(np.dot(x_c, y - y_mean)) / (x_ss * abs(y_mean))

    return series.rolling(window).apply(_slope, raw=True)


def _safe_divide(num: pd.Series, denom: pd.Series, floor: float = 1e-10) -> pd.Series:
    return num / denom.clip(lower=floor)


# ---------------------------------------------------------------------------
# Individual feature families (pure functions, no side-effects)
# ---------------------------------------------------------------------------


def compute_log_returns(close: pd.Series) -> pd.DataFrame:
    """Log returns over 1, 5, and 20 periods."""
    lr1 = np.log(close / close.shift(1))
    return pd.DataFrame(
        {
            F_LOG_RETURN_1: lr1,
            F_LOG_RETURN_5: np.log(close / close.shift(5)),
            F_LOG_RETURN_20: np.log(close / close.shift(20)),
        },
        index=close.index,
    )


def compute_volatility_features(close: pd.Series) -> pd.DataFrame:
    """Realised volatility (20-bar annualised) and 5/20 vol ratio."""
    lr1 = np.log(close / close.shift(1))
    vol20 = lr1.rolling(20, min_periods=10).std() * np.sqrt(252)
    vol5 = lr1.rolling(5, min_periods=3).std() * np.sqrt(252)
    vol_ratio = _safe_divide(vol5, vol20).clip(upper=5.0)   # cap outliers
    return pd.DataFrame(
        {F_REALIZED_VOL_20: vol20, F_VOL_RATIO: vol_ratio},
        index=close.index,
    )


def compute_volume_features(volume: pd.Series) -> pd.DataFrame:
    """Volume z-score (vs 50-bar mean) and normalised trend (slope of 10-bar SMA)."""
    sma10 = volume.rolling(10, min_periods=5).mean()
    sma50 = volume.rolling(50, min_periods=25).mean()
    std50 = volume.rolling(50, min_periods=25).std()

    vol_zscore = _safe_divide(volume - sma50, std50.clip(lower=1.0))
    vol_trend = _rolling_linear_slope(sma10, window=10)

    return pd.DataFrame(
        {F_VOLUME_ZSCORE: vol_zscore, F_VOLUME_TREND: vol_trend},
        index=volume.index,
    )


def compute_trend_features(
    high: pd.Series, low: pd.Series, close: pd.Series
) -> pd.DataFrame:
    """ADX(14) and percentage slope of the 50-bar SMA over a 5-bar horizon."""
    adx = ADXIndicator(high=high, low=low, close=close, window=14).adx()

    sma50 = close.rolling(50, min_periods=25).mean()
    # Percentage change of SMA over 5 bars: forward-safe (uses only past values)
    sma50_slope = _safe_divide(sma50 - sma50.shift(5), sma50.shift(5)) * 100.0

    return pd.DataFrame(
        {F_ADX_14: adx, F_SMA50_SLOPE: sma50_slope},
        index=close.index,
    )


def compute_mean_reversion_features(close: pd.Series) -> pd.DataFrame:
    """RSI(14) rolling z-score and % distance from the 200-bar SMA."""
    rsi = RSIIndicator(close=close, window=14).rsi()
    rsi_zscore = rolling_zscore(rsi, _ZSCORE_WINDOW)

    sma200 = close.rolling(200, min_periods=100).mean()
    dist_sma200 = _safe_divide(close - sma200, sma200) * 100.0

    return pd.DataFrame(
        {F_RSI14_ZSCORE: rsi_zscore, F_DIST_SMA200: dist_sma200},
        index=close.index,
    )


def compute_momentum_features(close: pd.Series) -> pd.DataFrame:
    """Rate-of-change over 10 and 20 periods (%)."""
    roc10 = (close / close.shift(10) - 1.0) * 100.0
    roc20 = (close / close.shift(20) - 1.0) * 100.0
    return pd.DataFrame(
        {F_ROC_10: roc10, F_ROC_20: roc20},
        index=close.index,
    )


def compute_range_features(
    high: pd.Series, low: pd.Series, close: pd.Series
) -> pd.DataFrame:
    """Normalised ATR(14): ATR divided by close price."""
    atr = AverageTrueRange(high=high, low=low, close=close, window=14).average_true_range()
    atr_norm = _safe_divide(atr, close)
    return pd.DataFrame({F_ATR_NORM: atr_norm}, index=close.index)


# ---------------------------------------------------------------------------
# Top-level pipeline
# ---------------------------------------------------------------------------


def build_features(
    ohlcv: pd.DataFrame,
    normalise: bool = True,
    dropna: bool = True,
) -> pd.DataFrame:
    """Build the full feature matrix from an OHLCV DataFrame.

    Parameters
    ----------
    ohlcv:
        DatetimeIndex DataFrame with columns: open, high, low, close, volume.
        Column names are lower-cased internally.
    normalise:
        If True, apply a 252-bar rolling z-score to every feature column so
        the HMM sees stationary, zero-mean, unit-variance inputs.
    dropna:
        If True, drop leading rows where any feature is NaN (warm-up period).

    Returns
    -------
    pd.DataFrame
        Same DatetimeIndex as *ohlcv* (after potential dropna), columns = FEATURE_COLUMNS.

    Notes
    -----
    No future data is used.  Every rolling operation looks only backward.
    The returned matrix is safe to use as-is for HMM training or inference.
    """
    cols = {c.lower() for c in ohlcv.columns}
    required = {"open", "high", "low", "close", "volume"}
    missing = required - cols
    if missing:
        raise ValueError(f"OHLCV DataFrame missing columns: {missing}")

    ohlcv = ohlcv.copy()
    ohlcv.columns = [c.lower() for c in ohlcv.columns]

    close = ohlcv["close"]
    high = ohlcv["high"]
    low = ohlcv["low"]
    volume = ohlcv["volume"]

    parts = [
        compute_log_returns(close),
        compute_volatility_features(close),
        compute_volume_features(volume),
        compute_trend_features(high, low, close),
        compute_mean_reversion_features(close),
        compute_momentum_features(close),
        compute_range_features(high, low, close),
    ]

    features = pd.concat(parts, axis=1)[FEATURE_COLUMNS]

    if normalise:
        features = features.apply(lambda col: rolling_zscore(col, _ZSCORE_WINDOW))

    if dropna:
        n_before = len(features)
        features = features.dropna()
        n_dropped = n_before - len(features)
        if n_dropped > 0:
            logger.debug("Dropped %d warm-up rows (NaN) from feature matrix.", n_dropped)

    return features


# ---------------------------------------------------------------------------
# FeatureEngineer class (stateless wrapper for ergonomic use)
# ---------------------------------------------------------------------------


class FeatureEngineer:
    """Stateless wrapper around the pure feature functions.

    Parameters
    ----------
    vol_window:
        Rolling window for realised volatility.  Defaults to 21 but overridden
        to 20 inside build_features() to match the spec exactly.
    atr_window:
        ATR look-back period.  Defaults to 14.

    All computation is delegated to the module-level pure functions, so this
    class carries no mutable state and is safe to use from multiple threads.
    """

    def __init__(
        self,
        vol_window: int = 20,
        atr_window: int = 14,
    ) -> None:
        self.vol_window = vol_window
        self.atr_window = atr_window

    # ------------------------------------------------------------------
    # Top-level pipeline
    # ------------------------------------------------------------------

    def build_features(
        self,
        ohlcv: pd.DataFrame,
        normalise: bool = True,
        dropna: bool = True,
    ) -> pd.DataFrame:
        """Run the complete feature pipeline on an OHLCV DataFrame."""
        return build_features(ohlcv, normalise=normalise, dropna=dropna)

    # ------------------------------------------------------------------
    # Individual feature builders (delegate to pure functions)
    # ------------------------------------------------------------------

    def log_returns(self, close: pd.Series) -> pd.DataFrame:
        return compute_log_returns(close)

    def realised_volatility(self, close: pd.Series) -> pd.DataFrame:
        return compute_volatility_features(close)

    def volume_features(self, volume: pd.Series) -> pd.DataFrame:
        return compute_volume_features(volume)

    def trend_features(
        self, high: pd.Series, low: pd.Series, close: pd.Series
    ) -> pd.DataFrame:
        return compute_trend_features(high, low, close)

    def mean_reversion_features(self, close: pd.Series) -> pd.DataFrame:
        return compute_mean_reversion_features(close)

    def momentum_features(self, close: pd.Series) -> pd.DataFrame:
        return compute_momentum_features(close)

    def range_features(
        self, high: pd.Series, low: pd.Series, close: pd.Series
    ) -> pd.DataFrame:
        return compute_range_features(high, low, close)

    # ------------------------------------------------------------------
    # Normalisation helpers
    # ------------------------------------------------------------------

    def rolling_zscore(
        self, series: pd.Series, window: int = _ZSCORE_WINDOW
    ) -> pd.Series:
        return rolling_zscore(series, window)

    def normalise_features(
        self, features: pd.DataFrame, window: int = _ZSCORE_WINDOW
    ) -> pd.DataFrame:
        return features.apply(lambda col: rolling_zscore(col, window))

    # ------------------------------------------------------------------
    # Cross-symbol helpers
    # ------------------------------------------------------------------

    def build_multi_symbol_features(
        self,
        multi_ohlcv: pd.DataFrame,
        normalise: bool = True,
    ) -> dict[str, pd.DataFrame]:
        """Run build_features for each symbol in a MultiIndex (symbol, timestamp) DataFrame."""
        if not isinstance(multi_ohlcv.index, pd.MultiIndex):
            raise ValueError("Expected MultiIndex with levels (symbol, timestamp).")
        result: dict[str, pd.DataFrame] = {}
        for symbol in multi_ohlcv.index.get_level_values(0).unique():
            sym_ohlcv = multi_ohlcv.loc[symbol]
            result[symbol] = build_features(sym_ohlcv, normalise=normalise, dropna=True)
        return result

    def aggregate_universe_features(
        self, symbol_features: dict[str, pd.DataFrame]
    ) -> pd.DataFrame:
        """Cross-sectional mean of each feature across all symbols."""
        return pd.concat(symbol_features.values()).groupby(level=0).mean()
