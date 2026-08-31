"""Insights: what the record says once sliced, and what it cannot say.

The layer's danger is arithmetic that looks like knowledge. Slicing two
hundred trades across four regimes and eight instruments leaves buckets of
six, and six trades say nothing — so almost every test here is about a claim
being *withheld* rather than made.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from cfdinvest.config import AppConfig, PathConfig
from cfdinvest.insights import (
    CONFIDENT_SAMPLE,
    MINIMUM_SAMPLE,
    build_report,
    execution_quality_trend,
    expectancy_trend,
    fx_sensitivity,
    instrument_performance,
    margin_behaviour_trend,
    regime_performance,
    run_insights,
    spread_sensitivity,
    volatility_sensitivity,
    win_loss_distribution,
)
from cfdinvest.persist import insights as store

NOW = pd.Timestamp("2026-08-25 12:00", tz="UTC")


@pytest.fixture
def lake(tmp_path) -> AppConfig:
    return AppConfig(paths=PathConfig(data_root=tmp_path / "data"))


def trips(n=120, *, seed=7, assets=("SPY", "GLD", "TLT"), **columns):
    """A round-trip frame. ``net_pnl`` present is what marks it as trips."""
    rng = np.random.default_rng(seed)
    frame = pd.DataFrame(
        {
            "net_pnl": rng.normal(12, 90, n),
            "asset": rng.choice(list(assets), n),
            "spread_cost": rng.gamma(2, 3, n),
            "fx_rate": rng.normal(1.0, 0.05, n),
            "entry_time": pd.date_range(
                "2025-01-01", periods=n, freq="2D", tz="UTC"
            ),
        }
    )
    for name, values in columns.items():
        frame[name] = values
    return frame


# --------------------------------------------------------------------------- #
# The sample gate
# --------------------------------------------------------------------------- #
def test_a_thin_record_makes_no_claim():
    """The single most important behaviour here."""
    result = win_loss_distribution(trips(6))
    assert result.confidence == "insufficient"
    assert result.finding == ""


def test_a_withheld_claim_still_says_why():
    """"We did not look" and "we looked and found nothing" are different, and
    only one of them means keep trading."""
    result = win_loss_distribution(trips(6))
    assert result.caveats
    assert str(MINIMUM_SAMPLE) in result.caveats[0]


def test_a_middling_record_is_provisional_not_confident():
    result = win_loss_distribution(trips(MINIMUM_SAMPLE + 2))
    assert result.confidence == "provisional"
    assert result.finding


def test_a_long_record_is_confident():
    result = win_loss_distribution(trips(CONFIDENT_SAMPLE + 20))
    assert result.confidence == "confident"


def test_an_insufficient_insight_is_not_usable():
    assert not win_loss_distribution(trips(4)).usable
    assert win_loss_distribution(trips(120)).usable


def test_the_numbers_survive_even_when_the_claim_does_not():
    """What is withheld is the assertion, not the arithmetic."""
    result = instrument_performance(trips(12))
    assert result.finding == ""
    assert result.buckets
    assert sum(bucket.trades for bucket in result.buckets) == 12


# --------------------------------------------------------------------------- #
# Slicing makes the sample smaller
# --------------------------------------------------------------------------- #
def test_thin_buckets_are_left_out_and_named():
    """Two hundred trades across eight instruments is buckets of twenty-five."""
    frame = trips(60, assets=("SPY", "GLD", "TLT", "NAS100", "UKX", "OIL"))
    result = instrument_performance(frame)

    for bucket in result.buckets:
        if bucket.label in result.finding:
            assert bucket.enough


def test_one_bucket_alone_cannot_be_compared():
    result = instrument_performance(trips(60, assets=("SPY",)))
    assert result.confidence == "insufficient"
    assert "two" in result.caveats[0].lower()


def test_naming_a_best_always_names_the_others():
    """With several buckets one is always the best; printing only the winner
    turns an ordering into a discovery."""
    result = instrument_performance(trips(150))
    assert result.finding
    assert "Worst" in result.finding
    assert any("by construction" in caveat for caveat in result.caveats)


def test_the_multiple_comparisons_caveat_is_always_attached():
    for result in (
        instrument_performance(trips(150)),
        spread_sensitivity(trips(150)),
    ):
        assert any(
            "question rather than a result" in caveat for caveat in result.caveats
        )


# --------------------------------------------------------------------------- #
# Expectancy over time
# --------------------------------------------------------------------------- #
def test_expectancy_needs_both_sides_of_the_comparison():
    result = expectancy_trend(trips(MINIMUM_SAMPLE + 2))
    assert result.confidence == "insufficient"
    assert "each side" in result.caveats[0]


def test_expectancy_compares_recent_against_earlier():
    result = expectancy_trend(trips(120), window=30)
    assert result.finding
    assert len(result.buckets) == 2
    assert result.buckets[1].trades == 30


def test_a_small_move_is_called_a_normal_run():
    """A change that happens often by chance should not change how you trade."""
    steady = pd.DataFrame(
        {
            "net_pnl": [10.0] * 60 + [10.5] * 30,
            "entry_time": pd.date_range("2025-01-01", periods=90, tz="UTC"),
        }
    )
    result = expectancy_trend(steady)
    assert "normal run" in result.finding
    assert result.caveats


# --------------------------------------------------------------------------- #
# Win and loss distribution
# --------------------------------------------------------------------------- #
def test_a_win_rate_is_never_reported_alone():
    """30% winners at 4:1 is good; 70% at 1:4 is not. The pair is the finding."""
    result = win_loss_distribution(trips(120))
    assert "%" in result.finding
    assert "Payoff ratio" in result.finding


def test_a_low_win_rate_with_a_big_payoff_is_explained():
    frame = pd.DataFrame({"net_pnl": [-10.0] * 70 + [60.0] * 30})
    result = win_loss_distribution(frame)
    assert "normal shape of a trend strategy" in result.finding


def test_winning_often_and_small_is_flagged():
    frame = pd.DataFrame({"net_pnl": [5.0] * 70 + [-40.0] * 30})
    result = win_loss_distribution(frame)
    assert "looks best and survives worst" in result.finding


def test_one_dominating_loss_is_called_out():
    """The average is describing that trade more than the strategy."""
    frame = pd.DataFrame({"net_pnl": [5.0] * 40 + [-2.0] * 10 + [-900.0]})
    result = win_loss_distribution(frame)
    assert any("more than the strategy" in caveat for caveat in result.caveats)


# --------------------------------------------------------------------------- #
# By regime
# --------------------------------------------------------------------------- #
def regime_history(n=200) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-12-01", periods=n, tz="UTC"),
            "combined_regime": ["risk_on"] * (n // 2) + ["crisis"] * (n - n // 2),
        }
    )


def test_trades_are_judged_by_the_regime_they_were_taken_in():
    """Today's classification says nothing about a trade from March."""
    result = regime_performance(trips(150), regime_history())
    labels = {bucket.label for bucket in result.buckets}
    assert labels <= {"risk_on", "crisis"}
    assert len(labels) >= 1


