"""Portfolio risk: exposure, leverage, margin, currency, regimes, drawdown.

The properties worth asserting are about **not answering a question that was
not asked of the data**. A risk page that quietly assumes a leverage, a
currency or an account size produces a number that looks like a measurement
and is not one — so most of what follows checks that an absent input comes
back absent rather than filled in.

Nothing here runs the pipeline except the one integration test, which uses
the real runner so that a change to what gets persisted breaks this suite
rather than the dashboard.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from cfdinvest.config import AppConfig, PathConfig
from cfdinvest.risk import RiskLimits, scheduler_status, snapshot_from_disk
from cfdinvest.risk.model import (
    build_risk_snapshot,
    compute_current_risk,
    compute_fx_risk,
    compute_leverage_and_margin,
    compute_pnl_risk,
    compute_recent_trade_risk,
    compute_regime_risk,
)

ACCOUNT = RiskLimits(account_balance_gbp=2_000.0)


@pytest.fixture
def lake(tmp_path) -> AppConfig:
    return AppConfig(paths=PathConfig(data_root=tmp_path / "data"))


def positions(**held) -> pd.DataFrame:
    """A positions panel whose last bar carries ``held``."""
    index = pd.bdate_range("2026-01-01", periods=3, tz="UTC", name="timestamp")
    return pd.DataFrame(
        {name: [0.0, 0.0, units] for name, units in held.items()}, index=index
    )


def prices(**marks) -> pd.Series:
    return pd.Series(marks, dtype="float64")


def analysis(**marks) -> pd.DataFrame:
    """The stored close panel, in the long form the run persists.

    build_risk_snapshot takes no prices directly -- it reads them out of the
    analysis frame, which is the only place a real run has them.
    """
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2026-01-02"] * len(marks), utc=True),
            "panel": ["prices.close"] * len(marks),
            "series": list(marks),
            "value": [float(v) for v in marks.values()],
        }
    )


# --------------------------------------------------------------------------- #
# Exposure
# --------------------------------------------------------------------------- #
def test_exposure_is_units_times_price():
    result = compute_current_risk(
        positions(SPY=10.0, GLD=-5.0), ACCOUNT,
        prices=prices(SPY=100.0, GLD=80.0),
    )
    assert result["exposure_by_instrument"] == pytest.approx(
        {"SPY": 1_000.0, "GLD": 400.0}
    )
    assert result["gross_exposure_gbp"] == pytest.approx(1_400.0)


def test_a_short_adds_to_gross_and_subtracts_from_net():
    """Gross is what you are carrying; net is which way it leans."""
    result = compute_current_risk(
        positions(SPY=10.0, GLD=-5.0), ACCOUNT,
        prices=prices(SPY=100.0, GLD=80.0),
    )
    assert result["gross_exposure_gbp"] == pytest.approx(1_400.0)
    assert result["net_exposure_gbp"] == pytest.approx(600.0)


def test_only_the_latest_bar_counts():
    """Positions is a panel over time; the book is its last row."""
    index = pd.bdate_range("2026-01-01", periods=3, tz="UTC", name="timestamp")
    frame = pd.DataFrame({"SPY": [50.0, 20.0, 3.0]}, index=index)

    result = compute_current_risk(frame, ACCOUNT, prices=prices(SPY=100.0))
    assert result["gross_exposure_gbp"] == pytest.approx(300.0)


def test_a_flat_book_is_flat_not_missing():
    result = compute_current_risk(positions(SPY=0.0), ACCOUNT, prices=prices(SPY=1.0))
    assert result["flat"] is True
    assert result["open_positions"] == 0


def test_no_positions_at_all_reports_the_artefact_absent():
    result = compute_current_risk(None, ACCOUNT)
    assert result["flat"] is True
    assert "positions" in result["unavailable"]


def test_positions_without_prices_report_units_and_no_money():
    """Guessing a price would produce a measurement nobody made."""
    result = compute_current_risk(positions(SPY=10.0), ACCOUNT, prices=prices())

    assert result["units"] == {"SPY": 10.0}
    assert result["gross_exposure_gbp"] is None
    assert "prices" in result["unavailable"]


def test_one_missing_price_does_not_lose_the_others():
    result = compute_current_risk(
        positions(SPY=10.0, GLD=5.0), ACCOUNT, prices=prices(SPY=100.0)
    )
    assert result["exposure_by_instrument"] == pytest.approx({"SPY": 1_000.0})
    assert "price:GLD" in result["unavailable"]


def test_prices_are_read_from_the_stored_analysis_panels():
    """Long form: timestamp, panel, series, value."""
    analysis = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                ["2026-01-01", "2026-01-02", "2026-01-02"], utc=True
            ),
            "panel": ["prices.close"] * 3,
            "series": ["SPY", "SPY", "GLD"],
            "value": [90.0, 100.0, 80.0],
        }
    )
    result = compute_current_risk(
        positions(SPY=10.0, GLD=5.0), ACCOUNT, analysis_frame=analysis
    )
    assert result["exposure_by_instrument"]["SPY"] == pytest.approx(1_000.0)
    assert result["exposure_by_instrument"]["GLD"] == pytest.approx(400.0)


def test_concentration_is_reported():
    """A book inside every absolute limit can still be one position."""
    result = compute_current_risk(
        positions(SPY=10.0, GLD=1.0), ACCOUNT,
        prices=prices(SPY=100.0, GLD=100.0),
    )
    assert result["largest_instrument"] == "SPY"
    assert result["largest_instrument_share"] == pytest.approx(10 / 11)


def test_exposure_groups_by_asset_class():
    result = compute_current_risk(
        positions(SPY=10.0, GLD=5.0), ACCOUNT,
        prices=prices(SPY=100.0, GLD=80.0),
        asset_classes={"SPY": "equity", "GLD": "commodity"},
    )
    assert result["exposure_by_asset_class"] == pytest.approx(
        {"equity": 1_000.0, "commodity": 400.0}
    )


# --------------------------------------------------------------------------- #
# Leverage and margin
# --------------------------------------------------------------------------- #
def test_effective_leverage_is_exposure_over_balance():
    exposure = compute_current_risk(
        positions(SPY=100.0), ACCOUNT, prices=prices(SPY=100.0)
    )
    leverage, _ = compute_leverage_and_margin(exposure, ACCOUNT)
    assert leverage["effective_leverage"] == pytest.approx(5.0)


def test_margin_needs_the_instruments_leverage():
    """Trading 212 does not publish it, so it cannot be assumed."""
    exposure = compute_current_risk(
        positions(SPY=10.0), ACCOUNT, prices=prices(SPY=100.0)
    )
    _, margin = compute_leverage_and_margin(exposure, ACCOUNT)

    assert margin["margin_required_gbp"] is None
    assert margin["complete"] is False
    assert "SPY" in margin["unknown_leverage"]


def test_margin_is_computed_where_leverage_is_known():
    exposure = compute_current_risk(
        positions(SPY=10.0), ACCOUNT, prices=prices(SPY=100.0)
    )
    _, margin = compute_leverage_and_margin(
        exposure, ACCOUNT, instrument_leverage={"SPY": 20.0}
    )
    assert margin["margin_required_gbp"] == pytest.approx(50.0)
    assert margin["margin_usage_pct"] == pytest.approx(0.025)
    assert margin["complete"] is True


def test_partial_leverage_knowledge_is_reported_as_partial():
    exposure = compute_current_risk(
        positions(SPY=10.0, GLD=10.0), ACCOUNT,
        prices=prices(SPY=100.0, GLD=100.0),
    )
    _, margin = compute_leverage_and_margin(
        exposure, ACCOUNT, instrument_leverage={"SPY": 20.0}
    )
    assert margin["complete"] is False
    assert margin["unknown_leverage"] == ("GLD",)


def test_committed_margin_counts_towards_usage():
    loaded = RiskLimits(account_balance_gbp=2_000.0, used_margin_gbp=900.0)
    exposure = compute_current_risk(
        positions(SPY=10.0), loaded, prices=prices(SPY=100.0)
    )
    _, margin = compute_leverage_and_margin(
        exposure, loaded, instrument_leverage={"SPY": 20.0}
    )
    assert margin["margin_usage_pct"] == pytest.approx(0.475)


# --------------------------------------------------------------------------- #
# Currency
# --------------------------------------------------------------------------- #
def test_currency_exposure_groups_by_quote_currency():
    exposure = compute_current_risk(
        positions(SPY=10.0, UKX=5.0), ACCOUNT,
        prices=prices(SPY=100.0, UKX=100.0),
    )
    fx = compute_fx_risk(
        exposure, ACCOUNT, instrument_currencies={"SPY": "USD", "UKX": "GBP"}
    )
    assert fx["exposure_by_currency"] == pytest.approx({"USD": 1_000.0, "GBP": 500.0})
    assert fx["foreign_exposure_gbp"] == pytest.approx(1_000.0)
    assert fx["foreign_share_of_account"] == pytest.approx(0.5)


def test_unmapped_instruments_are_domestic_and_listed():
    """Understating foreign exposure visibly beats inventing it."""
    exposure = compute_current_risk(
        positions(MYSTERY=10.0), ACCOUNT, prices=prices(MYSTERY=100.0)
    )
    fx = compute_fx_risk(exposure, ACCOUNT, instrument_currencies={})

    assert fx["foreign_exposure_gbp"] == pytest.approx(0.0)
    assert fx["unmapped_instruments"] == ("MYSTERY",)


# --------------------------------------------------------------------------- #
# Regimes
# --------------------------------------------------------------------------- #
def regime_frame(**latest) -> pd.DataFrame:
    index = pd.to_datetime(["2026-01-01", "2026-01-02"], utc=True)
    return pd.DataFrame(
        {"timestamp": index, **{k: ["low", v] for k, v in latest.items()}}
    )


def test_the_latest_classification_is_taken():
    result = compute_regime_risk(
        regime_frame(volatility_regime="high", combined_regime="crisis")
    )
    assert result["regimes"]["volatility_regime"] == "high"
    assert result["regimes"]["combined_regime"] == "crisis"
    assert result["available"] is True


def test_elevated_regimes_are_flagged_not_blocked():
    """A crisis is also where the opportunity is."""
    result = compute_regime_risk(regime_frame(combined_regime="crisis"))
    assert "combined_regime" in result["elevated"]


def test_a_calm_regime_is_not_flagged():
    result = compute_regime_risk(regime_frame(combined_regime="risk_on"))
    assert result["elevated"] == ()


def test_no_regime_frame_is_absent_not_unknown():
    """"Not measured" and "measured, inconclusive" are different."""
    result = compute_regime_risk(None)
    assert result["available"] is False
    assert result["regimes"] == {}


# --------------------------------------------------------------------------- #
# Recent trades
# --------------------------------------------------------------------------- #
def assessments(*rows) -> pd.DataFrame:
    return pd.DataFrame(list(rows))


def test_verdicts_are_counted():
    result = compute_recent_trade_risk(
        assessments(
            {"status": "SAFE", "risk_gbp": 10.0, "critical_checks": []},
            {"status": "UNSAFE", "risk_gbp": 300.0, "critical_checks": ["risk"]},
            {"status": "UNSAFE", "risk_gbp": 200.0, "critical_checks": ["risk"]},
        )
    )
    assert result["counts"] == {"SAFE": 1, "UNSAFE": 2}
    assert result["average_risk_gbp"] == pytest.approx(170.0)


def test_unsafe_reasons_group_by_check_not_by_prose():
    """The sentences carry amounts, so grouping on them never groups."""
    result = compute_recent_trade_risk(
        assessments(
            {"status": "UNSAFE", "critical_checks": ["exposure"],
             "reasons": ["Exposure £4,000.00 is over your limit."]},
            {"status": "UNSAFE", "critical_checks": ["exposure"],
             "reasons": ["Exposure £3,000.00 is over your limit."]},
        )
    )
    assert result["unsafe_reasons"] == {"Exposure over the limit": 2}


def test_only_the_blocking_checks_count_as_reasons():
    """An info finding is not why a trade was refused."""
    result = compute_recent_trade_risk(
        assessments(
            {"status": "UNSAFE", "critical_checks": ["stop_loss"],
             "warning_checks": ["volatility"]},
        )
    )
    assert result["unsafe_reasons"] == {"No stop-loss set": 1}


def test_no_recorded_verdicts_is_absent_not_zero():
    """No record is not the same as no unsafe trades."""
    result = compute_recent_trade_risk(None)
    assert result["available"] is False
    assert result["counts"] == {}


# --------------------------------------------------------------------------- #
# Profit and loss
# --------------------------------------------------------------------------- #
def pnl_frame(totals, **legs) -> pd.DataFrame:
    index = pd.bdate_range("2026-01-01", periods=len(totals), tz="UTC")
    frame = pd.DataFrame({"timestamp": index, "total": totals})
    for name, values in legs.items():
        frame[name] = values
    return frame


def test_drawdown_uses_the_runs_own_starting_equity():
    """Not the operator's balance.

    A run simulated at 100,000 and measured against a 2,000 balance reads as
    a 75% drawdown when the run itself never lost 3%. The fraction has to be
    computed against the equity the run actually had.
    """
    frame = pnl_frame([1_000.0, -3_000.0, 500.0])

    against_run = compute_pnl_risk(frame, ACCOUNT, initial_equity=100_000.0)
    against_operator = compute_pnl_risk(frame, ACCOUNT)

    assert against_run["max_drawdown"] == pytest.approx(-3_000 / 101_000, rel=1e-6)
    assert against_run["initial_equity"] == pytest.approx(100_000.0)
    assert abs(against_operator["max_drawdown"]) > abs(against_run["max_drawdown"])


def test_the_overnight_split_is_reported():
    """The split is the finding: a stop cannot bound a gap."""
    frame = pnl_frame([10.0, 10.0], gap=[8.0, 8.0], intraday=[2.0, 2.0])
    result = compute_pnl_risk(frame, ACCOUNT, initial_equity=1_000.0)

    assert result["gap_pnl"] == pytest.approx(16.0)
    assert result["intraday_pnl"] == pytest.approx(4.0)
    assert result["overnight_share"] == pytest.approx(0.8)


def test_drawdown_is_never_positive():
    result = compute_pnl_risk(
        pnl_frame([5.0, -2.0, 3.0]), ACCOUNT, initial_equity=1_000.0
    )
    assert result["max_drawdown"] <= 0
    assert result["current_drawdown"] <= 0


def test_no_pnl_frame_is_absent():
    assert compute_pnl_risk(None, ACCOUNT)["available"] is False


def test_a_frame_without_a_total_column_is_absent():
    assert compute_pnl_risk(pd.DataFrame({"gap": [1.0]}), ACCOUNT)["available"] is False


# --------------------------------------------------------------------------- #
# Scheduler status
# --------------------------------------------------------------------------- #
NOW = pd.Timestamp("2026-08-25 12:00", tz="UTC")


def test_a_completed_run_is_ok():
    result = scheduler_status.compute_scheduler_status(
        {"last_run": "2026-08-25T09:00:00Z", "last_success": "2026-08-25T09:00:42Z"},
        now=NOW,
    )
    assert result["run_status"] == "OK"
    assert result["message"] == "Last run completed successfully."
    assert result["incomplete"] is False


def test_a_run_that_started_and_died_is_failed():
    """The case the whole ordering exists for."""
    result = scheduler_status.compute_scheduler_status(
        {"last_run": "2026-08-25T09:00:00Z", "last_success": None}, now=NOW
    )
    assert result["run_status"] == "FAILED"
    assert result["incomplete"] is True
    assert "did not complete" in result["message"]


def test_a_start_newer_than_the_last_success_is_failed():
    result = scheduler_status.compute_scheduler_status(
        {"last_run": "2026-08-25T09:00:00Z", "last_success": "2026-08-24T09:00:00Z"},
        now=NOW,
    )
    assert result["run_status"] == "FAILED"


def test_no_runs_at_all_is_unknown_not_ok():
    result = scheduler_status.compute_scheduler_status({}, now=NOW)
    assert result["run_status"] == "UNKNOWN"
    assert result["message"] == "No runs recorded yet."


def test_a_missing_state_file_is_unknown():
    assert scheduler_status.compute_scheduler_status(None, now=NOW)[
        "run_status"
    ] == "UNKNOWN"


def test_a_recorded_error_is_failed_even_with_a_success():
    result = scheduler_status.compute_scheduler_status(
        {
            "last_run": "2026-08-25T09:00:00Z",
            "last_success": "2026-08-25T09:00:42Z",
            "last_error": "something broke",
        },
        now=NOW,
    )
    assert result["run_status"] == "FAILED"
    assert "something broke" in result["message"]


def test_an_old_success_is_still_a_success_but_flagged_stale():
    """Succeeded three days ago is not the same as failed."""
    result = scheduler_status.compute_scheduler_status(
        {"last_run": "2026-08-20T09:00:00Z", "last_success": "2026-08-20T09:00:42Z"},
        now=NOW,
    )
    assert result["run_status"] == "OK"
    assert result["stale"] is True
    assert "hours ago" in result["message"]


# --------------------------------------------------------------------------- #
# Flags
# --------------------------------------------------------------------------- #
def test_exposure_over_the_limit_is_flagged():
    result = build_risk_snapshot(
        positions_frame=positions(SPY=10.0),
        analysis_frame=analysis(SPY=100.0),
        limits=RiskLimits(account_balance_gbp=2_000.0, max_total_exposure_gbp=500.0),
    )
    assert result.severity == "critical"
    assert any("Gross exposure" in flag for flag in result.flags)


def test_a_per_instrument_breach_names_the_instrument():
    result = build_risk_snapshot(
        positions_frame=positions(SPY=10.0, GLD=1.0),
        analysis_frame=analysis(SPY=100.0, GLD=100.0),
        limits=RiskLimits(
            account_balance_gbp=2_000.0,
            max_total_exposure_gbp=10_000.0,
            max_exposure_per_instrument_gbp=500.0,
        ),
    )
    assert any(flag.startswith("SPY") for flag in result.flags)
    assert not any(flag.startswith("GLD") for flag in result.flags)


def test_leverage_over_the_limit_is_flagged():
    result = build_risk_snapshot(
        positions_frame=positions(SPY=100.0),
        analysis_frame=analysis(SPY=100.0),
        limits=RiskLimits(
            account_balance_gbp=2_000.0,
            max_total_exposure_gbp=1_000_000.0,
            max_exposure_per_instrument_gbp=1_000_000.0,
            max_effective_leverage=2.0,
        ),
    )
    assert any("leverage" in flag.lower() for flag in result.flags)


def test_concentration_alone_can_breach():
    """Inside every absolute limit and still one position."""
    result = build_risk_snapshot(
        positions_frame=positions(SPY=10.0, GLD=1.0),
        analysis_frame=analysis(SPY=100.0, GLD=100.0),
        limits=RiskLimits(
            account_balance_gbp=100_000.0,
            max_total_exposure_gbp=1_000_000.0,
            max_exposure_per_instrument_gbp=1_000_000.0,
            max_instrument_concentration_pct=0.40,
        ),
    )
    assert any("concentration" in flag for flag in result.flags)


def test_an_unreadable_artefact_is_a_warning_not_silence():
    """"No limit breached" and "no limit checked" are different."""
    result = build_risk_snapshot(limits=ACCOUNT)

    assert result.severity == "warning"
    assert result.flags == ()
    assert result.unavailable


def test_positions_with_no_prices_cannot_be_checked():
    """Unvalued exposure must read as unchecked, never as within limits."""
    result = build_risk_snapshot(positions_frame=positions(SPY=10.0), limits=ACCOUNT)

    assert result.flags == ()
    assert "exposure" in result.unavailable
    assert result.severity == "warning"


def test_a_flat_book_inside_every_limit_is_ok():
    result = build_risk_snapshot(
        positions_frame=positions(SPY=0.0),
        regime_frame=regime_frame(combined_regime="risk_on"),
        pnl_frame=pnl_frame([1.0, 1.0]),
        limits=ACCOUNT,
        run_initial_equity=2_000.0,
    )
    assert result.severity == "ok"
    assert result.message == "The book is flat."


# --------------------------------------------------------------------------- #
# Reading from disk, and the runner
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
    summary = run_scheduled(
        list(TICKERS),
        RunConfig(backtest=BacktestConfig(interval="1d")),
        repository=DataRepository(config),
        now=pd.Timestamp("2026-08-25 07:00", tz="UTC"),
    )
    assert summary.status == "ok", summary.error
    return config, summary.run_id


def test_a_snapshot_reads_a_real_run(recorded_lake):
    config, run_id = recorded_lake
    result = snapshot_from_disk(config)

    assert result.run_id == run_id
    assert result.regime_metrics["available"] is True
    assert result.pnl_risk_metrics["available"] is True
    assert result.scheduler_status["run_status"] == "OK"


def test_the_snapshot_drawdown_matches_the_analytics_layer(recorded_lake):
    """Two modules computing the same thing must agree."""
    from cfdinvest.analytics import score_run

    config, run_id = recorded_lake
    snapshot_dd = snapshot_from_disk(config).pnl_risk_metrics["max_drawdown"]
    analytics_dd = score_run(config, run_id).metrics["drawdown"][
        "max_drawdown_fraction"
    ]

    assert snapshot_dd == pytest.approx(analytics_dd)


def test_a_snapshot_with_no_runs_still_answers(lake):
    """A risk page that renders nothing cannot say the pipeline has not run."""
    result = snapshot_from_disk(lake)

    assert result.run_id is None
    assert result.scheduler_status["run_status"] == "UNKNOWN"
    assert result.severity in ("warning", "ok")


def test_the_snapshot_does_not_write_anything(recorded_lake):
    """Read-only: computing risk must not change the lake."""
    config, _ = recorded_lake
    root = Path(config.paths.data_root)
    before = sorted(p.name for p in root.rglob("*") if p.is_file())

    snapshot_from_disk(config)

    assert sorted(p.name for p in root.rglob("*") if p.is_file()) == before


# --------------------------------------------------------------------------- #
# Snapshot persistence
# --------------------------------------------------------------------------- #
def test_a_snapshot_round_trips(lake):
    from cfdinvest.persist import risk as risk_store

    result = build_risk_snapshot(
        positions_frame=positions(SPY=10.0),
        limits=ACCOUNT,
        scheduler_status={"run_status": "OK"},
    )
    path = risk_store.write_risk_snapshot(
        result, lake, now=pd.Timestamp("2026-08-25 12:00", tz="UTC")
    )

    assert path is not None
    assert Path(path).name == "20260825-120000-risk.json"

    read = risk_store.load_latest_risk_snapshot(lake)
    assert read["scheduler_status"]["run_status"] == "OK"
    assert read["schema_version"] == risk_store.SCHEMA_VERSION


def test_snapshots_accumulate_rather_than_overwrite(lake):
    """A snapshot is a reading, and that moment does not come back."""
    from cfdinvest.persist import risk as risk_store

    for hour in (9, 10, 11):
        risk_store.write_risk_snapshot(
            build_risk_snapshot(limits=ACCOUNT),
            lake,
            now=pd.Timestamp(f"2026-08-25 {hour}:00", tz="UTC"),
        )
    assert len(risk_store.list_risk_snapshots(lake)) == 3


def test_the_history_is_a_table_not_the_documents(lake):
    from cfdinvest.persist import risk as risk_store

    for hour in (9, 10):
        risk_store.write_risk_snapshot(
            build_risk_snapshot(
                positions_frame=positions(SPY=10.0), limits=ACCOUNT
            ),
            lake,
            now=pd.Timestamp(f"2026-08-25 {hour}:00", tz="UTC"),
        )
    history = risk_store.load_risk_history(lake)

    assert len(history) == 2
    assert "gross_exposure_gbp" in history.columns
    assert history["timestamp"].is_monotonic_increasing


def test_no_snapshots_is_an_empty_table(lake):
    from cfdinvest.persist import risk as risk_store

    assert risk_store.load_latest_risk_snapshot(lake) is None
    assert risk_store.load_risk_history(lake).empty


def test_a_corrupt_snapshot_is_skipped(lake):
    from cfdinvest.persist import risk as risk_store

    risk_store.write_risk_snapshot(
        build_risk_snapshot(limits=ACCOUNT), lake,
        now=pd.Timestamp("2026-08-25 09:00", tz="UTC"),
    )
    bad = risk_store.risk_root(lake) / "20260825-100000-risk.json"
    bad.write_text("{not json", encoding="utf-8")

    assert risk_store.load_latest_risk_snapshot(lake) is not None
    assert len(risk_store.load_risk_history(lake)) == 1


# --------------------------------------------------------------------------- #
# The assessment log
# --------------------------------------------------------------------------- #
def test_an_assessment_is_recorded_and_read_back(lake):
    from cfdinvest.livehelper import LiveHelperConfig, assess
    from cfdinvest.persist import assessments

    intent, assessment, sizing = assess(
        "long UKX 0.5 entry=100 stop=96 tp=112 lev=20", LiveHelperConfig()
    )
    assessments.record_assessment(intent, assessment, sizing, lake)

    log = assessments.load_assessments(lake)
    assert len(log) == 1
    assert log["instrument"].iloc[0] == "UKX"
    assert log["status"].iloc[0] in ("SAFE", "BORDERLINE", "UNSAFE")


def test_assessments_append_rather_than_replace(lake):
    from cfdinvest.livehelper import LiveHelperConfig, assess
    from cfdinvest.persist import assessments

    for quantity in (0.5, 1.0, 2.0):
        intent, assessment, sizing = assess(
            f"long UKX {quantity} entry=100 stop=96 lev=20", LiveHelperConfig()
        )
        assessments.record_assessment(intent, assessment, sizing, lake)

    assert len(assessments.load_assessments(lake)) == 3


def test_a_truncated_line_does_not_lose_the_log(lake):
    """An append log's last line is what a crash takes."""
    from cfdinvest.livehelper import LiveHelperConfig, assess
    from cfdinvest.persist import assessments

    intent, assessment, sizing = assess(
        "long UKX 0.5 entry=100 stop=96 lev=20", LiveHelperConfig()
    )
    assessments.record_assessment(intent, assessment, sizing, lake)

    with assessments.assessments_path(lake).open("a", encoding="utf-8") as handle:
        handle.write('{"truncated": ')

    assert len(assessments.load_assessments(lake)) == 1


