"""Behavioural patterns: described from the record, never diagnosed.

This is the layer most able to do harm by being confident. It makes claims
about how a person trades from a few dozen rows, those claims are the kind
people internalise, and the severities it produces **shrink real positions**
through the refinement layer.

So most of what follows is about restraint: patterns named rather than people,
innocent readings always present, nothing asserted on a thin record, and an
unmeasured pattern staying absent rather than becoming a reassuring zero.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from cfdinvest.bias import (
    ASSERT_THRESHOLD,
    BIASES,
    CONFIDENT_OBSERVATIONS,
    MINIMUM_OBSERVATIONS,
    build_bias_report,
    detect_fear_driven_underexposure,
    detect_late_exits,
    detect_leverage_creep,
    detect_loss_aversion,
    detect_margin_blindness,
    detect_overconfidence,
    detect_premature_exits,
    detect_revenge_trading,
    detect_risk_avoidance,
    run_bias_detection,
)
from cfdinvest.config import AppConfig, PathConfig
from cfdinvest.persist import bias as store

NOW = pd.Timestamp("2026-08-25 12:00", tz="UTC")


@pytest.fixture
def lake(tmp_path) -> AppConfig:
    return AppConfig(paths=PathConfig(data_root=tmp_path / "data"))


def confirmations(n=60, *, sizes=None, outcomes=None, **columns) -> pd.DataFrame:
    """A confirmation log. ``n`` rows, three days apart."""
    frame = pd.DataFrame(
        {
            "timestamp": pd.date_range("2025-01-01", periods=n, freq="3D", tz="UTC"),
            "quantity": (
                sizes if sizes is not None else np.full(n, 1.0)
            ),
            "realised_pnl": (
                outcomes if outcomes is not None else np.tile([50.0, -50.0], n // 2 + 1)[:n]
            ),
            "action": ["open"] * n,
        }
    )
    for name, values in columns.items():
        frame[name] = values
    return frame


def sized_after_outcome(n=60, *, after_win=2.0, after_loss=0.5):
    """A log where the next size depends on the previous result."""
    outcomes = np.tile([80.0, -80.0], n // 2 + 1)[:n]
    sizes = np.empty(n)
    sizes[0] = 1.0
    for index in range(1, n):
        sizes[index] = after_win if outcomes[index - 1] > 0 else after_loss
    return confirmations(n, sizes=sizes, outcomes=outcomes)


# --------------------------------------------------------------------------- #
# What this layer must not become
# --------------------------------------------------------------------------- #
#: Words that describe a person rather than a pattern. A finding containing
#: one of these has stopped reporting evidence and started diagnosing.
DIAGNOSING = (
    "undisciplined", "emotional", "irrational", "greedy", "reckless",
    "impulsive", "your problem", "you have a", "you always", "you never",
    "you tend to", "you struggle",
)


def every_finding():
    """Every finding this layer can produce, over a record that triggers each."""
    return build_bias_report(
        confirmations=sized_after_outcome(),
        trades=pd.DataFrame(),
        risk_history=pd.DataFrame({"effective_leverage": np.linspace(2, 6, 40)}),
        assessments=pd.DataFrame(
            {"margin_usage_pct_after_trade": np.full(40, 0.45)}
        ),
        now=NOW,
    ).findings


@pytest.mark.parametrize("finding", every_finding(), ids=lambda f: f.key)
def test_no_finding_describes_the_person(finding):
    """"You are overconfident" is a claim about character from a spreadsheet."""
    text = " ".join(
        (finding.evidence, finding.innocent_reading, finding.action, finding.title)
    ).lower()
    assert not [word for word in DIAGNOSING if word in text]


@pytest.mark.parametrize("finding", every_finding(), ids=lambda f: f.key)
def test_every_asserted_finding_offers_the_other_reading(finding):
    """The same numbers are usually consistent with having meant to."""
    if not finding.asserted:
        pytest.skip("not asserted")
    assert finding.innocent_reading
    assert finding.evidence


@pytest.mark.parametrize("finding", every_finding(), ids=lambda f: f.key)
def test_every_asserted_finding_proposes_a_process_not_a_temperament(finding):
    """"Be less emotional" is not an action."""
    if not finding.asserted:
        pytest.skip("not asserted")
    assert finding.action
    assert "try to" not in finding.action.lower()
    assert "be less" not in finding.action.lower()


# --------------------------------------------------------------------------- #
# The sample floor, and why it is higher here
# --------------------------------------------------------------------------- #
def test_a_thin_record_measures_nothing():
    result = detect_overconfidence(sized_after_outcome(8))
    assert result.severity is None
    assert result.confidence == "insufficient"


def test_an_unmeasured_pattern_is_absent_not_zero():
    """"We could not look" and "there is nothing there" are different, and the
    refinement layer treats them differently on purpose."""
    result = detect_overconfidence(None)
    assert result.severity is None
    assert not result.measured


def test_the_floor_is_higher_than_the_insights_layers():
    """Behavioural inference is weaker evidence than P&L arithmetic, and a
    severity here shrinks a real position."""
    from cfdinvest.insights import MINIMUM_SAMPLE

    assert MINIMUM_OBSERVATIONS > MINIMUM_SAMPLE


def test_nothing_is_asserted_below_the_floor():
    for count in (5, MINIMUM_OBSERVATIONS - 1):
        result = detect_overconfidence(sized_after_outcome(count))
        assert not result.asserted


def test_a_middling_record_is_provisional():
    result = detect_overconfidence(sized_after_outcome(MINIMUM_OBSERVATIONS + 6))
    assert result.confidence == "provisional"


def test_a_long_record_is_confident():
    result = detect_overconfidence(sized_after_outcome(CONFIDENT_OBSERVATIONS + 10))
    assert result.confidence == "confident"


# --------------------------------------------------------------------------- #
# The detectors
# --------------------------------------------------------------------------- #
def test_sizing_up_after_wins_is_detected():
    result = detect_overconfidence(sized_after_outcome(60))
    assert result.severity == pytest.approx(1.0)
    assert "after a win" in result.evidence


def test_a_steady_size_is_not_a_pattern():
    """A trader who always trades large is not reacting to anything."""
    result = detect_overconfidence(sized_after_outcome(60, after_win=1.0,
                                                       after_loss=1.0))
    assert result.severity == pytest.approx(0.0)
    assert not result.asserted


def test_sizing_down_after_wins_is_not_overconfidence():
    result = detect_overconfidence(sized_after_outcome(60, after_win=0.5,
                                                       after_loss=2.0))
    assert result.severity == pytest.approx(0.0)


def test_overconfidence_needs_outcomes_not_just_sizes():
    frame = confirmations(60).drop(columns=["realised_pnl"])
    result = detect_overconfidence(frame)
    assert result.severity is None
    assert "outcomes are not recorded" in result.details["reason"]


def test_revenge_trading_needs_both_speed_and_size():
    """Trading again soon after a loss is normal; soon *and* larger is not."""
    n = 60
    outcomes = np.tile([-80.0, 50.0], n // 2)[:n]
    sizes = np.where(np.arange(n) % 2 == 1, 5.0, 1.0)
    quick = pd.DataFrame(
        {
            "timestamp": pd.date_range(
                "2025-01-01", periods=n, freq="1h", tz="UTC"
            ),
            "quantity": sizes,
            "realised_pnl": outcomes,
            "action": ["open"] * n,
        }
    )
    assert detect_revenge_trading(quick).severity > ASSERT_THRESHOLD

    slow = quick.copy()
    slow["timestamp"] = pd.date_range("2025-01-01", periods=n, freq="5D", tz="UTC")
    assert detect_revenge_trading(slow).severity == pytest.approx(0.0)


def test_taking_less_than_the_plan_is_detected():
    frame = confirmations(60, quantity_deviation=np.full(60, 0.4))
    result = detect_fear_driven_underexposure(frame)
    assert result.severity == pytest.approx(1.0)


def test_the_underexposure_finding_never_suggests_trading_larger():
    """The worst thing this repository could do."""
    frame = confirmations(60, quantity_deviation=np.full(60, 0.4))
    result = detect_fear_driven_underexposure(frame)

    assert "ever tell you to trade larger" in result.action
    for phrase in ("trade bigger", "size up", "increase your size"):
        assert phrase not in result.action.lower()


def test_declining_after_losses_is_detected():
    n = 60
    outcomes = np.tile([-80.0, 50.0], n // 2)[:n]
    actions = ["ignore" if outcomes[i - 1] <= 0 and i else "open" for i in range(n)]
    frame = confirmations(n, outcomes=outcomes, action=actions)

    result = detect_risk_avoidance(frame)
    assert result.severity > ASSERT_THRESHOLD


def test_no_recorded_declines_is_not_evidence_of_not_declining():
    """An unrecorded pass leaves no trace at all."""
    result = detect_risk_avoidance(confirmations(60))
    assert result.severity is None
    assert "not evidence of not declining" in result.details["reason"]


def test_holding_losers_longer_is_detected():
    n = 60
    trips = pd.DataFrame(
        {
            "net_pnl": np.tile([50.0, -50.0], n // 2),
            "bars_held": np.tile([2.0, 20.0], n // 2),
        }
    )
    result = detect_loss_aversion(trips)
    assert result.severity > ASSERT_THRESHOLD
    assert "longer" in result.evidence


def test_loss_aversion_names_the_mechanical_explanation():
    """A wide stop and a near target produce this without anyone flinching."""
    trips = pd.DataFrame(
        {"net_pnl": np.tile([50.0, -50.0], 30), "bars_held": np.tile([2.0, 20.0], 30)}
    )
    assert "wide stop" in detect_loss_aversion(trips).innocent_reading


def test_loss_aversion_needs_a_holding_period():
    trips = pd.DataFrame({"net_pnl": np.tile([50.0, -50.0], 30)})
    result = detect_loss_aversion(trips)
    assert result.severity is None
    assert "no holding period" in result.details["reason"]


def exit_trips(n=60, *, target_share=1.0, stop_share=1.0):
    """Round trips whose exits land a given share of the way to the level."""
    winners = pd.DataFrame(
        {
            "net_pnl": np.full(n, 50.0),
            "entry_price": np.full(n, 100.0),
            "take_profit_level": np.full(n, 110.0),
            "stop_level": np.full(n, 95.0),
            "exit_price": np.full(n, 100.0 + 10.0 * target_share),
        }
    )
    losers = pd.DataFrame(
        {
            "net_pnl": np.full(n, -50.0),
            "entry_price": np.full(n, 100.0),
            "take_profit_level": np.full(n, 110.0),
            "stop_level": np.full(n, 95.0),
            "exit_price": np.full(n, 100.0 - 5.0 * stop_share),
        }
    )
    return pd.concat([winners, losers], ignore_index=True)


def test_closing_winners_early_is_detected():
    result = detect_premature_exits(exit_trips(target_share=0.3))
    assert result.severity > ASSERT_THRESHOLD
    assert "30%" in result.evidence


def test_reaching_the_target_is_not_a_pattern():
    assert detect_premature_exits(exit_trips(target_share=1.0)).severity == (
        pytest.approx(0.0)
    )


def test_closing_losers_late_is_detected():
    result = detect_late_exits(exit_trips(stop_share=1.6))
    assert result.severity > ASSERT_THRESHOLD


def test_the_late_exit_finding_names_gap_risk():
    """A gap through the stop closes past it with nobody deciding anything."""
    result = detect_late_exits(exit_trips(stop_share=1.6))
    assert "gap" in result.innocent_reading.lower()


def test_leverage_creep_is_detected():
    result = detect_leverage_creep(
        pd.DataFrame({"effective_leverage": np.linspace(2.0, 6.0, 40)})
    )
    assert result.severity > ASSERT_THRESHOLD
    assert "+" in result.evidence


def test_steady_leverage_is_not_creep():
    result = detect_leverage_creep(
        pd.DataFrame({"effective_leverage": np.full(40, 3.0)})
    )
    assert result.severity == pytest.approx(0.0)


def test_leverage_creep_names_the_rising_balance_explanation():
    result = detect_leverage_creep(
        pd.DataFrame({"effective_leverage": np.linspace(2.0, 6.0, 40)})
    )
    assert "rising balance" in result.innocent_reading


def test_trading_on_with_high_margin_is_detected():
    result = detect_margin_blindness(
        pd.DataFrame({"margin_usage_pct_after_trade": np.full(40, 0.48)})
    )
    assert result.severity > ASSERT_THRESHOLD


def test_low_margin_is_not_blindness():
    result = detect_margin_blindness(
        pd.DataFrame({"margin_usage_pct_after_trade": np.full(40, 0.05)})
    )
    assert result.severity == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# The report and the profile
# --------------------------------------------------------------------------- #
def test_the_report_looks_for_every_pattern():
    report = build_bias_report(confirmations=sized_after_outcome(), now=NOW)
    assert {f.key for f in report.findings} == set(BIASES)


def test_an_empty_record_measures_nothing_and_says_so():
    """"No biases detected" over a record too short to detect one is the most
    misleading sentence this layer could produce."""
    report = build_bias_report(now=NOW)
    assert not report.measured
    assert "too short" in report.headline


def test_a_measured_but_quiet_record_says_that_instead():
    report = build_bias_report(
        confirmations=sized_after_outcome(60, after_win=1.0, after_loss=1.0),
        now=NOW,
    )
    assert report.measured
    assert not report.asserted
    assert "none is pronounced enough" in report.headline


def test_the_profile_fills_the_contract_the_refinements_read():
    """One shape, filled here and read there."""
    from cfdinvest.livehelper.context import BiasProfile

    profile = build_bias_report(
        confirmations=sized_after_outcome(), now=NOW
    ).to_profile()

    assert isinstance(profile, BiasProfile)
    assert profile.available
    assert profile.severity("overconfidence") == pytest.approx(1.0)


def test_unmeasured_patterns_stay_none_in_the_profile():
    """Rounding them to zero would turn "we could not look" into "nothing
    there", and the refinements act on the difference."""
    profile = build_bias_report(
        confirmations=sized_after_outcome(), now=NOW
    ).to_profile()

    assert profile.severity("late_exits") is None
    assert "late_exits" not in profile.measured


