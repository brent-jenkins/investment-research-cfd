"""Per-asset trend signals: crossovers, breakouts, and a strength score.

Where :mod:`cfdinvest.regimes.trend` answers "what kind of market is this?"
at the index level, this module answers "what is *this asset* doing?" —
one column per ticker, because a signal engine trades names, not the market.

Three independent readings, deliberately kept separate before they are
blended:

* **Moving-average crossover.** Fast average above slow. The classic
  state variable: slow to turn, and unambiguous once it has.
* **Breakout / breakdown.** Price clearing the highest high (or lowest low)
  of the trailing window. Catches a move the averages have not caught up
  with yet.
* **Momentum.** The trailing total return, scaled so a configurable move
  counts as full conviction.

They are averaged into a signed **strength** in ``[-1, +1]``. Keeping the
components separate until the last step is what lets ``confidence``
downstream report *how much* the readings agreed, rather than only how big
the blend was.

All functions are pure and vectorised. No look-ahead: every rolling window is
trailing, and the breakout bands exclude the bar being tested.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = [
    "moving_average_crossover",
    "crossover_events",
    "donchian_bounds",
    "breakout_state",
    "momentum_component",
    "trend_components",
    "trend_strength",
    "component_agreement",
]


def moving_average_crossover(
    close: pd.DataFrame,
    fast: int = 20,
    slow: int = 100,
    *,
    min_periods: int | None = None,
) -> pd.DataFrame:
    """Return ``+1`` where the fast average is above the slow one, else ``-1``.

    Unlike the regime layer's moving-average state, this does not also demand
    that price sits on the right side of the slow average: a crossover is a
    statement about the averages alone, and the extra condition belongs to
    the regime reading rather than the entry rule.

    Parameters
    ----------
    close:
        ``timestamp x ticker`` price panel.
    fast, slow:
        Window lengths in bars.

    Returns
    -------
    pandas.DataFrame
        ``+1`` / ``-1``, NaN while either average is warming up. Exact
        equality yields ``0``, which is vanishingly rare on real prices but
        must not silently become a long.

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

    state = pd.DataFrame(
        np.select([fast_ma > slow_ma, fast_ma < slow_ma], [1.0, -1.0], default=0.0),
        index=close.index,
        columns=close.columns,
    )
    return state.where(fast_ma.notna() & slow_ma.notna())


def crossover_events(state: pd.DataFrame) -> pd.DataFrame:
    """Return ``+1`` on the bar a golden cross occurs, ``-1`` on a death cross.

    Pure. Everything else is ``0``. Useful for entry-timing rules that want
    the transition rather than the standing state; the combined signal uses
    the state, so this is exposed for callers who want the event.
    """
    if state is None or state.empty:
        return pd.DataFrame()

    previous = state.shift(1)
    changed = state.notna() & previous.notna() & (state != previous)
    return (state * changed.astype("float64")).fillna(0.0)


