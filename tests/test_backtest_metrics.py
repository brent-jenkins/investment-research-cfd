"""Metric definitions, checked against hand-computed answers.

Every metric here has several defensible definitions. These tests pin the
one this codebase uses, so a future change to a convention is a visible
decision rather than a silent shift in every reported number.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from cfdinvest.backtest.metrics import (
    MetricsConfig,
    annualised_volatility,
    attribute_financing,
    average_r_multiple,
    cagr,
    calmar_ratio,
    equity_returns,
    expectancy,
    holding_time_distribution,
    max_drawdown,
    round_trips,
    sharpe_ratio,
    sortino_ratio,
    total_return,
    trade_metrics,
    win_rate,
)
from cfdinvest.execution.engine import TRADE_COLUMNS


def curve(values, start="2020-01-01", freq="B") -> pd.Series:
    index = pd.date_range(start, periods=len(values), freq=freq, tz="UTC")
    return pd.Series(np.asarray(values, dtype="float64"), index=index, name="equity")


# --------------------------------------------------------------------------- #
# Equity curve
# --------------------------------------------------------------------------- #
def test_total_return_is_exact():
    assert total_return(curve([100.0, 110.0, 121.0])) == pytest.approx(0.21)


def test_total_return_of_a_wiped_out_account():
    assert np.isnan(total_return(curve([0.0, 10.0])))


def test_equity_returns_are_per_bar():
    returns = equity_returns(curve([100.0, 110.0, 99.0]))
    assert len(returns) == 2
    assert returns.iloc[0] == pytest.approx(0.10)
    assert returns.iloc[1] == pytest.approx(-0.10)


def test_equity_returns_guard_against_non_positive_equity():
    returns = equity_returns(curve([100.0, 0.0, 50.0]))
    assert pd.isna(returns.iloc[1])


def test_cagr_uses_the_calendar_span():
    """Two years of daily bars doubling: CAGR is sqrt(2) - 1, not 100%."""
    index = pd.date_range("2020-01-01", "2022-01-01", freq="D", tz="UTC")
    equity = pd.Series(np.linspace(100.0, 200.0, len(index)), index=index)

    assert cagr(equity) == pytest.approx(np.sqrt(2) - 1, rel=0.02)


def test_cagr_of_a_partial_year_is_annualised_up():
    index = pd.date_range("2020-01-01", periods=91, freq="D", tz="UTC")
    equity = pd.Series(np.linspace(100.0, 110.0, len(index)), index=index)

    # Roughly a quarter at +10% annualises to about +46%.
    assert cagr(equity) == pytest.approx(0.46, abs=0.05)


def test_cagr_of_too_short_a_period():
    assert np.isnan(cagr(curve([100.0, 101.0], freq="h")))


def test_max_drawdown_measures_peak_to_trough():
    result = max_drawdown(curve([100.0, 120.0, 90.0, 110.0]))

    assert result["max_drawdown"] == pytest.approx(-30.0)
    assert result["max_drawdown_fraction"] == pytest.approx(-0.25)


def test_max_drawdown_counts_bars_under_water():
    result = max_drawdown(curve([100.0, 120.0, 110.0, 105.0, 130.0]))
    assert result["drawdown_bars"] == 2


def test_a_monotone_curve_has_no_drawdown():
    result = max_drawdown(curve([100.0, 110.0, 120.0]))
    assert result["max_drawdown"] == pytest.approx(0.0)
    assert result["drawdown_bars"] == 0


# --------------------------------------------------------------------------- #
# Risk-adjusted
# --------------------------------------------------------------------------- #
def test_sharpe_matches_the_definition():
    rng = np.random.default_rng(3)
    returns = rng.normal(0.0005, 0.01, 500)
    equity = curve(100.0 * np.cumprod(1 + np.r_[0.0, returns]))

    realised = equity_returns(equity).dropna()
    expected = realised.mean() / realised.std(ddof=1) * np.sqrt(252)

    assert sharpe_ratio(equity) == pytest.approx(expected)


def test_sharpe_subtracts_the_risk_free_rate():
    rng = np.random.default_rng(4)
    equity = curve(100.0 * np.cumprod(1 + np.r_[0.0, rng.normal(0.0006, 0.01, 400)]))

    with_rf = sharpe_ratio(equity, MetricsConfig(risk_free_rate=0.05))
    without = sharpe_ratio(equity, MetricsConfig(risk_free_rate=0.0))
    assert with_rf < without


def test_sharpe_of_a_flat_account_is_nan():
    assert np.isnan(sharpe_ratio(curve([100.0] * 50)))


def test_sortino_exceeds_sharpe_when_downside_is_scarce():
    """Mostly-up returns have less downside deviation than total deviation."""
    values = np.r_[np.full(40, 0.01), np.full(10, -0.002)]
    equity = curve(100.0 * np.cumprod(1 + np.r_[0.0, values]))

    assert sortino_ratio(equity) > sharpe_ratio(equity)


def test_sortino_divides_by_all_observations():
    """The convention: the downside deviation's denominator counts every bar."""
    values = np.r_[np.full(9, 0.01), [-0.02]]
    equity = curve(100.0 * np.cumprod(1 + np.r_[0.0, values]))

    returns = equity_returns(equity).dropna()
    downside = np.sqrt(np.mean(np.square(np.minimum(returns, 0.0))))
    expected = returns.mean() / downside * np.sqrt(252)

    assert sortino_ratio(equity) == pytest.approx(expected)


