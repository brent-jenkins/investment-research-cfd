"""State machine, fills, financing and P&L decomposition."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from cfdinvest.execution import fills as fills_module
from cfdinvest.execution import financing as financing_module
from cfdinvest.execution import pnl as pnl_module
from cfdinvest.execution import state as state_module
from cfdinvest.execution.state import FLAT, LONG, SHORT, HysteresisThresholds
from cfdinvest.schema import TICKER, TIMESTAMP


def row(*values) -> np.ndarray:
    return np.asarray(values, dtype="float64")


def directions(*values) -> np.ndarray:
    return np.asarray(values, dtype=np.int8)


# --------------------------------------------------------------------------- #
# Hysteresis thresholds
# --------------------------------------------------------------------------- #
def test_default_band_is_valid():
    thresholds = HysteresisThresholds()
    assert thresholds.exit_size < thresholds.entry_size
    assert thresholds.exit_confidence < thresholds.entry_confidence


def test_inverted_size_band_is_rejected():
    with pytest.raises(ValueError, match="inverted band churns"):
        HysteresisThresholds(entry_size=0.05, exit_size=0.10)


def test_inverted_confidence_band_is_rejected():
    with pytest.raises(ValueError, match="exit_confidence"):
        HysteresisThresholds(entry_confidence=0.2, exit_confidence=0.5)


def test_equal_thresholds_are_permitted():
    """A zero-width band is degenerate but legal; it just churns."""
    HysteresisThresholds(entry_size=0.1, exit_size=0.1)


# --------------------------------------------------------------------------- #
# State machine
# --------------------------------------------------------------------------- #
def test_transition_table_is_exhaustive():
    table = state_module.transition_table()
    assert len(table) == 27          # 3 held x 3 desired x 3 bands
    assert table["action"].notna().all()
    assert set(table["action"]) <= set(state_module.ACTIONS)


def test_transition_table_is_deterministic():
    pd.testing.assert_frame_equal(
        state_module.transition_table(), state_module.transition_table()
    )


def test_hold_band_keeps_an_open_position():
    """The defining property: mid-band holds what is open."""
    thresholds = HysteresisThresholds()
    mid_size = (thresholds.exit_size + thresholds.entry_size) / 2
    mid_confidence = (thresholds.exit_confidence + thresholds.entry_confidence) / 2

    new = state_module.next_direction(
        directions(LONG), directions(LONG), row(mid_size), row(mid_confidence),
        thresholds,
    )
    assert new[0] == LONG


def test_hold_band_does_not_open_a_new_position():
    """The other half: the same signal that holds does not enter."""
    thresholds = HysteresisThresholds()
    mid_size = (thresholds.exit_size + thresholds.entry_size) / 2
    mid_confidence = (thresholds.exit_confidence + thresholds.entry_confidence) / 2

    new = state_module.next_direction(
        directions(FLAT), directions(LONG), row(mid_size), row(mid_confidence),
        thresholds,
    )
    assert new[0] == FLAT


def test_signal_below_exit_closes():
    new = state_module.next_direction(
        directions(LONG), directions(LONG), row(0.0), row(0.0), HysteresisThresholds()
    )
    assert new[0] == FLAT


def test_signal_above_entry_opens():
    new = state_module.next_direction(
        directions(FLAT), directions(LONG), row(1.0), row(1.0), HysteresisThresholds()
    )
    assert new[0] == LONG


def test_strong_opposing_signal_flips():
    new = state_module.next_direction(
        directions(LONG), directions(SHORT), row(1.0), row(1.0), HysteresisThresholds()
    )
    assert new[0] == SHORT


def test_weak_opposing_signal_closes_rather_than_flips():
    thresholds = HysteresisThresholds()
    mid = (thresholds.exit_size + thresholds.entry_size) / 2
    mid_confidence = (thresholds.exit_confidence + thresholds.entry_confidence) / 2

    new = state_module.next_direction(
        directions(LONG), directions(SHORT), row(mid), row(mid_confidence), thresholds
    )
    assert new[0] == FLAT


def test_weak_opposing_signal_can_be_ignored_instead():
    thresholds = HysteresisThresholds(exit_on_opposing_signal=False)
    mid = (thresholds.exit_size + thresholds.entry_size) / 2
    mid_confidence = (thresholds.exit_confidence + thresholds.entry_confidence) / 2

    new = state_module.next_direction(
        directions(LONG), directions(SHORT), row(mid), row(mid_confidence), thresholds
    )
    assert new[0] == LONG


def test_flat_signal_always_closes():
    new = state_module.next_direction(
        directions(LONG, SHORT), directions(FLAT, FLAT), row(1.0, 1.0),
        row(1.0, 1.0), HysteresisThresholds(),
    )
    assert list(new) == [FLAT, FLAT]


def test_confidence_alone_can_close_a_position():
    """Size can be strong while confidence has collapsed."""
    new = state_module.next_direction(
        directions(LONG), directions(LONG), row(1.0), row(0.0), HysteresisThresholds()
    )
    assert new[0] == FLAT


def test_state_machine_is_vectorised_across_assets():
    new = state_module.next_direction(
        directions(FLAT, LONG, SHORT, LONG),
        directions(LONG, FLAT, LONG, LONG),
        row(1.0, 1.0, 1.0, 0.0),
        row(1.0, 1.0, 1.0, 0.0),
        HysteresisThresholds(),
    )
    assert list(new) == [LONG, FLAT, LONG, FLAT]


def test_desired_direction_encoding():
    codes = state_module.desired_direction(
        np.array(["long", "short", "flat", None, "nonsense"], dtype=object)
    )
    assert list(codes) == [LONG, SHORT, FLAT, FLAT, FLAT]


@pytest.mark.parametrize(
    ("held", "new", "action"),
    [
        (FLAT, LONG, "open_long"),
        (FLAT, SHORT, "open_short"),
        (LONG, FLAT, "close"),
        (SHORT, FLAT, "close"),
        (LONG, SHORT, "flip_long_to_short"),
        (SHORT, LONG, "flip_short_to_long"),
        (LONG, LONG, "hold"),
        (FLAT, FLAT, "hold"),
    ],
)
def test_action_classification(held, new, action):
    result = state_module.classify_actions(
        directions(held), directions(new), np.array([False])
    )
    assert result[0] == action


def test_resize_is_reported_separately():
    result = state_module.classify_actions(
        directions(LONG), directions(LONG), np.array([True])
    )
    assert result[0] == "resize"


def test_needs_resize_uses_relative_drift():
    current = row(100.0, 100.0, 100.0)
    target = row(110.0, 130.0, 100.0)

    result = state_module.needs_resize(current, target, threshold=0.25)
    assert list(result) == [False, True, False]


def test_needs_resize_from_flat_always_true():
    assert state_module.needs_resize(row(0.0), row(1.0), threshold=0.25)[0]


def test_needs_resize_rejects_a_negative_threshold():
    with pytest.raises(ValueError, match="threshold must be >= 0"):
        state_module.needs_resize(row(1.0), row(1.0), threshold=-0.1)


# --------------------------------------------------------------------------- #
# Fills
# --------------------------------------------------------------------------- #
@pytest.fixture
def calendar() -> pd.DatetimeIndex:
    return pd.bdate_range("2024-01-01", periods=6, tz="UTC", name=TIMESTAMP)


def test_execution_lag_delays_the_signal(calendar):
    frame = pd.DataFrame({"A": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]}, index=calendar)
    shifted = fills_module.shift_for_execution(frame, lag=1)

    assert pd.isna(shifted["A"].iloc[0])
    assert shifted["A"].iloc[1] == 1.0
    assert shifted["A"].iloc[-1] == 5.0


def test_zero_lag_is_permitted_but_is_look_ahead(calendar):
    frame = pd.DataFrame({"A": [1.0] * 6}, index=calendar)
    pd.testing.assert_frame_equal(fills_module.shift_for_execution(frame, 0), frame)


def test_negative_lag_is_refused(calendar):
    frame = pd.DataFrame({"A": [1.0] * 6}, index=calendar)
    with pytest.raises(ValueError, match="trades on the future"):
        fills_module.shift_for_execution(frame, -1)


def test_fill_mode_open(calendar):
    prices = {
        "open": pd.DataFrame({"A": [10.0] * 6}, index=calendar),
        "high": pd.DataFrame({"A": [12.0] * 6}, index=calendar),
        "low": pd.DataFrame({"A": [8.0] * 6}, index=calendar),
        "close": pd.DataFrame({"A": [11.0] * 6}, index=calendar),
    }
    assert fills_module.fill_prices(prices, "open")["A"].iloc[0] == 10.0
    assert fills_module.fill_prices(prices, "mid")["A"].iloc[0] == 10.0
    assert fills_module.fill_prices(prices, "close")["A"].iloc[0] == 11.0


def test_unknown_fill_mode_is_refused(calendar):
    with pytest.raises(ValueError, match="unknown fill mode"):
        fills_module.fill_prices({}, "vwap")


def test_mid_fills_require_high_and_low(calendar):
    with pytest.raises(ValueError, match="need high and low"):
        fills_module.fill_prices(
            {"close": pd.DataFrame({"A": [1.0] * 6}, index=calendar)}, "mid"
        )


def test_open_fills_fall_back_to_close(calendar):
    """A close-only lake still executes, struck at the close."""
    prices = {"close": pd.DataFrame({"A": [11.0] * 6}, index=calendar)}
    assert fills_module.fill_prices(prices, "open")["A"].iloc[0] == 11.0


def test_spread_resolution_order():
    columns = pd.Index(["SPY", "GLD", "WEIRD"], name=TICKER)
    spreads = fills_module.resolve_spreads(
        columns,
        asset_classes={"SPY": "equity", "GLD": "commodity"},
        per_asset_bps={"SPY": 100.0},
    )
    assert spreads["SPY"] == pytest.approx(0.01)      # explicit override wins
    assert spreads["GLD"] == pytest.approx(0.0004)    # commodity default
    assert spreads["WEIRD"] == pytest.approx(0.0005)  # fallback


def test_fills_are_always_adverse():
    reference = row(100.0, 100.0)
    traded = row(10.0, -10.0)          # a buy and a sell
    spread = row(0.002, 0.002)

    filled = fills_module.effective_fill_price(reference, traded, spread, 0.0)

    assert filled[0] > 100.0     # buys pay up
    assert filled[1] < 100.0     # sells get hit


def test_transaction_costs_scale_with_traded_notional():
    spread_cost, slippage = fills_module.transaction_costs(
        row(10.0), row(100.0), row(0.002), 0.001
    )
    assert spread_cost[0] == pytest.approx(10 * 100 * 0.001)
    assert slippage[0] == pytest.approx(10 * 100 * 0.001)


def test_holding_costs_nothing():
    spread_cost, slippage = fills_module.transaction_costs(
        row(0.0), row(100.0), row(0.002), 0.001
    )
    assert spread_cost[0] == 0.0
    assert slippage[0] == 0.0


def test_costs_are_never_negative():
    spread_cost, slippage = fills_module.transaction_costs(
        row(-50.0), row(100.0), row(0.002), 0.001
    )
    assert spread_cost[0] > 0
    assert slippage[0] > 0


# --------------------------------------------------------------------------- #
# Financing
# --------------------------------------------------------------------------- #
def test_rate_pair_from_benchmark_and_markup():
    rates = financing_module.financing_rates(benchmark=0.05, markup=0.03)
    assert rates.long_rate == pytest.approx(0.08)
    assert rates.short_rate == pytest.approx(-0.02)


def test_shorts_earn_when_the_benchmark_exceeds_the_markup():
    rates = financing_module.financing_rates(benchmark=0.05, markup=0.03)
    assert rates.short_rate < 0, "a short should be paid carry at these rates"


def test_shorts_pay_when_rates_are_low():
    rates = financing_module.financing_rates(benchmark=0.001, markup=0.03)
    assert rates.short_rate > 0


def test_negative_markup_is_refused():
    with pytest.raises(ValueError, match="markup must be >= 0"):
        financing_module.financing_rates(markup=-0.01)


def test_weekend_counts_three_nights():
    index = pd.DatetimeIndex(
        ["2024-01-04", "2024-01-05", "2024-01-08"], tz="UTC"
    )  # Thu, Fri, Mon
    nights = financing_module.nights_between(index)
    assert list(nights) == [0.0, 1.0, 3.0]


def test_first_bar_accrues_nothing():
    index = pd.bdate_range("2024-01-01", periods=5, tz="UTC")
    assert financing_module.nights_between(index)[0] == 0.0


def test_intraday_bars_accrue_no_nights():
    index = pd.date_range("2024-01-02 09:00", periods=5, freq="h", tz="UTC")
    assert (financing_module.nights_between(index) == 0.0).all()


def test_long_financing_is_a_cost():
    cost = financing_module.financing_cost(
        row(100.0), row(50.0), nights=1, long_rate=0.08, short_rate=-0.02
    )
    assert cost[0] == pytest.approx(100 * 50 * 0.08 / 365)
    assert cost[0] > 0


def test_short_financing_can_be_a_credit():
    cost = financing_module.financing_cost(
        row(-100.0), row(50.0), nights=1, long_rate=0.08, short_rate=-0.02
    )
    assert cost[0] < 0


def test_financing_uses_absolute_notional():
    """A short borrows the instrument just as a long borrows the cash."""
    long_cost = financing_module.financing_cost(
        row(100.0), row(50.0), 1, 0.08, 0.08
    )
    short_cost = financing_module.financing_cost(
        row(-100.0), row(50.0), 1, 0.08, 0.08
    )
    assert long_cost[0] == pytest.approx(short_cost[0])


def test_flat_positions_finance_nothing():
    cost = financing_module.financing_cost(row(0.0), row(50.0), 1, 0.08, -0.02)
    assert cost[0] == 0.0


def test_zero_nights_costs_nothing():
    cost = financing_module.financing_cost(row(100.0), row(50.0), 0, 0.08, -0.02)
    assert cost[0] == 0.0


def test_three_nights_costs_three_times_one():
    one = financing_module.financing_cost(row(100.0), row(50.0), 1, 0.08, -0.02)
    three = financing_module.financing_cost(row(100.0), row(50.0), 3, 0.08, -0.02)
    assert three[0] == pytest.approx(3 * one[0])


def test_per_asset_financing_overrides():
    columns = pd.Index(["SPY", "ODD"], name=TICKER)
    long_rates, short_rates = financing_module.resolve_rates(
        columns, benchmark=0.05, markup=0.03, per_asset={"ODD": (0.20, 0.15)}
    )
    assert long_rates[0] == pytest.approx(0.08)
    assert long_rates[1] == pytest.approx(0.20)
    assert short_rates[1] == pytest.approx(0.15)


# --------------------------------------------------------------------------- #
# P&L
# --------------------------------------------------------------------------- #
def test_components_sum_to_total():
    result = pnl_module.bar_pnl(
        previous_units=row(10.0),
        new_units=row(5.0),
        previous_close=row(100.0),
        fill_price=row(101.0),
        close=row(102.0),
        spread_cost=row(1.0),
        slippage_cost=row(0.5),
        financing=row(0.25),
    )
    parts = sum(result[name] for name in pnl_module.PNL_COMPONENTS)
    assert np.allclose(parts, result["total"])


def test_gap_belongs_to_the_old_position():
    """A position opened at the fill must not earn the overnight gap."""
    result = pnl_module.bar_pnl(
        previous_units=row(0.0),
        new_units=row(10.0),
        previous_close=row(100.0),
        fill_price=row(110.0),      # a 10-point gap up
        close=row(110.0),
        spread_cost=row(0.0), slippage_cost=row(0.0), financing=row(0.0),
    )
    assert result["gap"][0] == 0.0
    assert result["intraday"][0] == 0.0


def test_gap_is_earned_by_a_position_held_through_it():
    result = pnl_module.bar_pnl(
        previous_units=row(10.0),
        new_units=row(10.0),
        previous_close=row(100.0),
        fill_price=row(110.0),
        close=row(110.0),
        spread_cost=row(0.0), slippage_cost=row(0.0), financing=row(0.0),
    )
    assert result["gap"][0] == pytest.approx(100.0)


def test_intraday_is_earned_by_the_new_position():
    result = pnl_module.bar_pnl(
        previous_units=row(0.0),
        new_units=row(10.0),
        previous_close=row(100.0),
        fill_price=row(100.0),
        close=row(105.0),
        spread_cost=row(0.0), slippage_cost=row(0.0), financing=row(0.0),
    )
    assert result["intraday"][0] == pytest.approx(50.0)


def test_costs_and_financing_reduce_pnl():
    result = pnl_module.bar_pnl(
        row(0.0), row(0.0), row(100.0), row(100.0), row(100.0),
        spread_cost=row(3.0), slippage_cost=row(2.0), financing=row(1.0),
    )
    assert result["total"][0] == pytest.approx(-6.0)


def test_realized_on_a_full_close():
    realized = pnl_module.realized_on_close(
        previous_units=row(10.0), new_units=row(0.0),
        entry_price=row(100.0), fill_price=row(110.0),
    )
    assert realized[0] == pytest.approx(100.0)


def test_realized_on_a_partial_reduction():
    realized = pnl_module.realized_on_close(
        previous_units=row(10.0), new_units=row(4.0),
        entry_price=row(100.0), fill_price=row(110.0),
    )
    assert realized[0] == pytest.approx(60.0)


def test_adding_to_a_position_realizes_nothing():
    realized = pnl_module.realized_on_close(
        previous_units=row(4.0), new_units=row(10.0),
        entry_price=row(100.0), fill_price=row(110.0),
    )
    assert realized[0] == 0.0


def test_a_flip_realizes_the_whole_original_position():
    realized = pnl_module.realized_on_close(
        previous_units=row(10.0), new_units=row(-3.0),
        entry_price=row(100.0), fill_price=row(110.0),
    )
    assert realized[0] == pytest.approx(100.0)


def test_short_realizes_positively_when_price_falls():
    realized = pnl_module.realized_on_close(
        previous_units=row(-10.0), new_units=row(0.0),
        entry_price=row(100.0), fill_price=row(90.0),
    )
    assert realized[0] == pytest.approx(100.0)


def test_equity_curve_accumulates():
    index = pd.bdate_range("2024-01-01", periods=4, tz="UTC")
    total = pd.Series([10.0, -5.0, 20.0, 0.0], index=index)

    equity = pnl_module.equity_curve(total, 1000.0)
    assert list(equity) == [1010.0, 1005.0, 1025.0, 1025.0]


def test_equity_curve_rejects_non_positive_start():
    index = pd.bdate_range("2024-01-01", periods=2, tz="UTC")
    with pytest.raises(ValueError, match="initial_equity"):
        pnl_module.equity_curve(pd.Series([1.0, 2.0], index=index), 0.0)


def test_drawdown_is_causal():
    """The peak is a running maximum, usable inside the loop."""
    index = pd.bdate_range("2024-01-01", periods=4, tz="UTC")
    equity = pd.Series([100.0, 120.0, 90.0, 110.0], index=index)

    curve = pnl_module.drawdown(equity)
    assert list(curve["peak"]) == [100.0, 120.0, 120.0, 120.0]
    assert curve["drawdown"].min() == pytest.approx(-30.0)
    assert curve["drawdown_fraction"].min() == pytest.approx(-0.25)


def test_drawdown_of_an_empty_curve():
    assert pnl_module.drawdown(pd.Series(dtype="float64")).empty
