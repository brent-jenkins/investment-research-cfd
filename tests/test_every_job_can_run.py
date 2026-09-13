"""Every job in ``JOBS`` can actually be dispatched, and one cannot kill the rest.

`test_jobs_are_partitioned.py` proves every job has an OWNER.
`test_split_runs_the_same_work.py` proves the units do the same WORK.
Neither proves a job can be RUN, and on 2026-09-12 one could not.

`verify-day` was added to `JOBS` and to the daily unit's `--only` without
ever being given a subcommand. `_job_args` builds a namespace with
``build_parser().parse_args([command])``, so the daemon asked argparse for a
choice that did not exist. Argparse printed an error and raised
**SystemExit**, which is a BaseException and therefore walks straight through
``except Exception``. The consequences, in order:

  * the tick died on the spot, so `verify-day` never recorded a timestamp;
  * `prune-runs` is behind it in `JOBS` and so did not run either -- for two
    days, which is retention switched off;
  * the group was gated on `monitoring-snapshot`'s timestamp, which HAD been
    recorded, so the shared gate reported the whole group done until the
    next day.

Every unit still reported success on the tick where nothing was due, the
dashboard showed `verify-day` as `unchecked` and `prune-runs` as a 40-hour
warning, and that was the only sign.

Three properties are asserted here, because the failure needed all three to
be absent.

  EVERY JOB RESOLVES. `_job_args` succeeds for every name in `JOBS`, so a
  job added without a runnable command fails the build instead of failing at
  03:05 on the box.

  A JOB CANNOT KILL THE TICK, including by raising SystemExit. Error
  containment is not optional here: the jobs run in one sequential pass and
  anything that escapes takes every later job with it.

  THE FLAGS ARE NOT DECORATIVE. `--date` on `verify-day` is read by the
  command rather than accepted and discarded.

Mutations checked:

  M1690 `verify-day`'s subparser removed: `_job_args` raises SystemExit again
  M1691 the SystemExit arm dropped from the daemon loop: a bad job stops the
        ones behind it
  M1692 `--date` accepted but ignored by `_command_verify_day`
  M1693 a new name added to JOBS with no dispatch and no parser
"""
from __future__ import annotations

import argparse

import pandas as pd
import pytest

from cfdinvest.schedule import app as app_module
from cfdinvest.schedule import daemon


class _DaemonArgs:
    """What the daemon hands a job: its own namespace, not the job's."""

    command = "daemon"
    config = data_root = tickers = interval = start = end = None
    log_level = "INFO"
    dry_run = False
    no_email = True
    account = None
    max_ticks = 1
    tick_seconds = 60
    only = None


def test_every_job_can_be_dispatched_without_exiting(monkeypatch) -> None:
    """M1690, M1693: a job in JOBS the dispatch cannot actually run.

    This is the check that was missing, and getting it right took a wrong
    version first: asserting that every job resolves its OWN name fails on
    most of them by design. Jobs deliberately BORROW a registered command's
    flags -- `record-candidate-spreads`, `record-spreads` and the two
    ingests all pass `poll-live-quotes`, because what they need is that
    command's namespace, not one of their own.

    So the property is not "every job names a subcommand". It is that
    `run_daemon_job` can dispatch every job without raising -- which is
    exercised here with the real dispatch and the real parser, and every
    `_command_*` stubbed so nothing does any work.
    """
    called: list[str] = []
    for attribute in dir(app_module):
        if attribute.startswith("_command_"):
            monkeypatch.setattr(
                app_module, attribute,
                lambda *a, _n=attribute, **k: called.append(_n),
            )

    # `screen-universe` is the one job that does NOT go through a
    # `_command_*`: it calls `universe.cli.main(["screen"])`. Stubbed too, or
    # this test screens a hundred instruments and writes a real selection --
    # which the first version of it did. It is also a second place SystemExit
    # can come from, since that main parses arguments of its own.
    from cfdinvest.universe import cli as universe_cli

    monkeypatch.setattr(
        universe_cli, "main", lambda *a, **k: called.append("universe_cli.main")
    )

    # The ingest branches call `runner._refresh` inline rather than a
    # `_command_*` too. Stubbed for the same reason, and the first version of
    # this test proved the point: left unstubbed it passed in isolation --
    # where it failed early on absent Capital credentials -- and went to
    # yfinance over the network when the whole suite ran. A test whose
    # behaviour depends on what ran before it is not a test.
    monkeypatch.setattr(
        app_module.runner_module, "_refresh",
        lambda *a, **k: called.append("runner._refresh"),
    )

    exited: list[str] = []
    silent: list[str] = []
    for name in daemon.JOBS:
        before = len(called)
        reached = False
        try:
            app_module.run_daemon_job(name, _DaemonArgs(), _app_config())
            reached = len(called) > before
        except SystemExit as error:
            exited.append(f"{name} (SystemExit {error.code})")
            continue
        except Exception:  # noqa: BLE001
            # The job got as far as its own code and failed on something
            # environmental -- no Capital credentials on a dev machine, say.
            # That is the dispatch WORKING. This test is about whether a job
            # can be reached, not about whether it can complete here.
            reached = True
        if not reached:
            silent.append(name)

    assert not exited, (
        f"these scheduled jobs exit the process instead of running: {exited}. "
        "`_job_args` resolves its argument against the CLI parser, so a job "
        "needs its own subcommand or a registered one to borrow flags from "
        "-- and SystemExit is not caught by the loop's `except Exception`, so "
        "it takes every later job in the tick with it."
    )
    assert not silent, (
        f"`run_daemon_job` has no branch for {silent}, so the daemon logs "
        "'running' and does nothing -- success and doing nothing being "
        "indistinguishable is this project's oldest failure"
    )


