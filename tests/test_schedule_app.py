"""The daemon's dispatch and command layer.

This module was 44% covered, and it is where several of this project's
defects have lived: a lake that silently stopped refreshing when the trading
interval changed, a job dispatch nothing could call, and a daemon that
forgot what it had run and re-ran the daily pipeline on every restart.

The pattern is worth naming. Every one of those bugs sat in a branch no test
could reach, not because the branch was subtle but because the surrounding
function did too much IO to invoke. Coverage here is not a number to raise;
it is the difference between a branch that can fail loudly and one that
fails silently for eleven hours.
"""

from __future__ import annotations

import pandas as pd
import pytest

from cfdinvest.config import AppConfig, MarketDataConfig, PathConfig
import cfdinvest.schedule.app as app


def parsed(command: str, *extra: str, email: bool = False):
    """Real arguments, from the real parser.

    Hand-rolled stand-ins drift: a command reads an attribute the stub does
    not have and the test fails for a reason that has nothing to do with the
    behaviour under test. Building them through the parser also asserts,
    incidentally, that the parser supplies everything the commands read.
    """
    prefix = [] if email else ["--no-email"]
    return app.build_parser().parse_args([*prefix, command, *extra])


class Args:
    """A minimal stand-in, for the few helpers that read only a field or two."""

    no_email = True
    interval = None
    start = None
    end = None
    tickers = None
    account = None
    dry_run = False
    refresh = False
    trade_alerts = False
    min_confidence = None
    balance = None
    max_ticks = 1
    tick_seconds = 0
    alert = False


def config_for(root) -> AppConfig:
    return AppConfig(
        paths=PathConfig(data_root=root),
        market=MarketDataConfig(tickers=("AAPL", "MSFT"), interval="1h"),
    )


# --------------------------------------------------------------------------- #
# The dispatch
# --------------------------------------------------------------------------- #
def test_every_job_the_daemon_schedules_can_be_dispatched(tmp_path, monkeypatch):
    """M200: a job name the loop queues and the dispatch does not handle.

    ``jobs_due`` returns names and ``run_daemon_job`` acts on them. They are
    two lists of one fact, and a name in the first with no branch in the
    second is a job that is silently never run -- the symptom being nothing
    at all, which is the hardest failure to notice.
    """
    from cfdinvest.schedule import daemon

    ran: list[str] = []
    for name in daemon.JOBS:
        monkeypatch.setattr(
            app, "_command_poll_live_quotes", lambda *a, **k: ran.append("quotes")
        )
    # Every branch is asserted structurally: the alternative is stubbing a
    # dozen collaborators to prove a dispatch table is complete.
    import inspect

    source = inspect.getsource(app.run_daemon_job)
    for name in daemon.JOBS:
        assert f'"{name}"' in source, (
            f"the daemon queues {name} but the dispatch cannot run it"
        )


def test_an_unknown_job_is_ignored_rather_than_raising(tmp_path):
    """A name that reaches the dispatch and matches nothing must not stop
    the loop -- one unknown job is not a reason to lose every other."""
    app.run_daemon_job("not-a-real-job", Args(), config_for(tmp_path))


# --------------------------------------------------------------------------- #
# The loop
# --------------------------------------------------------------------------- #
def test_a_job_that_raises_does_not_stop_the_loop(tmp_path, monkeypatch):
    """M201: one failing job taking the daemon down.

    The whole point of a resident loop is that tomorrow's run still happens
    after today's failed. A vendor timeout on the quote poller must not
    cost the paper book its pass.
    """
    calls: list[str] = []

    def explode(name, args, app_config, smtp=None, transport=None):
        calls.append(name)
        if name == "poll-live-quotes":
            raise RuntimeError("the vendor timed out")

    monkeypatch.setattr(app, "run_daemon_job", explode)
    monkeypatch.setattr(
        app, "_execution_config", lambda *a, **k: app.ExecutionConfig()
        if hasattr(app, "ExecutionConfig") else None
    )

    from cfdinvest.schedule import daemon

    monkeypatch.setattr(
        daemon, "jobs_due",
        lambda now, last, market_open=None: ["poll-live-quotes", "advisor-alert"],
    )

    args = Args()
    args.max_ticks = 1
    result = app._command_daemon(args, config_for(tmp_path), None, None)

    assert result == app.EXIT_OK, "a failing job returned a failing daemon"
    assert calls == ["poll-live-quotes", "advisor-alert"], (
        "the loop stopped after the failure instead of carrying on"
    )


