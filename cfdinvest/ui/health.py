"""The critical-warning checks behind the banner.

**The failure this exists to catch is a system that looks fine.** A stale
lake still serves prices. A dead scheduler still leaves yesterday's run
sitting there. A failed FX conversion still produces a number. Every page in
this app will render perfectly against data that is quietly wrong, and the
only defence is to check the things that do not announce themselves and say
so at the top of every page.

Each check answers one question, states what it found, and says what to do
about it. Checks never raise: a check that crashes is a check that silently
stops protecting you, so an unexpected error inside one is itself reported
as a failure of that check.

Everything here is pure given its inputs. The clock is injected and the data
arrives as :class:`~cfdinvest.ui.sources.Source` objects, so a test can
construct any combination of broken states without a filesystem.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np
import pandas as pd

from cfdinvest.monitoring.core import DEFAULTS as _MONITORING_DEFAULTS
from cfdinvest.ui.sources import Source, utcnow

logger = logging.getLogger(__name__)

__all__ = [
    "BANNER_TEXT",
    "SEVERITIES",
    "HealthThresholds",
    "Check",
    "HealthReport",
    "run_checks",
]

#: What the banner says. Fixed text: an operator learns to recognise one
#: string, and a banner that reworded itself per failure would be read as a
#: different, lesser event.
BANNER_TEXT: str = (
    "⚠ Critical system warning — results may be unreliable. "
    "See Status page."
)

#: Ordered worst-first, which is also the display order.
SEVERITIES: tuple[str, ...] = ("critical", "warning", "ok")


@dataclass(frozen=True, slots=True)
class HealthThresholds:
    """When "old" becomes "wrong".

    Attributes
    ----------
    lake_stale_days:
        How far the newest data in the lake may fall behind now. Three days
        covers a normal weekend; four means a Friday run was missed.
    scheduler_stale_hours:
        How long since the last completed run before it is treated as a
        failure. Forty-eight hours is two missed daily runs — one could be a
        holiday, two is a broken scheduler.
    missed_run_grace_minutes:
        How long a scheduled event may sit in the past before it counts as
        missed, allowing for a run that is simply in progress.
    disk_free_gb, disk_free_fraction:
        A full disk makes every other failure confusing: the lake stops
        updating, runs fail to write, and nothing says why. Either bound
        tripping is enough.
    publication_calendar_stale_days:
        How stale the macro publication calendar may be. Generous, because
        monthly series legitimately go weeks between prints.
    """

    # Taken from cfdinvest.monitoring rather than repeated. The banner and the
    # unattended email alert read the same numbers, and two copies would drift
    # -- the day they drift is the day the banner says fine while the email
    # says critical, and neither is obviously the one to believe.
    lake_stale_days: float = _MONITORING_DEFAULTS.lake_stale_days
    scheduler_stale_hours: float = _MONITORING_DEFAULTS.scheduler_stale_hours
    missed_run_grace_minutes: float = _MONITORING_DEFAULTS.missed_run_grace_minutes
    disk_free_gb: float = _MONITORING_DEFAULTS.disk_free_gb
    disk_free_fraction: float = _MONITORING_DEFAULTS.disk_free_fraction
    publication_calendar_stale_days: float = (
        _MONITORING_DEFAULTS.publication_calendar_stale_days
    )


@dataclass(frozen=True, slots=True)
class Check:
    """One question asked of the system, and the answer.

    Attributes
    ----------
    triggered:
        ``True`` means something is wrong. A check that could not run at all
        is also triggered — an unanswerable question about safety is not a
        reassuring answer.
    detail:
        What was actually found, with the numbers in it. "Lake is stale" is
        not actionable; "newest data is 6.2 days old (threshold 3.0)" is.
    remedy:
        The next thing to do.
    """

    key: str
    title: str
    triggered: bool
    severity: str = "critical"
    detail: str = ""
    remedy: str = ""
    value: Any = None

    @property
    def status(self) -> str:
        """``"critical"``/``"warning"`` when triggered, else ``"ok"``."""
        return self.severity if self.triggered else "ok"


@dataclass(frozen=True, slots=True)
class HealthReport:
    """Every check, and whether the banner should show."""

    checks: tuple[Check, ...] = ()
    checked_at: pd.Timestamp | None = None

    @property
    def triggered(self) -> tuple[Check, ...]:
        """Only the checks that fired, worst first."""
        order = {name: position for position, name in enumerate(SEVERITIES)}
        return tuple(
            sorted(
                (check for check in self.checks if check.triggered),
                key=lambda check: order.get(check.severity, 99),
            )
        )

    @property
    def critical(self) -> tuple[Check, ...]:
        return tuple(c for c in self.triggered if c.severity == "critical")

    @property
    def show_banner(self) -> bool:
        """Whether the red banner belongs at the top of every page."""
        return bool(self.critical)

    @property
    def healthy(self) -> bool:
        return not self.triggered

    def to_frame(self) -> pd.DataFrame:
        """Every check as a row, for the Status page."""
        return pd.DataFrame(
            [
                {
                    "check": check.title,
                    "status": check.status,
                    "detail": check.detail,
                    "remedy": check.remedy,
                }
                for check in self.checks
            ]
        )


def _safe(key: str, title: str) -> Callable:
    """Wrap a check so that its own failure is reported, not raised."""

    def decorate(function):
        def guarded(*args, **kwargs) -> Check:
            try:
                return function(*args, **kwargs)
            except Exception as error:  # noqa: BLE001
                logger.exception("Health check %s failed", key)
                return Check(
                    key=key,
                    title=title,
                    triggered=True,
                    detail=f"the check itself failed: {error}",
                    remedy="This is a bug in the dashboard, not necessarily "
                    "in the pipeline. Treat the underlying state as unknown.",
                )

        return guarded

    return decorate


# --------------------------------------------------------------------------- #
# Individual checks
# --------------------------------------------------------------------------- #
@_safe("config", "Config readable")
def _check_config(config_source: Source) -> Check:
    return Check(
        key="config",
        title="Config readable",
        triggered=config_source.failed,
        detail=config_source.error or "loaded",
        remedy="Fix or restore config.toml; every path on every other page "
        "is a guess until this loads.",
    )


@_safe("lake_readable", "Lake directory readable")
def _check_lake_readable(lake: Source) -> Check:
    unreadable = lake.failed
    return Check(
        key="lake_readable",
        title="Lake directory readable",
        triggered=unreadable,
        detail=lake.error or lake.note or f"readable at {lake.path}",
        remedy="Check CFD_DATA_ROOT, the mount, and filesystem permissions.",
    )


@_safe("lake_fresh", "Lake freshness")
def _check_lake_freshness(lake: Source, thresholds: HealthThresholds) -> Check:
    metadata = lake.value or {}
    # Trading days, matching the units ``lake_stale_days`` is expressed in.
    # The source reports calendar days; comparing those against a trading
    # threshold called a perfectly healthy lake critical every weekend, and
    # is the same unit mismatch in reverse that the email had.
    age = metadata.get("trading_age_days")
    if age is None:
        age = metadata.get("age_days")

    if lake.failed:
        return Check(
            key="lake_fresh", title="Lake freshness", triggered=True,
            detail="cannot determine freshness: the lake is unreadable",
            remedy="Resolve the lake read error first.",
        )
    if age is None:
        return Check(
            key="lake_fresh", title="Lake freshness", triggered=True,
            detail="no dated series in the lake, so freshness is unknown",
            remedy="Run the ingestion layer at least once.",
        )

    stale = age > thresholds.lake_stale_days
    return Check(
        key="lake_fresh",
        title="Lake freshness",
        triggered=stale,
        detail=(
            f"newest data is {age:.1f} trading days old "
            f"(threshold {thresholds.lake_stale_days:.1f})"
        ),
        remedy="Run ingestion. If it is scheduled, check why the schedule "
        "is not firing.",
        value=age,
    )


@_safe("scheduler_recent", "Scheduler activity")
def _check_scheduler(
    status: Source, thresholds: HealthThresholds, now: pd.Timestamp
) -> Check:
    state = status.value or {}
    last = state.get("last_run")

    if last is None:
        return Check(
            key="scheduler_recent", title="Scheduler activity", triggered=True,
            detail=status.error or status.note or "no completed run on record",
            remedy="Run the pipeline once, then confirm the schedule is "
            "installed (crontab or the systemd timer).",
        )

    hours = (now - last).total_seconds() / 3600.0
    stale = hours > thresholds.scheduler_stale_hours
    return Check(
        key="scheduler_recent",
        title="Scheduler activity",
        triggered=stale,
        detail=(
            f"last completed run was {hours:.1f} h ago "
            f"(threshold {thresholds.scheduler_stale_hours:.0f} h)"
        ),
        remedy="The scheduler keeps no heartbeat, so this only measures "
        "completed runs. Check the scheduler process and its logs.",
        value=hours,
    )


@_safe("missed_run", "Scheduled run not missed")
def _check_missed_run(
    status: Source, thresholds: HealthThresholds, now: pd.Timestamp
) -> Check:
    state = status.value or {}
    due = state.get("next_run")
    last = state.get("last_run")

    if due is None:
        return Check(
            key="missed_run", title="Scheduled run not missed", triggered=False,
            severity="warning",
            detail="no scheduled event computed (a 24/7 universe has none)",
            remedy="",
        )

    overdue = (now - due).total_seconds() / 60.0
    missed = overdue > thresholds.missed_run_grace_minutes and (
        last is None or last < due
    )
    return Check(
        key="missed_run",
        title="Scheduled run not missed",
        triggered=missed,
        detail=(
            f"a run was due at {due:%Y-%m-%d %H:%M UTC} "
            f"({overdue:.0f} min ago) and has not completed"
            if missed
            else f"next run due {due:%Y-%m-%d %H:%M UTC}"
        ),
        remedy="Check that the scheduler process is alive and the machine "
        "clock is correct.",
        value=due,
    )


@_safe("pipeline_ok", "Last pipeline run succeeded")
def _check_pipeline(summary: Source) -> Check:
    record = summary.value or {}
    status = record.get("status")

    if summary.failed:
        return Check(
            key="pipeline_ok", title="Last pipeline run succeeded", triggered=True,
            detail=summary.error or "run summary unreadable",
            remedy="Inspect the run directory; the summary may be truncated.",
        )
    if not summary.available:
        return Check(
            key="pipeline_ok", title="Last pipeline run succeeded", triggered=True,
            detail=summary.note or "the pipeline has never run",
            remedy="Run the pipeline once.",
        )

    failed = status == "failed"
    return Check(
        key="pipeline_ok",
        title="Last pipeline run succeeded",
        triggered=failed,
        detail=(
            f"run {record.get('run_id', '?')} failed: {record.get('error') or 'no detail'}"
            if failed
            else f"run {record.get('run_id', '?')} finished with status {status!r}"
        ),
        remedy="Read the error on the Status page and the traceback in the logs.",
        value=status,
    )


@_safe("ingestion_ok", "Ingestion succeeded")
def _check_ingestion(lake: Source, summary: Source) -> Check:
    metadata = lake.value or {}
    coverage = metadata.get("coverage")

    failures: list[str] = []
    if isinstance(coverage, pd.DataFrame) and not coverage.empty:
        if "status" in coverage.columns:
            bad = coverage[coverage["status"].astype(str) != "ok"]
            failures.extend(f"{ticker}: {row}" for ticker, row in bad["status"].items())

    error = str((summary.value or {}).get("error") or "")
    if "ingest" in error.lower() or "download" in error.lower():
        failures.append(f"last run: {error}")

    return Check(
        key="ingestion_ok",
        title="Ingestion succeeded",
        triggered=bool(failures),
        detail="; ".join(failures[:5]) if failures else "no ingestion failures recorded",
        remedy="Re-run ingestion for the affected tickers and check the "
        "vendor response in the logs.",
        value=len(failures),
    )


@_safe("universe_covered", "Every configured instrument has data")
def _check_universe_coverage(config_source: Source, lake: Source) -> Check:
    """An instrument in the universe with no bars behind it.

    Adding a ticker to the configuration does not fetch its data. Until the
    next ingest it is a name the engine will look for, fail to find, and
    skip with a log line nobody is reading -- so the universe says twenty
    while nineteen are traded, and nothing anywhere says so.

    That is what happened when Caterpillar was added: the config changed at
    09:22, the daily ingest had run at 03:01, and the instrument had no bars
    at all until the next intraday pass hours later.

    It heals itself, which is exactly why it needs saying out loud. A gap
    that closes on its own is a gap nobody investigates, and the same shape
    of silence covers a ticker that was mistyped, delisted, or that the
    vendor has stopped serving -- none of which heal.
    """
    config = config_source.value
    wanted = [str(t) for t in getattr(getattr(config, "market", None), "tickers", ()) or ()]
    if not wanted:
        return Check(
            key="universe_covered", title="Every configured instrument has data",
            triggered=False, severity="warning",
            detail="no universe is configured", value=0,
        )

    coverage = (lake.value or {}).get("coverage")
    if not isinstance(coverage, pd.DataFrame) or coverage.empty:
        return Check(
            key="universe_covered", title="Every configured instrument has data",
            triggered=True, severity="warning",
            detail="the lake reported no coverage, so this could not be checked",
            remedy="Check that the lake is readable and that ingestion has run.",
            value=None,
        )

    have = {str(name) for name in coverage.index}
    missing = [t for t in wanted if t not in have]
    return Check(
        key="universe_covered",
        title="Every configured instrument has data",
        triggered=bool(missing),
        severity="warning",
        detail=(
            f"{len(missing)} of {len(wanted)} configured instruments have no "
            f"data in the lake: {', '.join(missing[:6])}"
            + ("…" if len(missing) > 6 else "")
            if missing
            else f"all {len(wanted)} configured instruments have data"
        ),
        remedy="Run ingestion for the missing tickers, or remove them from "
        "config.market.tickers if they were added in error.",
        value=len(missing),
    )


@_safe("execution_ok", "Execution engine clean")
def _check_execution(summary: Source, errors: Source) -> Check:
    reasons: list[str] = []

    error = str((summary.value or {}).get("error") or "")
    if error and any(
        word in error.lower() for word in ("execution", "backtest", "engine")
    ):
        reasons.append(f"last run: {error}")

    for line in (errors.value or [])[-200:]:
        if "cfdinvest.execution" in line and (
            "ERROR" in line or "CRITICAL" in line or "Traceback" in line
        ):
            reasons.append(line.strip()[:200])

    return Check(
        key="execution_ok",
        title="Execution engine clean",
        triggered=bool(reasons),
        detail="; ".join(reasons[:3]) if reasons else "no execution errors recorded",
        remedy="Read the traceback on the Logs page. An engine exception "
        "means the positions and P&L shown are incomplete.",
        value=len(reasons),
    )


@_safe("stops_ok", "Stop / take-profit fills computed")
def _check_stops(execution: Source) -> Check:
    parts = execution.value or {}
    trades = parts.get("trades")
    if not isinstance(trades, pd.DataFrame) or trades.empty:
        return Check(
            key="stops_ok", title="Stop / take-profit fills computed",
            triggered=False, detail="no trades to check", remedy="",
        )

    required = {"reason", "stop_fill_price", "take_profit_fill_price"}
    if not required.issubset(trades.columns):
        return Check(
            key="stops_ok", title="Stop / take-profit fills computed",
            triggered=False, severity="warning",
            detail="the trade log predates the stops module",
            remedy="Re-run the pipeline to record stop detail.",
        )

    stopped = trades[trades["reason"] == "stop_loss"]
    targeted = trades[trades["reason"] == "take_profit"]
    broken = int(
        stopped["stop_fill_price"].isna().sum()
        + targeted["take_profit_fill_price"].isna().sum()
    )

    return Check(
        key="stops_ok",
        title="Stop / take-profit fills computed",
        triggered=broken > 0,
        detail=(
            f"{broken} stop/target exit(s) recorded no fill price"
            if broken
            else f"{len(stopped) + len(targeted)} level exit(s), all priced"
        ),
        remedy="A level exit with no fill means the position was closed at "
        "an unknown price; the P&L for those trades cannot be trusted.",
        value=broken,
    )


@_safe("fx_ok", "FX conversion succeeded")
def _check_fx(execution: Source, summary: Source) -> Check:
    parts = execution.value or {}
    trades = parts.get("trades")
    metrics = (summary.value or {}).get("metrics") or {}

    if not isinstance(trades, pd.DataFrame) or trades.empty:
        return Check(
            key="fx_ok", title="FX conversion succeeded", triggered=False,
            detail="no trades to check", remedy="",
        )
    if "fx_rate" not in trades.columns:
        return Check(
            key="fx_ok", title="FX conversion succeeded", triggered=False,
            severity="warning",
            detail="the trade log predates the FX module",
            remedy="Re-run the pipeline to record FX detail.",
        )

    rates = pd.to_numeric(trades["fx_rate"], errors="coerce")
    unusable = int((~np.isfinite(rates)).sum() + (rates <= 0).sum())

    reasons = []
    if unusable:
        reasons.append(f"{unusable} trade(s) have no usable FX rate")
    error = str((summary.value or {}).get("error") or "")
    if "fx" in error.lower() or "currency" in error.lower():
        reasons.append(f"last run: {error}")
    if metrics.get("total_fx") is not None and not np.isfinite(
        float(metrics.get("total_fx", 0.0) or 0.0)
    ):
        reasons.append("reported FX cost is not a finite number")

    return Check(
        key="fx_ok",
        title="FX conversion succeeded",
        triggered=bool(reasons),
        detail="; ".join(reasons) if reasons else "every trade converted at a valid rate",
        remedy="A missing rate means that trade's price is in the wrong "
        "currency. Check fx_rates and the FX series in the lake.",
        value=unusable,
    )


@_safe("disk", "Disk space")
def _check_disk(disk: Source, thresholds: HealthThresholds) -> Check:
    usage = disk.value or {}
    if disk.failed or not usage:
        return Check(
            key="disk", title="Disk space", triggered=True,
            detail=disk.error or "disk usage unavailable",
            remedy="Check the mount.",
        )

    free_gb = usage["free_gb"]
    fraction = usage["free_fraction"]
    low = free_gb < thresholds.disk_free_gb or fraction < thresholds.disk_free_fraction

    return Check(
        key="disk",
        title="Disk space",
        triggered=low,
        detail=(
            f"{free_gb:.1f} GB free ({fraction:.1%}); thresholds "
            f"{thresholds.disk_free_gb:.0f} GB / "
            f"{thresholds.disk_free_fraction:.0%}"
        ),
        remedy="Prune old run directories (RunConfig.keep_runs) or grow the "
        "volume. A full disk stops the lake updating without saying so.",
        value=free_gb,
    )


def _plural(count: float, noun: str) -> str:
    """``"1 day"`` / ``"2 days"``.

    Deliberately local rather than imported from ``components``: that module
    imports this one, and a check that formats a string is not worth a cycle.
    """
    whole = round(float(count))
    return f"{whole:,.0f} {noun}{'' if whole == 1 else 's'}"


@_safe("calendar_fresh", "Publication calendar freshness")
def _check_calendar(lake: Source, thresholds: HealthThresholds) -> Check:
    metadata = lake.value or {}
    age = metadata.get("publication_calendar_freshness")

    if age is None:
        return Check(
            key="calendar_fresh", title="Publication calendar freshness",
            triggered=False, severity="warning",
            detail="no macro publication calendar in the lake",
            remedy="Only relevant if the strategy uses macro series.",
        )

    stale = age > thresholds.publication_calendar_stale_days
    return Check(
        key="calendar_fresh",
        title="Publication calendar freshness",
        triggered=stale,
        severity="warning",
        detail=(
            f"newest publication is {_plural(age, 'day')} old "
            f"(threshold {thresholds.publication_calendar_stale_days:.0f})"
        ),
        remedy="Re-ingest the macro series; stale publication dates make "
        "the look-ahead guard optimistic.",
        value=age,
    )


# --------------------------------------------------------------------------- #
# The whole report
# --------------------------------------------------------------------------- #
def run_checks(
    *,
    config_source: Source,
    lake: Source,
    scheduler_status: Source,
    summary: Source,
    execution: Source,
    errors: Source,
    disk: Source,
    thresholds: HealthThresholds | None = None,
    now: pd.Timestamp | None = None,
) -> HealthReport:
    """Every check, against already-read sources.

    Takes :class:`~cfdinvest.ui.sources.Source` objects rather than paths so
    the page reads each file once and the checks share those reads — and so
    a test can assemble any combination of broken states without a disk.
    """
    limits = thresholds or HealthThresholds()
    moment = utcnow(now)

    checks = (
        _check_config(config_source),
        _check_lake_readable(lake),
        _check_lake_freshness(lake, limits),
        _check_scheduler(scheduler_status, limits, moment),
        _check_missed_run(scheduler_status, limits, moment),
        _check_pipeline(summary),
        _check_ingestion(lake, summary),
        _check_universe_coverage(config_source, lake),
        _check_execution(summary, errors),
        _check_stops(execution),
        _check_fx(execution, summary),
        _check_disk(disk, limits),
        _check_calendar(lake, limits),
    )
    return HealthReport(checks=checks, checked_at=moment)