def test_no_regime_history_means_no_regime_finding():
    result = regime_performance(trips(150), None)
    assert result.confidence == "insufficient"
    assert "says nothing about a trade from March" in result.caveats[0]


# --------------------------------------------------------------------------- #
# Sensitivities
# --------------------------------------------------------------------------- #
def test_a_sensitivity_needs_enough_for_three_buckets():
    result = spread_sensitivity(trips(20))
    assert result.confidence == "insufficient"
    assert str(MINIMUM_SAMPLE * 3) in result.caveats[0]


def test_a_constant_cost_has_nothing_to_compare():
    frame = trips(120)
    frame["spread_cost"] = 2.0
    result = spread_sensitivity(frame)
    assert result.confidence == "insufficient"
    assert "barely varies" in result.caveats[0]


def test_the_spread_confound_is_stated():
    """A wide-spread third is also a more-traded third."""
    result = spread_sensitivity(trips(150))
    assert any("hard to separate" in caveat for caveat in result.caveats)


def test_an_unrecorded_measure_says_so_rather_than_passing():
    frame = trips(120).drop(columns=["fx_rate"])
    result = fx_sensitivity(frame)
    assert result.confidence == "insufficient"
    assert "records no FX detail" in result.caveats[0]


def test_volatility_needs_a_volatility_column():
    result = volatility_sensitivity(trips(120))
    assert result.confidence == "insufficient"
    assert "no volatility measure" in result.caveats[0]


def test_volatility_works_when_the_column_is_there():
    rng = np.random.default_rng(3)
    frame = trips(150, volatility=rng.gamma(2, 0.05, 150))
    result = volatility_sensitivity(frame)
    assert result.finding


