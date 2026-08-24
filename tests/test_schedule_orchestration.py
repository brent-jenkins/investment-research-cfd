"""Runner, notifications, scheduler and CLI — all offline.

The scheduler tests inject a fake clock, so a week of market events is
simulated in milliseconds and the assertions are about *which* events fired
rather than about elapsed wall-clock time.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from cfdinvest.backtest import BacktestConfig
from cfdinvest.ingestion.market import MarketDataIngestor
from cfdinvest.preprocessing import PreprocessConfig
from cfdinvest.repository import DataRepository
from cfdinvest.schedule import app as app_module
from cfdinvest.schedule.notify import (
    ConsoleTransport,
    RecordingTransport,
    SmtpConfig,
    build_daily_summary,
    build_error_alert,
    build_trade_alert,
    send,
    send_daily_summary,
    send_error_alert,
    send_test_email,
    send_trade_alert,
)
from cfdinvest.schedule.runner import RunConfig, RunSummary, load_summary, run_pipeline
from cfdinvest.schedule.scheduler import (
    ScheduleEvent,
    Scheduler,
    SchedulerConfig,
    any_open,
    crontab_lines,
    next_event,
    open_assets,
)
from tests.conftest import make_yf_frame


@pytest.fixture(autouse=True)
def restore_logging():
    """Undo the CLI's global logging configuration after each test.

    ``app.main()`` calls ``configure_logging``, which detaches the package
    logger from the root by setting ``propagate = False`` -- correct for an
    application entry point, and fatal for every later test that relies on
    ``caplog``. Snapshot and restore, or these tests silently break unrelated
    ones depending on collection order.
    """
    import logging

    package = logging.getLogger("cfdinvest")
    handlers = list(package.handlers)
    propagate, level = package.propagate, package.level
    try:
        yield
    finally:
        package.handlers = handlers
        package.propagate = propagate
        package.setLevel(level)


# --------------------------------------------------------------------------- #
# A small offline lake
# --------------------------------------------------------------------------- #
@pytest.fixture
def lake(config):
    """A repository backed by a real Parquet lake, built without network."""
    sessions = pd.bdate_range("2021-01-04", periods=420, name="Date")

    def downloader(tickers, **kwargs):
        return pd.concat(
            {t: make_yf_frame(sessions, 100.0 + 25 * i) for i, t in enumerate(tickers)},
            axis=1,
        )

    MarketDataIngestor(config, downloader=downloader).ingest(["SPY", "TLT", "GLD"])
    return DataRepository(config)


@pytest.fixture
def run_config():
    return RunConfig(
        backtest=BacktestConfig(
            tickers=["SPY", "TLT", "GLD"],
            preprocess=PreprocessConfig(correlation_window=63),
        ),
        keep_runs=5,
    )


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
def test_run_produces_a_summary(lake, run_config):
    summary = run_pipeline(["SPY", "TLT", "GLD"], run_config, repository=lake)

    assert summary.status == "ok"
    assert summary.ok
    assert summary.bars > 0
    assert summary.universe == ("GLD", "SPY", "TLT")
    assert summary.as_of is not None


def test_run_persists_its_outputs(lake, run_config, config):
    summary = run_pipeline(["SPY", "TLT", "GLD"], run_config, repository=lake)

    assert "summary" in summary.output_paths
    for path in summary.output_paths.values():
        assert Path(path).is_file()

    restored = load_summary(summary.output_paths["summary"])
    assert restored["run_id"] == summary.run_id
    assert restored["status"] == "ok"


def test_run_can_skip_persistence(lake, run_config):
    summary = run_pipeline(
        ["SPY"], replace(run_config, save_outputs=False), repository=lake
    )
    assert summary.output_paths == {}


def test_run_id_is_derived_from_the_clock(lake, run_config):
    moment = pd.Timestamp("2026-03-04 09:15", tz="UTC")
    summary = run_pipeline(["SPY"], run_config, repository=lake, now=moment)
    assert summary.run_id == "20260304T091500Z"


def test_runs_are_deterministic_for_a_fixed_clock(lake, run_config):
    moment = pd.Timestamp("2026-03-04 09:15", tz="UTC")
    first = run_pipeline(["SPY", "TLT"], run_config, repository=lake, now=moment)
    second = run_pipeline(["SPY", "TLT"], run_config, repository=lake, now=moment)

    assert first.run_id == second.run_id
    assert first.metrics["final_equity"] == pytest.approx(second.metrics["final_equity"])


def test_latest_slice_is_the_final_bar(lake, run_config):
    summary = run_pipeline(["SPY", "TLT", "GLD"], run_config, repository=lake)

    if not summary.latest_signals.empty:
        stamps = summary.latest_signals.index.get_level_values(0).unique()
        assert list(stamps) == [summary.as_of]
    if not summary.latest_trades.empty:
        assert (summary.latest_trades["timestamp"] == summary.as_of).all()


def test_a_failure_is_captured_not_raised(lake, run_config, monkeypatch):
    """An unattended run must not die silently."""

    def explode(*args, **kwargs):
        raise RuntimeError("vendor exploded")

    monkeypatch.setattr(
        "cfdinvest.schedule.runner.run_backtest_pipeline", explode
    )
    summary = run_pipeline(["SPY"], run_config, repository=lake)

    assert summary.status == "failed"
    assert not summary.ok
    assert "vendor exploded" in summary.error
    assert "traceback" in summary.metrics


def test_an_empty_universe_is_reported_not_crashed(lake, run_config):
    summary = run_pipeline(["NOT_INGESTED"], run_config, repository=lake)
    assert summary.status in {"empty", "ok"}
    assert summary.ok


def test_old_runs_are_pruned(lake, config):
    """Unattended runs accumulate; without a cap the disk fills up."""
    settings = RunConfig(
        backtest=BacktestConfig(
            tickers=["SPY"], preprocess=PreprocessConfig(correlation_window=63)
        ),
        keep_runs=3,
    )
    for hour in range(6):
        run_pipeline(
            ["SPY"], settings, repository=lake,
            now=pd.Timestamp(f"2026-03-04 {hour:02d}:00", tz="UTC"),
        )

    runs = sorted((Path(config.paths.data_root) / "runs").iterdir())
    assert len(runs) == 3
    # The newest survive.
    assert runs[-1].name == "20260304T050000Z"


def test_pruning_can_be_disabled(lake):
    settings = RunConfig(
        backtest=BacktestConfig(
            tickers=["SPY"], preprocess=PreprocessConfig(correlation_window=63)
        ),
        keep_runs=None,
    )
    for hour in range(3):
        summary = run_pipeline(
            ["SPY"], settings, repository=lake,
            now=pd.Timestamp(f"2026-03-05 {hour:02d}:00", tz="UTC"),
        )
    assert Path(summary.output_paths["summary"]).is_file()


def test_summary_serialises_to_json(lake, run_config):
    summary = run_pipeline(["SPY", "TLT"], run_config, repository=lake)
    text = json.dumps(summary.to_dict(), default=str)
    assert summary.run_id in text


def test_headline_describes_each_status():
    base = dict(
        run_id="R1",
        started_at=pd.Timestamp("2026-01-01", tz="UTC"),
        finished_at=pd.Timestamp("2026-01-01", tz="UTC"),
    )
    assert "FAILED" in RunSummary(**base, status="failed", error="boom").headline()
    assert "no data" in RunSummary(**base, status="empty").headline()


# --------------------------------------------------------------------------- #
# Notifications
# --------------------------------------------------------------------------- #
@pytest.fixture
def smtp() -> SmtpConfig:
    return SmtpConfig(
        sender="bot@example.com", recipients=("me@example.com",), enabled=True
    )


def test_config_from_environment():
    config = SmtpConfig.from_env(
        {
            "CFD_SMTP_HOST": "mail.example.com",
            "CFD_SMTP_PORT": "465",
            "CFD_SMTP_USER": "bot",
            "CFD_SMTP_PASSWORD": "hunter2",
            "CFD_SMTP_RECIPIENTS": "a@example.com, b@example.com",
        }
    )
    assert config.host == "mail.example.com"
    assert config.port == 465
    assert config.recipients == ("a@example.com", "b@example.com")
    assert config.sender == "bot"
    assert config.enabled is True


def test_config_defaults_to_disabled_without_recipients():
    assert SmtpConfig.from_env({}).enabled is False


def test_the_password_is_never_in_a_repr():
    config = SmtpConfig(username="bot", password="hunter2")
    assert "hunter2" not in repr(config)
    assert "redacted" in repr(config)


def test_validate_lists_every_problem():
    problems = SmtpConfig(username="bot").validate()
    assert any("recipients" in problem for problem in problems)
    assert any("password" in problem for problem in problems)


def test_a_complete_config_validates(smtp):
    assert smtp.validate() == []


def test_trade_alert_contains_the_trade(smtp):
    message = build_trade_alert(
        {
            "timestamp": pd.Timestamp("2026-01-05", tz="UTC"),
            "asset": "SPY",
            "action": "open_long",
            "traded_units": 12.5,
            "price": 420.0,
            "realized_pnl": 0.0,
        },
        smtp,
    )
    body = message.get_content()

    assert "open_long SPY" in message["Subject"]
    assert "SPY" in body
    assert "420" in body
    assert "No order was placed" in body


def test_daily_summary_contains_the_headline(lake, run_config, smtp):
    summary = run_pipeline(["SPY", "TLT", "GLD"], run_config, repository=lake)
    message = build_daily_summary(summary, smtp)
    body = message.get_content()

    assert "daily summary" in message["Subject"]
    assert summary.run_id in body
    assert "Active signals" in body
    assert "Simulated results" in body


def test_error_alert_names_the_failure(smtp):
    message = build_error_alert(ValueError("bad thing"), smtp, context="run R1")
    body = message.get_content()

    assert "RUN FAILED" in message["Subject"]
    assert "ValueError: bad thing" in body
    assert "run R1" in body


def test_messages_are_addressed_from_the_config(smtp):
    message = build_error_alert("boom", smtp)
    assert message["From"] == "bot@example.com"
    assert message["To"] == "me@example.com"
    assert message["Subject"].startswith("[cfdinvest]")


def test_recording_transport_captures_instead_of_sending(smtp):
    transport = RecordingTransport()
    assert send_error_alert("boom", smtp, transport) is True

    assert len(transport.messages) == 1
    assert "RUN FAILED" in transport.subjects[0]
    assert "boom" in transport.body()


def test_console_transport_writes_to_a_stream(smtp):
    import io

    stream = io.StringIO()
    send_error_alert("boom", smtp, ConsoleTransport(stream))
    assert "boom" in stream.getvalue()


def test_a_transport_failure_never_propagates(smtp):
    """A mail outage must not mark a completed run as broken."""

    def broken(message):
        raise ConnectionError("smtp down")

    assert send_error_alert("boom", smtp, broken) is False


def test_disabled_config_does_not_send():
    disabled = SmtpConfig(sender="a@b.c", recipients=("d@e.f",), enabled=False)
    assert send(build_error_alert("boom", disabled), disabled) is False


def test_incomplete_config_does_not_send():
    incomplete = SmtpConfig(enabled=True)
    assert send(build_error_alert("boom", incomplete), incomplete) is False


def test_test_email_reports_configuration_problems():
    transport = RecordingTransport()
    send_test_email(SmtpConfig(enabled=True), transport)
    assert "no recipients" in transport.body()


def test_send_helpers_all_work_through_a_transport(lake, run_config, smtp):
    summary = run_pipeline(["SPY"], run_config, repository=lake)
    transport = RecordingTransport()

    assert send_daily_summary(summary, smtp, transport)
    assert send_error_alert("boom", smtp, transport)
    assert send_trade_alert({"asset": "SPY", "action": "close"}, smtp, transport)
    assert len(transport.messages) == 3


# --------------------------------------------------------------------------- #
# Scheduler
# --------------------------------------------------------------------------- #
UNIVERSE = ["SPY", "TLT"]


def test_open_assets_reports_who_is_trading():
    assert open_assets(UNIVERSE, "2025-06-11 15:00") == ("SPY", "TLT")
    assert open_assets(UNIVERSE, "2025-06-14 15:00") == ()


def test_any_open():
    assert any_open(UNIVERSE, "2025-06-11 15:00")
    assert not any_open(UNIVERSE, "2025-06-14 15:00")


def test_next_event_is_the_earliest_close():
    config = SchedulerConfig(delay_minutes=15)
    event = next_event(UNIVERSE, "2025-06-11 15:00", config)

    # SPY closes at 20:00 UTC, TLT at 21:00; plus 15 minutes.
    assert event.at == pd.Timestamp("2025-06-11 20:15", tz="UTC")
    assert event.kind == "close"
    assert event.asset == "SPY"


def test_next_event_can_trigger_on_opens():
    config = SchedulerConfig(on_open=True, on_close=False, delay_minutes=0)
    event = next_event(["SPY"], "2025-06-11 21:00", config)
    assert event.at == pd.Timestamp("2025-06-12 13:30", tz="UTC")
    assert event.kind == "open"


def test_next_event_is_strictly_in_the_future():
    config = SchedulerConfig(delay_minutes=0)
    moment = pd.Timestamp("2025-06-11 20:00", tz="UTC")
    event = next_event(["SPY"], moment, config)
    assert event.at > moment


def test_next_event_of_a_continuous_universe():
    assert next_event(["BTC-USD"], "2025-06-11 15:00", SchedulerConfig()) is None


def test_scheduler_runs_at_each_event():
    """A fake clock walks the calendar; no real time passes."""
    clock = {"now": pd.Timestamp("2025-06-11 15:00", tz="UTC")}
    fired = []

    def advance(seconds):
        clock["now"] = clock["now"] + pd.Timedelta(seconds=seconds)

    scheduler = Scheduler(
        ["SPY"],
        job=lambda event: fired.append(event),
        config=SchedulerConfig(max_runs=3, delay_minutes=15),
        clock=lambda: clock["now"],
        sleeper=advance,
    )
    events = scheduler.run_forever()

    assert len(events) == 3
    assert len(fired) == 3
    assert all(event.kind == "close" for event in events)
    # Consecutive trading days.
    assert [event.at.date().isoformat() for event in events] == [
        "2025-06-11", "2025-06-12", "2025-06-13"
    ]


def test_the_scheduler_skips_the_weekend():
    clock = {"now": pd.Timestamp("2025-06-13 15:00", tz="UTC")}   # Friday

    def advance(seconds):
        clock["now"] = clock["now"] + pd.Timedelta(seconds=seconds)

    scheduler = Scheduler(
        ["SPY"], job=lambda event: None,
        config=SchedulerConfig(max_runs=2, delay_minutes=15),
        clock=lambda: clock["now"], sleeper=advance,
    )
    events = scheduler.run_forever()

    assert events[0].at.date().isoformat() == "2025-06-13"
    assert events[1].at.date().isoformat() == "2025-06-16"   # Monday


def test_a_failing_job_does_not_stop_the_loop():
    clock = {"now": pd.Timestamp("2025-06-11 15:00", tz="UTC")}
    errors = []

    def advance(seconds):
        clock["now"] = clock["now"] + pd.Timedelta(seconds=seconds)

    def explode(event):
        raise RuntimeError("run failed")

    scheduler = Scheduler(
        ["SPY"], job=explode,
        config=SchedulerConfig(max_runs=2, delay_minutes=15),
        clock=lambda: clock["now"], sleeper=advance,
        on_error=lambda error, event: errors.append(error),
    )
    events = scheduler.run_forever()

    assert len(events) == 2
    assert len(errors) == 2


def test_run_once_contains_a_failure():
    scheduler = Scheduler(
        ["SPY"], job=lambda event: (_ for _ in ()).throw(ValueError("nope"))
    )
    assert scheduler.run_once() is None
    assert len(scheduler.history) == 1


def test_a_continuous_universe_runs_immediately():
    scheduler = Scheduler(
        ["BTC-USD"], job=lambda event: "ran",
        config=SchedulerConfig(max_runs=1),
        clock=lambda: pd.Timestamp("2025-06-14 03:00", tz="UTC"),
        sleeper=lambda seconds: None,
    )
    assert scheduler.run_forever() == []
    assert len(scheduler.history) == 1


def test_crontab_lines_group_by_local_time():
    lines = crontab_lines(["SPY", "TLT", "EWJ"], "run-me")
    text = "\n".join(lines)

    assert "CRON_TZ=America/New_York" in text
    assert "CRON_TZ=Asia/Tokyo" in text
    assert "run-me" in text
    assert "* * 1-5" in text


def test_crontab_warns_about_timezones():
    text = "\n".join(crontab_lines(["SPY"]))
    assert "daylight-saving" in text


def test_crontab_skips_always_open_assets():
    assert not any("BTC" in line for line in crontab_lines(["BTC-USD"]))


def test_apscheduler_builds_jobs():
    pytest.importorskip("apscheduler")
    scheduler = None
    try:
        from cfdinvest.schedule.scheduler import build_apscheduler

        scheduler = build_apscheduler(["SPY", "TLT"], job=lambda event: None)
        assert len(scheduler.get_jobs()) == 2
    finally:
        if scheduler is not None and scheduler.running:  # pragma: no cover
            scheduler.shutdown()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_parser_accepts_every_command():
    parser = app_module.build_parser()
    for command in (
        "run-once", "run-continuous", "send-test-email", "market-status", "crontab"
    ):
        assert parser.parse_args([command]).command == command


def test_tickers_are_comma_separated_not_greedy():
    """A greedy list option would swallow the subcommand."""
    args = app_module.build_parser().parse_args(
        ["--tickers", "SPY,TLT,GLD", "market-status"]
    )
    assert args.command == "market-status"
    assert app_module._universe(args) == ["SPY", "TLT", "GLD"]


def test_crontab_command_option_does_not_clobber_the_subcommand():
    args = app_module.build_parser().parse_args(["crontab", "--command", "my-runner"])
    assert args.command == "crontab"
    assert args.cron_command == "my-runner"


def test_market_status_runs(capsys, config, monkeypatch):
    monkeypatch.setenv("CFD_DATA_ROOT", str(config.paths.data_root))
    code = app_module.main(["--tickers", "SPY,EURUSD=X", "market-status"])

    assert code == 0
    assert "SPY" in capsys.readouterr().out


def test_crontab_command_prints_lines(capsys):
    code = app_module.main(["--tickers", "SPY", "crontab"])
    assert code == 0
    assert "CRON_TZ" in capsys.readouterr().out


def test_send_test_email_dry_run(capsys):
    code = app_module.main(["--dry-run", "send-test-email"])
    output = capsys.readouterr().out

    assert code == 0
    assert "test email" in output


def test_send_test_email_reports_missing_configuration(capsys, monkeypatch):
    for name in (
        "CFD_SMTP_HOST", "CFD_SMTP_USER", "CFD_SMTP_PASSWORD",
        "CFD_SMTP_SENDER", "CFD_SMTP_RECIPIENTS", "CFD_SMTP_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)

    code = app_module.main(["send-test-email"])
    assert code == 2
    assert "not configured" in capsys.readouterr().err


def test_run_once_through_the_cli(lake, config, monkeypatch, capsys):
    # Pin every input the CLI would otherwise take from the ambient
    # environment, so the test cannot be steered by a stray CFD_* variable.
    for name in ("CFD_INTERVAL", "CFD_TICKERS", "CFD_START", "CFD_END"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CFD_DATA_ROOT", str(config.paths.data_root))

    code = app_module.main(
        [
            "--data-root", str(config.paths.data_root),
            "--tickers", "SPY,TLT,GLD",
            "--interval", "1d",
            "--log-level", "ERROR",
            "--no-email",
            "run-once", "--force",
        ]
    )
    assert code == 0
    assert "bars" in capsys.readouterr().out


def test_run_once_skips_a_closed_market(config, monkeypatch, capsys):
    monkeypatch.setattr(
        "cfdinvest.schedule.scheduler.any_open", lambda *a, **k: False
    )
    code = app_module.main(
        [
            "--data-root", str(config.paths.data_root),
            "--tickers", "SPY",
            "--log-level", "ERROR",
            "--no-email",
            "run-once",
        ]
    )
    assert code == 0
    assert "is_open" in capsys.readouterr().out


def test_run_once_returns_a_failure_code(lake, config, monkeypatch, capsys):
    def explode(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("cfdinvest.schedule.runner.run_backtest_pipeline", explode)
    code = app_module.main(
        [
            "--data-root", str(config.paths.data_root),
            "--tickers", "SPY",
            "--log-level", "CRITICAL",
            "--no-email",
            "run-once", "--force",
        ]
    )
    assert code == 1
