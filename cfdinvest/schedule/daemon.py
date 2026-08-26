"""The long-running scheduler loop, and the pure decision behind it.

The loop itself is four lines of sleeping. Everything worth testing is in
:func:`jobs_due`, which takes a moment and the last time each job ran and
returns what is owed -- no clock, no sleeping, no I/O. A scheduler whose
decisions can only be observed by waiting is a scheduler nobody checks.

Why a daemon rather than more timers
------------------------------------
Because the jobs are ordered. The pipeline reads what ingestion wrote, and
the monitoring and risk snapshots describe the run that has just finished.
Expressing "five minutes after ingestion, unless ingestion failed" as
separate systemd timers means encoding the ordering in wall-clock guesses and
hoping. Here it is a sequence, and a failed ingestion is a reason not to run
the pipeline rather than a race nobody sees.

This supersedes ``cfdinvest-pipeline.timer`` and ``cfdinvest-monitor.timer``.
Running both would put two schedulers on one lake -- see
``deployment/systemd/README.md``.
"""

from __future__ import annotations

import logging
from typing import Mapping

import pandas as pd

logger = logging.getLogger(__name__)

__all__ = [
    "QUOTE_INTERVAL_MINUTES",
    "INGEST_AT_UTC",
    "RUN_AT_UTC",
    "JOBS",
    "jobs_due",
]

#: Every five minutes, matching the poller's own cadence.
QUOTE_INTERVAL_MINUTES = 5

#: Daily, in UTC, and in this order. The gap is deliberate: the pipeline must
#: read a lake that ingestion has finished writing.
INGEST_AT_UTC = (3, 0)
RUN_AT_UTC = (3, 5)

#: Every job this loop owns, in the order they are run when several are due.
#: The snapshots come last because they describe the run.
JOBS: tuple[str, ...] = (
    "poll-live-quotes",
    "advisor-alert",
    "paper-book",
    "ingest-daily",
    "run-daily",
    "monitoring-snapshot",
    "risk-snapshot",
    "prune-runs",
)


def _utc(value) -> pd.Timestamp | None:
    if value is None:
        return None
    try:
        stamp = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    return stamp.tz_localize("UTC") if stamp.tz is None else stamp.tz_convert("UTC")


def _due_daily(now: pd.Timestamp, last, at: tuple[int, int]) -> bool:
    """True when today's appointed time has passed and today's run has not.

    Keyed on the *date* of the last run rather than on elapsed hours. A
    daemon restarted at 03:30 has missed nothing if it already ran at 03:00,
    and has missed today's if it has not -- and an elapsed-time check gets
    exactly that case wrong.
    """
    hour, minute = at
    appointed = now.normalize() + pd.Timedelta(hours=hour, minutes=minute)
    if now < appointed:
        return False
    previous = _utc(last)
    return previous is None or previous < appointed


def jobs_due(now, last_run: Mapping[str, object] | None = None) -> list[str]:
    """Which jobs are owed at ``now``, in the order they should be run.

    Pure. ``last_run`` maps a job name to when it last completed; a job
    absent from it has never run, which makes it due.

    The two snapshots are owed only when the pipeline has just run, because
    they describe a run. Asking for a monitoring snapshot on a schedule of
    its own is what the superseded monitor timer did, and it produced
    snapshots describing nothing in particular.
    """
    moment = _utc(now)
    if moment is None:
        return []
    history = dict(last_run or {})
    due: list[str] = []

    quotes_last = _utc(history.get("poll-live-quotes"))
    if quotes_last is None or (
        (moment - quotes_last).total_seconds() >= QUOTE_INTERVAL_MINUTES * 60
    ):
        due.append("poll-live-quotes")
        # Straight after the poll, on the same cadence: the whole point of a
        # fresh price is noticing what it means before you are asked.
        due.append("advisor-alert")
        # The paper book on the same cadence, and for the same reason: a
        # forward record is only worth having if the decision was written
        # down at a moment it could actually have been acted on.
        due.append("paper-book")

    if _due_daily(moment, history.get("ingest-daily"), INGEST_AT_UTC):
        due.append("ingest-daily")
    if _due_daily(moment, history.get("run-daily"), RUN_AT_UTC):
        due.append("run-daily")

    if "run-daily" in due:
        due.append("monitoring-snapshot")
        due.append("risk-snapshot")
        # After the snapshots, so a run is only ever pruned once everything
        # that describes it has been written.
        due.append("prune-runs")

    return [job for job in JOBS if job in due]