# --------------------------------------------------------------------------- #
# Execution and margin
# --------------------------------------------------------------------------- #
def test_nothing_confirmed_is_not_fills_matching_the_plan():
    result = execution_quality_trend(None)
    assert result.confidence == "insufficient"
    assert "not the same as fills matching the plan" in result.caveats[0]


def test_execution_quality_reads_the_confirmation_record():
    frame = pd.DataFrame(
        {
            "slippage": [0.1] * 10 + [0.9] * 10,
            "quantity_deviation": [1.0] * 20,
        }
    )
    result = execution_quality_trend(frame)
    assert "moved worse" in result.finding


def test_execution_quality_states_its_own_selection_bias():
    """It measures the fills you chose to confirm as much as the fills you got."""
    frame = pd.DataFrame({"slippage": [0.1] * 20})
    result = execution_quality_trend(frame)
    assert any("chose to confirm" in caveat for caveat in result.caveats)


def test_margin_behaviour_needs_three_snapshots():
    frame = pd.DataFrame({"margin_usage_pct": [0.2, 0.3]})
    result = margin_behaviour_trend(frame)
    assert result.confidence == "insufficient"
    assert "line through anything" in result.caveats[0]


def test_margin_behaviour_reports_the_peak_and_its_own_blind_spot():
    """Snapshots are taken on a schedule, not at the moment margin peaked."""
    frame = pd.DataFrame({"margin_usage_pct": [0.20, 0.35, 0.48, 0.30]})
    result = margin_behaviour_trend(frame)
    assert "peaking at 48%" in result.finding
    assert any("probably higher" in caveat for caveat in result.caveats)


def test_no_risk_snapshots_says_so():
    result = margin_behaviour_trend(None)
    assert result.confidence == "insufficient"
    assert "no history to read" in result.caveats[0]


# --------------------------------------------------------------------------- #
# The report
# --------------------------------------------------------------------------- #
def test_the_report_asks_every_question():
    report = build_report(trips(150), now=NOW)
    assert len(report.insights) == 9


def test_the_headline_counts_what_can_be_said_not_what_was_asked():
    """"9 insights" over a record too thin to support one would be the layer
    flattering itself."""
    thin = build_report(trips(5), now=NOW)
    assert "too short to slice" in thin.headline
    assert not thin.usable

    thick = build_report(trips(150), now=NOW)
    assert "of 9 insight(s)" in thick.headline
    assert thick.usable


def test_an_empty_record_still_produces_a_report():
    """"The record is too short" is the most useful thing this can tell
    someone new."""
    report = build_report(pd.DataFrame(), now=NOW)
    assert len(report.insights) == 9
    assert all(i.confidence == "insufficient" for i in report.insights)