def test_a_failing_job_still_records_that_it_ran(tmp_path, monkeypatch):
    """M202: a hot loop against somebody else's server.

    A job that fails is recorded as having run. Without that it comes due
    again on the very next tick and retries forever -- which is a denial of
    service aimed at a vendor, and the failure is already in the log.
    """
    import inspect

    source = inspect.getsource(app._command_daemon)
    # The stamp must be outside the try, or a raising job never records.
    assert "except Exception" in source
    failure = source.index("except Exception")
    stamp = source.index("last_run[name] =")
    assert stamp > failure, (
        "the completion stamp is inside the try, so a failing job is never "
        "recorded and will be retried every tick"
    )


def test_the_loop_honours_max_ticks(tmp_path, monkeypatch):
    """Without it a test of the daemon never returns."""
    ticks: list[int] = []

    def count(name, args, app_config, smtp=None, transport=None):
        ticks.append(1)

    monkeypatch.setattr(app, "run_daemon_job", count)
    from cfdinvest.schedule import daemon

    monkeypatch.setattr(
        daemon, "jobs_due", lambda now, last, market_open=None: ["advisor-alert"]
    )

    args = Args()
    args.max_ticks = 3
    app._command_daemon(args, config_for(tmp_path), None, None)
    assert len(ticks) == 3


def test_the_market_calendar_failing_means_closed_not_open(tmp_path, monkeypatch):
    """M203: trading on a guess about whether the market is open.

    Pausing costs a few bars. Trading into a market that is shut costs
    whatever the guess was worth, and a fill at a price nobody was quoting
    is not a fill.
    """
    seen: dict[str, object] = {}

    from cfdinvest.schedule import daemon, scheduler

    def refuse(*a, **k):
        raise RuntimeError("no calendar")

    monkeypatch.setattr(scheduler, "any_open", refuse)
    monkeypatch.setattr(app, "run_daemon_job", lambda *a, **k: None)

    def record(now, last, market_open=None):
        seen["market_open"] = market_open
        return []

    monkeypatch.setattr(daemon, "jobs_due", record)

    args = Args()
    args.max_ticks = 1
    app._command_daemon(args, config_for(tmp_path), None, None)

    assert seen.get("market_open") is False, (
        "an unreadable calendar was treated as an open market"
    )


# --------------------------------------------------------------------------- #
# The paper book command
# --------------------------------------------------------------------------- #
def test_every_subcommand_maps_to_a_handler():
    """M205: a command the parser accepts and main cannot run.

    argparse will happily accept a subcommand that dispatches to nothing,
    and the failure is a silent exit rather than an error.
    """
    import inspect

    parser = app.build_parser() if hasattr(app, "build_parser") else None
    if parser is None:
        pytest.skip("no build_parser to inspect")

    choices: set[str] = set()
    for action in parser._actions:
        if getattr(action, "choices", None):
            choices.update(action.choices)

    source = inspect.getsource(app.main)
    missing = [name for name in choices if f'"{name}"' not in source]
    assert not missing, f"the parser accepts {missing} but main cannot run them"


def test_the_job_arguments_carry_the_account_through(tmp_path):
    """M206: a job silently run against the wrong account.

    The account separation exists so a virtual book and a live one cannot be
    confused. A job that dropped it would write one account's artefacts under
    the other's root, which is the exact confusion it was built to stop.
    """
    args = Args()
    args.account = "virtual"
    passed = app._job_args(args, "advisor-alert")
    assert getattr(passed, "account", None) == "virtual"
    assert getattr(passed, "command", None) == "advisor-alert"


def test_job_arguments_do_not_leak_between_jobs(tmp_path):
    """Each job gets its own namespace, not a mutated shared one."""
    args = Args()
    first = app._job_args(args, "poll-live-quotes", refresh=True)
    second = app._job_args(args, "run-once")

    assert getattr(first, "command") == "poll-live-quotes"
    assert getattr(second, "command") == "run-once"
    assert getattr(first, "refresh", None) is True
    # The override on the first must not have reached the second.
    assert getattr(second, "refresh", False) is not True or args.refresh is True


