"""Trend signals, sizing, and the regime/correlation/macro filters."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from cfdinvest.signals import filters as filters_module
from cfdinvest.signals import sizing as sizing_module
from cfdinvest.signals import trend as trend_module
from cfdinvest.schema import TICKER, TIMESTAMP


@pytest.fixture
def calendar() -> pd.DatetimeIndex:
    return pd.bdate_range("2022-01-03", periods=300, tz="UTC", name=TIMESTAMP)


def panel(calendar, values, tickers=("AAA", "BBB")) -> pd.DataFrame:
    frame = pd.DataFrame({t: values for t in tickers}, index=calendar)
    frame.columns.name = TICKER
    return frame


# --------------------------------------------------------------------------- #
# Trend
# --------------------------------------------------------------------------- #
def test_crossover_is_positive_in_a_rising_market(calendar):
    rising = panel(calendar, np.linspace(100, 300, len(calendar)))
    state = trend_module.moving_average_crossover(rising, 10, 50)
    assert state.iloc[-1].eq(1.0).all()


def test_crossover_is_negative_in_a_falling_market(calendar):
    falling = panel(calendar, np.linspace(300, 100, len(calendar)))
    assert trend_module.moving_average_crossover(falling, 10, 50).iloc[-1].eq(-1.0).all()


def test_crossover_rejects_a_fast_window_that_is_not_faster(calendar):
    with pytest.raises(ValueError, match="must be shorter"):
        trend_module.moving_average_crossover(panel(calendar, np.ones(300)), 50, 50)


def test_crossover_events_fire_only_on_the_transition(calendar):
    values = np.r_[np.linspace(100, 60, 150), np.linspace(60, 200, 150)]
    prices = panel(calendar, values)

    state = trend_module.moving_average_crossover(prices, 10, 50)
    events = trend_module.crossover_events(state)

    fired = events[events["AAA"] != 0]
    assert len(fired) >= 1
    assert (events["AAA"].abs() <= 1).all()


def test_donchian_bands_exclude_the_current_bar(calendar):
    """The classic off-by-one: without the shift, no breakout can ever fire."""
    rising = panel(calendar, np.linspace(100, 300, len(calendar)))
    upper, _ = trend_module.donchian_bounds(rising, rising, 20)

    # Today's high is above yesterday's 20-bar max in a monotone series.
    assert (rising.iloc[-1] > upper.iloc[-1]).all()


def test_breakout_fires_on_a_new_high(calendar):
    values = np.r_[np.full(200, 100.0), np.linspace(100, 150, 100)]
    prices = panel(calendar, values)

    state = trend_module.breakout_state(prices, window=20)
    assert state.iloc[-1].eq(1.0).all()


def test_breakdown_fires_on_a_new_low(calendar):
    values = np.r_[np.full(200, 100.0), np.linspace(100, 50, 100)]
    prices = panel(calendar, values)
    assert trend_module.breakout_state(prices, window=20).iloc[-1].eq(-1.0).all()


def test_no_breakout_inside_the_range(calendar):
    oscillating = 100 + 2 * np.sin(np.arange(len(calendar)) * 0.4)
    prices = panel(calendar, oscillating)

    state = trend_module.breakout_state(prices, window=50)
    assert state.iloc[-1].eq(0.0).all()


def test_breakout_falls_back_to_close_without_high_low(calendar):
    prices = panel(calendar, np.linspace(100, 200, len(calendar)))
    assert not trend_module.breakout_state(prices, None, None, 20).empty


def test_momentum_saturates_at_full_scale(calendar):
    returns = panel(calendar, np.full(len(calendar), 0.5))
    assert trend_module.momentum_component(returns, 0.10).iloc[-1].eq(1.0).all()


def test_momentum_is_proportional_below_saturation(calendar):
    returns = panel(calendar, np.full(len(calendar), 0.05))
    assert trend_module.momentum_component(returns, 0.10).iloc[-1].eq(0.5).all()


def test_momentum_rejects_a_bad_scale(calendar):
    with pytest.raises(ValueError, match="full_scale must be > 0"):
        trend_module.momentum_component(panel(calendar, np.ones(300)), 0.0)


def test_strength_blends_available_components(calendar):
    components = {
        "a": panel(calendar, np.full(len(calendar), 1.0)),
        "b": panel(calendar, np.full(len(calendar), -1.0)),
    }
    assert trend_module.trend_strength(components).iloc[-1].eq(0.0).all()


def test_strength_renormalises_over_present_components(calendar):
    """A component that has not warmed up must not drag the blend to NaN."""
    warm = panel(calendar, np.full(len(calendar), 1.0))
    cold = panel(calendar, np.full(len(calendar), np.nan))

    strength = trend_module.trend_strength({"warm": warm, "cold": cold})
    assert strength.iloc[-1].eq(1.0).all()


def test_strength_respects_weights(calendar):
    components = {
        "a": panel(calendar, np.full(len(calendar), 1.0)),
        "b": panel(calendar, np.full(len(calendar), -1.0)),
    }
    strength = trend_module.trend_strength(components, {"a": 3.0, "b": 1.0})
    assert np.allclose(strength.iloc[-1], 0.5)


def test_strength_is_bounded(calendar):
    components = {"a": panel(calendar, np.full(len(calendar), 5.0))}
    assert trend_module.trend_strength(components).iloc[-1].eq(1.0).all()


def test_strength_of_no_components():
    assert trend_module.trend_strength({}).empty


def test_agreement_is_one_when_unanimous(calendar):
    components = {
        name: panel(calendar, np.full(len(calendar), 1.0)) for name in "abc"
    }
    assert trend_module.component_agreement(components).iloc[-1].eq(1.0).all()


def test_agreement_reports_a_split(calendar):
    components = {
        "a": panel(calendar, np.full(len(calendar), 1.0)),
        "b": panel(calendar, np.full(len(calendar), 1.0)),
        "c": panel(calendar, np.full(len(calendar), -1.0)),
    }
    assert np.allclose(trend_module.component_agreement(components).iloc[-1], 2 / 3)


def test_zero_components_abstain_from_agreement(calendar):
    """A flat breakout must not dilute agreement between the other two."""
    components = {
        "a": panel(calendar, np.full(len(calendar), 1.0)),
        "b": panel(calendar, np.full(len(calendar), 1.0)),
        "c": panel(calendar, np.zeros(len(calendar))),
    }
    assert trend_module.component_agreement(components).iloc[-1].eq(1.0).all()


def test_trend_components_skips_absent_inputs(calendar):
    prices = panel(calendar, np.linspace(100, 200, len(calendar)))
    components = trend_module.trend_components(prices, None, None, None, fast=10, slow=50)
    assert set(components) == {"crossover", "breakout"}


# --------------------------------------------------------------------------- #
# Sizing
# --------------------------------------------------------------------------- #
def test_volatility_target_halves_size_at_double_volatility(calendar):
    volatility = panel(calendar, np.full(len(calendar), 0.30))
    size = sizing_module.volatility_target_size(volatility, target_volatility=0.15)
    assert np.allclose(size.iloc[-1], 0.5)


def test_volatility_target_caps_at_max_size(calendar):
    volatility = panel(calendar, np.full(len(calendar), 0.01))
    size = sizing_module.volatility_target_size(volatility, target_volatility=0.15)
    assert size.iloc[-1].eq(1.0).all()


def test_volatility_floor_prevents_an_explosive_size(calendar):
    """A temporarily tiny denominator is when the estimate is least reliable."""
    volatility = panel(calendar, np.full(len(calendar), 1e-9))
    size = sizing_module.volatility_target_size(
        volatility, target_volatility=0.15, floor_volatility=0.02, max_size=100.0
    )
    assert np.allclose(size.iloc[-1], 7.5)


def test_volatility_target_rejects_bad_parameters(calendar):
    volatility = panel(calendar, np.full(len(calendar), 0.2))
    with pytest.raises(ValueError, match="target_volatility"):
        sizing_module.volatility_target_size(volatility, target_volatility=0)
    with pytest.raises(ValueError, match="floor_volatility"):
        sizing_module.volatility_target_size(volatility, floor_volatility=0)


def test_atr_risk_size_matches_the_arithmetic(calendar):
    """1% risk with a 2-ATR stop at 1% ATR implies a half-unit position."""
    atr_pct = panel(calendar, np.full(len(calendar), 0.01))
    size = sizing_module.atr_risk_size(atr_pct, risk_fraction=0.01, stop_multiple=2.0)
    assert np.allclose(size.iloc[-1], 0.5)


def test_atr_risk_rejects_bad_parameters(calendar):
    atr_pct = panel(calendar, np.full(len(calendar), 0.01))
    with pytest.raises(ValueError, match="risk_fraction"):
        sizing_module.atr_risk_size(atr_pct, risk_fraction=0)
    with pytest.raises(ValueError, match="stop_multiple"):
        sizing_module.atr_risk_size(atr_pct, stop_multiple=0)


def test_blend_takes_the_conservative_size(calendar):
    generous = panel(calendar, np.full(len(calendar), 0.9))
    cautious = panel(calendar, np.full(len(calendar), 0.2))

    blended = sizing_module.blend_sizes(generous, cautious)
    assert np.allclose(blended.iloc[-1], 0.2)


def test_blend_ignores_an_absent_method(calendar):
    only = panel(calendar, np.full(len(calendar), 0.4))
    assert sizing_module.blend_sizes(only, pd.DataFrame()).iloc[-1].eq(0.4).all()


def test_blend_of_nothing_is_empty():
    assert sizing_module.blend_sizes(pd.DataFrame(), None).empty


def test_conviction_scaling_shrinks_a_marginal_signal(calendar):
    size = panel(calendar, np.full(len(calendar), 1.0))
    strength = panel(calendar, np.full(len(calendar), 0.5))

    scaled = sizing_module.scale_by_conviction(size, strength, min_scale=0.0)
    assert np.allclose(scaled.iloc[-1], 0.5)


def test_conviction_floor_protects_a_weak_but_valid_signal(calendar):
    size = panel(calendar, np.full(len(calendar), 1.0))
    strength = panel(calendar, np.full(len(calendar), 0.05))

    scaled = sizing_module.scale_by_conviction(size, strength, min_scale=0.25)
    assert np.allclose(scaled.iloc[-1], 0.25)


def test_conviction_scaling_rejects_a_bad_floor(calendar):
    size = panel(calendar, np.full(len(calendar), 1.0))
    with pytest.raises(ValueError, match="min_scale"):
        sizing_module.scale_by_conviction(size, size, min_scale=1.5)


def test_gross_exposure_cap_scales_a_row_proportionally(calendar):
    size = pd.DataFrame(
        {"A": np.full(len(calendar), 1.0), "B": np.full(len(calendar), 3.0)},
        index=calendar,
    )
    capped = sizing_module.cap_gross_exposure(size, max_gross=2.0)

    assert capped.iloc[-1].sum() == pytest.approx(2.0)
    # Relative sizing is preserved: B stays three times A.
    assert capped.iloc[-1]["B"] / capped.iloc[-1]["A"] == pytest.approx(3.0)


def test_gross_exposure_cap_leaves_small_rows_alone(calendar):
    size = panel(calendar, np.full(len(calendar), 0.2))
    pd.testing.assert_frame_equal(
        sizing_module.cap_gross_exposure(size, max_gross=5.0), size
    )


def test_sizes_never_contain_nan(calendar):
    volatility = panel(calendar, np.full(len(calendar), np.nan))
    size = sizing_module.volatility_target_size(volatility)
    assert size.notna().all().all()
    assert size.eq(0.0).all().all()


# --------------------------------------------------------------------------- #
# Filters
# --------------------------------------------------------------------------- #
@pytest.fixture
def columns() -> pd.Index:
    return pd.Index(["SPY", "TLT", "GLD", "MYSTERY"], name=TICKER)


def series(calendar, value) -> pd.Series:
    return pd.Series([value] * len(calendar), index=calendar)


def test_broadcast_spreads_a_market_series(calendar, columns):
    result = filters_module.broadcast(series(calendar, "risk_on"), calendar, columns)
    assert result.shape == (len(calendar), len(columns))
    assert (result == "risk_on").all().all()


def test_broadcast_of_nothing_is_unknown(calendar, columns):
    result = filters_module.broadcast(None, calendar, columns)
    assert (result == "unknown").all().all()


def test_asset_classes_default_to_other(columns):
    classes = filters_module.classify_assets(columns)
    assert classes["SPY"] == "equity"
    assert classes["TLT"] == "bond"
    assert classes["GLD"] == "commodity"
    assert classes["MYSTERY"] == "other"


@pytest.mark.parametrize(
    ("regime", "permitted"),
    [("risk_on", True), ("expansion", True), ("neutral", False),
     ("risk_off", False), ("crisis", False), ("stagflation", False)],
)
def test_regime_filter_permits_longs_only_in_the_named_regimes(
    calendar, columns, regime, permitted
):
    outcome = filters_module.regime_filter(
        series(calendar, regime), None, None, calendar, columns
    )
    assert bool(outcome.allow_long.iloc[-1].all()) is permitted


def test_regime_filter_permits_shorts_in_crisis(calendar, columns):
    outcome = filters_module.regime_filter(
        series(calendar, "crisis"), None, None, calendar, columns
    )
    assert outcome.allow_short.all().all()
    assert not outcome.allow_long.any().any()


def test_regime_filter_permits_shorts_in_macro_contraction(calendar, columns):
    outcome = filters_module.regime_filter(
        series(calendar, "neutral"), None, series(calendar, "contraction"),
        calendar, columns,
    )
    assert outcome.allow_short.all().all()


def test_high_volatility_halves_size(calendar, columns):
    outcome = filters_module.regime_filter(
        series(calendar, "risk_on"), series(calendar, "high"), None, calendar, columns
    )
    assert outcome.long_multiplier.iloc[-1].eq(0.5).all()


def test_unknown_regime_blocks_trading_by_default(calendar, columns):
    outcome = filters_module.regime_filter(None, None, None, calendar, columns)
    assert not outcome.allow_long.any().any()
    assert not outcome.allow_short.any().any()


def test_unknown_regime_can_be_permitted(calendar, columns):
    outcome = filters_module.regime_filter(
        None, None, None, calendar, columns, allow_when_unknown=True
    )
    assert outcome.allow_long.all().all()


def test_regime_filter_rejects_a_bad_multiplier(calendar, columns):
    with pytest.raises(ValueError, match="high_volatility_multiplier"):
        filters_module.regime_filter(
            None, None, None, calendar, columns, high_volatility_multiplier=2.0
        )


def test_correlation_breakdown_suppresses_longs(calendar, columns):
    outcome = filters_module.correlation_filter(
        series(calendar, "breakdown"), calendar, columns
    )
    assert outcome.long_multiplier.eq(0.0).all().all()


def test_correlation_breakdown_spares_shorts_by_default(calendar, columns):
    """Otherwise shorts are unreachable: crisis requires a breakdown."""
    outcome = filters_module.correlation_filter(
        series(calendar, "breakdown"), calendar, columns
    )
    assert outcome.short_multiplier.eq(1.0).all().all()


def test_correlation_breakdown_can_suppress_shorts_too(calendar, columns):
    outcome = filters_module.correlation_filter(
        series(calendar, "breakdown"), calendar, columns, applies_to_shorts=True
    )
    assert outcome.short_multiplier.eq(0.0).all().all()


def test_correlation_filter_never_changes_permissions(calendar, columns):
    outcome = filters_module.correlation_filter(
        series(calendar, "breakdown"), calendar, columns
    )
    assert outcome.allow_long.all().all()
    assert outcome.allow_short.all().all()


def test_inflation_tilt_penalises_non_hedges(calendar, columns):
    outcome = filters_module.macro_filter(
        None, series(calendar, "rising"), None, calendar, columns
    )
    last = outcome.long_multiplier.iloc[-1]

    assert last["GLD"] == pytest.approx(1.0)     # commodity: a hedge
    assert last["SPY"] == pytest.approx(0.5)     # equity: penalised
    assert last["TLT"] == pytest.approx(0.5)     # bond: penalised


def test_inflation_tilt_never_levers_up(calendar, columns):
    """The tilt is relative; gross exposure must not increase."""
    outcome = filters_module.macro_filter(
        None, series(calendar, "rising"), None, calendar, columns
    )
    assert (outcome.long_multiplier <= 1.0).all().all()


def test_contraction_blocks_equity_longs_only(calendar, columns):
    outcome = filters_module.macro_filter(
        series(calendar, "contraction"), None, None, calendar, columns
    )
    last = outcome.allow_long.iloc[-1]

    assert not last["SPY"]
    assert last["TLT"] and last["GLD"] and last["MYSTERY"]
    assert outcome.allow_short.all().all()      # shorts remain permitted


def test_loosening_labor_applies_a_haircut(calendar, columns):
    outcome = filters_module.macro_filter(
        None, None, series(calendar, "loosening"), calendar, columns
    )
    assert outcome.long_multiplier.iloc[-1].eq(0.75).all()


def test_macro_filter_rejects_bad_multipliers(calendar, columns):
    with pytest.raises(ValueError, match="inflation_penalty"):
        filters_module.macro_filter(
            None, None, None, calendar, columns, inflation_penalty=1.5
        )


def test_diversification_shrinks_a_crowded_asset(calendar):
    tickers = ["A", "B", "C"]
    blocks = []
    for stamp in calendar[:5]:
        matrix = pd.DataFrame(
            [[1.0, 1.0, 0.0], [1.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
            index=tickers, columns=tickers,
        )
        matrix.index = pd.MultiIndex.from_product([[stamp], tickers])
        blocks.append(matrix)
    rolling = pd.concat(blocks)

    multiplier = filters_module.diversification_multiplier(
        rolling, calendar[:5], pd.Index(tickers), floor=0.4
    )
    # A and B move together; C is independent.
    assert multiplier.iloc[-1]["A"] < multiplier.iloc[-1]["C"]
    assert multiplier.iloc[-1]["C"] == pytest.approx(1.0)


def test_diversification_of_no_correlations_is_neutral(calendar, columns):
    multiplier = filters_module.diversification_multiplier(None, calendar, columns)
    assert multiplier.eq(1.0).all().all()


def test_combine_ands_permissions_and_multiplies_sizes(calendar, columns):
    permissive = filters_module.permissive_outcome(calendar, columns)
    restrictive = filters_module.FilterOutcome(
        allow_long=pd.DataFrame(False, index=calendar, columns=columns),
        allow_short=pd.DataFrame(True, index=calendar, columns=columns),
        long_multiplier=pd.DataFrame(0.5, index=calendar, columns=columns),
        short_multiplier=pd.DataFrame(0.25, index=calendar, columns=columns),
        flags={},
    )
    combined = filters_module.combine_outcomes(permissive, restrictive)

    assert not combined.allow_long.any().any()
    assert combined.allow_short.all().all()
    assert combined.long_multiplier.eq(0.5).all().all()
    assert combined.short_multiplier.eq(0.25).all().all()


def test_combine_is_order_independent(calendar, columns):
    first = filters_module.regime_filter(
        series(calendar, "risk_on"), series(calendar, "high"), None, calendar, columns
    )
    second = filters_module.macro_filter(
        None, series(calendar, "rising"), None, calendar, columns
    )

    forward = filters_module.combine_outcomes(first, second)
    reverse = filters_module.combine_outcomes(second, first)

    pd.testing.assert_frame_equal(forward.long_multiplier, reverse.long_multiplier)
    pd.testing.assert_frame_equal(forward.allow_long, reverse.allow_long)


def test_combine_requires_at_least_one_outcome():
    with pytest.raises(ValueError, match="at least one"):
        filters_module.combine_outcomes()


def test_reason_flags_exclude_the_redundant_block_flags(calendar, columns):
    outcome = filters_module.regime_filter(
        series(calendar, "neutral"), series(calendar, "high"), None, calendar, columns
    )
    named = set(outcome.reason_flags())

    assert "high_volatility" in named
    assert "regime_blocks_long" not in named    # the rule name already says it
    assert "regime_blocks_long" in outcome.flags   # still available for diagnostics


def test_multiplier_for_selects_by_wanted_direction(calendar, columns):
    outcome = filters_module.FilterOutcome(
        allow_long=pd.DataFrame(True, index=calendar, columns=columns),
        allow_short=pd.DataFrame(True, index=calendar, columns=columns),
        long_multiplier=pd.DataFrame(0.2, index=calendar, columns=columns),
        short_multiplier=pd.DataFrame(0.8, index=calendar, columns=columns),
    )
    raw = pd.DataFrame("long", index=calendar, columns=columns)
    raw.iloc[:, 0] = "short"

    selected = outcome.multiplier_for(raw)
    assert selected.iloc[-1].iloc[0] == pytest.approx(0.8)
    assert selected.iloc[-1].iloc[1] == pytest.approx(0.2)
