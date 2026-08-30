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
    "SNAPSHOT_AT_UTC",
    "SCREEN_AT_UTC",
    "CANDIDATE_SPREAD_AT_UTC",
    "MINUTE_INGEST_AT_UTC",
    "JOBS",
    "jobs_due",
]

#: Every five minutes, matching the poller's own cadence.
QUOTE_INTERVAL_MINUTES = 5

#: The daily top-up, in UTC. It remains even though the engine runs on hourly
#: bars, because it does something the intraday pass does not: refresh the
#: full daily history and the macro series. It is the once-a-day baseline,
#: not the trading cadence.
INGEST_AT_UTC = (3, 0)

#: The daily housekeeping: the two snapshots and the prune, five minutes
#: later so they describe a lake ingestion has finished writing.
#:
#: There used to be a ``run-daily`` here, a full pipeline run at 03:05, and
#: it was retired on 2026-08-27 because it could no longer do anything. A run
#: skips when every market in the universe is closed, and once the universe
#: became US-only, 03:05 UTC was always closed -- so it took 138ms, produced
#: nothing, and had been doing that since the universe narrowed. It was
#: redundant besides: ``run-intraday`` calls the identical command every
#: fifteen minutes while a market is open, and every run directory on the
#: server came from it.
#:
#: The snapshots stay daily. They describe a run rather than trigger one, and
#: taken on the intraday cadence they would be ninety operational records a
#: session saying the same thing.
SNAPSHOT_AT_UTC = (3, 5)

#: The universe screen, monthly, after the day's housekeeping.
#:
#: Monthly rather than daily because the answer barely moves: across
#: twenty-five monthly screens the selection changed by one or two names a
#: month and the ranking correlated at +0.97 to +0.99 between consecutive
#: runs. Daily would be twenty-odd identical records a month.
#:
#: And it records rather than acts. The screen has never been shown to
#: predict return -- it is a tradability filter -- so the job exists to
#: notice drift, not to rebalance. Changing the traded universe stays a
#: human act.
SCREEN_AT_UTC = (3, 10)

#: When the candidates' spreads are read. **Mid-session in New York, not on
#: the housekeeping appointment**, and the difference is the whole value of
#: the reading.
#:
#: It ran with the daily ingest at 03:00 UTC, which is 23:00 the previous
#: evening in New York. Every one of the first forty-one candidate readings
#: came back ``session_open=False``: US equities were shut, the quote was
#: the out-of-hours one, and an out-of-hours spread is several times the
#: session spread on a name nobody is making a market in. The screen filters
#: to core-session hours precisely so it does not rank on that, so the job
#: was accumulating a history that the thing it feeds would discard.
#:
#: 16:00 UTC is mid-session all year. New York is UTC-4 in summer and UTC-5
#: in winter, so the core session (10:00-15:00 local, matching
#: ``screen.SPREAD_HOURS``) covers 14:00-19:00 UTC and 15:00-20:00 UTC
#: respectively; 16:00 sits inside both and needs no daylight-saving
#: arithmetic of its own.
CANDIDATE_SPREAD_AT_UTC = (16, 0)

#: When minute bars are collected, and it is **after the close** rather than
#: during the session.
#:
#: The reasoning is the opposite of the spread job's. A spread must be read
#: while the market is open because it describes a live quote; a minute bar
#: is a completed fact, and fetching mid-session would collect a partial day
#: that the next run would have to refetch.
#:
#: 23:00 UTC is past the US close in both daylight-saving regimes -- 20:00
#: UTC in summer, 21:00 in winter -- with margin for late prints.
#:
#: Collected because it cannot be collected later. Minute history has the
#: shortest retention of any resolution the vendor serves, so a day not
#: fetched is permanently gone. It is *not* collected because it predicts:
#: measured over 34,818 decision points, no minute feature separates
#: positions worth closing from positions worth keeping. What it is for is
#: measuring execution quality -- the book's assumed fills against the
#: broker's actual ones -- and having the record if a later question needs
#: it.
MINUTE_INGEST_AT_UTC = (23, 0)

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
    "record-spreads",
    # The book decides, then the account mirrors it. This order is
    # load-bearing since 2026-08-28: `trade-capital` reads what the shadow
    # book holds rather than re-deriving it from the signals, so running it
    # first would mirror the previous pass's book and act one cycle late on
    # every entry and exit.
    "paper-book",
    "trade-capital",
    "ingest-daily",
    # After the daily ingest, before the snapshots: the screen's cost term
    # is an observation like any other and belongs with the housekeeping,
    # not on the trading cadence. The candidates are not traded, so nothing
    # needs their spread this minute -- only a history by the first of the
    # month.
    "record-candidate-spreads",
    "ingest-minute",
    "monitoring-snapshot",
    "risk-snapshot",
    "prune-runs",
    "screen-universe",
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


