"""Intrabar stop-loss and take-profit: the full test matrix.

The eleven cases the specification names are here under those names, plus
the ones that matter for the accounting: that enabling stops does not break
the P&L identity, and that a stopped bar never re-enters at a price which
occurred before the exit.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from cfdinvest.execution import ExecutionConfig, run_backtest
from cfdinvest.execution.state import ACTIONS
from cfdinvest.execution.stops import (
    DISTANCE_MODES,
    STOP_EVENT_COLUMNS,
    STOP_MODES,
    StopConfig,
    StopEvent,
    TakeProfitEvent,
    detect_breaches,
    level_distance,
    recommended_orders,
    resolve_breach,
    stop_levels,
)
from cfdinvest.preprocessing import PreprocessConfig, build_analysis_frame
from cfdinvest.schema import TICKER, TIMESTAMP
from cfdinvest.signals import SignalFrame

#: No slippage, so a fill price equals its reference exactly and the
#: assertions are about the *logic* rather than about a cost model.
CLEAN = StopConfig(enabled=True, slippage_bps=0.0)


def breach(direction, open_, high, low, stop, take_profit=None, config=CLEAN):
    return resolve_breach(
        "X", "2025-01-02", direction, open_, high, low, stop, take_profit, config
    )


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def test_stops_are_disabled_by_default():
    """Enabling them changes every recorded backtest, so it is opt-in."""
    assert StopConfig().enabled is False
    assert ExecutionConfig().stops.enabled is False


def test_the_documented_defaults():
    config = StopConfig()
    assert config.stop_mode == "hybrid"
    assert config.tp_priority is True
    assert config.stop_distance_mode == "atr"
    assert config.tp_distance_mode == "atr"
    assert config.slippage_bps == 0.0
    assert config.record_recommended_fill is True


@pytest.mark.parametrize("mode", STOP_MODES)
def test_every_stop_mode_is_usable(mode):
    assert StopConfig(stop_mode=mode).stop_mode == mode


@pytest.mark.parametrize("mode", DISTANCE_MODES)
def test_every_distance_mode_is_usable(mode):
    assert StopConfig(stop_distance_mode=mode, tp_distance_mode=mode)


def test_unknown_modes_are_refused():
    with pytest.raises(ValueError, match="stop_mode"):
        StopConfig(stop_mode="vibes")
    with pytest.raises(ValueError, match="stop_distance_mode"):
        StopConfig(stop_distance_mode="vibes")
    with pytest.raises(ValueError, match="tp_distance_mode"):
        StopConfig(tp_distance_mode="vibes")


def test_bad_distances_and_slippage_are_refused():
    with pytest.raises(ValueError, match="stop_distance"):
        StopConfig(stop_distance=0)
    with pytest.raises(ValueError, match="tp_distance"):
        StopConfig(tp_distance=-1)
    with pytest.raises(ValueError, match="slippage_bps"):
        StopConfig(slippage_bps=-1)
    with pytest.raises(ValueError, match="max_slippage_bps"):
        StopConfig(max_slippage_bps=-1)


def test_slippage_is_capped():
    config = StopConfig(slippage_bps=500.0, max_slippage_bps=20.0)
    assert config.stop_slippage_fraction == pytest.approx(0.002)


def test_stop_actions_are_declared():
    assert "stop_loss" in ACTIONS
    assert "take_profit" in ACTIONS


# --------------------------------------------------------------------------- #
# Level placement
# --------------------------------------------------------------------------- #
def test_atr_distance_mode():
    assert level_distance([100.0], [5.0], "atr", 2.0)[0] == pytest.approx(10.0)


def test_fixed_distance_mode():
    assert level_distance([100.0], [5.0], "fixed", 3.0)[0] == pytest.approx(3.0)


def test_percent_distance_mode():
    assert level_distance([100.0], [5.0], "percent", 0.05)[0] == pytest.approx(5.0)


def test_levels_sit_on_the_right_side():
    stop, target = stop_levels(
        [100.0, 100.0],
        [5.0, 5.0],
        [1, -1],
        StopConfig(stop_distance=2.0, tp_distance=3.0),
    )
    assert stop[0] == pytest.approx(90.0)  # long: below
    assert target[0] == pytest.approx(115.0)  # long: above
    assert stop[1] == pytest.approx(110.0)  # short: above
    assert target[1] == pytest.approx(85.0)  # short: below


def test_percent_levels_do_not_need_an_atr():
    stop, _ = stop_levels(
        [100.0],
        [np.nan],
        [1],
        StopConfig(stop_distance_mode="percent", stop_distance=0.05),
    )
    assert stop[0] == pytest.approx(95.0)


def test_fixed_levels_do_not_need_an_atr():
    stop, _ = stop_levels(
        [100.0],
        [np.nan],
        [1],
        StopConfig(stop_distance_mode="fixed", stop_distance=4.0),
    )
    assert stop[0] == pytest.approx(96.0)


def test_no_levels_without_a_position():
    stop, target = stop_levels([100.0], [5.0], [0], CLEAN)
    assert np.isnan(stop[0]) and np.isnan(target[0])


def test_atr_levels_need_an_atr():
    stop, _ = stop_levels([100.0, 100.0], [np.nan, 0.0], [1, 1], CLEAN)
    assert np.isnan(stop).all()


def test_take_profit_can_be_switched_off():
    stop, target = stop_levels([100.0], [5.0], [1], StopConfig(tp_distance=None))
    assert np.isfinite(stop[0])
    assert np.isnan(target[0])


# --------------------------------------------------------------------------- #
# Matrix 1-8: the core cases
# --------------------------------------------------------------------------- #
def test_1_stop_hit_without_a_gap():
    event = breach("long", 100, 104, 94, 95, 110)

    assert isinstance(event, StopEvent)
    assert event.kind == "stop"
    assert event.breach_type == "intrabar"
    assert event.gap_through is False
    assert event.fill_price == pytest.approx(95.0)  # fills at the level


def test_2_stop_hit_with_a_gap_through():
    """A level is worth what you can get, not what you asked for."""
    event = breach("long", 88, 92, 86, 95, 110)

    assert event.kind == "stop"
    assert event.breach_type == "gap"
    assert event.gap_through is True
    assert event.fill_price == pytest.approx(88.0)  # the open, not 95
    assert event.level == pytest.approx(95.0)


def test_3_stop_hit_exactly_at_the_open():
    """Opening *on* the level counts as a gap: it was reached at the open."""
    event = breach("long", 95, 99, 93, 95, 110)

    assert event.kind == "stop"
    assert event.breach_type == "gap"
    assert event.fill_price == pytest.approx(95.0)


def test_4_stop_breached_intrabar_but_price_recovers():
    """The whole reason this module exists.

    Close-to-close marking never sees this bar: the position rode through
    its stop and closed above it, and the drawdown was never recorded.
    """
    event = breach("long", 100, 106, 94, 95, 110)

    assert event is not None
    assert event.kind == "stop"
    assert event.breach_type == "intrabar"
    assert event.fill_price == pytest.approx(95.0)


def test_5_take_profit_hit():
    event = breach("long", 100, 112, 99, 95, 110)

    assert isinstance(event, TakeProfitEvent)
    assert event.kind == "take_profit"
    assert event.fill_price == pytest.approx(110.0)


def test_6_both_hit_intrabar_tp_priority_decides():
    optimistic = breach("long", 100, 112, 94, 95, 110)
    assert optimistic.kind == "take_profit"
    assert optimistic.ambiguous is True

    conservative = breach(
        "long",
        100,
        112,
        94,
        95,
        110,
        StopConfig(enabled=True, slippage_bps=0.0, tp_priority=False),
    )
    assert conservative.kind == "stop"
    assert conservative.ambiguous is True


def test_7_both_gap_through_nearest_level_to_the_open_wins():
    """Only reachable with unusually placed levels, and it must still resolve.

    Which level was crossed first is a fact here, not a convention: the one
    nearer the open. A long whose stop sits *above* its target is
    pathological, but the resolution must be defined rather than arbitrary.
    """
    # Long with an inverted pair: stop 105 above, target 95 below. An open at
    # 104 is beyond both, but only just beyond the stop.
    nearer_stop = breach("long", 104, 106, 103, 105, 95)
    assert nearer_stop.kind == "stop"
    assert nearer_stop.breach_type == "gap"
    assert nearer_stop.fill_price == pytest.approx(104.0)

    nearer_target = breach("long", 96, 106, 95, 105, 95)
    assert nearer_target.kind == "take_profit"
    assert nearer_target.breach_type == "gap"
    assert nearer_target.fill_price == pytest.approx(96.0)


def test_7_both_gap_through_never_leaves_the_bar_unresolved():
    result = detect_breaches([1], [104], [106], [103], [105], [95], CLEAN)
    assert result["breached"][0]
    assert result["breach_type"][0] == "gap"
    assert np.isfinite(result["fill_price"][0])


def test_8_no_stop_or_take_profit_hit():
    assert breach("long", 100, 104, 98, 95, 110) is None


# --------------------------------------------------------------------------- #
# Matrix 9: slippage
# --------------------------------------------------------------------------- #
def test_9_slippage_is_applied_after_the_fill_price():
    # The cap defaults to 50 bps, so it has to be raised out of the way for
    # the requested 100 bps to be the number under test.
    config = StopConfig(enabled=True, slippage_bps=100.0, max_slippage_bps=200.0)
    event = breach("long", 100, 104, 94, 95, None, config)

    assert event.reference_price == pytest.approx(95.0)
    assert event.fill_price == pytest.approx(95.0 * 0.99)
    assert event.slippage_applied == pytest.approx(0.95)
    assert event.slippage_bps == pytest.approx(100.0)


def test_9_slippage_is_always_adverse():
    config = StopConfig(enabled=True, slippage_bps=100.0, max_slippage_bps=200.0)

    long_stop = breach("long", 100, 104, 94, 95, None, config)
    short_stop = breach("short", 100, 106, 96, 105, None, config)

    assert long_stop.fill_price < 95.0  # a long sells lower
    assert short_stop.fill_price > 105.0  # a short buys higher


def test_9_slippage_respects_the_cap():
    config = StopConfig(enabled=True, slippage_bps=1_000.0, max_slippage_bps=100.0)
    event = breach("long", 100, 104, 94, 95, None, config)

    assert event.slippage_bps == pytest.approx(100.0)
    assert event.fill_price == pytest.approx(95.0 * 0.99)


def test_9_no_slippage_by_default_leaves_the_fill_on_the_level():
    event = breach("long", 100, 104, 94, 95, None)
    assert event.fill_price == pytest.approx(event.reference_price)
    assert event.slippage_applied == pytest.approx(0.0)


def test_9_a_take_profit_is_a_limit_and_does_not_slip_by_default():
    config = StopConfig(enabled=True, slippage_bps=100.0, max_slippage_bps=200.0)
    event = breach("long", 100, 112, 99, 95, 110, config)

    assert event.fill_price == pytest.approx(110.0)
    assert event.slippage_applied == pytest.approx(0.0)


def test_9_take_profit_slippage_can_be_configured():
    config = StopConfig(enabled=True, take_profit_slippage_bps=100.0, slippage_bps=0.0)
    assert breach("long", 100, 112, 99, 95, 110, config).fill_price < 110.0


# --------------------------------------------------------------------------- #
# Matrix 10: shorts mirror longs
# --------------------------------------------------------------------------- #
def test_10_short_stop_and_target_mirror():
    stopped = breach("short", 100, 106, 96, 105, 90)
    assert stopped.kind == "stop"
    assert stopped.fill_price == pytest.approx(105.0)

    target = breach("short", 100, 102, 88, 105, 90)
    assert target.kind == "take_profit"
    assert target.fill_price == pytest.approx(90.0)


def test_10_short_gap_through_a_stop():
    event = breach("short", 108, 110, 107, 105, 90)
    assert event.breach_type == "gap"
    assert event.fill_price == pytest.approx(108.0)


def test_10_short_stop_exactly_at_the_open():
    event = breach("short", 105, 107, 103, 105, 90)
    assert event.kind == "stop"
    assert event.breach_type == "gap"


def test_10_short_breached_but_recovers():
    event = breach("short", 100, 106, 96, 105, 90)
    assert event.kind == "stop"
    assert event.breach_type == "intrabar"


def test_10_short_with_no_breach():
    assert breach("short", 100, 103, 96, 105, 90) is None


def test_10_short_both_hit_intrabar():
    assert breach("short", 100, 106, 88, 105, 90).kind == "take_profit"


def test_an_invalid_direction_is_refused():
    with pytest.raises(ValueError, match="long.*short"):
        breach("sideways", 100, 104, 96, 95, 110)


# --------------------------------------------------------------------------- #
# Matrix 11: manual execution
# --------------------------------------------------------------------------- #
def test_11_a_recommended_fill_is_always_returned():
    event = breach("long", 88, 92, 86, 95, 110)
    assert event.recommended_fill == pytest.approx(event.fill_price)
    assert np.isfinite(event.recommended_fill)


def test_11_recommended_fill_can_be_suppressed():
    config = StopConfig(enabled=True, slippage_bps=0.0, record_recommended_fill=False)
    assert np.isnan(breach("long", 100, 104, 94, 95, None, config).recommended_fill)


def test_11_the_event_row_carries_the_recommended_fill():
    row = breach("long", 88, 92, 86, 95, 110).as_row()
    assert set(row) == set(STOP_EVENT_COLUMNS)
    assert row["recommended_fill"] == pytest.approx(88.0)


def test_11_recommended_orders_name_the_right_side():
    orders = recommended_orders(
        positions=pd.Series({"SPY": 10.0, "TLT": -5.0}),
        entry_price=pd.Series({"SPY": 100.0, "TLT": 90.0}),
        stop=pd.Series({"SPY": 95.0, "TLT": 94.0}),
        take_profit=pd.Series({"SPY": 110.0, "TLT": 82.0}),
        last_price=pd.Series({"SPY": 101.0, "TLT": 89.0}),
    )
    assert orders.loc["SPY", "stop_order"].startswith("SELL STOP")
    assert orders.loc["TLT", "stop_order"].startswith("BUY STOP")


def test_11_recommended_orders_skip_flat_positions():
    orders = recommended_orders(
        pd.Series({"SPY": 0.0, "TLT": -5.0}),
        pd.Series({"SPY": 100.0, "TLT": 90.0}),
        pd.Series({"SPY": 95.0, "TLT": 94.0}),
        pd.Series({"SPY": 110.0, "TLT": 82.0}),
        pd.Series({"SPY": 101.0, "TLT": 89.0}),
    )
    assert list(orders.index) == ["TLT"]


def test_11_recommended_orders_report_distance():
    orders = recommended_orders(
        pd.Series({"SPY": 10.0}),
        pd.Series({"SPY": 100.0}),
        pd.Series({"SPY": 95.0}),
        pd.Series({"SPY": 110.0}),
        pd.Series({"SPY": 100.0}),
    )
    assert orders.loc["SPY", "distance_to_stop"] == pytest.approx(-0.05)


def test_11_recommended_orders_of_an_empty_book():
    empty = pd.Series(dtype="float64")
    assert recommended_orders(empty, empty, empty, empty, empty).empty


# --------------------------------------------------------------------------- #
# stop_mode
# --------------------------------------------------------------------------- #
def test_breach_mode_ignores_the_gap():
    """Optimistic by construction: a fill the market never offered."""
    config = StopConfig(enabled=True, slippage_bps=0.0, stop_mode="breach")
    event = breach("long", 88, 92, 86, 95, 110, config)

    assert event.breach_type == "gap"  # still reported honestly
    assert event.fill_price == pytest.approx(95.0)


def test_gap_mode_always_fills_at_the_open():
    config = StopConfig(enabled=True, slippage_bps=0.0, stop_mode="gap")
    event = breach("long", 100, 104, 94, 95, 110, config)

    assert event.breach_type == "intrabar"
    assert event.fill_price == pytest.approx(100.0)


def test_hybrid_mode_is_the_realistic_middle():
    intrabar = breach("long", 100, 104, 94, 95, 110)
    gapped = breach("long", 88, 92, 86, 95, 110)

    assert intrabar.fill_price == pytest.approx(95.0)  # traded through
    assert gapped.fill_price == pytest.approx(88.0)  # gapped through


def test_breach_mode_is_never_worse_than_hybrid_for_a_long():
    gapped = breach("long", 88, 92, 86, 95, 110)
    optimistic = breach(
        "long",
        88,
        92,
        86,
        95,
        110,
        StopConfig(enabled=True, slippage_bps=0.0, stop_mode="breach"),
    )
    assert optimistic.fill_price > gapped.fill_price


# --------------------------------------------------------------------------- #
# Vectorised detection
# --------------------------------------------------------------------------- #
def test_detection_is_vectorised_across_assets():
    result = detect_breaches(
        direction=[1, 1, -1, 0],
        open_price=[100, 88, 100, 100],
        high=[104, 92, 106, 200],
        low=[98, 86, 96, 1],
        stop=[95, 95, 105, 95],
        take_profit=[110, 110, 90, 110],
        config=CLEAN,
    )
    assert list(result["breached"]) == [False, True, True, False]
    assert result["breach_type"][1] == "gap"
    assert result["breach_type"][2] == "intrabar"


def test_a_flat_position_never_breaches():
    assert not detect_breaches([0], [50], [200], [1], [95], [110], CLEAN)["breached"][0]


def test_missing_levels_never_breach():
    result = detect_breaches([1], [100], [200], [1], [np.nan], [np.nan], CLEAN)
    assert not result["breached"][0]


def test_detection_is_deterministic():
    args = ([1], [100], [112], [94], [95], [110], CLEAN)
    first, second = detect_breaches(*args), detect_breaches(*args)
    assert first["breached"] == second["breached"]
    assert first["fill_price"] == second["fill_price"]


def test_a_gap_is_never_flagged_ambiguous():
    """Opening beyond a level settles the order of events by itself."""
    assert breach("long", 88, 115, 86, 95, 110).ambiguous is False


def test_an_unambiguous_intrabar_bar_is_not_flagged():
    assert breach("long", 100, 104, 94, 95, 110).ambiguous is False


# --------------------------------------------------------------------------- #
# Engine integration
# --------------------------------------------------------------------------- #
BARS = 300


@pytest.fixture
def calendar() -> pd.DatetimeIndex:
    return pd.bdate_range("2021-01-04", periods=BARS, tz="UTC", name=TIMESTAMP)


def market(calendar, closes: dict[str, np.ndarray], width: float = 0.01):
    blocks = []
    for ticker, close in closes.items():
        blocks.append(
            pd.DataFrame(
                {
                    "open": close * 0.999,
                    "high": close * (1 + width),
                    "low": close * (1 - width),
                    "close": close,
                    "volume": 1e6,
                    TICKER: ticker,
                },
                index=calendar,
            )
        )
    return pd.concat(blocks).set_index(TICKER, append=True).sort_index()


def signals_for(analysis) -> SignalFrame:
    calendar, columns = analysis.calendar, analysis.close.columns
    return SignalFrame(
        direction=pd.DataFrame("long", index=calendar, columns=columns),
        size=pd.DataFrame(0.3, index=calendar, columns=columns),
        confidence=pd.DataFrame(0.9, index=calendar, columns=columns),
        reason=pd.DataFrame("test", index=calendar, columns=columns),
    )


@pytest.fixture
def analysis(calendar):
    rng = np.random.default_rng(23)
    closes = {
        t: 100 * np.exp(np.cumsum(rng.normal(0.0006, 0.012, BARS)))
        for t in ("AAA", "BBB")
    }
    return build_analysis_frame(
        market(calendar, closes),
        interval="1d",
        config=PreprocessConfig(correlation_window=63),
    )


@pytest.fixture
def always_long(analysis):
    return signals_for(analysis)


def enabled(**changes) -> ExecutionConfig:
    return ExecutionConfig(stops=StopConfig(enabled=True, **changes))


def test_disabled_stops_reproduce_the_previous_behaviour(analysis, always_long):
    """Backward compatibility: the default must not move any number."""
    execution = run_backtest(always_long, analysis, ExecutionConfig())

    assert execution.stop_events.empty
    assert execution.metadata["stops_enabled"] is False


def test_enabling_stops_produces_exits(analysis, always_long):
    execution = run_backtest(always_long, analysis, enabled(stop_distance=1.0))

    assert not execution.stop_events.empty
    assert set(execution.stop_events.columns) == set(STOP_EVENT_COLUMNS)


def test_stops_change_the_equity_curve(analysis, always_long):
    without = run_backtest(always_long, analysis, ExecutionConfig())
    with_stops = run_backtest(always_long, analysis, enabled(stop_distance=1.0))
    assert not np.allclose(without.equity_curve, with_stops.equity_curve)


def test_the_pnl_identity_survives_stops(analysis, always_long):
    """The accounting must stay exact with a mid-bar exit."""
    from cfdinvest.execution.pnl import PNL_COMPONENTS

    execution = run_backtest(always_long, analysis, enabled(stop_distance=1.0))
    parts = execution.pnl[list(PNL_COMPONENTS)].sum(axis=1)

    assert np.allclose(parts, execution.pnl["total"])
    assert np.allclose(
        execution.equity_curve,
        execution.metadata["initial_equity"] + execution.pnl["total"].cumsum(),
    )


def test_a_stopped_bar_does_not_re_enter(analysis, always_long):
    """Re-entering at the bar's open would be entering before the exit."""
    execution = run_backtest(always_long, analysis, enabled(stop_distance=1.0))

    for _, event in execution.stop_events.iterrows():
        assert execution.positions.loc[event["timestamp"], event["asset"]] == 0.0


