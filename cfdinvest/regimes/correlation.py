"""Correlation regime: diversified / normal / breakdown.

Two distinct failure modes, and they are not the same thing:

1. **Broad correlation spike.** Everything starts moving together, so
   position count stops buying diversification. Preprocessing already
   measures this (``correlations["average"]``) and flags it
   (``correlations["breakdown"]``); this module consumes those rather than
   recomputing them.

2. **Equity-bond decoupling.** Bonds normally hedge equities -- the
   correlation is negative, which is the entire premise of a 60/40 book and
   of holding TLT against SPY. When it turns *positive*, both legs fall at
   once and the hedge is not merely weak but inverted. 2022 is the worked
   example: a broad correlation measure barely moved while the hedge that
   mattered stopped working.

The second is why this module does not just re-export the preprocessing
flags. All functions are pure and vectorised.
"""

from __future__ import annotations

from typing import Sequence

import numpy as np
import pandas as pd

from cfdinvest.regimes.labels import UNKNOWN, as_regime, bucket, with_unknown
from cfdinvest.schema import TICKER, TIMESTAMP

__all__ = [
    "CORRELATION_REGIMES",
    "CORRELATION_LABELS",
    "HEDGE_REGIMES",
    "HEDGE_LABELS",
    "DEFAULT_EQUITY_TICKERS",
    "DEFAULT_BOND_TICKERS",
    "cross_group_correlation",
    "classify_hedge",
    "classify_correlation",
]

#: Ordered label set, benign to dangerous.
CORRELATION_REGIMES: tuple[str, ...] = ("diversified", "normal", "breakdown")
CORRELATION_LABELS: tuple[str, ...] = with_unknown(CORRELATION_REGIMES)

#: Equity-bond relationship, from working hedge to inverted one.
HEDGE_REGIMES: tuple[str, ...] = ("hedging", "decoupled", "co_moving")
HEDGE_LABELS: tuple[str, ...] = with_unknown(HEDGE_REGIMES)

#: Conventional proxies. Only tickers actually present are used, so a lake
#: without them simply yields ``unknown`` for the hedge component.
DEFAULT_EQUITY_TICKERS: tuple[str, ...] = (
    "SPY", "QQQ", "IWM", "DIA", "EFA", "EEM", "EWJ", "EWG",
    "XLK", "XLF", "XLE", "XLV", "^GSPC",
)
DEFAULT_BOND_TICKERS: tuple[str, ...] = ("TLT", "IEF", "LQD", "HYG", "AGG", "BND")


def cross_group_correlation(
    rolling: pd.DataFrame,
    group_a: Sequence[str],
    group_b: Sequence[str],
) -> pd.Series:
    """Mean correlation between two groups of assets, per timestamp.

    Only the cross-group block of each matrix is averaged -- never the
    within-group pairs, which would drown the signal in the (always high)
    equity-equity correlations.

    Pure and vectorised: the rolling frame is reshaped to a
    ``(time, asset, asset)`` tensor once and sliced, rather than iterating
    matrices.

    Parameters
    ----------
    rolling:
        MultiIndexed rolling correlation from
        ``AnalysisFrame.correlations["rolling"]``.
    group_a, group_b:
        Ticker names. Members absent from the universe are ignored.

    Returns
    -------
    pandas.Series
        NaN where either group has no members present, or the window has not
        filled.
    """
    if rolling is None or rolling.empty:
        return pd.Series(dtype="float64", name="cross_group_correlation")

    assets = list(rolling.columns)
    positions = {asset: i for i, asset in enumerate(assets)}
    rows = [positions[a] for a in group_a if a in positions]
    cols = [positions[b] for b in group_b if b in positions]

    timestamps = pd.DatetimeIndex(
        rolling.index.get_level_values(TIMESTAMP).unique(), name=TIMESTAMP
    )
    if not rows or not cols:
        return pd.Series(
            np.nan, index=timestamps, name="cross_group_correlation", dtype="float64"
        )

    tensor = rolling.to_numpy(dtype="float64").reshape(
        len(timestamps), len(assets), len(assets)
    )
    block = tensor[np.ix_(np.arange(len(timestamps)), rows, cols)]

    # An asset appearing in both groups would contribute its own diagonal 1.0
    # and bias the average upward; mask those cells out.
    overlap = np.array(
        [[assets[r] == assets[c] for c in cols] for r in rows], dtype=bool
    )
    flat = block.reshape(len(timestamps), -1)
    mask = np.tile(~overlap.reshape(-1), (len(timestamps), 1))

    valid = np.isfinite(flat) & mask
    counts = valid.sum(axis=1)
    totals = np.where(valid, flat, 0.0).sum(axis=1)
    averages = np.divide(
        totals, counts, out=np.full(len(timestamps), np.nan), where=counts > 0
    )
    return pd.Series(averages, index=timestamps, name="cross_group_correlation")


