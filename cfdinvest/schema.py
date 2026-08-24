"""Canonical schema for every frame that enters the data lake.

Every downstream layer (preprocessing, signals, execution, analytics) reads
these names and only these names. Vendor-specific spellings -- yfinance's
``Adj Close``, FRED's ``value`` -- are translated at the ingestion boundary
and never leak further in.

Conventions
-----------
* Index name is always :data:`TIMESTAMP` and is always **tz-aware UTC**.
  Daily bars and macro observations sit at midnight UTC; hourly bars carry
  the true bar-open instant. A single timezone across the lake removes an
  entire class of alignment bug when joining assets across exchanges.
* Column names are lowercase ``snake_case``.
* Prices are floats, ``volume`` is a nullable float (some CFD underlyings --
  indices, FX -- report no volume at all).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import pandas as pd

#: Name of the (UTC, tz-aware) index on every ingested frame.
TIMESTAMP = "timestamp"

#: Core OHLCV fields, in canonical order.
OHLCV_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close", "volume")

#: Optional corporate-action / unadjusted columns yfinance may supply.
MARKET_OPTIONAL_COLUMNS: tuple[str, ...] = ("adj_close", "dividends", "stock_splits")

#: Identity column carried inside every market file.
TICKER = "ticker"

#: Columns of a stored macro series.
MACRO_VALUE = "value"
SERIES_ID = "series_id"

#: Date a macro observation was first published (FRED's ``realtime_start``).
#: In ``latest`` vintage mode FRED stamps this with *today*, which is exactly
#: what makes the look-ahead bias auditable after the fact: a publication date
#: equal to the ingest date means "this is a revised value, not a first print".
PUBLICATION_DATE = "publication_date"

#: How a macro file was sourced with respect to revisions.
#:
#: ``publication_lag`` (default)
#:     Every observation carries a real publication date, obtained the best
#:     way available for that series: first prints where FRED serves them,
#:     otherwise latest values stamped ``observation_date + release lag``.
#:     Safe as a global default because the fallback only ever applies to
#:     high-vintage daily series, which are not revised.
#: ``initial_release``
#:     Strictly first prints. Falls back to ``publication_lag`` when FRED
#:     refuses the vintage span, and says so loudly.
#: ``latest``
#:     Newest revision of every observation. Convenient, look-ahead biased.
#: ``vintage``
#:     ALFRED snapshot as it stood on a fixed ``realtime`` date.
VINTAGE_MODES: frozenset[str] = frozenset(
    {"latest", "vintage", "initial_release", "publication_lag"}
)

#: The default sourcing mode. Chosen over ``latest`` because a macro-aware
#: backtest reading revised values is trading on numbers that did not exist
#: at the time.
DEFAULT_VINTAGE_MODE = "publication_lag"

#: Modes that guarantee a usable publication calendar.
POINT_IN_TIME_MODES: frozenset[str] = frozenset(
    {"initial_release", "publication_lag"}
)

#: yfinance field name -> canonical name.
YFINANCE_COLUMN_MAP: dict[str, str] = {
    "Open": "open",
    "High": "high",
    "Low": "low",
    "Close": "close",
    "Adj Close": "adj_close",
    "Volume": "volume",
    "Dividends": "dividends",
    "Stock Splits": "stock_splits",
    "Capital Gains": "capital_gains",
}

#: Intervals the engine supports end-to-end. yfinance caps intraday history
#: (roughly 730 days for 1h), which the ingestion layer warns about.
SUPPORTED_INTERVALS: frozenset[str] = frozenset({"1d", "1h", "1wk"})

#: Intervals that are intraday, i.e. subject to yfinance's history limit.
INTRADAY_INTERVALS: frozenset[str] = frozenset({"1h"})


@dataclass(frozen=True, slots=True)
class SymbolResult:
    """Outcome of ingesting a single symbol (ticker or FRED series)."""

    symbol: str
    status: str
    """One of ``ok``, ``empty``, ``skipped`` or ``failed``."""

    rows_written: int = 0
    rows_total: int = 0
    first_timestamp: pd.Timestamp | None = None
    last_timestamp: pd.Timestamp | None = None
    path: str | None = None
    error: str | None = None
    anomalies: dict[str, int] = field(default_factory=dict)
    """Data-quality flag counts. Recorded, never repaired -- see
    :func:`cfdinvest.ingestion.market.validate_ohlcv`."""

    vintage_mode: str | None = None
    """Macro only: the mode that ACTUALLY produced the file, after any
    fallback. May differ from the mode requested."""

    fell_back: bool = False
    """Macro only: ``True`` when first prints were unavailable and a
    synthetic publication lag was used instead."""

    @property
    def ok(self) -> bool:
        """``True`` when the symbol produced usable data."""
        return self.status == "ok"


@dataclass(frozen=True, slots=True)
class IngestionReport:
    """Aggregate result of one ingestion run.

    Returned rather than printed, so callers can assert on it in tests, feed
    it to a scheduler, or serialise it as a run manifest.
    """

    source: str
    results: tuple[SymbolResult, ...] = ()
    started_at: pd.Timestamp | None = None
    finished_at: pd.Timestamp | None = None
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def succeeded(self) -> tuple[SymbolResult, ...]:
        """Symbols that returned usable data."""
        return tuple(r for r in self.results if r.ok)

    @property
    def failed(self) -> tuple[SymbolResult, ...]:
        """Symbols that errored or returned nothing."""
        return tuple(r for r in self.results if r.status in {"failed", "empty"})

    @property
    def rows_written(self) -> int:
        """Total new/updated rows persisted across all symbols."""
        return sum(r.rows_written for r in self.results)

    def to_frame(self) -> pd.DataFrame:
        """Render the report as a DataFrame for logging or notebook display."""
        if not self.results:
            return pd.DataFrame(
                columns=["symbol", "status", "rows_written", "rows_total", "error"]
            )
        # asdict, not vars: these are slots=True dataclasses and have no __dict__.
        return pd.DataFrame([asdict(r) for r in self.results]).set_index("symbol")

    def summary(self) -> str:
        """Return a one-line human-readable summary of the run."""
        elapsed = (
            f" in {(self.finished_at - self.started_at).total_seconds():.1f}s"
            if self.started_at is not None and self.finished_at is not None
            else ""
        )
        return (
            f"{self.source}: {len(self.succeeded)}/{len(self.results)} symbols ok, "
            f"{self.rows_written} rows written{elapsed}"
        )


def ensure_utc_index(frame: pd.DataFrame, *, name: str = TIMESTAMP) -> pd.DataFrame:
    """Return a copy of ``frame`` with a sorted, unique, tz-aware UTC index.

    Pure. Naive indexes are assumed to already be in UTC (true for yfinance
    daily bars and FRED dates); aware indexes are converted.

    Parameters
    ----------
    frame:
        Any frame whose index is date-like.
    name:
        Name to assign to the resulting index.

    Returns
    -------
    pandas.DataFrame
        Frame with a ``DatetimeIndex`` in UTC, sorted ascending, duplicates
        removed (last occurrence wins).
    """
    if frame.empty:
        empty = frame.copy()
        empty.index = pd.DatetimeIndex([], tz="UTC", name=name)
        return empty

    result = frame.copy()
    # ``utc=True`` collapses all three cases in one call: a naive index is
    # localised to UTC, an aware index is converted, and an object index
    # holding MIXED offsets (what a multi-exchange concat produces) is
    # resolved instead of raising or degrading to object dtype.
    index = pd.DatetimeIndex(pd.to_datetime(result.index, errors="coerce", utc=True))

    result.index = index.rename(name)
    result = result[result.index.notna()]
    result = result[~result.index.duplicated(keep="last")]
    return result.sort_index()