def test_exposure_drops_to_zero_on_a_stopped_bar(analysis, always_long):
    """The risk-model claim: a stop removes exposure, it does not cap it."""
    execution = run_backtest(always_long, analysis, enabled(stop_distance=1.0))

    for _, event in execution.stop_events.iterrows():
        assert execution.notionals.loc[event["timestamp"], event["asset"]] == 0.0


def test_stop_exits_are_named_in_the_trade_log(analysis, always_long):
    execution = run_backtest(always_long, analysis, enabled(stop_distance=1.0))

    assert set(execution.trades["reason"]) & {"stop_loss", "take_profit"}
    assert set(execution.trades["action"]) <= set(ACTIONS)


def test_the_trade_log_carries_the_stop_columns(analysis, always_long):
    execution = run_backtest(always_long, analysis, enabled(stop_distance=1.0))

    for column in (
        "stop_level",
        "take_profit_level",
        "stop_fill_price",
        "take_profit_fill_price",
        "gap_through",
        "slippage_applied",
        "breach_type",
    ):
        assert column in execution.trades.columns

    exits = execution.trades[execution.trades["reason"] == "stop_loss"]
    if not exits.empty:
        assert exits["stop_fill_price"].notna().all()
        assert exits["take_profit_fill_price"].isna().all()
        assert (exits["breach_type"] != "").all()


