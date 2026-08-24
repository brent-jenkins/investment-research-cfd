"""Market-data ingestion from yfinance into the Parquet lake.

Design notes
------------
* **Batched downloads.** yfinance fetches many tickers per HTTP round-trip.
  We batch (default 25) so a 50-asset universe is two requests, not fifty.
* **Bucketed incremental updates.** On an incremental run each ticker has its
  own resume point. Tickers sharing a resume date are batched together, so a
  single newly-added ticker does not force a full-history refetch for the
  whole universe.
* **Vendor-shape agnostic.** yfinance has changed its column layout across
  versions (``group_by``, ``multi_level_index``). Instead of assuming a
  layout, :func:`split_yfinance_payload` detects which MultiIndex level holds
  the OHLCV field names.
* **Pure normalisation.** :func:`normalize_yfinance_frame` and
  :func:`validate_ohlcv` take frames and return frames/reports. They perform
  no I/O and are the natural unit-test surface.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

from cfdinvest import storage
from cfdinvest.config import AppConfig, MarketDataConfig
from cfdinvest.retry import retry_call
from cfdinvest.schema import (
    INTRADAY_INTERVALS,
    MARKET_OPTIONAL_COLUMNS,
    OHLCV_COLUMNS,
    SUPPORTED_INTERVALS,
    TICKER,
    TIMESTAMP,
    YFINANCE_COLUMN_MAP,
    IngestionReport,
    SymbolResult,
    ensure_utc_index,
)

logger = logging.getLogger(__name__)

#: yfinance serves roughly two years of hourly history; older requests come
#: back empty rather than erroring, so we warn the caller up front.
INTRADAY_HISTORY_LIMIT_DAYS = 720

#: Signature of an injectable downloader, matching ``yfinance.download``.
Downloader = Callable[..., "pd.DataFrame | None"]


def _default_downloader(*args, **kwargs):
    """Import yfinance lazily so the package imports without a network stack."""
    import yfinance  # noqa: PLC0415 - deliberate lazy import

    return yfinance.download(*args, **kwargs)


# --------------------------------------------------------------------------- #
# Pure transforms
# --------------------------------------------------------------------------- #
def chunked(items: Sequence[str], size: int) -> list[tuple[str, ...]]:
    """Split ``items`` into consecutive tuples of at most ``size`` elements.

    >>> chunked(["a", "b", "c"], 2)
    [('a', 'b'), ('c',)]
    """
    if size < 1:
        raise ValueError(f"batch size must be >= 1, got {size}")
    return [tuple(items[i : i + size]) for i in range(0, len(items), size)]


def _field_level(columns: pd.MultiIndex) -> int:
    """Return the MultiIndex level that holds yfinance field names.

    yfinance orders the levels differently depending on ``group_by``; rather
    than trust the flag, detect the level whose values overlap the known
    field vocabulary.
    """
    known = set(YFINANCE_COLUMN_MAP)
    overlaps = [len(known & set(columns.get_level_values(lvl))) for lvl in range(columns.nlevels)]
    return int(np.argmax(overlaps))


def split_yfinance_payload(
    payload: pd.DataFrame | None,
    tickers: Sequence[str],
) -> dict[str, pd.DataFrame]:
    """Split a multi-ticker yfinance response into one frame per ticker.

    Pure. Handles all three shapes yfinance may return: ``None``/empty, a
    flat single-ticker frame, and a two-level MultiIndex in either level
    order.

    Parameters
    ----------
    payload:
        Raw ``yfinance.download`` output.
    tickers:
        The tickers that were requested, used to key the result and to
        interpret a flat single-ticker frame.

    Returns
    -------
    dict
        ``{ticker: raw_frame}``. Tickers absent from the payload are omitted.
    """
    if payload is None or payload.empty:
        return {}

    if not isinstance(payload.columns, pd.MultiIndex):
        # Flat frame: yfinance collapses the level when one ticker is asked
        # for with multi_level_index=False.
        if len(tickers) != 1:
            logger.warning(
                "Flat yfinance frame returned for %d tickers; cannot attribute columns",
                len(tickers),
            )
            return {}
        return {tickers[0]: payload}

    field_level = _field_level(payload.columns)
    ticker_level = 1 - field_level
    available = set(payload.columns.get_level_values(ticker_level))

    frames: dict[str, pd.DataFrame] = {}
    for ticker in tickers:
        if ticker not in available:
            continue
        frames[ticker] = payload.xs(ticker, axis=1, level=ticker_level)
    return frames


def normalize_yfinance_frame(
    raw: pd.DataFrame,
    ticker: str,
    *,
    drop_empty_rows: bool = True,
) -> pd.DataFrame:
    """Convert a raw yfinance frame into the canonical OHLCV schema.

    Pure -- no I/O, no mutation of ``raw``. This is the single place where
    vendor spellings are translated, so the rest of the engine never sees
    ``Adj Close`` or a tz-naive index.

    Steps
    -----
    1. Rename vendor columns to lowercase canonical names.
    2. Force a sorted, unique, tz-aware UTC index named ``timestamp``.
    3. Cast prices to ``float64`` and drop bars with no ``close``.
    4. Order columns canonically and attach the ``ticker`` identity column.

    Parameters
    ----------
    raw:
        Single-ticker frame as returned by yfinance.
    ticker:
        Symbol to stamp into the ``ticker`` column.
    drop_empty_rows:
        Drop rows whose ``close`` is NaN. yfinance pads calendars across a
        mixed-exchange batch, producing empty bars for markets that were shut.

    Returns
    -------
    pandas.DataFrame
        Canonical frame; empty (but correctly typed) if nothing survived.
    """
    if raw is None or raw.empty:
        return _empty_market_frame()

    frame = raw.rename(columns=YFINANCE_COLUMN_MAP)
    # Deduplicate any columns the rename may have collided (e.g. a payload
    # carrying both "Close" and "close").
    frame = frame.loc[:, ~frame.columns.duplicated(keep="first")]

    frame = ensure_utc_index(frame, name=TIMESTAMP)

    present = [c for c in (*OHLCV_COLUMNS, *MARKET_OPTIONAL_COLUMNS) if c in frame.columns]
    frame = frame[present].astype("float64")

    if drop_empty_rows and "close" in frame.columns:
        frame = frame[frame["close"].notna()]

    frame[TICKER] = ticker
    frame[TICKER] = frame[TICKER].astype("category")
    return frame


def _empty_market_frame() -> pd.DataFrame:
    """Return a correctly typed, zero-row canonical market frame."""
    frame = pd.DataFrame(
        {col: pd.Series(dtype="float64") for col in OHLCV_COLUMNS}
        | {TICKER: pd.Series(dtype="object")}
    )
    frame.index = pd.DatetimeIndex([], tz="UTC", name=TIMESTAMP)
    return frame


def validate_ohlcv(frame: pd.DataFrame, ticker: str) -> dict[str, int]:
    """Return counts of data-quality violations in a canonical OHLCV frame.

    Fully vectorised; no rows are dropped. Ingestion records anomalies and
    lets the preprocessing layer decide what to do about them -- silently
    discarding bars here would hide real vendor problems.

    Returns
    -------
    dict
        Violation name -> row count. Only non-zero entries are included.
    """
    if frame.empty:
        return {}

    checks: dict[str, pd.Series] = {}
    price_cols = [c for c in ("open", "high", "low", "close") if c in frame.columns]

    if price_cols:
        prices = frame[price_cols]
        checks["non_positive_price"] = (prices <= 0).any(axis=1)
        checks["missing_price"] = prices.isna().any(axis=1)

    if {"high", "low"} <= set(frame.columns):
        checks["high_below_low"] = frame["high"] < frame["low"]

    if {"high", "low", "close"} <= set(frame.columns):
        checks["close_outside_range"] = (frame["close"] > frame["high"]) | (
            frame["close"] < frame["low"]
        )

    if "volume" in frame.columns:
        checks["negative_volume"] = frame["volume"] < 0

    if "close" in frame.columns and len(frame) > 1:
        # A >50% single-bar move is almost always an unadjusted split or a
        # bad print rather than a real move in a CFD-tradable underlying.
        returns = frame["close"].pct_change()
        checks["extreme_return"] = returns.abs() > 0.5

    violations = {name: int(mask.sum()) for name, mask in checks.items() if mask.sum()}
    if violations:
        logger.warning("Data-quality flags for %s: %s", ticker, violations)
    return violations


def resolve_start_dates(
    tickers: Sequence[str],
    last_seen: Mapping[str, pd.Timestamp | None],
    *,
    default_start: str,
    overlap_days: int,
) -> dict[str, str]:
    """Compute the download start date for each ticker.

    Pure. Tickers with existing data resume ``overlap_days`` before their last
    stored bar, so vendor revisions to recent bars are picked up; tickers with
    no data start at ``default_start``.

    Returns
    -------
    dict
        ``{ticker: "YYYY-MM-DD"}``.
    """
    overlap = pd.Timedelta(days=overlap_days)
    starts: dict[str, str] = {}
    for ticker in tickers:
        last = last_seen.get(ticker)
        if last is None or pd.isna(last):
            starts[ticker] = default_start
        else:
            starts[ticker] = (pd.Timestamp(last) - overlap).strftime("%Y-%m-%d")
    return starts


def bucket_by_start(start_dates: Mapping[str, str]) -> dict[str, tuple[str, ...]]:
    """Group tickers that share a resume date, so batches stay efficient.

    >>> bucket_by_start({"A": "2020-01-01", "B": "2024-01-01", "C": "2020-01-01"})
    {'2020-01-01': ('A', 'C'), '2024-01-01': ('B',)}
    """
    buckets: dict[str, list[str]] = {}
    for ticker, start in start_dates.items():
        buckets.setdefault(start, []).append(ticker)
    return {start: tuple(tickers) for start, tickers in sorted(buckets.items())}


# --------------------------------------------------------------------------- #
# Ingestor
# --------------------------------------------------------------------------- #
class MarketDataIngestor:
    """Downloads OHLCV bars from yfinance and persists them as Parquet.

    Parameters
    ----------
    config:
        Application configuration. Only ``paths``, ``market``, ``network`` and
        ``storage`` are consulted.
    downloader:
        Injection point for tests -- any callable with ``yfinance.download``'s
        signature. Defaults to the real thing, imported lazily.

    Example
    -------
    >>> ingestor = MarketDataIngestor(load_config())        # doctest: +SKIP
    >>> report = ingestor.ingest(["SPY", "^GSPC"])          # doctest: +SKIP
    >>> print(report.summary())                             # doctest: +SKIP
    """

    source = "yfinance"

    def __init__(
        self,
        config: AppConfig,
        *,
        downloader: Downloader | None = None,
    ) -> None:
        self.config = config
        self.paths = config.paths
        self.settings: MarketDataConfig = config.market
        self._download = downloader or _default_downloader

    # -- public API -------------------------------------------------------- #
    def ingest(
        self,
        tickers: Iterable[str] | None = None,
        *,
        interval: str | None = None,
        start: str | None = None,
        end: str | None = None,
        incremental: bool = True,
    ) -> IngestionReport:
        """Download and persist bars for ``tickers``.

        Parameters
        ----------
        tickers:
            Symbols to fetch. Defaults to ``config.market.tickers``.
        interval:
            Bar size, e.g. ``"1d"`` or ``"1h"``. Defaults to config.
        start, end:
            Explicit date bounds (``YYYY-MM-DD``). Supplying ``start``
            disables incremental resume for this run.
        incremental:
            When ``True`` (default) resume each ticker from its last stored
            bar minus ``refetch_overlap_days``.

        Returns
        -------
        IngestionReport
            Per-symbol outcomes; never raises for a single bad ticker.
        """
        started_at = pd.Timestamp.now(tz="UTC")
        settings = self._effective_settings(interval=interval, start=start, end=end)
        symbols = tuple(dict.fromkeys(tickers if tickers is not None else settings.tickers))

        if not symbols:
            logger.warning("No tickers configured; nothing to ingest")
            return IngestionReport(source=self.source, started_at=started_at,
                                   finished_at=pd.Timestamp.now(tz="UTC"))

        self._validate_interval(settings)
        self.paths.ensure()

        use_incremental = incremental and start is None
        start_dates = self._plan_start_dates(symbols, settings, use_incremental)

        logger.info(
            "Ingesting %d ticker(s) at %s interval (%s)",
            len(symbols),
            settings.interval,
            "incremental" if use_incremental else "full refetch",
        )

        results: list[SymbolResult] = []
        for bucket_start, bucket_tickers in bucket_by_start(start_dates).items():
            for batch in chunked(bucket_tickers, settings.batch_size):
                results.extend(self._ingest_batch(batch, bucket_start, settings))

        report = IngestionReport(
            source=self.source,
            results=tuple(results),
            started_at=started_at,
            finished_at=pd.Timestamp.now(tz="UTC"),
            params={
                "interval": settings.interval,
                "start": settings.start,
                "end": settings.end,
                "auto_adjust": settings.auto_adjust,
                "incremental": use_incremental,
            },
        )
        self._write_manifest(report, settings)
        logger.info(report.summary())
        return report

    # -- internals --------------------------------------------------------- #
    def _effective_settings(
        self, *, interval: str | None, start: str | None, end: str | None
    ) -> MarketDataConfig:
        """Return a copy of the market config with per-call overrides applied."""
        changes: dict[str, object] = {}
        if interval is not None:
            changes["interval"] = interval
        if start is not None:
            changes["start"] = start
        if end is not None:
            changes["end"] = end
        return replace(self.settings, **changes) if changes else self.settings

    @staticmethod
    def _validate_interval(settings: MarketDataConfig) -> None:
        """Warn about unsupported intervals and intraday history limits."""
        if settings.interval not in SUPPORTED_INTERVALS:
            logger.warning(
                "Interval %r is outside the engine's supported set %s; "
                "downstream layers may not handle it",
                settings.interval,
                sorted(SUPPORTED_INTERVALS),
            )
        if settings.interval in INTRADAY_INTERVALS:
            requested = pd.Timestamp.now(tz="UTC") - pd.Timestamp(settings.start, tz="UTC")
            if requested.days > INTRADAY_HISTORY_LIMIT_DAYS:
                logger.warning(
                    "Requested %d days of %s bars from %s, but yfinance serves only "
                    "~%d days of intraday history; earlier bars will be missing",
                    requested.days,
                    settings.interval,
                    settings.start,
                    INTRADAY_HISTORY_LIMIT_DAYS,
                )

    def _plan_start_dates(
        self,
        symbols: Sequence[str],
        settings: MarketDataConfig,
        incremental: bool,
    ) -> dict[str, str]:
        """Resolve each ticker's download start date."""
        if not incremental:
            return dict.fromkeys(symbols, settings.start)

        last_seen = {
            ticker: storage.last_timestamp(
                storage.market_path(self.paths, ticker, settings.interval)
            )
            for ticker in symbols
        }
        return resolve_start_dates(
            symbols,
            last_seen,
            default_start=settings.start,
            overlap_days=settings.refetch_overlap_days,
        )

    def _ingest_batch(
        self,
        batch: Sequence[str],
        start: str,
        settings: MarketDataConfig,
    ) -> list[SymbolResult]:
        """Download one batch and persist each ticker within it."""
        logger.info("Downloading %s from %s: %s", settings.interval, start, ", ".join(batch))

        try:
            payload = retry_call(
                lambda: self._download(
                    list(batch),
                    start=start,
                    end=settings.end,
                    interval=settings.interval,
                    auto_adjust=settings.auto_adjust,
                    actions=settings.include_actions,
                    group_by="ticker",
                    threads=settings.threads,
                    progress=False,
                    timeout=self.config.network.timeout,
                ),
                config=self.config.network,
                description=f"yfinance batch [{', '.join(batch)}]",
            )
        except Exception as exc:  # noqa: BLE001 - one bad batch must not abort the run
            logger.error("Batch download failed for %s: %s", ", ".join(batch), exc)
            return [
                SymbolResult(symbol=t, status="failed", error=str(exc)) for t in batch
            ]

        frames = split_yfinance_payload(payload, list(batch))
        return [self._persist(ticker, frames.get(ticker), settings) for ticker in batch]

    def _persist(
        self,
        ticker: str,
        raw: pd.DataFrame | None,
        settings: MarketDataConfig,
    ) -> SymbolResult:
        """Normalise, validate and upsert a single ticker's bars."""
        path = storage.market_path(self.paths, ticker, settings.interval)
        try:
            frame = normalize_yfinance_frame(raw, ticker)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Normalisation failed for %s", ticker)
            return SymbolResult(symbol=ticker, status="failed", error=str(exc))

        if frame.empty:
            logger.warning("No bars returned for %s", ticker)
            return SymbolResult(
                symbol=ticker,
                status="empty",
                error="vendor returned no rows",
                path=str(path),
            )

        try:
            merged = storage.upsert_parquet(frame, path, self.config.storage)
        except storage.StorageError as exc:
            logger.error("Persist failed for %s: %s", ticker, exc)
            return SymbolResult(symbol=ticker, status="failed", error=str(exc))

        # Validate the MERGED series, not just this run's slice: on an
        # incremental run the slice is a few days wide, and flags recorded
        # from it would silently drop every historical anomaly from the
        # manifest. The bars themselves are written either way -- anomalies
        # are recorded, never repaired.
        anomalies = validate_ohlcv(merged, ticker)

        return SymbolResult(
            symbol=ticker,
            status="ok",
            rows_written=len(frame),
            rows_total=len(merged),
            first_timestamp=merged.index.min(),
            last_timestamp=merged.index.max(),
            path=str(path),
            anomalies=anomalies,
        )

    def _write_manifest(self, report: IngestionReport, settings: MarketDataConfig) -> None:
        """Persist run provenance next to the data for reproducibility."""
        manifest_path = storage.metadata_path(
            self.paths, f"market_{settings.interval}_manifest"
        )
        existing = storage.read_json(manifest_path, default={}) or {}
        existing.update(
            {
                r.symbol: {
                    "status": r.status,
                    "rows_total": r.rows_total,
                    "first_timestamp": r.first_timestamp,
                    "last_timestamp": r.last_timestamp,
                    "updated_at": report.finished_at,
                    "params": report.params,
                    "anomalies": r.anomalies,
                    "error": r.error,
                }
                for r in report.results
            }
        )
        try:
            storage.write_json(existing, manifest_path)
        except storage.StorageError as exc:
            logger.warning("Could not write manifest: %s", exc)

    def available_tickers(self, interval: str | None = None) -> tuple[str, ...]:
        """List tickers already present in the lake for a given interval."""
        bar_interval = interval or self.settings.interval
        directory: Path = self.paths.raw_market_dir / bar_interval
        if not directory.is_dir():
            return ()
        return tuple(sorted(p.stem for p in directory.glob("*.parquet")))