def test_sortino_with_no_downside_is_nan():
    assert np.isnan(sortino_ratio(curve(100.0 * np.cumprod(np.full(50, 1.001)))))


def test_calmar_is_cagr_over_drawdown():
    index = pd.date_range("2020-01-01", "2022-01-01", freq="D", tz="UTC")
    values = np.linspace(100.0, 200.0, len(index))
    values[len(values) // 2] = 80.0
    equity = pd.Series(values, index=index)

    expected = cagr(equity) / abs(max_drawdown(equity)["max_drawdown_fraction"])
    assert calmar_ratio(equity) == pytest.approx(expected)


def test_annualised_volatility_scales_by_root_time():
    rng = np.random.default_rng(9)
    returns = rng.normal(0, 0.01, 600)
    equity = curve(100.0 * np.cumprod(1 + np.r_[0.0, returns]))

    realised = equity_returns(equity).dropna()
    assert annualised_volatility(equity) == pytest.approx(
        realised.std(ddof=1) * np.sqrt(252)
    )


def test_metrics_of_a_single_bar_are_nan_not_an_error():
    single = curve([100.0])
    assert np.isnan(sharpe_ratio(single))
    assert np.isnan(sortino_ratio(single))
    assert np.isnan(total_return(single))


# --------------------------------------------------------------------------- #
# Round trips
# --------------------------------------------------------------------------- #
def trade_log(rows) -> pd.DataFrame:
    """Build a trade log with the execution layer's schema."""
    frame = pd.DataFrame(rows)
    for column in TRADE_COLUMNS:
        if column not in frame.columns:
            frame[column] = 0.0
    return frame[list(TRADE_COLUMNS)]


@pytest.fixture
def calendar() -> pd.DatetimeIndex:
    return pd.date_range("2020-01-01", periods=20, freq="B", tz="UTC")


def test_a_simple_round_trip_is_paired(calendar):
    trades = trade_log(
        [
            {"timestamp": calendar[0], "asset": "AAA", "action": "open_long",
             "units": 10.0, "spread_cost": 1.0, "realized_pnl": 0.0,
             "equity": 1000.0, "notional": 500.0},
            {"timestamp": calendar[5], "asset": "AAA", "action": "close",
             "units": 0.0, "spread_cost": 1.0, "realized_pnl": 50.0,
             "equity": 1050.0, "notional": 0.0},
        ]
    )
    trips = round_trips(trades, calendar)

    assert len(trips) == 1
    assert trips["direction"].iloc[0] == "long"
    assert trips["bars_held"].iloc[0] == 5
    assert trips["realized_pnl"].iloc[0] == pytest.approx(50.0)
    assert trips["costs"].iloc[0] == pytest.approx(2.0)
    assert trips["net_pnl"].iloc[0] == pytest.approx(48.0)


def test_a_flip_counts_as_two_trips(calendar):
    trades = trade_log(
        [
            {"timestamp": calendar[0], "asset": "AAA", "action": "open_long",
             "units": 10.0, "realized_pnl": 0.0, "equity": 1000.0},
            {"timestamp": calendar[4], "asset": "AAA", "action": "flip_long_to_short",
             "units": -8.0, "realized_pnl": 40.0, "equity": 1040.0},
            {"timestamp": calendar[9], "asset": "AAA", "action": "close",
             "units": 0.0, "realized_pnl": 20.0, "equity": 1060.0},
        ]
    )
    trips = round_trips(trades, calendar)

    assert len(trips) == 2
    assert list(trips["direction"]) == ["long", "short"]
    assert trips["realized_pnl"].iloc[0] == pytest.approx(40.0)
    assert trips["realized_pnl"].iloc[1] == pytest.approx(20.0)


def test_a_still_open_position_is_not_a_round_trip(calendar):
    trades = trade_log(
        [
            {"timestamp": calendar[0], "asset": "AAA", "action": "open_long",
             "units": 10.0, "equity": 1000.0},
        ]
    )
    assert round_trips(trades, calendar).empty


def test_resizes_accumulate_into_one_trip(calendar):
    trades = trade_log(
        [
            {"timestamp": calendar[0], "asset": "AAA", "action": "open_long",
             "units": 10.0, "spread_cost": 1.0, "equity": 1000.0},
            {"timestamp": calendar[2], "asset": "AAA", "action": "resize",
             "units": 15.0, "spread_cost": 0.5, "realized_pnl": 0.0,
             "equity": 1010.0},
            {"timestamp": calendar[6], "asset": "AAA", "action": "close",
             "units": 0.0, "spread_cost": 1.5, "realized_pnl": 70.0,
             "equity": 1080.0},
        ]
    )
    trips = round_trips(trades, calendar)

    assert len(trips) == 1
    assert trips["costs"].iloc[0] == pytest.approx(3.0)
    assert trips["bars_held"].iloc[0] == 6


def test_round_trips_of_an_empty_log():
    assert round_trips(pd.DataFrame(), None).empty


def test_separate_assets_do_not_interleave(calendar):
    trades = trade_log(
        [
            {"timestamp": calendar[0], "asset": "AAA", "action": "open_long",
             "units": 10.0, "equity": 1000.0},
            {"timestamp": calendar[1], "asset": "BBB", "action": "open_short",
             "units": -5.0, "equity": 1000.0},
            {"timestamp": calendar[3], "asset": "AAA", "action": "close",
             "units": 0.0, "realized_pnl": 10.0, "equity": 1010.0},
            {"timestamp": calendar[4], "asset": "BBB", "action": "close",
             "units": 0.0, "realized_pnl": 5.0, "equity": 1015.0},
        ]
    )
    trips = round_trips(trades, calendar)

    assert len(trips) == 2
    assert set(trips["asset"]) == {"AAA", "BBB"}
    assert trips.set_index("asset").loc["BBB", "direction"] == "short"


# --------------------------------------------------------------------------- #
# Financing attribution
# --------------------------------------------------------------------------- #
def test_attributed_financing_sums_to_the_total(calendar):
    trips = pd.DataFrame(
        {
            "asset": ["A", "B"],
            "entry_notional": [1000.0, 3000.0],
            "days_held": [10.0, 5.0],
            "realized_pnl": [100.0, 50.0],
            "costs": [2.0, 3.0],
            "net_pnl": [98.0, 47.0],
        }
    )
    attributed = attribute_financing(trips, total_financing=90.0)

    assert attributed["financing"].sum() == pytest.approx(90.0)


def test_financing_is_weighted_by_notional_days(calendar):
    trips = pd.DataFrame(
        {
            "entry_notional": [1000.0, 1000.0],
            "days_held": [10.0, 30.0],
            "realized_pnl": [0.0, 0.0],
            "costs": [0.0, 0.0],
            "net_pnl": [0.0, 0.0],
        }
    )
    attributed = attribute_financing(trips, total_financing=40.0)

    # Three times the notional-days pays three times the carry.
    assert attributed["financing"].iloc[0] == pytest.approx(10.0)
    assert attributed["financing"].iloc[1] == pytest.approx(30.0)


def test_financing_reduces_net_pnl(calendar):
    trips = pd.DataFrame(
        {
            "entry_notional": [1000.0],
            "days_held": [10.0],
            "realized_pnl": [100.0],
            "costs": [5.0],
            "net_pnl": [95.0],
        }
    )
    attributed = attribute_financing(trips, total_financing=20.0)
    assert attributed["net_pnl"].iloc[0] == pytest.approx(75.0)


def test_attribution_of_empty_trips():
    assert attribute_financing(pd.DataFrame(), 100.0).empty


# --------------------------------------------------------------------------- #
# Trade metrics
# --------------------------------------------------------------------------- #
@pytest.fixture
def trips() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "asset": ["A", "B", "C", "D"],
            "direction": ["long"] * 4,
            "net_pnl": [100.0, -50.0, 200.0, -25.0],
            "entry_equity": [10_000.0] * 4,
            "entry_notional": [5_000.0] * 4,
            "bars_held": [5.0, 3.0, 12.0, 2.0],
            "days_held": [7.0, 4.0, 17.0, 3.0],
        }
    )


