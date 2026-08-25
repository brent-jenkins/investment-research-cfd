"""System vitals: what the scheduler owes, and whether it has delivered.

The daemon's own record of what it last ran lives in memory and dies with the
process, so this does not ask it. It asks the **artefacts**: each job writes
something, and the newest one it wrote is when it last genuinely ran. That
survives a restart, and it cannot claim success for a job that failed
half-way -- which an in-memory timestamp, written whether the job worked or
not, happily would.

Everything here is pure with respect to a clock: :func:`job_report` takes the
evidence and ``now`` and returns rows. Gathering the evidence is a separate
function, so the table can be tested without a lake.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping

import pandas as pd

logger = logging.getLogger(__name__)

__all__ = ["JobRow", "SCHEDULES", "job_report", "gather_evidence"]


@dataclass(frozen=True, slots=True)
class JobRow:
    """One scheduled job, and whether it is keeping up."""

    name: str
    schedule: str
    last_run: Any = None
    next_due: Any = None
    status: str = "unchecked"
    detail: str = ""

    @property
    def overdue(self) -> bool:
        return self.status in ("warning", "critical")


#: What each job is for, and how often it should happen. The cadences are
#: read from the daemon rather than restated, so a schedule changed there
#: cannot leave this page describing the old one.
SCHEDULES: tuple[tuple[str, str, str], ...] = (
    ("poll-live-quotes", "quotes", "Current price for every open position"),
    ("ingest-daily", "daily", "Top up the lake from the vendors"),
    ("run-daily", "daily", "Signals, execution and P&L over the lake"),
    ("monitoring-snapshot", "after-run", "Operational health, recorded"),
    ("risk-snapshot", "after-run", "The book's risk, recorded"),
)


def _utc(value) -> pd.Timestamp | None:
    if value is None:
        return None
    try:
        stamp = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(stamp):
        return None
    return stamp.tz_localize("UTC") if stamp.tz is None else stamp.tz_convert("UTC")


def _next_daily(now: pd.Timestamp, at: tuple[int, int]) -> pd.Timestamp:
    hour, minute = at
    today = now.normalize() + pd.Timedelta(hours=hour, minutes=minute)
    return today if now < today else today + pd.Timedelta(days=1)


def job_report(
    evidence: Mapping[str, Any],
    *,
    now=None,
    quotes_expected: bool = True,
) -> list[JobRow]:
    """One row per scheduled job. Pure.

    ``evidence`` maps a job name to when it last produced its artefact.
    ``None`` means it never has, which is reported as such rather than as a
    failure -- a deployment on its first morning has not failed at anything.

    ``quotes_expected`` is ``False`` when no position is confirmed open. The
    poller has nothing to price then, so silence is correct and reporting it
    overdue would train you to ignore this table.
    """
    from cfdinvest.schedule import daemon

    moment = _utc(now) or pd.Timestamp.now(tz="UTC")
    rows: list[JobRow] = []

    for name, kind, purpose in SCHEDULES:
        last = _utc(evidence.get(name))

        if kind == "quotes":
            interval = daemon.QUOTE_INTERVAL_MINUTES
            schedule = f"Every {interval} minutes"
            if not quotes_expected:
                rows.append(JobRow(
                    name, schedule, last, None, "pass",
                    "Nothing is confirmed open, so there is nothing to price.",
                ))
                continue
            next_due = None if last is None else last + pd.Timedelta(minutes=interval)
            if last is None:
                status, detail = "unchecked", "No quote has been taken yet."
            else:
                age = (moment - last).total_seconds() / 60.0
                # Three missed polls. One is a hiccup; three is the poller
                # not running -- the same limit the helper uses to decide a
                # quote is too old to price with.
                limit = interval * 3
                status = "pass" if age <= limit else "warning"
                detail = (
                    f"Last quote {age:.0f} minutes ago."
                    + ("" if status == "pass" else f" Expected within {limit:.0f}.")
                )

        elif kind == "daily":
            at = (
                daemon.INGEST_AT_UTC if name == "ingest-daily" else daemon.RUN_AT_UTC
            )
            schedule = f"Daily at {at[0]:02d}:{at[1]:02d} UTC"
            next_due = _next_daily(moment, at)
            if last is None:
                status, detail = "unchecked", "Has not run yet."
            else:
                hours = (moment - last).total_seconds() / 3600.0
                # A day and a half: a missed slot is visible, and an ordinary
                # run finishing late is not called a failure.
                status = "pass" if hours <= 36 else "warning"
                detail = f"Last ran {hours:.0f} hours ago."

        else:  # after-run
            schedule = "After each pipeline run"
            next_due = None
            produced = _utc(evidence.get("run-daily"))
            if last is None:
                status, detail = "unchecked", "Nothing recorded yet."
            elif produced is not None and last < produced:
                # The snapshot describes a run. Older than the newest run
                # means the run finished and the snapshot did not follow.
                status = "warning"
                detail = "Older than the most recent run."
            else:
                status = "pass"
                detail = f"Recorded {components_age(moment, last)}."

        rows.append(JobRow(name, schedule, last, next_due, status, detail))

    return rows


def components_age(now: pd.Timestamp, then: pd.Timestamp) -> str:
    """A short relative age, for a row that already shows the timestamp."""
    seconds = max((now - then).total_seconds(), 0.0)
    if seconds < 5400:
        return f"{seconds / 60:.0f} minutes ago"
    if seconds < 172_800:
        return f"{seconds / 3600:.0f} hours ago"
    return f"{seconds / 86_400:.0f} days ago"


def gather_evidence(config=None, account: str | None = None) -> dict[str, Any]:
    """When each job last produced its artefact. Never raises.

    Every lookup is guarded separately: one unreadable store should cost that
    row its timestamp, not the whole table.
    """
    evidence: dict[str, Any] = dict.fromkeys(
        (name for name, _, _ in SCHEDULES), None
    )

    def guarded(name: str, read):
        try:
            evidence[name] = read()
        except Exception as error:  # noqa: BLE001 - one row, not the table
            logger.warning("Could not date %s: %s", name, error)

    def newest_quote():
        from cfdinvest.livequotes import storage as quote_store

        return (quote_store.load_quotes(config, account) or {}).get("timestamp")

    def newest_ingest():
        from cfdinvest.repository import DataRepository

        coverage = DataRepository(config).coverage()
        if coverage is None or coverage.empty or "updated_at" not in coverage:
            return None
        return pd.to_datetime(coverage["updated_at"], utc=True, errors="coerce").max()

    def newest_run():
        from cfdinvest.persist import readers

        latest = readers.load_latest_run(config, account=account)
        # The run identifier is its own timestamp: 20260825T150644Z.
        return None if latest is None else pd.to_datetime(
            latest, format="%Y%m%dT%H%M%SZ", utc=True, errors="coerce"
        )

    def newest_monitoring():
        from cfdinvest.persist import monitoring as store

        document = store.load_latest_monitoring_snapshot(config, account=account)
        return (document or {}).get("timestamp")

    def newest_risk():
        from cfdinvest.persist import risk as store

        document = store.load_latest_risk_snapshot(config, account=account)
        return (document or {}).get("timestamp")

    guarded("poll-live-quotes", newest_quote)
    guarded("ingest-daily", newest_ingest)
    guarded("run-daily", newest_run)
    guarded("monitoring-snapshot", newest_monitoring)
    guarded("risk-snapshot", newest_risk)
    return evidence
