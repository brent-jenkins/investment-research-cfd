"""Operational monitoring: is the machine alright?

The layer exists to catch a system that *looks* fine, so most of what follows
is about the difference between "checked and clear" and "not checked" — a
distinction that, collapsed, turns a monitoring layer into a reassurance
machine.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from cfdinvest.config import AppConfig, PathConfig
from cfdinvest.monitoring import (
    MonitoringIssue,
    MonitoringThresholds,
    aggregate_monitoring_issues,
    check_confirmation_mismatches,
    check_disk_space,
    check_email_delivery,
    check_lake_freshness,
    check_missing_frames,
    check_pipeline_anomalies,
    check_run_failures,
    check_scheduler_health,
    check_system_resources,
    summarise_monitoring_state,
)
from cfdinvest.monitoring.core import TYPES

NOW = pd.Timestamp("2026-08-25 12:00", tz="UTC")
LIMITS = MonitoringThresholds()


@pytest.fixture
def lake(tmp_path) -> AppConfig:
    return AppConfig(paths=PathConfig(data_root=tmp_path / "data"))


def severities(issues) -> list[str]:
    return [issue.severity for issue in issues]


def types(issues) -> set[str]:
    return {issue.type for issue in issues}


# --------------------------------------------------------------------------- #
# The issue itself
# --------------------------------------------------------------------------- #
def test_every_issue_carries_a_remedy_or_says_nothing():
    """"Disk space low" is not actionable; the email has nowhere else to look."""
    issue = MonitoringIssue("disk", "critical", "0.4 GB free", remedy="Prune runs.")
    assert issue.to_document()["remedy"] == "Prune runs."


def test_an_invented_type_is_refused():
    with pytest.raises(ValueError, match="type must be one of"):
        MonitoringIssue("weather", "critical", "it is raining")


def test_an_invented_severity_is_refused():
    with pytest.raises(ValueError, match="severity must be one of"):
        MonitoringIssue("disk", "apocalyptic", "gone")


# --------------------------------------------------------------------------- #
# Lake freshness
# --------------------------------------------------------------------------- #
def test_a_fresh_lake_raises_nothing():
    assert check_lake_freshness(
        {"newest": NOW - pd.Timedelta("1D")}, LIMITS, now=NOW
    ) == []


def test_a_weekend_is_not_staleness():
    """Three days covers a normal weekend; four means a Friday run was missed."""
    assert check_lake_freshness(
        {"newest": NOW - pd.Timedelta("2D22h")}, LIMITS, now=NOW
    ) == []


def test_a_stale_lake_is_critical():
    """Age is measured in TRADING days now, not calendar days.

    Six calendar days spanning a weekend is four trading days, and the
    difference is the whole point: a Friday close read as three calendar
    days stale every Monday breakfast and fired a critical alert on a
    weekly schedule. Both figures are reported so the message can say which
    it means.
    """
    issues = check_lake_freshness(
        {"newest": NOW - pd.Timedelta("6D")}, LIMITS, now=NOW
    )
    assert severities(issues) == ["critical"]
    assert issues[0].details["age_days"] < issues[0].details["calendar_days"], (
        "trading-day age is not below calendar age across a weekend, so the "
        "measure is still counting closed days"
    )
    assert issues[0].details["calendar_days"] == pytest.approx(6.0)


def test_a_weekend_is_not_a_stale_lake():
    """The alert this rewrite exists to stop sending.

    Friday's close, read on Monday morning before the open: nothing has
    traded since, so nothing is stale -- however many calendar days have
    passed.
    """
    friday = pd.Timestamp("2026-08-28 19:00", tz="UTC")
    monday = pd.Timestamp("2026-08-31 07:33", tz="UTC")
    issues = check_lake_freshness({"newest": friday}, LIMITS, now=monday)
    assert issues == [], (
        "a weekend still reads as a stale lake; this fires every Monday and "
        "trains the reader to ignore the alert"
    )


def test_no_lake_metadata_is_a_warning_not_a_pass():
    """The rule the whole layer turns on."""
    issues = check_lake_freshness(None, LIMITS, now=NOW)
    assert severities(issues) == ["warning"]
    assert "could not be checked" in issues[0].message


def test_metadata_without_a_newest_bar_is_also_unchecked():
    issues = check_lake_freshness({"tickers": ["SPY"]}, LIMITS, now=NOW)
    assert severities(issues) == ["warning"]


# --------------------------------------------------------------------------- #
# Scheduler
# --------------------------------------------------------------------------- #
def test_a_healthy_scheduler_raises_nothing():
    assert check_scheduler_health(
        {
            "last_run": (NOW - pd.Timedelta("2h")).isoformat(),
            "last_success": (NOW - pd.Timedelta("2h")).isoformat(),
        },
        LIMITS, now=NOW,
    ) == []


def test_a_run_that_started_and_died_is_critical():
    """No amount of looking at run directories could tell you this."""
    issues = check_scheduler_health(
        {"last_run": (NOW - pd.Timedelta("2h")).isoformat(), "last_success": None},
        LIMITS, now=NOW,
    )
    assert severities(issues) == ["critical"]
    assert "never completed" in issues[0].message


def test_a_stale_scheduler_is_critical():
    old = (NOW - pd.Timedelta("72h")).isoformat()
    issues = check_scheduler_health(
        {"last_run": old, "last_success": old}, LIMITS, now=NOW
    )
    assert severities(issues) == ["critical"]


def test_no_runs_recorded_is_a_warning():
    """Expected on a fresh install, and not a clean bill of health."""
    assert severities(check_scheduler_health({}, LIMITS, now=NOW)) == ["warning"]


def test_no_state_file_is_a_warning():
    assert severities(check_scheduler_health(None, LIMITS, now=NOW)) == ["warning"]


def test_the_scheduler_check_agrees_with_the_risk_dashboard():
    """Two layers reading the same state must not disagree about a dead run."""
    from cfdinvest.risk import compute_scheduler_status

    state = {"last_run": (NOW - pd.Timedelta("2h")).isoformat(), "last_success": None}
    assert compute_scheduler_status(state, now=NOW)["run_status"] == "FAILED"
    assert severities(check_scheduler_health(state, LIMITS, now=NOW)) == ["critical"]


def test_past_failures_are_noted_but_do_not_alarm():
    issues = check_scheduler_health(
        {
            "last_run": NOW.isoformat(),
            "last_success": NOW.isoformat(),
            "failures": 3,
        },
        LIMITS, now=NOW,
    )
    assert severities(issues) == ["info"]


# --------------------------------------------------------------------------- #
# Disk
# --------------------------------------------------------------------------- #
def test_plenty_of_disk_raises_nothing():
    assert check_disk_space(
        {"free_gb": 100.0, "total_gb": 500.0}, LIMITS, now=NOW
    ) == []


def test_a_full_disk_is_critical():
    issues = check_disk_space({"free_gb": 0.4, "total_gb": 500.0}, LIMITS, now=NOW)
    assert severities(issues) == ["critical"]


def test_a_proportionally_full_disk_is_critical_even_with_room():
    """A percentage alone is useless on a small volume; an absolute on a large one."""
    issues = check_disk_space(
        {"free_gb": 40.0, "total_gb": 1_000.0}, LIMITS, now=NOW
    )
    assert severities(issues) == ["critical"]


def test_approaching_the_floor_is_a_warning():
    """Being told at the floor is being told too late to be gentle about it."""
    issues = check_disk_space({"free_gb": 3.0, "total_gb": 40.0}, LIMITS, now=NOW)
    assert severities(issues) == ["warning"]


def test_an_unreadable_disk_is_a_warning():
    assert severities(check_disk_space({"free_gb": None}, LIMITS, now=NOW)) == [
        "warning"
    ]


def test_the_disk_reading_is_real(tmp_path):
    from cfdinvest.monitoring import read_disk_usage

    usage = read_disk_usage(tmp_path)
    assert usage["free_gb"] > 0
    assert usage["total_gb"] >= usage["free_gb"]


def test_the_disk_reading_walks_up_to_a_volume_that_exists(tmp_path):
    """A fresh install has no data root yet; the volume still matters."""
    from cfdinvest.monitoring import read_disk_usage

    assert read_disk_usage(tmp_path / "not" / "created" / "yet")["free_gb"] > 0


# --------------------------------------------------------------------------- #
# Runs and artefacts
# --------------------------------------------------------------------------- #
def test_a_good_run_raises_nothing():
    assert check_run_failures({"status": "ok", "run_id": "r1"}, now=NOW) == []


def test_a_failed_run_is_critical():
    issues = check_run_failures(
        {"status": "failed", "run_id": "r1", "error": "boom"}, now=NOW
    )
    assert severities(issues) == ["critical"]
    assert "boom" in issues[0].message


def test_an_empty_run_is_a_warning_not_a_failure():
    """Normal when no signal fired."""
    assert severities(check_run_failures({"status": "empty"}, now=NOW)) == ["warning"]


def test_run_warnings_are_information():
    issues = check_run_failures(
        {"status": "ok", "warnings": ["a gap was filled"]}, now=NOW
    )
    assert severities(issues) == ["info"]


def test_a_complete_run_is_missing_nothing():
    assert check_missing_frames(
        {"trades", "positions", "equity", "summary", "metadata",
         "pnl", "analysis", "regimes", "attribution"},
        now=NOW,
    ) == []


def test_a_missing_required_artefact_is_critical():
    """Half a run looks exactly like a whole one from the outside."""
    issues = check_missing_frames(
        {"trades", "positions", "equity", "summary"}, now=NOW
    )
    assert "critical" in severities(issues)
    assert "metadata" in issues[0].message


def test_a_missing_optional_artefact_is_information():
    issues = check_missing_frames(
        {"trades", "positions", "equity", "summary", "metadata"}, now=NOW
    )
    assert severities(issues) == ["info"]


def test_no_run_directory_at_all_is_a_warning():
    assert severities(check_missing_frames(None, now=NOW)) == ["warning"]


# --------------------------------------------------------------------------- #
# Confirmations
# --------------------------------------------------------------------------- #
def panel(**held) -> pd.DataFrame:
    index = pd.bdate_range("2026-08-20", periods=2, tz="UTC", name="timestamp")
    return pd.DataFrame({k: [0.0, v] for k, v in held.items()}, index=index)


def book(**held) -> dict:
    return {
        name: {"quantity": quantity, "at": NOW, "action": "open"}
        for name, quantity in held.items()
    }


def test_nothing_confirmed_is_not_a_mismatch():
    """Otherwise every unattended week looks broken."""
    assert check_confirmation_mismatches(panel(SPY=1.0), {}, LIMITS, now=NOW) == []


def test_a_matching_confirmation_raises_nothing():
    assert check_confirmation_mismatches(
        panel(SPY=1.0), book(SPY=1.0), LIMITS, now=NOW
    ) == []


def test_rounding_to_what_the_ticket_takes_is_not_drift():
    assert check_confirmation_mismatches(
        panel(SPY=1.0), book(SPY=0.95), LIMITS, now=NOW
    ) == []


def test_a_real_difference_is_a_warning():
    issues = check_confirmation_mismatches(
        panel(SPY=1.0), book(SPY=0.5), LIMITS, now=NOW
    )
    assert severities(issues) == ["warning"]
    assert issues[0].details["drifted"]["SPY"]["confirmed"] == 0.5


def test_holding_something_the_system_does_not_trade_is_information():
    """Perfectly allowed, and carrying risk the engine is not modelling."""
    issues = check_confirmation_mismatches(
        panel(SPY=1.0), book(SPY=1.0, DOGE=5.0), LIMITS, now=NOW
    )
    assert severities(issues) == ["info"]
    assert "DOGE" in issues[0].message


def test_a_long_untouched_position_is_worth_asking_about():
    stale = {"GLD": {"quantity": 1.0, "at": NOW - pd.Timedelta("60D")}}
    issues = check_confirmation_mismatches(panel(GLD=1.0), stale, LIMITS, now=NOW)
    assert severities(issues) == ["warning"]
    assert "open since" in issues[0].message


def test_confirmations_with_no_simulation_are_unchecked():
    assert severities(
        check_confirmation_mismatches(None, book(SPY=1.0), LIMITS, now=NOW)
    ) == ["warning"]


# --------------------------------------------------------------------------- #
# Email
# --------------------------------------------------------------------------- #
def test_mail_that_went_raises_nothing():
    assert check_email_delivery({"enabled": True, "sent": 3, "failed": 0}, now=NOW) == []


def test_mail_that_all_failed_is_critical():
    """While this is failing, a failed run will not reach you."""
    issues = check_email_delivery(
        {"enabled": True, "sent": 0, "failed": 2, "last_error": "relay down"},
        now=NOW,
    )
    assert severities(issues) == ["critical"]
    assert issues[0].details["last_error"] == "relay down"


def test_some_mail_failing_is_a_warning():
    assert severities(
        check_email_delivery({"enabled": True, "sent": 3, "failed": 1}, now=NOW)
    ) == ["warning"]


def test_mail_switched_off_is_worth_saying_once():
    assert severities(check_email_delivery({"enabled": False}, now=NOW)) == ["info"]


def test_no_mail_state_raises_nothing():
    assert check_email_delivery(None, now=NOW) == []


def test_the_delivery_journal_records_a_failure():
    """send() swallows failures by design, which is why they surface here."""
    from cfdinvest.schedule import notify

    notify.reset_delivery_journal()
    try:
        notify.send(
            notify.build_error_alert("boom"),
            transport=lambda message: (_ for _ in ()).throw(RuntimeError("relay down")),
        )
        state = notify.delivery_state()
        assert state["failed"] == 1 and state["sent"] == 0
        assert "relay down" in state["last_error"]
        assert severities(check_email_delivery(state, now=NOW)) == ["critical"]
    finally:
        notify.reset_delivery_journal()


def test_the_delivery_journal_records_a_success():
    from cfdinvest.schedule import notify

    notify.reset_delivery_journal()
    try:
        notify.send(notify.build_error_alert("boom"), transport=lambda message: None)
        assert notify.delivery_state()["sent"] == 1
    finally:
        notify.reset_delivery_journal()


# --------------------------------------------------------------------------- #
# Pipeline anomalies
# --------------------------------------------------------------------------- #
def test_a_normal_run_raises_nothing():
    assert check_pipeline_anomalies(
        {"bars": 500},
        {"initial_equity": 100_000.0, "final_equity": 105_000.0,
         "max_drawdown_fraction": -0.06},
        LIMITS, now=NOW,
    ) == []


def test_a_wiped_out_account_is_a_bug_not_a_market():
    issues = check_pipeline_anomalies(
        {"bars": 500},
        {"initial_equity": 100_000.0, "final_equity": 0.0},
        LIMITS, now=NOW,
    )
    assert "critical" in severities(issues)


def test_a_nan_equity_is_critical():
    """One NaN makes every metric NaN, and none of them look wrong."""
    issues = check_pipeline_anomalies(
        {"bars": 500},
        {"initial_equity": 100_000.0, "final_equity": float("nan")},
        LIMITS, now=NOW,
    )
    assert "critical" in severities(issues)


def test_a_run_over_no_bars_is_critical():
    issues = check_pipeline_anomalies(
        {"bars": 0}, {"initial_equity": 1.0, "final_equity": 1.0}, LIMITS, now=NOW
    )
    assert any("no bars" in issue.message for issue in issues)


def test_a_big_drawdown_is_a_market_and_a_total_one_is_a_bug():
    """The distinction the check exists to draw."""
    market = check_pipeline_anomalies(
        {"bars": 500},
        {"initial_equity": 100.0, "final_equity": 60.0,
         "max_drawdown_fraction": -0.40},
        LIMITS, now=NOW,
    )
    bug = check_pipeline_anomalies(
        {"bars": 500},
        {"initial_equity": 100.0, "final_equity": 1.0,
         "max_drawdown_fraction": -0.995},
        LIMITS, now=NOW,
    )
    assert market == []
    assert "critical" in severities(bug)


def test_fx_without_a_recorded_source_is_a_warning():
    issues = check_pipeline_anomalies(
        {"bars": 5},
        {"initial_equity": 1.0, "final_equity": 1.0, "fx_enabled": True},
        LIMITS, now=NOW,
    )
    assert severities(issues) == ["warning"]


def test_no_execution_metadata_is_unchecked():
    assert severities(check_pipeline_anomalies({}, None, LIMITS, now=NOW)) == [
        "warning"
    ]


# --------------------------------------------------------------------------- #
# The machine
# --------------------------------------------------------------------------- #
def test_a_quiet_box_raises_nothing():
    assert check_system_resources(12.0, 40.0, LIMITS, now=NOW) == []


def test_a_loaded_box_is_a_warning_not_a_failure():
    """A box at 95% memory is doing its job right up until it is not."""
    issues = check_system_resources(95.0, 96.0, LIMITS, now=NOW)
    assert severities(issues) == ["warning", "warning"]


def test_unmeasured_resources_say_so_rather_than_passing():
    """Absent is not the same as healthy."""
    issues = check_system_resources(None, None, LIMITS, now=NOW)
    assert severities(issues) == ["info"]
    assert "not measured" in issues[0].message


def test_one_reading_present_does_not_invent_the_other():
    issues = check_system_resources(95.0, None, LIMITS, now=NOW)
    assert len(issues) == 1
    assert "CPU" in issues[0].message


def test_reading_the_machine_never_raises_without_psutil(tmp_path, monkeypatch):
    """The dependency is optional, and its absence must degrade not crash."""
    import builtins

    from cfdinvest.monitoring import resources as resources_module

    real = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name == "psutil":
            raise ImportError("no psutil here")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)
    reading = resources_module.read_system_resources(tmp_path)

    assert reading["cpu_percent"] is None
    assert reading["psutil"] is False
    assert reading["free_gb"] > 0
    assert "cpu" not in reading["measured"]


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #
def issue(severity, kind="disk"):
    return MonitoringIssue(kind, severity, f"a {severity} thing")


def test_nothing_wrong_is_green():
    assert aggregate_monitoring_issues([])["overall_status"] == "green"


def test_a_warning_is_yellow():
    assert aggregate_monitoring_issues([issue("warning")])["overall_status"] == "yellow"


def test_a_critical_is_red():
    aggregate = aggregate_monitoring_issues([issue("warning"), issue("critical")])
    assert aggregate["overall_status"] == "red"
    assert aggregate["critical_count"] == 1


def test_information_alone_stays_green():
    """A note that psutil is missing is not a reason to page anyone."""
    assert aggregate_monitoring_issues([issue("info")])["overall_status"] == "green"


def test_issues_are_ordered_worst_first():
    aggregate = aggregate_monitoring_issues(
        [issue("info"), issue("critical"), issue("warning")]
    )
    assert severities(aggregate["issues"]) == ["critical", "warning", "info"]


def test_clean_types_are_reported():
    """A summary of only what broke cannot say what was looked at."""
    aggregate = aggregate_monitoring_issues([issue("critical", "disk")])
    assert "disk" not in aggregate["clean_types"]
    assert set(aggregate["clean_types"]) == set(TYPES) - {"disk"}


def test_the_headline_names_the_worst_thing_not_a_count():
    """"7 issues" tells you nothing about whether to get out of bed."""
    summary = summarise_monitoring_state(
        [issue("warning"), MonitoringIssue("lake", "critical", "The lake is stale.")],
        now=NOW,
    )
    assert summary["headline"].startswith("The lake is stale.")
    assert "1 other" in summary["headline"]


def test_a_clean_headline_says_what_was_checked():
    assert summarise_monitoring_state([], now=NOW)["headline"] == (
        "Everything checked is clear."
    )


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
def snapshot_of(issues=(), **changes):
    from cfdinvest.monitoring.aggregate import MonitoringSnapshot

    return MonitoringSnapshot.from_summary(
        summarise_monitoring_state(list(issues), now=changes.pop("now", NOW)),
        **changes,
    )


def test_a_snapshot_round_trips(lake):
    from cfdinvest.persist import monitoring as store

    path = store.write_monitoring_snapshot(
        snapshot_of([issue("critical")]), lake, now=NOW
    )
    assert Path(path).name == "20260825-120000-monitor.json"

    document = store.load_latest_monitoring_snapshot(lake)
    assert document["overall_status"] == "red"
    assert document["issues"][0]["severity"] == "critical"
    assert document["schema_version"] == store.SCHEMA_VERSION


def test_snapshots_accumulate(lake):
    from cfdinvest.persist import monitoring as store

    for hour in (9, 10, 11):
        store.write_monitoring_snapshot(
            snapshot_of(now=pd.Timestamp(f"2026-08-25 {hour}:00", tz="UTC")), lake
        )
    assert len(store.list_monitoring_snapshots(lake)) == 3


def test_the_history_is_a_table(lake):
    from cfdinvest.persist import monitoring as store

    store.write_monitoring_snapshot(snapshot_of(), lake)
    store.write_monitoring_snapshot(
        snapshot_of([issue("critical")], now=NOW + pd.Timedelta("1h")), lake
    )
    history = store.load_monitoring_history(lake)

    assert list(history["overall_status"]) == ["green", "red"]
    assert history["timestamp"].is_monotonic_increasing


def test_a_failed_write_returns_none_rather_than_raising(lake, monkeypatch):
    """The likeliest reason a monitoring write fails is the full disk it
    exists to report. Crashing would replace the report with a traceback."""
    from cfdinvest.persist import monitoring as store

    monkeypatch.setattr(
        store, "atomic_write_text",
        lambda *a, **k: (_ for _ in ()).throw(OSError("no space left on device")),
    )
    assert store.write_monitoring_snapshot(snapshot_of(), lake, now=NOW) is None


def test_a_corrupt_snapshot_is_skipped(lake):
    from cfdinvest.persist import monitoring as store

    store.write_monitoring_snapshot(snapshot_of(), lake, now=NOW)
    (store.monitoring_root(lake) / "20260825-130000-monitor.json").write_text(
        "{not json", encoding="utf-8"
    )
    assert store.load_latest_monitoring_snapshot(lake) is not None
    assert len(store.load_monitoring_history(lake)) == 1


def test_two_passes_in_the_same_second_do_not_overwrite(lake):
    """Append-only should be true, not nearly true."""
    from cfdinvest.persist import monitoring as store

    store.write_monitoring_snapshot(snapshot_of(), lake)
    store.write_monitoring_snapshot(snapshot_of([issue("critical")]), lake)

    assert len(store.list_monitoring_snapshots(lake)) == 2


def test_the_filename_and_the_content_agree(lake):
    """Otherwise "the latest file" and "the latest timestamp" differ."""
    from cfdinvest.persist import monitoring as store

    path = Path(store.write_monitoring_snapshot(snapshot_of(now=NOW), lake))
    document = json.loads(path.read_text(encoding="utf-8"))

    assert path.stem.startswith("20260825-120000")
    assert pd.Timestamp(document["timestamp"]) == NOW


def test_no_snapshots_is_an_empty_table(lake):
    from cfdinvest.persist import monitoring as store

    assert store.load_latest_monitoring_snapshot(lake) is None
    assert store.load_monitoring_history(lake).empty


def test_pruning_keeps_the_newest(lake):
    from cfdinvest.persist import monitoring as store

    for hour in range(9, 15):
        store.write_monitoring_snapshot(
            snapshot_of(now=pd.Timestamp(f"2026-08-25 {hour}:00", tz="UTC")), lake
        )
    assert store.prune_monitoring_snapshots(lake, keep=2) == 4

    remaining = [p.name for p in store.list_monitoring_snapshots(lake)]
    assert remaining == ["20260825-140000-monitor.json", "20260825-130000-monitor.json"]


# --------------------------------------------------------------------------- #
# The thresholds are shared
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "name",
    [
        "lake_stale_days",
        "scheduler_stale_hours",
        "missed_run_grace_minutes",
        "disk_free_gb",
        "disk_free_fraction",
        "publication_calendar_stale_days",
    ],
)
def test_the_banner_and_the_alert_read_the_same_numbers(name):
    """The day they drift is the day the banner says fine and the email says
    critical, and neither is obviously the one to believe."""
    from cfdinvest.ui.health import HealthThresholds

    assert getattr(HealthThresholds(), name) == getattr(MonitoringThresholds(), name)


# --------------------------------------------------------------------------- #
# Collecting, against a real lake
# --------------------------------------------------------------------------- #
@pytest.fixture
def recorded_lake(tmp_path):
    """A lake with one completed run, written by the real scheduler."""
    from tests.test_analytics import TICKERS, build_lake
    from cfdinvest.backtest.runner import BacktestConfig
    from cfdinvest.repository import DataRepository
    from cfdinvest.schedule.runner import RunConfig
    from cfdinvest.schedule.runner import run_pipeline as run_scheduled

    config = build_lake(tmp_path / "data", bars=260)

    def run(**changes):
        settings = RunConfig(backtest=BacktestConfig(interval="1d"), **changes)
        summary = run_scheduled(
            list(TICKERS),
            settings,
            repository=DataRepository(config),
            now=pd.Timestamp("2026-08-25 07:00", tz="UTC"),
        )
        assert summary.status == "ok", summary.error
        return summary

    return config, run


def test_a_pass_over_a_real_run_finds_the_stale_lake(recorded_lake):
    """The fixture's data ends in the past, so this is the true answer."""
    from cfdinvest.monitoring import run_monitoring

    config, run = recorded_lake
    run(monitoring=False)
    snapshot = run_monitoring(config, store=False, now=NOW)

    assert snapshot.overall_status == "red"
    assert "lake" in types(snapshot.issues)