def test_metadata_records_the_whole_policy(analysis, always_long):
    execution = run_backtest(
        always_long,
        analysis,
        enabled(stop_distance=1.5, tp_priority=False, stop_mode="gap"),
    )
    assert execution.metadata["stops_enabled"] is True
    assert execution.metadata["stop_distance"] == 1.5
    assert execution.metadata["tp_priority"] is False
    assert execution.metadata["stop_mode"] == "gap"
    assert execution.metadata["stop_events"] == len(execution.stop_events)


def test_a_tighter_stop_exits_more_often(analysis, always_long):
    loose = run_backtest(always_long, analysis, enabled(stop_distance=6.0))
    tight = run_backtest(always_long, analysis, enabled(stop_distance=0.5))
    assert len(tight.stop_events) > len(loose.stop_events)


def test_percent_distance_mode_works_end_to_end(analysis, always_long):
    execution = run_backtest(
        always_long,
        analysis,
        enabled(
            stop_distance_mode="percent",
            stop_distance=0.02,
            tp_distance_mode="percent",
            tp_distance=0.04,
        ),
    )
    assert not execution.stop_events.empty


def test_fixed_distance_mode_works_end_to_end(analysis, always_long):
    execution = run_backtest(
        always_long,
        analysis,
        enabled(
            stop_distance_mode="fixed",
            stop_distance=2.0,
            tp_distance_mode="fixed",
            tp_distance=4.0,
        ),
    )
    assert not execution.stop_events.empty