def test_a_profile_from_nothing_is_unavailable():
    profile = build_bias_report(now=NOW).to_profile()
    assert not profile.available
    assert profile.measured == ()


def test_every_bias_key_matches_the_contract():
    """The contract was written first; this layer fills it rather than
    defining a second."""
    from cfdinvest.livehelper.context import BiasProfile

    fields = set(BiasProfile.__slots__)
    assert set(BIASES) <= fields


def test_a_detected_bias_reaches_the_sizing_refinement():
    """The consequence that justifies the conservatism."""
    from cfdinvest.livehelper import LiveHelperConfig, parse_trade_intent
    from cfdinvest.livehelper.context import TradingContext
    from cfdinvest.livehelper.refine_sizing import refine_sizing

    profile = build_bias_report(
        confirmations=sized_after_outcome(), now=NOW
    ).to_profile()
    result = refine_sizing(
        parse_trade_intent("long SPX500 1.0 entry=100 stop=96"),
        None, None, TradingContext(bias=profile), LiveHelperConfig(),
    )
    assert result.multiplier < 1.0
    assert any(key.startswith("bias:") for key in result.contributions)


def test_one_failing_detector_does_not_silence_the_others(monkeypatch):
    from cfdinvest.bias import model as model_module

    monkeypatch.setattr(
        model_module, "detect_overconfidence",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    report = build_bias_report(confirmations=sized_after_outcome(), now=NOW)

    assert len(report.findings) == len(BIASES)
    broken = report.get("overconfidence")
    assert broken.severity is None
    assert "not a finding about you" in broken.details["reason"]


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
def test_a_report_round_trips(lake):
    path = store.write_bias_report(
        build_bias_report(confirmations=sized_after_outcome(), now=NOW), lake
    )
    assert Path(path).name == "20260825-120000-bias.json"

    document = store.load_latest_bias_report(lake)
    assert len(document["findings"]) == len(BIASES)


def test_the_history_shows_how_a_pattern_moved(lake):
    """A severity that spiked once and settled is a week you had, not a trait
    you have."""
    # The second report has to be genuinely quiet: leaving after_loss at its
    # default kept the ratio at 2.0 and both readings at 100%.
    for hour, sizes in ((9, (2.0, 0.5)), (10, (1.0, 1.0))):
        store.write_bias_report(
            build_bias_report(
                confirmations=sized_after_outcome(
                    60, after_win=sizes[0], after_loss=sizes[1]
                ),
                now=pd.Timestamp(f"2026-08-25 {hour}:00", tz="UTC"),
            ),
            lake,
        )
    history = store.load_bias_history(lake, key="overconfidence")

    assert len(history) == 2
    assert history["severity"].iloc[0] > history["severity"].iloc[1]


def test_a_failed_write_returns_none(lake, monkeypatch):
    monkeypatch.setattr(
        store, "atomic_write_text",
        lambda *a, **k: (_ for _ in ()).throw(OSError("no space")),
    )
    assert store.write_bias_report(build_bias_report(now=NOW), lake) is None


def test_no_reports_is_an_empty_history(lake):
    assert store.load_latest_bias_report(lake) is None
    assert store.load_bias_history(lake).empty


def test_running_over_an_empty_lake_still_answers(lake):
    report = run_bias_detection(lake, store=False, now=NOW)
    assert len(report.findings) == len(BIASES)
    assert "too short" in report.headline


# --------------------------------------------------------------------------- #
# The digest
# --------------------------------------------------------------------------- #
def test_the_digest_keeps_the_caveat_beside_the_evidence():
    """Separated, the caveat is the part that gets skipped."""
    from cfdinvest.bias.digest import build_bias_digest

    body = build_bias_digest(
        build_bias_report(confirmations=sized_after_outcome(), now=NOW)
    ).get_content()

    assert "But:" in body
    evidence = body.index("after a win")
    assert body.index("But:", evidence) - evidence < 600


def test_the_digest_says_these_are_not_conclusions_about_you():
    from cfdinvest.bias.digest import build_bias_digest

    body = build_bias_digest(
        build_bias_report(confirmations=sized_after_outcome(), now=NOW)
    ).get_content()
    assert "not conclusions about you" in body
    assert "places no orders" in body


def test_the_digest_distinguishes_unmeasured_from_clear():
    from cfdinvest.bias.digest import build_bias_digest

    body = build_bias_digest(
        build_bias_report(confirmations=sized_after_outcome(), now=NOW)
    ).get_content()
    assert "NOT ENOUGH RECORD TO LOOK" in body
    assert "certainly not a clean bill" in body


# --------------------------------------------------------------------------- #
# Boundaries
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "module", sorted(Path("cfdinvest/bias").glob("*.py")), ids=lambda p: p.name
)
def test_bias_detection_reaches_nothing_that_could_trade(module):
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
                 "cfdinvest.schedule.runner")
    assert not [
        name for name in imported
        if any(name == bad or name.startswith(bad + ".") for bad in forbidden)
    ]
