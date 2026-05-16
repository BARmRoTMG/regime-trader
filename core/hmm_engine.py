"""HMM regime detection engine.

The HMM is a VOLATILITY CLASSIFIER.  It answers: "Is the market in a calm,
moderate, or turbulent volatility environment right now?"  It does NOT predict
price direction.  The strategy layer reads the classification and adjusts
portfolio allocation accordingly.

Critical design constraint
--------------------------
We use the FORWARD ALGORITHM only, never model.predict() (Viterbi).  Viterbi
re-visits and revises past state assignments using future observations, which
introduces look-ahead bias into backtests.  The forward algorithm computes
P(state_t | obs_1 … obs_t) using only data available at bar T.

Regime labels
-------------
States are sorted by MEAN LOG-RETURN (ascending) and assigned human-readable
names (CRASH → BULL etc.).  The label ordering is for human readability only.
The strategy layer independently sorts by VOLATILITY when deciding allocation.
"""

from __future__ import annotations

import logging
import pickle
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from hmmlearn.hmm import GaussianHMM
from scipy.special import logsumexp
from scipy.stats import multivariate_normal

from data.feature_engineering import FEATURE_COLUMNS, F_LOG_RETURN_1

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Regime label maps (sorted by mean return, ascending)
# ---------------------------------------------------------------------------

REGIME_LABELS: dict[int, list[str]] = {
    3: ["BEAR", "NEUTRAL", "BULL"],
    4: ["CRASH", "BEAR", "BULL", "EUPHORIA"],
    5: ["CRASH", "BEAR", "NEUTRAL", "BULL", "EUPHORIA"],
    6: ["CRASH", "STRONG_BEAR", "WEAK_BEAR", "WEAK_BULL", "STRONG_BULL", "EUPHORIA"],
    7: ["CRASH", "STRONG_BEAR", "WEAK_BEAR", "NEUTRAL", "WEAK_BULL", "STRONG_BULL", "EUPHORIA"],
}

# Recommended strategy type by label (used to populate RegimeInfo)
_STRATEGY_BY_LABEL: dict[str, str] = {
    "CRASH":        "defensive",
    "STRONG_BEAR":  "defensive",
    "BEAR":         "defensive",
    "WEAK_BEAR":    "reduced",
    "NEUTRAL":      "moderate",
    "WEAK_BULL":    "growth",
    "BULL":         "growth",
    "STRONG_BULL":  "growth",
    "EUPHORIA":     "reduced",   # trim in euphoria – mean reversion risk
}

_MAX_LEVERAGE_BY_LABEL: dict[str, float] = {
    "CRASH":        0.0,
    "STRONG_BEAR":  0.0,
    "BEAR":         0.5,
    "WEAK_BEAR":    0.75,
    "NEUTRAL":      1.0,
    "WEAK_BULL":    1.0,
    "BULL":         1.25,
    "STRONG_BULL":  1.25,
    "EUPHORIA":     1.0,
}