def test_no_log_is_an_empty_frame(lake):
    from cfdinvest.persist import assessments

    assert assessments.load_assessments(lake).empty


# --------------------------------------------------------------------------- #
# The periodic snapshot task
# --------------------------------------------------------------------------- #
def test_a_snapshot_is_taken_and_recorded(recorded_lake):
    from cfdinvest.risk import take_risk_snapshot

    config, run_id = recorded_lake
    observation = take_risk_snapshot(config, now=NOW)

    assert observation.snapshot.run_id == run_id
    assert observation.at == NOW
    assert Path(observation.stored_path).exists()


def test_reporting_without_recording_writes_nothing(lake):
    from cfdinvest.persist import risk as risk_store
    from cfdinvest.risk import take_risk_snapshot

    observation = take_risk_snapshot(lake, store=False, now=NOW)

    assert observation.stored_path is None
    assert risk_store.list_risk_snapshots(lake) == []


def test_the_task_never_drives_the_pipeline(recorded_lake):
    """The property the whole module exists to keep.

    A monitor that could start a run would make the dashboard a trading
    system by accident: the artefacts it reads would be ones it caused.
    """
    from cfdinvest.persist import readers
    from cfdinvest.risk import RiskMonitor, RiskMonitorConfig

    config, _ = recorded_lake
    before = readers.list_runs(config)

    monitor = RiskMonitor(
        config,
        RiskMonitorConfig(interval_minutes=1.0, max_observations=3),
        sleeper=lambda seconds: None,
    )
    monitor.run_forever()

    assert readers.list_runs(config) == before