def test_breach_mode_fills_on_the_level_even_after_a_gap(analysis, always_long):
    """The optimism of ``breach`` mode, visible in the event log.

    Note this is asserted per event rather than on the equity curve: a
    different fill changes the equity, which changes the next position size,
    which changes which bars breach at all. The modes cannot be ordered by
    final equity even though each individual fill can be.
    """
    execution = run_backtest(
        always_long, analysis, enabled(stop_distance=1.0, stop_mode="breach")
    )
    gapped = execution.stop_events[execution.stop_events["breach_type"] == "gap"]

    assert not gapped.empty, "the fixture must actually gap through a level"
    assert np.allclose(gapped["fill_price"], gapped["level"])


def test_gap_mode_fills_at_the_open_even_without_a_gap(analysis, always_long):
    execution = run_backtest(
        always_long, analysis, enabled(stop_distance=1.0, stop_mode="gap")
    )
    intrabar = execution.stop_events[
        execution.stop_events["breach_type"] == "intrabar"
    ]

    assert not intrabar.empty
    assert not np.allclose(intrabar["fill_price"], intrabar["level"])


def gapless_analysis(calendar, seed: int = 31):
    """Bars whose open equals the previous close: every breach is intrabar.

    With gaps present a ``tp_priority`` comparison is muddied, because a gap
    through a level is unambiguous by construction and the flag never sees it.
    """
    rng = np.random.default_rng(seed)
    blocks = []
    for ticker in ("AAA", "BBB"):
        close = 100 * np.exp(np.cumsum(rng.normal(0.0004, 0.008, BARS)))
        opens = np.r_[close[0], close[:-1]]  # open == previous close
        blocks.append(
            pd.DataFrame(
                {
                    "open": opens,
                    "high": np.maximum(opens, close) * 1.05,
                    "low": np.minimum(opens, close) * 0.95,
                    "close": close,
                    "volume": 1e6,
                    TICKER: ticker,
                },
                index=calendar,
            )
        )
    return build_analysis_frame(
        pd.concat(blocks).set_index(TICKER, append=True).sort_index(),
        interval="1d",
        config=PreprocessConfig(correlation_window=63),
    )


