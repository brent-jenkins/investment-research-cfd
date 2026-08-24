"""Macro regime from the publication-aware forward-filled macro panel.

Three independent readings, then a quadrant:

``growth``      expansion / slowdown / contraction   (real GDP)
``inflation``   rising / stable / falling            (CPI)
``labor``       tightening / loosening               (unemployment rate)

The macro regime itself is the growth-inflation quadrant, because that pair
is what actually determines which assets work: falling growth with rising
prices (stagflation) is hostile to both equities and bonds simultaneously,
which no single-axis reading captures. Labour is carried alongside as
confirmation rather than folded in -- it lags, and folding a lagging series
into the headline label would smear the transitions.

**Look-ahead.** Every input arrives already publication-aligned from
preprocessing, so a value only exists here from the date it became public.
This module never reaches back to an observation date. Differencing a
forward-filled step function is deliberate: comparing today's *known* GDP to
the GDP that was *known* a year ago is exactly the point-in-time question.

All functions are pure and vectorised.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from cfdinvest.regimes.labels import (
    UNKNOWN,
    as_regime,
    bucket,
    unknown_regime,
    with_unknown,
)

logger = logging.getLogger(__name__)

__all__ = [
    "GROWTH_REGIMES",
    "INFLATION_REGIMES",
    "LABOR_REGIMES",
    "MACRO_REGIMES",
    "GROWTH_LABELS",
    "INFLATION_LABELS",
    "LABOR_LABELS",
    "MACRO_LABELS",
    "DEFAULT_GDP_SERIES",
    "DEFAULT_CPI_SERIES",
    "DEFAULT_UNEMPLOYMENT_SERIES",
    "year_over_year",
    "classify_growth",
    "classify_inflation",
    "classify_labor",
    "combine_macro",
    "classify_macro",
]

GROWTH_REGIMES: tuple[str, ...] = ("contraction", "slowdown", "expansion")
INFLATION_REGIMES: tuple[str, ...] = ("falling", "stable", "rising")
LABOR_REGIMES: tuple[str, ...] = ("tightening", "loosening")
MACRO_REGIMES: tuple[str, ...] = (
    "contraction",
    "stagflation",
    "slowdown",
    "expansion",
    "overheating",
)

GROWTH_LABELS: tuple[str, ...] = with_unknown(GROWTH_REGIMES)
INFLATION_LABELS: tuple[str, ...] = with_unknown(INFLATION_REGIMES)
LABOR_LABELS: tuple[str, ...] = with_unknown(LABOR_REGIMES)
MACRO_LABELS: tuple[str, ...] = with_unknown(MACRO_REGIMES)

#: FRED series ids this module looks for, in preference order. The first one
#: present in the panel is used, so a lake with only a subset still works.
DEFAULT_GDP_SERIES: tuple[str, ...] = ("GDPC1", "GDP", "INDPRO")
DEFAULT_CPI_SERIES: tuple[str, ...] = ("CPIAUCSL", "CPILFESL", "PCEPI")
DEFAULT_UNEMPLOYMENT_SERIES: tuple[str, ...] = ("UNRATE", "U6RATE")


def _first_present(macro: pd.DataFrame, candidates) -> pd.Series | None:
    """Return the first candidate column present in the panel, else ``None``."""
    for name in candidates:
        if name in macro.columns:
            return macro[name]
    return None


def year_over_year(series: pd.Series, periods: int) -> pd.Series:
    """Fractional change over ``periods`` bars.

    Pure. Non-positive denominators are excluded, because a percentage change
    from zero or a negative level is not meaningful -- some FRED series (net
    exports, real rates) legitimately go negative.
    """
    if series is None or series.empty:
        return pd.Series(dtype="float64")
    if periods < 1:
        raise ValueError(f"periods must be >= 1, got {periods}")

    previous = series.shift(periods)
    return series / previous.where(previous > 0) - 1.0


def classify_growth(
    gdp: pd.Series | None,
    *,
    periods: int = 252,
    momentum_periods: int = 63,
    contraction_threshold: float = 0.0,
    momentum_threshold: float = 0.0,
    index: pd.Index | None = None,
) -> pd.Series:
    """Classify growth as expansion / slowdown / contraction.

    * ``contraction`` -- year-on-year growth at or below
      ``contraction_threshold`` (default: outright negative).
    * ``slowdown`` -- still growing, but *decelerating*: growth is lower than
      it was ``momentum_periods`` ago. This is the distinction the label set
      demands and a level-only rule cannot make -- 2% growth on the way down
      is a different world from 2% on the way up.
    * ``expansion`` -- growing and not decelerating.

    Returns
    -------
    pandas.Series
        Categorical over :data:`GROWTH_LABELS`.
    """
    if gdp is None or gdp.empty:
        return unknown_regime(
            index if index is not None else pd.Index([]),
            GROWTH_LABELS,
            name="growth_regime",
        )

    growth = year_over_year(gdp, periods)
    momentum = growth - growth.shift(momentum_periods)

    contracting = (growth <= contraction_threshold).fillna(False)
    decelerating = (momentum < momentum_threshold).fillna(False)

    labels = np.select(
        [growth.isna().to_numpy(), contracting.to_numpy(), decelerating.to_numpy()],
        [UNKNOWN, "contraction", "slowdown"],
        default="expansion",
    )
    return as_regime(labels, GROWTH_LABELS, index=gdp.index, name="growth_regime")


def classify_inflation(
    cpi: pd.Series | None,
    *,
    periods: int = 252,
    momentum_periods: int = 63,
    threshold: float = 0.005,
    index: pd.Index | None = None,
) -> pd.Series:
    """Classify inflation as rising / stable / falling.

    Judges the *change in* the year-on-year rate, not the rate itself: 3%
    inflation heading to 5% and 3% heading to 1% call for opposite
    positioning, and a level rule cannot tell them apart.

    Parameters
    ----------
    threshold:
        Change in the annual rate, in fractional terms, needed to call a
        direction. The default of ``0.005`` is 0.5 percentage points over
        ``momentum_periods``.

    Returns
    -------
    pandas.Series
        Categorical over :data:`INFLATION_LABELS`.
    """
    if cpi is None or cpi.empty:
        return unknown_regime(
            index if index is not None else pd.Index([]),
            INFLATION_LABELS,
            name="inflation_regime",
        )
    if threshold < 0:
        raise ValueError(f"threshold must be >= 0, got {threshold}")

    rate = year_over_year(cpi, periods)
    change = rate - rate.shift(momentum_periods)

    labels = bucket(change, (-threshold, threshold), INFLATION_REGIMES)
    return as_regime(
        labels, INFLATION_LABELS, index=cpi.index, name="inflation_regime"
    )


def classify_labor(
    unemployment: pd.Series | None,
    *,
    periods: int = 63,
    smoothing: int = 21,
    index: pd.Index | None = None,
) -> pd.Series:
    """Classify the labour market as tightening / loosening.

    Falling unemployment tightens, rising loosens. The level is smoothed
    before differencing, which is what keeps a single noisy print from
    flipping the label; a dead band is deliberately *not* used, because the
    requested label set has exactly two states.

    A change of exactly zero counts as ``tightening`` (non-worsening).

    Returns
    -------
    pandas.Series
        Categorical over :data:`LABOR_LABELS`.
    """
    if unemployment is None or unemployment.empty:
        return unknown_regime(
            index if index is not None else pd.Index([]),
            LABOR_LABELS,
            name="labor_regime",
        )

    smoothed = unemployment.rolling(max(smoothing, 1), min_periods=1).mean()
    change = smoothed - smoothed.shift(periods)

    labels = np.select(
        [change.isna().to_numpy(), (change <= 0).to_numpy()],
        [UNKNOWN, "tightening"],
        default="loosening",
    )
    return as_regime(
        labels, LABOR_LABELS, index=unemployment.index, name="labor_regime"
    )


def combine_macro(growth: pd.Series, inflation: pd.Series) -> pd.Series:
    """Map the growth-inflation quadrant to a single macro regime.

    ==============  ==============  ==================
    growth          inflation       macro_regime
    ==============  ==============  ==================
    expansion       rising          ``overheating``
    expansion       stable/falling  ``expansion``
    slowdown        rising          ``stagflation``
    slowdown        stable/falling  ``slowdown``
    contraction     rising          ``stagflation``
    contraction     stable/falling  ``contraction``
    any unknown     -               ``unknown``
    ==============  ==============  ==================

    Both slowdown and contraction map to ``stagflation`` under rising prices:
    the defining feature is weakening growth *with* accelerating inflation,
    and the depth of the weakness does not change which assets suffer.

    Pure and total over the two label sets.
    """
    if growth.empty or inflation.empty:
        index = growth.index if not growth.empty else inflation.index
        return unknown_regime(index, MACRO_LABELS, name="macro_regime")

    growth_values = growth.astype("object").reindex(inflation.index.union(growth.index))
    inflation_values = inflation.astype("object").reindex(growth_values.index)

    known = growth_values.isin(GROWTH_REGIMES) & inflation_values.isin(
        INFLATION_REGIMES
    )
    rising = inflation_values.eq("rising")

    labels = np.select(
        [
            (~known).to_numpy(),
            (growth_values.eq("expansion") & rising).to_numpy(),
            growth_values.eq("expansion").to_numpy(),
            rising.to_numpy(),                                    # weak + rising
            growth_values.eq("slowdown").to_numpy(),
        ],
        [UNKNOWN, "overheating", "expansion", "stagflation", "slowdown"],
        default="contraction",
    )
    return as_regime(
        labels, MACRO_LABELS, index=growth_values.index, name="macro_regime"
    )


def classify_macro(
    macro: pd.DataFrame,
    *,
    gdp_series=DEFAULT_GDP_SERIES,
    cpi_series=DEFAULT_CPI_SERIES,
    unemployment_series=DEFAULT_UNEMPLOYMENT_SERIES,
    growth_periods: int = 252,
    growth_momentum_periods: int = 63,
    inflation_periods: int = 252,
    inflation_momentum_periods: int = 63,
    inflation_threshold: float = 0.005,
    labor_periods: int = 63,
    labor_smoothing: int = 21,
) -> pd.DataFrame:
    """Classify the macro backdrop from a forward-filled macro panel.

    Missing series are tolerated: each component independently reports
    ``unknown``, and the headline regime is ``unknown`` only when growth or
    inflation is unavailable. A lake with no macro data at all still produces
    a correctly shaped, entirely-``unknown`` result.

    Parameters
    ----------
    macro:
        ``timestamp x series_id`` from ``AnalysisFrame.macro``.
    gdp_series, cpi_series, unemployment_series:
        Candidate FRED ids in preference order.

    Returns
    -------
    pandas.DataFrame
        Columns ``growth_regime``, ``inflation_regime``, ``labor_regime``,
        ``macro_regime``; all categorical.
    """
    index = macro.index if macro is not None else pd.Index([])
    if macro is None or macro.empty:
        empty = {
            "growth_regime": unknown_regime(index, GROWTH_LABELS),
            "inflation_regime": unknown_regime(index, INFLATION_LABELS),
            "labor_regime": unknown_regime(index, LABOR_LABELS),
            "macro_regime": unknown_regime(index, MACRO_LABELS),
        }
        return pd.DataFrame(empty, index=index)

    gdp = _first_present(macro, gdp_series)
    cpi = _first_present(macro, cpi_series)
    unemployment = _first_present(macro, unemployment_series)

    for label, found, candidates in (
        ("growth", gdp, gdp_series),
        ("inflation", cpi, cpi_series),
        ("labour", unemployment, unemployment_series),
    ):
        if found is None:
            logger.info(
                "No %s series in the macro panel (looked for %s); that component "
                "will be 'unknown'.",
                label,
                ", ".join(candidates),
            )

    growth = classify_growth(
        gdp,
        periods=growth_periods,
        momentum_periods=growth_momentum_periods,
        index=index,
    )
    inflation = classify_inflation(
        cpi,
        periods=inflation_periods,
        momentum_periods=inflation_momentum_periods,
        threshold=inflation_threshold,
        index=index,
    )
    labor = classify_labor(
        unemployment, periods=labor_periods, smoothing=labor_smoothing, index=index
    )

    growth = (
        growth.reindex(index) if len(growth) else unknown_regime(index, GROWTH_LABELS)
    )
    inflation = (
        inflation.reindex(index)
        if len(inflation)
        else unknown_regime(index, INFLATION_LABELS)
    )
    labor = labor.reindex(index) if len(labor) else unknown_regime(index, LABOR_LABELS)

    combined = combine_macro(growth, inflation).reindex(index)

    return pd.DataFrame(
        {
            "growth_regime": as_regime(
                growth.astype("object"), GROWTH_LABELS, index=index
            ),
            "inflation_regime": as_regime(
                inflation.astype("object"), INFLATION_LABELS, index=index
            ),
            "labor_regime": as_regime(
                labor.astype("object"), LABOR_LABELS, index=index
            ),
            "macro_regime": as_regime(
                combined.astype("object"), MACRO_LABELS, index=index
            ),
        },
        index=index,
    )
