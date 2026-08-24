"""The :class:`RegimeFrame`: one label per timestamp, per component.

Reads exclusively from :class:`~cfdinvest.preprocessing.AnalysisFrame` and
writes nothing back. The regime layer is a *description* of conditions --
what kind of market and macro backdrop this bar sits in -- and takes no view
on what to trade with that information.

Missing inputs degrade to ``unknown`` rather than raising, so a lake with no
macro data still yields market regimes, and a universe of one asset still
yields volatility and trend.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Any, Sequence

import pandas as pd

from cfdinvest.regimes import combined as combined_module
from cfdinvest.regimes import correlation as correlation_module
from cfdinvest.regimes import macro as macro_module
from cfdinvest.regimes import trend as trend_module
from cfdinvest.regimes import volatility as volatility_module
from cfdinvest.regimes.labels import UNKNOWN, as_regime
from cfdinvest.schema import TIMESTAMP

logger = logging.getLogger(__name__)

__all__ = ["RegimeConfig", "RegimeFrame", "classify_regimes", "attach_regimes"]


@dataclass(frozen=True, slots=True)
class RegimeConfig:
    """Thresholds and windows for the regime classifiers."""

    # -- volatility ------------------------------------------------------- #
    #: Trailing window for the volatility percentile rank.
    volatility_window: int = 252
    volatility_low_threshold: float = 1 / 3
    volatility_high_threshold: float = 2 / 3
    realized_weight: float = 0.5
    atr_weight: float = 0.5

    # -- trend ------------------------------------------------------------ #
    fast_ma: int = 50
    slow_ma: int = 200
    momentum_threshold: float = 0.02
    #: Per-asset score at or beyond which a direction is claimed (max 2.0).
    trend_score_threshold: float = 1.0
    #: Net breadth that must be strictly exceeded for a directional call.
    breadth_threshold: float = 0.2

    # -- correlation ------------------------------------------------------ #
    diversified_threshold: float = 0.3
    hedging_threshold: float = -0.1
    co_moving_threshold: float = 0.2
    equity_tickers: Sequence[str] = correlation_module.DEFAULT_EQUITY_TICKERS
    bond_tickers: Sequence[str] = correlation_module.DEFAULT_BOND_TICKERS

    # -- macro ------------------------------------------------------------ #
    gdp_series: Sequence[str] = macro_module.DEFAULT_GDP_SERIES
    cpi_series: Sequence[str] = macro_module.DEFAULT_CPI_SERIES
    unemployment_series: Sequence[str] = macro_module.DEFAULT_UNEMPLOYMENT_SERIES
    #: Lookback for year-on-year growth and inflation, in bars.
    macro_lookback: int | None = None
    #: Lookback for the change-in-rate momentum, in bars.
    macro_momentum_lookback: int = 63
    inflation_threshold: float = 0.005
    labor_lookback: int = 63
    labor_smoothing: int = 21


@dataclass(frozen=True, slots=True)
class RegimeFrame:
    """Per-timestamp regime labels and their provenance.

    Attributes
    ----------
    volatility_regime:
        ``low`` / ``medium`` / ``high``.
    trend_regime:
        ``uptrend`` / ``sideways`` / ``downtrend``.
    correlation_regime:
        ``diversified`` / ``normal`` / ``breakdown``.
    macro_regime:
        ``expansion`` / ``overheating`` / ``slowdown`` / ``stagflation`` /
        ``contraction``.
    combined_regime:
        ``crisis`` / ``stagflation`` / ``risk_off`` / ``neutral`` /
        ``risk_on`` / ``expansion``.
    components:
        Sub-readings that fed the headline labels -- growth, inflation,
        labour, the equity-bond hedge state, and the ambiguity flags.
    metadata:
        Windows, thresholds, universe and calendar bounds.

    Every component may also be ``unknown``, which means the inputs were
    absent or still warming up -- never a guess.
    """

    volatility_regime: pd.Series = field(default_factory=pd.Series)
    trend_regime: pd.Series = field(default_factory=pd.Series)
    correlation_regime: pd.Series = field(default_factory=pd.Series)
    macro_regime: pd.Series = field(default_factory=pd.Series)
    combined_regime: pd.Series = field(default_factory=pd.Series)
    components: pd.DataFrame = field(default_factory=pd.DataFrame)
    metadata: dict[str, Any] = field(default_factory=dict)

    # -- views -------------------------------------------------------------- #
    @property
    def index(self) -> pd.Index:
        """The calendar every label shares."""
        return self.combined_regime.index

    @property
    def ambiguous(self) -> pd.Series:
        """Whether each bar's combined label is a contestable judgement call."""
        if "ambiguous" in self.components.columns:
            return self.components["ambiguous"]
        return pd.Series(False, index=self.index, name="ambiguous")

    def to_frame(self) -> pd.DataFrame:
        """Return the five headline regimes as one DataFrame."""
        return pd.DataFrame(
            {
                "volatility_regime": self.volatility_regime,
                "trend_regime": self.trend_regime,
                "correlation_regime": self.correlation_regime,
                "macro_regime": self.macro_regime,
                "combined_regime": self.combined_regime,
            },
            index=self.index,
        )

    def summary(self) -> pd.DataFrame:
        """Count bars per label for every component.

        Includes zero-count labels, because a regime that never occurred is
        itself worth seeing -- it usually means a threshold is mis-set.
        """
        rows = []
        for name, series in self.to_frame().items():
            counts = series.value_counts(dropna=False)
            for label, count in counts.items():
                rows.append(
                    {
                        "component": name,
                        "regime": str(label),
                        "bars": int(count),
                        "share": float(count) / max(len(series), 1),
                    }
                )
        return pd.DataFrame(rows)

    def transitions(self, component: str = "combined_regime") -> pd.DataFrame:
        """Return each contiguous run of one regime, with its span.

        Useful for sanity-checking that a classifier is not flickering: a
        regime that changes every other bar is measuring noise.
        """
        series = self.to_frame()[component]
        if series.empty:
            return pd.DataFrame(columns=["regime", "start", "end", "bars"])

        values = series.astype("object")
        changed = values.ne(values.shift())
        group = changed.cumsum()

        runs = (
            pd.DataFrame({"regime": values, "timestamp": series.index, "group": group})
            .groupby("group", sort=True)
            .agg(
                regime=("regime", "first"),
                start=("timestamp", "min"),
                end=("timestamp", "max"),
                bars=("timestamp", "size"),
            )
            .reset_index(drop=True)
        )
        return runs