def test_tp_priority_reaches_the_engine(calendar):
    analysis = gapless_analysis(calendar)
    signals = signals_for(analysis)

    def run(priority):
        return run_backtest(
            signals,
            analysis,
            ExecutionConfig(
                stops=StopConfig(
                    enabled=True,
                    stop_distance=0.5,
                    tp_distance=0.5,
                    slippage_bps=0.0,
                    tp_priority=priority,
                )
            ),
        )

    optimistic, conservative = run(True), run(False)

    ambiguous = optimistic.stop_events[optimistic.stop_events["ambiguous"]]
    assert not ambiguous.empty, "the fixture must actually produce ambiguity"
    assert (ambiguous["kind"] == "take_profit").all()

    conservative_ambiguous = conservative.stop_events[
        conservative.stop_events["ambiguous"]
    ]
    assert (conservative_ambiguous["kind"] == "stop").all()

    # The optimistic reading finishes richer on identical data, which is
    # exactly why it is flagged rather than assumed harmless.
    assert optimistic.equity_curve.iloc[-1] > conservative.equity_curve.iloc[-1]


def test_slippage_reaches_the_engine(analysis, always_long):
    clean = run_backtest(always_long, analysis, enabled(stop_distance=1.0))
    costly = run_backtest(
        always_long, analysis, enabled(stop_distance=1.0, slippage_bps=50.0)
    )

    def stops_only(execution):
        events = execution.stop_events
        return events[events["kind"] == "stop"]

    assert not stops_only(clean).empty
    assert np.allclose(stops_only(clean)["slippage_applied"], 0.0)

    slipped = stops_only(costly)
    assert not slipped.empty
    assert (slipped["slippage_applied"] > 0).all()
    assert np.allclose(slipped["slippage_bps"], 50.0)
    # Adverse in both directions: the fill is always worse than the level.
    assert np.allclose(
        slipped["fill_price"],
        slipped["reference_price"] * (1 - 0.005 * np.sign(slipped["units_closed"])),
    )