_MAX_POS_PCT_BY_LABEL: dict[str, float] = {
    "CRASH":        0.0,
    "STRONG_BEAR":  0.0,
    "BEAR":         0.10,
    "WEAK_BEAR":    0.12,
    "NEUTRAL":      0.15,
    "WEAK_BULL":    0.15,
    "BULL":         0.15,
    "STRONG_BULL":  0.15,
    "EUPHORIA":     0.12,
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class RegimeInfo:
    """Static metadata about one HMM state, populated after fitting."""

    regime_id: int
    regime_name: str
    expected_return: float         # mean annualised log-return for this state
    expected_volatility: float     # mean annualised realised volatility for this state
    recommended_strategy_type: str
    max_leverage_allowed: float
    max_position_size_pct: float
    min_confidence_to_act: float = 0.55


@dataclass
class RegimeState:
    """Current regime output, including stability information."""

    label: str                      # return-sorted label e.g. "BULL"
    state_id: int                   # raw HMM state index
    probability: float              # filtered P(state_t | obs_1:t)
    state_probabilities: np.ndarray # full filtered distribution (n_states,)
    timestamp: pd.Timestamp
    is_confirmed: bool              # True once stable for stability_bars
    consecutive_bars: int           # bars spent in the current confirmed state

    # Convenience properties for backward compatibility
    @property
    def confidence(self) -> float:
        return self.probability

    @property
    def is_stable(self) -> bool:
        return self.is_confirmed

    @property
    def posteriors(self) -> np.ndarray:
        return self.state_probabilities

    @property
    def state_index(self) -> int:
        return self.state_id

    @property
    def flicker_count(self) -> int:
        # Populated by HMMEngine after construction via _flicker_count attribute
        return getattr(self, "_flicker_count", 0)


@dataclass
class HMMConfig:
    """Parameters consumed by HMMEngine, sourced from settings.yaml[hmm]."""

    n_candidates: list[int] = field(default_factory=lambda: [3, 4, 5, 6, 7])
    n_init: int = 10
    covariance_type: str = "full"
    min_train_bars: int = 504          # 2 trading years
    stability_bars: int = 3
    flicker_window: int = 20
    flicker_threshold: int = 4
    min_confidence: float = 0.55
    n_iter: int = 200                  # max EM iterations per candidate


# ---------------------------------------------------------------------------
# HMMEngine
# ---------------------------------------------------------------------------


class HMMEngine:
    """Gaussian HMM regime detector with forward-only (causal) inference.

    Workflow
    --------
    1. Call ``fit(ohlcv)`` with at least ``config.min_train_bars`` bars of OHLCV.
    2. Use ``get_current_regime(features)`` to classify the latest bar.
    3. For backtesting, call ``predict_regime_filtered(X)`` to get the full
       filtered probability matrix over a feature sequence.

    Parameters
    ----------
    config:
        HMM hyper-parameters.
    """

    def __init__(self, config: HMMConfig) -> None:
        self.config = config
        self._model: Optional[GaussianHMM] = None
        self._n_states: int = 0
        self._n_features: int = len(FEATURE_COLUMNS)

        # State ↔ label mappings (populated after fit)
        self._state_to_label: dict[int, str] = {}
        self._label_to_state: dict[str, int] = {}
        self._regimes: dict[int, RegimeInfo] = {}

        # Log-space transition matrix cache (populated after fit)
        self._log_transmat: Optional[np.ndarray] = None

        # Stability & flicker tracking (stateful, updated per bar)
        self._confirmed_state: int = -1
        self._candidate_state: int = -1
        self._candidate_bars: int = 0
        self._consecutive_bars: int = 0
        self._regime_history: list[int] = []   # raw state ids, last flicker_window bars

        # Incremental forward-algorithm cache (for live one-bar-at-a-time updates)
        self._cached_log_alpha: Optional[np.ndarray] = None

        # Training metadata
        self._training_date: Optional[datetime] = None
        self._selected_bic: float = np.inf
        self._all_bic_scores: dict[int, float] = {}

    # ------------------------------------------------------------------
    # Fitting
    # ------------------------------------------------------------------

    def fit(self, ohlcv: pd.DataFrame) -> None:
        """Train the HMM on OHLCV data.

        Features are computed internally via ``FeatureEngineer``.
        The best number of states is selected by BIC across
        ``config.n_candidates`` candidates with ``config.n_init`` restarts each.

        Parameters
        ----------
        ohlcv:
            DataFrame with columns open, high, low, close, volume and a
            DatetimeIndex sorted ascending.

        Raises
        ------
        ValueError
            If fewer than ``config.min_train_bars`` rows are available after
            feature computation and NaN-dropping.
        """
        from data.feature_engineering import build_features

        logger.info("HMMEngine.fit(): computing features …")
        features = build_features(ohlcv, normalise=True, dropna=True)

        if len(features) < self.config.min_train_bars:
            raise ValueError(
                f"Need at least {self.config.min_train_bars} bars after NaN-drop "
                f"(got {len(features)}).  Provide more history."
            )

        X = features.values.astype(float)
        logger.info("HMMEngine.fit(): selecting best model from candidates %s …", self.config.n_candidates)

        self._model = self._select_best_model(X)
        self._n_states = self._model.n_components
        self._n_features = X.shape[1]
        self._log_transmat = np.log(self._model.transmat_ + 1e-300)

        self._map_states_to_regimes(X)
        self._reset_stability_state()
        self._training_date = datetime.now(tz=timezone.utc)

        logger.info(
            "HMMEngine.fit(): selected n_states=%d  BIC=%.2f  training_date=%s",
            self._n_states,
            self._selected_bic,
            self._training_date.strftime("%Y-%m-%d"),
        )

    def _select_best_model(self, X: np.ndarray) -> GaussianHMM:
        """Cross-validate candidate models; return the one with the lowest BIC."""
        best_bic = np.inf
        best_model: Optional[GaussianHMM] = None
        bic_scores: dict[int, float] = {}

        for n_states in self.config.n_candidates:
            state_bic = np.inf
            state_model: Optional[GaussianHMM] = None

            for init_idx in range(self.config.n_init):
                try:
                    model = GaussianHMM(
                        n_components=n_states,
                        covariance_type=self.config.covariance_type,
                        n_iter=self.config.n_iter,
                        random_state=init_idx * 17 + n_states * 7,
                        tol=1e-4,
                        verbose=False,
                    )
                    model.fit(X)
                    if not model.monitor_.converged:
                        logger.debug(
                            "n_states=%d init=%d: did not converge in %d iters",
                            n_states, init_idx, self.config.n_iter,
                        )
                    bic = self._compute_bic(model, X)
                    if bic < state_bic:
                        state_bic = bic
                        state_model = model
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "HMM fit failed n_states=%d init=%d: %s", n_states, init_idx, exc
                    )

            if state_model is not None:
                bic_scores[n_states] = state_bic
                logger.info("  n_states=%d  BIC=%.2f", n_states, state_bic)
                if state_bic < best_bic:
                    best_bic = state_bic
                    best_model = state_model

        if best_model is None:
            raise RuntimeError("All HMM candidate fits failed.  Check data quality.")

        self._selected_bic = best_bic
        self._all_bic_scores = bic_scores
        logger.info(
            "BIC scores: %s  →  selected n_states=%d",
            {k: round(v, 1) for k, v in bic_scores.items()},
            best_model.n_components,
        )
        return best_model

    def _compute_bic(self, model: GaussianHMM, X: np.ndarray) -> float:
        """BIC = -2 * log_likelihood + n_params * log(n_samples).

        For a full-covariance Gaussian HMM with k states and d features:
            n_params = (k-1)              # start probs
                     + k*(k-1)           # transition matrix rows
                     + k*d               # means
                     + k*d*(d+1)//2      # full covariance matrices
        """
        n_samples, d = X.shape
        k = model.n_components
        n_params = (
            (k - 1)
            + k * (k - 1)
            + k * d
            + k * d * (d + 1) // 2
        )
        log_likelihood = model.score(X) * n_samples   # score() returns per-sample
        return -2.0 * log_likelihood + n_params * np.log(n_samples)

    def _map_states_to_regimes(self, X: np.ndarray) -> None:
        """Assign return-sorted labels and populate RegimeInfo for each state.

        States are ranked by mean log-return (index 0 in feature matrix).
        The lowest-return state gets CRASH / BEAR, the highest gets BULL / EUPHORIA.
        """
        assert self._model is not None

        # Mean log-return per state (feature index 0 = F_LOG_RETURN_1)
        ret_col = FEATURE_COLUMNS.index(F_LOG_RETURN_1)
        vol_col = FEATURE_COLUMNS.index("realized_vol_20")

        mean_returns = self._model.means_[:, ret_col]
        mean_vols = self._model.means_[:, vol_col]   # standardised; higher = more volatile

        # Sort state indices by ascending mean return
        sorted_by_return = np.argsort(mean_returns)   # index 0 = lowest return

        labels = REGIME_LABELS.get(self._n_states)
        if labels is None:
            # Fallback: generate generic labels
            labels = [f"REGIME_{i}" for i in range(self._n_states)]

        self._state_to_label = {}
        self._label_to_state = {}
        self._regimes = {}

        # Annualisation factor for standardised log-return → approximate annualised return
        ann_factor = 252.0

        for rank, state_idx in enumerate(sorted_by_return):
            label = labels[rank]
            raw_mean_ret = float(mean_returns[state_idx])
            raw_mean_vol = float(mean_vols[state_idx])

            info = RegimeInfo(
                regime_id=int(state_idx),
                regime_name=label,
                expected_return=raw_mean_ret * ann_factor,    # standardised-space proxy
                expected_volatility=raw_mean_vol,             # standardised-space proxy
                recommended_strategy_type=_STRATEGY_BY_LABEL.get(label, "moderate"),
                max_leverage_allowed=_MAX_LEVERAGE_BY_LABEL.get(label, 1.0),
                max_position_size_pct=_MAX_POS_PCT_BY_LABEL.get(label, 0.15),
                min_confidence_to_act=self.config.min_confidence,
            )
            self._regimes[state_idx] = info
            self._state_to_label[state_idx] = label
            self._label_to_state[label] = state_idx

        logger.info(
            "Regime labels (by ascending mean return): %s",
            {self._state_to_label[s]: round(float(mean_returns[s]), 4)
             for s in range(self._n_states)},
        )

    # ------------------------------------------------------------------
    # Forward algorithm (causal inference, NO look-ahead)
    # ------------------------------------------------------------------

    def predict_regime_filtered(
        self,
        features: np.ndarray | pd.DataFrame,
        use_cache: bool = False,
    ) -> np.ndarray:
        """Return filtered state probabilities P(state_t | obs_1 … obs_t) for every t.

        Uses ONLY data up to and including bar t.  Adding future bars beyond t
        does not change the output at t — this is the fundamental property that
        prevents look-ahead bias.

        Parameters
        ----------
        features:
            Observation sequence of shape (T, n_features).  May be a DataFrame
            with FEATURE_COLUMNS or a bare numpy array.
        use_cache:
            If True, continue from the cached alpha (last bar's filtered
            distribution) rather than restarting from the prior.  Useful for
            live one-bar-at-a-time updates.  The cache is always updated.

        Returns
        -------
        np.ndarray
            Shape (T, n_states).  Each row is a probability distribution over
            states (sums to 1) conditioned only on observations up to that row.
        """
        if not self.is_fitted():
            raise RuntimeError("HMMEngine is not fitted.  Call fit() first.")

        if isinstance(features, pd.DataFrame):
            X = features[FEATURE_COLUMNS].values.astype(float)
        else:
            X = np.asarray(features, dtype=float)

        if X.ndim != 2 or X.shape[1] != self._n_features:
            raise ValueError(
                f"Expected features of shape (T, {self._n_features}), got {X.shape}."
            )

        T = len(X)
        log_alphas = np.empty((T, self._n_states), dtype=float)

        # Precompute log emission probs for all observations at once
        log_emit = self._log_emission_probs_batch(X)   # (T, n_states)

        # --- Initialisation ---
        if use_cache and self._cached_log_alpha is not None:
            # Transition from the cached (previous bar's) alpha
            log_alpha = (
                logsumexp(
                    self._cached_log_alpha[:, np.newaxis] + self._log_transmat,
                    axis=0,
                )
                + log_emit[0]
            )
        else:
            log_alpha = np.log(self._model.startprob_ + 1e-300) + log_emit[0]

        log_alpha -= logsumexp(log_alpha)   # normalise (stay in log space)
        log_alphas[0] = log_alpha

        # --- Forward recursion ---
        for t in range(1, T):
            # P(state_t=j) ∝ sum_i P(state_{t-1}=i) * P(j|i) * P(obs_t|j)
            log_alpha = (
                logsumexp(
                    log_alphas[t - 1, :, np.newaxis] + self._log_transmat,
                    axis=0,
                )
                + log_emit[t]
            )
            log_alpha -= logsumexp(log_alpha)
            log_alphas[t] = log_alpha

        self._cached_log_alpha = log_alphas[-1]
        return np.exp(log_alphas)

    def _log_emission_probs_batch(self, X: np.ndarray) -> np.ndarray:
        """Compute log p(obs_t | state=s) for all t and all s.

        Returns shape (T, n_states).  Uses scipy multivariate_normal for
        numerical robustness; falls back to a regularised covariance on
        near-singular matrices.
        """
        T = len(X)
        log_probs = np.zeros((T, self._n_states), dtype=float)
        for s in range(self._n_states):
            cov = self._model.covars_[s]
            mean = self._model.means_[s]
            try:
                log_probs[:, s] = multivariate_normal.logpdf(X, mean=mean, cov=cov)
            except np.linalg.LinAlgError:
                # Near-singular: add small ridge regularisation
                cov_reg = cov + np.eye(self._n_features) * 1e-6
                log_probs[:, s] = multivariate_normal.logpdf(X, mean=mean, cov=cov_reg)
        return log_probs

    # ------------------------------------------------------------------
    # Public inference helpers
    # ------------------------------------------------------------------

    def get_current_regime(
        self,
        features: pd.DataFrame,
        update_stability: bool = True,
    ) -> RegimeState:
        """Classify the latest bar and apply stability / flicker filters.

        Parameters
        ----------
        features:
            Feature DataFrame up to *and including* the current bar.
        update_stability:
            If True (default), update the internal stability tracker.
            Set to False for read-only queries (e.g. from the dashboard).

        Returns
        -------
        RegimeState
            Reflects the *confirmed* regime (may differ from the raw
            prediction if a regime change has not yet been confirmed).
        """
        # Run forward algorithm on the full feature history
        proba = self.predict_regime_filtered(features)
        current_proba = proba[-1]                         # shape (n_states,)
        raw_state = int(np.argmax(current_proba))
        probability = float(current_proba[raw_state])

        ts = (
            features.index[-1]
            if isinstance(features.index, pd.DatetimeIndex)
            else pd.Timestamp.now(tz="UTC")
        )

        if update_stability:
            return self._apply_stability_filter(raw_state, probability, current_proba, ts)

        label = self._state_to_label[raw_state]
        return RegimeState(
            label=label,
            state_id=raw_state,
            probability=probability,
            state_probabilities=current_proba,
            timestamp=ts,
            is_confirmed=True,
            consecutive_bars=0,
        )

    def predict_latest(self, features: pd.DataFrame) -> RegimeState:
        """Convenience alias for ``get_current_regime``."""
        return self.get_current_regime(features)

    def predict(self, features: pd.DataFrame) -> list[RegimeState]:
        """Return a RegimeState for EVERY row in *features* (no stability filter).

        This re-runs the forward algorithm on the full sequence.  Suitable for
        backtesting where you want per-bar regime labels without modifying
        the live stability tracker.
        """
        if not self.is_fitted():
            raise RuntimeError("HMMEngine is not fitted.  Call fit() first.")

        X = features[FEATURE_COLUMNS].values.astype(float)
        probas = self.predict_regime_filtered(X)          # (T, n_states)
        index = features.index

        results: list[RegimeState] = []
        for t in range(len(probas)):
            proba_t = probas[t]
            state_t = int(np.argmax(proba_t))
            prob_t = float(proba_t[state_t])
            ts = index[t] if isinstance(index, pd.DatetimeIndex) else pd.Timestamp.now()
            results.append(
                RegimeState(
                    label=self._state_to_label[state_t],
                    state_id=state_t,
                    probability=prob_t,
                    state_probabilities=proba_t,
                    timestamp=ts,
                    is_confirmed=(prob_t >= self.config.min_confidence),
                    consecutive_bars=0,
                )
            )
        return results

    def predict_regime_proba(self, features: pd.DataFrame) -> np.ndarray:
        """Return filtered probability distributions for each bar. Shape (T, n_states)."""
        X = features[FEATURE_COLUMNS].values.astype(float)
        return self.predict_regime_filtered(X)

    # ------------------------------------------------------------------
    # Stability & flicker
    # ------------------------------------------------------------------

    def _apply_stability_filter(
        self,
        raw_state: int,
        probability: float,
        state_proba: np.ndarray,
        timestamp: pd.Timestamp,
    ) -> RegimeState:
        """Update stability state machine and return the (possibly lagged) confirmed regime."""
        # Append to flicker history
        self._regime_history.append(raw_state)
        if len(self._regime_history) > self.config.flicker_window:
            self._regime_history.pop(0)

        # Low confidence → return confirmed regime but mark as unconfirmed
        if probability < self.config.min_confidence or self.is_flickering():
            label = (
                self._state_to_label[self._confirmed_state]
                if self._confirmed_state >= 0
                else "UNKNOWN"
            )
            state = RegimeState(
                label=label,
                state_id=self._confirmed_state if self._confirmed_state >= 0 else raw_state,
                probability=probability,
                state_probabilities=state_proba,
                timestamp=timestamp,
                is_confirmed=False,
                consecutive_bars=self._consecutive_bars,
            )
            state._flicker_count = self.get_regime_flicker_rate()  # type: ignore[attr-defined]
            return state

        # ---- First bar ever ----
        if self._confirmed_state < 0:
            self._confirmed_state = raw_state
            self._candidate_state = raw_state
            self._candidate_bars = 1
            self._consecutive_bars = 1
            label = self._state_to_label[raw_state]
            logger.info("HMM initial regime: %s (prob=%.3f)", label, probability)
            return RegimeState(
                label=label,
                state_id=raw_state,
                probability=probability,
                state_probabilities=state_proba,
                timestamp=timestamp,
                is_confirmed=True,
                consecutive_bars=1,
            )

        # ---- Continuing in confirmed regime ----
        if raw_state == self._confirmed_state:
            self._candidate_state = raw_state
            self._candidate_bars = 1
            self._consecutive_bars += 1
            label = self._state_to_label[raw_state]
            return RegimeState(
                label=label,
                state_id=raw_state,
                probability=probability,
                state_probabilities=state_proba,
                timestamp=timestamp,
                is_confirmed=True,
                consecutive_bars=self._consecutive_bars,
            )

        # ---- New candidate state ----
        if raw_state == self._candidate_state:
            self._candidate_bars += 1
        else:
            self._candidate_state = raw_state
            self._candidate_bars = 1

        # ---- Candidate confirmed after stability_bars ----
        if self._candidate_bars >= self.config.stability_bars:
            old_label = self._state_to_label[self._confirmed_state]
            new_label = self._state_to_label[raw_state]
            logger.warning(
                "Regime change confirmed: %s → %s  (prob=%.3f, after %d bars)",
                old_label, new_label, probability, self._candidate_bars,
            )
            self._confirmed_state = raw_state
            self._consecutive_bars = self._candidate_bars
            self._candidate_bars = 1
            state = RegimeState(
                label=new_label,
                state_id=raw_state,
                probability=probability,
                state_probabilities=state_proba,
                timestamp=timestamp,
                is_confirmed=True,
                consecutive_bars=self._consecutive_bars,
            )
            state._flicker_count = self.get_regime_flicker_rate()  # type: ignore[attr-defined]
            return state

        # ---- Candidate not yet confirmed: hold previous regime ----
        label = self._state_to_label[self._confirmed_state]
        state = RegimeState(
            label=label,
            state_id=self._confirmed_state,
            probability=probability,
            state_probabilities=state_proba,
            timestamp=timestamp,
            is_confirmed=False,            # in transition
            consecutive_bars=self._consecutive_bars,
        )
        state._flicker_count = self.get_regime_flicker_rate()  # type: ignore[attr-defined]
        return state

    def _reset_stability_state(self) -> None:
        """Reset the stability tracker (call after re-fitting)."""
        self._confirmed_state = -1
        self._candidate_state = -1
        self._candidate_bars = 0
        self._consecutive_bars = 0
        self._regime_history = []
        self._cached_log_alpha = None

    # ------------------------------------------------------------------
    # Public stability / flicker queries
    # ------------------------------------------------------------------

    def get_regime_stability(self) -> int:
        """Number of consecutive bars the current confirmed regime has persisted."""
        return self._consecutive_bars

    def get_transition_matrix(self) -> np.ndarray:
        """Return the learned transition probability matrix (n_states × n_states)."""
        if not self.is_fitted():
            raise RuntimeError("HMMEngine is not fitted.")
        return self._model.transmat_.copy()

    def detect_regime_change(self, features: pd.DataFrame) -> bool:
        """Return True only if a confirmed regime change just occurred.

        A change is confirmed when the candidate state has persisted for
        at least ``config.stability_bars`` consecutive bars.
        """
        proba = self.predict_regime_filtered(features.values[-1:])
        raw_state = int(np.argmax(proba[0]))
        if raw_state != self._confirmed_state:
            return self._candidate_bars + 1 >= self.config.stability_bars
        return False

    def get_regime_flicker_rate(self) -> int:
        """Number of regime switches in the last ``config.flicker_window`` bars."""
        if len(self._regime_history) < 2:
            return 0
        return sum(
            self._regime_history[i] != self._regime_history[i - 1]
            for i in range(1, len(self._regime_history))
        )

    def is_flickering(self) -> bool:
        """True if the flicker rate exceeds ``config.flicker_threshold``."""
        return self.get_regime_flicker_rate() > self.config.flicker_threshold

    # ------------------------------------------------------------------
    # Regime info accessors
    # ------------------------------------------------------------------

    def get_regime_info(self, state_id: int) -> RegimeInfo:
        """Return RegimeInfo for a given raw state id."""
        if state_id not in self._regimes:
            raise KeyError(f"Unknown state_id {state_id}.  Valid: {list(self._regimes)}")
        return self._regimes[state_id]

    def get_all_regime_infos(self) -> dict[str, RegimeInfo]:
        """Return a dict keyed by label with all RegimeInfo objects."""
        return {info.regime_name: info for info in self._regimes.values()}

    def state_volatility_means(self) -> dict[int, float]:
        """Return mean (standardised) volatility per HMM state."""
        if not self.is_fitted():
            return {}
        vol_col = FEATURE_COLUMNS.index("realized_vol_20")
        return {
            s: float(self._model.means_[s, vol_col])
            for s in range(self._n_states)
        }

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, path: Path | str) -> None:
        """Pickle the engine to *path* (creates parent directories if needed)."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "model": self._model,
            "config": self.config,
            "n_states": self._n_states,
            "n_features": self._n_features,
            "state_to_label": self._state_to_label,
            "label_to_state": self._label_to_state,
            "regimes": self._regimes,
            "log_transmat": self._log_transmat,
            "training_date": self._training_date,
            "selected_bic": self._selected_bic,
            "all_bic_scores": self._all_bic_scores,
        }
        with open(path, "wb") as fh:
            pickle.dump(payload, fh, protocol=pickle.HIGHEST_PROTOCOL)
        logger.info("HMMEngine saved to %s", path)

    @classmethod
    def load(cls, path: Path | str) -> "HMMEngine":
        """Restore an HMMEngine from a pickle file."""
        with open(path, "rb") as fh:
            payload = pickle.load(fh)
        engine = cls(config=payload["config"])
        engine._model = payload["model"]
        engine._n_states = payload["n_states"]
        engine._n_features = payload["n_features"]
        engine._state_to_label = payload["state_to_label"]
        engine._label_to_state = payload["label_to_state"]
        engine._regimes = payload["regimes"]
        engine._log_transmat = payload["log_transmat"]
        engine._training_date = payload["training_date"]
        engine._selected_bic = payload["selected_bic"]
        engine._all_bic_scores = payload["all_bic_scores"]
        logger.info(
            "HMMEngine loaded from %s  (n_states=%d, trained=%s)",
            path, engine._n_states,
            engine._training_date.strftime("%Y-%m-%d") if engine._training_date else "unknown",
        )
        return engine

    # ------------------------------------------------------------------
    # Misc
    # ------------------------------------------------------------------

    def is_fitted(self) -> bool:
        return self._model is not None

    def __repr__(self) -> str:
        if not self.is_fitted():
            return "HMMEngine(not fitted)"
        return (
            f"HMMEngine(n_states={self._n_states}, "
            f"bic={self._selected_bic:.1f}, "
            f"trained={self._training_date.strftime('%Y-%m-%d') if self._training_date else 'unknown'})"
        )
