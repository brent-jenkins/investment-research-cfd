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
#:
#: These remain even though the engine now runs on hourly bars, because they
#: do something the intraday pass does not: top up the full history and
#: refresh the macro series. They are the once-a-day baseline, not the
#: trading cadence.
INGEST_AT_UTC = (3, 0)
RUN_AT_UTC = (3, 5)

#: How often the lake and the signals are refreshed while a market is open.
#:
#: A daily pipeline on daily bars gave one decision per instrument per
#: session, which for a CFD is far too slow -- the position was chosen once
#: and then only watched. Four runs across forty minutes produced the same
#: two signals with confidence moving by 0.002, because re-reading an
#: unchanged daily bar cannot produce a different answer.
#:
#: Fifteen minutes against an hourly bar is deliberate overlap: it means a
#: bar that has just closed is acted on within a quarter of an hour rather
#: than up to an hour later, at the cost of some repeated work. The repeated
#: work is cheap; the missed move is not.
INTRADAY_MINUTES = 15

#: Every job this loop owns, in the order they are run when several are due.
#: The snapshots come last because they describe the run.
JOBS: tuple[str, ...] = (
    "poll-live-quotes",
    "advisor-alert",
    "ingest-intraday",
    "run-intraday",
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


def jobs_due(
    now,
    last_run: Mapping[str, object] | None = None,
    *,
    market_open: bool | None = None,
) -> list[str]:
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

    # The trading cadence: fresh bars, then fresh signals, while a market is
    # open. Ordered so the pipeline reads a lake ingestion has finished
    # writing -- the same reason the daily pair is ordered.
    #
    # ``market_open`` is passed in rather than looked up, so this stays pure
    # and testable. None means "not known", which is treated as closed: a
    # daemon that cannot tell the time should not be trading on the guess.
    if market_open:
        since = _utc(history.get("run-intraday"))
        if since is None or (
            (moment - since).total_seconds() >= INTRADAY_MINUTES * 60
        ):
            due.append("ingest-intraday")
            due.append("run-intraday")

    if _due_daily(moment, history.get("ingest-daily"), INGEST_AT_UTC):
        due.append("ingest-daily")
    if _due_daily(moment, history.get("run-daily"), RUN_AT_UTC):
        due.append("run-daily")

    # Deliberately the daily run only, not the intraday one. The snapshots
    # and the prune describe a day: taken every fifteen minutes they would
    # be ninety operational records a session saying the same thing, and a
    # prune that runs before the run it would prune is worse than useless.
    if "run-daily" in due:
        due.append("monitoring-snapshot")
        due.append("risk-snapshot")
        # After the snapshots, so a run is only ever pruned once everything
        # that describes it has been written.
        due.append("prune-runs")

    return [job for job in JOBS if job in due]
