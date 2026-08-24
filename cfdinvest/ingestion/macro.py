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

``publication_lag`` (default)
    Every observation carries a real publication date, obtained the best way
    available for that series: FRED's first prints where it serves them,
    otherwise the latest values stamped ``observation_date + release lag``.
    Safe as a global default because only high-vintage *daily* series ever
    take the fallback, and those are not revised.
``initial_release``
    Strictly first prints. Falls back to ``publication_lag`` when FRED
    refuses the vintage span (>2000 vintages), and warns when it does.
``latest``
    Newest revision. Convenient, look-ahead biased. FRED stamps
    ``publication_date`` with *today*, which makes the bias detectable.
``vintage``
    ALFRED snapshot as of a fixed ``realtime`` date. Real-time bounds are
    forwarded exactly as configured.

Any of these can be pinned for a single series via
:attr:`MacroDataConfig.series_overrides`; see :func:`series_settings`.

Modes are never mixed inside one file: :meth:`MacroDataIngestor.ingest`
refuses to append vintage rows onto a latest-revision file unless the
caller explicitly opts in with ``allow_mode_change=True``.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields as dataclass_fields
from dataclasses import replace
from typing import Any, Iterable, Mapping, Sequence

import pandas as pd
import requests

from cfdinvest import storage
from cfdinvest.config import AppConfig, MacroDataConfig, NetworkConfig
from cfdinvest.retry import retry_call
from cfdinvest.schema import (
    DEFAULT_VINTAGE_MODE,
    MACRO_VALUE,
    POINT_IN_TIME_MODES,
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


class FredVintageLimitError(FredError):
    """Raised when a series has too many vintages for a first-print request.

    Distinct from :class:`FredError` because it is *recoverable*: the
    ingestor answers it by falling back to a synthetic publication lag
    rather than failing the series.
    """


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


def series_settings(settings: MacroDataConfig, series_id: str) -> MacroDataConfig:
    """Return ``settings`` with any per-series override applied.

    Pure. Per-series overrides live in
    :attr:`MacroDataConfig.series_overrides` and may set any field of the
    macro config -- in practice ``vintage_mode`` and ``release_lag_days``.
    Resolving them into a normal config object means every downstream
    function keeps working on a plain :class:`MacroDataConfig` and never
    has to know overrides exist.

    Unknown keys are ignored with a warning rather than raising, so a typo
    in one series' table cannot abort a 50-series run.
    """
    override = (settings.series_overrides or {}).get(series_id)
    if not override:
        return settings

    known = {f.name for f in dataclass_fields(MacroDataConfig)}
    changes = {}
    for key, value in override.items():
        if key == "release_lag_days":
            # Friendlier per-series spelling of the run-level field.
            changes["fallback_release_lag_days"] = int(value)
        elif key in known:
            changes[key] = value
        else:
            logger.warning(
                "Ignoring unknown override %r for series %s", key, series_id
            )
    return replace(settings, **changes) if changes else settings


def release_lag(settings: MacroDataConfig) -> pd.Timedelta:
    """Publication lag used when a real first-print date is unavailable."""
    return pd.Timedelta(days=max(int(settings.fallback_release_lag_days), 0))


def apply_synthetic_publication_dates(
    frame: pd.DataFrame, lag: pd.Timedelta
) -> pd.DataFrame:
    """Stamp ``publication_date = observation_date + lag`` on every row.

    Pure -- returns a new frame. Used only on the fallback path, where FRED
    refused to serve first prints. Overwrites whatever ``realtime_start``
    the latest-revision response carried, because in that mode FRED stamps
    it with *today*, which is not a publication date at all.

    The result is honest for unrevised series (the value really was public
    ``lag`` after the observation period) and is the only reason the
    fallback is safe: only near-daily series can breach FRED's vintage cap,
    and those are not revised.
    """
    if frame.empty:
        return frame
    result = frame.copy()
    result[PUBLICATION_DATE] = result.index + lag
    return result


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
        One of ``"publication_lag"`` (the default), ``"initial_release"``,
        ``"latest"`` or ``"vintage"``.

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
    if settings.realtime_start or settings.realtime_end:
        return "vintage"
    return DEFAULT_VINTAGE_MODE


def build_observation_params(
    series_id: str,
    settings: MacroDataConfig,
    *,
    observation_start: str | None = None,
    mode: str | None = None,
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
    ``initial_release`` / ``publication_lag``
        ``output_type=4`` plus a real-time window wide enough to span every
        vintage, so FRED returns each observation's first print stamped with
        its true publication date. Explicit bounds still win. Both modes make
        the same request; they differ only in how the ingestor reacts when
        FRED refuses it.

    Parameters
    ----------
    mode:
        Override the mode derived from ``settings``. The ingestor passes
        ``"latest"`` here on the fallback path.
    """
    params: dict[str, str] = {
        "series_id": series_id,
        "api_key": settings.api_key or "",
        "file_type": "json",
        "observation_start": observation_start or settings.observation_start,
    }
    if settings.observation_end:
        params["observation_end"] = settings.observation_end

    mode = mode or resolve_vintage_mode(settings)
    if mode in POINT_IN_TIME_MODES:
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
            # Recoverable: the caller falls back to a synthetic publication
            # lag. Only near-daily series can breach the vintage cap, and
            # those are not revised, so the fallback is sound for them.
            raise FredVintageLimitError(
                f"{description}: too many vintage dates for a first-print "
                f"request ({detail})"
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
        support = self.stored_initial_release_support()
        per_series = {s: series_settings(settings, s) for s in symbols}
        use_incremental = incremental and observation_start is None
        logger.info(
            "Ingesting %d FRED series in %s mode (%s)",
            len(symbols),
            mode,
            "incremental" if use_incremental else "full refetch",
        )
        overridden = {
            s: resolve_vintage_mode(cfg)
            for s, cfg in per_series.items()
            if resolve_vintage_mode(cfg) != mode
        }
        if overridden:
            logger.info("Per-series mode overrides: %s", overridden)
        for series_id, cfg in per_series.items():
            if resolve_vintage_mode(cfg) == "latest":
                logger.warning(
                    "%s: vintage_mode=latest returns the newest revision of "
                    "every observation; values are NOT what a trader saw at "
                    "the time.",
                    series_id,
                )

        # FRED work is I/O-bound and independent per series, so a small thread
        # pool is the right shape; the shared RateLimiter keeps us compliant.
        workers = max(1, min(settings.max_workers, len(symbols)))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="fred") as pool:
            results = tuple(
                pool.map(
                    lambda s: self._ingest_series(
                        s,
                        per_series[s],
                        use_incremental,
                        stored_mode=stored_modes.get(s),
                        initial_release_supported=support.get(s),
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
        mode: str | None = None,
    ) -> pd.DataFrame:
        """Fetch one series and return it in the canonical schema.

        Public because it is useful on its own (ad-hoc exploration, tests)
        and because it performs no writes.

        Parameters
        ----------
        mode:
            Override the mode derived from ``settings``; the fallback path
            passes ``"latest"``.
        """
        params = build_observation_params(
            series_id, settings, observation_start=observation_start, mode=mode
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

    def fetch_point_in_time(
        self,
        series_id: str,
        settings: MacroDataConfig,
        *,
        mode: str,
        observation_start: str | None = None,
        initial_release_supported: bool | None = None,
    ) -> tuple[pd.DataFrame, str, bool]:
        """Fetch a series, falling back when first prints are unavailable.

        FRED refuses ``output_type=4`` for series with more than ~2000
        vintage dates. Only near-daily series reach that count, and those
        are not revised -- so rather than failing, we refetch the latest
        values (identical to the first prints, for an unrevised series)
        and stamp ``observation_date + release_lag`` as the publication
        date. The result still satisfies the point-in-time contract.

        Parameters
        ----------
        mode:
            Requested mode.
        initial_release_supported:
            Cached answer from a previous run. ``False`` skips the request
            that is known to fail; ``None`` means unknown, so try it.

        Returns
        -------
        tuple
            ``(frame, effective_mode, initial_release_supported)``.
        """
        if mode not in POINT_IN_TIME_MODES:
            frame = self.fetch_observations(
                series_id, settings, observation_start=observation_start,
                mode=mode,
            )
            return frame, mode, True

        if initial_release_supported is not False:
            try:
                frame = self.fetch_observations(
                    series_id,
                    settings,
                    observation_start=observation_start,
                    mode="initial_release",
                )
                return frame, mode, True
            except FredVintageLimitError as exc:
                logger.info(
                    "%s has too many vintages for first prints (%s)",
                    series_id,
                    exc,
                )

        lag = release_lag(settings)
        log = logger.warning if mode == "initial_release" else logger.info
        log(
            "%s: first prints unavailable from FRED; falling back to "
            "publication_lag with a %d-day release lag. This is sound "
            "because only near-daily series exceed the vintage cap and "
            "those are not revised -- but the publication dates are "
            "DERIVED, not vendor-reported.",
            series_id,
            lag.days,
        )

        frame = self.fetch_observations(
            series_id, settings, observation_start=observation_start, mode="latest"
        )
        return apply_synthetic_publication_dates(frame, lag), "publication_lag", False

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
        stored_mode: str | None = None,
        initial_release_supported: bool | None = None,
        allow_mode_change: bool = False,
    ) -> SymbolResult:
        """Fetch, persist and report on a single series.

        ``settings`` must already have this series' overrides applied --
        see :func:`series_settings`.
        """
        path = storage.macro_path(self.paths, series_id)
        mode = resolve_vintage_mode(settings)

        # Resolve the mode the request will actually PRODUCE before the
        # guardrail runs, so a series known to fall back is not reported
        # as a conflict against the publication_lag file it wrote last time.
        if mode == "initial_release" and initial_release_supported is False:
            mode = "publication_lag"

        if stored_mode is not None and stored_mode != mode and path.is_file():
            if not allow_mode_change:
                message = (
                    f"{series_id} was ingested in {stored_mode!r} mode but this "
                    f"run is {mode!r}. Mixing revision vintages in one file "
                    f"would make it unusable for point-in-time work. Either "
                    f"pin this series with a per-series override "
                    f"([macro.series_overrides.{series_id}] vintage_mode = "
                    f"{stored_mode!r}), or re-run with allow_mode_change=True "
                    f"to replace the file."
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
            frame, effective_mode, supported = self.fetch_point_in_time(
                series_id,
                settings,
                mode=mode,
                observation_start=start,
                initial_release_supported=initial_release_supported,
            )
        except Exception as exc:  # noqa: BLE001 - isolate per-series failures
            logger.error("Fetch failed for %s: %s", series_id, exc)
            return SymbolResult(
                symbol=series_id, status="failed", error=str(exc),
                vintage_mode=mode,
            )

        fell_back = effective_mode != mode or (
            mode in POINT_IN_TIME_MODES and not supported
        )

        if frame.empty:
            logger.warning("No observations returned for %s", series_id)
            return SymbolResult(
                symbol=series_id,
                status="empty",
                error="no observations in range",
                path=str(path),
                vintage_mode=effective_mode,
                fell_back=fell_back,
            )

        try:
            merged = storage.upsert_parquet(frame, path, self.config.storage)
        except storage.StorageError as exc:
            logger.error("Persist failed for %s: %s", series_id, exc)
            return SymbolResult(
                symbol=series_id, status="failed", error=str(exc),
                vintage_mode=effective_mode,
            )

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
            vintage_mode=effective_mode,
            fell_back=fell_back,
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
                    # Per-series truth, which can differ from the run-level
                    # params when an override or a fallback applied.
                    "vintage_mode": r.vintage_mode,
                    "fell_back": r.fell_back,
                    "initial_release_supported": not r.fell_back,
                    "error": r.error,
                }
                for r in report.results
            }
        )
        try:
            storage.write_json(existing, manifest_path)
        except storage.StorageError as exc:
            logger.warning("Could not write manifest: %s", exc)

    def stored_initial_release_support(self) -> dict[str, bool]:
        """Return ``{series_id: first_prints_available}`` from the manifest.

        Lets a later run skip the ``output_type=4`` request that is already
        known to be refused, saving one wasted round-trip per daily series.
        """
        manifest = storage.read_json(
            storage.metadata_path(self.paths, "macro_manifest"), default={}
        ) or {}
        support: dict[str, bool] = {}
        for series_id, entry in manifest.items():
            if not isinstance(entry, dict):
                continue
            flag = entry.get("initial_release_supported")
            if isinstance(flag, bool):
                support[series_id] = flag
        return support

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
            # Per-series value first; fall back to the run-level params
            # written by older versions, then to what those versions did.
            params = entry.get("params") or {}
            modes[series_id] = (
                entry.get("vintage_mode")
                or params.get("vintage_mode")
                or "latest"
            )
        return modes

    def available_series(self) -> tuple[str, ...]:
        """List FRED series already present in the lake."""
        directory = self.paths.raw_macro_dir
        if not directory.is_dir():
            return ()
        return tuple(sorted(p.stem for p in directory.glob("*.parquet")))