def test_stops_are_skipped_without_an_atr_panel(calendar, caplog):
    """A close-only lake has no range to place an ATR level against."""
    rng = np.random.default_rng(2)
    closes = {
        t: 100 * np.exp(np.cumsum(rng.normal(0.0005, 0.01, BARS)))
        for t in ("AAA", "BBB")
    }
    tidy = market(calendar, closes)[["close"]].copy()
    analysis = build_analysis_frame(
        tidy, config=PreprocessConfig(correlation_window=63)
    )
    execution = run_backtest(signals_for(analysis), analysis, enabled())

    assert execution.stop_events.empty
    assert "no ATR panel" in caplog.text


def test_working_orders_describe_the_open_book(analysis, always_long):
    execution = run_backtest(always_long, analysis, enabled(stop_distance=3.0))
    orders = execution.working_orders()

    if not orders.empty:
        assert {"stop_order", "take_profit_order", "direction"} <= set(orders.columns)
        assert (orders["units"] != 0).all()


def test_stop_summary_groups_by_kind_and_type(analysis, always_long):
    execution = run_backtest(always_long, analysis, enabled(stop_distance=1.0))
    summary = execution.stop_summary()

    assert summary["events"].sum() == len(execution.stop_events)
    assert (summary["total_units_closed"] >= 0).all()


