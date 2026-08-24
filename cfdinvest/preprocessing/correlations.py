"""Rolling correlation structure and diversification-breakdown flags.

A multi-asset CFD book's real risk is not the sum of its positions' risks --
it is what happens when the correlations that justified the diversification
stop holding. This module measures that structure and flags when it shifts.

The flags here are **diagnostics, not signals**: they describe the state of
the correlation matrix, and take no view on what to trade. Every function is
pure.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from cfdinvest.schema import TICKER, TIMESTAMP

__all__ = [
    "rolling_correlation",
    "correlation_matrix",
    "correlation_tensor",
    "average_pairwise_correlation",
    "pairwise_correlations",
    "correlation_breakdown_flags",
]

#: Columns of the breakdown-flag frame, in order.
BREAKDOWN_COLUMNS: tuple[str, ...] = (
    "average_correlation",
    "baseline",
    "dispersion",
    "zscore",
    "change",
    "elevated",
    "spike",
    "breakdown",
)


def rolling_correlation(
    returns: pd.DataFrame,
    window: int = 63,
    *,
    min_periods: int | None = None,
) -> pd.DataFrame:
    """Rolling pairwise correlation matrix for the whole universe.

    Parameters
    ----------
    returns:
        ``timestamp x ticker`` return panel.
    window:
        Lookback in bars. The default of 63 is roughly one quarter of daily
        bars -- long enough to be stable, short enough to move when regimes do.
    min_periods:
        Defaults to ``window``.

    Returns
    -------
    pandas.DataFrame
        MultiIndexed ``(timestamp, ticker) x ticker``: one full matrix per
        timestamp, which is what pandas' rolling ``corr`` produces natively.
        Use :func:`correlation_tensor` for a 3-D numpy view.

    Raises
    ------
    ValueError
        If ``window`` is below 2.
    """
    if window < 2:
        raise ValueError(f"window must be >= 2 for a correlation, got {window}")
    if returns.empty:
        return pd.DataFrame()

    result = returns.rolling(
        window, min_periods=window if min_periods is None else min_periods
    ).corr()
    result.index.names = [TIMESTAMP, TICKER]
    return result


def correlation_matrix(
    returns: pd.DataFrame,
    *,
    min_periods: int | None = None,
) -> pd.DataFrame:
    """Return a single correlation matrix over the whole sample.

    Pairwise-complete: each pair uses the bars where both assets traded, so
    one asset's short history does not truncate the entire matrix.
    """
    if returns.empty:
        return pd.DataFrame()
    matrix = returns.corr(min_periods=min_periods)
    matrix.index.name = TICKER
    matrix.columns.name = TICKER
    return matrix


def correlation_tensor(rolling: pd.DataFrame) -> tuple[np.ndarray, pd.DatetimeIndex]:
    """Reshape a rolling-correlation frame into ``(time, asset, asset)``.

    Pure. pandas emits the rolling matrices stacked vertically in a stable
    ``timestamp``-major order, so a single reshape recovers the tensor with
    no copying of individual matrices -- which is what keeps the aggregations
    below loop-free over a 50-asset, 20-year panel.

    Returns
    -------
    tuple
        ``(tensor, timestamps)``.
    """
    if rolling.empty:
        return np.empty((0, 0, 0)), pd.DatetimeIndex([], tz="UTC", name=TIMESTAMP)

    timestamps = pd.DatetimeIndex(
        rolling.index.get_level_values(TIMESTAMP).unique(), name=TIMESTAMP
    )
    n_assets = rolling.shape[1]
    tensor = rolling.to_numpy(dtype="float64").reshape(
        len(timestamps), n_assets, n_assets
    )
    return tensor, timestamps


def average_pairwise_correlation(rolling: pd.DataFrame) -> pd.Series:
    """Mean off-diagonal correlation at each timestamp.

    The single number that says how much diversification a book actually
    has. It rises toward 1 in a crisis, which is precisely when a portfolio
    that looked diversified stops behaving that way.

    Pure and vectorised via :func:`correlation_tensor`.

    Returns
    -------
    pandas.Series
        Indexed by timestamp; NaN where the window was not yet full.
    """
    if rolling.empty:
        return pd.Series(dtype="float64", name="average_correlation")

    tensor, timestamps = correlation_tensor(rolling)
    n_assets = tensor.shape[1]
    if n_assets < 2:
        return pd.Series(
            np.nan, index=timestamps, name="average_correlation", dtype="float64"
        )

    off_diagonal = ~np.eye(n_assets, dtype=bool)
    flat = tensor[:, off_diagonal]

    # Hand-rolled nan-mean rather than np.nanmean: a timestamp whose window is
    # not yet full is entirely NaN, and nanmean warns "Mean of empty slice" on
    # every such row. The count-and-divide form yields the same NaN silently,
    # and still lets a partially observed timestamp contribute its valid pairs.
    valid = np.isfinite(flat)
    counts = valid.sum(axis=1)
    totals = np.where(valid, flat, 0.0).sum(axis=1)
    averages = np.divide(
        totals,
        counts,
        out=np.full(len(timestamps), np.nan),
        where=counts > 0,
    )
    return pd.Series(averages, index=timestamps, name="average_correlation")


def pairwise_correlations(rolling: pd.DataFrame) -> pd.DataFrame:
    """Flatten rolling correlations to ``timestamp x "A|B"`` columns.

    One column per unordered pair, using the upper triangle only, so the
    result carries each relationship once rather than twice.

    Returns
    -------
    pandas.DataFrame
        Columns named ``"{asset_a}|{asset_b}"``, sorted.
    """
    if rolling.empty:
        return pd.DataFrame()

    tensor, timestamps = correlation_tensor(rolling)
    assets = list(rolling.columns)
    rows, cols = np.triu_indices(len(assets), k=1)
    if rows.size == 0:
        return pd.DataFrame(index=timestamps)

    data = tensor[:, rows, cols]
    # Name each pair with its members in sorted order, so a column label
    # identifies the relationship rather than the order the assets happened
    # to arrive in. Without this, the same pair is "A|B" or "B|A" depending
    # on the caller's column ordering.
    names = ["|".join(sorted((assets[i], assets[j]))) for i, j in zip(rows, cols)]
    return pd.DataFrame(data, index=timestamps, columns=names).sort_index(axis=1)


def correlation_breakdown_flags(
    average_correlation: pd.Series,
    *,
    baseline_window: int = 252,
    min_periods: int | None = None,
    zscore_threshold: float = 2.0,
    level_threshold: float = 0.8,
    change_window: int = 21,
    change_threshold: float = 0.25,
) -> pd.DataFrame:
    """Flag periods where diversification is breaking down.

    Pure. Three independent conditions, reported separately so a caller can
    see *why* a bar was flagged rather than trusting one opaque boolean:

    ``elevated``
        Average correlation is more than ``zscore_threshold`` standard
        deviations above its own trailing baseline. Relative to the asset
        mix's own history, so it adapts to a book that is always fairly
        correlated.
    ``spike``
        Average correlation exceeds ``level_threshold`` outright. An absolute
        backstop for when the baseline itself has drifted up.
    ``breakdown``
        Correlation rose by more than ``change_threshold`` over
        ``change_window`` bars. Catches the *transition*, which usually
        precedes the level alarm.

    The baseline is computed on data strictly *before* each bar (the rolling
    window is shifted by one), so a flag never uses the bar it is flagging.

    Parameters
    ----------
    average_correlation:
        Output of :func:`average_pairwise_correlation`.

    Returns
    -------
    pandas.DataFrame
        Columns :data:`BREAKDOWN_COLUMNS`; boolean columns are real ``bool``,
        never object dtype, so they can index a frame directly.
    """
    if average_correlation.empty:
        return pd.DataFrame(columns=list(BREAKDOWN_COLUMNS))

    effective = (
        max(baseline_window // 4, 2) if min_periods is None else min_periods
    )
    # shift(1): the baseline must not include the observation being judged,
    # or an extreme bar partly explains away its own extremity.
    history = average_correlation.shift(1).rolling(
        baseline_window, min_periods=effective
    )
    baseline = history.mean()
    dispersion = history.std(ddof=1)

    zscore = (average_correlation - baseline) / dispersion.replace(0.0, np.nan)
    change = average_correlation - average_correlation.shift(change_window)

    elevated = (zscore > zscore_threshold).fillna(False)
    spike = (average_correlation > level_threshold).fillna(False)
    breakdown = (change > change_threshold).fillna(False)

    return pd.DataFrame(
        {
            "average_correlation": average_correlation,
            "baseline": baseline,
            "dispersion": dispersion,
            "zscore": zscore,
            "change": change,
            "elevated": elevated.astype(bool),
            "spike": spike.astype(bool),
            "breakdown": breakdown.astype(bool),
        }
    )[list(BREAKDOWN_COLUMNS)]
