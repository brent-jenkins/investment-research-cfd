"""The four component classifiers: volatility, trend, correlation, macro.

Constructed inputs with known answers, so a failure names the defect rather
than reporting that a number moved.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from cfdinvest.regimes import correlation as corr_module
from cfdinvest.regimes import macro as macro_module
from cfdinvest.regimes import trend as trend_module
from cfdinvest.regimes import volatility as vol_module
from cfdinvest.regimes.labels import UNKNOWN, bucket, cross_sectional, trailing_rank
from cfdinvest.schema import TICKER, TIMESTAMP


@pytest.fixture
def calendar() -> pd.DatetimeIndex:
    return pd.bdate_range("2022-01-03", periods=400, tz="UTC", name=TIMESTAMP)


def panel(calendar, values, tickers=("AAA", "BBB")) -> pd.DataFrame:
    """A panel with the same column repeated across tickers."""
    frame = pd.DataFrame({t: values for t in tickers}, index=calendar)
    frame.columns.name = TICKER
    return frame


# --------------------------------------------------------------------------- #
# labels helpers
# --------------------------------------------------------------------------- #
def test_bucket_boundaries_are_inclusive_below():
    scores = pd.Series([0.0, 0.33, 0.34, 0.67, 0.68, np.nan])
    labels = bucket(scores, (0.33, 0.67), ("low", "medium", "high"))
    assert list(labels) == ["low", "low", "medium", "medium", "high", UNKNOWN]


def test_bucket_rejects_mismatched_labels():
    with pytest.raises(ValueError, match="expected 3 labels"):
        bucket(pd.Series([0.5]), (0.3, 0.6), ("low", "high"))


def test_bucket_rejects_unsorted_thresholds():
    with pytest.raises(ValueError, match="must ascend"):
        bucket(pd.Series([0.5]), (0.6, 0.3), ("a", "b", "c"))


def test_trailing_rank_uses_only_the_past(calendar):
    """Truncating the future must not change any past rank.

    The definitive no-look-ahead property: ranks computed on the full series
    and on a prefix of it must agree everywhere they overlap.
    """
    values = pd.Series(np.r_[np.ones(200), np.full(200, 99.0)], index=calendar)

    full = trailing_rank(values, 100)
    prefix = trailing_rank(values.iloc[:200], 100)

    pd.testing.assert_series_equal(full.iloc[:200], prefix)
    # A flat stretch ranks mid (all values tied), not at the top.
    assert full.iloc[150] == pytest.approx(0.5, abs=0.02)
    # Once the level steps up, the new regime ranks at the top of its window.
    assert full.iloc[-1] == pytest.approx(0.5, abs=0.02)   # flat again, tied
    assert full.iloc[205] > 0.9


def test_trailing_rank_rejects_a_tiny_window(calendar):
    with pytest.raises(ValueError, match="window must be >= 2"):
        trailing_rank(pd.Series([1.0], index=calendar[:1]), 1)


def test_cross_sectional_median_ignores_one_outlier(calendar):
    frame = pd.DataFrame(
        {"A": np.ones(len(calendar)), "B": np.ones(len(calendar)), "C": np.full(len(calendar), 100.0)},
        index=calendar,
    )
    assert cross_sectional(frame, "median").iloc[0] == pytest.approx(1.0)
    assert cross_sectional(frame, "mean").iloc[0] == pytest.approx(34.0)


def test_cross_sectional_rejects_unknown_aggregation(calendar):
    with pytest.raises(ValueError, match="unknown aggregation"):
        cross_sectional(pd.DataFrame({"A": [1.0]}), "geometric")


# --------------------------------------------------------------------------- #
# Volatility regime
# --------------------------------------------------------------------------- #
def test_atr_percent_normalises_by_price(calendar):
    atr = panel(calendar, np.full(len(calendar), 4.0))
    close = panel(calendar, np.full(len(calendar), 200.0))

    assert np.allclose(vol_module.atr_percent(atr, close), 0.02)


def test_atr_percent_excludes_non_positive_prices(calendar):
    atr = panel(calendar, np.full(len(calendar), 4.0))
    close = panel(calendar, np.full(len(calendar), 200.0))
    close.iloc[5] = 0.0

    assert vol_module.atr_percent(atr, close).iloc[5].isna().all()


def test_rising_volatility_ends_high(calendar):
    rising = panel(calendar, np.linspace(0.05, 0.60, len(calendar)))
    regime = vol_module.classify_volatility(rising, window=252)

    assert regime.iloc[-1] == "high"
    assert regime.iloc[260] in {"high", "medium"}


def test_falling_volatility_ends_low(calendar):
    falling = panel(calendar, np.linspace(0.60, 0.05, len(calendar)))
    regime = vol_module.classify_volatility(falling, window=252)
    assert regime.iloc[-1] == "low"


def test_volatility_regime_is_relative_to_own_history(calendar):
    """A market that is always 60% vol is not permanently 'high'."""
    steady = panel(calendar, np.full(len(calendar), 0.60))
    regime = vol_module.classify_volatility(steady, window=252)

    # 60% annualised volatility would trip any absolute threshold, but a
    # market that is *always* there is not in a high-volatility regime: every
    # value ties, the rank sits mid-distribution, and the label is 'medium'.
    assert regime.iloc[-1] == "medium"


def test_volatility_is_unknown_during_warm_up(calendar):
    values = panel(calendar, np.linspace(0.1, 0.3, len(calendar)))
    regime = vol_module.classify_volatility(values, window=252)
    assert regime.iloc[0] == UNKNOWN


def test_atr_contributes_to_the_blend(calendar):
    """Realised calm plus violent ranges must not read as pure calm."""
    realized = panel(calendar, np.full(len(calendar), 0.10))
    atr_pct = panel(calendar, np.linspace(0.001, 0.20, len(calendar)))

    without = vol_module.volatility_pressure(realized, None, window=252)
    with_atr = vol_module.volatility_pressure(realized, atr_pct, window=252)

    # A constant series ties across its whole window, so its percentile rank
    # sits at ~0.5 rather than at the top. Blending in a steadily rising ATR
    # must therefore lift the score -- which is the whole point of using both.
    assert without.iloc[-1] == pytest.approx(0.5, abs=0.02)
    assert with_atr.iloc[-1] > without.iloc[-1]
    assert with_atr.iloc[-1] == pytest.approx(0.75, abs=0.02)


def test_missing_atr_falls_back_to_realised_alone(calendar):
    realized = panel(calendar, np.linspace(0.05, 0.5, len(calendar)))
    blended = vol_module.volatility_pressure(realized, None, window=252)
    assert blended.notna().any()


def test_volatility_of_empty_input():
    assert vol_module.classify_volatility(pd.DataFrame()).empty


def test_per_asset_volatility_ranks_each_name_separately(calendar):
    frame = pd.DataFrame(
        {
            "CALM": np.linspace(0.05, 0.06, len(calendar)),
            "WILD": np.linspace(0.40, 0.80, len(calendar)),
        },
        index=calendar,
    )
    per_asset = vol_module.asset_volatility_regime(frame, window=252)

    # Both rise within their own history, so both end high despite very
    # different absolute levels. That is the intended per-asset semantics.
    assert per_asset["CALM"].iloc[-1] == "high"
    assert per_asset["WILD"].iloc[-1] == "high"


# --------------------------------------------------------------------------- #
# Trend regime
# --------------------------------------------------------------------------- #
def test_moving_average_state_needs_agreement(calendar):
    rising = panel(calendar, np.linspace(100, 300, len(calendar)))
    state = trend_module.moving_average_state(rising, fast=10, slow=50)
    assert state.iloc[-1].eq(1.0).all()

    falling = panel(calendar, np.linspace(300, 100, len(calendar)))
    assert trend_module.moving_average_state(falling, 10, 50).iloc[-1].eq(-1.0).all()


def test_moving_average_rejects_a_fast_window_that_is_not_faster(calendar):
    with pytest.raises(ValueError, match="must be shorter"):
        trend_module.moving_average_state(panel(calendar, np.ones(len(calendar))), 50, 50)


def test_momentum_state_has_a_dead_band(calendar):
    tiny = panel(calendar, np.full(len(calendar), 0.001))
    assert trend_module.momentum_state(tiny, threshold=0.02).iloc[-1].eq(0.0).all()

    strong = panel(calendar, np.full(len(calendar), 0.05))
    assert trend_module.momentum_state(strong, threshold=0.02).iloc[-1].eq(1.0).all()


def test_momentum_rejects_a_negative_threshold(calendar):
    with pytest.raises(ValueError, match="threshold must be >= 0"):
        trend_module.momentum_state(panel(calendar, np.ones(len(calendar))), -0.1)


def test_trend_score_spans_minus_two_to_two(calendar):
    rising = panel(calendar, np.linspace(100, 300, len(calendar)))
    returns = panel(calendar, np.full(len(calendar), 0.05))

    score = trend_module.trend_score(rising, returns, fast=10, slow=50)
    assert score.iloc[-1].eq(2.0).all()


def test_uptrend_is_classified(calendar):
    rising = panel(calendar, np.linspace(100, 300, len(calendar)))
    returns = panel(calendar, np.full(len(calendar), 0.05))

    regime = trend_module.classify_trend(rising, returns, fast=10, slow=50)
    assert regime.iloc[-1] == "uptrend"


def test_downtrend_is_classified(calendar):
    falling = panel(calendar, np.linspace(300, 100, len(calendar)))
    returns = panel(calendar, np.full(len(calendar), -0.05))

    regime = trend_module.classify_trend(falling, returns, fast=10, slow=50)
    assert regime.iloc[-1] == "downtrend"


def test_choppy_market_is_sideways(calendar):
    oscillating = 200 + 5 * np.sin(np.arange(len(calendar)) * 0.5)
    prices = panel(calendar, oscillating)
    returns = panel(calendar, np.zeros(len(calendar)))

    regime = trend_module.classify_trend(prices, returns, fast=10, slow=50)
    assert regime.iloc[-1] == "sideways"


def test_breadth_requires_more_than_a_narrow_rally(calendar):
    """Two names up out of ten is not an uptrend."""
    labels = pd.DataFrame(
        {f"A{i}": ["uptrend" if i < 2 else "sideways"] * len(calendar) for i in range(10)},
        index=calendar,
    )
    breadth = trend_module.trend_breadth(labels)
    assert breadth.iloc[-1] == pytest.approx(0.2)

    regime = trend_module.classify_trend.__wrapped__ if False else None  # noqa: F841
    assert list(bucket(breadth, (-0.2, 0.2), trend_module.TREND_REGIMES))[-1] == (
        "sideways"
    )


def test_breadth_excludes_unknown_assets(calendar):
    labels = pd.DataFrame(
        {
            "A": ["uptrend"] * len(calendar),
            "B": ["downtrend"] * len(calendar),
            "C": [UNKNOWN] * len(calendar),
        },
        index=calendar,
    )
    breadth = trend_module.trend_breadth(labels)
    assert breadth.iloc[-1] == pytest.approx(0.0)   # 1 up, 1 down, C ignored


def test_trend_rejects_a_negative_breadth_threshold(calendar):
    prices = panel(calendar, np.linspace(100, 200, len(calendar)))
    with pytest.raises(ValueError, match="breadth_threshold must be >= 0"):
        trend_module.classify_trend(prices, prices, breadth_threshold=-0.1)


def test_trend_of_empty_input():
    assert trend_module.classify_trend(pd.DataFrame(), pd.DataFrame()).empty


# --------------------------------------------------------------------------- #
# Correlation regime
# --------------------------------------------------------------------------- #
def rolling_corr(calendar, returns, window=60):
    result = returns.rolling(window).corr()
    result.index.names = [TIMESTAMP, TICKER]
    return result


def test_cross_group_correlation_uses_only_the_cross_block(calendar):
    """Equity-equity correlation must not leak into the equity-bond reading."""
    base = np.sin(np.arange(len(calendar)) * 0.3) * 0.01
    returns = pd.DataFrame(
        {"SPY": base, "QQQ": base, "TLT": -base}, index=calendar
    )
    cross = corr_module.cross_group_correlation(
        rolling_corr(calendar, returns), ("SPY", "QQQ"), ("TLT",)
    )
    # SPY-TLT and QQQ-TLT are both -1; the +1 SPY-QQQ pair is excluded.
    assert cross.dropna().iloc[-1] == pytest.approx(-1.0)


def test_cross_group_ignores_absent_tickers(calendar):
    base = np.sin(np.arange(len(calendar)) * 0.3) * 0.01
    returns = pd.DataFrame({"SPY": base, "TLT": -base}, index=calendar)

    cross = corr_module.cross_group_correlation(
        rolling_corr(calendar, returns), ("SPY", "NOT_HELD"), ("TLT",)
    )
    assert cross.dropna().iloc[-1] == pytest.approx(-1.0)


def test_cross_group_is_nan_when_a_group_is_missing(calendar):
    base = np.sin(np.arange(len(calendar)) * 0.3) * 0.01
    returns = pd.DataFrame({"SPY": base, "QQQ": base}, index=calendar)

    cross = corr_module.cross_group_correlation(
        rolling_corr(calendar, returns), ("SPY",), ("TLT",)
    )
    assert cross.isna().all()


def test_overlapping_groups_do_not_count_the_diagonal(calendar):
    base = np.sin(np.arange(len(calendar)) * 0.3) * 0.01
    returns = pd.DataFrame({"SPY": base, "TLT": -base}, index=calendar)

    cross = corr_module.cross_group_correlation(
        rolling_corr(calendar, returns), ("SPY", "TLT"), ("TLT",)
    )
    # SPY-TLT only; the TLT-TLT 1.0 is masked out.
    assert cross.dropna().iloc[-1] == pytest.approx(-1.0)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(-0.6, "hedging"), (-0.1, "hedging"), (0.0, "decoupled"),
     (0.2, "decoupled"), (0.5, "co_moving")],
)
def test_hedge_states(calendar, value, expected):
    cross = pd.Series(np.full(len(calendar), value), index=calendar)
    assert corr_module.classify_hedge(cross).iloc[-1] == expected


def test_hedge_rejects_inverted_thresholds(calendar):
    cross = pd.Series(np.zeros(len(calendar)), index=calendar)
    with pytest.raises(ValueError, match="must not exceed"):
        corr_module.classify_hedge(cross, hedging_threshold=0.5, co_moving_threshold=0.1)


def test_low_average_correlation_is_diversified(calendar):
    average = pd.Series(np.full(len(calendar), 0.1), index=calendar)
    regime = corr_module.classify_correlation(average)
    assert (regime == "diversified").all()


def test_high_average_correlation_is_normal_without_a_flag(calendar):
    average = pd.Series(np.full(len(calendar), 0.6), index=calendar)
    regime = corr_module.classify_correlation(average)
    assert (regime == "normal").all()


def test_a_preprocessing_flag_forces_breakdown(calendar):
    average = pd.Series(np.full(len(calendar), 0.1), index=calendar)
    flags = pd.DataFrame(
        {"elevated": True, "spike": False, "breakdown": False}, index=calendar
    )
    regime = corr_module.classify_correlation(average, flags)
    assert (regime == "breakdown").all()


def test_an_inverted_hedge_forces_breakdown_despite_a_calm_average(calendar):
    """The 2022 case: broad correlation calm, the hedge that matters inverted."""
    average = pd.Series(np.full(len(calendar), 0.15), index=calendar)
    hedge = pd.Series(["co_moving"] * len(calendar), index=calendar)

    regime = corr_module.classify_correlation(average, None, hedge)
    assert (regime == "breakdown").all()


def test_a_working_hedge_does_not_force_breakdown(calendar):
    average = pd.Series(np.full(len(calendar), 0.15), index=calendar)
    hedge = pd.Series(["hedging"] * len(calendar), index=calendar)

    regime = corr_module.classify_correlation(average, None, hedge)
    assert (regime == "diversified").all()


def test_correlation_is_unknown_without_inputs(calendar):
    regime = corr_module.classify_correlation(index=calendar)
    assert (regime == UNKNOWN).all()


# --------------------------------------------------------------------------- #
# Macro regime
# --------------------------------------------------------------------------- #
@pytest.fixture
def macro_calendar() -> pd.DatetimeIndex:
    return pd.bdate_range("2018-01-01", periods=900, tz="UTC", name=TIMESTAMP)


def test_year_over_year_is_exact(macro_calendar):
    level = pd.Series(
        100 * np.exp(np.arange(len(macro_calendar)) * 0.0001), index=macro_calendar
    )
    yoy = macro_module.year_over_year(level, 252)
    assert yoy.iloc[-1] == pytest.approx(np.exp(252 * 0.0001) - 1, rel=1e-9)


def test_year_over_year_excludes_non_positive_denominators(macro_calendar):
    level = pd.Series(np.zeros(len(macro_calendar)), index=macro_calendar)
    assert macro_module.year_over_year(level, 252).isna().all()


def test_year_over_year_rejects_a_bad_lookback(macro_calendar):
    level = pd.Series(np.ones(len(macro_calendar)), index=macro_calendar)
    with pytest.raises(ValueError, match="periods must be >= 1"):
        macro_module.year_over_year(level, 0)


def test_shrinking_gdp_is_contraction(macro_calendar):
    gdp = pd.Series(
        20000 * np.exp(-0.0004 * np.arange(len(macro_calendar))), index=macro_calendar
    )
    assert macro_module.classify_growth(gdp).iloc[-1] == "contraction"


def test_accelerating_gdp_is_expansion(macro_calendar):
    growth_rate = np.linspace(0.0002, 0.0010, len(macro_calendar))
    gdp = pd.Series(20000 * np.exp(np.cumsum(growth_rate)), index=macro_calendar)
    assert macro_module.classify_growth(gdp).iloc[-1] == "expansion"


def test_decelerating_but_positive_gdp_is_slowdown(macro_calendar):
    """The distinction a level-only rule cannot make."""
    growth_rate = np.linspace(0.0012, 0.0002, len(macro_calendar))
    gdp = pd.Series(20000 * np.exp(np.cumsum(growth_rate)), index=macro_calendar)

    result = macro_module.classify_growth(gdp).iloc[-1]
    assert result == "slowdown"


def test_accelerating_prices_are_rising_inflation(macro_calendar):
    rate = np.linspace(0.00005, 0.0004, len(macro_calendar))
    cpi = pd.Series(250 * np.exp(np.cumsum(rate)), index=macro_calendar)
    assert macro_module.classify_inflation(cpi).iloc[-1] == "rising"


def test_decelerating_prices_are_falling_inflation(macro_calendar):
    rate = np.linspace(0.0004, 0.00005, len(macro_calendar))
    cpi = pd.Series(250 * np.exp(np.cumsum(rate)), index=macro_calendar)
    assert macro_module.classify_inflation(cpi).iloc[-1] == "falling"


def test_steady_prices_are_stable_inflation(macro_calendar):
    cpi = pd.Series(
        250 * np.exp(0.00008 * np.arange(len(macro_calendar))), index=macro_calendar
    )
    assert macro_module.classify_inflation(cpi).iloc[-1] == "stable"


def test_inflation_rejects_a_negative_threshold(macro_calendar):
    cpi = pd.Series(np.ones(len(macro_calendar)), index=macro_calendar)
    with pytest.raises(ValueError, match="threshold must be >= 0"):
        macro_module.classify_inflation(cpi, threshold=-0.1)


def test_falling_unemployment_is_tightening(macro_calendar):
    unrate = pd.Series(np.linspace(8.0, 3.5, len(macro_calendar)), index=macro_calendar)
    assert macro_module.classify_labor(unrate).iloc[-1] == "tightening"


def test_rising_unemployment_is_loosening(macro_calendar):
    unrate = pd.Series(np.linspace(3.5, 8.0, len(macro_calendar)), index=macro_calendar)
    assert macro_module.classify_labor(unrate).iloc[-1] == "loosening"


@pytest.mark.parametrize(
    ("growth", "inflation", "expected"),
    [
        ("expansion", "rising", "overheating"),
        ("expansion", "stable", "expansion"),
        ("expansion", "falling", "expansion"),
        ("slowdown", "rising", "stagflation"),
        ("slowdown", "stable", "slowdown"),
        ("slowdown", "falling", "slowdown"),
        ("contraction", "rising", "stagflation"),
        ("contraction", "stable", "contraction"),
        ("contraction", "falling", "contraction"),
        (UNKNOWN, "rising", UNKNOWN),
        ("expansion", UNKNOWN, UNKNOWN),
    ],
)
def test_macro_quadrant_is_total(calendar, growth, inflation, expected):
    growth_series = pd.Series([growth] * len(calendar), index=calendar)
    inflation_series = pd.Series([inflation] * len(calendar), index=calendar)

    combined = macro_module.combine_macro(growth_series, inflation_series)
    assert combined.iloc[-1] == expected


def test_classify_macro_end_to_end(macro_calendar):
    n = len(macro_calendar)
    macro = pd.DataFrame(
        {
            "GDPC1": 20000 * np.exp(np.cumsum(np.linspace(0.0012, 0.0001, n))),
            "CPIAUCSL": 250 * np.exp(np.cumsum(np.linspace(0.00005, 0.0005, n))),
            "UNRATE": np.linspace(3.5, 6.0, n),
        },
        index=macro_calendar,
    )
    result = macro_module.classify_macro(macro)

    assert list(result.columns) == [
        "growth_regime",
        "inflation_regime",
        "labor_regime",
        "macro_regime",
    ]
    assert result["growth_regime"].iloc[-1] == "slowdown"
    assert result["inflation_regime"].iloc[-1] == "rising"
    assert result["labor_regime"].iloc[-1] == "loosening"
    assert result["macro_regime"].iloc[-1] == "stagflation"


def test_missing_series_degrade_to_unknown(macro_calendar):
    macro = pd.DataFrame(
        {"DGS10": np.full(len(macro_calendar), 2.0)}, index=macro_calendar
    )
    result = macro_module.classify_macro(macro)

    assert (result["growth_regime"] == UNKNOWN).all()
    assert (result["macro_regime"] == UNKNOWN).all()


def test_empty_macro_panel_is_shaped_correctly():
    result = macro_module.classify_macro(pd.DataFrame())
    assert list(result.columns) == [
        "growth_regime",
        "inflation_regime",
        "labor_regime",
        "macro_regime",
    ]
    assert result.empty


def test_alternative_series_names_are_accepted(macro_calendar):
    """A lake with INDPRO but no GDPC1 still yields a growth reading."""
    n = len(macro_calendar)
    macro = pd.DataFrame(
        {"INDPRO": 100 * np.exp(np.cumsum(np.full(n, 0.0004)))}, index=macro_calendar
    )
    result = macro_module.classify_macro(macro)
    assert result["growth_regime"].iloc[-1] != UNKNOWN
