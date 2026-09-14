"""The status page reports the schedule the scheduler actually keeps.

Two faults, found by the owner reading the page on a Monday lunchtime and
asking whether a warning was expected. It was expected, and the page around
it was wrong.

**THE SCHEDULE WAS A SECOND COPY, AND IT HAD DRIFTED.** `job_report` labelled
every daily row but `ingest-daily` with `SNAPSHOT_AT_UTC`, which is right for
three of the eight and wrong for three:

    record-candidate-spreads   runs 16:00 UTC   page said 03:05
    ingest-minute              runs 23:00 UTC   page said 03:05
    screen-universe            runs 03:10 UTC   page said 03:05

So the page told the owner a job would next run at 03:05 tomorrow when it was
really due at 16:00 that afternoon. Not a crash -- a page confidently wrong
about the one thing it exists to report. `daemon.DAILY_APPOINTMENTS` is now
the single owner and both sides read it.

**AND THE WARNING COULD NOT BE SATISFIED.** `record-candidate-spreads` is
gated on the market being open as well as on the hour, so it last runs Friday
16:00 and cannot run again until Monday 16:00. That is SEVENTY-TWO hours,
judged by a rule that warns past thirty-six -- a warning from Saturday
morning to Monday teatime, every week, about a job doing exactly as it was
told. Roughly a third of the time.

The monthly branch of this same function already carries the lesson in its
own comment: a job judged on the wrong cadence "would report 'last ran 700
hours ago' as a warning every day of its life, which is how a status page
teaches you to stop reading it". The daily branch had the identical fault one
`elif` away.

Counted in weekday APPOINTMENTS MISSED instead, the reading means something:

    moment                        old rule   new rule   hours  missed
    Friday 17:00, just after      pass       pass         1      0
    Sunday 12:00                  WARNING    pass        44      0
    Monday 12:40, before the slot WARNING    pass        69      0
    Monday 16:30, slot missed     WARNING    pass        72      1
    Wednesday, still failing      WARNING    WARNING    116      2

Mutations checked:

  M1700 the daily label restated instead of read from `daemon`
  M1701 a market-gated job judged on elapsed hours again
  M1702 `next_due` offering a weekend slot a gated job cannot use
  M1703 the missed-slot count ignoring whether the day is a weekday
  M1704 the slack widened so a genuinely dead job never warns
  M1706 the spread job's appointment moved back out of the US session

One mutation is NOT reachable and is recorded rather than faked. Pointing
`DAILY_APPOINTMENTS["record-candidate-spreads"]` at the wrong constant
changes nothing any comparison here can see, because `jobs_due` and the
page now both read that map -- a single owner cannot drift from itself,
which is the whole point of making it one. What remains testable is the
job's PURPOSE, and that is M1706: the reading only means anything taken
while New York is trading, which is why it moved off 03:00 in the first
place.
"""
from __future__ import annotations

import pandas as pd
import pytest

from cfdinvest.schedule import daemon
from cfdinvest.ui import vitals

FRIDAY_RUN = pd.Timestamp("2026-09-11 16:00", tz="UTC")
SPREADS = "record-candidate-spreads"


def _rows(now, evidence):
    return {row.name: row for row in vitals.job_report(evidence, now=now)}


def test_the_page_reports_each_jobs_real_appointment() -> None:
    """M1700: the page and the scheduler disagreeing about when a job runs.

    Asserted against `daemon.DAILY_APPOINTMENTS`, which `jobs_due` itself
    reads, so the two cannot drift apart again without this failing.
    """
    now = pd.Timestamp("2026-09-14 12:40", tz="UTC")
    evidence = dict.fromkeys((name for name, _, _ in vitals.SCHEDULES), now)
    rows = _rows(now, evidence)

    wrong = []
    for name, at in daemon.DAILY_APPOINTMENTS.items():
        row = rows.get(name)
        if row is None:
            continue
        expected = f"{at[0]:02d}:{at[1]:02d} UTC"
        if expected not in row.schedule:
            wrong.append(f"{name}: page says {row.schedule!r}, runs at {expected}")

    assert not wrong, (
        "the status page states a schedule the scheduler does not keep: "
        f"{wrong}"
    )


def test_a_market_gated_job_is_not_warned_about_for_the_weekend() -> None:
    """M1701: the exact reading the owner queried, and the days around it.

    A job that cannot run at a weekend must not be judged on a clock that
    counts weekends.
    """
    evidence = {name: None for name, _, _ in vitals.SCHEDULES}
    evidence[SPREADS] = FRIDAY_RUN

    for label, moment in (
        ("Saturday", pd.Timestamp("2026-09-12 12:00", tz="UTC")),
        ("Sunday", pd.Timestamp("2026-09-13 12:00", tz="UTC")),
        ("Monday before the 16:00 slot", pd.Timestamp("2026-09-14 12:40", tz="UTC")),
    ):
        row = _rows(moment, evidence)[SPREADS]
        assert row.status == "pass", (
            f"{label}: {SPREADS} reported {row.status!r} -- "
            f"{row.detail!r}. It is gated on an open market and has not had "
            "one, so there is nothing to warn about."
        )


def test_a_market_gated_job_that_really_stops_is_still_caught() -> None:
    """M1704: slack so wide the check stops being a check.

    The weekend must be forgiven and a dead job must not be. Two missed
    weekday slots is the line: one is a late or failed run, two is a job
    that is not running.
    """
    evidence = {name: None for name, _, _ in vitals.SCHEDULES}
    evidence[SPREADS] = FRIDAY_RUN

    # Monday's slot and Tuesday's both gone by.
    moment = pd.Timestamp("2026-09-16 12:00", tz="UTC")
    row = _rows(moment, evidence)[SPREADS]
    assert row.status == "warning", (
        f"two weekday slots missed and the page reports {row.status!r}. A "
        "threshold nothing can trip is not a safeguard."
    )


