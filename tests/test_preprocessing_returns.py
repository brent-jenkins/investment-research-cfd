"""Return calculations: correctness against hand-computed values, and the
NaN discipline that stops a gap becoming a fabricated zero return."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from cfdinvest.preprocessing.returns import (
    cumulative_returns,
    intraday_returns,
    log_returns,
    overnight_returns,
    rolling_returns,
    simple_returns,
)
from cfdinvest.schema import TICKER, TIMESTAMP


@pytest.fixture
def calendar() -> pd.DatetimeIndex:
    return pd.bdate_range("2024-01-01", periods=6, tz="UTC", name=TIMESTAMP)


@pytest.fixture
def prices(calendar) -> pd.DataFrame:
    """Two assets with exactly computable moves."""
    frame = pd.DataFrame(
        {
            "AAA": [100.0, 110.0, 99.0, 99.0, 108.9, 108.9],
            "BBB": [50.0, 50.0, 25.0, 50.0, 100.0, 50.0],
        },
        index=calendar,
    )
    frame.columns.name = TICKER
    return frame


# --------------------------------------------------------------------------- #
# Simple returns
# --------------------------------------------------------------------------- #
def test_simple_returns_are_exact(prices):
    result = simple_returns(prices)

    assert pd.isna(result["AAA"].iloc[0])
    assert result["AAA"].iloc[1] == pytest.approx(0.10)      # 100 -> 110
    assert result["AAA"].iloc[2] == pytest.approx(-0.10)     # 110 -> 99
    assert result["BBB"].iloc[2] == pytest.approx(-0.50)     # 50 -> 25
    assert result["BBB"].iloc[3] == pytest.approx(1.00)      # 25 -> 50


def test_simple_returns_preserve_shape_and_labels(prices):
    result = simple_returns(prices)
    assert result.shape == prices.shape
    assert result.index.equals(prices.index)
    assert list(result.columns) == list(prices.columns)


def test_multi_period_simple_returns(prices):
    result = simple_returns(prices, periods=2)
    assert result["AAA"].iloc[2] == pytest.approx(-0.01)     # 100 -> 99
    assert result["AAA"].iloc[:2].isna().all()


def test_gap_does_not_become_a_zero_return(calendar):
    """A missing bar must yield NaN, never an invented flat return."""
    prices = pd.DataFrame(
        {"AAA": [100.0, np.nan, 110.0, 110.0, np.nan, 121.0]}, index=calendar
    )
    result = simple_returns(prices)["AAA"]

    assert pd.isna(result.iloc[1])
    assert pd.isna(result.iloc[2])          # no valid previous price
    assert result.iloc[3] == pytest.approx(0.0)
    assert pd.isna(result.iloc[4])


def test_non_positive_prices_are_excluded(calendar):
    prices = pd.DataFrame({"AAA": [100.0, 0.0, -5.0, 110.0, 121.0, 121.0]}, index=calendar)
    result = simple_returns(prices)["AAA"]

    assert result.isna().iloc[:4].all()
    assert result.iloc[4] == pytest.approx(0.10)


def test_returns_do_not_mutate_input(prices):
    before = prices.copy()
    simple_returns(prices)
    pd.testing.assert_frame_equal(prices, before)


def test_empty_input(calendar):
    empty = pd.DataFrame(index=calendar)
    assert simple_returns(empty).empty


# --------------------------------------------------------------------------- #
# Log returns
# --------------------------------------------------------------------------- #
def test_log_returns_are_exact(prices):
    result = log_returns(prices)
    assert result["AAA"].iloc[1] == pytest.approx(np.log(1.10))
    assert result["BBB"].iloc[2] == pytest.approx(np.log(0.5))


def test_log_returns_are_additive_across_time(prices):
    """The property that makes log returns worth having."""
    one_step = log_returns(prices)["AAA"]
    two_step = log_returns(prices, periods=2)["AAA"]

    assert two_step.iloc[2] == pytest.approx(one_step.iloc[1] + one_step.iloc[2])


def test_log_and_simple_agree_for_small_moves(calendar):
    prices = pd.DataFrame({"AAA": [100.0, 100.1, 100.2, 100.3, 100.4, 100.5]}, index=calendar)
    simple = simple_returns(prices)["AAA"].dropna()
    logged = log_returns(prices)["AAA"].dropna()

    assert np.allclose(simple, logged, atol=1e-5)


def test_log_returns_never_produce_infinity(calendar):
    prices = pd.DataFrame({"AAA": [100.0, 0.0, 110.0, 110.0, 110.0, 110.0]}, index=calendar)
    result = log_returns(prices)["AAA"]
    assert not np.isinf(result.to_numpy()).any()


# --------------------------------------------------------------------------- #
# Rolling returns
# --------------------------------------------------------------------------- #
def test_rolling_returns_measure_the_window(prices):
    result = rolling_returns(prices, window=3)
    # AAA: 100 -> 99 over three bars.
    assert result["AAA"].iloc[3] == pytest.approx(-0.01)
    assert result["AAA"].iloc[:3].isna().all()


def test_rolling_returns_reject_a_bad_window(prices):
    with pytest.raises(ValueError, match="window must be >= 1"):
        rolling_returns(prices, window=0)


def test_rolling_window_of_one_equals_simple(prices):
    pd.testing.assert_frame_equal(rolling_returns(prices, 1), simple_returns(prices))


# --------------------------------------------------------------------------- #
# Overnight / intraday
# --------------------------------------------------------------------------- #
def test_overnight_returns_measure_the_gap(calendar):
    opens = pd.DataFrame({"AAA": [100.0, 105.0, 99.0, 99.0, 99.0, 99.0]}, index=calendar)
    closes = pd.DataFrame({"AAA": [100.0, 110.0, 99.0, 99.0, 99.0, 99.0]}, index=calendar)

    result = overnight_returns(opens, closes)["AAA"]

    assert pd.isna(result.iloc[0])
    assert result.iloc[1] == pytest.approx(0.05)     # open 105 vs prior close 100
    assert result.iloc[2] == pytest.approx(-0.10)    # open 99 vs prior close 110


def test_overnight_and_intraday_compose_to_the_full_move(calendar):
    """(1+overnight)(1+intraday) - 1 == close-to-close return."""
    opens = pd.DataFrame({"AAA": [100.0, 103.0, 101.0, 99.0, 104.0, 107.0]}, index=calendar)
    closes = pd.DataFrame({"AAA": [102.0, 101.0, 100.0, 105.0, 106.0, 110.0]}, index=calendar)

    gap = overnight_returns(opens, closes)["AAA"]
    session = intraday_returns(opens, closes)["AAA"]
    total = simple_returns(closes)["AAA"]

    composed = (1 + gap) * (1 + session) - 1
    pd.testing.assert_series_equal(
        composed.dropna(), total.dropna(), check_names=False
    )


def test_overnight_aligns_mismatched_columns(calendar):
    opens = pd.DataFrame({"AAA": [1.0] * 6, "BBB": [1.0] * 6}, index=calendar)
    closes = pd.DataFrame({"AAA": [1.0] * 6, "CCC": [1.0] * 6}, index=calendar)

    result = overnight_returns(opens, closes)
    assert list(result.columns) == ["AAA"]


def test_overnight_of_empty_input(calendar):
    empty = pd.DataFrame(index=calendar)
    assert overnight_returns(empty, empty).empty


# --------------------------------------------------------------------------- #
# Cumulative
# --------------------------------------------------------------------------- #
def test_cumulative_returns_compound(calendar):
    returns = pd.DataFrame({"AAA": [np.nan, 0.10, 0.10, 0.0, 0.0, 0.0]}, index=calendar)
    result = cumulative_returns(returns)["AAA"]

    assert result.iloc[0] == pytest.approx(0.0)
    assert result.iloc[2] == pytest.approx(0.21)     # 1.1 * 1.1 - 1


def test_cumulative_from_log_returns_matches_simple(prices):
    simple_path = cumulative_returns(simple_returns(prices))
    log_path = cumulative_returns(log_returns(prices), log=True)

    pd.testing.assert_frame_equal(simple_path, log_path, atol=1e-12)


def test_cumulative_reconstructs_the_price_path(prices):
    """The end-to-end invariant: compounding returns rebuilds the prices."""
    path = cumulative_returns(simple_returns(prices))
    rebuilt = prices.iloc[0] * (1 + path)

    pd.testing.assert_frame_equal(rebuilt, prices, atol=1e-9)
