"""Unified read API over the cfdinvest Parquet lake.

Ingestion writes; :class:`DataRepository` reads. Every downstream layer --
preprocessing, signal generation, the CFD execution simulator, analytics --
depends on this class and never touches file paths or vendor libraries.

Two shapes are offered, because backtests need both:

``load_market``
    *Tidy/long*: ``MultiIndex(timestamp, ticker) -> ohlcv``. Natural for
    per-trade iteration, groupby-per-asset feature engineering and joins.

``load_panel``
    *Wide*: ``timestamp -> one column per ticker`` for a single field. This
    is the shape vectorised signal and portfolio maths wants -- a 50-asset
    momentum screen is one ``.pct_change()`` call, not a loop.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterable, Sequence

import numpy as np
import pandas as pd

from cfdinvest import storage
from cfdinvest.ingestion import capital_prices
from cfdinvest.config import AppConfig
from cfdinvest.schema import (
    MACRO_VALUE,
    PUBLICATION_DATE,
    SERIES_ID,
    TICKER,
    TIMESTAMP,
    ensure_utc_index,
)

logger = logging.getLogger(__name__)

#: Manifest keys holding the first and last *observation* in a series.
#:
#: The ingestors write ``first_timestamp`` / ``last_timestamp``
#: (:class:`~cfdinvest.schema.IngestionReport`); the older spellings are kept
#: so a lake written by an earlier version still reads.
#:
#: ``updated_at`` is deliberately absent. It is the moment the file was
#: *written*, and a lake rewritten this morning from a feed that returned
#: nothing new would look fresh while its data rots. Freshness is a property
#: of the newest bar, never of the mtime -- reading the write time here is a
#: check that can never fail, which is worse than no check at all.
OBSERVATION_START_KEYS: tuple[str, ...] = (
    "first_timestamp", "start", "observation_start",
)
OBSERVATION_END_KEYS: tuple[str, ...] = (
    "last_timestamp", "end", "observation_end",
)


def _span_of(frame, keys: tuple[str, ...]):
    """The extreme observation date in ``frame`` across ``keys``, or ``None``."""
    if frame is None or getattr(frame, "empty", True):
        return None
    stamps = []
    for key in keys:
        if key in frame.columns:
            values = pd.to_datetime(frame[key], utc=True, errors="coerce")
            if values.notna().any():
                stamps.append(values)
    if not stamps:
        return None
    return pd.concat(stamps)


class DataNotFoundError(FileNotFoundError):
    """Raised when a requested symbol has no data in the lake."""


# --------------------------------------------------------------------------- #
# Pure frame helpers
# --------------------------------------------------------------------------- #
def to_utc(moment: str | pd.Timestamp | None) -> pd.Timestamp | None:
    """Coerce a date-like value to a tz-aware UTC timestamp (or ``None``)."""
    if moment is None:
        return None
    stamp = pd.Timestamp(moment)
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def slice_range(
    frame: pd.DataFrame,
    start: str | pd.Timestamp | None = None,
    end: str | pd.Timestamp | None = None,
    *,
    level: str | None = None,
) -> pd.DataFrame:
    """Return the rows of ``frame`` whose timestamp falls in ``[start, end]``.

    Pure. Works on both a plain ``DatetimeIndex`` and the ``timestamp`` level
    of a MultiIndex. Bounds are inclusive; either may be ``None``.
    """
    if frame.empty or (start is None and end is None):
        return frame

    index = frame.index.get_level_values(level) if level is not None else frame.index
    mask = np.ones(len(frame), dtype=bool)
    lower, upper = to_utc(start), to_utc(end)

    if lower is not None:
        mask &= np.asarray(index >= lower)
    if upper is not None:
        mask &= np.asarray(index <= upper)
    return frame[mask]


def to_panel(tidy: pd.DataFrame, field_name: str) -> pd.DataFrame:
    """Pivot a tidy market frame into ``timestamp x ticker`` for one field.

    Pure. The result is sorted on both axes so downstream vectorised
    operations (rolling windows, correlation matrices) are deterministic.

    Parameters
    ----------
    tidy:
        Frame indexed by ``(timestamp, ticker)``.
    field_name:
        Column to spread across the ticker axis, e.g. ``"close"``.

    Raises
    ------
    KeyError
        If ``field_name`` is not a column of ``tidy``.
    """
    if tidy.empty:
        return pd.DataFrame(index=pd.DatetimeIndex([], tz="UTC", name=TIMESTAMP))
    if field_name not in tidy.columns:
        raise KeyError(f"{field_name!r} not in {list(tidy.columns)}")

    panel = tidy[field_name].unstack(TICKER)
    panel.columns.name = TICKER
    return panel.sort_index().sort_index(axis=1)


def macro_to_panel(
    long_macro: pd.DataFrame, values: str = MACRO_VALUE
) -> pd.DataFrame:
    """Pivot stacked macro observations into ``timestamp x series_id``.

    Pure. No forward-filling happens here -- that is a preprocessing decision
    that depends on each series' publication calendar.

    Parameters
    ----------
    long_macro:
        Stacked frame carrying ``series_id`` and the ``values`` column.
    values:
        Column to spread. ``value`` (default) yields the data panel;
        ``publication_date`` yields the matching publication calendar.
    """
    if long_macro.empty or values not in long_macro.columns:
        return pd.DataFrame(index=pd.DatetimeIndex([], tz="UTC", name=TIMESTAMP))

    flat = long_macro.reset_index()
    # ``pivot``, not ``pivot_table``: the latter silently drops rows whose
    # value is NaN, which would erase exactly the publication gaps this
    # layer promises to preserve. Cast the id to str so a categorical dtype
    # cannot materialise columns for unobserved categories.
    flat[SERIES_ID] = flat[SERIES_ID].astype(str)
    panel = flat.pivot(index=TIMESTAMP, columns=SERIES_ID, values=values)
    panel.columns.name = SERIES_ID
    return panel.sort_index().sort_index(axis=1)


# --------------------------------------------------------------------------- #
# Bundle
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class DataBundle:
    """Everything one backtest run needs, loaded and aligned in shape.

    Attributes
    ----------
    market:
        Tidy OHLCV indexed by ``(timestamp, ticker)``.
    macro:
        Wide macro panel indexed by ``timestamp``, one column per series.
    interval:
        Bar size the market data was ingested at.
    """

    market: pd.DataFrame
    macro: pd.DataFrame
    interval: str
    tickers: tuple[str, ...] = ()
    series_ids: tuple[str, ...] = ()
    metadata: dict[str, object] = field(default_factory=dict)

    def panel(self, field_name: str = "close") -> pd.DataFrame:
        """Return the wide ``timestamp x ticker`` panel for one OHLCV field."""
        return to_panel(self.market, field_name)

    def describe(self) -> pd.DataFrame:
        """Summarise per-ticker coverage: rows, first and last bar."""
        if self.market.empty:
            return pd.DataFrame(columns=["rows", "first", "last"])

        stamps = pd.Series(
            self.market.index.get_level_values(TIMESTAMP),
            index=self.market.index.get_level_values(TICKER),
        )
        grouped = stamps.groupby(level=0, observed=True)
        summary = pd.DataFrame(
            {"rows": grouped.size(), "first": grouped.min(), "last": grouped.max()}
        )
        summary.index.name = TICKER
        return summary.sort_index()


# --------------------------------------------------------------------------- #
# Repository
# --------------------------------------------------------------------------- #
class DataRepository:
    """Read-only façade over the Parquet lake.

    Parameters
    ----------
    config:
        Application configuration; only ``paths`` and ``market.interval`` are
        consulted.

    Example
    -------
    >>> repo = DataRepository(load_config())                    # doctest: +SKIP
    >>> closes = repo.load_panel("close", start="2015-01-01")   # doctest: +SKIP
    >>> returns = closes.pct_change()                           # doctest: +SKIP
    """

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.paths = config.paths
        #: What the last Capital load repaired; see ``_repair_capital``.
        self.last_price_repairs: list = []

    # -- discovery --------------------------------------------------------- #
    def available_tickers(self, interval: str | None = None) -> tuple[str, ...]:
        """Return tickers present in the lake for ``interval``.

        Prefers the ingestion manifest, which stores original symbols, and
        falls back to filename stems when no manifest exists.
        """
        bar_interval = interval or self.config.market.interval
        manifest = storage.read_json(
            storage.metadata_path(self.paths, f"market_{bar_interval}_manifest"),
            default={},
        )
        if manifest:
            return tuple(sorted(manifest))

        directory = self.paths.raw_market_dir / bar_interval
        if not directory.is_dir():
            return ()
        return tuple(sorted(p.stem for p in directory.glob("*.parquet")))

    def available_series(self) -> tuple[str, ...]:
        """Return FRED series present in the lake."""
        manifest = storage.read_json(
            storage.metadata_path(self.paths, "macro_manifest"), default={}
        )
        if manifest:
            return tuple(sorted(manifest))

        directory = self.paths.raw_macro_dir
        if not directory.is_dir():
            return ()
        return tuple(sorted(p.stem for p in directory.glob("*.parquet")))

    def coverage(self, interval: str | None = None) -> pd.DataFrame:
        """Return the ingestion manifest as a frame: status, rows, date span."""
        bar_interval = interval or self.config.market.interval
        manifest = storage.read_json(
            storage.metadata_path(self.paths, f"market_{bar_interval}_manifest"),
            default={},
        )
        if not manifest:
            return pd.DataFrame()
        return pd.DataFrame(manifest).T.sort_index()

    def coverage_span(
        self, interval: str | None = None, *, include_macro: bool = False,
    ) -> tuple[pd.Timestamp | None, pd.Timestamp | None]:
        """The oldest and newest observation in the lake, or ``(None, None)``.

        The one place the lake's date span is computed. Three call sites used
        to spell the manifest's column names out for themselves and all three
        guessed wrong -- the monitoring layer looked for ``end``/``newest``,
        the analytics CLI for ``start``/``end`` -- so on a real lake the
        freshness check never ran and the sweep window silently lost its
        bounds. Neither failed loudly, because both treat "no dates" as an
        acceptable answer.

        Absence is still named: a lake with no readable dates returns
        ``None``, never a substituted "now".

        Parameters
        ----------
        include_macro:
            Fold the macro manifest in as well. Freshness wants this; a
            backtest window does not, because macro series are published on
            their own much slower calendars and would widen every window to
            the oldest CPI print.
        """
        frames = [self.coverage(interval)]
        if include_macro:
            frames.append(self.macro_coverage())

        starts = [s for s in (_span_of(f, OBSERVATION_START_KEYS) for f in frames)
                  if s is not None]
        ends = [e for e in (_span_of(f, OBSERVATION_END_KEYS) for f in frames)
                if e is not None]

        first = pd.concat(starts).min() if starts else None
        last = pd.concat(ends).max() if ends else None
        return (
            None if first is None or pd.isna(first) else pd.Timestamp(first),
            None if last is None or pd.isna(last) else pd.Timestamp(last),
        )

    def macro_coverage(self) -> pd.DataFrame:
        """Return the macro manifest as a frame, including vintage mode.

        This is the guardrail that makes look-ahead auditable: every stored
        series reports the mode it was fetched under, so a backtest can
        assert it is not reading revised data.
        """
        manifest = storage.read_json(
            storage.metadata_path(self.paths, "macro_manifest"), default={}
        )
        if not manifest:
            return pd.DataFrame()

        frame = pd.DataFrame(manifest).T.sort_index()
        modes = self.macro_vintage_modes()
        frame["vintage_mode"] = [modes.get(s, "latest") for s in frame.index]
        if "fell_back" not in frame.columns:
            frame["fell_back"] = False
        frame["fell_back"] = frame["fell_back"].fillna(False).astype(bool)
        return frame

    def macro_vintage_modes(self) -> dict[str, str]:
        """Return ``{series_id: vintage_mode}`` for every series in the lake."""
        manifest = storage.read_json(
            storage.metadata_path(self.paths, "macro_manifest"), default={}
        ) or {}
        modes: dict[str, str] = {}
        for series_id, entry in manifest.items():
            if not isinstance(entry, dict):
                continue
            modes[series_id] = (
                entry.get("vintage_mode")
                or (entry.get("params") or {}).get("vintage_mode")
                or "latest"
            )
        return modes

    def macro_vintage_mode(self, series_id: str) -> str | None:
        """Return the vintage mode a series was ingested under.

        ``None`` when the series is not in the lake. Series ingested before
        mode tracking report ``"latest"``, which is what that code did.
        """
        manifest = storage.read_json(
            storage.metadata_path(self.paths, "macro_manifest"), default={}
        ) or {}
        entry = manifest.get(series_id)
        if not isinstance(entry, dict):
            return None
        # Per-series value first: it reflects overrides and fallbacks, which
        # the run-level params cannot.
        return (
            entry.get("vintage_mode")
            or (entry.get("params") or {}).get("vintage_mode")
            or "latest"
        )

    def anomalies(self, interval: str | None = None) -> pd.DataFrame:
        """Return per-ticker data-quality flag counts recorded at ingest.

        Anomalous bars are stored unrepaired; this is how a caller finds
        out which tickers carry them and how many. Rows are tickers,
        columns are flag names, values are counts (0 where clean).
        """
        bar_interval = interval or self.config.market.interval
        manifest = storage.read_json(
            storage.metadata_path(self.paths, f"market_{bar_interval}_manifest"),
            default={},
        ) or {}

        flags = {
            symbol: (entry.get("anomalies") or {})
            for symbol, entry in manifest.items()
            if isinstance(entry, dict)
        }
        if not flags:
            return pd.DataFrame()
        frame = pd.DataFrame(flags).T.fillna(0).astype(int).sort_index()
        frame.index.name = TICKER
        return frame

    # -- loading ----------------------------------------------------------- #
    def load_market(
        self,
        tickers: Iterable[str] | None = None,
        *,
        interval: str | None = None,
        start: str | pd.Timestamp | None = None,
        end: str | pd.Timestamp | None = None,
        columns: Sequence[str] | None = None,
        strict: bool = False,
        repair: bool = True,
    ) -> pd.DataFrame:
        """Load OHLCV bars as a tidy frame indexed by ``(timestamp, ticker)``.

        Parameters
        ----------
        tickers:
            Symbols to load. Defaults to everything in the lake for
            ``interval``.
        interval:
            Bar size. Defaults to ``config.market.interval``.
        start, end:
            Inclusive date bounds.
        columns:
            Subset of OHLCV columns to read. Pushing the projection down to
            Parquet keeps a 50-asset close-only load cheap.
        strict:
            Raise :class:`DataNotFoundError` when a requested ticker has no
            file. Default is to warn and continue, so one un-ingested symbol
            does not break a research run.
        repair:
            Rescale Capital bars whose split adjustment is wrong, against the
            daily store -- see :mod:`cfdinvest.ingestion.price_repair`. What
            was repaired is kept on ``last_price_repairs``. Off only for the
            repair's own reads.

        Returns
        -------
        pandas.DataFrame
            Sorted tidy frame. Empty (correctly indexed) when nothing matched.
        """
        bar_interval = interval or self.config.market.interval
        symbols = tuple(tickers) if tickers is not None else self.available_tickers(bar_interval)

        if not symbols:
            logger.warning("No tickers available for interval %s", bar_interval)
            return self._empty_tidy()

        read_columns = self._read_columns(columns)
        frames: list[pd.DataFrame] = []

        for ticker in symbols:
            path = storage.market_path(self.paths, ticker, bar_interval)
            if not path.is_file():
                message = f"No {bar_interval} data for {ticker} at {path}"
                if strict:
                    raise DataNotFoundError(message)
                logger.warning("%s; skipping", message)
                continue

            frame = storage.read_parquet(path, columns=read_columns)
            if frame.empty:
                continue
            frame = ensure_utc_index(frame, name=TIMESTAMP)
            frame = slice_range(frame, start, end)
            if frame.empty:
                continue
            # Re-stamp identity: the stored column is authoritative, but a
            # projection may have excluded it.
            frame[TICKER] = ticker
            frames.append(frame)

        if not frames:
            logger.warning("No rows matched for %s in [%s, %s]", bar_interval, start, end)
            return self._empty_tidy()

        combined = pd.concat(frames)
        combined[TICKER] = combined[TICKER].astype("category")
        tidy = combined.set_index(TICKER, append=True)
        # Capital's feed carries pre-market and after-hours bars; yfinance's
        # does not. Lookbacks here are counted in bars, so handing the raw
        # feed to the engine rescales every window by 1.67x -- see
        # ``capital_prices.SESSION_HOURS``. Filtered once, here, rather than
        # by each caller remembering to.
        if capital_prices.is_capital_interval(bar_interval):
            before = len(tidy)
            tidy = capital_prices.restrict_to_session(tidy)
            if before and not len(tidy):
                logger.warning(
                    "Every %s bar fell outside the regular session; check the "
                    "timestamps rather than treating this as no data",
                    bar_interval,
                )
            # Capital's history is not always split-adjusted: PANW sat at
            # twice its price for 456 sessions, AVGO and NOW carry broken
            # bars. Repaired here, once, for the same reason as the session
            # filter above. Found 2026-10-13.
            if repair:
                tidy = self._repair_capital(tidy, symbols, bar_interval, start, end)

        return tidy.sort_index()

    def _repair_capital(self, tidy, symbols, bar_interval, start, end):
        """Rescale mis-adjusted Capital bars; record what was done."""
        from cfdinvest.ingestion import price_repair

        self.last_price_repairs = []
        prices = [c for c in price_repair.PRICE_COLUMNS if c in tidy.columns]
        if tidy.empty or not prices:
            return tidy
        basis = tidy
        if "close" not in tidy.columns:
            basis = self.load_market(
                symbols, interval=bar_interval, start=start, end=end,
                columns=["open", "close"], repair=False,
            )
            basis = capital_prices.restrict_to_session(basis).reindex(tidy.index)
        named = [t for t in symbols
                 if storage.market_path(self.paths, t, "1d").is_file()]
        daily_start = (pd.Timestamp(start) - pd.Timedelta(days=10)
                       if start is not None else None)
        daily = (self.load_market(named, interval="1d", start=daily_start,
                                  end=end, columns=["close"])["close"]
                 if named else None)
        if daily is None or daily.empty:
            logger.warning(
                "No daily prices to check %s splits against; Capital's bars "
                "are used unrepaired", bar_interval)
            return tidy
        factors, repairs = price_repair.repair_factors(basis, daily)
        self.last_price_repairs = repairs
        changed = factors.to_numpy() != 1.0
        if changed.any():
            tidy = tidy.copy()
            tidy.loc[changed, prices] = tidy.loc[changed, prices].mul(
                factors[changed], axis=0)
            logger.info("Repaired %d %s bar(s): %s", int(changed.sum()),
                        bar_interval,
                        "; ".join(f"{r.ticker} {r.kind} {r.start.date()}..{r.end.date()} x{r.factor:.3f}"
                                  for r in repairs if r.kind != "unverified"))
        return tidy

    def load_panel(
        self,
        field_name: str = "close",
        tickers: Iterable[str] | None = None,
        *,
        interval: str | None = None,
        start: str | pd.Timestamp | None = None,
        end: str | pd.Timestamp | None = None,
    ) -> pd.DataFrame:
        """Load a single OHLCV field as a wide ``timestamp x ticker`` panel.

        Only the requested field is read from Parquet, so this is materially
        cheaper than loading full bars and pivoting afterwards.
        """
        tidy = self.load_market(
            tickers,
            interval=interval,
            start=start,
            end=end,
            columns=[field_name],
        )
        return to_panel(tidy, field_name)

    def load_macro(
        self,
        series_ids: Iterable[str] | None = None,
        *,
        start: str | pd.Timestamp | None = None,
        end: str | pd.Timestamp | None = None,
        wide: bool = True,
        strict: bool = False,
    ) -> pd.DataFrame:
        """Load FRED series from the lake.

        Parameters
        ----------
        series_ids:
            Series to load. Defaults to everything present.
        start, end:
            Inclusive date bounds.
        wide:
            ``True`` (default) returns ``timestamp x series_id``; ``False``
            returns the stacked form with ``value``/``series_id`` columns.
        strict:
            Raise :class:`DataNotFoundError` for a missing series instead of
            warning.

        Notes
        -----
        Values are returned exactly as published: no forward-fill, no
        resampling, no release-lag shift. Those transformations belong to the
        preprocessing layer, which must apply them without look-ahead.
        """
        symbols = tuple(series_ids) if series_ids is not None else self.available_series()
        if not symbols:
            logger.warning("No macro series available in %s", self.paths.raw_macro_dir)
            return pd.DataFrame(index=pd.DatetimeIndex([], tz="UTC", name=TIMESTAMP))

        frames: list[pd.DataFrame] = []
        for series_id in symbols:
            path = storage.macro_path(self.paths, series_id)
            if not path.is_file():
                message = f"No macro data for {series_id} at {path}"
                if strict:
                    raise DataNotFoundError(message)
                logger.warning("%s; skipping", message)
                continue

            frame = storage.read_parquet(path)
            if frame.empty:
                continue
            frame = ensure_utc_index(frame, name=TIMESTAMP)
            frame = slice_range(frame, start, end)
            if frame.empty:
                continue
            frame[SERIES_ID] = series_id
            frames.append(frame)

        if not frames:
            return pd.DataFrame(index=pd.DatetimeIndex([], tz="UTC", name=TIMESTAMP))

        stacked = pd.concat(frames)
        return macro_to_panel(stacked) if wide else stacked.sort_index()

    def publication_calendar(
        self,
        series_ids: Iterable[str] | None = None,
        *,
        start: str | pd.Timestamp | None = None,
        end: str | pd.Timestamp | None = None,
        wide: bool = False,
    ) -> pd.DataFrame:
        """Return the unified publication calendar across all macro series.

        This is the contract the preprocessing layer forward-fills against: a
        value may enter the feature set from its ``publication_date`` onward,
        never from its ``observation_date``.

        Parameters
        ----------
        series_ids:
            Series to include. Defaults to every series in the lake.
        start, end:
            Inclusive bounds on the *observation* date.
        wide:
            ``False`` (default) returns the tidy four-column form below.
            ``True`` returns the ``timestamp x series_id`` panel of
            publication dates, which aligns element-wise with
            :meth:`load_macro`.

        Returns
        -------
        pandas.DataFrame
            Columns ``series_id``, ``observation_date``, ``publication_date``,
            ``vintage_mode``; one row per observation, sorted by
            ``(series_id, observation_date)`` with a clean ``RangeIndex``.
            Deterministic: the same lake always yields the same row order,
            regardless of the order series were requested or ingested in.

        Notes
        -----
        ``vintage_mode`` is carried per row so a caller can tell, without a
        second lookup, whether a given publication date is vendor-reported
        (``initial_release``), derived from a release lag
        (``publication_lag`` after a fallback), or meaningless because the
        series was ingested biased (``latest``, where FRED stamps the ingest
        date). Mixed frequencies coexist happily: rows are per observation,
        so a daily and a quarterly series simply contribute different counts.
        """
        stacked = self.load_macro(series_ids, start=start, end=end, wide=False)
        if wide:
            return macro_to_panel(stacked, values=PUBLICATION_DATE)

        if stacked.empty or PUBLICATION_DATE not in stacked.columns:
            return pd.DataFrame(
                {
                    SERIES_ID: pd.Series(dtype="object"),
                    "observation_date": pd.Series(dtype="datetime64[ns, UTC]"),
                    PUBLICATION_DATE: pd.Series(dtype="datetime64[ns, UTC]"),
                    "vintage_mode": pd.Series(dtype="object"),
                }
            )

        modes = self.macro_vintage_modes()
        calendar = pd.DataFrame(
            {
                SERIES_ID: stacked[SERIES_ID].astype(str).to_numpy(),
                "observation_date": stacked.index,
                PUBLICATION_DATE: stacked[PUBLICATION_DATE].to_numpy(),
            }
        )
        calendar["vintage_mode"] = (
            calendar[SERIES_ID].map(modes).fillna("latest").astype(str)
        )
        calendar = calendar.sort_values(
            [SERIES_ID, "observation_date"], kind="stable"
        ).reset_index(drop=True)
        return calendar[
            [SERIES_ID, "observation_date", PUBLICATION_DATE, "vintage_mode"]
        ]

    def load_bundle(
        self,
        tickers: Iterable[str] | None = None,
        series_ids: Iterable[str] | None = None,
        *,
        interval: str | None = None,
        start: str | pd.Timestamp | None = None,
        end: str | pd.Timestamp | None = None,
    ) -> DataBundle:
        """Load market and macro data together as a single :class:`DataBundle`.

        This is the entry point the preprocessing layer will consume.
        """
        bar_interval = interval or self.config.market.interval
        market = self.load_market(tickers, interval=bar_interval, start=start, end=end)
        macro = self.load_macro(series_ids, start=start, end=end, wide=True)

        loaded_tickers = (
            tuple(market.index.get_level_values(TICKER).unique())
            if not market.empty
            else ()
        )
        return DataBundle(
            market=market,
            macro=macro,
            interval=bar_interval,
            tickers=loaded_tickers,
            series_ids=tuple(macro.columns) if not macro.empty else (),
            metadata={"start": start, "end": end, "data_root": str(self.paths.data_root)},
        )

    # -- internals --------------------------------------------------------- #
    @staticmethod
    def _read_columns(columns: Sequence[str] | None) -> list[str] | None:
        """Add the identity column to a projection so it survives the read."""
        if columns is None:
            return None
        requested = list(dict.fromkeys(columns))
        if TICKER not in requested:
            requested.append(TICKER)
        return requested

    @staticmethod
    def _empty_tidy() -> pd.DataFrame:
        """Return an empty tidy frame with the correct MultiIndex."""
        index = pd.MultiIndex.from_arrays(
            [pd.DatetimeIndex([], tz="UTC"), pd.Index([], dtype="object")],
            names=[TIMESTAMP, TICKER],
        )
        return pd.DataFrame(index=index)