#: Importing any of these would give some module here a way to act rather
#: than observe -- to run the pipeline, refresh the lake, or reach a broker.
FORBIDDEN_IMPORTS = (
    "cfdinvest.backtest",
    "cfdinvest.repository",
    "cfdinvest.schedule.runner",
    "cfdinvest.sources",
    "cfdinvest.vendors",
    "requests",
    "urllib",
    "httpx",
    "socket",
    "selenium",
    "playwright",
    "subprocess",
    "webbrowser",
)


def imported_modules(source: str) -> set[str]:
    import ast

    tree = ast.parse(source)
    modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    modules |= {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    return modules


@pytest.mark.parametrize(
    "module", sorted(Path("cfdinvest/risk").glob("*.py")), ids=lambda p: p.name
)
def test_no_module_here_can_trade_or_drive_the_pipeline(module):
    """Structural, not behavioural: there is no path to the engine at all.

    A risk dashboard that could start a run would be reading artefacts it
    caused, and one that could reach a broker would not be a dashboard. Both
    are prevented by there being nothing to call, and this is what checks it
    stays that way.
    """
    found = imported_modules(module.read_text(encoding="utf-8"))
    offending = sorted(
        name
        for name in found
        if any(name == bad or name.startswith(bad + ".") for bad in FORBIDDEN_IMPORTS)
    )
    assert not offending, f"{module.name} imports {offending}"


@pytest.mark.parametrize(
    "module", sorted(Path("cfdinvest/risk").glob("*.py")), ids=lambda p: p.name
)
def test_no_module_here_mentions_a_broker(module):
    source = module.read_text(encoding="utf-8").lower()
    for needle in ("https://", "trading212.com", "api_key", "api key"):
        assert needle not in source, f"{module.name} mentions {needle!r}"


def test_the_loop_waits_the_configured_interval(lake):
    from cfdinvest.risk import RiskMonitor, RiskMonitorConfig

    waits: list[float] = []
    monitor = RiskMonitor(
        lake,
        RiskMonitorConfig(interval_minutes=5.0, max_observations=3, store=False),
        sleeper=waits.append,
    )
    observations = monitor.run_forever()

    assert len(observations) == 3
    # Two gaps between three observations, and no trailing sleep before exit.
    assert waits == [300.0, 300.0]


def test_a_failed_read_does_not_end_the_watch(lake, monkeypatch):
    """The breach at 04:00 is missed if 03:00's failure stops the loop."""
    from cfdinvest.risk import RiskMonitor, RiskMonitorConfig, monitor as monitor_module

    calls = {"n": 0}
    real = monitor_module.take_risk_snapshot

    def sometimes(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("the disk went away")
        return real(*args, **kwargs)

    monkeypatch.setattr(monitor_module, "take_risk_snapshot", sometimes)

    watcher = RiskMonitor(
        lake,
        RiskMonitorConfig(interval_minutes=1.0, max_observations=3, store=False),
        sleeper=lambda seconds: None,
    )
    observations = watcher.run_forever()

    assert calls["n"] == 3
    assert len(observations) == 2


def test_a_failing_alert_does_not_end_the_watch(lake):
    """One bad mail server must not stop the monitoring."""
    from cfdinvest.risk import RiskMonitor, RiskMonitorConfig

    def explode(observation):
        raise RuntimeError("smtp is down")

    watcher = RiskMonitor(
        lake,
        RiskMonitorConfig(interval_minutes=1.0, max_observations=2, store=False),
        on_observation=explode,
        sleeper=lambda seconds: None,
    )
    assert len(watcher.run_forever()) == 2


def test_stop_on_critical_ends_the_loop():
    """Off by default, but available to an operator driving this by hand."""
    from cfdinvest.risk import RiskMonitor, RiskMonitorConfig
    from cfdinvest.risk.monitor import RiskObservation

    breached = build_risk_snapshot(
        positions_frame=positions(SPY=10.0),
        analysis_frame=analysis(SPY=100.0),
        limits=RiskLimits(account_balance_gbp=2_000.0, max_total_exposure_gbp=1.0),
    )
    assert breached.severity == "critical"

    def watcher(stop: bool) -> RiskMonitor:
        monitor = RiskMonitor(
            None,
            RiskMonitorConfig(
                interval_minutes=1.0, max_observations=5, store=False,
                stop_on_critical=stop,
            ),
            sleeper=lambda seconds: None,
        )

        def observe(now=None):
            observation = RiskObservation(at=NOW, snapshot=breached)
            monitor.history.append(observation)
            return observation

        monitor.observe = observe
        return monitor

    assert len(watcher(stop=True).run_forever()) == 1
    assert len(watcher(stop=False).run_forever()) == 5


def test_a_zero_interval_is_refused():
    """A zero interval is a busy loop, not a monitor."""
    from cfdinvest.risk import RiskMonitorConfig

    with pytest.raises(ValueError, match="busy loop"):
        RiskMonitorConfig(interval_minutes=0)


# --------------------------------------------------------------------------- #
# Which book the exposure describes
#
# M214 the backtest's positions read as the book
# M215 no book at all reported as a flat one
# M216 the paper book preferred over what was actually confirmed
# M217 a closed confirmed book falling through to the simulation
# M218 the arms summed into one exposure
# M219 a short carried as a long
# M220 a dollar mark labelled sterling
# M221 the account balance taken from the run's initial equity
# --------------------------------------------------------------------------- #
PAPER_FX = 0.75


def _paper_event(ticker, *, cohort, direction="long", quantity=10.0, price=100.0,
                 event="open", at="2026-08-26T16:00:00+00:00"):
    return {
        "at": at, "cohort": cohort, "event": event, "ticker": ticker,
        "direction": direction, "quantity": quantity, "price": price,
        "fx_rate": PAPER_FX, "notional_gbp": quantity * price * PAPER_FX,
    }


def _write_paper_book(config, events):
    import json

    from cfdinvest.persist.storage import account_root

    path = Path(account_root(config, None)) / "paper" / "book.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8"
    )


def _baseline_arm(config):
    from cfdinvest.advisor.papertrade_run import cohorts_for

    return cohorts_for(getattr(config, "strategy", None))[0][0]


def test_the_book_is_the_paper_book_not_the_backtests_positions(recorded_lake):
    """M214: the defect this replaced.

    The snapshot read ``positions.parquet`` from the newest run -- a
    simulation whose equity had compounded for two years -- and measured it
    against a 2,000 pound account's limits. It reported 810,945 of gross
    exposure against a 3,000 limit and went critical every single day, on
    positions nobody held. A limit breached every day is a limit nobody reads.
    """
    config, _ = recorded_lake
    arm = _baseline_arm(config)
    _write_paper_book(config, [_paper_event("SPY", cohort=arm, quantity=4.0)])

    result = snapshot_from_disk(config)
    stored = pd.read_parquet(
        Path(config.paths.data_root) / "runs" / result.run_id / "positions.parquet"
    )

    assert result.book == f"paper:{arm}"
    assert set(result.exposure_metrics["units"]) == {"SPY"}
    # The run really does hold other instruments -- so this is not passing
    # because there was nothing else to read.
    assert set(stored.columns) - {"SPY"}


def test_no_book_at_all_is_absent_rather_than_flat(recorded_lake):
    """M215: silence read as "you are carrying nothing".

    No confirmation and no paper book means nobody knows what is held. That
    is a warning, not the calm of a flat account.
    """
    config, _ = recorded_lake

    result = snapshot_from_disk(config)

    assert result.book is None
    assert "positions" in result.unavailable
    assert result.severity == "warning"


def test_a_confirmed_position_wins_over_the_simulation(recorded_lake):
    """M216: the real account losing to the paper one.

    A confirmation is what you recorded actually placing. Once one exists it
    is the book, whatever the simulation believes.
    """
    from cfdinvest.persist.confirm import Confirmation, write_confirmation

    config, _ = recorded_lake
    arm = _baseline_arm(config)
    _write_paper_book(config, [_paper_event("SPY", cohort=arm, quantity=4.0)])
    write_confirmation(
        Confirmation(
            ticker="TLT", action="open", quantity=3.0, price=100.0,
            timestamp=pd.Timestamp("2026-08-26 17:00", tz="UTC"), fx_rate=PAPER_FX,
        ),
        config,
    )

    result = snapshot_from_disk(config)

    assert result.book == "confirmed"
    assert set(result.exposure_metrics["units"]) == {"TLT"}


@pytest.mark.parametrize(
    "recorded, holding",
    [
        # Opened and then closed: the replay leaves a zero-quantity entry.
        ((("open", 3.0, 17), ("close", 0.0, 18)), "sold"),
        # Only decisions not to trade: the replay leaves *nothing at all*,
        # which is the case that separates "no confirmations exist" from
        # "the confirmations say you hold nothing". Getting it wrong reads
        # the paper book instead and reports positions you never took.
        ((("ignore", 0.0, 17),), "declined"),
    ],
    ids=["closed", "ignored"],
)
def test_a_confirmed_book_holding_nothing_is_still_the_book(
    recorded_lake, recorded, holding
):
    """M217: a real account that went flat falling back to the simulation.

    Having closed everything -- or having decided not to trade at all -- is a
    fact about the real book, not an absence of one. Reporting the paper
    book's positions instead would say you are carrying something you never
    bought.
    """
    from cfdinvest.persist.confirm import Confirmation, write_confirmation

    config, _ = recorded_lake
    arm = _baseline_arm(config)
    _write_paper_book(config, [_paper_event("SPY", cohort=arm, quantity=4.0)])
    for action, quantity, hour in recorded:
        write_confirmation(
            Confirmation(
                ticker="TLT", action=action, quantity=quantity,
                price=None if action == "ignore" else 100.0,
                timestamp=pd.Timestamp(f"2026-08-26 {hour}:00", tz="UTC"),
                fx_rate=PAPER_FX,
            ),
            config,
        )

    result = snapshot_from_disk(config)

    assert result.book == "confirmed", holding
    assert "SPY" not in (result.exposure_metrics["units"] or {})


def test_only_the_baseline_arm_counts(recorded_lake):
    """M218: four independent experiments added together.

    The arms are the same signals at four thresholds. Summing them reports
    the exposure of a book that trades every threshold at once, which is
    nobody's book and four times too big.
    """
    config, _ = recorded_lake
    arm = _baseline_arm(config)
    _write_paper_book(config, [
        _paper_event("SPY", cohort=arm, quantity=4.0),
        _paper_event("TLT", cohort="c70", quantity=9.0),
    ])

    result = snapshot_from_disk(config)

    assert set(result.exposure_metrics["units"]) == {"SPY"}


def test_a_short_is_carried_short(recorded_lake):
    """M219: the paper book records size unsigned and direction separately.

    Reading the quantity without the direction makes every short a long, so
    net exposure comes out at gross and a hedged book looks like a doubled
    one.
    """
    config, _ = recorded_lake
    arm = _baseline_arm(config)
    _write_paper_book(config, [
        _paper_event("SPY", cohort=arm, direction="short", quantity=4.0),
    ])

    result = snapshot_from_disk(config)
    metrics = result.exposure_metrics

    assert metrics["units"]["SPY"] < 0
    assert metrics["net_exposure_gbp"] < 0
    assert metrics["gross_exposure_gbp"] == pytest.approx(
        abs(metrics["net_exposure_gbp"])
    )


def test_the_mark_is_converted_to_sterling(recorded_lake):
    """M220: units times a dollar close, labelled GBP.

    The old figure was never converted -- which on a US universe overstated
    every exposure by the exchange rate, in the direction that breaches
    limits. Each book records the rate its position was struck at; a stale
    rate is a small error, no rate is a wrong number that looks right.
    """
    config, _ = recorded_lake
    arm = _baseline_arm(config)
    _write_paper_book(config, [_paper_event("SPY", cohort=arm, quantity=4.0)])

    result = snapshot_from_disk(config)
    analysis_frame = pd.read_parquet(
        Path(config.paths.data_root) / "runs" / result.run_id / "analysis.parquet"
    )
    spy = analysis_frame[
        (analysis_frame["panel"].astype(str) == "prices.close")
        & (analysis_frame["series"].astype(str) == "SPY")
    ]
    close = float(spy.iloc[-1]["value"])

    assert result.exposure_metrics["gross_exposure_gbp"] == pytest.approx(
        4.0 * close * PAPER_FX
    )


def test_the_balance_is_the_accounts_not_the_runs(recorded_lake):
    """M221: leverage measured against the simulation's starting equity.

    The balance is the denominator of leverage and margin usage. Taking it
    from the run made a real book's leverage a statement about a backtest --
    the same mismatch as the positions, moved into the denominator.
    """
    config, _ = recorded_lake
    arm = _baseline_arm(config)
    _write_paper_book(config, [_paper_event("SPY", cohort=arm, quantity=4.0)])

    result = snapshot_from_disk(config)
    gross = result.exposure_metrics["gross_exposure_gbp"]
    leverage = result.leverage_metrics["effective_leverage"]

    assert leverage == pytest.approx(gross / RiskLimits().account_balance_gbp)