# --------------------------------------------------------------------------- #
# The commands, invoked rather than read
# --------------------------------------------------------------------------- #
def test_pruning_reports_what_it_removed_and_never_fails_the_caller(
    tmp_path, monkeypatch, capsys
):
    """M207: a prune that fails the job that asked for it.

    This runs after the daily pipeline. A full disk is a worse outcome than
    a failed delete, but neither is a reason to lose the run that just
    completed -- so it reports and returns cleanly either way.
    """
    from cfdinvest.persist import retention

    monkeypatch.setattr(
        retention, "prune_runs",
        lambda *a, **k: {"slimmed": ["a/x.parquet"], "deleted": ["b"],
                         "freed_bytes": 1024, "error": None, "dry_run": False},
    )
    assert app._command_prune_runs(
        parsed("prune-runs"), config_for(tmp_path)
    ) == app.EXIT_OK
    out = capsys.readouterr().out
    assert "1" in out

    monkeypatch.setattr(
        retention, "prune_runs",
        lambda *a, **k: {"slimmed": [], "deleted": [], "freed_bytes": 0,
                         "error": "the store was unreadable", "dry_run": False},
    )
    assert app._command_prune_runs(
        parsed("prune-runs"), config_for(tmp_path)
    ) == app.EXIT_OK, "a failed prune failed the caller"


def test_polling_nothing_is_not_a_failed_command(tmp_path, monkeypatch, capsys):
    """M208: a non-zero exit over a quiet market.

    A poll that finds nothing to price is not an error -- the readers fall
    back to the daily bar, which is what they do between polls anyway. A
    failing exit here would have systemd restarting the loop over an empty
    book.
    """
    from cfdinvest.livequotes import poller

    monkeypatch.setattr(poller, "instruments_to_price", lambda *a, **k: [])
    assert app._command_poll_live_quotes(
        parsed("poll-live-quotes"), config_for(tmp_path)
    ) == app.EXIT_OK
    assert "nothing to price" in capsys.readouterr().out.lower()


def test_polling_writes_what_it_priced_and_names_what_it_could_not(
    tmp_path, monkeypatch, capsys
):
    """A price that could not be read is reported absent, never as zero."""
    from cfdinvest.livequotes import poller
    import cfdinvest.schedule.app as module

    import cfdinvest.livequotes as livequotes

    monkeypatch.setattr(poller, "instruments_to_price", lambda *a, **k: ["AAPL", "XXX"])
    # Patched where the command imports them from -- it does the import
    # inside the function, so patching the attribute on `app` never takes.
    monkeypatch.setattr(
        livequotes, "poll_live_quotes", lambda held: {"AAPL": 231.4, "XXX": None}
    )
    written: dict = {}
    monkeypatch.setattr(
        livequotes, "write_quotes",
        lambda prices, config, account=None: written.update(prices) or "/tmp/q.json",
    )

    assert app._command_poll_live_quotes(
        parsed("poll-live-quotes"), config_for(tmp_path)
    ) == app.EXIT_OK
    assert written.get("AAPL") == pytest.approx(231.4)
    assert written.get("XXX") is None, "an unreadable price was dropped or zeroed"
    assert "1 of 2" in capsys.readouterr().out


def test_main_dispatches_and_returns_the_commands_code(tmp_path, monkeypatch):
    """M210: a command that runs and whose exit code is thrown away.

    systemd reads the exit code. A command that failed and returned zero is
    a failure nothing will retry and nobody will see.
    """
    seen: dict = {}

    def fake(args, app_config, *rest):
        seen["ran"] = args.command
        return 3

    monkeypatch.setattr(app, "_command_prune_runs", fake)
    monkeypatch.setattr(app, "_app_config", lambda args: config_for(tmp_path))

    code = app.main(["prune-runs"])
    assert seen.get("ran") == "prune-runs"
    assert code == 3, "the command's exit code was discarded"


def test_a_crashing_command_never_looks_like_success(tmp_path, monkeypatch):
    """M211: a failure that exits zero.

    `main` deliberately catches only KeyboardInterrupt. Anything else
    propagates, which for a systemd unit means a non-zero exit and a
    traceback in the journal -- more useful than a swallowed error and a
    tidy exit code. What must never happen is a crash reported as success,
    because nothing retries a job that says it worked.
    """
    def explode(*a, **k):
        raise RuntimeError("the lake caught fire")

    monkeypatch.setattr(app, "_command_prune_runs", explode)
    monkeypatch.setattr(app, "_app_config", lambda args: config_for(tmp_path))

    with pytest.raises(RuntimeError):
        app.main(["prune-runs"])