def test_a_pass_writes_no_run(recorded_lake):
    """A monitoring pass that could start a run would report on a system it
    had just disturbed."""
    from cfdinvest.monitoring import run_monitoring
    from cfdinvest.persist import readers

    config, run = recorded_lake
    run(monitoring=False)
    before = readers.list_runs(config)

    run_monitoring(config, store=False, now=NOW)
    assert readers.list_runs(config) == before


def test_the_collector_imports_nothing_that_could_trade():
    """Structural. The repository is allowed: it reads a written manifest."""
    import ast

    for name in ("core.py", "aggregate.py", "collect.py", "resources.py"):
        source = Path("cfdinvest/monitoring") / name
        tree = ast.parse(source.read_text(encoding="utf-8"))
        imported = {
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        } | {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        forbidden = ("cfdinvest.backtest", "cfdinvest.schedule.runner", "requests",
                     "urllib", "httpx", "selenium")
        assert not [
            module
            for module in imported
            if any(module == bad or module.startswith(bad + ".") for bad in forbidden)
        ], f"{name} imports something it should not"


def test_the_monitoring_layer_does_not_need_the_dashboard():
    """It has to work on a headless box where streamlit was never installed."""
    import ast

    for name in ("core.py", "aggregate.py", "collect.py", "resources.py"):
        tree = ast.parse(
            (Path("cfdinvest/monitoring") / name).read_text(encoding="utf-8")
        )
        modules = {
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        } | {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        assert not [
            module
            for module in modules
            if module == "streamlit"
            or module == "cfdinvest.ui"
            or module.startswith("cfdinvest.ui.")
            or module.startswith("streamlit.")
        ], f"{name} needs the dashboard"


def test_one_failing_check_does_not_silence_the_others(recorded_lake, monkeypatch):
    """Which is exactly the case where you most need the other eight."""
    from cfdinvest.monitoring import collect as collect_module
    from cfdinvest.monitoring import core as core_module

    config, run = recorded_lake
    run(monitoring=False)

    monkeypatch.setattr(
        core_module, "check_disk_space",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("check is broken")),
    )
    issues, _ = collect_module.collect_issues(config, now=NOW)

    assert "lake" in types(issues)
    broken = [i for i in issues if "check itself failed" in i.message]
    assert len(broken) == 1
    assert broken[0].severity == "warning"


def test_the_runner_records_a_pass(recorded_lake):
    from cfdinvest.persist import monitoring as store

    config, run = recorded_lake
    run(monitoring_alerts=False)

    document = store.load_latest_monitoring_snapshot(config)
    assert document is not None
    assert document["overall_status"] in ("green", "yellow", "red")


def test_the_runner_alerts_only_on_red(recorded_lake, monkeypatch):
    """An alert that arrives daily for a yellow is one nobody opens on the
    day it is red."""
    from cfdinvest.schedule import notify

    config, run = recorded_lake
    sent = []
    monkeypatch.setattr(
        notify, "send_monitoring_alert",
        lambda snapshot, *a, **k: sent.append(snapshot) or True,
    )
    run(monitoring_alerts=True)

    # The fixture lake is stale, so this run is genuinely red.
    assert len(sent) == 1
    assert sent[0].overall_status == "red"


def test_the_runner_stays_quiet_on_yellow(recorded_lake, monkeypatch):
    """The other half of "only on red", and the half a red fixture cannot show.

    A daily alert for a yellow is an alert nobody opens on the day it is red.
    """
    import cfdinvest.monitoring as monitoring_package
    from cfdinvest.schedule import notify

    config, run = recorded_lake
    sent = []
    monkeypatch.setattr(
        notify, "send_monitoring_alert",
        lambda snapshot, *a, **k: sent.append(snapshot) or True,
    )
    monkeypatch.setattr(
        monitoring_package, "run_monitoring",
        lambda *a, **k: snapshot_of([issue("warning")]),
    )
    run(monitoring_alerts=True)

    assert sent == []


def test_a_monitoring_failure_does_not_fail_the_run(recorded_lake, monkeypatch):
    """The layer that reports problems must not become one."""
    import cfdinvest.monitoring as monitoring_package

    config, run = recorded_lake
    monkeypatch.setattr(
        monitoring_package, "run_monitoring",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("monitoring exploded")),
    )
    assert run().status == "ok"


