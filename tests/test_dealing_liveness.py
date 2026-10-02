"""A daemon can be alive, `active`, supervised, and not working.

On 2026-09-09 the scheduler exceeded its cgroup ``MemoryHigh`` by one
megabyte. The kernel's answer to ``memory.high`` is to sleep the task on
every allocation in proportion to the overage, so it was throttled 39,932
times and finished nothing further. It never reached ``MemoryMax``, so it
was never killed -- ``max`` events were zero -- and therefore:

  * the process stayed alive, so ``Restart=always`` never fired
  * the unit stayed ``active``, so ``cfdinvest.timer`` saw nothing wrong
  * every monitoring check reported healthy, because each asks whether a
    component EXISTS rather than whether it WORKS

The account went 45 minutes of an open session without a dealing pass and
nothing anywhere said so. It was found because the owner noticed signals on
the dashboard with no positions against them.

So this check asks the question none of the others do. Its signal is the
margin-usage row the dealing path writes on every pass -- including a pass
that examines the book and correctly does nothing, which is what makes it
liveness rather than activity.

Mutations checked, each reintroduced as a bug and confirmed to fail:
  M470 the check silent while the market is open and dealing has stopped
  M471 an unreadable calendar treated as closed rather than as unknown
  M472 a fresh install reported critical rather than as a first run
  M473 the staleness compared against the wrong end, so age never grows
  M474 the check firing when the market is shut
  M1995 a pass that could not read the account reported as a stalled dealer
  M1996 a stale or superseded block taken to explain a new stall
  M1997 the dealer's note and the monitor's reader disagreeing on its kind
"""

from __future__ import annotations

import pandas as pd
import pytest

from cfdinvest.monitoring.core import MonitoringThresholds, check_dealing_liveness

NOW = pd.Timestamp("2026-09-09 14:22:00+00:00")


def only(issues):
    assert len(issues) == 1, f"expected exactly one issue, got {issues}"
    return issues[0]


def test_the_stall_that_cost_a_session_is_reported():
    """M470: the whole reason this exists.

    The real timings: the last dealing pass completed at 13:35, the market
    was open, and by 14:22 nothing had looked at the account for 47 minutes.
    """
    issue = only(check_dealing_liveness("2026-09-09 13:35:00+00:00", True, now=NOW))

    assert issue.severity == "critical", issue.severity
    assert issue.type == "dealing"
    assert "47 minutes" in issue.message, issue.message
    assert issue.details["age_minutes"] == pytest.approx(47.0, abs=0.5)


def test_a_recent_pass_is_silent():
    """The mirror. Without it the test above passes on a constant."""
    assert check_dealing_liveness("2026-09-09 14:10:00+00:00", True, now=NOW) == []


def test_nothing_is_said_while_the_market_is_shut():
    """M474: not dealing overnight is correct, not a fault.

    A check that fires every night is one that gets muted, and a muted check
    is worse than none because it reads as coverage.
    """
    assert check_dealing_liveness("2026-09-08 19:41:00+00:00", False, now=NOW) == []


def test_an_unreadable_calendar_is_unknown_rather_than_closed():
    """M471: absence named, and the asymmetry with the daemon is deliberate.

    ``schedule/app.py`` treats an unreadable calendar as CLOSED, because
    dealing on a guess is worse than pausing. Monitoring must not: "the
    market was shut" and "we could not tell" are different statements, and
    only one of them excuses a silent daemon. Reusing the daemon's
    convention here would hide precisely the outage this file is about.
    """
    issue = only(check_dealing_liveness("2026-09-09 13:35:00+00:00", None, now=NOW))

    assert issue.severity != "critical", "an unknown calendar is not a fault"
    assert "could not" in (issue.message + issue.remedy).lower()


def test_a_fresh_install_is_a_warning_rather_than_a_failure():
    """M472: crying critical on every new deployment.

    ``check_scheduler_health`` draws the same line about a first pipeline
    run. Never having dealt is what a fresh box looks like; having dealt and
    then stopped is the fault.
    """
    issue = only(check_dealing_liveness(None, True, now=NOW))

    assert issue.severity == "warning", issue.severity
    assert "fresh install" in issue.remedy


