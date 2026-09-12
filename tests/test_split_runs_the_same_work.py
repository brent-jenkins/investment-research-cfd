"""The four units do the same work the one loop did.

`tests/test_jobs_are_partitioned.py` proves every job has exactly one OWNER.
That is necessary and it is not sufficient: a job can be owned by a unit that
never becomes due, and the partition looks perfect while the work stops.

That is not hypothetical. It is what the split shipped with on 2026-09-11.
``ingest-intraday`` and ``run-intraday`` were both gated on ``run-intraday``'s
last-run time -- safe while ONE loop returned both from a single call and ran
them before either timestamp moved, and a race the moment they became two
processes asking independently every minute. Whichever asked first recorded
``run-intraday`` and closed the gate on the other for the whole cadence, so
ingestion stopped and the analysis went on reading the lake it had stopped
refreshing. Replayed over a session: **26 ingestions became 0**.

Nothing caught it. Every unit reported ``Result=success``; the partition was
exact; an idle tick was 0.76s. Success and doing nothing look identical from
outside, which is this project's oldest failure and the reason the check here
is behavioural rather than structural.

`jobs_due` is pure, so a whole day is replayed against it with no box, no
market and no lake: a clock and a last-run map in, what is owed out, and
running a job only moves a timestamp. Two properties are asserted.

  THE SAME WORK. Each job runs the same number of times under the split as
  under the loop. Counted over 1,440 minutes so the daily jobs and the
  five-minute quote cadence are both exercised, not just the trading one.

  THE SAME ORDER. The loop got "the pipeline reads a lake ingestion has
  finished writing" for free, by returning both jobs from one call. Across
  processes it has to be stated, so every ``run-intraday`` must be preceded
  by an ``ingest-intraday`` newer than the previous analysis.

Both are replayed under EVERY interleaving of the units within a minute,
because systemd does not order timers that fire together. A result that holds
only under a favourable order is not a result -- and the shipped bug passed
under exactly half of them.

Mutations checked:

  M1670 ingestion gated on ``run-intraday``'s clock again (the shipped bug):
        ingest-intraday 26 -> 0 in half the interleavings
  M1671 analysis gated on its own clock rather than on fresh ingestion:
        counts stay equal, the ORDER property fails
  M1672 the ingest unit's --only pointed at another job: its own never runs
  M1674 the whole intraday block ungated from ``market_open``: the counts
        rise on both sides, so the comparison alone would not catch it --
        asserted separately against the session length and the calendar

**A sixth mutation SURVIVED, and it belongs to another test.** Claiming
``ingest-intraday`` in two units as well does not change any count here:
each job now keys off its own last-run time, so whichever unit asks first
records it and the other finds nothing owed. That is the shared state file
doing its job in a simulation where the units take turns -- and it is not a
guarantee, because the real units are concurrent processes and two that ask
in the same second both see it due. This file cannot see that, and saying it
could would be worse than not checking: `test_jobs_are_partitioned.py`
(M1661) owns double-claiming, structurally, and is where that check belongs.
"""
from __future__ import annotations

import itertools
import re
from pathlib import Path

import pandas as pd
import pytest

from cfdinvest.schedule import daemon

SYSTEMD = Path(__file__).resolve().parents[1] / "deployment" / "systemd"

#: How often each unit asks "what is owed?", from the shipped timers. Read
#: here rather than assumed: a timer slowed to five minutes would halve the
#: trading cadence, and the counts are what would show it.
UNIT_TIMERS = (
    ("cfdinvest", "cfdinvest.timer"),
    ("cfdinvest-quotes", "cfdinvest-quotes.timer"),
    ("cfdinvest-ingest", "cfdinvest-ingest.timer"),
    ("cfdinvest-daily", "cfdinvest-daily.timer"),
)

OPEN_FROM = pd.Timedelta(hours=13, minutes=30)
OPEN_UNTIL = pd.Timedelta(hours=20)

#: A Monday, so the session is a real one rather than a weekend that would
#: make every intraday assertion vacuously true.
DAY = pd.Timestamp("2026-09-14 00:00", tz="UTC")


def _live(body: str) -> str:
    """The unit file with comments removed and continuations joined.

    A test that matched a commented-out `# --only foo` would pass against a
    unit claiming nothing.
    """
    kept = "\n".join(
        line for line in body.splitlines() if not line.lstrip().startswith("#")
    )
    return kept.replace("\\\n", " ")


def unit_claims() -> dict[str, set[str]]:
    """What each unit's ``--only`` says it runs."""
    found = {}
    for unit, _timer in UNIT_TIMERS:
        body = _live((SYSTEMD / f"{unit}.service").read_text(encoding="utf-8"))
        match = re.search(r"--only\s+([A-Za-z0-9_,\-]+)", body)
        found[unit] = {name for name in match.group(1).split(",") if name} if match else set()
    return found


