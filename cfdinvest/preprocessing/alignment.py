"""Timestamp alignment and publication-aware macro forward-fill.

Two jobs, one concern -- putting every series onto a single trading clock
without letting information travel backwards in time:

1. **Market alignment.** Resample OHLCV to a unified bar frequency and
   reindex every asset onto one calendar. Missing sessions stay missing;
   nothing is invented.
2. **Macro alignment.** Forward-fill each macro series onto that calendar
   using its ``publication_date``, never its ``observation_date``. A value
   enters the feature set on the day it became public, not the day it
   describes.

The second point is the whole reason this module exists. April 2020
unemployment (14.7%) describes a period ending 2020-04-30 but was not
published until 2020-05-08. A naive ``.reindex(...).ffill()`` on the
observation date hands a backtest that number a week early -- a strategy
that "predicts" the COVID crash. Every function here is pure.
"""

from __future__ import annotations

import logging
from typing import Mapping

import numpy as np
import pandas as pd

from cfdinvest.schema import (
    MACRO_VALUE,
    OHLCV_COLUMNS,
    PUBLICATION_DATE,
    SERIES_ID,
    TICKER,
    TIMESTAMP,
)

logger = logging.getLogger(__name__)

#: How each OHLCV field is aggregated when resampling to a coarser bar.
OHLCV_AGGREGATION: dict[str, str] = {
    "open": "first",
    "high": "max",
    "low": "min",
    "close": "last",
    "adj_close": "last",
    "volume": "sum",
    "dividends": "sum",
    "stock_splits": "prod",
}

#: Bars per year, used to annualise volatility. Daily uses the conventional
#: 252 trading days; hourly assumes a 6.5-hour US equity session.
PERIODS_PER_YEAR: dict[str, int] = {
    "1h": 252 * 7,
    "1d": 252,
    "1wk": 52,
    "1mo": 12,
}


class AlignmentError(ValueError):
    """Raised when data cannot be aligned without inventing information."""


# --------------------------------------------------------------------------- #
# Frequency helpers
# --------------------------------------------------------------------------- #
def infer_bar_interval(index: pd.DatetimeIndex) -> str:
    """Return the engine's interval label that best describes ``index``.

    Uses the *median* spacing rather than ``pandas.infer_freq``, which
    returns ``None`` for any real trading calendar (weekends and holidays
    make the spacing irregular by construction).

    >>> idx = pd.date_range("2024-01-01", periods=5, freq="D", tz="UTC")
    >>> infer_bar_interval(idx)
    '1d'
    """
    if len(index) < 2:
        return "1d"
    spacing = pd.Series(index).diff().dropna().median()
    if spacing <= pd.Timedelta(hours=2):
        return "1h"
    if spacing <= pd.Timedelta(days=3):
        return "1d"
    if spacing <= pd.Timedelta(days=10):
        return "1wk"
    return "1mo"


def periods_per_year(index_or_interval: pd.DatetimeIndex | str) -> int:
    """Return the annualisation factor for an index or an interval label.

    >>> periods_per_year("1d")
    252
    """
    interval = (
        index_or_interval
        if isinstance(index_or_interval, str)
        else infer_bar_interval(index_or_interval)
    )
    return PERIODS_PER_YEAR.get(interval, 252)