def test_one_failing_insight_does_not_silence_the_others(monkeypatch):
    from cfdinvest.insights import model as model_module

    monkeypatch.setattr(
        model_module, "win_loss_distribution",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    report = build_report(trips(150), now=NOW)

    assert len(report.insights) == 9
    broken = report.get("win_loss")
    assert broken.confidence == "insufficient"
    assert "bug here, not a finding about your trading" in broken.caveats[0]


def test_a_crashed_insight_does_not_vanish():
    """One that disappeared would read as one with nothing to say."""
    report = build_report(trips(150), now=NOW)
    assert {i.key for i in report.insights} == {
        "expectancy_trend", "win_loss", "regime_performance",
        "instrument_performance", "volatility_sensitivity",
        "spread_sensitivity", "fx_sensitivity", "execution_quality",
        "margin_behaviour",
    }


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
def test_a_report_round_trips(lake):
    path = store.write_insight_report(build_report(trips(150), now=NOW), lake)

    assert Path(path).name == "20260825-120000-insights.json"
    document = store.load_latest_insight_report(lake)
    assert len(document["insights"]) == 9
    assert document["schema_version"] == store.SCHEMA_VERSION


def test_reports_accumulate(lake):
    for hour in (9, 10, 11):
        store.write_insight_report(
            build_report(trips(150), now=pd.Timestamp(f"2026-08-25 {hour}:00", tz="UTC")),
            lake,
        )
    assert len(store.list_insight_reports(lake)) == 3


def test_two_reports_in_the_same_second_do_not_overwrite(lake):
    store.write_insight_report(build_report(trips(150), now=NOW), lake)
    store.write_insight_report(build_report(trips(150), now=NOW), lake)
    assert len(store.list_insight_reports(lake)) == 2


def test_the_history_shows_how_one_finding_moved(lake):
    """A finding that looked strong in June and has reversed is invisible in
    the current report and obvious in the sequence."""
    for hour, count in ((9, 150), (10, 40)):
        store.write_insight_report(
            build_report(
                trips(count), now=pd.Timestamp(f"2026-08-25 {hour}:00", tz="UTC")
            ),
            lake,
        )
    history = store.load_insight_history(lake, key="win_loss")

    assert len(history) == 2
    assert history["timestamp"].is_monotonic_increasing
    assert list(history["trades"]) == [150, 40]


def test_a_failed_write_returns_none(lake, monkeypatch):
    monkeypatch.setattr(
        store, "atomic_write_text",
        lambda *a, **k: (_ for _ in ()).throw(OSError("no space")),
    )
    assert store.write_insight_report(build_report(trips(20), now=NOW), lake) is None


def test_a_corrupt_report_is_skipped(lake):
    store.write_insight_report(build_report(trips(150), now=NOW), lake)
    (store.insights_root(lake) / "20260825-130000-insights.json").write_text(
        "{not json", encoding="utf-8"
    )
    assert store.load_latest_insight_report(lake) is not None


def test_no_reports_is_an_empty_history(lake):
    assert store.load_latest_insight_report(lake) is None
    assert store.load_insight_history(lake).empty


def test_running_over_an_empty_lake_still_answers(lake):
    report = run_insights(lake, store=False, now=NOW)
    assert len(report.insights) == 9
    assert "too short to slice" in report.headline


# --------------------------------------------------------------------------- #
# The digest
# --------------------------------------------------------------------------- #
def test_the_digest_separates_what_is_supported_from_what_is_not():
    """Burying the sample warnings turns two findings and seven absences into
    what looks like nine findings."""
    from cfdinvest.insights.digest import build_insight_digest

    body = build_insight_digest(build_report(trips(150), now=NOW)).get_content()
    assert "WHAT THE RECORD SUPPORTS" in body
    assert "NOT ENOUGH RECORD YET" in body
    assert body.index("WHAT THE RECORD SUPPORTS") < body.index("NOT ENOUGH RECORD YET")


def test_the_digest_subject_counts_the_supported_ones():
    from cfdinvest.insights.digest import build_insight_digest

    message = build_insight_digest(build_report(trips(150), now=NOW))
    assert "of 9 supported" in message["Subject"]


def test_the_digest_carries_the_multiple_comparisons_warning():
    from cfdinvest.insights.digest import build_insight_digest

    body = build_insight_digest(build_report(trips(150), now=NOW)).get_content()
    assert "by chance" in body
    # The notice now says what the system *can* do -- deal on a demo
    # account -- rather than claiming it cannot trade, which stopped
    # being true on 2026-08-27. Asserted by meaning, not wording.
    assert "demo account" in body and "live account" in body


# --------------------------------------------------------------------------- #
# It does not recompute what analytics owns
# --------------------------------------------------------------------------- #
def test_expectancy_comes_from_the_analytics_layer():
    """A second expectancy formula here would be a second answer."""
    source = Path("cfdinvest/insights/model.py").read_text(encoding="utf-8")
    assert "compute_expectancy" in source
    assert "as_round_trips" in source


def test_the_insight_expectancy_matches_the_analytics_one():
    """The property that matters, not just the import."""
    from cfdinvest.analytics.metrics import compute_expectancy

    frame = trips(150)
    theirs = compute_expectancy(frame)["expectancy"]
    ours = win_loss_distribution(frame).details["expectancy"]

    assert ours == pytest.approx(theirs)


@pytest.mark.parametrize(
    "module", sorted(Path("cfdinvest/insights").glob("*.py")), ids=lambda p: p.name
)
def test_insights_reach_nothing_that_could_trade(module):
    tree = ast.parse(module.read_text(encoding="utf-8"))
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
    forbidden = ("requests", "urllib", "httpx", "cfdinvest.backtest",
                 "cfdinvest.schedule.runner", "cfdinvest.livehelper.safety")
    assert not [
        name for name in imported
        if any(name == bad or name.startswith(bad + ".") for bad in forbidden)
    ]
