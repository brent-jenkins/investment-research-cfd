"""Read-only access to everything the UI displays.

**Nothing in this module writes, mutates, refreshes or trades.** Every
function opens files the pipeline already wrote and returns what it found.
That is the whole safety contract of this package, and it is enforceable by
reading this one file.

**Every function returns a** :class:`Source` **and none of them raise.** A
dashboard whose job is to report that the system is broken must not itself
break when the system is broken: a missing lake, an unreadable config or a
half-written parquet file all have to render as a red panel, not a stack
trace. So failures are captured and returned, and the caller decides how
loudly to say so.

**Where the pipeline persists nothing, this module says so** rather than
inventing a value. The scheduler keeps no state file, and regimes and the
analysis frame are never written to disk, so the helpers for those return
``available=False`` with a reason. A blank panel and an absent panel look
the same to a reader; only one of them is honest.

Clocks are injected. Anything that needs the current time takes ``now`` so
the same inputs always produce the same output.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from cfdinvest import storage
from cfdinvest.config import AppConfig, load_config
from cfdinvest.repository import DataRepository
from cfdinvest.schedule import market_calendar, scheduler
from cfdinvest.schedule.runner import load_summary

logger = logging.getLogger(__name__)

__all__ = [
    "Source",
    "utcnow",
    "load_app_config",
    "open_repository",
    "load_lake_metadata",
    "list_runs",
    "load_last_pipeline_summary",
    "load_scheduler_status",
    "market_status",
    "load_signal_frame",
    "load_execution_frame",
    "load_regime_frame",
    "load_analysis_frame",
    "load_error_log",
    "load_scheduler_log",
    "load_log_files",
    "disk_usage",
    "load_run_analytics",
    "load_stored_analytics",
    "load_persisted_run",
]

#: Files a run directory may contain, and what each holds.
RUN_ARTEFACTS: dict[str, str] = {
    "summary": "summary.json",
    "equity": "equity.parquet",
    "trades": "trades.parquet",
    "positions": "positions.parquet",
    "positions_confirmed": "positions_confirmed.parquet",
    "signals": "signals.parquet",
}


@dataclass(frozen=True, slots=True)
class Source:
    """One read, and whether it worked.

    Attributes
    ----------
    value:
        Whatever was read. Never ``None`` for frame-shaped reads — an empty
        DataFrame instead, so a caller can render it without checking.
    available:
        ``False`` when there was nothing to read. Not an error: a fresh
        install has no runs yet, and that is worth saying plainly.
    error:
        Set when the read was attempted and failed. **This is the field
        that matters** — an unreadable lake is a critical warning, while an
        empty one is merely early.
    note:
        Why the data is absent, when its absence is structural rather than
        incidental. Rendered as an explanation instead of a blank panel.
    path:
        Where it was read from, for the Status page to display.
    """

    value: Any = None
    available: bool = True
    error: str | None = None
    note: str | None = None
    path: str | None = None

    @property
    def ok(self) -> bool:
        """Read succeeded and produced something."""
        return self.error is None and self.available

    @property
    def failed(self) -> bool:
        """The read was attempted and broke. Distinct from simply empty."""
        return self.error is not None

    @classmethod
    def missing(cls, note: str, path: str | None = None, value: Any = None):
        """Nothing to read, for a reason worth stating."""
        return cls(value=value, available=False, note=note, path=path)

    @classmethod
    def broken(cls, error: str, path: str | None = None, value: Any = None):
        """A read that failed."""
        return cls(value=value, available=False, error=error, path=path)


def utcnow(now: pd.Timestamp | None = None) -> pd.Timestamp:
    """The current UTC instant, or the injected one.

    Every clock in this package goes through here so a test can pin it.
    """
    if now is not None:
        return pd.Timestamp(now).tz_convert("UTC") if pd.Timestamp(now).tz else (
            pd.Timestamp(now).tz_localize("UTC")
        )
    return pd.Timestamp.now(tz="UTC")


def _empty(*columns: str) -> pd.DataFrame:
    return pd.DataFrame(columns=list(columns))


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def load_app_config(config_path: str | os.PathLike[str] | None = None) -> Source:
    """The :class:`~cfdinvest.config.AppConfig` the pipeline runs under.

    An unreadable config is a critical warning rather than a crash: it means
    every path on every other page is a guess.
    """
    try:
        config = load_config(config_path) if config_path else load_config()
        return Source(value=config, path=str(config_path) if config_path else None)
    except Exception as error:  # noqa: BLE001 - the UI must survive anything
        logger.warning("Config unreadable: %s", error)
        return Source.broken(f"config unreadable: {error}", value=AppConfig())


def open_repository(config: AppConfig) -> Source:
    """A :class:`~cfdinvest.repository.DataRepository` over the lake.

    Read-only by construction: the repository has no write path.
    """
    try:
        root = Path(config.paths.data_root)
        if not root.exists():
            return Source.missing(
                f"lake directory does not exist: {root}", path=str(root)
            )
        if not os.access(root, os.R_OK):
            return Source.broken(
                f"lake directory is not readable: {root}", path=str(root)
            )
        return Source(value=DataRepository(config), path=str(root))
    except Exception as error:  # noqa: BLE001
        return Source.broken(f"lake unreadable: {error}")


# --------------------------------------------------------------------------- #
# Lake
# --------------------------------------------------------------------------- #
def load_lake_metadata(
    config: AppConfig,
    repository: DataRepository | None = None,
    *,
    now: pd.Timestamp | None = None,
) -> Source:
    """Freshness, size and data-quality counts for the lake.

    Returns a dict with ``last_updated``, ``series_count``,
    ``anomalies_count``, ``publication_calendar_freshness``,
    ``vintage_mode``, plus the coverage frames the Status page tabulates.

    ``last_updated`` is the **latest end date across every stored series**,
    not a file mtime: a lake that was rewritten yesterday with data ending
    last month is stale, and the mtime would hide that.
    """
    if repository is None:
        opened = open_repository(config)
        if not opened.ok:
            return Source.broken(
                opened.error or opened.note or "lake unavailable", path=opened.path
            )
        repository = opened.value

    root = Path(config.paths.data_root)
    try:
        coverage = repository.coverage()
        macro = repository.macro_coverage()
        anomalies = repository.anomalies()
    except Exception as error:  # noqa: BLE001
        return Source.broken(f"lake metadata unreadable: {error}", path=str(root))

    # The repository owns this: three call sites used to spell the
    # manifest's column names out themselves and all three guessed wrong.
    _, last_updated = repository.coverage_span(include_macro=True)
    modes = sorted(set(macro["vintage_mode"])) if "vintage_mode" in macro else []

    calendar_age = None
    calendar_rows = 0
    try:
        calendar = repository.publication_calendar()
        calendar_rows = len(calendar)
        if not calendar.empty and "publication_date" in calendar.columns:
            latest = pd.to_datetime(calendar["publication_date"], utc=True).max()
            if pd.notna(latest):
                calendar_age = (utcnow(now) - latest).total_seconds() / 86_400.0
    except Exception as error:  # noqa: BLE001
        logger.debug("Publication calendar unavailable: %s", error)

    metadata = {
        "data_root": str(root),
        "last_updated": last_updated,
        "age_days": None
        if last_updated is None
        else (utcnow(now) - last_updated).total_seconds() / 86_400.0,
        "series_count": int(len(coverage)) + int(len(macro)),
        "market_series": int(len(coverage)),
        "macro_series": int(len(macro)),
        "anomalies_count": int(anomalies.to_numpy().sum()) if not anomalies.empty else 0,
        "publication_calendar_rows": calendar_rows,
        "publication_calendar_freshness": calendar_age,
        "vintage_mode": ", ".join(modes) if modes else None,
        "coverage": coverage,
        "macro_coverage": macro,
        "anomalies": anomalies,
    }
    if coverage.empty and macro.empty:
        return Source.missing("the lake holds no series yet", path=str(root),
                              value=metadata)
    return Source(value=metadata, path=str(root))


# --------------------------------------------------------------------------- #
# Runs
# --------------------------------------------------------------------------- #
def runs_root(
    config: AppConfig, output_dir: str = "runs", account: str | None = None
) -> Path:
    """Where this account's runs live.

    ``account=None`` resolves from ``CFD_ACCOUNT_MODE`` and falls back to the
    shared root, which is what a deployment that has not enabled separation
    still uses. Every run reader goes through here so the dashboard's account
    selector reaches all of them at once.
    """
    from cfdinvest.persist.storage import account_root

    try:
        return account_root(config, account) / output_dir
    except ValueError:
        # An unknown account must not take the dashboard down; the page that
        # would tell you the account is wrong still has to render.
        return Path(config.paths.data_root) / output_dir


def list_runs(
    config: AppConfig, output_dir: str = "runs", account: str | None = None
) -> Source:
    """Every run directory, newest first.

    Run ids sort lexicographically in time order, which is what makes
    "newest" a sort rather than a stat of every directory.
    """
    root = runs_root(config, output_dir, account)
    try:
        if not root.is_dir():
            return Source.missing(
                f"no run directory yet at {root}; the pipeline has not run",
                path=str(root),
                value=[],
            )
        directories = sorted(
            (p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True
        )
        if not directories:
            return Source.missing(
                "the run directory is empty", path=str(root), value=[]
            )
        return Source(value=directories, path=str(root))
    except Exception as error:  # noqa: BLE001
        return Source.broken(f"run directory unreadable: {error}", path=str(root),
                             value=[])


def load_last_pipeline_summary(
    config: AppConfig, output_dir: str = "runs", account: str | None = None
) -> Source:
    """The most recent run's ``summary.json``.

    The single most informative file in the system: it carries the run's
    status, duration, universe, metrics, latest signals and latest trades.
    """
    runs = list_runs(config, output_dir, account)
    if not runs.ok:
        return Source(
            value={}, available=False, error=runs.error, note=runs.note, path=runs.path
        )

    for directory in runs.value:
        path = directory / RUN_ARTEFACTS["summary"]
        if not path.is_file():
            continue
        try:
            summary = load_summary(path)
        except Exception as error:  # noqa: BLE001
            return Source.broken(
                f"run summary unreadable: {error}", path=str(path), value={}
            )
        if summary:
            summary.setdefault("run_id", directory.name)
            summary["run_dir"] = str(directory)
            return Source(value=summary, path=str(path))

    return Source.missing(
        "no run has written a summary yet", path=str(runs.path), value={}
    )


def _read_parquet(path: Path, *columns: str) -> Source:
    """One parquet artefact, or an explanation."""
    if not path.is_file():
        return Source.missing(
            f"{path.name} was not written by the last run",
            path=str(path),
            value=_empty(*columns),
        )
    try:
        return Source(value=storage.read_parquet(path), path=str(path))
    except Exception as error:  # noqa: BLE001
        return Source.broken(
            f"{path.name} unreadable: {error}", path=str(path), value=_empty(*columns)
        )


def load_signal_frame(
    config: AppConfig, output_dir: str = "runs", account: str | None = None
) -> Source:
    """The last run's signals, in long ``(timestamp, ticker)`` form.

    Only *active* signals are persisted by the pipeline — flat cells are the
    overwhelming majority and are dropped before writing — so an instrument
    absent from this frame is flat, not missing.
    """
    summary = load_last_pipeline_summary(config, output_dir, account)
    if not summary.ok:
        return Source(
            value=_empty("direction", "size", "confidence", "reason"),
            available=False, error=summary.error, note=summary.note,
        )
    return _read_parquet(
        Path(summary.value["run_dir"]) / RUN_ARTEFACTS["signals"],
        "direction", "size", "confidence", "reason",
    )


def load_execution_frame(
    config: AppConfig, output_dir: str = "runs", account: str | None = None
) -> Source:
    """The last run's execution artefacts: trades, positions and equity.

    Not an :class:`~cfdinvest.execution.frame.ExecutionFrame` — that object
    lives in memory during a run and is never serialised whole. What the
    pipeline persists is these three frames, and between them they carry
    almost everything the object did: the trade log alone holds costs,
    financing, realised P&L, stop levels, gap flags and FX detail.

    The per-bar P&L decomposition is **not** persisted, so the P&L page
    reconstructs what it can from the trade log and reports the rest from
    the run summary's metrics. See the README.
    """
    summary = load_last_pipeline_summary(config, output_dir, account)
    empty = {
        "trades": _empty(), "positions": _empty(), "equity": _empty(),
        "positions_source": "simulated",
    }
    if not summary.ok:
        return Source(
            value=empty, available=False, error=summary.error, note=summary.note
        )

    directory = Path(summary.value["run_dir"])
    parts, errors, notes = {}, [], []
    for name in ("trades", "positions", "equity"):
        read = _read_parquet(directory / RUN_ARTEFACTS[name])
        parts[name] = read.value
        if read.failed:
            errors.append(read.error)
        elif not read.available and read.note:
            notes.append(read.note)

    # Your confirmed fills win over the simulation where you recorded any.
    # The simulated frame is kept alongside rather than replaced: the useful
    # question afterwards is how far the two differ, and an overwritten
    # artefact cannot answer it.
    parts["positions_simulated"] = parts["positions"]
    parts["positions_source"] = "simulated"
    overlay = _read_parquet(directory / RUN_ARTEFACTS["positions_confirmed"])
    if overlay.available and overlay.value is not None and not overlay.value.empty:
        parts["positions"] = overlay.value
        parts["positions_source"] = "confirmed"

    if errors:
        return Source.broken("; ".join(errors), path=str(directory), value=parts)
    if all(frame is None or frame.empty for frame in parts.values()):
        return Source.missing(
            "; ".join(notes) or "the last run produced no execution output",
            path=str(directory), value=parts,
        )
    return Source(value=parts, path=str(directory), note="; ".join(notes) or None)


def load_regime_frame(
    config: AppConfig, output_dir: str = "runs", account: str | None = None
) -> Source:
    """Regime classifications — **not persisted by the pipeline**.

    The runner writes signals, trades, positions and equity; regimes are
    computed in memory and discarded. Recomputing them here would mean
    re-running preprocessing inside a web request, which is neither a read
    nor fast, so this reports the absence instead.

    The signal ``reason`` string carries the regime context that drove each
    signal, and the Signals page surfaces that in its place.
    """
    return Source.missing(
        "regimes are not persisted by the pipeline; the signal 'reason' "
        "column carries the regime context instead",
        value=_empty(),
    )


def load_analysis_frame(
    config: AppConfig, output_dir: str = "runs", account: str | None = None
) -> Source:
    """Preprocessed panels — **not persisted by the pipeline**.

    Same reason as :func:`load_regime_frame`. Prices themselves are always
    available through the repository; it is the derived panels (returns,
    volatility, ATR, correlations) that are not written out.
    """
    return Source.missing(
        "the analysis frame is not persisted by the pipeline; prices are "
        "readable from the lake, derived panels are not",
        value=_empty(),
    )


# --------------------------------------------------------------------------- #
# Scheduler and market
# --------------------------------------------------------------------------- #
def load_scheduler_status(
    config: AppConfig,
    universe: list[str] | None = None,
    *,
    settings: scheduler.SchedulerConfig | None = None,
    output_dir: str = "runs",
    account: str | None = None,
    now: pd.Timestamp | None = None,
) -> Source:
    """When the pipeline last ran, and when it is next due.

    **The scheduler keeps no state file**, so "last run" is read from the
    run directory rather than from the scheduler itself. That is a genuine
    difference and worth knowing: this reports when a run last *completed*,
    which says nothing about whether a scheduler process is alive right now.
    A dead scheduler shows up here only as a last run that keeps getting
    older — which is exactly what the staleness warning is for.

    ``next_run`` is computed from the session calendar, not read: given the
    universe and the schedule settings it is a pure function of the clock.
    """
    moment = utcnow(now)
    runs = list_runs(config, output_dir, account)
    summary = load_last_pipeline_summary(config, output_dir, account)
    recorded = _recorded_scheduler_state(config, account)

    status: dict[str, Any] = {
        "last_run": None,
        "last_run_started": None,
        "next_run": None,
        "next_run_asset": None,
        "next_run_kind": None,
        "last_duration": None,
        "last_exit_code": None,
        "last_status": None,
        "last_error": None,
        "run_count": len(runs.value or []),
        "state_file": None,
        "history": _run_history(runs.value or []),
    }

    if summary.ok:
        record = summary.value
        status["last_run"] = _timestamp(record.get("finished_at"))
        status["last_run_started"] = _timestamp(record.get("started_at"))
        status["last_duration"] = record.get("duration_seconds")
        status["last_status"] = record.get("status")
        status["last_error"] = record.get("error")
        # There is no exit code to read; the run's own status is the closest
        # honest equivalent, and it is labelled as derived.
        status["last_exit_code"] = 0 if record.get("status") == "ok" else 1
        status["run_id"] = record.get("run_id")

    if universe:
        try:
            event = scheduler.next_event(universe, moment, settings)
            if event is not None:
                status["next_run"] = event.at
                status["next_run_asset"] = event.asset
                status["next_run_kind"] = event.kind
        except Exception as error:  # noqa: BLE001
            logger.debug("Could not compute the next scheduled event: %s", error)

    # The scheduler now records its own state, which answers a question the
    # run directory cannot: a run that *started* and never finished leaves a
    # last_run with no matching last_success. Where the file exists it wins;
    # where it does not, the derived figures stand and say so.
    if recorded:
        status.update(
            {
                "last_run": _timestamp(recorded.get("last_run")) or status["last_run"],
                "last_success": _timestamp(recorded.get("last_success")),
                "last_error": recorded.get("last_error") or status["last_error"],
                "last_error_at": _timestamp(recorded.get("last_error_at")),
                "last_duration": recorded.get("last_duration")
                if recorded.get("last_duration") is not None
                else status["last_duration"],
                "recorded_runs": recorded.get("runs", 0),
                "recorded_failures": recorded.get("failures", 0),
                "state_file": recorded.get("_path"),
                "derived": False,
            }
        )
        started, succeeded = status["last_run"], status["last_success"]
        status["incomplete_run"] = bool(
            started is not None and (succeeded is None or succeeded < started)
        )
    else:
        status["derived"] = True
        status["incomplete_run"] = False

    if summary.failed:
        return Source.broken(summary.error or "run summary unreadable", value=status)
    if not summary.available and not recorded:
        return Source.missing(
            summary.note or "the pipeline has not run yet", value=status
        )
    return Source(value=status)


def _recorded_scheduler_state(
    config: AppConfig, account: str | None = None
) -> dict[str, Any] | None:
    """The scheduler's own state file, or ``None`` when it has never written.

    Absent is meaningful: it means no run has passed through a scheduler that
    records state, and the derived figures are all there is.
    """
    try:
        from cfdinvest.persist import scheduler_state, storage as persist_storage

        path = persist_storage.get_scheduler_state_path(config, account)
        if not path.is_file():
            return None
        state = dict(scheduler_state.load_scheduler_state(config, account))
        state["_path"] = str(path)
        return state
    except Exception as error:  # noqa: BLE001 - a dashboard must not crash
        logger.warning("Scheduler state unreadable: %s", error)
        return None


def _run_history(directories: list[Path], limit: int = 20) -> pd.DataFrame:
    """Status and duration for the most recent runs."""
    rows = []
    for directory in directories[:limit]:
        path = directory / RUN_ARTEFACTS["summary"]
        if not path.is_file():
            continue
        try:
            record = load_summary(path)
        except Exception:  # noqa: BLE001
            rows.append({"run_id": directory.name, "status": "unreadable"})
            continue
        rows.append(
            {
                "run_id": record.get("run_id", directory.name),
                "status": record.get("status"),
                "started_at": _timestamp(record.get("started_at")),
                "duration_seconds": record.get("duration_seconds"),
                "bars": record.get("bars"),
                "error": record.get("error"),
            }
        )
    return pd.DataFrame(rows)


def _timestamp(value: Any) -> pd.Timestamp | None:
    """Parse a stored timestamp, tolerating anything."""
    if value in (None, ""):
        return None
    try:
        parsed = pd.Timestamp(value)
    except Exception:  # noqa: BLE001
        return None
    if parsed.tz is None:
        parsed = parsed.tz_localize("UTC")
    return parsed.tz_convert("UTC")


def market_status(
    universe: list[str] | None,
    *,
    now: pd.Timestamp | None = None,
    asset_classes: dict[str, str] | None = None,
) -> Source:
    """Open/closed, next open, next close and holiday state per instrument.

    Pure: the session calendar is computed, not fetched. No exchange is
    contacted, which is what makes this deterministic and offline.
    """
    moment = utcnow(now)
    if not universe:
        return Source.missing("no universe configured", value=_empty())
    try:
        frame = market_calendar.describe(universe, moment, asset_classes)
    except Exception as error:  # noqa: BLE001
        return Source.broken(f"market calendar failed: {error}", value=_empty())

    if frame.empty:
        return Source.missing("no instruments to describe", value=frame)

    holidays = set()
    try:
        holidays = set(market_calendar.us_market_holidays(moment.year))
    except Exception as error:  # noqa: BLE001
        logger.debug("Holiday calendar unavailable: %s", error)

    # is_holiday takes the date and the session *rule*, not the ticker: two
    # instruments on the same session share a holiday calendar.
    frame = frame.copy()
    frame["is_holiday"] = [
        market_calendar.is_holiday(
            moment.date(), market_calendar.session_for(asset, asset_classes)
        )
        for asset in frame.index
    ]
    return Source(value={"sessions": frame, "holidays": sorted(holidays)})


# --------------------------------------------------------------------------- #
# Logs and disk
# --------------------------------------------------------------------------- #
#: Log files the UI looks for, by role. The pipeline writes at most one
#: (``LoggingConfig.log_file``); the rest are conventional names a systemd
#: unit or a cron wrapper is likely to have created beside it.
LOG_ROLES: dict[str, tuple[str, ...]] = {
    "application": ("cfdinvest.log", "app.log"),
    "scheduler": ("scheduler.log", "cron.log"),
    "error": ("error.log", "errors.log"),
    "ingestion": ("ingestion.log", "ingest.log"),
    "execution": ("execution.log",),
}


def load_log_files(config: AppConfig) -> Source:
    """Every log file the UI can find, by role.

    Looks beside the configured ``log_file`` and under ``<data_root>/logs``.
    Nothing is created; a role with no file is reported as absent.
    """
    candidates: dict[str, Path] = {}
    directories: list[Path] = []

    configured = getattr(config.logging, "log_file", None)
    if configured:
        path = Path(configured)
        candidates["application"] = path
        directories.append(path.parent)
    directories.append(Path(config.paths.data_root) / "logs")

    for role, names in LOG_ROLES.items():
        if role in candidates:
            continue
        for directory in directories:
            for name in names:
                path = directory / name
                if path.is_file():
                    candidates[role] = path
                    break
            if role in candidates:
                break

    present = {role: path for role, path in candidates.items() if path.is_file()}
    if not present:
        return Source.missing(
            "no log files found; set logging.log_file (or CFD_LOG_FILE) to "
            "enable file logging",
            value={},
        )
    return Source(value=present)


def _tail(path: Path, lines: int) -> Source:
    """The last ``lines`` lines of a text file, without loading all of it."""
    try:
        if not path.is_file():
            return Source.missing(f"{path} does not exist", path=str(path), value=[])
        size = path.stat().st_size
        # 512 bytes a line is generous for a log; capped so a runaway file
        # cannot pull hundreds of megabytes into a web request.
        window = min(size, max(lines * 512, 8192), 4_000_000)
        with path.open("rb") as handle:
            handle.seek(size - window)
            content = handle.read().decode("utf-8", errors="replace")
        found = content.splitlines()
        if window < size and found:
            found = found[1:]  # the first line is probably truncated
        return Source(value=found[-lines:], path=str(path))
    except Exception as error:  # noqa: BLE001
        return Source.broken(f"log unreadable: {error}", path=str(path), value=[])


def load_error_log(config: AppConfig, lines: int = 200) -> Source:
    """The error log, or the error lines of the application log.

    Falls back to filtering the application log when no dedicated error file
    exists, which is the common case: one log file, mixed levels.
    """
    files = load_log_files(config)
    available = files.value or {}

    if "error" in available:
        return _tail(available["error"], lines)

    if "application" in available:
        tail = _tail(available["application"], lines * 5)
        if tail.failed:
            return tail
        errors = [
            line
            for line in tail.value
            if "ERROR" in line or "CRITICAL" in line or "Traceback" in line
        ]
        return Source(
            value=errors[-lines:],
            available=bool(errors),
            note=None if errors else "no errors in the application log",
            path=tail.path,
        )

    return Source.missing(files.note or "no log file configured", value=[])


def load_scheduler_log(config: AppConfig, lines: int = 200) -> Source:
    """The scheduler log, if a wrapper is writing one."""
    files = load_log_files(config)
    available = files.value or {}
    if "scheduler" in available:
        return _tail(available["scheduler"], lines)
    return Source.missing(
        "no scheduler log found; a cron or systemd wrapper usually writes one",
        value=[],
    )


def load_run_analytics(
    config: AppConfig, output_dir: str = "runs", account: str | None = None
) -> Source:
    """Metrics and attribution for the last run, computed from what it wrote.

    **Computed here rather than read.** The analytics command-line tool
    stores its results, but nobody should have to run it before the P&L page
    shows a Sharpe ratio. These functions are pure and cheap — they operate
    on a trade log and an equity curve that are already in memory — so the
    dashboard computes them on the spot.

    The attribution is the reconstruction from trades and equity, not the
    engine's own per-bar decomposition: the pipeline does not persist the
    latter. Costs are exact either way, because a cost is only ever charged
    on a trade; what is lost is the split between the overnight gap and the
    intraday move.
    """
    from cfdinvest.analytics import attribution as attribution_module
    from cfdinvest.analytics import metrics as metrics_module
    from cfdinvest.execution.frame import ExecutionFrame

    execution = load_execution_frame(config, output_dir)
    if not execution.ok:
        return Source(
            value={}, available=False, error=execution.error, note=execution.note
        )

    parts = execution.value or {}
    trades = parts.get("trades", pd.DataFrame())
    equity_frame = parts.get("equity", pd.DataFrame())
    positions = parts.get("positions", pd.DataFrame())

    if equity_frame is None or equity_frame.empty:
        return Source.missing("the last run wrote no equity curve", value={})

    equity = pd.to_numeric(
        equity_frame.iloc[:, 0], errors="coerce"
    ).rename("equity")

    summary = load_last_pipeline_summary(config, output_dir, account)
    metadata = dict((summary.value or {}).get("metrics") or {})

    # The engine's own per-bar decomposition, if the run stored one. With it
    # the gap and intraday legs are separable; without it they are folded into
    # one market leg and the reconstruction says so.
    stored_pnl = pd.DataFrame()
    try:
        from cfdinvest.persist import readers as persist_readers

        stored_pnl = persist_readers.load_pnl_frame(None, config)
    except Exception as error:  # noqa: BLE001
        logger.debug("No persisted P&L frame: %s", error)

    try:
        frame = ExecutionFrame(
            positions=positions if positions is not None else pd.DataFrame(),
            trades=trades if trades is not None else pd.DataFrame(),
            equity_curve=equity,
            pnl=stored_pnl,
            metadata=metadata,
        )
        computed = metrics_module.compute_all_metrics(frame)
        if not stored_pnl.empty:
            attribution = attribution_module.compute_pnl_attribution(frame)
            costs = attribution_module.compute_cost_breakdown(frame)
        else:
            attribution = attribution_module.attribution_from_trades(trades, equity)
            costs = attribution_module.cost_breakdown_from_trades(trades, equity)
        per_instrument = attribution_module.compute_instrument_attribution(frame)
    except Exception as error:  # noqa: BLE001 - a dashboard must not crash
        logger.warning("Could not compute analytics: %s", error)
        return Source.broken(f"analytics could not be computed: {error}", value={})

    return Source(
        value={
            "metrics": computed,
            "attribution": attribution,
            "costs": costs,
            "per_instrument": per_instrument,
            "run_id": (summary.value or {}).get("run_id"),
        }
    )


def load_persisted_run(config: AppConfig, run_id: str | None = None) -> Source:
    """The artefacts the runner now writes beyond trades and equity.

    The per-bar P&L decomposition, the regime classifications, the execution
    settings and the analysis panels. Each is reported present or absent
    individually, because a run from before this layer existed has the first
    five artefacts and none of these — and "written by an older runner" is a
    different statement from "something went wrong".
    """
    try:
        from cfdinvest.persist import readers as persist_readers
    except Exception as error:  # noqa: BLE001
        return Source.broken(f"persistence unavailable: {error}", value={})

    try:
        chosen = run_id or persist_readers.load_latest_run(config)
        if chosen is None:
            return Source.missing("no run has been recorded yet", value={})

        payload = {
            "run_id": chosen,
            "pnl": persist_readers.load_pnl_frame(chosen, config),
            "regimes": persist_readers.load_regime_frame(chosen, config),
            "analysis": persist_readers.load_analysis_frame(chosen, config),
            "attribution": persist_readers.load_attribution(chosen, config),
            "costs": persist_readers.load_cost_breakdown(chosen, config),
            "execution_metadata": persist_readers.load_execution_metadata(
                chosen, config
            ),
            "run_metadata": persist_readers.load_run_metadata(chosen, config),
            "warnings": persist_readers.load_run_warnings(chosen, config),
            "status": persist_readers.artefact_status(chosen, config),
        }
    except Exception as error:  # noqa: BLE001
        return Source.broken(f"persisted artefacts unreadable: {error}", value={})

    if payload["pnl"].empty and payload["run_metadata"] is None:
        return Source.missing(
            f"run {chosen} predates the persistence layer, so it carries only "
            "trades, positions and equity",
            value=payload,
        )
    return Source(value=payload)


def load_stored_analytics(config: AppConfig, run_id: str | None = None) -> Source:
    """Sweeps, walk-forward and Monte Carlo written by the analytics tool.

    Unlike :func:`load_run_analytics` these cannot be computed on demand —
    each one re-runs the pipeline many times — so they exist only if
    ``cfdinvest-analytics`` has been run. Falls back to the newest analytics
    directory when the requested run has none, because the common case is
    scoring the latest run without pinning an identifier.
    """
    from cfdinvest.analytics import store as analytics_store

    try:
        available = analytics_store.list_analytics_runs(config)
    except Exception as error:  # noqa: BLE001
        return Source.broken(f"analytics directory unreadable: {error}", value={})

    if not available:
        return Source.missing(
            "no analytics have been written yet; run `cfdinvest-analytics "
            "sweep`, `walkforward` or `montecarlo` to populate this",
            value={},
        )

    chosen = run_id if run_id in available else available[0]
    try:
        payload = {
            "run_id": chosen,
            "available_runs": available,
            "is_fallback": chosen != run_id,
            "metrics": analytics_store.load_run_metrics(config, chosen),
            "attribution": analytics_store.load_run_attribution(config, chosen),
            "costs": analytics_store.load_cost_breakdown(config, chosen),
            "warnings": analytics_store.load_run_warnings(config, chosen),
            "sweep": analytics_store.load_sweep(config, chosen),
            "walkforward": analytics_store.load_walkforward_windows(config, chosen),
            "walkforward_summary": analytics_store.load_walkforward_summary(
                config, chosen
            ),
            "montecarlo": analytics_store.load_montecarlo_summary(config, chosen),
            "montecarlo_paths": analytics_store.load_montecarlo_paths(config, chosen),
        }
    except Exception as error:  # noqa: BLE001
        return Source.broken(f"stored analytics unreadable: {error}", value={})

    return Source(value=payload, path=str(analytics_store.analytics_root(config)))


def disk_usage(path: str | os.PathLike[str]) -> Source:
    """Free space on the volume holding ``path``.

    A full disk is the failure that makes every other failure confusing: the
    lake stops updating, runs fail to write, and nothing says why.
    """
    target = Path(path)
    try:
        probe = target
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        usage = shutil.disk_usage(probe)
        return Source(
            value={
                "total_bytes": usage.total,
                "used_bytes": usage.used,
                "free_bytes": usage.free,
                "free_fraction": usage.free / usage.total if usage.total else 0.0,
                "free_gb": usage.free / 1024**3,
                "path": str(probe),
            },
            path=str(probe),
        )
    except Exception as error:  # noqa: BLE001
        return Source.broken(f"disk usage unavailable: {error}", path=str(target))