def unit_cadences() -> dict[str, int]:
    """How many minutes between one pass finishing and the next starting."""
    found = {}
    for unit, timer in UNIT_TIMERS:
        body = _live((SYSTEMD / timer).read_text(encoding="utf-8"))
        match = re.search(r"OnUnitInactiveSec=(\d+)(min|s|sec)?", body)
        assert match, f"{timer} declares no OnUnitInactiveSec"
        value = int(match.group(1))
        found[unit] = value if match.group(2) == "min" else max(1, value // 60)
    return found


def market_is_open(moment: pd.Timestamp) -> bool:
    if moment.dayofweek >= 5:
        return False
    return OPEN_FROM <= (moment - moment.normalize()) < OPEN_UNTIL


def _tick(moment, state, log, claimed):
    """One pass of one unit: ask what is owed, run only what it claims."""
    for job in daemon.jobs_due(moment, state, market_open=market_is_open(moment)):
        if job in claimed:
            log.append((moment, job))
            state[job] = moment


def replay_loop() -> list[tuple[pd.Timestamp, str]]:
    """The single resident loop: one process, every minute, everything owed."""
    state: dict[str, object] = {}
    log: list[tuple[pd.Timestamp, str]] = []
    everything = set(daemon.JOBS)
    for minute in range(24 * 60):
        _tick(DAY + pd.Timedelta(minutes=minute), state, log, everything)
    return log


def replay_split(order, claims=None, cadences=None):
    """The four units, with ``order`` deciding who asks first within a minute."""
    claims = claims if claims is not None else unit_claims()
    cadences = cadences if cadences is not None else unit_cadences()
    state: dict[str, object] = {}
    log: list[tuple[pd.Timestamp, str]] = []
    for minute in range(24 * 60):
        moment = DAY + pd.Timedelta(minutes=minute)
        for unit in order:
            if minute % cadences[unit] == 0:
                _tick(moment, state, log, claims[unit])
    return log


def counts(log) -> dict[str, int]:
    tally: dict[str, int] = {job: 0 for job in daemon.JOBS}
    for _moment, job in log:
        tally[job] += 1
    return tally


def every_order():
    return list(itertools.permutations([unit for unit, _ in UNIT_TIMERS]))


def test_the_split_runs_every_job_as_often_as_the_loop_did() -> None:
    """M1670, M1672, M1673: work silently lost, or silently doubled.

    Counted rather than sampled, because the failure being guarded against
    produced a perfectly healthy-looking system doing none of the work.
    """
    expected = counts(replay_loop())
    assert expected["ingest-intraday"] > 0, (
        "the replayed day ingests nothing, so this test cannot detect "
        "ingestion being starved -- check DAY is a trading day"
    )

    for order in every_order():
        actual = counts(replay_split(order))
        for job in daemon.JOBS:
            assert actual[job] == expected[job], (
                f"with the units firing in the order {order}, {job} runs "
                f"{actual[job]} time(s) a day against the single loop's "
                f"{expected[job]}. The split must do the same work, not "
                "merely start cleanly."
            )


def test_analysis_never_reads_a_lake_ingestion_has_not_refreshed() -> None:
    """M1671: the ordering the one loop got for free.

    Equal counts are not enough. Ingestion and analysis could each run 26
    times a session with the analysis always going first, which is a whole
    cadence of staleness on every decision the account acts on.
    """
    for order in every_order():
        log = replay_split(order)
        last_ingest = None
        last_analysis = None
        for moment, job in log:
            if job == "ingest-intraday":
                last_ingest = moment
            elif job == "run-intraday":
                assert last_ingest is not None, (
                    f"order {order}: run-intraday at {moment} is the first "
                    "intraday job of the day -- the pipeline read a lake "
                    "nothing had refreshed"
                )
                assert last_analysis is None or last_ingest > last_analysis, (
                    f"order {order}: run-intraday at {moment} reads bars "
                    f"last written at {last_ingest}, which the previous run "
                    f"at {last_analysis} had already read. The analysis is a "
                    "whole cadence stale."
                )
                last_analysis = moment


def test_a_starved_unit_is_visible_in_the_counts() -> None:
    """The detector responds, so a pass means something.

    Asserting the property above proves nothing unless breaking it fails.
    This removes the ingest unit's claim -- exactly what a dead or masked
    unit looks like to the rest of the system -- and requires the comparison
    to notice.
    """
    claims = unit_claims()
    claims["cfdinvest-ingest"] = set()

    expected = counts(replay_loop())
    starved = counts(replay_split(every_order()[0], claims=claims))

    assert starved["ingest-intraday"] == 0
    assert starved["ingest-intraday"] != expected["ingest-intraday"], (
        "a unit that runs nothing produces the same counts as one that "
        "works, so this suite cannot see the failure it exists to catch"
    )


def test_the_trading_cadence_is_the_configured_one() -> None:
    """M1674: the intraday block ungated, on either side of the comparison.

    A mutation that loosens the gate raises the counts in BOTH replays, so
    equality would still hold. The cadence is therefore checked against the
    session length and `INTRADAY_MINUTES` directly rather than against the
    other architecture.
    """
    session_minutes = (OPEN_UNTIL - OPEN_FROM).total_seconds() / 60
    expected = int(session_minutes // daemon.INTRADAY_MINUTES) + 1

    tally = counts(replay_split(every_order()[0]))
    assert tally["run-intraday"] == pytest.approx(expected, abs=1), (
        f"the analysis runs {tally['run-intraday']} times in a "
        f"{session_minutes:.0f}-minute session against "
        f"{daemon.INTRADAY_MINUTES}-minute cadence, which should be about "
        f"{expected}"
    )
    assert tally["ingest-intraday"] == tally["run-intraday"], (
        "ingestion and analysis must run on the same cadence: one refreshes "
        "what the other reads"
    )


def test_nothing_intraday_runs_while_the_market_is_shut() -> None:
    """A Saturday owes no trading work, whichever unit asks.

    The split's units fire every minute regardless of the calendar, so the
    gate is the only thing stopping a weekend of runs against a lake no
    vendor is updating.
    """
    saturday = DAY + pd.Timedelta(days=5)
    assert saturday.dayofweek == 5

    state: dict[str, object] = {}
    log: list[tuple[pd.Timestamp, str]] = []
    claims = unit_claims()
    for minute in range(24 * 60):
        moment = saturday + pd.Timedelta(minutes=minute)
        for unit in claims:
            _tick(moment, state, log, claims[unit])

    traded = [job for _m, job in log
              if job in {"ingest-intraday", "run-intraday", "record-spreads"}]
    assert not traded, f"these ran on a Saturday: {sorted(set(traded))}"