def test_win_rate(trips):
    assert win_rate(trips) == pytest.approx(0.5)


def test_a_scratch_counts_as_a_loss():
    trips = pd.DataFrame({"net_pnl": [0.0, 10.0]})
    assert win_rate(trips) == pytest.approx(0.5)


def test_expectancy_decomposition(trips):
    result = expectancy(trips)

    assert result["expectancy"] == pytest.approx(56.25)
    assert result["average_win"] == pytest.approx(150.0)
    assert result["average_loss"] == pytest.approx(37.5)
    assert result["payoff_ratio"] == pytest.approx(4.0)
    assert result["profit_factor"] == pytest.approx(300.0 / 75.0)


def test_expectancy_of_no_trades():
    assert np.isnan(expectancy(pd.DataFrame())["expectancy"])


def test_r_multiple_on_the_equity_fraction_basis(trips):
    result = average_r_multiple(trips, MetricsConfig(risk_fraction=0.01))

    # 1R = 1% of 10,000 = 100.
    assert result["risk_per_trade"] == pytest.approx(100.0)
    assert result["average_r"] == pytest.approx(0.5625)
    assert result["r_basis"] == "equity_fraction"


def test_r_multiple_on_the_average_loss_basis(trips):
    result = average_r_multiple(trips, MetricsConfig(r_basis="average_loss"))

    assert result["risk_per_trade"] == pytest.approx(37.5)
    assert result["average_r"] == pytest.approx(56.25 / 37.5)