def test_execution_with_stops_is_deterministic(analysis, always_long):
    config = enabled(stop_distance=1.0)
    first = run_backtest(always_long, analysis, config)
    second = run_backtest(always_long, analysis, config)

    assert np.allclose(first.equity_curve, second.equity_curve)
    pd.testing.assert_frame_equal(first.stop_events, second.stop_events)


def test_empty_frame_stop_views_do_not_raise():
    from cfdinvest.execution import ExecutionFrame

    empty = ExecutionFrame()
    assert empty.stop_summary().empty
    assert empty.working_orders().empty


# --------------------------------------------------------------------------- #
# M255 the time stop's closes labelled as the signal's
# M256 a reason the engine emits with no translation
# --------------------------------------------------------------------------- #
def test_the_time_stop_says_it_was_the_time_stop():
    """M255: a wrong label, which is worse than a missing one.

    The time stop closes a position by zeroing it, and that fell through to
    ``"signal"`` -- so every close in a two-year run claimed to be the
    signal's doing. It was not a small share: 1,622 of 2,139, 75.8%, and
    they behave nothing like the ones the signal really made (44.3% win at
    -0.278% against 64.0% at +0.768%).

    "How often does the time stop fire" was unanswerable from the log that
    exists to answer it, and the answer looked like zero.
    """
    from cfdinvest.execution.engine import _trade_reason

    assert _trade_reason(False, False, None, 0, True) == "time_stop"
    assert _trade_reason(False, False, None, 0, False) == "signal"

    # And it never outranks a rule that acted first. A position stopped out
    # at the book level, or through its own price stop, was closed by that
    # -- the time stop only fires on one nothing else has closed.
    assert _trade_reason(True, False, None, 0, True) == "stop_out"
    breach = {"is_take_profit": [False]}
    assert _trade_reason(False, True, breach, 0, True) == "stop_loss"