def test_monitoring_can_be_switched_off(recorded_lake):
    from cfdinvest.persist import monitoring as store

    config, run = recorded_lake
    run(monitoring=False)
    assert store.list_monitoring_snapshots(config) == []


# --------------------------------------------------------------------------- #
# The alert
# --------------------------------------------------------------------------- #
def test_the_alert_subject_carries_the_worst_problem():
    from cfdinvest.schedule.notify import build_monitoring_alert

    message = build_monitoring_alert(
        snapshot_of([MonitoringIssue("lake", "critical", "The lake is 6 days old.")])
    )
    assert "RED" in message["Subject"]
    assert "6 days old" in message["Subject"]


def test_the_alert_prints_every_remedy():
    """An alert that names a problem without a next step gets archived."""
    from cfdinvest.schedule.notify import build_monitoring_alert

    body = build_monitoring_alert(
        snapshot_of(
            [MonitoringIssue("disk", "critical", "0.4 GB free", remedy="Prune runs.")]
        )
    ).get_content()

    assert "Prune runs." in body


def test_the_alert_says_what_was_checked_and_clear():
    from cfdinvest.schedule.notify import build_monitoring_alert

    body = build_monitoring_alert(
        snapshot_of([MonitoringIssue("disk", "critical", "0.4 GB free")])
    ).get_content()

    assert "Checked and clear" in body
    assert "Data lake" in body