def test_a_job_that_exits_does_not_stop_the_ones_behind_it(monkeypatch) -> None:
    """M1691: SystemExit escaping the loop, which is what actually happened.

    `except Exception` does not catch SystemExit. Argparse raises it. So the
    guard that looks like it contains a failing job did not contain this one,
    and every job later in the tick was skipped.
    """
    ran: list[str] = []
    recorded: list[str] = []

    def fake_run(name, args, app_config, smtp=None, transport=None):
        ran.append(name)
        if name == "advisor-alert":
            raise SystemExit(2)

    monkeypatch.setattr(app_module, "run_daemon_job", fake_run)
    monkeypatch.setattr(
        app_module.daemon_module if hasattr(app_module, "daemon_module") else daemon,
        "JOBS", daemon.JOBS, raising=False,
    )

    from cfdinvest.persist import scheduler_state

    monkeypatch.setattr(
        scheduler_state, "record_job",
        lambda name, stamp, **kwargs: recorded.append(name),
    )
    monkeypatch.setattr(
        scheduler_state, "last_job_runs", lambda *a, **k: {},
    )
    monkeypatch.setattr(
        daemon, "jobs_due",
        lambda *a, **k: ["poll-live-quotes", "advisor-alert", "prune-runs"],
    )

    args = _DaemonArgs()
    code = app_module._command_daemon(  # noqa: SLF001
        args, _app_config(), None, None
    )

    assert code == app_module.EXIT_OK
    assert "prune-runs" in ran, (
        "a job raised SystemExit and the jobs behind it never ran. That is "
        "two days of no retention, which is exactly what happened on "
        "2026-09-12 and 2026-09-13."
    )
    assert "advisor-alert" in recorded, (
        "the failing job recorded no timestamp, so it is retried every tick "
        "-- a hot loop against whatever made it fail"
    )


def test_the_verify_date_flag_is_read_rather_than_accepted() -> None:
    """M1692: a flag the parser advertises and the command discards.

    Asserted against the command's behaviour rather than its source: the
    day it reports on must follow ``--date``.
    """
    parser = app_module.build_parser()
    args = parser.parse_args(["verify-day", "--date", "2026-01-05"])
    assert getattr(args, "date", None) == "2026-01-05"

    seen: dict[str, object] = {}

    import cfdinvest.verify as verify_module

    original = verify_module.verify_day

    class _Report:
        verdict = "ok"

        def describe(self):
            return "fine"

    def fake_verify(app_config, *, account=None, day=None):
        seen["day"] = day
        return _Report()

    verify_module.verify_day = fake_verify
    try:
        app_module._command_verify_day(args, _app_config(), None, None)  # noqa: SLF001
    finally:
        verify_module.verify_day = original

    assert seen.get("day") == "2026-01-05", (
        f"--date was given as 2026-01-05 and the command verified "
        f"{seen.get('day')!r}. A flag that is parsed and ignored reads as a "
        "capability and provides none."
    )


def test_the_daily_jobs_do_not_share_one_gate() -> None:
    """The group must not be lockable out by one member failing.

    All four hung off `monitoring-snapshot`'s timestamp, so when the tick
    died between `risk-snapshot` and `prune-runs` the shared gate reported
    the group done for the day and retention simply stopped.
    """
    stamp = pd.Timestamp("2026-09-14 03:06", tz="UTC")
    # Everything the daily block owns has run EXCEPT prune-runs -- the state
    # the box was actually in.
    history = {
        "ingest-daily": pd.Timestamp("2026-09-14 03:00", tz="UTC"),
        "monitoring-snapshot": pd.Timestamp("2026-09-14 03:05", tz="UTC"),
        "risk-snapshot": pd.Timestamp("2026-09-14 03:05", tz="UTC"),
        "ingest-minute": pd.Timestamp("2026-09-13 23:02", tz="UTC"),
        "screen-universe": pd.Timestamp("2026-09-14 03:10", tz="UTC"),
    }
    due = daemon.jobs_due(stamp, history, market_open=False)

    assert "prune-runs" in due, (
        "prune-runs has not run today and is not owed, because the gate "
        "belongs to a job that HAS run. Retention stops and nothing says so."
    )
    assert "verify-day" in due, (
        "verify-day has never run and is not owed, for the same reason"
    )


def _app_config():
    from cfdinvest.config import load_config

    return load_config()