# --------------------------------------------------------------------------- #
# Market alignment
# --------------------------------------------------------------------------- #
def to_panels(tidy: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Split a tidy ``(timestamp, ticker)`` frame into one panel per field.

    Pure. Each panel is ``timestamp x ticker``, sorted on both axes so that
    downstream rolling maths is deterministic.

    Returns
    -------
    dict
        ``{field: DataFrame}`` for every OHLCV field present.
    """
    if tidy.empty:
        return {}

    fields = [c for c in tidy.columns if c != TICKER]
    panels: dict[str, pd.DataFrame] = {}
    for field in fields:
        panel = tidy[field].unstack(TICKER)
        panel.columns.name = TICKER
        panel.index.name = TIMESTAMP
        panels[field] = panel.sort_index().sort_index(axis=1)
    return panels


def resample_panels(
    panels: Mapping[str, pd.DataFrame],
    rule: str,
    *,
    label: str = "left",
    closed: str = "left",
) -> dict[str, pd.DataFrame]:
    """Resample OHLCV panels to a coarser bar, field by field.

    Pure. Each field uses its own aggregation (open first, high max, low
    min, close last, volume sum) so the resulting bar is a genuine bar and
    not a column-wise average.

    Buckets containing no observations are dropped rather than emitted as
    zero-volume phantom bars -- a market that was shut has no bar.

    Parameters
    ----------
    panels:
        ``{field: timestamp x ticker}`` as produced by :func:`to_panels`.
    rule:
        Any pandas offset alias, e.g. ``"W-FRI"``, ``"ME"``, ``"4h"``.
    label, closed:
        Bucket labelling convention. The default stamps a bar with the
        instant it *opened*, which is what a backtest may act on.

    Returns
    -------
    dict
        Resampled panels, aligned to a common index.
    """
    if not panels:
        return {}

    resampled: dict[str, pd.DataFrame] = {}
    for field, panel in panels.items():
        how = OHLCV_AGGREGATION.get(field, "last")
        grouped = panel.resample(rule, label=label, closed=closed)
        if how == "sum":
            # min_count=1 keeps an empty bucket NaN instead of turning it
            # into a spurious zero-volume bar.
            resampled[field] = grouped.sum(min_count=1)
        elif how == "prod":
            resampled[field] = grouped.prod(min_count=1)
        else:
            resampled[field] = getattr(grouped, how)()

    reference = resampled.get("close")
    if reference is not None:
        keep = reference.notna().any(axis=1)
        resampled = {field: panel[keep] for field, panel in resampled.items()}
    return resampled


def trading_calendar(
    panels: Mapping[str, pd.DataFrame],
    *,
    field: str = "close",
) -> pd.DatetimeIndex:
    """Return the master calendar: every timestamp with at least one quote.

    Pure. The market defines the clock; macro data is later conformed to it
    rather than the other way round, because a backtest can only act when a
    market is open.
    """
    panel = panels.get(field)
    if panel is None or panel.empty:
        return pd.DatetimeIndex([], tz="UTC", name=TIMESTAMP)
    index = panel.index[panel.notna().any(axis=1)]
    return pd.DatetimeIndex(index, name=TIMESTAMP).sort_values()


def align_panels(
    panels: Mapping[str, pd.DataFrame],
    calendar: pd.DatetimeIndex,
) -> dict[str, pd.DataFrame]:
    """Reindex every panel onto ``calendar`` **without filling**.

    Pure. A ticker that did not trade on a session in the calendar (a local
    holiday, a later listing date) gets NaN, not a carried-forward price.
    Deciding what to do about that gap is a modelling choice, and this layer
    does not make it silently.
    """
    return {
        field: panel.reindex(calendar).sort_index(axis=1)
        for field, panel in panels.items()
    }


def align_market(
    tidy: pd.DataFrame,
    *,
    rule: str | None = None,
    calendar: pd.DatetimeIndex | None = None,
) -> tuple[dict[str, pd.DataFrame], pd.DatetimeIndex]:
    """Take a tidy market frame to aligned panels on one calendar.

    Pure. Composes :func:`to_panels`, :func:`resample_panels` (when ``rule``
    is given) and :func:`align_panels`.

    Returns
    -------
    tuple
        ``(panels, calendar)``.
    """
    panels = to_panels(tidy)
    if rule:
        panels = resample_panels(panels, rule)
    resolved = trading_calendar(panels) if calendar is None else calendar
    return align_panels(panels, resolved), resolved


# --------------------------------------------------------------------------- #
# Publication-aware macro alignment
# --------------------------------------------------------------------------- #
def _as_of_values(
    observation: np.ndarray,
    values: np.ndarray,
    published: np.ndarray,
    target: np.ndarray,
) -> np.ndarray:
    """Return, for each ``target`` instant, the latest value public by then.

    Pure and fully vectorised -- no Python loop over timestamps, so a
    30-year daily calendar costs one sort and one binary search.

    The algorithm:

    1. Order observations by **publication** date, because that is the order
       a trader learns them in.
    2. Walk that order keeping the index of the largest *observation* date
       seen so far. Publication order and observation order normally agree,
       but this makes the result correct even when a late revision of an old
       period arrives after a newer period's first print.
    3. Binary-search each target instant into the publication timeline; the
       running-best index at that point is the answer.
    """
    if observation.size == 0:
        return np.full(target.size, np.nan)

    order = np.argsort(published, kind="stable")
    observation = observation[order]
    values = values[order]
    published = published[order]

    positions = np.arange(observation.size)
    running_max = np.maximum.accumulate(observation)
    # Where an element attains the running maximum, remember its position;
    # accumulating the maximum of those positions carries the winner forward.
    attains = np.where(observation == running_max, positions, -1)
    best = np.maximum.accumulate(attains)

    known = np.searchsorted(published, target, side="right")
    result = np.full(target.size, np.nan)
    seen = known > 0
    if seen.any():
        result[seen] = values[best[known[seen] - 1]]
    return result


def diagnose_publication_dates(macro_long: pd.DataFrame) -> dict[str, dict[str, bool]]:
    """Flag macro series whose publication dates cannot support a backtest.

    Pure. A series ingested in ``latest`` mode has every row stamped with the
    *ingest* date, so nothing is ever "published" during the backtest window
    and publication-aware filling correctly yields an all-NaN column. That is
    the right answer but a baffling one to debug, so it is named here instead.

    Returns
    -------
    dict
        ``{series_id: {"constant_publication_date": bool,
                       "published_after_last_observation": bool,
                       "usable": bool}}``
    """
    if macro_long.empty or PUBLICATION_DATE not in macro_long.columns:
        return {}

    report: dict[str, dict[str, bool]] = {}
    for series_id, block in macro_long.groupby(SERIES_ID, observed=True):
        published = block[PUBLICATION_DATE].dropna()
        if published.empty:
            report[str(series_id)] = {
                "constant_publication_date": False,
                "published_after_last_observation": False,
                "usable": False,
            }
            continue

        constant = bool(published.nunique() == 1)
        after_end = bool(published.min() > block.index.max())
        report[str(series_id)] = {
            "constant_publication_date": constant,
            "published_after_last_observation": after_end,
            "usable": not (constant and after_end),
        }
    return report


def resolve_publication_dates(
    macro_long: pd.DataFrame,
    *,
    missing: str = "error",
    fallback_lag_days: int = 1,
) -> pd.Series:
    """Return the publication date for every macro observation.

    Backward compatibility: files written before publication dates were
    stored have no such column. Rather than silently substituting the
    observation date -- which would reintroduce exactly the look-ahead this
    layer exists to prevent -- the caller must choose.

    Parameters
    ----------
    missing:
        ``"error"`` (default) raises when dates are absent.
        ``"lag"`` derives ``observation_date + fallback_lag_days`` and warns.

    Raises
    ------
    AlignmentError
        When dates are missing and ``missing="error"``.
    """
    has_column = PUBLICATION_DATE in macro_long.columns
    published = (
        macro_long[PUBLICATION_DATE]
        if has_column
        else pd.Series(pd.NaT, index=macro_long.index)
    )

    if not has_column or published.isna().all():
        if missing != "lag":
            raise AlignmentError(
                "Macro data carries no publication_date, so it cannot be "
                "forward-filled without look-ahead. Re-ingest with "
                "vintage_mode='publication_lag', or pass "
                "missing_publication='lag' to derive dates from a fixed "
                "release lag and accept the approximation."
            )
        logger.warning(
            "No publication dates available; deriving observation_date + %d "
            "day(s). These are ASSUMED, not vendor-reported.",
            fallback_lag_days,
        )
        return pd.Series(
            macro_long.index + pd.Timedelta(days=fallback_lag_days),
            index=macro_long.index,
        )

    gaps = int(published.isna().sum())
    if gaps:
        logger.warning(
            "%d macro observation(s) have no publication date and will never "
            "become available.",
            gaps,
        )
    return published


def publication_aware_ffill(
    macro_long: pd.DataFrame,
    calendar: pd.DatetimeIndex,
    *,
    missing_publication: str = "error",
    fallback_lag_days: int = 1,
    skip_missing_values: bool = True,
) -> pd.DataFrame:
    """Forward-fill macro series onto ``calendar`` using publication dates.

    Pure. This is the layer's central guarantee: cell ``[t, s]`` holds the
    most recent observation of series ``s`` that had been **published** on or
    before ``t``. Before a series' first release the column is NaN, because
    nothing was known yet.

    Parameters
    ----------
    macro_long:
        Stacked macro frame as returned by ``DataRepository.load_macro(
        wide=False)`` -- index ``timestamp``, columns ``value``,
        ``publication_date``, ``series_id``.
    calendar:
        Target index, normally the market's trading calendar.
    missing_publication, fallback_lag_days:
        See :func:`resolve_publication_dates`.
    skip_missing_values:
        Exclude NaN observations from the fill chain (default). A holiday
        with no print carries no information, so the previous real value is
        carried instead of a hole.

    Returns
    -------
    pandas.DataFrame
        ``calendar x series_id``, columns sorted, no look-ahead.
    """
    empty = pd.DataFrame(index=pd.DatetimeIndex(calendar, name=TIMESTAMP))
    if macro_long.empty or len(calendar) == 0:
        return empty

    published = resolve_publication_dates(
        macro_long,
        missing=missing_publication,
        fallback_lag_days=fallback_lag_days,
    )
    frame = macro_long.assign(**{PUBLICATION_DATE: published})

    target = np.asarray(pd.DatetimeIndex(calendar), dtype="datetime64[ns]")
    columns: dict[str, np.ndarray] = {}

    for series_id, block in frame.groupby(SERIES_ID, observed=True):
        usable = block[block[PUBLICATION_DATE].notna()]
        if skip_missing_values:
            usable = usable[usable[MACRO_VALUE].notna()]

        columns[str(series_id)] = _as_of_values(
            np.asarray(usable.index, dtype="datetime64[ns]"),
            usable[MACRO_VALUE].to_numpy(dtype="float64"),
            np.asarray(
                pd.DatetimeIndex(usable[PUBLICATION_DATE]), dtype="datetime64[ns]"
            ),
            target,
        )

    aligned = pd.DataFrame(columns, index=pd.DatetimeIndex(calendar, name=TIMESTAMP))
    aligned.columns.name = SERIES_ID
    return aligned.sort_index(axis=1)


def value_available_at(
    values: pd.Series,
    published: pd.Series,
    as_of: str | pd.Timestamp,
    *,
    skip_missing: bool = True,
) -> float | None:
    """Return the latest value of one series public on or before ``as_of``.

    Pure, and the scalar form of :func:`publication_aware_ffill` -- useful in
    tests, notebooks and assertions where a single point-in-time lookup is
    clearer than a whole panel.

    Parameters
    ----------
    values, published:
        Aligned series indexed by observation date.
    as_of:
        The moment to evaluate. Naive values are read as UTC.
    skip_missing:
        Ignore NaN observations when choosing the answer.

    Returns
    -------
    float or None
        ``None`` when nothing had been published yet.

    Examples
    --------
    >>> obs = pd.DatetimeIndex(["2020-01-01", "2020-02-01"], tz="UTC")
    >>> values = pd.Series([3.6, 3.5], index=obs)
    >>> published = pd.Series(
    ...     pd.DatetimeIndex(["2020-02-07", "2020-03-06"], tz="UTC"), index=obs
    ... )
    >>> value_available_at(values, published, "2020-02-06") is None
    True
    >>> value_available_at(values, published, "2020-02-07")
    3.6
    """
    moment = pd.Timestamp(as_of)
    if moment.tzinfo is None:
        moment = moment.tz_localize("UTC")

    frame = pd.DataFrame({"value": values, "published": published}).dropna(
        subset=["published"]
    )
    if skip_missing:
        frame = frame.dropna(subset=["value"])
    if frame.empty:
        return None

    result = _as_of_values(
        np.asarray(pd.DatetimeIndex(frame.index), dtype="datetime64[ns]"),
        frame["value"].to_numpy(dtype="float64"),
        np.asarray(pd.DatetimeIndex(frame["published"]), dtype="datetime64[ns]"),
        np.asarray(pd.DatetimeIndex([moment]), dtype="datetime64[ns]"),
    )
    return None if np.isnan(result[0]) else float(result[0])


def align_macro(
    macro_long: pd.DataFrame,
    calendar: pd.DatetimeIndex,
    *,
    missing_publication: str = "error",
    fallback_lag_days: int = 1,
) -> pd.DataFrame:
    """Align macro data to the market calendar, publication-aware.

    Thin wrapper over :func:`publication_aware_ffill` that also logs any
    series diagnosed as unusable, so an all-NaN column is explained rather
    than merely observed.
    """
    for series_id, flags in diagnose_publication_dates(macro_long).items():
        if not flags["usable"]:
            logger.warning(
                "%s has a single publication date after its last observation: "
                "it was ingested in 'latest' mode and carries no usable "
                "release calendar. Its aligned column will be empty.",
                series_id,
            )
    return publication_aware_ffill(
        macro_long,
        calendar,
        missing_publication=missing_publication,
        fallback_lag_days=fallback_lag_days,
    )


def ohlcv_fields_present(panels: Mapping[str, pd.DataFrame]) -> tuple[str, ...]:
    """Return which canonical OHLCV fields the panels supply, in order."""
    return tuple(field for field in OHLCV_COLUMNS if field in panels)