def test_the_alert_says_what_the_system_can_actually_do():
    """It used to claim the system "places no orders with any broker", which
    stopped being true on 2026-08-27 and survived in four files. A stale
    reassurance is worse than none in an alert."""
    from cfdinvest.schedule.notify import build_monitoring_alert

    body = build_monitoring_alert(snapshot_of()).get_content()
    assert "demo account" in body
    assert "live account" in body, (
        "the alert does not say what would have to change for it to be live"
    )
    assert "places no orders" not in body


def test_the_alert_says_when_the_machine_was_not_measured():
    from cfdinvest.schedule.notify import build_monitoring_alert

    body = build_monitoring_alert(
        snapshot_of(system_resources={"free_gb": 40.0, "measured": ("disk",)})
    ).get_content()

    assert "not measured" in body


# --------------------------------------------------------------------------- #
# M294 a credential that expires without warning
# M295 an unrecorded expiry read as "fine"
# M296 the reminder speaking every day for a year
# --------------------------------------------------------------------------- #
def test_a_key_close_to_expiring_warns_a_week_ahead():
    """M294: a total outage that arrives without notice.

    A key that expires is not a failure until it is, and then everything
    stops at once -- orders refused, readings absent -- with an
    authentication error indistinguishable from a wrong password. Replacing
    it needs a person, two-factor authentication and a web interface, none
    of which are available at 03:00 on the morning it lapses.
    """
    from cfdinvest.monitoring.core import KEY_EXPIRY_WARNING_DAYS, check_credential_expiry

    now = pd.Timestamp("2027-08-20", tz="UTC")
    inside = check_credential_expiry("2027-08-24", now=now)
    assert len(inside) == 1
    assert inside[0].severity == "warning"
    assert "expires in" in inside[0].message

    # Exactly at the boundary still speaks.
    edge = check_credential_expiry(
        str((now + pd.Timedelta(days=KEY_EXPIRY_WARNING_DAYS)).date()), now=now
    )
    assert edge and edge[0].severity == "warning"


