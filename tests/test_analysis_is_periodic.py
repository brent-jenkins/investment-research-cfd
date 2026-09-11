"""The analysis pass exits, and something still runs it.

The daemon leaked about 360MB an hour. Measured from a clean start on
2026-09-11, every ten-minute sample was higher than the last -- 900MB at
17:23 to 1796MB at 19:53, linear rather than a ratchet -- and running the
pipeline three times in one process retained +511MB, +23MB, +6MB, converging
to nothing. So `run_pipeline` is not the leak, and the leak has NOT been
found.

It did not need to be found to be survivable. Sixteen hours of uptime is
what turned 360MB/hour into the 5.7GB that could not allocate a correlation
panel, wedged the daemon, and left the account undealt for nineteen hours
through a trading session. **A process that exits cannot accumulate.**

So this asserts the shape rather than the cause: the analysis unit runs one
tick and exits, its timer starts it again, and the option that makes it exit
still exists. Each of those can be broken independently and two of them
break silently -- a unit that never exits looks exactly like a healthy one,
which is the whole history of this file.

Mutations checked:

  M1650 Type=simple restored, so the pass never exits
  M1651 --max-ticks dropped from ExecStart, so one tick becomes a loop
  M1652 --max-ticks deleted from the parser as test-only scaffolding
  M1653 Restart=always on a oneshot, turning the timer into a hot loop
  M1654 the timer pointed at a different unit, so nothing runs the analysis
  M1655 MemoryHigh reintroduced, which throttles instead of failing
  M1656 the timer's interval measured from start rather than finish, so a
        slow pass queues behind itself
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

SYSTEMD = Path(__file__).resolve().parents[1] / "deployment" / "systemd"


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def directive(body: str, name: str) -> str | None:
    """The value of a systemd directive, ignoring commented-out ones.

    Anchored to the start of a line. A substring search finds the word
    inside this file's own explanatory comments, which is how a test asserting
    `TimeoutStartSec=240` once passed against `# TimeoutStartSec=240`.
    """
    found = re.search(rf"^{re.escape(name)}=(.*)$", body, re.M)
    return found.group(1).strip() if found else None


def test_the_analysis_pass_exits():
    """M1650: a resident loop is what let the leak accumulate."""
    body = text(SYSTEMD / "cfdinvest.service")

    assert directive(body, "Type") == "oneshot", (
        "the analysis unit must run one tick and exit; a resident loop "
        "accumulates a 360MB/hour leak until it cannot allocate"
    )


def test_the_command_is_told_to_stop_after_one_tick():
    """M1651: Type=oneshot alone does not make a loop exit.

    systemd waits for the process; if the process loops forever the unit
    simply activates forever. `Type=oneshot` and `--max-ticks` are two halves
    of one guarantee and neither is sufficient.
    """
    start = directive(text(SYSTEMD / "cfdinvest.service"), "ExecStart")

    assert start and "--max-ticks" in start, (
        "ExecStart must bound the number of ticks, or Type=oneshot waits "
        "on a loop that never returns"
    )


def test_the_option_that_makes_it_exit_still_exists():
    """M1652: it is documented as being for tests, and it is not.

    `--max-ticks` was added for tests and dry runs. It is now the production
    path, and the unit file cannot say so loudly enough to survive somebody
    tidying up test-only scaffolding. Asserted against the parser rather than
    against the help string, because the help can be reworded.
    """
    from cfdinvest.schedule import app

    parser = app.build_parser()
    actions = {
        option
        for action in parser._subparsers._group_actions  # noqa: SLF001
        for name, sub in getattr(action, "choices", {}).items()
        if name == "daemon"
        for sub_action in sub._actions  # noqa: SLF001
        for option in sub_action.option_strings
    }

    assert "--max-ticks" in actions, (
        "the daemon command lost --max-ticks, which cfdinvest.service "
        "depends on to exit; without it the unit runs forever and the leak "
        "accumulates again"
    )


def test_a_one_shot_pass_is_not_restarted():
    """M1653: Restart=always on a oneshot is a hot loop, not resilience."""
    body = text(SYSTEMD / "cfdinvest.service")
    restart = directive(body, "Restart")

    assert restart in (None, "no", "on-failure"), (
        f"Restart={restart} on a oneshot restarts it the instant it "
        "finishes, which is the resident loop again wearing a timer"
    )


def test_something_actually_runs_it():
    """M1654: a unit that exits and is never started again is a unit that
    stopped. The timer is now the schedule, not a watchdog."""
    timer = text(SYSTEMD / "cfdinvest.timer")

    assert directive(timer, "Unit") == "cfdinvest.service", (
        "cfdinvest.timer must start cfdinvest.service; nothing else does"
    )


def test_the_interval_is_measured_from_the_end_of_the_last_pass():
    """M1656: a slow pass must delay the next one, not queue behind itself."""
    timer = text(SYSTEMD / "cfdinvest.timer")

    assert directive(timer, "OnUnitInactiveSec"), (
        "the timer must fire relative to when the last pass FINISHED; "
        "OnUnitActiveSec measures from the start, so a pass slower than the "
        "interval is immediately due again"
    )


def test_the_analysis_fails_loudly_rather_than_throttling():
    """M1655: MemoryHigh sleeps a task; MemoryMax kills it.

    2026-09-09: the pipeline crossed MemoryHigh by one megabyte, took 39,932
    throttle events, and spent 45 minutes in mem_cgroup_handle_over_high
    during an open session. It never reached MemoryMax, so it was never
    killed, so Restart=always never fired and the unit stayed `active` while
    the account silently stopped dealing.
    """
    body = text(SYSTEMD / "cfdinvest.service")

    assert not re.search(r"^MemoryHigh=", body, re.M), (
        "MemoryHigh throttles instead of failing, which is the state that "
        "is neither working nor dead"
    )
    assert directive(body, "MemoryMax"), (
        "an unbounded pass takes the neighbours down with it"
    )


@pytest.mark.parametrize("unit", ["cfdinvest.service", "cfdinvest.timer"])
def test_the_units_exist(unit: str) -> None:
    assert (SYSTEMD / unit).exists(), f"{unit} is missing"
