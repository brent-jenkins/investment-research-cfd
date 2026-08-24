"""Volatility estimators, checked against closed-form values."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from cfdinvest.preprocessing.volatility import (
    annualize,
    average_true_range,
    realized_volatility,
    rolling_volatility,
    true_range,
)
from cfdinvest.schema import TICKER, TIMESTAMP


@pytest.fixture
def calendar() -> pd.DatetimeIndex:
    return pd.bdate_range("2024-01-01", periods=30, tz="UTC", name=TIMESTAMP)


@pytest.fixture
def returns(calendar) -> pd.DataFrame:
    """Two deterministic series: a constant and an alternating one."""
    n = len(calendar)
    frame = pd.DataFrame(
        {
            "FLAT": np.full(n, 0.01),                       # zero dispersion
            "ALT": np.where(np.arange(n) % 2 == 0, 0.02, -0.02),
        },
        index=calendar,
    )
    frame.columns.name = TICKER
    return frame


# --------------------------------------------------------------------------- #
# Rolling standard deviation
# --------------------------------------------------------------------------- #
def test_constant_returns_have_zero_volatility(returns):
    result = rolling_volatility(returns, window=5, annualized=False)
    assert result["FLAT"].dropna().eq(0.0).all()


def test_alternating_returns_match_the_closed_form(returns):
    """+2%/-2% alternating has sample stdev 0.02 * sqrt(n/(n-1))."""
    result = rolling_volatility(returns, window=4, annualized=False)
    expected = 0.02 * np.sqrt(4 / 3)
    assert result["ALT"].dropna().iloc[0] == pytest.approx(expected)


def test_annualisation_is_sqrt_of_time(returns):
    raw = rolling_volatility(returns, window=5, annualized=False)
    annual = rolling_volatility(returns, window=5, annualized=True, periods=252)

    pd.testing.assert_frame_equal(annual, raw * np.sqrt(252))


def test_periods_are_inferred_from_the_index(returns):
    inferred = rolling_volatility(returns, window=5)
    explicit = rolling_volatility(returns, window=5, periods=252)
    pd.testing.assert_frame_equal(inferred, explicit)


def test_min_periods_defaults_to_the_full_window(returns):
    result = rolling_volatility(returns, window=10, annualized=False)
    assert result.iloc[:9].isna().all().all()
    assert result.iloc[9].notna().all()


def test_min_periods_can_be_relaxed(returns):
    result = rolling_volatility(returns, window=10, min_periods=3, annualized=False)
    assert result.iloc[:2].isna().all().all()
    assert result.iloc[2].notna().all()


def test_window_below_two_is_rejected(returns):
    with pytest.raises(ValueError, match="window must be >= 2"):
        rolling_volatility(returns, window=1)


def test_volatility_does_not_mutate_input(returns):
    before = returns.copy()
    rolling_volatility(returns, window=5)
    pd.testing.assert_frame_equal(returns, before)


def test_annualize_helper(returns):
    pd.testing.assert_frame_equal(annualize(returns, 4), returns * 2.0)


# --------------------------------------------------------------------------- #
# Realized volatility
# --------------------------------------------------------------------------- #
def test_realized_volatility_is_rms(returns):
    result = realized_volatility(returns, window=4, annualized=False)
    # Alternating +/-2%: RMS is exactly 0.02, with no mean subtraction.
    assert result["ALT"].dropna().iloc[0] == pytest.approx(0.02)


def test_realized_counts_drift_as_risk(returns):
    """A constant +1% return has zero stdev but non-zero realised vol."""
    stdev = rolling_volatility(returns, window=5, annualized=False)["FLAT"]
    realized = realized_volatility(returns, window=5, annualized=False)["FLAT"]

    assert stdev.dropna().iloc[0] == pytest.approx(0.0)
    assert realized.dropna().iloc[0] == pytest.approx(0.01)


def test_realized_volatility_is_never_negative(returns):
    result = realized_volatility(returns, window=5)
    assert (result.dropna() >= 0).all().all()


def test_realized_window_must_be_positive(returns):
    with pytest.raises(ValueError, match="window must be >= 1"):
        realized_volatility(returns, window=0)


# --------------------------------------------------------------------------- #
# True range / ATR
# --------------------------------------------------------------------------- #
@pytest.fixture
def bars(calendar):
    """High/low/close where the true range is easy to reason about."""
    n = len(calendar)
    close = pd.DataFrame({"AAA": np.full(n, 100.0)}, index=calendar)
    high = close + 2.0
    low = close - 2.0
    return high, low, close


def test_true_range_uses_the_bar_range_when_there_is_no_gap(bars):
    high, low, close = bars
    result = true_range(high, low, close)["AAA"]
    assert result.dropna().eq(4.0).all()


def test_true_range_captures_an_overnight_gap(calendar):
    close = pd.DataFrame({"AAA": [100.0, 120.0] + [120.0] * (len(calendar) - 2)}, index=calendar)
    high = close + 1.0
    low = close - 1.0

    result = true_range(high, low, close)["AAA"]

    # Bar 1 gaps from a 100 close to a 119-121 range: 121 - 100 = 21, which
    # is far wider than the 2-point intrabar range.
    assert result.iloc[1] == pytest.approx(21.0)


def test_true_range_first_bar_falls_back_to_high_minus_low(bars):
    high, low, close = bars
    assert true_range(high, low, close)["AAA"].iloc[0] == pytest.approx(4.0)


def test_true_range_is_nan_where_the_bar_is_missing(calendar):
    close = pd.DataFrame({"AAA": np.full(len(calendar), 100.0)}, index=calendar)
    high = close + 1.0
    low = close - 1.0
    high.iloc[3] = np.nan

    assert pd.isna(true_range(high, low, close)["AAA"].iloc[3])


def test_atr_of_a_constant_range_equals_that_range(bars):
    high, low, close = bars
    atr = average_true_range(high, low, close, window=5)["AAA"]
    assert atr.dropna().iloc[-1] == pytest.approx(4.0)


def test_wilder_and_sma_agree_on_a_constant_series(bars):
    high, low, close = bars
    wilder = average_true_range(high, low, close, 5, method="wilder")["AAA"]
    sma = average_true_range(high, low, close, 5, method="sma")["AAA"]

    assert wilder.dropna().iloc[-1] == pytest.approx(sma.dropna().iloc[-1])


def test_wilder_smooths_a_shock_more_slowly_than_sma(calendar):
    close = pd.DataFrame({"AAA": np.full(len(calendar), 100.0)}, index=calendar)
    high, low = close + 1.0, close - 1.0
    high.iloc[15] = 150.0                     # one violent bar

    wilder = average_true_range(high, low, close, 10, method="wilder")["AAA"]
    sma = average_true_range(high, low, close, 10, method="sma")["AAA"]

    # The SMA jumps by shock/window immediately; Wilder's by shock/window too,
    # but then decays instead of dropping the bar abruptly out of the window.
    assert wilder.iloc[-1] > sma.iloc[-1]


def test_atr_is_in_price_units_not_percent(calendar):
    close = pd.DataFrame({"AAA": np.full(len(calendar), 1000.0)}, index=calendar)
    high, low = close + 20.0, close - 20.0

    atr = average_true_range(high, low, close, 5)["AAA"]
    assert atr.dropna().iloc[-1] == pytest.approx(40.0)


def test_atr_rejects_an_unknown_method(bars):
    high, low, close = bars
    with pytest.raises(ValueError, match="unknown ATR method"):
        average_true_range(high, low, close, method="exponential")


def test_atr_rejects_a_bad_window(bars):
    high, low, close = bars
    with pytest.raises(ValueError, match="window must be >= 1"):
        average_true_range(high, low, close, window=0)


def test_atr_handles_multiple_assets(calendar):
    close = pd.DataFrame(
        {"AAA": np.full(len(calendar), 100.0), "BBB": np.full(len(calendar), 50.0)},
        index=calendar,
    )
    high = close * 1.02
    low = close * 0.98

    atr = average_true_range(high, low, close, 5)

    assert list(atr.columns) == ["AAA", "BBB"]
    assert atr["AAA"].dropna().iloc[-1] == pytest.approx(4.0)
    assert atr["BBB"].dropna().iloc[-1] == pytest.approx(2.0)


def test_atr_of_empty_input(calendar):
    empty = pd.DataFrame(index=calendar)
    assert average_true_range(empty, empty, empty).empty
