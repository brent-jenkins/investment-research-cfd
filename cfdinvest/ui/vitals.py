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
#: Every job, in the order the daemon runs them.
#:
#: This list and ``daemon.JOBS`` are two statements of the same fact, and a
#: test fails the build when they disagree. They already had: paper-book and
#: advisor-alert ran on the box every five minutes and appeared on no status
#: page, so the forward record looked stopped while it was working. A job
#: absent from this table is a job nobody is watching.
SCHEDULES: tuple[tuple[str, str, str], ...] = (
    ("poll-live-quotes", "quotes", "Current price for every open position"),
    ("advisor-alert", "cadence", "Compare the book against the newest prices"),
    ("ingest-intraday", "session", "Keep the hourly bars current while open"),
    ("run-intraday", "session", "Fresh signals every quarter hour while open"),
    ("record-spreads", "session", "What a trade costs right now, appended"),
    # Runs as `cfdinvest-dealer.service` rather than inside the daemon, so
    # its evidence comes from what the DEALING PASS writes rather than from
    # the daemon's job history -- see `newest_dealing` below. Keeping the row
    # matters more than where it is scheduled: dealing vanishing from this
    # table is precisely the "job nobody is watching" failure above.
    ("trade-capital", "session", "Place, resize and close on the demo account"),
    ("ingest-daily", "daily", "Top up the lake from the vendors"),
    ("record-candidate-spreads", "daily",
     "What the screen's candidates cost to trade, once a day"),
    ("monitoring-snapshot", "daily", "Operational health, recorded"),
    ("risk-snapshot", "daily", "The book's risk, recorded"),
    ("verify-day", "daily",
     "Did the system do what it said it would, yesterday"),
    ("prune-runs", "daily", "Bound the artefact store"),
    ("ingest-minute", "daily",
     "Minute bars after the close, for everything ever selected"),
    ("record-client-sentiment", "every 30 minutes",
     "Record Capital's client positioning, which the broker keeps no history of"),
    ("record-headlines", "hourly",
     "Record headlines with the time first seen, because the feed keeps no history"),
    ("screen-universe", "daily", "Rank the pool and record the selection"),
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


def _next_daily(
    now: pd.Timestamp, at: tuple[int, int], *, weekdays_only: bool = False
) -> pd.Timestamp:
    """The next time this appointment comes round.

    ``weekdays_only`` for a job gated on the market being open: Saturday's
    16:00 is not a time it can run, so offering it as "next due" is telling
    the reader something that will not happen.
    """
    hour, minute = at
    today = now.normalize() + pd.Timedelta(hours=hour, minutes=minute)
    following = today if now < today else today + pd.Timedelta(days=1)
    if weekdays_only:
        # At most two skips: Saturday and Sunday.
        while following.dayofweek >= 5:
            following = following + pd.Timedelta(days=1)
    return following


def _weekday_appointments_between(
    last: pd.Timestamp, now: pd.Timestamp, at: tuple[int, int]
) -> int:
    """How many weekday slots have passed since ``last`` without a run.

    The staleness measure for a market-gated job. Hours since the last run
    counts weekends the job was never allowed to work, so it condemns a job
    for the calendar rather than for its behaviour.

    Weekday rather than a real exchange calendar, deliberately: a holiday
    costs this one missed slot out of the two it tolerates, which reads as
    slack rather than as a false alarm. Reaching for the calendar here would
    put a second copy of the session rules in the UI layer, which is the
    fault this whole change exists to remove.
    """
    hour, minute = at
    missed = 0
    day = pd.Timestamp(last).normalize()
    while day <= pd.Timestamp(now).normalize():
        slot = day + pd.Timedelta(hours=hour, minutes=minute)
        if day.dayofweek < 5 and last < slot <= now:
            missed += 1
        day = day + pd.Timedelta(days=1)
    return missed


def job_report(
    evidence: Mapping[str, Any],
    *,
    now=None,
    quotes_expected: bool = True,
    session_open: bool = True,
) -> list[JobRow]:
    """One row per scheduled job. Pure.

    ``evidence`` maps a job name to when it last produced its artefact.
    ``None`` means it never has, which is reported as such rather than as a
    failure -- a deployment on its first morning has not failed at anything.

    ``quotes_expected`` is ``False`` when the engine holds nothing. The
    poller has nothing to price then, so silence is correct and reporting it
    overdue would train you to ignore this table.

    ``session_open`` is ``False`` outside market hours. The intraday jobs run
    only while a market is open, so an overnight gap is the system working
    and calling it a warning would cry wolf every morning. Passed in rather
    than looked up, so this function stays pure.
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
                    "The engine holds nothing, so there is nothing to "
                    "price.",
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

        elif kind == "session":
            # Only while a market is open, so silence overnight is correct
            # rather than a fault. Saying so matters: a row reading "last ran
            # 14 hours ago" at 06:00 is a working system, and a status page
            # that called that a warning would be crying wolf every morning.
            from cfdinvest.schedule import daemon as daemon_module

            every = daemon_module.INTRADAY_MINUTES
            schedule = f"Every {every} minutes while open"
            next_due = None
            if last is None:
                status, detail = "unchecked", "Has not run yet."
            else:
                age = (moment - last).total_seconds() / 60.0
                if not session_open:
                    status = "pass"
                    detail = f"Markets closed. Last ran {components_age(moment, last)}."
                else:
                    limit = every * 3
                    status = "pass" if age <= limit else "warning"
                    detail = (
                        f"Last ran {age:.0f} minutes ago."
                        + ("" if status == "pass" else f" Expected within {limit:.0f}.")
                    )

        elif kind == "cadence":
            # Same cadence as the quotes, because the daemon queues them off
            # the same tick -- but no "nothing is open" excuse. The paper
            # book has work to do whether or not anything is currently held:
            # deciding not to open is a decision, and its absence from this
            # table is what made the forward record look stopped.
            schedule = f"Every {interval} minutes"
            next_due = None if last is None else last + pd.Timedelta(minutes=interval)
            if last is None:
                status, detail = "unchecked", "Has not run yet."
            else:
                age = (moment - last).total_seconds() / 60.0
                limit = interval * 3
                status = "pass" if age <= limit else "warning"
                detail = (
                    f"Last ran {age:.0f} minutes ago."
                    + ("" if status == "pass" else f" Expected within {limit:.0f}.")
                )

        elif kind == "monthly":
            # Its own rule, not the daily one. A monthly job judged on the
            # daily threshold would report "last ran 700 hours ago" as a
            # warning every day of its life, which is how a status page
            # teaches you to stop reading it.
            at = daemon.SCREEN_AT_UTC
            schedule = f"Monthly, 1st at {at[0]:02d}:{at[1]:02d} UTC"
            next_due = None
            if last is None:
                status, detail = "unchecked", "Has not run yet."
            else:
                days = (moment - last).total_seconds() / 86_400.0
                # Thirty-five: a month plus the slack for a machine that was
                # off over a month end, which is late rather than broken.
                status = "pass" if days <= 35 else "warning"
                detail = f"Last ran {days:.0f} days ago."

        else:  # daily
            # The snapshots and the prune used to be timed against the daily
            # pipeline run: older than the newest run meant the run finished
            # and the snapshot did not follow. That test died with the job it
            # keyed on -- signals now refresh every fifteen minutes, so a
            # once-a-day snapshot is *always* older than the newest run and
            # the check would warn every session about a scheduler doing
            # exactly what it was told.
            # Read from `daemon`, never restated. This branch used to label
            # every daily job but `ingest-daily` with SNAPSHOT_AT_UTC, which
            # is right for three of the eight and wrong for three: the page
            # said `record-candidate-spreads` runs at 03:05 and was next due
            # tomorrow morning, when it runs at 16:00 and was due that
            # afternoon. A second copy of a schedule is a schedule that
            # drifts.
            at = daemon.DAILY_APPOINTMENTS.get(name, daemon.SNAPSHOT_AT_UTC)
            gated = name in daemon.MARKET_GATED_DAILY
            schedule = (
                f"Daily at {at[0]:02d}:{at[1]:02d} UTC"
                + (" while open" if gated else "")
            )
            next_due = _next_daily(moment, at, weekdays_only=gated)
            if last is None:
                status, detail = "unchecked", "Has not run yet."
            elif gated:
                # A market-gated job cannot run at a weekend, so an hour
                # count judges it against a clock it does not live on. From
                # Friday 16:00 to Monday 16:00 is SEVENTY-TWO hours of
                # blameless staleness against a thirty-six hour rule, so it
                # warned about a third of every week for doing as it was
                # told -- which is how a status page teaches you to stop
                # reading it, the same lesson the monthly branch above
                # already carries.
                #
                # Counted in APPOINTMENTS MISSED instead: how many weekday
                # slots have come and gone since it last ran. One is slack
                # for a late or failed run; two means it is genuinely not
                # running.
                missed = _weekday_appointments_between(last, moment, at)
                status = "pass" if missed <= 1 else "warning"
                hours = (moment - last).total_seconds() / 3600.0
                detail = (
                    f"Last ran {hours:.0f} hours ago"
                    + (
                        "; the market has not been open at "
                        f"{at[0]:02d}:{at[1]:02d} UTC since."
                        if missed == 0
                        else f"; {missed} weekday slot(s) missed."
                    )
                )
            else:
                hours = (moment - last).total_seconds() / 3600.0
                # A day and a half: a missed slot is visible, and an ordinary
                # run finishing late is not called a failure.
                status = "pass" if hours <= 36 else "warning"
                detail = f"Last ran {hours:.0f} hours ago."

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

    def newest_dealing():
        """When the dealing pass last EXAMINED the account.

        Read from `persist.margin`, which the dealing path writes on every
        pass -- including one that looks at the book and correctly does
        nothing. That is what makes it a liveness signal rather than an
        activity signal, and it is the same source `check_dealing_liveness`
        uses.

        The daemon's job history cannot answer this any more: dealing runs
        in its own unit and never writes there. Leaving it on that source
        would have left this row reading "never run" for ever, which is
        worse than no row at all.
        """
        from cfdinvest.persist import margin as margin_store

        rows = [
            row for row in margin_store.load_margin(config, account)
            if str(row.get("kind") or "") == "usage" and row.get("at")
        ]
        return rows[-1]["at"] if rows else None

    guarded("trade-capital", newest_dealing)
    guarded("poll-live-quotes", newest_quote)
    guarded("ingest-daily", newest_ingest)
    guarded("monitoring-snapshot", newest_monitoring)
    guarded("risk-snapshot", newest_risk)

    # The intraday run writes a run directory, but so did the daily one
    # while it existed, and the newest directory could not say which made
    # it -- which reported the intraday run's time under the daily row. The
    # daily run is retired, so only one job writes runs now; it is still
    # dated by the daemon's record rather than by the directory, because
    # what is being asked is "did the job run", not "does an artefact exist".

    # The rest have no artefact of their own to date. The paper book writes a
    # file only when it opens something, and "decided not to open" is a run
    # that happened -- dating it by its output would report the job as never
    # having run for as long as it correctly declined to trade.
    #
    # So the daemon's own record is the evidence, which is why it is now
    # persisted rather than held in a local variable.
    def recorded(job: str):
        def read():
            from cfdinvest.persist import scheduler_state

            return (scheduler_state.last_job_runs(config, account) or {}).get(job)

        return read

    guarded("run-intraday", recorded("run-intraday"))

    # Everything else falls back to the daemon's record only when it wrote
    # no artefact to be dated by -- the paper book writes a file solely when
    # it opens something, and declining to trade is a run that happened.
    for job in evidence:
        if evidence.get(job) is None:
            guarded(job, recorded(job))
    return evidence