def test_r_multiple_on_the_notional_basis(trips):
    result = average_r_multiple(
        trips, MetricsConfig(r_basis="notional", stop_fraction=0.02)
    )
    assert result["risk_per_trade"] == pytest.approx(100.0)


def test_the_r_basis_changes_the_answer(trips):
    """Which basis you pick matters, so it is recorded alongside the number."""
    equity_basis = average_r_multiple(trips, MetricsConfig(r_basis="equity_fraction"))
    loss_basis = average_r_multiple(trips, MetricsConfig(r_basis="average_loss"))

    assert equity_basis["average_r"] != pytest.approx(loss_basis["average_r"])


def test_an_unknown_r_basis_is_refused(trips):
    with pytest.raises(ValueError, match="unknown r_basis"):
        average_r_multiple(trips, MetricsConfig(r_basis="vibes"))


def test_holding_time_distribution(trips):
    result = holding_time_distribution(trips)

    assert result["trades"] == 4
    assert result["median_bars_held"] == pytest.approx(4.0)
    assert result["min_bars_held"] == pytest.approx(2.0)
    assert result["max_bars_held"] == pytest.approx(12.0)


def test_holding_time_of_no_trades():
    assert holding_time_distribution(pd.DataFrame())["trades"] == 0


def test_trade_metrics_apply_financing_when_given(calendar):
    trades = trade_log(
        [
            {"timestamp": calendar[0], "asset": "AAA", "action": "open_long",
             "units": 10.0, "equity": 1000.0, "notional": 1000.0},
            {"timestamp": calendar[10], "asset": "AAA", "action": "close",
             "units": 0.0, "realized_pnl": 100.0, "equity": 1100.0},
        ]
    )
    gross = trade_metrics(trades, calendar)
    net = trade_metrics(trades, calendar, total_financing=40.0)

    assert gross["expectancy"] == pytest.approx(100.0)
    assert net["expectancy"] == pytest.approx(60.0)