def test_the_time_stop_is_labelled_in_a_real_run(tmp_path):
    """The seam, not the helper.

    Asserting on ``_trade_reason`` proves the branch works while leaving the
    call site untouched -- and the call site is where this was wrong for two
    years. So run the engine and read the log.
    """
    import dataclasses

    from cfdinvest.backtest import BacktestConfig, run_backtest_pipeline
    from cfdinvest.execution import run_backtest
    from cfdinvest.repository import DataRepository
    from tests.test_analytics import INTERVAL, TICKERS, build_lake

    config = build_lake(tmp_path / "data", bars=400)
    settings = BacktestConfig(tickers=list(TICKERS), interval=INTERVAL)
    settings = dataclasses.replace(
        settings,
        execution=dataclasses.replace(settings.execution, time_stop_days=1.0),
    )
    pipeline = run_backtest_pipeline(DataRepository(config), settings)
    result = run_backtest(pipeline.signals, pipeline.analysis, settings.execution)

    closes = result.trades[result.trades["action"] == "close"]
    assert len(closes), "no closes to label"
    assert "time_stop" in set(closes["reason"].astype(str)), (
        "a run with a one-day time stop produced no close attributed to it"
    )


def test_the_engine_emits_no_reason_the_dashboard_cannot_read():
    """M256: a reason with no translation.

    There is a test pinning the trade-log *actions* against the glossary and
    none that pinned the reasons, which is how ``time_stop`` could have
    shipped as a bare token on the page. Read out of the engine's own source
    rather than restated, so a reason added later is caught by this rather
    than by somebody noticing an odd word on a table.
    """
    import re
    from pathlib import Path

    from cfdinvest.ui import explain

    source = Path("cfdinvest/execution/engine.py").read_text(encoding="utf-8")
    body = source[source.index("def _trade_reason("):]
    body = body[: body.index("\ndef ")]
    emitted = set(re.findall(r'return\s+"([a-z_]+)"', body))
    # The ternary branch returns two names on one line.
    emitted |= set(re.findall(r'"([a-z_]+)"\s+if\s+.*else\s+"([a-z_]+)"', body)[0]) \
        if re.search(r'"[a-z_]+"\s+if\s+.*else\s+"[a-z_]+"', body) else set()

    assert emitted, "no reasons found; the parse is wrong, not the code"
    missing = sorted(emitted - set(explain.ACTIONS))
    assert not missing, f"the engine emits {missing} with no translation"