def classify_hedge(
    cross_correlation: pd.Series,
    *,
    hedging_threshold: float = -0.1,
    co_moving_threshold: float = 0.2,
) -> pd.Series:
    """Label the equity-bond relationship.

    ``hedging``
        Correlation at or below ``hedging_threshold``: bonds move opposite
        equities, so the hedge works.
    ``decoupled``
        Around zero. Bonds neither help nor hurt.
    ``co_moving``
        Above ``co_moving_threshold``: both legs fall together and the hedge
        is inverted.

    Returns
    -------
    pandas.Series
        Categorical over :data:`HEDGE_LABELS`.
    """
    if hedging_threshold > co_moving_threshold:
        raise ValueError(
            f"hedging_threshold ({hedging_threshold}) must not exceed "
            f"co_moving_threshold ({co_moving_threshold})"
        )
    if cross_correlation.empty:
        return as_regime(
            [], HEDGE_LABELS, index=cross_correlation.index, name="hedge_regime"
        )

    labels = bucket(
        cross_correlation, (hedging_threshold, co_moving_threshold), HEDGE_REGIMES
    )
    return as_regime(
        labels, HEDGE_LABELS, index=cross_correlation.index, name="hedge_regime"
    )


def classify_correlation(
    average_correlation: pd.Series | None = None,
    breakdown_flags: pd.DataFrame | None = None,
    hedge_regime: pd.Series | None = None,
    *,
    diversified_threshold: float = 0.3,
    index: pd.Index | None = None,
) -> pd.Series:
    """Classify each timestamp's correlation structure.

    Precedence, most severe first:

    1. ``breakdown`` -- any preprocessing breakdown flag fires (``elevated``,
       ``spike`` or ``breakdown``), **or** the equity-bond hedge is inverted.
       The hedge condition is included because a broad average can look calm
       while the one relationship a book actually relies on has failed.
    2. ``diversified`` -- average pairwise correlation at or below
       ``diversified_threshold``.
    3. ``normal`` -- anything else with data.
    4. ``unknown`` -- no usable inputs at that timestamp.

    Parameters
    ----------
    average_correlation:
        From ``AnalysisFrame.correlations["average"]``.
    breakdown_flags:
        From ``AnalysisFrame.correlations["breakdown"]``.
    hedge_regime:
        Output of :func:`classify_hedge`. Optional.
    index:
        Result index when no other input supplies one.

    Returns
    -------
    pandas.Series
        Categorical over :data:`CORRELATION_LABELS`.
    """
    resolved = index
    for candidate in (average_correlation, breakdown_flags, hedge_regime):
        if candidate is not None and len(candidate):
            resolved = candidate.index
            break
    if resolved is None or len(resolved) == 0:
        return as_regime([], CORRELATION_LABELS, index=pd.Index([]),
                         name="correlation_regime")

    average = (
        pd.Series(np.nan, index=resolved)
        if average_correlation is None
        else average_correlation.reindex(resolved)
    )

    flagged = pd.Series(False, index=resolved)
    if breakdown_flags is not None and not breakdown_flags.empty:
        present = [
            column
            for column in ("elevated", "spike", "breakdown")
            if column in breakdown_flags.columns
        ]
        if present:
            flagged = (
                breakdown_flags.reindex(resolved)[present]
                .fillna(False)
                .astype(bool)
                .any(axis=1)
            )

    inverted = pd.Series(False, index=resolved)
    if hedge_regime is not None and len(hedge_regime):
        inverted = hedge_regime.reindex(resolved).astype("object").eq("co_moving")
        inverted = inverted.fillna(False).astype(bool)

    has_data = average.notna() | flagged | inverted
    labels = np.select(
        [
            ~has_data.to_numpy(),
            (flagged | inverted).to_numpy(),
            (average <= diversified_threshold).fillna(False).to_numpy(),
        ],
        [UNKNOWN, "breakdown", "diversified"],
        default="normal",
    )
    return as_regime(
        labels, CORRELATION_LABELS, index=resolved, name="correlation_regime"
    )
