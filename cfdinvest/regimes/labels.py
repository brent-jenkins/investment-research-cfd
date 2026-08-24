"""Shared labelling primitives for the regime classifiers.

Small internal module so the four component classifiers agree on how a label
set is declared, how missing data is spelled, and how a continuous score
becomes a discrete bucket. Everything here is pure.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

#: The label every classifier uses when it cannot decide. Never guessed at:
#: a regime built from absent data must say so rather than default to a
#: plausible-looking answer.
UNKNOWN = "unknown"


def with_unknown(labels: tuple[str, ...]) -> tuple[str, ...]:
    """Return a label set extended with :data:`UNKNOWN`, in order."""
    return (*labels, UNKNOWN)


def as_regime(
    values, categories: tuple[str, ...], index=None, name: str | None = None
) -> pd.Series:
    """Return a categorical regime Series with a fixed, ordered label set.

    Pinning the categories does real work: it guarantees the column can only
    ever hold declared labels, makes ``value_counts`` report zero-count
    regimes instead of silently omitting them, and keeps the combined
    lookup's key space finite and enumerable.

    Parameters
    ----------
    values:
        Array-like of label strings; NaN becomes :data:`UNKNOWN`.
    categories:
        The complete, ordered label set.
    index, name:
        Passed through to the resulting Series.
    """
    series = pd.Series(values, index=index, dtype="object")
    series = series.where(series.notna(), UNKNOWN)
    ordered = categories if UNKNOWN in categories else with_unknown(categories)
    return pd.Series(
        pd.Categorical(series, categories=ordered, ordered=True),
        index=series.index,
        name=name,
    )


def unknown_regime(
    index, categories: tuple[str, ...], name: str | None = None
) -> pd.Series:
    """Return an all-``unknown`` regime Series over ``index``.

    Used wherever a classifier's inputs are absent: the result must still be
    a full-length, correctly typed column so downstream alignment works, and
    it must say ``unknown`` rather than fabricate a label.
    """
    return as_regime([UNKNOWN] * len(index), categories, index=index, name=name)


def bucket(
    score: pd.Series,
    thresholds: tuple[float, ...],
    labels: tuple[str, ...],
) -> pd.Series:
    """Bucket a continuous score into ordered labels. Vectorised.

    ``labels`` must be one longer than ``thresholds``: value ``s`` takes
    ``labels[i]`` for the first ``i`` where ``s <= thresholds[i]``, else the
    final label. NaN yields :data:`UNKNOWN`.

    Parameters
    ----------
    score:
        Continuous values.
    thresholds:
        Ascending cut points.
    labels:
        Label per bucket, low to high.

    Raises
    ------
    ValueError
        If the lengths disagree or the thresholds are not ascending.

    Examples
    --------
    >>> s = pd.Series([0.1, 0.5, 0.9, float("nan")])
    >>> list(bucket(s, (0.33, 0.67), ("low", "medium", "high")))
    ['low', 'medium', 'high', 'unknown']
    """
    if len(labels) != len(thresholds) + 1:
        raise ValueError(
            f"expected {len(thresholds) + 1} labels for {len(thresholds)} "
            f"thresholds, got {len(labels)}"
        )
    if list(thresholds) != sorted(thresholds):
        raise ValueError(f"thresholds must ascend, got {thresholds}")

    values = score.to_numpy(dtype="float64")
    conditions = [values <= cut for cut in thresholds]
    conditions.append(np.ones_like(values, dtype=bool))

    chosen = np.select(conditions, list(labels), default=UNKNOWN)
    chosen = np.where(np.isnan(values), UNKNOWN, chosen)
    return pd.Series(chosen, index=score.index, dtype="object")


def trailing_rank(series: pd.Series, window: int, *, min_periods: int | None = None):
    """Percentile rank of each value within its own trailing window.

    The bucketing backbone: it adapts to a series' own history, so "high
    volatility" means high *for this market*, and it is strictly trailing --
    a full-sample quantile would rank today against data from the future and
    quietly leak it into every regime label.

    Returns
    -------
    pandas.Series
        Values in ``(0, 1]``; NaN until ``min_periods`` observations exist.
    """
    if window < 2:
        raise ValueError(f"window must be >= 2, got {window}")
    effective = max(window // 4, 2) if min_periods is None else min_periods
    return series.rolling(window, min_periods=effective).rank(pct=True)


def cross_sectional(panel: pd.DataFrame, how: str = "median") -> pd.Series:
    """Collapse a ``timestamp x ticker`` panel to one market-level series.

    The median is the default because a single blown-up asset should not
    define the market's regime; use ``"mean"`` when every name should count
    equally.

    Raises
    ------
    ValueError
        On an unknown aggregation.
    """
    if panel is None or panel.empty:
        return pd.Series(dtype="float64")
    if how == "median":
        return panel.median(axis=1, skipna=True)
    if how == "mean":
        return panel.mean(axis=1, skipna=True)
    raise ValueError(f"unknown aggregation {how!r}; expected 'median' or 'mean'")
