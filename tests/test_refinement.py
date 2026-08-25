"""Refinement: adjusting a trade for what the per-trade checks cannot see.

The layer's danger is that it sits between the safety engine and the trader
and could quietly undo either. So most of what follows is about the lines it
must not cross: it may cut size and never raise it past what was approved, it
may not turn an unmeasured input into a reassuring one, and it may not answer
a question the safety engine already owns with a different number.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pandas as pd
import pytest

from cfdinvest.livehelper import (
    LiveHelperConfig,
    MarketState,
    compute_safe_sizing,
    evaluate_trade_safety,
    parse_trade_intent,
)
from cfdinvest.livehelper.context import (
    BiasProfile,
    MarginState,
    MetaStrategyPlan,
    PerformanceTrends,
    RegimeProfile,
    TradingContext,
)
from cfdinvest.livehelper.margin_warning import compute_margin_warning
from cfdinvest.livehelper.refine_checklist import refine_checklist
from cfdinvest.livehelper.refine_exit import ExitRefinementConfig, refine_exit
from cfdinvest.livehelper.refine_quality import UNMEASURED, refine_quality
from cfdinvest.livehelper.refine_sizing import RefinementConfig, refine_sizing
from cfdinvest.livehelper.safe_exit import SafeExitConfig, compute_safe_exit

#: Roomy on purpose: these tests are about the refinements, and a config that
#: bound on every trade would make every result about the safety engine.
ROOMY = LiveHelperConfig(
    account_balance_gbp=100_000.0,
    max_exposure_per_instrument_gbp=100_000.0,
    max_risk_per_trade_gbp=1_000.0,
    max_fx_exposure_pct=1.0,
)


def intent(text="long SPX500 1.0 entry=4434.8 stop=4390 tp=4520 lev=20"):
    return parse_trade_intent(text)


def assessed(text=None, config=None, **market):
    return evaluate_trade_safety(
        intent(text) if text else intent(), config or ROOMY, MarketState(**market)
    )


def context(**changes) -> TradingContext:
    return TradingContext(**changes)


def performing(**changes) -> PerformanceTrends:
    fields = dict(
        expectancy=40.0, recent_expectancy=38.0, trades=150, recent_trades=40,
        current_drawdown=-0.01, available=True,
    )
    fields.update(changes)
    return PerformanceTrends(**fields)


def regime_of(**labels) -> RegimeProfile:
    elevated = tuple(
        name for name, label in labels.items()
        if str(label).lower() in {"crisis", "high", "breakdown", "risk_off"}
    )
    return RegimeProfile(regimes=dict(labels), elevated=elevated, available=True)


# --------------------------------------------------------------------------- #
# The line this layer must not cross
# --------------------------------------------------------------------------- #
def test_a_refinement_can_cut_freely():
    """A smaller position is inside every limit the larger one was."""
    result = refine_sizing(
        intent(), None, None,
        context(performance=performing(current_drawdown=-0.15)),
        ROOMY,
    )
    assert result.multiplier < 1.0
    assert result.adjusted_quantity < 1.0


def test_a_refinement_can_never_raise_size_past_the_safe_size():
    """Otherwise a meta-strategy layer becomes a way round the limits."""
    tight = LiveHelperConfig(
        account_balance_gbp=100_000.0, max_exposure_per_instrument_gbp=2_000.0
    )
    sizing = compute_safe_sizing(intent(), tight, MarketState())
    result = refine_sizing(
        intent(), None, sizing,
        context(plan=MetaStrategyPlan(target_scale=1.5, available=True)),
        tight,
    )
    assert result.multiplier > 1.0
    assert result.capped_by_safety
    assert result.adjusted_quantity <= sizing.recommended_quantity


def test_being_capped_is_reported_rather_than_silent():
    """A reason to size up that was overruled is a different situation from
    one that was never proposed."""
    tight = LiveHelperConfig(
        account_balance_gbp=100_000.0, max_exposure_per_instrument_gbp=2_000.0
    )
    sizing = compute_safe_sizing(intent(), tight, MarketState())
    result = refine_sizing(
        intent(), None, sizing,
        context(plan=MetaStrategyPlan(target_scale=1.5, available=True)),
        tight,
    )
    assert any("cannot raise a limit" in reason for reason in result.reasons)


def test_with_no_safe_size_nothing_caps_the_refinement():
    result = refine_sizing(
        intent(), None, None,
        context(plan=MetaStrategyPlan(target_scale=1.2, available=True)),
        ROOMY,
    )
    assert not result.capped_by_safety
    assert result.adjusted_quantity == pytest.approx(1.2)


# --------------------------------------------------------------------------- #
# Compounding
# --------------------------------------------------------------------------- #
def test_several_small_cuts_are_clamped():
    """Six 0.8x factors are 0.26x, which is not a position."""
    result = refine_sizing(
        intent(), None, None,
        context(
            performance=performing(
                current_drawdown=-0.15, expectancy=40.0, recent_expectancy=5.0
            ),
            regime=regime_of(volatility_regime="high", correlation_regime="breakdown"),
            bias=BiasProfile(overconfidence=0.9, revenge_trading=0.9, available=True),
        ),
        ROOMY,
    )
    assert result.details["raw_multiplier"] < result.multiplier
    assert result.multiplier == pytest.approx(RefinementConfig().minimum_multiplier)
    assert any("compound faster" in reason for reason in result.reasons)


def test_every_contributor_is_itemised():
    """A 0.25x nobody can take apart is a number nobody can argue with."""
    result = refine_sizing(
        intent(), None, None,
        context(
            performance=performing(current_drawdown=-0.15),
            regime=regime_of(volatility_regime="high"),
        ),
        ROOMY,
    )
    assert set(result.contributions) == {"drawdown", "volatility_regime"}
    assert all(0 < factor <= 1 for factor in result.contributions.values())


def test_a_crisis_is_not_charged_twice():
    """Volatility already cut the size; the elevated flag must not cut again
    for the same fact."""
    result = refine_sizing(
        intent(), None, None,
        context(regime=regime_of(volatility_regime="high", combined_regime="crisis")),
        ROOMY,
    )
    assert "elevated_regime" not in result.contributions


# --------------------------------------------------------------------------- #
# Acting on evidence, not on noise
# --------------------------------------------------------------------------- #
def test_too_few_trades_makes_no_expectancy_adjustment():
    """An expectancy from four trades moves more than the thing it measures."""
    result = refine_sizing(
        intent(), None, None,
        context(performance=performing(trades=4, expectancy=-50.0)),
        ROOMY,
    )
    assert "negative_expectancy" not in result.contributions
    assert any("Too few" in reason for reason in result.reasons)


def test_a_losing_strategy_is_not_read_as_a_fading_one():
    """They want different responses, and "fading" is the gentler.

    With a negative expectancy, half of it is *greater* than it -- so a naive
    "recent below half the long run" test fires on almost anything.
    """
    losing = performing(trades=200, expectancy=-5.0, recent_expectancy=-5.0)
    assert not losing.deteriorating


def test_a_measurable_negative_expectancy_does_cut():
    result = refine_sizing(
        intent(), None, None,
        context(performance=performing(trades=200, expectancy=-5.0,
                                       recent_expectancy=-5.0)),
        ROOMY,
    )
    assert "negative_expectancy" in result.contributions


def test_a_fading_edge_cuts_size():
    result = refine_sizing(
        intent(), None, None,
        context(performance=performing(expectancy=40.0, recent_expectancy=5.0)),
        ROOMY,
    )
    assert result.contributions["fading_edge"] < 1.0


def test_a_healthy_record_changes_nothing():
    result = refine_sizing(intent(), None, None, context(
        performance=performing()), ROOMY)
    assert result.unchanged


def test_absent_inputs_are_named_not_assumed():
    """A refinement made on four inputs of six is smaller than one made on
    all six, and the difference has to be visible."""
    result = refine_sizing(intent(), None, None, context(), ROOMY)
    assert "a behavioural profile" in result.missing
    assert "the market regime" in result.missing
    assert result.unchanged


# --------------------------------------------------------------------------- #
# Bias
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "name", ["overconfidence", "revenge_trading", "leverage_creep"]
)
def test_biases_that_mean_trading_too_big_cut_size(name):
    result = refine_sizing(
        intent(), None, None,
        context(bias=BiasProfile(available=True, **{name: 0.8})),
        ROOMY,
    )
    assert result.contributions[f"bias:{name}"] < 1.0


def test_fear_driven_underexposure_never_increases_size():
    """The worst thing this repository could do is talk a frightened trader
    into a bigger position."""
    result = refine_sizing(
        intent(), None, None,
        context(bias=BiasProfile(fear_driven_underexposure=0.9, available=True)),
        ROOMY,
    )
    assert result.multiplier <= 1.0
    assert any("No size increase" in reason for reason in result.reasons)


def test_an_unmeasured_bias_is_not_a_clean_bill():
    """None means not measured, which is not zero."""
    result = refine_sizing(
        intent(), None, None,
        context(bias=BiasProfile(available=True, overconfidence=None)),
        ROOMY,
    )
    assert not any(key.startswith("bias:") for key in result.contributions)


def test_an_avoided_instrument_is_cut():
    result = refine_sizing(
        intent(), None, None,
        context(plan=MetaStrategyPlan(
            avoided_instruments=("SPX500",), available=True)),
        ROOMY,
    )
    assert result.contributions["meta_strategy"] < 1.0


# --------------------------------------------------------------------------- #
# Exits: the direction that matters
# --------------------------------------------------------------------------- #
def test_high_volatility_widens_the_stop():
    """The insight the module is built on.

    The intuition runs the other way -- things are dangerous, get out quickly
    -- and following it guarantees being stopped out by noise on the way to
    being right.
    """
    result = refine_exit(
        intent(), None, context(regime=regime_of(volatility_regime="high")), ROOMY
    )
    assert result.exit_multiplier > 1.0
    assert result.adjusted_stop < result.original_stop


def test_the_widening_and_the_cutting_point_the_same_way():
    """A wider stop at the same size is just more risk. The pairing is the
    adjustment, and the two modules must not disagree about the regime."""
    where = context(regime=regime_of(volatility_regime="high"))
    assert refine_exit(intent(), None, where, ROOMY).exit_multiplier > 1.0
    assert refine_sizing(intent(), None, None, where, ROOMY).multiplier < 1.0


def test_a_calm_market_allows_a_closer_stop():
    result = refine_exit(
        intent(), None, context(regime=regime_of(volatility_regime="low")), ROOMY
    )
    assert result.exit_multiplier < 1.0


def test_widening_re_checks_the_risk_limit():
    """The whole point of a per-trade risk limit is that it binds."""
    tight = LiveHelperConfig(
        account_balance_gbp=100_000.0, max_risk_per_trade_gbp=20.0,
        max_exposure_per_instrument_gbp=100_000.0,
    )
    result = refine_exit(
        intent(), None, context(regime=regime_of(volatility_regime="high")), tight
    )
    assert result.breaches_risk_limit
    assert result.size_for_widened_stop < 1.0


def test_the_affordable_size_is_reported_not_just_the_refusal():
    """"Wider stop, smaller position" is the answer; half of it is not."""
    tight = LiveHelperConfig(
        account_balance_gbp=100_000.0, max_risk_per_trade_gbp=20.0,
        max_exposure_per_instrument_gbp=100_000.0,
    )
    result = refine_exit(
        intent(), None, context(regime=regime_of(volatility_regime="high")), tight
    )
    assert any("fits at" in reason for reason in result.reasons)


def test_a_stop_never_crosses_the_entry():
    """A stop on the wrong side is an instruction to close at once, dressed
    as advice."""
    settings = ExitRefinementConfig(
        min_stop_multiple=0.75, max_stop_multiple=50.0,
        high_volatility_stop=40.0,
    )
    result = refine_exit(
        intent("long X 1.0 entry=100 stop=99 tp=110"), None,
        context(regime=regime_of(volatility_regime="high")), ROOMY, settings,
    )
    assert result.adjusted_stop is None or result.adjusted_stop < 100


def test_a_short_stop_sits_above_the_entry():
    result = refine_exit(
        intent("short SPX500 1.0 entry=4434.8 stop=4480 tp=4350"), None,
        context(regime=regime_of(volatility_regime="high")), ROOMY,
    )
    assert result.adjusted_stop > 4434.8
    assert result.adjusted_tp < 4434.8


def test_a_drawdown_brings_the_target_in():
    result = refine_exit(
        intent(), None,
        context(performance=performing(current_drawdown=-0.08)), ROOMY,
    )
    assert result.target_multiplier < 1.0
    assert result.adjusted_tp < result.original_tp


def test_premature_and_late_exit_findings_pull_opposite_ways():
    early = refine_exit(
        intent(), None,
        context(bias=BiasProfile(premature_exits=0.8, available=True)), ROOMY,
    )
    late = refine_exit(
        intent(), None,
        context(bias=BiasProfile(late_exits=0.8, available=True)), ROOMY,
    )
    assert early.target_multiplier > 1.0
    assert late.target_multiplier < 1.0


def test_both_exit_biases_at_once_mostly_cancel():
    """Which mostly means neither is established."""
    result = refine_exit(
        intent(), None,
        context(bias=BiasProfile(
            premature_exits=0.8, late_exits=0.8, available=True)),
        ROOMY,
    )
    assert 0.9 < result.target_multiplier < 1.1
    assert any("largely cancel" in reason for reason in result.reasons)


def test_no_context_leaves_the_exits_alone():
    assert refine_exit(intent(), None, context(), ROOMY).unchanged


# --------------------------------------------------------------------------- #
# Margin, as a direction
# --------------------------------------------------------------------------- #
def margin_of(**changes) -> MarginState:
    fields = dict(usage_pct=0.20, available=True)
    fields.update(changes)
    return MarginState(**fields)


def test_a_quiet_account_says_nothing():
    result = compute_margin_warning(None, context(margin=margin_of()), ROOMY)
    assert result.quiet


def test_two_readings_are_not_a_trend():
    """Two points make a line through anything."""
    result = compute_margin_warning(
        None, context(margin=margin_of(history=(0.10, 0.40))), ROOMY
    )
    assert result.trend is None
    assert not any("has risen" in flag for flag in result.flags)
    assert result.severity == "ok"


def test_three_rising_readings_are():
    """The shape a margin call actually has: a slope, not a step."""
    result = compute_margin_warning(
        None, context(margin=margin_of(usage_pct=0.35,
                                       history=(0.10, 0.22, 0.35))), ROOMY,
    )
    assert result.severity == "watch"
    assert any("has risen" in flag for flag in result.flags)


def test_leverage_creep_is_caught():
    result = compute_margin_warning(
        None,
        context(margin=margin_of(leverage_history=(2.0, 2.5, 3.2))),
        ROOMY,
    )
    assert any("crept up" in flag for flag in result.flags)


def test_being_over_the_limit_is_act_not_watch():
    result = compute_margin_warning(
        None, context(margin=margin_of(usage_pct=0.60)), ROOMY
    )
    assert result.act


def test_unmeasurable_margin_is_a_warning_not_silence():
    """Unknown margin is not low margin, and T212 closes on the ratio."""
    result = compute_margin_warning(None, context(), ROOMY)
    assert result.severity == "watch"
    assert not result.quiet


# --------------------------------------------------------------------------- #
# Quality
# --------------------------------------------------------------------------- #
def test_a_good_trade_scores_well():
    result = refine_quality(
        intent(), assessed(),
        context(performance=performing(), regime=regime_of(
            volatility_regime="low", combined_regime="risk_on")),
        ROOMY,
    )
    assert result.percent >= 65
    assert result.grade in ("sound", "strong")


def test_an_unmeasured_component_scores_the_midpoint_not_full_marks():
    """Full marks would let missing data flatter a trade.

    Asserted against the *property*, not against the constant: comparing to
    ``UNMEASURED`` would move with any change to it and prove nothing.
    """
    result = refine_quality(intent(), assessed(), context(), ROOMY)
    behavioural = next(
        c for c in result.components if c.name == "behavioural state"
    )
    assert not behavioural.measured
    assert 0.0 < behavioural.score < 1.0


def test_measuring_nothing_never_scores_as_well_as_measuring_good_things():
    """The consequence that matters: a trade nobody could assess must not
    come out level with one assessed and found sound."""
    blind = refine_quality(intent(), assessed(), context(), ROOMY)
    seen = refine_quality(
        intent(), assessed(),
        context(
            performance=performing(),
            regime=regime_of(volatility_regime="low", combined_regime="risk_on"),
            bias=BiasProfile(overconfidence=0.0, available=True),
        ),
        ROOMY,
        execution_history=pd.DataFrame({"quantity_deviation": [1.0, 1.0]}),
    )
    assert blind.percent < seen.percent
    assert not blind.confident and seen.confident


def test_the_score_says_how_much_of_it_was_measured():
    thin = refine_quality(intent(), assessed(), context(), ROOMY)
    thick = refine_quality(
        intent(), assessed(),
        context(performance=performing(), regime=regime_of(
            volatility_regime="low"),
            bias=BiasProfile(overconfidence=0.1, available=True)),
        ROOMY,
    )
    assert thin.measured_weight < thick.measured_weight
    assert not thin.confident


def test_the_score_cannot_permit_anything():
    """A high score on an UNSAFE trade is still an UNSAFE trade."""
    tight = LiveHelperConfig(
        account_balance_gbp=2_000.0, max_exposure_per_instrument_gbp=100.0
    )
    assessment = assessed(config=tight)
    assert assessment.status == "UNSAFE"

    result = refine_quality(
        intent(), assessment,
        context(performance=performing(), regime=regime_of(
            volatility_regime="low")),
        tight,
    )
    assert result.verdict == "UNSAFE"


def test_a_negative_expectancy_scores_zero_on_that_component():
    result = refine_quality(
        intent(), assessed(),
        context(performance=performing(expectancy=-10.0, recent_expectancy=-10.0)),
        ROOMY,
    )
    expectancy = next(c for c in result.components if c.name == "expectancy")
    assert expectancy.score == 0.0


def test_a_trade_at_the_edge_of_a_limit_scores_worse_than_one_with_room():
    """It passes, and one tick of slippage puts it over."""
    snug = LiveHelperConfig(
        account_balance_gbp=100_000.0, max_exposure_per_instrument_gbp=4_500.0,
        max_risk_per_trade_gbp=1_000.0, max_fx_exposure_pct=1.0,
    )
    tight_score = refine_quality(intent(), assessed(config=snug), context(), snug)
    roomy_score = refine_quality(intent(), assessed(), context(), ROOMY)

    assert tight_score.percent < roomy_score.percent


def test_execution_drift_lowers_the_score():
    history = pd.DataFrame({"quantity_deviation": [1.6, 0.4, 1.8]})
    drifted = refine_quality(
        intent(), assessed(), context(), ROOMY, execution_history=history
    )
    steady = refine_quality(
        intent(), assessed(), context(), ROOMY,
        execution_history=pd.DataFrame({"quantity_deviation": [1.0, 1.0, 1.0]}),
    )
    assert drifted.percent < steady.percent


# --------------------------------------------------------------------------- #
# The checklist
# --------------------------------------------------------------------------- #
def test_an_unchecked_blocking_item_is_not_a_pass():
    """"I did not look" and "it was fine" feel the same afterwards."""
    result = refine_checklist(intent(), None, context(), ROOMY)
    assert not result.go
    assert result.unchecked


def test_a_clean_trade_passes_the_list():
    result = refine_checklist(
        intent(), assessed(),
        context(performance=performing(), regime=regime_of(
            volatility_regime="low"), margin=margin_of()),
        ROOMY,
        margin_warning=compute_margin_warning(
            None, context(margin=margin_of()), ROOMY
        ),
    )
    assert result.go


def test_the_list_names_the_first_thing_to_fix():
    """A person with a ticket open needs what to change, not that to stop."""
    tight = LiveHelperConfig(
        account_balance_gbp=2_000.0, max_exposure_per_instrument_gbp=100.0
    )
    result = refine_checklist(intent(), assessed(config=tight), context(), tight)
    assert result.first_blocker is not None
    assert result.first_blocker.action


def test_the_items_are_ordered_by_what_stops_you_soonest():
    """A list that asks about reward to risk before the stop can be completed
    on a trade with no stop."""
    result = refine_checklist(intent(), assessed(), context(), ROOMY)
    keys = [item.key for item in result.items]
    assert keys.index("stop") < keys.index("conditions")
    assert keys.index("price") < keys.index("stop")


def test_the_regime_item_never_blocks():
    """A crisis is also where the opportunity is."""
    result = refine_checklist(
        intent(), assessed(),
        context(regime=regime_of(combined_regime="crisis")), ROOMY,
    )
    regime = next(item for item in result.items if item.key == "regime")
    assert regime.failed
    assert not regime.blocking
    assert result.go


def test_the_four_new_items_are_present():
    result = refine_checklist(intent(), assessed(), context(), ROOMY)
    keys = {item.key for item in result.items}
    assert {"regime", "bias", "plan", "margin_stability"} <= keys


def test_an_absent_behavioural_layer_shows_as_unchecked_not_passed():
    result = refine_checklist(intent(), assessed(), context(), ROOMY)
    bias = next(item for item in result.items if item.key == "bias")
    assert bias.state == "unchecked"


# --------------------------------------------------------------------------- #
# Getting out
# --------------------------------------------------------------------------- #
def test_a_comfortable_account_needs_no_exit():
    plan = compute_safe_exit(
        intent(), None, context(margin=margin_of(usage_pct=0.10)), ROOMY,
        position_quantity=1.0,
    )
    assert not plan.needed
    assert plan.urgency == "not needed"


def test_a_stressed_account_gets_a_partial_exit():
    plan = compute_safe_exit(
        intent(), None, context(margin=margin_of(usage_pct=0.40)), ROOMY,
        position_quantity=1.0,
    )
    assert plan.needed
    assert 0 < plan.fraction < 1


def test_the_exit_overshoots_the_limit():
    """Exiting to sit exactly on the limit means the next tick puts you back
    over, and the exit cost a spread for nothing."""
    plan = compute_safe_exit(
        intent(), None, context(margin=margin_of(usage_pct=0.40)), ROOMY,
        position_quantity=1.0,
    )
    assert plan.usage_after < ROOMY.max_margin_usage_pct_after_trade * (
        SafeExitConfig().target_usage_fraction + 0.01
    )


def test_approaching_the_brokers_ratio_is_urgent():
    """Trading 212 picks the position, not you."""
    plan = compute_safe_exit(
        intent(), None, context(margin=margin_of(usage_pct=0.48)), ROOMY,
        position_quantity=1.0,
    )
    assert plan.urgent
    assert any("picks the position itself" in reason for reason in plan.reasons)


def test_a_tiny_exit_is_not_worth_making():
    """The spread and the conversion fee cost more than the headroom."""
    plan = compute_safe_exit(
        intent(), None, context(margin=margin_of(usage_pct=0.355)), ROOMY,
        position_quantity=1.0,
    )
    assert not plan.needed
    assert any("cost more than the headroom" in reason for reason in plan.reasons)


def test_volatility_makes_a_convenient_exit_a_soon_one():
    plan = compute_safe_exit(
        intent(), None,
        context(margin=margin_of(usage_pct=0.40),
                regime=regime_of(volatility_regime="high")),
        ROOMY, position_quantity=1.0,
    )
    assert plan.urgency in ("soon", "now")


def test_an_exit_below_the_minimum_size_becomes_a_full_one():
    """An exit you cannot place is not an exit."""
    from cfdinvest.livehelper.t212_rules import InstrumentRules, RuleTable

    rules = RuleTable(rules={"SPX500": InstrumentRules(
        ticker="SPX500", min_quantity=1.0, quantity_precision=2)})
    plan = compute_safe_exit(
        intent(), None, context(margin=margin_of(usage_pct=0.40)), ROOMY,
        position_quantity=1.0, rules=rules,
    )
    assert plan.kind == "full"
    assert any("not an exit" in warning for warning in plan.warnings)


def test_a_remainder_below_the_minimum_becomes_a_full_exit():
    """Otherwise you are left holding a position you cannot later reduce."""
    from cfdinvest.livehelper.t212_rules import InstrumentRules, RuleTable

    rules = RuleTable(rules={"SPX500": InstrumentRules(
        ticker="SPX500", min_quantity=0.1, quantity_precision=2)})
    plan = compute_safe_exit(
        intent(), None, context(margin=margin_of(usage_pct=0.49)), ROOMY,
        position_quantity=0.12, rules=rules,
    )
    assert plan.kind == "full"


def test_the_exit_quantity_respects_the_instruments_precision():
    from cfdinvest.livehelper.t212_rules import InstrumentRules, RuleTable

    rules = RuleTable(rules={"SPX500": InstrumentRules(
        ticker="SPX500", quantity_precision=2)})
    plan = compute_safe_exit(
        intent(), None, context(margin=margin_of(usage_pct=0.40)), ROOMY,
        position_quantity=1.0, rules=rules,
    )
    assert plan.quantity == round(plan.quantity, 2)


def test_it_does_not_choose_which_position_to_close():
    """That is a judgement about the trades; this knows only margin."""
    plan = compute_safe_exit(
        intent(), None, context(margin=margin_of(usage_pct=0.40)), ROOMY,
        position_quantity=1.0,
    )
    assert any("leaves the choice of which to you" in r for r in plan.reasons)


def test_unmeasurable_margin_computes_no_exit_and_says_why():
    plan = compute_safe_exit(intent(), None, context(), ROOMY,
                             position_quantity=1.0)
    assert not plan.needed
    assert any("not low margin" in warning for warning in plan.warnings)


# --------------------------------------------------------------------------- #
# Reuse, not duplication
# --------------------------------------------------------------------------- #
#: The limits that already have an owner. A refinement module that defined its
#: own copy would give one question two answers, and they would drift.
OWNED_ELSEWHERE = {
    "max_risk_per_trade_gbp",
    "max_exposure_per_instrument_gbp",
    "max_leverage_per_trade",
    "max_margin_usage_pct_after_trade",
    "max_fx_exposure_pct",
    "borderline_fraction",
    "volatility_warning_threshold",
    "spread_warning_threshold",
    "max_total_exposure_gbp",
    "max_effective_leverage",
    "max_drawdown_pct",
    "lake_stale_days",
    "scheduler_stale_hours",
}

REFINEMENT_MODULES = [
    "context.py", "refine_sizing.py", "refine_exit.py", "margin_warning.py",
    "refine_quality.py", "refine_checklist.py", "safe_exit.py",
]


@pytest.mark.parametrize("module", REFINEMENT_MODULES)
def test_no_refinement_redefines_a_limit_that_already_has_an_owner(module):
    """The spec's rule, made structural.

    A refinement may *read* ``max_risk_per_trade_gbp``; it may not declare
    one. Two copies of a threshold is two answers to one question.
    """
    source = Path("cfdinvest/livehelper") / module
    tree = ast.parse(source.read_text(encoding="utf-8"))

    declared = {
        target.target.id if isinstance(target, ast.AnnAssign) else None
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
        for target in node.body
        if isinstance(target, ast.AnnAssign) and isinstance(target.target, ast.Name)
    }
    assert not (declared & OWNED_ELSEWHERE), (
        f"{module} redeclares {declared & OWNED_ELSEWHERE}"
    )


@pytest.mark.parametrize("module", REFINEMENT_MODULES)
def test_no_refinement_reaches_a_broker_or_the_engine(module):
    source = Path("cfdinvest/livehelper") / module
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
    forbidden = ("requests", "urllib", "httpx", "socket", "selenium",
                 "cfdinvest.backtest", "cfdinvest.schedule.runner")
    assert not [
        name for name in imported
        if any(name == bad or name.startswith(bad + ".") for bad in forbidden)
    ]


def test_the_regime_verdict_comes_from_the_risk_layer():
    """One opinion about what counts as elevated, not two."""
    from cfdinvest.livehelper import context as context_module

    source = Path("cfdinvest/livehelper/context.py").read_text(encoding="utf-8")
    assert "compute_regime_risk" in source
    assert "ELEVATED_REGIMES" not in source


# --------------------------------------------------------------------------- #
# All together
# --------------------------------------------------------------------------- #
def test_one_call_runs_every_refinement():
    from cfdinvest.livehelper import refine

    result = refine(
        intent(), assessed(),
        compute_safe_sizing(intent(), ROOMY, MarketState()),
        context(performance=performing(), regime=regime_of(
            volatility_regime="high"), margin=margin_of()),
        ROOMY,
        position_quantity=1.0,
    )
    assert result.sizing.multiplier < 1.0
    assert result.exit.exit_multiplier > 1.0
    assert result.quality is not None
    assert result.checklist is not None
    assert result.margin is not None
    assert result.exit_plan is not None


def test_the_bundle_reports_what_it_could_not_read():
    from cfdinvest.livehelper import refine

    result = refine(intent(), assessed(), None, context(), ROOMY)
    assert "a behavioural profile" in result.missing
    assert "a meta-strategy plan" in result.missing


def test_the_bundles_go_is_the_checklists_not_a_second_verdict():
    from cfdinvest.livehelper import refine

    result = refine(intent(), assessed(), None, context(), ROOMY)
    assert result.go == result.checklist.go


def test_loading_a_context_from_an_empty_lake_says_everything_is_missing(tmp_path):
    from cfdinvest.config import AppConfig, PathConfig
    from cfdinvest.livehelper import load_context

    result = load_context(AppConfig(paths=PathConfig(data_root=tmp_path / "data")))
    assert not result.complete
    assert not result.performance.available
    assert not result.regime.available