# --------------------------------------------------------------------------- #
# Builder
# --------------------------------------------------------------------------- #
def classify_regimes(
    analysis_frame,
    config: RegimeConfig | None = None,
) -> RegimeFrame:
    """Classify every timestamp of an :class:`AnalysisFrame`.

    Pure with respect to its input: the analysis frame is read, never
    mutated. Each component is computed independently, so one missing input
    degrades exactly one component.

    Parameters
    ----------
    analysis_frame:
        A :class:`~cfdinvest.preprocessing.AnalysisFrame`.
    config:
        Thresholds and windows.

    Returns
    -------
    RegimeFrame
    """
    settings = config or RegimeConfig()
    calendar = analysis_frame.calendar

    volatility = _classify_volatility(analysis_frame, settings, calendar)
    trend = _classify_trend(analysis_frame, settings, calendar)
    hedge, correlation = _classify_correlation(analysis_frame, settings, calendar)
    macro_components = _classify_macro(analysis_frame, settings, calendar)
    macro = macro_components["macro_regime"]

    combined = combined_module.classify_combined(
        volatility, trend, correlation, macro, index=calendar
    )

    components = pd.concat(
        [
            macro_components[["growth_regime", "inflation_regime", "labor_regime"]],
            hedge.rename("hedge_regime"),
            combined[["rule", "ambiguous", "ambiguity_reason"]],
        ],
        axis=1,
    )

    metadata = {
        "bars": len(calendar),
        "calendar_start": calendar.min() if len(calendar) else None,
        "calendar_end": calendar.max() if len(calendar) else None,
        "tickers": analysis_frame.metadata.get("tickers", ()),
        "series_ids": analysis_frame.metadata.get("series_ids", ()),
        "periods_per_year": analysis_frame.metadata.get("periods_per_year"),
        "macro_lookback": _macro_lookback(analysis_frame, settings),
        "config": {
            "volatility_window": settings.volatility_window,
            "fast_ma": settings.fast_ma,
            "slow_ma": settings.slow_ma,
            "breadth_threshold": settings.breadth_threshold,
            "diversified_threshold": settings.diversified_threshold,
            "co_moving_threshold": settings.co_moving_threshold,
        },
        "ambiguous_bars": int(combined["ambiguous"].sum()),
        "decision_table_size": len(combined_module.decision_table()),
    }

    return RegimeFrame(
        volatility_regime=volatility,
        trend_regime=trend,
        correlation_regime=correlation,
        macro_regime=macro,
        combined_regime=combined["combined_regime"],
        components=components,
        metadata=metadata,
    )


def _macro_lookback(analysis_frame, settings: RegimeConfig) -> int:
    """Bars in a year, from config or the frame's own annualisation factor."""
    if settings.macro_lookback is not None:
        return settings.macro_lookback
    return int(analysis_frame.metadata.get("periods_per_year") or 252)


