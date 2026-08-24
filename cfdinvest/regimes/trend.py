"""Trend regime: uptrend / sideways / downtrend.

Two independent pieces of evidence per asset, deliberately kept separate:

* **Moving-average structure.** Price above a slow average, and a fast
  average above the slow one. Slow to react but hard to fool with noise.
* **Rolling return momentum.** The trailing total return, from
  ``AnalysisFrame.returns["rolling"]``. Faster, and noisier.

They are summed into a per-asset score in ``[-2, +2]``. Requiring agreement
for a directional call is what keeps a choppy market labelled ``sideways``
instead of flipping every few bars.

The market-level regime is then **breadth**: the share of the universe
trending up minus the share trending down. A rally carried by two names out
of fifty is not an uptrend, and breadth is what says so.

All functions are pure and vectorised.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from cfdinvest.regimes.labels import UNKNOWN, as_regime, bucket, with_unknown

__all__ = [
    "TREND_REGIMES",
    "TREND_LABELS",
    "moving_average_state",
    "momentum_state",
    "trend_score",
    "asset_trend_regime",
    "trend_breadth",
    "classify_trend",
]

#: Ordered label set, bearish to bullish.
TREND_REGIMES: tuple[str, ...] = ("downtrend", "sideways", "uptrend")

#: The complete category set, including the undecided case.
TREND_LABELS: tuple[str, ...] = with_unknown(TREND_REGIMES)


def moving_average_state(
    close: pd.DataFrame, fast: int = 50, slow: int = 200, *, min_periods: int | None = None
) -> pd.DataFrame:
    """Return ``+1`` / ``0`` / ``-1`` per asset from moving-average structure.

    ``+1`` requires *both* price above the slow average and the fast average
    above it; ``-1`` requires both below. Anything mixed is ``0``, which is
    the honest answer during a crossover rather than an arbitrary tie-break.

    Pure. NaN while either average is still warming up.

    Raises
    ------
    ValueError
        If ``fast`` is not shorter than ``slow``.
    """
    if fast >= slow:
        raise ValueError(f"fast window ({fast}) must be shorter than slow ({slow})")
    if close is None or close.empty:
        return pd.DataFrame()

    fast_ma = close.rolling(fast, min_periods=min_periods or fast).mean()
    slow_ma = close.rolling(slow, min_periods=min_periods or slow).mean()

    bullish = (close > slow_ma) & (fast_ma > slow_ma)
    bearish = (close < slow_ma) & (fast_ma < slow_ma)

    state = pd.DataFrame(
        np.select([bullish, bearish], [1.0, -1.0], default=0.0),
        index=close.index,
        columns=close.columns,
    )
    return state.where(slow_ma.notna() & fast_ma.notna() & close.notna())


def momentum_state(
    rolling_returns: pd.DataFrame, threshold: float = 0.02
) -> pd.DataFrame:
    """Return ``+1`` / ``0`` / ``-1`` per asset from trailing total return.

    The dead band around zero matters: without it, a return of +0.01% would
    register as momentum, and the market-level breadth would be dominated by
    rounding.

    Parameters
    ----------
    threshold:
        Absolute return outside which a direction is claimed, e.g. ``0.02``
        for 2% over the rolling-return window.
    """
    if rolling_returns is None or rolling_returns.empty:
        return pd.DataFrame()
    if threshold < 0:
        raise ValueError(f"threshold must be >= 0, got {threshold}")

    up = rolling_returns > threshold
    down = rolling_returns < -threshold

    state = pd.DataFrame(
        np.select([up, down], [1.0, -1.0], default=0.0),
        index=rolling_returns.index,
        columns=rolling_returns.columns,
    )
    return state.where(rolling_returns.notna())


def trend_score(
    close: pd.DataFrame,
    rolling_returns: pd.DataFrame,
    *,
    fast: int = 50,
    slow: int = 200,
    momentum_threshold: float = 0.02,
) -> pd.DataFrame:
    """Sum the two states into a per-asset score in ``[-2, +2]``.

    A score of ``+2`` means structure and momentum agree; ``+1`` means only
    one of them does. Keeping the sum (rather than collapsing early) is what
    lets the caller decide how much agreement to demand.
    """
    ma = moving_average_state(close, fast, slow)
    momentum = momentum_state(rolling_returns, momentum_threshold)

    if ma.empty and momentum.empty:
        return pd.DataFrame()
    if ma.empty:
        return momentum
    if momentum.empty:
        return ma

    ma, momentum = ma.align(momentum, join="inner")
    # add with fill_value: one warmed-up measure still scores while the other
    # is still filling its window.
    return ma.add(momentum, fill_value=0.0).where(ma.notna() | momentum.notna())


def asset_trend_regime(
    close: pd.DataFrame,
    rolling_returns: pd.DataFrame,
    *,
    fast: int = 50,
    slow: int = 200,
    momentum_threshold: float = 0.02,
    score_threshold: float = 1.0,
) -> pd.DataFrame:
    """Per-asset trend label.

    Parameters
    ----------
    score_threshold:
        Score at or beyond which a direction is claimed. The default of 1.0
        accepts one of the two measures; raise it to 2.0 to demand both.

    Returns
    -------
    pandas.DataFrame
        ``timestamp x ticker`` of label strings.
    """
    scores = trend_score(
        close,
        rolling_returns,
        fast=fast,
        slow=slow,
        momentum_threshold=momentum_threshold,
    )
    if scores.empty:
        return pd.DataFrame()

    # Scores are integers in {-2,-1,0,1,2} (a sum of two ternary states), so
    # the upper cut sits half a step below the threshold. That gives an exact
    # boundary -- score == score_threshold is an uptrend -- with no epsilon.
    labelled = {
        column: bucket(
            scores[column], (-score_threshold, score_threshold - 0.5), TREND_REGIMES
        )
        for column in scores.columns
    }
    return pd.DataFrame(labelled, index=scores.index).fillna(UNKNOWN)


def trend_breadth(asset_regimes: pd.DataFrame) -> pd.Series:
    """Share of the universe trending up minus the share trending down.

    Ranges ``[-1, +1]``. Assets whose label is ``unknown`` are excluded from
    the denominator, so a young universe is not diluted toward zero by names
    that have no verdict yet.
    """
    if asset_regimes is None or asset_regimes.empty:
        return pd.Series(dtype="float64", name="trend_breadth")

    known = asset_regimes.isin(TREND_REGIMES)
    counted = known.sum(axis=1)

    up = (asset_regimes == "uptrend").sum(axis=1)
    down = (asset_regimes == "downtrend").sum(axis=1)

    breadth = (up - down) / counted.where(counted > 0)
    breadth.name = "trend_breadth"
    return breadth


def classify_trend(
    close: pd.DataFrame,
    rolling_returns: pd.DataFrame,
    *,
    fast: int = 50,
    slow: int = 200,
    momentum_threshold: float = 0.02,
    score_threshold: float = 1.0,
    breadth_threshold: float = 0.2,
) -> pd.Series:
    """Classify each timestamp as uptrend / sideways / downtrend.

    Parameters
    ----------
    breadth_threshold:
        Net breadth that must be *strictly exceeded* for the market to be
        called directional. At the default of 0.2, 65% up versus 35% down
        (breadth 0.30) qualifies; an even 60/40 split (breadth 0.20) does
        not, and stays ``sideways``.

    Returns
    -------
    pandas.Series
        Categorical over :data:`TREND_LABELS`.
    """
    if breadth_threshold < 0:
        raise ValueError(f"breadth_threshold must be >= 0, got {breadth_threshold}")

    per_asset = asset_trend_regime(
        close,
        rolling_returns,
        fast=fast,
        slow=slow,
        momentum_threshold=momentum_threshold,
        score_threshold=score_threshold,
    )
    breadth = trend_breadth(per_asset)
    if breadth.empty:
        return as_regime([], TREND_LABELS, index=breadth.index, name="trend_regime")

    labels = bucket(
        breadth, (-breadth_threshold, breadth_threshold), TREND_REGIMES
    )
    return as_regime(labels, TREND_LABELS, index=breadth.index, name="trend_regime")
