"""Macro-series ingestion from the FRED API into the Parquet lake.

Uses ``requests`` directly against the public FRED REST endpoints rather than
a wrapper library, which keeps the dependency surface small and the retry /
rate-limit behaviour under our own control.

Endpoints used
--------------
``GET /fred/series/observations``  -- the time series itself
``GET /fred/series``               -- title, units, frequency, seasonality

Look-ahead warning
------------------
By default FRED returns the *latest revision* of every observation. A
backtest that trades on 2008 GDP using today's revised figure is cheating:
that number did not exist in 2008. Three sourcing modes are supported, and
every file records which one produced it in ``macro_manifest.json``:

``latest`` (default)
    Newest revision. Convenient, look-ahead biased. FRED stamps
    ``publication_date`` with *today*, which makes the bias detectable.
``vintage``
    ALFRED snapshot as of a fixed ``realtime`` date. Real-time bounds are
    forwarded exactly as configured.
``initial_release``
    Each observation's first print, carrying its true publication date.
    This is the point-in-time correct mode, and the one whose stored
    ``publication_date`` column lets preprocessing forward-fill without
    look-ahead. Note FRED rejects this mode for series with more than 2000
    vintage dates (daily rate series); those are effectively unrevised
    anyway, so ``latest`` is appropriate for them.

Modes are never mixed inside one file: :meth:`MacroDataIngestor.ingest`
refuses to append vintage rows onto a latest-revision file unless the
caller explicitly opts in with ``allow_mode_change=True``.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any, Iterable, Mapping, Sequence

import pandas as pd
import requests

from cfdinvest import storage
from cfdinvest.config import AppConfig, MacroDataConfig, NetworkConfig
from cfdinvest.retry import retry_call
from cfdinvest.schema import (
    MACRO_VALUE,
    PUBLICATION_DATE,
    SERIES_ID,
    TIMESTAMP,
    VINTAGE_MODES,
    IngestionReport,
    SymbolResult,
    ensure_utc_index,
)

logger = logging.getLogger(__name__)

#: FRED encodes a missing observation as a bare period.
MISSING_VALUE_SENTINEL = "."

#: HTTP statuses worth retrying: rate limit plus transient server errors.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

#: FRED's real-time period bounds. ``output_type=4`` needs a window wide
#: enough to contain every vintage, otherwise FRED reports that no vintage
#: dates exist in the requested period.
FRED_REALTIME_MIN = "1776-07-04"
FRED_REALTIME_MAX = "9999-12-31"

#: ``output_type`` selecting first-release-only observations.
OUTPUT_TYPE_INITIAL_RELEASE = "4"

#: FRED caps a vintage-spanning request at 2000 vintage dates. Daily series
#: (DGS10 has >5000) blow through it; the monthly and quarterly series where
#: revisions actually matter do not. Detected to give a usable error.
VINTAGE_LIMIT_HINT = "maximum number of vintage dates"


class FredError(RuntimeError):
    """Raised when the FRED API returns an unrecoverable error."""


class FredRateLimitError(FredError):
    """Raised on a throttled or transiently failing FRED response."""


class RateLimiter:
    """Thread-safe minimum-interval throttle.

    FRED allows 120 requests/minute; exceeding it earns a 429 and a
    temporary block. A shared limiter across worker threads keeps the whole
    process under the ceiling regardless of how the work is scheduled.
    """

    def __init__(self, min_interval: float) -> None:
        self._min_interval = max(min_interval, 0.0)
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def acquire(self) -> None:
        """Block until the caller is allowed to issue its request."""
        if self._min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait = self._next_allowed - now
            self._next_allowed = max(now, self._next_allowed) + self._min_interval
        if wait > 0:
            time.sleep(wait)


# --------------------------------------------------------------------------- #
# Pure transforms
# --------------------------------------------------------------------------- #
def normalize_fred_observations(
    observations: Sequence[Mapping[str, Any]],
    series_id: str,
) -> pd.DataFrame:
    """Convert a FRED ``observations`` payload into the canonical schema.

    Pure -- no I/O. Fully vectorised: the payload becomes a DataFrame in one
    step and conversions are column-wise, so a 30-year daily series costs no
    Python-level loop.

    Parameters
    ----------
    observations:
        The ``observations`` list from a FRED JSON response. Each element has
        at least ``date`` and ``value`` keys.
    series_id:
        Identifier stamped into the ``series_id`` column.

    Returns
    -------
    pandas.DataFrame
        Index ``timestamp`` (UTC midnight), columns ``value`` (float64, NaN
        for missing observations), ``publication_date`` (UTC, taken from
        FRED's ``realtime_start``) and ``series_id`` (category).

    Notes
    -----
    Missing observations are kept as NaN rather than dropped. The gap itself
    is information -- forward-filling belongs to the preprocessing layer,
    which knows the publication calendar. Storing ``publication_date`` is
    what lets that layer forward-fill *without* look-ahead: a value may be
    used from its publication date onward, never from its observation date.
    """
    if not observations:
        return empty_macro_frame()

    raw = pd.DataFrame(list(observations))
    if "date" not in raw.columns or "value" not in raw.columns:
        raise FredError(
            f"Unexpected FRED payload for {series_id}: columns {list(raw.columns)}"
        )

    values = raw["value"].replace(MISSING_VALUE_SENTINEL, pd.NA)
    # ``.to_numpy()`` is essential: passing the Series with an explicit
    # ``index=`` would make pandas *reindex* it onto the dates and yield an
    # all-NaN column.
    frame = pd.DataFrame(
        {MACRO_VALUE: pd.to_numeric(values, errors="coerce").astype("float64").to_numpy()},
        index=pd.to_datetime(raw["date"], errors="coerce"),
    )
    if "realtime_start" in raw.columns:
        # FRED's realtime_start IS the publication date of that print.
        # Attach it before reindexing so it travels with its observation.
        # ``.array`` (not ``.to_numpy()``): it assigns positionally, so no
        # reindex-to-NaT, and it keeps the tz instead of degrading to an
        # object array that pandas has to re-infer.
        frame[PUBLICATION_DATE] = pd.to_datetime(
            raw["realtime_start"], errors="coerce", utc=True
        ).array

    frame = ensure_utc_index(frame, name=TIMESTAMP)
    frame[SERIES_ID] = series_id
    frame[SERIES_ID] = frame[SERIES_ID].astype("category")
    return frame


def empty_macro_frame() -> pd.DataFrame:
    """Return a correctly typed, zero-row canonical macro frame."""
    frame = pd.DataFrame(
        {
            MACRO_VALUE: pd.Series(dtype="float64"),
            PUBLICATION_DATE: pd.Series(dtype="datetime64[ns, UTC]"),
            SERIES_ID: pd.Series(dtype="object"),
        }
    )
    frame.index = pd.DatetimeIndex([], tz="UTC", name=TIMESTAMP)
    return frame


def resolve_vintage_mode(settings: MacroDataConfig) -> str:
    """Return the effective vintage mode for ``settings``.

    Pure, and the single source of truth for the mode: an explicit
    :attr:`MacroDataConfig.vintage_mode` wins, otherwise the mode is derived
    from whether ALFRED real-time bounds were supplied. Deriving keeps older
    configs -- which only ever set ``realtime_start``/``realtime_end`` --
    working unchanged while still reporting an accurate mode.

    Returns
    -------
    str
        One of ``"latest"``, ``"vintage"`` or ``"initial_release"``.

    Raises
    ------
    FredError
        If an unrecognised mode is configured.
    """
    if settings.vintage_mode is not None:
        mode = settings.vintage_mode.strip().lower()
        if mode not in VINTAGE_MODES:
            raise FredError(
                f"Unknown vintage_mode {settings.vintage_mode!r}; "
                f"expected one of {sorted(VINTAGE_MODES)}"
            )
        return mode
    return "vintage" if (settings.realtime_start or settings.realtime_end) else "latest"


def build_observation_params(
    series_id: str,
    settings: MacroDataConfig,
    *,
    observation_start: str | None = None,
) -> dict[str, str]:
    """Build the query string for ``/fred/series/observations``.

    Pure, and separated from the request so the parameter logic (including
    the ALFRED vintage fields) is unit-testable without a network call.

    Mode behaviour
    --------------
    ``latest``
        No real-time parameters are sent; FRED returns newest revisions.
    ``vintage``
        ``realtime_start``/``realtime_end`` are forwarded **exactly** as
        configured -- never defaulted, never widened. A vintage file must
        mean precisely what the config said or it is mislabelled.
    ``initial_release``
        ``output_type=4`` plus a real-time window wide enough to span every
        vintage, so FRED returns each observation's first print stamped with
        its true publication date. Explicit bounds still win.
    """
    params: dict[str, str] = {
        "series_id": series_id,
        "api_key": settings.api_key or "",
        "file_type": "json",
        "observation_start": observation_start or settings.observation_start,
    }
    if settings.observation_end:
        params["observation_end"] = settings.observation_end

    mode = resolve_vintage_mode(settings)
    if mode == "initial_release":
        params["output_type"] = OUTPUT_TYPE_INITIAL_RELEASE
        # Widen only where the caller left a bound unset.
        params["realtime_start"] = settings.realtime_start or FRED_REALTIME_MIN
        params["realtime_end"] = settings.realtime_end or FRED_REALTIME_MAX
    elif mode == "vintage":
        for key in ("realtime_start", "realtime_end"):
            value = getattr(settings, key)
            if value:
                params[key] = value

    return params


def resolve_observation_start(
    last: pd.Timestamp | None,
    settings: MacroDataConfig,
) -> str:
    """Return the incremental start date for one series.

    Pure. Rewinds ``refetch_overlap_days`` behind the last stored observation
    so that revisions to recent prints are captured.
    """
    if last is None or pd.isna(last):
        return settings.observation_start
    rewound = pd.Timestamp(last) - pd.Timedelta(days=settings.refetch_overlap_days)
    floor = pd.Timestamp(settings.observation_start, tz="UTC")
    return max(rewound, floor).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def _check_response(response: requests.Response, description: str) -> dict[str, Any]:
    """Raise on error statuses, otherwise return the decoded JSON body.

    Retryable statuses raise :class:`FredRateLimitError`; everything else
    raises :class:`FredError`, which :func:`retry_call` is told not to retry.
    """
    if response.status_code in RETRYABLE_STATUS:
        raise FredRateLimitError(
            f"{description}: HTTP {response.status_code} {response.reason}"
        )
    if not response.ok:
        # 400 from FRED carries a useful "error_message" field.
        detail = ""
        try:
            detail = response.json().get("error_message", "")
        except ValueError:
            detail = response.text[:200]
        if VINTAGE_LIMIT_HINT in detail:
            # Seen on daily series in initial_release mode. The cryptic
            # vendor message hides an actionable choice, so spell it out.
            raise FredError(
                f"{description}: too many vintage dates for initial_release "
                f"mode. Daily FRED series are essentially never revised -- "
                f"ingest them with vintage_mode='latest', and reserve "
                f"initial_release for monthly/quarterly series. ({detail})"
            )
        raise FredError(f"{description}: HTTP {response.status_code} {detail}")

    try:
        return response.json()
    except ValueError as exc:
        raise FredError(f"{description}: response was not valid JSON") from exc


# --------------------------------------------------------------------------- #
# Ingestor
# --------------------------------------------------------------------------- #
class MacroDataIngestor:
    """Downloads FRED series and persists them as Parquet.

    Parameters
    ----------
    config:
        Application configuration; ``macro.api_key`` must be set (usually via
        the ``FRED_API_KEY`` environment variable).
    session:
        Injectable :class:`requests.Session`. Supplying a fake session makes
        every code path here testable offline.

    Example
    -------
    >>> ingestor = MacroDataIngestor(load_config())              # doctest: +SKIP
    >>> report = ingestor.ingest(["DGS10", "T10Y2Y"])            # doctest: +SKIP
    """

    source = "fred"

    def __init__(
        self,
        config: AppConfig,
        *,
        session: requests.Session | None = None,
    ) -> None:
        self.config = config
        self.paths = config.paths
        self.settings: MacroDataConfig = config.macro
        self.network: NetworkConfig = config.network
        self._session = session or requests.Session()
        self._owns_session = session is None
        self._limiter = RateLimiter(self.settings.min_request_interval)

    # -- context manager so the session is always closed ------------------- #
    def __enter__(self) -> "MacroDataIngestor":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the HTTP session if this instance created it."""
        if self._owns_session:
            self._session.close()

    # -- public API -------------------------------------------------------- #
    def ingest(
        self,
        series_ids: Iterable[str] | None = None,
        *,
        observation_start: str | None = None,
        observation_end: str | None = None,
        incremental: bool = True,
        allow_mode_change: bool = False,
    ) -> IngestionReport:
        """Download and persist the given FRED series.

        Parameters
        ----------
        series_ids:
            FRED identifiers. Defaults to ``config.macro.series_ids``.
        observation_start, observation_end:
            Explicit bounds; supplying ``observation_start`` disables
            incremental resume for this run.
        incremental:
            Resume each series ``refetch_overlap_days`` before its last
            stored observation.
        allow_mode_change:
            Permit re-ingesting a series under a different vintage mode,
            **replacing** the stored file. Without it a mode change is
            refused: merging first-print rows into a latest-revision file
            would produce a series that is neither, with no way to tell
            which row came from where.

        Returns
        -------
        IngestionReport
            Per-series outcomes; a single failing series never aborts the run.
        """
        started_at = pd.Timestamp.now(tz="UTC")
        settings = self._effective_settings(observation_start, observation_end)
        symbols = tuple(
            dict.fromkeys(series_ids if series_ids is not None else settings.series_ids)
        )

        if not symbols:
            logger.warning("No FRED series configured; nothing to ingest")
            return IngestionReport(source=self.source, started_at=started_at,
                                   finished_at=pd.Timestamp.now(tz="UTC"))

        if not settings.api_key:
            raise FredError(
                "No FRED API key. Set FRED_API_KEY in the environment or a .env "
                "file (free key: https://fred.stlouisfed.org/docs/api/api_key.html)."
            )

        self.paths.ensure()
        mode = resolve_vintage_mode(settings)
        stored_modes = self.stored_vintage_modes()
        use_incremental = incremental and observation_start is None
        logger.info(
            "Ingesting %d FRED series in %s mode (%s)",
            len(symbols),
            mode,
            "incremental" if use_incremental else "full refetch",
        )
        if mode == "latest":
            logger.warning(
                "vintage_mode=latest returns the newest revision of every "
                "observation; values are NOT what a trader saw at the time. "
                "Use initial_release for point-in-time backtests."
            )

        # FRED work is I/O-bound and independent per series, so a small thread
        # pool is the right shape; the shared RateLimiter keeps us compliant.
        workers = max(1, min(settings.max_workers, len(symbols)))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="fred") as pool:
            results = tuple(
                pool.map(
                    lambda s: self._ingest_series(
                        s,
                        settings,
                        use_incremental,
                        mode=mode,
                        stored_mode=stored_modes.get(s),
                        allow_mode_change=allow_mode_change,
                    ),
                    symbols,
                )
            )

        report = IngestionReport(
            source=self.source,
            results=results,
            started_at=started_at,
            finished_at=pd.Timestamp.now(tz="UTC"),
            params={
                "observation_start": settings.observation_start,
                "observation_end": settings.observation_end,
                "realtime_start": settings.realtime_start,
                "realtime_end": settings.realtime_end,
                "vintage_mode": mode,
                "point_in_time": mode != "latest",
                "incremental": use_incremental,
            },
        )
        self._write_manifest(report)
        logger.info(report.summary())
        return report

    def fetch_observations(
        self,
        series_id: str,
        settings: MacroDataConfig,
        *,
        observation_start: str | None = None,
    ) -> pd.DataFrame:
        """Fetch one series and return it in the canonical schema.

        Public because it is useful on its own (ad-hoc exploration, tests)
        and because it performs no writes.
        """
        params = build_observation_params(
            series_id, settings, observation_start=observation_start
        )
        description = f"FRED observations [{series_id}]"

        def _request() -> dict[str, Any]:
            self._limiter.acquire()
            response = self._session.get(
                f"{settings.base_url}/series/observations",
                params=params,
                timeout=self.network.timeout,
            )
            return _check_response(response, description)

        payload = retry_call(
            _request,
            config=self.network,
            description=description,
            # FredError (bad series id, bad key) is permanent -- do not retry it.
            retry_on=(FredRateLimitError, requests.RequestException),
        )
        return normalize_fred_observations(payload.get("observations", []), series_id)

    def fetch_series_metadata(
        self, series_id: str, settings: MacroDataConfig
    ) -> dict[str, Any]:
        """Fetch descriptive metadata (title, units, frequency) for a series.

        Metadata is a convenience, not a requirement: failures are logged and
        swallowed so they cannot fail an otherwise successful ingest.
        """
        description = f"FRED metadata [{series_id}]"

        def _request() -> dict[str, Any]:
            self._limiter.acquire()
            response = self._session.get(
                f"{settings.base_url}/series",
                params={
                    "series_id": series_id,
                    "api_key": settings.api_key or "",
                    "file_type": "json",
                },
                timeout=self.network.timeout,
            )
            return _check_response(response, description)

        try:
            payload = retry_call(
                _request,
                config=self.network,
                description=description,
                retry_on=(FredRateLimitError, requests.RequestException),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Metadata unavailable for %s: %s", series_id, exc)
            return {}

        entries = payload.get("seriess") or []
        if not entries:
            return {}
        wanted = (
            "id", "title", "units", "units_short", "frequency", "frequency_short",
            "seasonal_adjustment_short", "observation_start", "observation_end",
            "last_updated", "notes",
        )
        return {k: entries[0].get(k) for k in wanted if k in entries[0]}

    # -- internals --------------------------------------------------------- #
    def _effective_settings(
        self, observation_start: str | None, observation_end: str | None
    ) -> MacroDataConfig:
        """Return a copy of the macro config with per-call overrides applied."""
        changes: dict[str, object] = {}
        if observation_start is not None:
            changes["observation_start"] = observation_start
        if observation_end is not None:
            changes["observation_end"] = observation_end
        return replace(self.settings, **changes) if changes else self.settings

    def _ingest_series(
        self,
        series_id: str,
        settings: MacroDataConfig,
        incremental: bool,
        *,
        mode: str = "latest",
        stored_mode: str | None = None,
        allow_mode_change: bool = False,
    ) -> SymbolResult:
        """Fetch, persist and report on a single series."""
        path = storage.macro_path(self.paths, series_id)

        if stored_mode is not None and stored_mode != mode and path.is_file():
            if not allow_mode_change:
                message = (
                    f"{series_id} was ingested in {stored_mode!r} mode but this "
                    f"run is {mode!r}. Mixing revision vintages in one file "
                    f"would make it unusable for point-in-time work. Re-run "
                    f"with allow_mode_change=True to replace the file."
                )
                logger.error("%s", message)
                return SymbolResult(
                    symbol=series_id, status="failed", error=message, path=str(path)
                )
            logger.warning(
                "Replacing %s: vintage mode %r -> %r", series_id, stored_mode, mode
            )
            # A mode change invalidates every stored row, so drop the file
            # and refetch in full rather than merging into stale vintages.
            path.unlink(missing_ok=True)
            incremental = False

        start = (
            resolve_observation_start(storage.last_timestamp(path), settings)
            if incremental
            else settings.observation_start
        )

        try:
            frame = self.fetch_observations(series_id, settings, observation_start=start)
        except Exception as exc:  # noqa: BLE001 - isolate per-series failures
            logger.error("Fetch failed for %s: %s", series_id, exc)
            return SymbolResult(symbol=series_id, status="failed", error=str(exc))

        if frame.empty:
            logger.warning("No observations returned for %s", series_id)
            return SymbolResult(
                symbol=series_id,
                status="empty",
                error="no observations in range",
                path=str(path),
            )

        try:
            merged = storage.upsert_parquet(frame, path, self.config.storage)
        except storage.StorageError as exc:
            logger.error("Persist failed for %s: %s", series_id, exc)
            return SymbolResult(symbol=series_id, status="failed", error=str(exc))

        if settings.fetch_metadata:
            metadata = self.fetch_series_metadata(series_id, settings)
            if metadata:
                storage.write_json(
                    metadata, storage.metadata_path(self.paths, f"fred_{series_id}")
                )

        return SymbolResult(
            symbol=series_id,
            status="ok",
            rows_written=len(frame),
            rows_total=len(merged),
            first_timestamp=merged.index.min(),
            last_timestamp=merged.index.max(),
            path=str(path),
        )

    def _write_manifest(self, report: IngestionReport) -> None:
        """Persist run provenance next to the data for reproducibility."""
        manifest_path = storage.metadata_path(self.paths, "macro_manifest")
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
                    "error": r.error,
                }
                for r in report.results
            }
        )
        try:
            storage.write_json(existing, manifest_path)
        except storage.StorageError as exc:
            logger.warning("Could not write manifest: %s", exc)

    def stored_vintage_modes(self) -> dict[str, str]:
        """Return ``{series_id: vintage_mode}`` recorded in the manifest.

        Series ingested before vintage modes were tracked report
        ``"latest"``, which is what the old code path actually did.
        """
        manifest = storage.read_json(
            storage.metadata_path(self.paths, "macro_manifest"), default={}
        ) or {}
        modes: dict[str, str] = {}
        for series_id, entry in manifest.items():
            if not isinstance(entry, dict):
                continue
            params = entry.get("params") or {}
            modes[series_id] = params.get("vintage_mode") or "latest"
        return modes

    def available_series(self) -> tuple[str, ...]:
        """List FRED series already present in the lake."""
        directory = self.paths.raw_macro_dir
        if not directory.is_dir():
            return ()
        return tuple(sorted(p.stem for p in directory.glob("*.parquet")))