def test_the_threshold_is_read_rather_than_hardcoded():
    """M473: an age that never grows, or a limit nothing consults.

    Three missed passes is the shipped bar. Asserted by moving the
    threshold rather than by naming the constant, so the test measures the
    property and not the number.
    """
    stamp = "2026-09-09 13:52:00+00:00"          # 30 minutes before NOW
    assert check_dealing_liveness(stamp, True, now=NOW) == [], (
        "30 minutes is inside the 45-minute default and must be silent"
    )

    tight = MonitoringThresholds(dealing_stale_minutes=20.0)
    issue = only(check_dealing_liveness(stamp, True, tight, now=NOW))
    assert issue.severity == "critical"
    assert issue.details["threshold_minutes"] == 20.0


def test_the_age_grows_with_the_gap():
    """M473, from the other side: a comparison against the wrong end.

    Subtracting the wrong way, or comparing the reading against itself,
    yields an age that is constant or negative and a check that never fires.
    """
    older = only(check_dealing_liveness("2026-09-09 12:00:00+00:00", True, now=NOW))
    newer = only(check_dealing_liveness("2026-09-09 13:00:00+00:00", True, now=NOW))

    assert older.details["age_minutes"] > newer.details["age_minutes"], (
        "an older reading must report a larger age"
    )
    assert newer.details["age_minutes"] > 0


def test_the_remedy_names_the_cause_that_hid_for_45_minutes():
    """The operator reading this at 3am has to know that `active` proves
    nothing. That sentence is the difference between finding it in minutes
    and finding it when somebody notices missing trades."""
    issue = only(check_dealing_liveness("2026-09-09 13:35:00+00:00", True, now=NOW))
    remedy = " ".join(issue.remedy.split())

    assert "active" in remedy, remedy
    # It named the cgroup memory limit until 2026-10-02. The box has had no
    # MemoryHigh since 2026-09-12 (deploy.sh refuses one) and dealing is its
    # own oneshot unit, so the advice now names where a stopped pass shows.
    assert "cfdinvest-dealer" in remedy, remedy


def test_a_pass_that_could_not_read_the_account_is_named_as_such():
    """M1995: on 2026-09-30 Capital returned a zero balance and every pass
    stopped before its usage row. The alert said "not examined" and blamed
    memory; the dealer was running the whole time."""
    issue = only(check_dealing_liveness(
        "2026-09-09 13:35:00+00:00", True, now=NOW,
        last_blocked=("2026-09-09 14:20:00+00:00", "the balance could not be read: zero"),
    ))
    text = " ".join((issue.message + " " + issue.remedy).split())
    assert "dealer is running" in text and "the balance could not be read" in text, text
    assert "memory" not in text.lower()
    assert issue.severity == "critical"


def test_an_old_block_does_not_explain_a_new_stall():
    """M1996: a blocked pass from before the last good pass, or older than
    the staleness window (45 minutes; 13:36 is 46 before 14:22), says
    nothing about why nothing has run since."""
    for blocked_at in ("2026-09-09 13:00:00+00:00", "2026-09-09 13:36:00+00:00"):
        issue = only(check_dealing_liveness(
            "2026-09-09 13:35:00+00:00", True, now=NOW,
            last_blocked=(blocked_at, "the balance could not be read: zero"),
        ))
        assert "dealer is running" not in issue.message, blocked_at


def test_the_dealer_writes_what_the_monitor_reads(tmp_path, monkeypatch):
    """M1997: the note and its reader agree on kind and fields, and the
    dealing pass calls it on both unreadable branches (checked on
    uncommented lines -- the dealer has no dry run)."""
    import inspect

    from cfdinvest.monitoring import collect
    from cfdinvest.persist import margin as margin_store
    from cfdinvest.schedule import app

    path = tmp_path / "margin.jsonl"
    monkeypatch.setattr(margin_store, "_path", lambda config=None, account=None: path)
    app._record_blocked(None, None, "the balance could not be read: zero")
    at, reason = collect._last_blocked(None)
    assert reason == "the balance could not be read: zero" and at

    live = [line.split("#", 1)[0] for line in inspect.getsource(app._command_trade_capital).splitlines()]
    calls = [line for line in live if "_record_blocked(" in line]
    assert any("balance" in line for line in calls) and any("positions" in line for line in calls), calls