def test_an_expired_key_is_critical_not_a_warning():
    from cfdinvest.monitoring.core import check_credential_expiry

    out = check_credential_expiry("2027-08-19", now=pd.Timestamp("2027-08-20", tz="UTC"))
    assert out[0].severity == "critical"
    assert "expired" in out[0].message


def test_an_unrecorded_expiry_is_unchecked_not_fine():
    """M295: the strongest claim from the weakest evidence.

    Nobody has to record an expiry. But staying silent about a credential
    whose lifetime is unknown would say it is fine, which is not something
    the absence of a date can support.
    """
    from cfdinvest.monitoring.core import check_credential_expiry

    for value in (None, "", "   ", "not-a-date"):
        out = check_credential_expiry(value)
        assert out, f"{value!r} produced no issue at all"
        assert out[0].severity == "warning"
        assert "could not be checked" in out[0].message


def test_the_reminder_is_silent_while_there_is_time():
    """M296: a check that speaks every day for eleven months.

    That is a check nobody reads in the twelfth, which is the month it
    matters.
    """
    from cfdinvest.monitoring.core import check_credential_expiry

    assert check_credential_expiry(
        "2028-01-01", now=pd.Timestamp("2027-08-20", tz="UTC")
    ) == []


def test_the_new_issue_type_is_registered_everywhere():
    """A type the aggregator cannot label shows as a bare token to a person.

    There is a closed set of types and a map of what each is called. Adding
    to one and not the other is how a new check ships looking like a bug.
    """
    from cfdinvest.monitoring.aggregate import TYPE_LABELS
    from cfdinvest.monitoring.core import TYPES

    assert "credentials" in TYPES
    assert set(TYPES) <= set(TYPE_LABELS), (
        f"types with no label: {sorted(set(TYPES) - set(TYPE_LABELS))}"
    )


