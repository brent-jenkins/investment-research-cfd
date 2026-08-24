"""RegimeFrame assembly, and the end-to-end path from an AnalysisFrame.

Scenarios are engineered so the expected regime is known in advance: a calm
bull market, an engineered crisis, and a stagflationary macro backdrop.
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from cfdinvest.preprocessing import PreprocessConfig, build_analysis_frame
from cfdinvest.regimes import (
    RegimeConfig,
    RegimeFrame,
    attach_regimes,
    classify_regimes,
)
from cfdinvest.regimes.combined import COMBINED_LABELS
from cfdinvest.regimes.correlation import CORRELATION_LABELS, HEDGE_LABELS
from cfdinvest.regimes.labels import UNKNOWN
from cfdinvest.regimes.macro import MACRO_LABELS
from cfdinvest.regimes.trend import TREND_LABELS
from cfdinvest.regimes.volatility import VOLATILITY_LABELS
from cfdinvest.schema import TICKER, TIMESTAMP

BARS = 800


@pytest.fixture
def calendar() -> pd.DatetimeIndex:
    return pd.bdate_range("2018-01-02", periods=BARS, tz="UTC", name=TIMESTAMP)


def market_from_returns(calendar, returns: dict[str, np.ndarray]) -> pd.DataFrame:
    """Build a tidy OHLCV frame from per-ticker return paths."""
    blocks = []
    for ticker, path in returns.items():
        close = 100 * np.exp(np.cumsum(path))
        blocks.append(
            pd.DataFrame(
                {
                    "open": close * 0.999,
                    "high": close * 1.006,
                    "low": close * 0.994,
                    "close": close,
                    "volume": 1e6,
                    TICKER: ticker,
                },
                index=calendar,
            )
        )
    return pd.concat(blocks).set_index(TICKER, append=True).sort_index()


def macro_long(calendar, **levels) -> pd.DataFrame:
    """Monthly macro observations with a realistic 40-day publication lag."""
    observations = pd.date_range(
        calendar[0] - pd.Timedelta(days=730), periods=60, freq="MS", tz="UTC",
        name=TIMESTAMP,
    )
    blocks = []
    for series_id, values in levels.items():
        blocks.append(
            pd.DataFrame(
                {
                    "value": np.asarray(values, dtype="float64"),
                    "publication_date": observations + pd.Timedelta(days=40),
                    "series_id": series_id,
                },
                index=observations,
            )
        )
    return pd.concat(blocks).sort_index()


@pytest.fixture
def calm_bull(calendar):
    """Steadily rising, low volatility, uncorrelated names."""
    rng = np.random.default_rng(5)
    returns = {
        t: rng.normal(0.0006, 0.004, BARS) for t in ("SPY", "QQQ", "EFA", "TLT")
    }
    return build_analysis_frame(
        market_from_returns(calendar, returns),
        interval="1d",
        config=PreprocessConfig(correlation_window=63),
    )


@pytest.fixture
def crisis_market(calendar):
    """A violent, correlated, falling market with an inverted bond hedge."""
    rng = np.random.default_rng(9)
    common = rng.normal(0, 1, BARS)
    scale = np.where(np.arange(BARS) < 500, 0.004, 0.035)
    drift = np.where(np.arange(BARS) < 500, 0.0006, -0.004)

    returns = {}
    for ticker in ("SPY", "QQQ", "EFA"):
        returns[ticker] = common * scale + drift + rng.normal(0, 0.002, BARS)
    # Bonds hedge while calm, then co-move once the crisis starts.
    bond_beta = np.where(np.arange(BARS) < 500, -0.6, 0.8)
    returns["TLT"] = bond_beta * common * scale + drift * 0.5

    return build_analysis_frame(
        market_from_returns(calendar, returns),
        interval="1d",
        config=PreprocessConfig(correlation_window=63),
    )


@pytest.fixture
def stagflation_frame(calendar):
    """Decelerating growth with accelerating prices."""
    rng = np.random.default_rng(3)
    returns = {t: rng.normal(0.0002, 0.005, BARS) for t in ("SPY", "TLT")}

    gdp = 20000 * np.exp(np.cumsum(np.linspace(0.006, 0.0005, 60)))
    cpi = 250 * np.exp(np.cumsum(np.linspace(0.0005, 0.009, 60)))
    unrate = np.linspace(3.5, 6.5, 60)

    return build_analysis_frame(
        market_from_returns(calendar, returns),
        macro_long(calendar, GDPC1=gdp, CPIAUCSL=cpi, UNRATE=unrate),
        interval="1d",
        config=PreprocessConfig(correlation_window=63),
    )


# --------------------------------------------------------------------------- #
# Structure
# --------------------------------------------------------------------------- #
def test_frame_exposes_the_specified_members(calm_bull):
    frame = classify_regimes(calm_bull)
    for member in (
        "volatility_regime",
        "trend_regime",
        "correlation_regime",
        "macro_regime",
        "combined_regime",
        "metadata",
    ):
        assert hasattr(frame, member)


def test_every_series_shares_the_calendar(calm_bull):
    frame = classify_regimes(calm_bull)
    calendar = calm_bull.calendar

    for series in (
        frame.volatility_regime,
        frame.trend_regime,
        frame.correlation_regime,
        frame.macro_regime,
        frame.combined_regime,
    ):
        assert series.index.equals(calendar)
    assert frame.components.index.equals(calendar)


@pytest.mark.parametrize(
    ("attribute", "labels"),
    [
        ("volatility_regime", VOLATILITY_LABELS),
        ("trend_regime", TREND_LABELS),
        ("correlation_regime", CORRELATION_LABELS),
        ("macro_regime", MACRO_LABELS),
        ("combined_regime", COMBINED_LABELS),
    ],
)
def test_each_component_is_categorical_over_its_declared_labels(
    calm_bull, attribute, labels
):
    frame = classify_regimes(calm_bull)
    series = getattr(frame, attribute)

    assert isinstance(series.dtype, pd.CategoricalDtype)
    assert list(series.cat.categories) == list(labels)


def test_no_label_escapes_its_declared_set(calm_bull):
    frame = classify_regimes(calm_bull)
    for column, labels in (
        ("volatility_regime", VOLATILITY_LABELS),
        ("trend_regime", TREND_LABELS),
        ("correlation_regime", CORRELATION_LABELS),
        ("macro_regime", MACRO_LABELS),
        ("combined_regime", COMBINED_LABELS),
    ):
        observed = set(frame.to_frame()[column].astype(str).unique())
        assert observed <= set(labels), column


def test_to_frame_has_the_five_headline_columns(calm_bull):
    frame = classify_regimes(calm_bull).to_frame()
    assert list(frame.columns) == [
        "volatility_regime",
        "trend_regime",
        "correlation_regime",
        "macro_regime",
        "combined_regime",
    ]


def test_components_carry_the_sub_readings(calm_bull):
    components = classify_regimes(calm_bull).components
    for column in (
        "growth_regime",
        "inflation_regime",
        "labor_regime",
        "hedge_regime",
        "rule",
        "ambiguous",
        "ambiguity_reason",
    ):
        assert column in components.columns


def test_metadata_records_provenance(calm_bull):
    metadata = classify_regimes(calm_bull).metadata

    assert metadata["bars"] == len(calm_bull.calendar)
    assert metadata["decision_table_size"] == 384
    assert metadata["macro_lookback"] == 252
    assert metadata["calendar_start"] == calm_bull.calendar.min()
    assert "config" in metadata


def test_ambiguity_accessor(calm_bull):
    frame = classify_regimes(calm_bull)
    assert frame.ambiguous.dtype == np.bool_
    assert len(frame.ambiguous) == len(frame.index)


# --------------------------------------------------------------------------- #
# Scenario behaviour
# --------------------------------------------------------------------------- #
def test_calm_bull_is_never_a_crisis(calm_bull):
    frame = classify_regimes(calm_bull)
    assert not (frame.combined_regime == "crisis").any()


def test_calm_bull_spends_time_constructive(calm_bull):
    frame = classify_regimes(calm_bull)
    constructive = frame.combined_regime.isin(["risk_on", "expansion", "neutral"])
    assert constructive.mean() > 0.4


def test_engineered_crisis_is_detected(crisis_market):
    frame = classify_regimes(crisis_market)
    crisis = frame.combined_regime == "crisis"

    assert crisis.any(), "the engineered crash must produce at least one crisis bar"
    # And only after the break, never during the calm first half.
    first_crisis = frame.index[crisis][0]
    assert first_crisis > frame.index[480]


def test_crisis_period_is_volatile_and_falling(crisis_market):
    frame = classify_regimes(crisis_market)
    crisis_bars = frame.to_frame()[frame.combined_regime == "crisis"]

    assert (crisis_bars["volatility_regime"] == "high").all()
    assert (crisis_bars["trend_regime"] == "downtrend").all()
    assert (crisis_bars["correlation_regime"] == "breakdown").all()


def test_bond_hedge_inverts_during_the_crisis(crisis_market):
    frame = classify_regimes(crisis_market)
    hedge = frame.components["hedge_regime"].astype(str)

    assert hedge.iloc[400] == "hedging"
    assert hedge.iloc[700] == "co_moving"


def test_stagflation_backdrop_is_labelled(stagflation_frame):
    frame = classify_regimes(stagflation_frame)

    assert (frame.macro_regime == "stagflation").any()
    # And the macro rule overrides whatever the tape is doing.
    stag = frame.combined_regime[frame.macro_regime == "stagflation"]
    assert (stag == "stagflation").all()


def test_stagflation_components_agree(stagflation_frame):
    frame = classify_regimes(stagflation_frame)
    components = frame.components[frame.macro_regime == "stagflation"]

    assert (components["inflation_regime"].astype(str) == "rising").all()
    assert set(components["growth_regime"].astype(str)) <= {"slowdown", "contraction"}


def test_warm_up_period_is_unknown(calm_bull):
    frame = classify_regimes(calm_bull)
    assert frame.combined_regime.iloc[0] == UNKNOWN


# --------------------------------------------------------------------------- #
# Degraded inputs
# --------------------------------------------------------------------------- #
def test_no_macro_still_classifies_market_state(calm_bull):
    frame = classify_regimes(calm_bull)

    assert (frame.macro_regime == UNKNOWN).all()
    assert (frame.volatility_regime != UNKNOWN).any()
    assert (frame.combined_regime != UNKNOWN).any()


def test_single_asset_universe_degrades_correlation_only(calendar):
    rng = np.random.default_rng(1)
    single = build_analysis_frame(
        market_from_returns(calendar, {"ONLY": rng.normal(0.0005, 0.005, BARS)}),
        config=PreprocessConfig(correlation_window=63),
    )
    frame = classify_regimes(single)

    assert (frame.correlation_regime == UNKNOWN).all()
    assert (frame.volatility_regime != UNKNOWN).any()
    assert (frame.trend_regime != UNKNOWN).any()


def test_empty_analysis_frame_yields_an_empty_regime_frame():
    empty = build_analysis_frame(pd.DataFrame())
    frame = classify_regimes(empty)

    assert len(frame.index) == 0
    assert frame.to_frame().empty


def test_close_only_universe_still_classifies(calendar):
    rng = np.random.default_rng(2)
    tidy = market_from_returns(
        calendar, {t: rng.normal(0.0004, 0.005, BARS) for t in ("AAA", "BBB")}
    )
    closes_only = tidy[["close"]].copy()

    frame = classify_regimes(
        build_analysis_frame(closes_only, config=PreprocessConfig(correlation_window=63))
    )
    # No ATR available, so volatility rests on realised alone -- still decided.
    assert (frame.volatility_regime != UNKNOWN).any()


# --------------------------------------------------------------------------- #
# Determinism and purity
# --------------------------------------------------------------------------- #
def test_classification_is_deterministic(calm_bull):
    first = classify_regimes(calm_bull)
    second = classify_regimes(calm_bull)
    pd.testing.assert_frame_equal(first.to_frame(), second.to_frame())


def test_classification_does_not_mutate_the_analysis_frame(calm_bull):
    before_close = calm_bull.close.copy()
    before_regimes = calm_bull.regimes.copy()

    classify_regimes(calm_bull)

    pd.testing.assert_frame_equal(calm_bull.close, before_close)
    pd.testing.assert_frame_equal(calm_bull.regimes, before_regimes)


def test_ticker_order_does_not_change_the_result(calendar):
    rng = np.random.default_rng(4)
    paths = {t: rng.normal(0.0005, 0.005, BARS) for t in ("AAA", "BBB", "CCC")}
    forward = market_from_returns(calendar, paths)
    reverse = market_from_returns(calendar, dict(reversed(list(paths.items()))))

    config = PreprocessConfig(correlation_window=63)
    lhs = classify_regimes(build_analysis_frame(forward, config=config))
    rhs = classify_regimes(build_analysis_frame(reverse, config=config))

    pd.testing.assert_frame_equal(lhs.to_frame(), rhs.to_frame())


def test_regimes_are_not_recomputed_from_the_future(crisis_market):
    """Classifying a prefix must give the same labels over that prefix.

    The end-to-end no-look-ahead check: every window in every component is
    trailing, so truncating the future cannot change the past.
    """
    prefix_length = 600
    full = classify_regimes(crisis_market)

    truncated = replace(
        crisis_market,
        prices={k: v.iloc[:prefix_length] for k, v in crisis_market.prices.items()},
        returns={k: v.iloc[:prefix_length] for k, v in crisis_market.returns.items()},
        volatility={
            k: v.iloc[:prefix_length] for k, v in crisis_market.volatility.items()
        },
        correlations={
            "rolling": crisis_market.correlations["rolling"].loc[
                : crisis_market.calendar[prefix_length - 1]
            ],
            "average": crisis_market.correlations["average"].iloc[:prefix_length],
            "breakdown": crisis_market.correlations["breakdown"].iloc[:prefix_length],
        },
        macro=crisis_market.macro.iloc[:prefix_length],
        regimes=crisis_market.regimes.iloc[:prefix_length],
    )
    partial = classify_regimes(truncated)

    # Volatility and trend are pure trailing windows over the same inputs.
    pd.testing.assert_series_equal(
        full.volatility_regime.iloc[:prefix_length],
        partial.volatility_regime,
        check_freq=False,
    )
    pd.testing.assert_series_equal(
        full.trend_regime.iloc[:prefix_length], partial.trend_regime, check_freq=False
    )


# --------------------------------------------------------------------------- #
# Views
# --------------------------------------------------------------------------- #
def test_summary_counts_every_label(calm_bull):
    summary = classify_regimes(calm_bull).summary()

    assert set(summary["component"]) == {
        "volatility_regime",
        "trend_regime",
        "correlation_regime",
        "macro_regime",
        "combined_regime",
    }
    # Shares within one component sum to 1.
    totals = summary.groupby("component")["share"].sum()
    assert np.allclose(totals, 1.0)


def test_summary_includes_unobserved_labels(calm_bull):
    """A regime that never occurred is worth seeing -- it flags a bad threshold."""
    summary = classify_regimes(calm_bull).summary()
    combined = summary[summary["component"] == "combined_regime"]
    assert len(combined) == len(COMBINED_LABELS)


def test_transitions_are_contiguous_runs(calm_bull):
    runs = classify_regimes(calm_bull).transitions()

    assert list(runs.columns) == ["regime", "start", "end", "bars"]
    assert runs["bars"].sum() == len(calm_bull.calendar)
    # Adjacent runs must differ, or they would not be separate runs.
    assert (runs["regime"] != runs["regime"].shift()).all()


def test_transitions_of_another_component(calm_bull):
    runs = classify_regimes(calm_bull).transitions("volatility_regime")
    assert runs["bars"].sum() == len(calm_bull.calendar)


def test_transitions_of_an_empty_frame():
    frame = classify_regimes(build_analysis_frame(pd.DataFrame()))
    assert frame.transitions().empty


# --------------------------------------------------------------------------- #
# attach_regimes
# --------------------------------------------------------------------------- #
def test_attach_fills_the_preprocessing_placeholder(calm_bull):
    regimes = classify_regimes(calm_bull)
    attached = attach_regimes(calm_bull, regimes)

    assert list(attached.regimes.columns) == list(regimes.to_frame().columns)
    assert attached.regimes.index.equals(calm_bull.calendar)


def test_attach_returns_a_copy_and_leaves_the_original_alone(calm_bull):
    regimes = classify_regimes(calm_bull)
    attached = attach_regimes(calm_bull, regimes)

    assert calm_bull.regimes.shape[1] == 0     # placeholder untouched
    assert attached is not calm_bull
    pd.testing.assert_frame_equal(attached.close, calm_bull.close)


def test_attach_preserves_every_other_panel(calm_bull):
    attached = attach_regimes(calm_bull, classify_regimes(calm_bull))

    assert set(attached.prices) == set(calm_bull.prices)
    assert attached.metadata == calm_bull.metadata