def test_an_interrupt_is_caught_and_reported_rather_than_traced(
    tmp_path, monkeypatch, capsys
):
    """Ctrl-C on a long run is a person, not a fault."""
    def interrupt(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(app, "_command_prune_runs", interrupt)
    monkeypatch.setattr(app, "_app_config", lambda args: config_for(tmp_path))

    assert app.main(["prune-runs"]) != app.EXIT_OK
    assert "interrupted" in capsys.readouterr().err.lower()


def test_an_unknown_command_is_a_usage_error(tmp_path, monkeypatch):
    """Falling off the dispatch must not read as a clean run."""
    monkeypatch.setattr(app, "_app_config", lambda args: config_for(tmp_path))

    class Bare(Args):
        command = "no-such-command"

    monkeypatch.setattr(app, "build_parser", lambda: type(
        "P", (), {"parse_args": staticmethod(lambda argv: Bare())}
    )())
    assert app.main([]) == app.EXIT_USAGE


def test_the_monitoring_snapshot_reports_red_without_failing_the_unit(
    tmp_path, monkeypatch, capsys
):
    """M212: a red health snapshot exiting non-zero.

    A red monitor means the *lake* is unhealthy, not that the monitoring job
    failed. Exiting non-zero would have systemd restarting a unit that did
    its work correctly, and would mask the one case where the job itself
    broke.
    """
    from cfdinvest.monitoring import collect

    class Snapshot:
        status = "red"
        reasons = ("the run is missing trades",)
        checks = ()
        timestamp = "2026-08-27T03:05:00Z"

        def to_dict(self):
            return {"status": self.status, "reasons": list(self.reasons)}

    monkeypatch.setattr(
        collect, "collect_monitoring_snapshot", lambda *a, **k: Snapshot(),
        raising=False,
    )
    code = app._command_monitor(parsed("monitor"), config_for(tmp_path), None, None)

    assert code == app.EXIT_OK, "a red lake failed the monitoring unit"
    assert "red" in capsys.readouterr().out.lower()


def test_the_risk_snapshot_runs_with_an_empty_book(tmp_path, monkeypatch, capsys):
    """Nothing open is a state the snapshot must record, not skip.

    "No positions" and "not measured" are different statements, and only one
    of them is reassuring -- which is the rule this whole project runs on.
    """
    code = app._command_risk_snapshot(parsed("risk-snapshot"), config_for(tmp_path))
    assert code == app.EXIT_OK


def test_the_advisor_says_nothing_when_there_is_nothing_new(
    tmp_path, monkeypatch, capsys
):
    """M213: an alert every five minutes about the same suggestion.

    The advisor emails what is *newly* worth acting on. Re-sending an
    unchanged suggestion on every pass is how the one that matters gets
    filtered into a folder nobody opens.
    """
    from cfdinvest.advisor import alerts

    monkeypatch.setattr(
        app, "_advisor_inputs", lambda *a, **k: ({}, {}, [], None), raising=False
    )
    monkeypatch.setattr(alerts, "load_seen", lambda *a, **k: {})
    monkeypatch.setattr(alerts, "save_seen", lambda *a, **k: None)
    monkeypatch.setattr(alerts, "decide_alerts", lambda *a, **k: ([], {}))

    sent: list = []
    from cfdinvest.schedule import notify

    monkeypatch.setattr(
        notify, "send_advisor_alert", lambda *a, **k: sent.append(1) or True
    )

    # The suggestion gate has to be *open*, or this proves nothing: with it
    # shut nothing sends whatever the freshness guard does, and the first
    # version of this test passed against a mutation that removed the guard
    # entirely.
    smtp = notify.SmtpConfig(
        sender="a@b.c", recipients=("d@e.f",), send_advisor_suggestions=True,
    )
    # Email left on, or --no-email blocks the send before the freshness
    # guard is ever consulted -- which is what made the first two versions
    # of this test pass against a mutation that deleted the guard.
    code = app._command_advisor_alert(
        parsed("advisor-alert", email=True), config_for(tmp_path), smtp, None
    )
    assert code == app.EXIT_OK
    assert not sent, "an alert was sent with nothing new to say"


# --------------------------------------------------------------------------- #
# M240 the conversion pairs named but never fetched
# M241 a faded signal and a reversed one reported as one thing
# --------------------------------------------------------------------------- #
def test_every_conversion_pair_the_engine_names_is_also_ingested():
    """M240: the map is right and the lake is empty.

    The engine reads its rates from the lake, and ``LAKE_FX_PAIRS`` says
    which series to read. The pairs were dropped from the ingest list when
    the universe narrowed to twenty US shares -- so the map went on naming
    GBPUSD=X and nothing went on fetching it, and every intraday run marked
    a $150 share at £150.

    It announced itself, too: twenty warnings a run, one per instrument,
    into a log nobody was reading. The fix is not another list -- it is
    deriving the ingest set from the map, so the two cannot disagree.
    """
    from cfdinvest.schedule.app import (
        LAKE_FX_PAIRS, _with_fx_support, fx_support_tickers,
    )

    named = set()
    for value in LAKE_FX_PAIRS.values():
        legs = value if isinstance(value, list) else [value]
        for leg in legs:
            named.add(leg[0] if isinstance(leg, tuple) else leg)

    assert named, "the engine names no pairs at all"
    assert named <= set(fx_support_tickers()), (
        f"named but never fetched: {sorted(named - set(fx_support_tickers()))}"
    )

    universe = ["NVDA", "AAPL"]
    wanted = _with_fx_support(universe)
    assert set(universe) <= set(wanted), "the universe was dropped"
    assert named <= set(wanted)
    assert len(wanted) == len(set(wanted)), "a ticker was fetched twice"


@pytest.mark.parametrize("job", ["ingest-daily", "ingest-intraday"])
def test_the_ingest_jobs_actually_ask_for_the_pairs(job, tmp_path, monkeypatch):
    """M240: the seam the previous test missed.

    Asserting that ``_with_fx_support`` returns the right list proves the
    helper works while leaving the defect untouched -- it lived in the call
    sites, which were passing the bare universe. The first version of this
    test passed happily against the mutation that put that back, which is
    the same way an earlier bug in this project survived its own test.
    """
    from cfdinvest.schedule import runner as runner_module
    from cfdinvest.schedule.app import fx_support_tickers

    asked: list[list[str]] = []
    monkeypatch.setattr(
        runner_module, "_refresh",
        lambda repository, tickers, settings: asked.append(list(tickers)),
    )

    app.run_daemon_job(job, parsed("run-once"), config_for(tmp_path))

    assert asked, f"{job} fetched nothing at all"
    for request in asked:
        assert set(fx_support_tickers()) <= set(request), (
            f"{job} asked for {request} -- the conversion pairs are missing, "
            "so every dollar price will be marked as sterling"
        )
        assert {"AAPL", "MSFT"} <= set(request), "the universe was dropped"


def test_the_support_pairs_are_fetched_but_never_traded():
    """Read, not held. A pair is an input to pricing, not a position.

    Adding them to the traded universe instead would put five currency
    positions in a book that exists to measure twenty US shares.
    """
    from cfdinvest.config import load_config
    from cfdinvest.schedule.app import fx_support_tickers

    traded = set(load_config().market.tickers)
    assert not (traded & set(fx_support_tickers())), (
        "a conversion pair is in the traded universe"
    )


def test_a_faded_signal_and_a_reversed_one_are_told_apart():
    """M241: two different events under one label.

    A re-check returning flat means the signal faded on fresh data -- often
    because the bar in progress has barely moved, which dilutes momentum. A
    re-check returning the opposite side means the trend turned overnight.
    Both refuse, and both should. But the first session with this gate
    running refused five entries under "direction changed since the signal",
    and there was no way to tell how many were reversals and how many were
    marginal signals falling under their own bar.
    """
    from cfdinvest.advisor.livecheck import holds

    faded = {"AAPL": {"direction": "flat", "confidence": 0.0}}
    reversed_ = {"AAPL": {"direction": "short", "confidence": 0.9}}

    ok, why_faded = holds(faded, "AAPL", "long", 0.35)
    assert ok is False
    ok, why_reversed = holds(reversed_, "AAPL", "long", 0.35)
    assert ok is False

    assert why_faded != why_reversed, "a fade and a reversal read the same"
    assert "flat" not in why_faded, "the reason should read as prose, not state"
    assert "revers" in why_reversed


# --------------------------------------------------------------------------- #
# M257 the mail carrying every arm's events
# M258 the arm named in the mail rather than read from the entry bar
# --------------------------------------------------------------------------- #