def _curve(values, start="2026-01-01"):
    import numpy as np
    index = pd.date_range(start, periods=len(values), freq="D", tz="UTC")
    return pd.Series(np.asarray(values, dtype=float), index=index)


def test_a_book_that_halves_is_noticed_at_all():
    """M-live: monitoring watched staleness and wipeouts and nothing between.

    The only drawdown check fired at 99%, which is a broken run rather than
    a bad market, so a book could halve while every check reported green.
    The owner's policy is to run for growth and review when things turn bad;
    that policy needs something to tell them they have.
    """
    import numpy as np

    from cfdinvest.monitoring.core import review_triggers

    growing = _curve(np.linspace(5_000, 9_000, 200))
    assert review_triggers(growing) == [], "a healthy book raised a review"

    halved = _curve(np.concatenate([
        np.linspace(5_000, 9_000, 100), np.linspace(9_000, 4_200, 100)]))
    issues = review_triggers(halved)
    assert issues, "a book that more than halved from its peak said nothing"
    assert all(i.severity == "warning" for i in issues), (
        "a review trigger reported as critical -- it is a question for a "
        "person, not an outage"
    )
    assert any("below its peak" in i.message for i in issues)


def test_going_sideways_is_caught_when_no_drawdown_threshold_would():
    """The known failure mode for a trend follower does not announce itself
    as a crash. It announces itself as a flat line."""
    import numpy as np

    from cfdinvest.monitoring.core import review_triggers

    flat = _curve(np.concatenate([
        np.linspace(5_000, 9_000, 100), np.linspace(9_000, 8_500, 100)]))
    issues = review_triggers(flat)

    assert any("returned" in i.message for i in issues), (
        "months of going nowhere raised nothing, which is the gap this "
        "exists for -- the drawdown here is far too shallow to trip"
    )
    drawdown = float((flat / flat.cummax() - 1).min())
    assert abs(drawdown) < 0.10, "the fixture drew down enough to trip the other rule"


