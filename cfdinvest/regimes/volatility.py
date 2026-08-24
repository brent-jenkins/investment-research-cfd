"""Volatility regime: low / medium / high.

Blends the two volatility measures preprocessing produces, because they see
different things:

* **Realised volatility** is close-to-close. It misses everything that
  happened inside the bar.
* **ATR** uses the high/low range and the overnight gap, so it catches the
  violent intraday session that closed flat -- the one that stops out a CFD
  position while the realised number stays calm.

Both are converted to a *trailing* percentile rank before blending, which
solves two problems at once: it makes them comparable despite ATR being in
price units, and it makes "high" mean high for this market's own recent
history rather than against a hardcoded number that ages badly.

All functions are pure and vectorised.
"""

from __future__ import annotations

import pandas as pd

from cfdinvest.regimes.labels import (
    UNKNOWN,
    as_regime,
    bucket,
    cross_sectional,
    trailing_rank,
    with_unknown,
)

__all__ = [
    "VOLATILITY_REGIMES",
    "VOLATILITY_LABELS",
    "atr_percent",
    "volatility_pressure",
    "classify_volatility",
    "asset_volatility_regime",
]

#: Ordered label set, calm to violent.
VOLATILITY_REGIMES: tuple[str, ...] = ("low", "medium", "high")

#: The complete category set, including the undecided case.
VOLATILITY_LABELS: tuple[str, ...] = with_unknown(VOLATILITY_REGIMES)


def atr_percent(atr: pd.DataFrame, close: pd.DataFrame) -> pd.DataFrame:
    """Express ATR as a fraction of price.

    Raw ATR is in price units, so a 40-point range on a 4000-level index and
    a 40-point range on a 100-dollar stock are not remotely comparable.
    Dividing by close makes the measure cross-sectional.

    Pure. Non-positive closes are excluded rather than producing a division
    blow-up.
    """
    if atr is None or close is None or atr.empty or close.empty:
        return pd.DataFrame(index=getattr(atr, "index", None))

    numerator, denominator = atr.align(close, join="inner")
    return numerator / denominator.where(denominator > 0)


def volatility_pressure(
    realized: pd.DataFrame,
    atr_pct: pd.DataFrame | None = None,
    *,
    window: int = 252,
    min_periods: int | None = None,
    realized_weight: float = 0.5,
    atr_weight: float = 0.5,
    aggregation: str = "median",
) -> pd.Series:
    """Blend both volatility measures into one market-level score in ``(0, 1]``.

    Each measure is collapsed across the universe, converted to a trailing
    percentile rank, then averaged with the given weights. When one measure
    is missing the other carries the full weight, so a lake without high/low
    data still produces a usable score instead of nothing.

    Parameters
    ----------
    realized:
        Annualised realised volatility panel.
    atr_pct:
        ATR as a fraction of price; see :func:`atr_percent`. Optional.
    window:
        Trailing window for the percentile rank.
    realized_weight, atr_weight:
        Relative weights. Renormalised over whichever measures are present.

    Returns
    -------
    pandas.Series
        Higher means more volatile *relative to this market's own history*.
    """
    components: list[tuple[pd.Series, float]] = []

    if realized is not None and not realized.empty and realized_weight > 0:
        market = cross_sectional(realized, aggregation)
        components.append(
            (trailing_rank(market, window, min_periods=min_periods), realized_weight)
        )

    if atr_pct is not None and not atr_pct.empty and atr_weight > 0:
        market = cross_sectional(atr_pct, aggregation)
        components.append(
            (trailing_rank(market, window, min_periods=min_periods), atr_weight)
        )

    if not components:
        return pd.Series(dtype="float64", name="volatility_pressure")

    frame = pd.concat([ranked for ranked, _ in components], axis=1)
    weights = pd.Series([weight for _, weight in components], index=frame.columns)

    # Weight only the measures that are actually present at each timestamp,
    # so an early bar where ATR has not warmed up yet is scored on realised
    # volatility alone instead of collapsing to NaN.
    available = frame.notna()
    weighted = (frame.fillna(0.0) * weights).sum(axis=1)
    total = (available * weights).sum(axis=1)

    pressure = weighted / total.where(total > 0)
    pressure.name = "volatility_pressure"
    return pressure


def classify_volatility(
    realized: pd.DataFrame,
    atr_pct: pd.DataFrame | None = None,
    *,
    window: int = 252,
    min_periods: int | None = None,
    low_threshold: float = 1 / 3,
    high_threshold: float = 2 / 3,
    realized_weight: float = 0.5,
    atr_weight: float = 0.5,
    aggregation: str = "median",
) -> pd.Series:
    """Classify each timestamp as low / medium / high volatility.

    Parameters
    ----------
    low_threshold, high_threshold:
        Percentile cut points on the blended pressure score. The defaults
        split the trailing distribution into equal thirds.

    Returns
    -------
    pandas.Series
        Categorical over :data:`VOLATILITY_LABELS`.
    """
    pressure = volatility_pressure(
        realized,
        atr_pct,
        window=window,
        min_periods=min_periods,
        realized_weight=realized_weight,
        atr_weight=atr_weight,
        aggregation=aggregation,
    )
    if pressure.empty:
        return as_regime([], VOLATILITY_LABELS, index=pressure.index,
                         name="volatility_regime")

    labels = bucket(
        pressure, (low_threshold, high_threshold), VOLATILITY_REGIMES
    )
    return as_regime(
        labels, VOLATILITY_LABELS, index=pressure.index, name="volatility_regime"
    )


def asset_volatility_regime(
    realized: pd.DataFrame,
    atr_pct: pd.DataFrame | None = None,
    *,
    window: int = 252,
    min_periods: int | None = None,
    low_threshold: float = 1 / 3,
    high_threshold: float = 2 / 3,
    realized_weight: float = 0.5,
    atr_weight: float = 0.5,
) -> pd.DataFrame:
    """Per-asset volatility regime, ranked against each asset's own history.

    The market-level classification answers "is this a volatile market"; this
    answers "is this name volatile *for itself*", which is what a per-asset
    position sizer needs. Not consumed by the combined regime.

    Returns
    -------
    pandas.DataFrame
        ``timestamp x ticker`` of label strings.
    """
    if realized is None or realized.empty:
        return pd.DataFrame()

    total_weight = realized_weight + (
        atr_weight if atr_pct is not None and not atr_pct.empty else 0.0
    )
    ranked = trailing_rank(realized, window, min_periods=min_periods) * realized_weight

    if atr_pct is not None and not atr_pct.empty:
        atr_ranked = trailing_rank(atr_pct, window, min_periods=min_periods)
        ranked = ranked.add(atr_ranked * atr_weight, fill_value=0.0)

    pressure = ranked / total_weight

    labelled = {
        column: bucket(
            pressure[column], (low_threshold, high_threshold), VOLATILITY_REGIMES
        )
        for column in pressure.columns
    }
    return pd.DataFrame(labelled, index=pressure.index).fillna(UNKNOWN)