def donchian_bounds(
    high: pd.DataFrame,
    low: pd.DataFrame,
    window: int = 55,
    *,
    min_periods: int | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return the trailing highest high and lowest low, excluding today.

    The ``shift(1)`` is the whole point: without it, today's own high is part
    of the band it is being compared against, so price can never exceed it
    and no breakout ever fires. This is the classic off-by-one in a Donchian
    implementation.

    Returns
    -------
    tuple
        ``(upper, lower)`` panels.
    """
    if window < 2:
        raise ValueError(f"window must be >= 2, got {window}")
    if high is None or low is None or high.empty or low.empty:
        return pd.DataFrame(), pd.DataFrame()

    effective = min_periods or window
    upper = high.rolling(window, min_periods=effective).max().shift(1)
    lower = low.rolling(window, min_periods=effective).min().shift(1)
    return upper, lower


def breakout_state(
    close: pd.DataFrame,
    high: pd.DataFrame | None = None,
    low: pd.DataFrame | None = None,
    window: int = 55,
    *,
    min_periods: int | None = None,
) -> pd.DataFrame:
    """Return ``+1`` on a breakout above the band, ``-1`` on a breakdown below.

    Falls back to using ``close`` for the bands when highs and lows are
    unavailable, which makes the signal usable on a close-only lake at the
    cost of missing intrabar breaks.

    Returns
    -------
    pandas.DataFrame
        ``+1`` / ``0`` / ``-1``; NaN while the band is warming up.
    """
    if close is None or close.empty:
        return pd.DataFrame()

    upper_source = high if high is not None and not high.empty else close
    lower_source = low if low is not None and not low.empty else close

    upper, lower = donchian_bounds(
        upper_source, lower_source, window, min_periods=min_periods
    )
    if upper.empty:
        return pd.DataFrame()

    upper = upper.reindex_like(close)
    lower = lower.reindex_like(close)

    state = pd.DataFrame(
        np.select([close > upper, close < lower], [1.0, -1.0], default=0.0),
        index=close.index,
        columns=close.columns,
    )
    return state.where(upper.notna() & lower.notna() & close.notna())


def momentum_component(
    rolling_returns: pd.DataFrame, full_scale: float = 0.10
) -> pd.DataFrame:
    """Scale trailing returns into ``[-1, +1]``.

    Parameters
    ----------
    full_scale:
        The return magnitude that counts as full conviction. At the default,
        a 10% move over the rolling-return window saturates the component,
        and anything beyond adds nothing — which stops one violent name from
        dominating a blended score.

    Returns
    -------
    pandas.DataFrame
        Clipped to ``[-1, +1]``; NaN preserved.
    """
    if rolling_returns is None or rolling_returns.empty:
        return pd.DataFrame()
    if full_scale <= 0:
        raise ValueError(f"full_scale must be > 0, got {full_scale}")

    return (rolling_returns / full_scale).clip(-1.0, 1.0)


def trend_components(
    close: pd.DataFrame,
    rolling_returns: pd.DataFrame | None = None,
    high: pd.DataFrame | None = None,
    low: pd.DataFrame | None = None,
    *,
    fast: int = 20,
    slow: int = 100,
    breakout_window: int = 55,
    momentum_full_scale: float = 0.10,
) -> dict[str, pd.DataFrame]:
    """Compute the three trend components, each in ``[-1, +1]``.

    Returns
    -------
    dict
        ``{"crossover", "breakout", "momentum"}``. A component is omitted
        when its inputs are unavailable, so a close-only lake still yields
        crossover and breakout readings.
    """
    components: dict[str, pd.DataFrame] = {}
    if close is None or close.empty:
        return components

    crossover = moving_average_crossover(close, fast, slow)
    if not crossover.empty:
        components["crossover"] = crossover

    breakout = breakout_state(close, high, low, breakout_window)
    if not breakout.empty:
        components["breakout"] = breakout

    if rolling_returns is not None and not rolling_returns.empty:
        momentum = momentum_component(rolling_returns, momentum_full_scale)
        components["momentum"] = momentum.reindex_like(close)

    return components


def trend_strength(
    components: dict[str, pd.DataFrame],
    weights: dict[str, float] | None = None,
) -> pd.DataFrame:
    """Blend the components into a signed strength in ``[-1, +1]``.

    Only the components that are actually present at each cell contribute,
    with the weights renormalised over them. A name whose momentum has not
    warmed up is therefore scored on its crossover and breakout rather than
    dropping to NaN and losing the signal entirely.

    Parameters
    ----------
    components:
        Output of :func:`trend_components`.
    weights:
        Per-component weight. Missing entries default to 1.0.

    Returns
    -------
    pandas.DataFrame
        NaN only where *no* component has a reading.
    """
    if not components:
        return pd.DataFrame()

    resolved = weights or {}
    weighted_sum: pd.DataFrame | None = None
    weight_total: pd.DataFrame | None = None

    for name, panel in components.items():
        weight = float(resolved.get(name, 1.0))
        if weight == 0:
            continue
        present = panel.notna()
        contribution = panel.fillna(0.0) * weight
        weights_here = present.astype("float64") * weight

        weighted_sum = contribution if weighted_sum is None else weighted_sum.add(
            contribution, fill_value=0.0
        )
        weight_total = weights_here if weight_total is None else weight_total.add(
            weights_here, fill_value=0.0
        )

    if weighted_sum is None or weight_total is None:
        return pd.DataFrame()

    strength = weighted_sum / weight_total.where(weight_total > 0)
    return strength.clip(-1.0, 1.0)


def component_agreement(components: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Fraction of readings that agree on direction, in ``[0, 1]``.

    Three components all pointing long score ``1.0``; two long and one short
    score ``2/3``. Components reading exactly zero are treated as abstaining
    and excluded from both the numerator and the denominator, so a flat
    breakout does not dilute a genuine agreement between the other two.

    This is what makes ``confidence`` mean something other than "the average
    was large": a strength of 0.5 from unanimous mild readings is a different
    proposition from 0.5 with one component pulling the other way.

    Returns
    -------
    pandas.DataFrame
        NaN where nothing has an opinion.
    """
    if not components:
        return pd.DataFrame()

    signs = [np.sign(panel) for panel in components.values()]
    stacked_long = None
    stacked_short = None

    for sign in signs:
        is_long = (sign > 0).astype("float64").where(sign.notna())
        is_short = (sign < 0).astype("float64").where(sign.notna())
        stacked_long = is_long if stacked_long is None else stacked_long.add(
            is_long, fill_value=0.0
        )
        stacked_short = is_short if stacked_short is None else stacked_short.add(
            is_short, fill_value=0.0
        )

    voting = stacked_long + stacked_short
    majority = np.maximum(stacked_long, stacked_short)
    return majority / voting.where(voting > 0)