def _classify_volatility(analysis_frame, settings, calendar) -> pd.Series:
    """Volatility regime from realised volatility and ATR."""
    realized = analysis_frame.volatility.get("realized", pd.DataFrame())
    atr = analysis_frame.volatility.get("atr")
    close = analysis_frame.prices.get("close")

    atr_pct = None
    if atr is not None and close is not None and not atr.empty:
        atr_pct = volatility_module.atr_percent(atr, close)

    if realized is None or realized.empty:
        logger.debug("No realised volatility; volatility regime will be unknown")
        return as_regime(
            [UNKNOWN] * len(calendar),
            volatility_module.VOLATILITY_LABELS,
            index=calendar,
            name="volatility_regime",
        )

    result = volatility_module.classify_volatility(
        realized,
        atr_pct,
        window=settings.volatility_window,
        low_threshold=settings.volatility_low_threshold,
        high_threshold=settings.volatility_high_threshold,
        realized_weight=settings.realized_weight,
        atr_weight=settings.atr_weight,
    )
    return _conform(result, calendar, volatility_module.VOLATILITY_LABELS,
                    "volatility_regime")


def _classify_trend(analysis_frame, settings, calendar) -> pd.Series:
    """Trend regime from moving averages and rolling returns."""
    close = analysis_frame.prices.get("close")
    rolling = analysis_frame.returns.get("rolling", pd.DataFrame())

    if close is None or close.empty:
        return as_regime(
            [UNKNOWN] * len(calendar),
            trend_module.TREND_LABELS,
            index=calendar,
            name="trend_regime",
        )

    result = trend_module.classify_trend(
        close,
        rolling,
        fast=settings.fast_ma,
        slow=settings.slow_ma,
        momentum_threshold=settings.momentum_threshold,
        score_threshold=settings.trend_score_threshold,
        breadth_threshold=settings.breadth_threshold,
    )
    return _conform(result, calendar, trend_module.TREND_LABELS, "trend_regime")


def _classify_correlation(analysis_frame, settings, calendar):
    """Correlation regime, plus the equity-bond hedge state that feeds it."""
    correlations = analysis_frame.correlations
    rolling = correlations.get("rolling")
    average = correlations.get("average")
    flags = correlations.get("breakdown")

    if average is not None and isinstance(average, pd.DataFrame):
        average = average.iloc[:, 0] if average.shape[1] else None

    hedge = as_regime(
        [UNKNOWN] * len(calendar),
        correlation_module.HEDGE_LABELS,
        index=calendar,
        name="hedge_regime",
    )
    if rolling is not None and not rolling.empty:
        cross = correlation_module.cross_group_correlation(
            rolling, settings.equity_tickers, settings.bond_tickers
        )
        hedge = _conform(
            correlation_module.classify_hedge(
                cross,
                hedging_threshold=settings.hedging_threshold,
                co_moving_threshold=settings.co_moving_threshold,
            ),
            calendar,
            correlation_module.HEDGE_LABELS,
            "hedge_regime",
        )

    regime = correlation_module.classify_correlation(
        average,
        flags,
        hedge,
        diversified_threshold=settings.diversified_threshold,
        index=calendar,
    )
    return hedge, _conform(
        regime, calendar, correlation_module.CORRELATION_LABELS, "correlation_regime"
    )


def _classify_macro(analysis_frame, settings, calendar) -> pd.DataFrame:
    """Growth, inflation, labour and the headline macro regime."""
    lookback = _macro_lookback(analysis_frame, settings)
    classified = macro_module.classify_macro(
        analysis_frame.macro,
        gdp_series=settings.gdp_series,
        cpi_series=settings.cpi_series,
        unemployment_series=settings.unemployment_series,
        growth_periods=lookback,
        growth_momentum_periods=settings.macro_momentum_lookback,
        inflation_periods=lookback,
        inflation_momentum_periods=settings.macro_momentum_lookback,
        inflation_threshold=settings.inflation_threshold,
        labor_periods=settings.labor_lookback,
        labor_smoothing=settings.labor_smoothing,
    )

    # Each column keeps its own declared label set, so conforming to the
    # calendar cannot silently widen or narrow the categories.
    label_sets = {
        "growth_regime": macro_module.GROWTH_LABELS,
        "inflation_regime": macro_module.INFLATION_LABELS,
        "labor_regime": macro_module.LABOR_LABELS,
        "macro_regime": macro_module.MACRO_LABELS,
    }
    return pd.DataFrame(
        {
            name: _conform(classified.get(name), calendar, categories, name)
            for name, categories in label_sets.items()
        },
        index=calendar,
    )


def _conform(series: pd.Series, calendar, categories, name) -> pd.Series:
    """Reindex a component onto the master calendar, filling gaps as unknown."""
    if series is None or len(series) == 0:
        return as_regime([UNKNOWN] * len(calendar), categories, index=calendar, name=name)
    aligned = series.reindex(calendar).astype("object").fillna(UNKNOWN)
    return as_regime(aligned, categories, index=calendar, name=name)


def attach_regimes(analysis_frame, regime_frame: RegimeFrame):
    """Return a copy of ``analysis_frame`` with its ``regimes`` slot filled.

    Preprocessing left :attr:`AnalysisFrame.regimes` as an empty placeholder
    carrying only the calendar. This fills it *without modifying
    preprocessing*: the frame is frozen, so a copy is returned.

    Returns
    -------
    AnalysisFrame
        A new frame; the original is untouched.
    """
    return replace(analysis_frame, regimes=regime_frame.to_frame())