def test_a_review_trigger_never_reports_as_an_outage():
    """It must not be wired to execution, and must not read as something
    that stops trading. Four automatic pauses were tested causally on this
    strategy and every one lost money."""
    import inspect
    import numpy as np

    from cfdinvest.monitoring import core

    source = inspect.getsource(core.review_triggers)
    for forbidden in ("halt", "stop_trading", "pause", "disable", "flatten"):
        assert f"{forbidden}(" not in source and f"return {forbidden}" not in source, (
            f"{forbidden!r} appears in a review trigger -- it notifies and "
            "acts on nothing"
        )
    issues = core.review_triggers(_curve(np.linspace(9_000, 3_000, 120)))
    assert issues and all(i.severity != "critical" for i in issues)


def test_absent_equity_is_unchecked_rather_than_silent():
    from cfdinvest.monitoring.core import review_triggers

    for empty in (None, _curve([])):
        issues = review_triggers(empty)
        assert issues, "an unreadable book reported as fine"
        assert "could not be checked" in issues[0].message


def test_a_public_holiday_is_not_a_stale_lake():
    """The long-weekend case, which the plain weekend fix does not cover.

    Labour Day 2026 falls on Monday 7 September, so Friday's close is 3.5
    calendar days old by Tuesday breakfast -- worse than an ordinary
    weekend, and it would have fired the same two criticals a day later.
    Trading time across that gap is 0.15 session-days, because no session
    ran in between.

    The holiday calendar is the market's, not the machine's: the same code
    path also covers Thanksgiving, Independence Day observed on the 3rd, and
    Good Friday.
    """
    friday = pd.Timestamp("2026-09-04 19:00", tz="UTC")
    tuesday = pd.Timestamp("2026-09-08 07:33", tz="UTC")

    issues = check_lake_freshness({"newest": friday}, LIMITS, now=tuesday)
    assert issues == [], (
        "a three-day holiday weekend reads as a stale lake; the alert would "
        "fire on the Tuesday after every US market holiday"
    )


def test_the_holiday_calendar_covers_the_year():
    """Guards the assumption the test above rests on. A calendar missing a
    holiday would put the staleness measure back on calendar time for that
    week without anything saying so."""
    import datetime as dt

    from cfdinvest.schedule.market_calendar import us_market_holidays

    holidays = us_market_holidays(2026)
    assert dt.date(2026, 9, 7) in holidays, "Labour Day is missing"
    assert dt.date(2026, 11, 26) in holidays, "Thanksgiving is missing"
    assert dt.date(2026, 4, 3) in holidays, "Good Friday is missing"
    assert len(holidays) >= 9, f"only {len(holidays)} holidays in 2026"