def _due_monthly(now: pd.Timestamp, last, at: tuple[int, int]) -> bool:
    """True when this calendar month's run has not happened yet.

    Keyed on the month rather than on elapsed days, for the same reason the
    daily check is keyed on the date: a daemon that was down on the first of
    the month has not missed the month, it is late, and an elapsed-time
    check would either skip it or fire it twice.

    The appointed time is on the first, but the test is "past it and not yet
    run this month" -- so a machine that was off for a week still screens
    when it comes back, once.
    """
    hour, minute = at
    appointed = (
        now.normalize().replace(day=1) + pd.Timedelta(hours=hour, minutes=minute)
    )
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

    The two snapshots and the prune are owed once a day, together and in
    that order. They describe the newest run rather than cause one, so they
    do not need a run of their own to hang off -- which is what they used to
    do, until the run they hung off stopped being able to happen.
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
            # On the same tick as the run, so a reading lines up with the
            # bar the engine decided on rather than falling between two.
            due.append("record-spreads")
            # The book, then the account. The book is also on the quote
            # cadence above, but the two cadences do not line up: a fresh
            # run can land on a tick where the quote poll is not due, and
            # the account would then mirror a book built from the *previous*
            # signals -- acting a cycle late on every entry and exit, which
            # is the failure this whole arrangement exists to avoid.
            #
            # Appending it twice is harmless: `jobs_due` returns
            # `[job for job in JOBS if job in due]`, so JOBS decides the
            # order and duplicates collapse.
            due.append("paper-book")
            # And then the account, last: it mirrors the book that run has
            # just fed, and the reading of what a trade costs is taken
            # before anything is placed rather than after.
            due.append("trade-capital")

    if _due_daily(moment, history.get("ingest-daily"), INGEST_AT_UTC):
        due.append("ingest-daily")

    # Once a day, not on the intraday cadence: the snapshots and the prune
    # describe a day, and taken every fifteen minutes they would be ninety
    # operational records a session saying the same thing. Keyed on the
    # monitoring snapshot because it is the first of the three -- and
    # because a prune that runs before the run it would prune is worse
    # than useless.
    # Once a day like the ingest, but on its own appointment, because what
    # it reads only exists while the exchange is open. Frequency was never
    # the problem -- one reading a day over a hundred names is plenty for a
    # ninety-day trailing median -- the hour was.
    #
    # Gated on the market being open as well as on the hour. Moving the job
    # into the session fixed the hour and not the day: on a Saturday every
    # hour is out of session, so it went on writing readings its only reader
    # discards -- two days in seven, plus holidays. ``market_open`` unknown
    # is treated as closed here for the same reason it is below: a reading
    # taken on a guess about whether the exchange was open is not a
    # measurement of anything.
    if market_open and _due_daily(
        moment, history.get("record-candidate-spreads"), CANDIDATE_SPREAD_AT_UTC
    ):
        due.append("record-candidate-spreads")

    # Deliberately NOT gated on the market being open: this runs after the
    # close, so by construction it is never due while trading. Gating it on
    # an open market would mean it never ran at all.
    if _due_daily(moment, history.get("ingest-minute"), MINUTE_INGEST_AT_UTC):
        due.append("ingest-minute")

    if _due_daily(moment, history.get("monitoring-snapshot"), SNAPSHOT_AT_UTC):
        due.append("monitoring-snapshot")
        due.append("risk-snapshot")
        # After the snapshots, so a run is only ever pruned once everything
        # that describes it has been written.
        due.append("prune-runs")

    # Daily since 2026-08-29, having been monthly.
    #
    # Monthly was right while the selection was only a report: consecutive
    # rankings correlate +0.97 to +0.99, so daily meant twenty near-identical
    # records a month and the drift they exist to show buried among them.
    #
    # ``universe.follow_screen`` made the selection an instruction as well as
    # a record, and the two want opposite things. As an instruction, monthly
    # means the traded list is up to a month behind the conditions it was
    # chosen for, and a configuration change leaves a record of the wrong
    # size standing until the next screen -- a window that opened for real
    # today. Cadence costs nothing in return: measured over 3.65 years,
    # daily returned 74.4% against monthly's 74.9%, well inside the 14.6pp
    # that changing only the *start day* of a monthly cadence moves it.
    #
    # The legibility objection was answered where it belongs, at the read
    # side: observations stay append-only and ``universe history`` collapses
    # runs of identical selections. Recording only changes would have been
    # the other fix and the wrong one -- it loses the evidence that the
    # screen ran at all, which is most of what the record is for.
    if _due_daily(moment, history.get("screen-universe"), SCREEN_AT_UTC):
        due.append("screen-universe")

    return [job for job in JOBS if job in due]