def test_the_next_slot_offered_is_one_the_job_could_use() -> None:
    """M1702: telling the reader a gated job runs on Saturday.

    Called on a Friday evening, so the naive answer is Saturday 16:00 -- a
    time the market is shut and the job cannot run.
    """
    friday_evening = pd.Timestamp("2026-09-11 18:00", tz="UTC")
    at = daemon.DAILY_APPOINTMENTS[SPREADS]

    gated = vitals._next_daily(friday_evening, at, weekdays_only=True)
    assert gated.dayofweek < 5, (
        f"next due {gated}, which is a {gated.day_name()} -- the market is "
        "shut and the job cannot run then"
    )
    assert gated == pd.Timestamp("2026-09-14 16:00", tz="UTC")

    # The ungated helper is unchanged, so ordinary daily jobs still read
    # tomorrow rather than skipping a weekend they are happy to work.
    plain = vitals._next_daily(friday_evening, at)
    assert plain == pd.Timestamp("2026-09-12 16:00", tz="UTC")


def test_missed_slots_are_counted_on_weekdays_only() -> None:
    """M1703: counting weekend slots, which puts the old fault back."""
    at = daemon.DAILY_APPOINTMENTS[SPREADS]
    monday_before = pd.Timestamp("2026-09-14 12:40", tz="UTC")

    assert vitals._weekday_appointments_between(FRIDAY_RUN, monday_before, at) == 0, (
        "Saturday and Sunday 16:00 were counted as slots the job missed"
    )
    # Monday's slot has now gone by, and it is one.
    monday_after = pd.Timestamp("2026-09-14 16:30", tz="UTC")
    assert vitals._weekday_appointments_between(FRIDAY_RUN, monday_after, at) == 1


def test_the_ungated_daily_jobs_are_unchanged() -> None:
    """The fix must not quietly relax the jobs that CAN run at a weekend.

    `ingest-minute` is deliberately not market-gated -- a minute bar is a
    completed fact -- so a weekend is no excuse for it.
    """
    now = pd.Timestamp("2026-09-14 12:00", tz="UTC")
    evidence = {name: None for name, _, _ in vitals.SCHEDULES}
    evidence["ingest-minute"] = pd.Timestamp("2026-09-12 23:00", tz="UTC")

    row = _rows(now, evidence)["ingest-minute"]
    assert row.status == "warning", (
        "ingest-minute is 37 hours stale and is not market-gated, so it "
        "should still be warned about"
    )


def test_the_appointments_are_when_the_scheduler_really_becomes_due() -> None:
    """M1705: the map itself drifting, which the comparison above cannot see.

    Asserting that the page matches `DAILY_APPOINTMENTS` is a tautology once
    both read it -- a wrong entry moves both sides together and the check
    stays green. That mutation survived, so this asserts the PROPERTY
    instead: at one minute before its appointment a job is not owed, and at
    one minute after it is. `jobs_due` is pure, so this is the scheduler's
    real answer rather than a restatement of the table.
    """
    for name, at in daemon.DAILY_APPOINTMENTS.items():
        gated = name in daemon.MARKET_GATED_DAILY
        # A Wednesday, so a weekday for the gated job, and late enough in the
        # day that every appointment has a "before" on the same date.
        day = pd.Timestamp("2026-09-16", tz="UTC")
        slot = day + pd.Timedelta(hours=at[0], minutes=at[1])

        before = daemon.jobs_due(
            slot - pd.Timedelta(minutes=1), {}, market_open=gated or True
        )
        after = daemon.jobs_due(
            slot + pd.Timedelta(minutes=1), {}, market_open=gated or True
        )

        assert name not in before, (
            f"{name} is owed at {slot - pd.Timedelta(minutes=1)}, a minute "
            f"BEFORE the {at[0]:02d}:{at[1]:02d} the page advertises"
        )
        assert name in after, (
            f"{name} is not owed a minute after {at[0]:02d}:{at[1]:02d}, so "
            "the appointment the page shows is not the one the scheduler keeps"
        )


def test_the_spread_reading_is_taken_while_new_york_is_trading() -> None:
    """M1706: the appointment back outside the session, where it began.

    The only check on the appointment map that is not circular. Every other
    assertion here compares the page against the map, and `jobs_due` reads
    that map too -- so a wrong entry moves both sides together and nothing
    notices. That is a single owner working as intended, not a gap.

    What is independently true is why the job runs when it does. It ran at
    03:00 UTC, which is 23:00 the previous evening in New York, and every one
    of the first forty-one readings came back with the exchange shut -- an
    out-of-hours spread on a name nobody is making a market in, collected for
    a screen that filters those out. So the appointment must sit inside the
    core session in BOTH daylight-saving regimes.
    """
    from cfdinvest.universe.screen import SPREAD_HOURS

    hour, _minute = daemon.DAILY_APPOINTMENTS[SPREADS]
    low, high = SPREAD_HOURS  # local New York hours, the screen's own owner

    # New York is UTC-4 in summer and UTC-5 in winter, so the core session is
    # low+4..high+4 UTC and low+5..high+5 UTC. The appointment has to be in
    # both, or half the year is collected with the exchange shut.
    summer = range(low + 4, high + 4)
    winter = range(low + 5, high + 5)

    assert hour in summer and hour in winter, (
        f"the spread job runs at {hour:02d}:00 UTC, which is outside the "
        f"core session in {'summer' if hour not in summer else 'winter'}. "
        "The reading describes a live quote, so taken out of session it "
        "measures a market nobody is making."
    )
