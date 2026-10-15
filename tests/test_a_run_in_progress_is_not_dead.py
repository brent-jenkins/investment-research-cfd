"""A run still going is not a run that died.

On 2026-10-15 the monitor read the scheduler state at 19:16:42, eleven
seconds into an analysis run that finished at 19:16:45, saw a start with no
success after it, and mailed a RED: "The last run started and never
completed." Runs take 15-30 seconds and the monitor looks every thirty
minutes, so roughly one check in sixty lands inside one -- a false alarm on
a schedule, which is the kind nobody reads.

The other half matters as much: a run that genuinely wedges (2026-09-11 sat
inside one call for fifty minutes) must still read as dead, within one
monitoring cycle.
"""
from __future__ import annotations

import dataclasses

import pandas as pd

from cfdinvest.monitoring import core
from cfdinvest.risk.scheduler_status import compute_scheduler_status

STARTED = pd.Timestamp("2026-10-15 19:16:33", tz="UTC")
STATE = {
    "last_run": STARTED.isoformat(),
    "last_success": pd.Timestamp("2026-10-15 18:57:59", tz="UTC").isoformat(),
    "last_error": None,
    "runs": 790,
    "failures": 13,
}


def _critical(issues):
    return [i for i in issues if i.severity == "critical"]


def test_the_monitor_at_19_16_42_sees_a_run_in_progress():
    """The exact moment of the false RED."""
    now = pd.Timestamp("2026-10-15 19:16:42", tz="UTC")
    status = compute_scheduler_status(STATE, now=now)
    assert status["running"] and not status["incomplete"]
    assert status["run_status"] == "RUNNING"
    assert _critical(core.check_scheduler_health(STATE, now=now)) == []


def test_a_wedged_run_still_reads_as_dead():
    limit = core.MonitoringThresholds().run_overrun_minutes
    now = STARTED + pd.Timedelta(minutes=limit + 1)
    status = compute_scheduler_status(STATE, now=now)
    assert status["incomplete"] and not status["running"]
    issues = _critical(core.check_scheduler_health(STATE, now=now))
    assert issues and "never completed" in issues[0].message


def test_the_allowance_is_read_from_the_monitoring_thresholds():
    """One owner: tightening the threshold moves the alarm."""
    now = STARTED + pd.Timedelta(seconds=30)
    tight = dataclasses.replace(core.MonitoringThresholds(), run_overrun_minutes=0.1)
    assert _critical(core.check_scheduler_health(STATE, now=now)) == []
    assert _critical(core.check_scheduler_health(STATE, tight, now=now))


def test_a_run_that_recorded_an_error_is_not_excused_by_being_recent():
    now = STARTED + pd.Timedelta(seconds=5)
    state = {**STATE, "last_error": "boom", "last_error_at": STARTED.isoformat()}
    assert compute_scheduler_status(state, now=now)["run_status"] == "FAILED"
