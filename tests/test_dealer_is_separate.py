"""Dealing runs in its own process, and exactly one thing may do it.

The daemon runs its jobs SEQUENTIALLY, so while ``trade-capital`` was in
that list the lightest and most time-critical work in the system queued
behind the heaviest. On 2026-09-11 the pipeline wedged at 13:35 and the
account was not dealt for again that session — no opens, and far worse no
CLOSES, with a position waiting on a three-and-a-half-year backtest to
finish before it could be flattened at the bell.

It is now ``cfdinvest-dealer.service`` on a five-minute timer. Two
properties have to hold together and neither is safe alone:

* **The daemon must not deal.** Otherwise both halves deal, on two
  schedules, against one account — the mirror of the two-schedulers-on-one-
  lake failure the pipeline timer stays disabled to avoid.
* **The dealer must actually be deployed and enabled.** A unit that exists
  but was never switched on does nothing, which is precisely what happened
  to ``cfdinvest-monitor.timer``: written, committed, correct, and disabled
  on the box for a fortnight while two stalls went unreported.

And the dealer must NOT conflict with the daemon. The whole point is that
it keeps running while the daemon is busy, so a ``Conflicts=`` line here
would rebuild the failure it exists to remove — which is not hypothetical,
because that is exactly the line that kept the monitor switched off.

Mutations checked, each reintroduced as a bug and confirmed to fail:
  M1600 trade-capital put back in the daemon's JOBS
  M1610 trade-capital re-appended in the due-logic
  M1620 the dealer timer dropped from the deploy
  M1630 the dealer service made to conflict with the daemon
  M1640 the dealer given a MemoryHigh, which throttles instead of failing
  M1650 the start timeout commented out rather than removed

One mutation SURVIVED and is recorded because it says something about the
design rather than about the suite: re-adding ``due.append("trade-capital")``
to the due-logic changes nothing, because ``jobs_due`` returns
``[job for job in JOBS if job in due]`` and the name is no longer in JOBS.
The filter makes it inert. That is the two guards working together rather
than a hole -- and M1600, which puts the name back in JOBS, is caught.
"""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd
import pytest

from cfdinvest.schedule.daemon import JOBS, jobs_due

DEPLOYMENT = Path(__file__).resolve().parents[1] / "deployment"
SYSTEMD = DEPLOYMENT / "systemd"


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_the_daemon_does_not_deal():
    """M1600: the half that prevents dealing twice."""
    assert "trade-capital" not in JOBS, (
        "the daemon lists trade-capital again, so it and the dealer unit "
        "would both deal against one account"
    )


@pytest.mark.parametrize("market_open", [True, False])
def test_the_daemon_never_schedules_dealing(market_open):
    """M1610: absent from the list is not enough if the due-logic adds it.

    Asserted through ``jobs_due`` itself rather than by reading JOBS, so
    re-appending it anywhere in that function fails here.
    """
    due = jobs_due(
        pd.Timestamp("2026-09-14 15:00", tz="UTC"), {}, market_open=market_open
    )
    assert "trade-capital" not in due


def test_the_dealer_unit_exists_and_runs_the_command():
    """A unit that does not invoke the dealer is scenery."""
    body = text(SYSTEMD / "cfdinvest-dealer.service")

    assert re.search(r"^ExecStart=.*cfdinvest-schedule trade-capital", body, re.M), (
        "the dealer unit does not run the dealing command"
    )
    assert "Type=oneshot" in body, (
        "the dealer must exit between passes; a resident process would "
        "accumulate the leak the daemon already demonstrates"
    )


def test_the_dealer_does_not_conflict_with_the_daemon():
    """M1630: the line that kept the monitor switched off for a fortnight.

    The dealer exists to keep running while the daemon is busy. A
    ``Conflicts=`` between them would either stop the daemon every five
    minutes or make the dealer unschedulable — and the second is what
    happened to the monitor.
    """
    dealer = text(SYSTEMD / "cfdinvest-dealer.service")
    daemon = text(SYSTEMD / "cfdinvest.service")

    for body, name in ((dealer, "the dealer"), (daemon, "the daemon")):
        conflicts = re.search(r"^Conflicts=(.*)$", body, re.M)
        if not conflicts:
            continue
        assert "cfdinvest-dealer.service" not in conflicts.group(1), (
            f"{name} conflicts with the dealer"
        )
        assert "cfdinvest.service" not in conflicts.group(1), (
            f"{name} conflicts with the daemon"
        )


def test_the_dealer_fails_loudly_rather_than_throttling():
    """M1640: MemoryHigh sleeps a task; MemoryMax kills it.

    This project has twice been stalled by a process throttled between
    MemoryHigh and MemoryMax: alive, busy, never killed, never restarted,
    and never reported. The dealer is light by construction, so a ceiling
    here is a tripwire — and a tripwire must trip, not sedate.
    """
    body = text(SYSTEMD / "cfdinvest-dealer.service")

    # Anchored to the start of a line, because a commented-out directive
    # still contains the substring -- which is how the first version of
    # this assertion passed against `# TimeoutStartSec=240`.
    assert re.search(r"^MemoryMax=", body, re.M), (
        "the dealer has no active memory ceiling at all"
    )
    assert not re.search(r"^MemoryHigh=", body, re.M), (
        "MemoryHigh throttles instead of failing, which is the exact "
        "failure mode that hid two stalls"
    )
    assert re.search(r"^TimeoutStartSec=\d+", body, re.M), (
        "a dealing pass must not be allowed to outlive its own cadence"
    )


def test_the_deploy_enables_the_dealer():
    """M1620: written, committed, correct, and never switched on.

    That is not hypothetical — `cfdinvest-monitor.timer` sat disabled on the
    box for a fortnight while the stall it was written to catch happened
    twice. Asserted against the TIMERS list the deploy actually iterates.
    """
    body = text(DEPLOYMENT / "deploy.sh")
    timers = re.search(r"^TIMERS=\((.*)\)$", body, re.M)

    assert timers, "deploy.sh must declare which timers it enables"
    assert "cfdinvest-dealer.timer" in timers.group(1), (
        "the deploy does not enable the dealer timer, so a fresh box would "
        "analyse the market and never trade"
    )


def test_the_dealer_timer_runs_often_enough_to_close():
    """Closing is the time-critical half.

    A position that should be shut wants shutting now, not when the next
    pipeline happens to finish. The cadence is asserted as a bound rather
    than a value so it can be tuned without silently becoming hourly.
    """
    body = text(SYSTEMD / "cfdinvest-dealer.timer")
    every = re.search(r"^OnUnitActiveSec=(\d+)min", body, re.M)

    assert every, "the dealer timer must state its cadence in minutes"
    assert int(every.group(1)) <= 15, (
        f"dealing every {every.group(1)} minutes is too slow to close on"
    )
