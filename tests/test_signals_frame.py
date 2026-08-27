"""SignalFrame assembly, invariants, and the end-to-end path.

The invariant tests matter most: the execution layer will consume these
panels directly, so a NaN direction or a flat cell carrying size would
become a phantom trade downstream.
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from cfdinvest.preprocessing import PreprocessConfig, build_analysis_frame
from cfdinvest.regimes import RegimeConfig, classify_regimes
from cfdinvest.signals import SignalConfig, SignalFrame, generate_signals
from cfdinvest.signals.combined import DIRECTIONS
from cfdinvest.schema import TICKER, TIMESTAMP

BARS = 800
TICKERS = ("SPY", "QQQ", "TLT", "GLD")


@pytest.fixture
def calendar() -> pd.DatetimeIndex:
    return pd.bdate_range("2018-01-02", periods=BARS, tz="UTC", name=TIMESTAMP)


def market_from_returns(calendar, returns: dict[str, np.ndarray]) -> pd.DataFrame:
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
    observations = pd.date_range(
        calendar[0] - pd.Timedelta(days=730), periods=60, freq="MS", tz="UTC",
        name=TIMESTAMP,
    )
    return pd.concat(
        [
            pd.DataFrame(
                {
                    "value": np.asarray(values, dtype="float64"),
                    "publication_date": observations + pd.Timedelta(days=40),
                    "series_id": series_id,
                },
                index=observations,
            )
            for series_id, values in levels.items()
        ]
    ).sort_index()


@pytest.fixture
def analysis(calendar):
    """A trending market with a violent, correlated second half."""
    rng = np.random.default_rng(17)
    common = rng.normal(0, 1, BARS)
    scale = np.where(np.arange(BARS) < 500, 0.006, 0.028)
    drift = np.where(np.arange(BARS) < 500, 0.0009, -0.003)

    returns = {}
    for ticker in ("SPY", "QQQ"):
        returns[ticker] = common * scale + drift + rng.normal(0, 0.003, BARS)
    returns["TLT"] = -0.5 * common * scale + 0.0002 + rng.normal(0, 0.002, BARS)
    returns["GLD"] = 0.2 * common * scale + 0.0004 + rng.normal(0, 0.004, BARS)

    gdp = 20000 * np.exp(np.cumsum(np.r_[np.full(45, 0.004), np.full(15, -0.005)]))
    cpi = 250 * np.exp(np.cumsum(np.r_[np.full(40, 0.001), np.full(20, 0.007)]))
    unrate = np.r_[np.linspace(5.0, 3.6, 45), np.linspace(3.6, 7.0, 15)]

    return build_analysis_frame(
        market_from_returns(calendar, returns),
        macro_long(calendar, GDPC1=gdp, CPIAUCSL=cpi, UNRATE=unrate),
        interval="1d",
        config=PreprocessConfig(correlation_window=63),
    )


@pytest.fixture
def regimes(analysis):
    return classify_regimes(analysis, RegimeConfig(volatility_window=252))


@pytest.fixture
def signals(analysis, regimes):
    return generate_signals(analysis, regimes, SignalConfig())


# --------------------------------------------------------------------------- #
# Structure
# --------------------------------------------------------------------------- #
def test_frame_exposes_the_specified_members(signals):
    for member in ("direction", "size", "confidence", "reason", "metadata"):
        assert hasattr(signals, member)


def test_every_panel_shares_the_calendar_and_universe(signals, analysis):
    for panel in (signals.direction, signals.size, signals.confidence, signals.reason):
        assert panel.index.equals(analysis.calendar)
        assert list(panel.columns) == list(analysis.close.columns)


def test_metadata_records_provenance(signals, analysis):
    metadata = signals.metadata

    assert metadata["bars"] == len(analysis.calendar)
    assert metadata["direction_table_size"] == 32
    assert metadata["regime_filtered"] is True
    assert set(metadata["components"]) == {"crossover", "breakout", "momentum"}
    assert metadata["tickers"] == tuple(analysis.close.columns)


def test_diagnostics_expose_the_intermediates(signals):
    for key in (
        "strength",
        "agreement",
        "raw_direction",
        "base_size",
        "size_multiplier",
        "long_multiplier",
        "short_multiplier",
        "rule",
    ):
        assert key in signals.diagnostics


# --------------------------------------------------------------------------- #
# Invariants the execution layer will rely on
# --------------------------------------------------------------------------- #
def test_direction_is_always_one_of_three_labels(signals):
    values = set(signals.direction.to_numpy().ravel())
    assert values <= set(DIRECTIONS)


def test_direction_has_no_missing_values(signals):
    assert signals.direction.notna().all().all()


def test_size_is_within_bounds_and_finite(signals):
    size = signals.size.to_numpy()
    assert np.isfinite(size).all()
    assert (size >= 0).all()
    assert (size <= signals.metadata["max_size"]).all()


def test_confidence_is_within_bounds_and_finite(signals):
    confidence = signals.confidence.to_numpy()
    assert np.isfinite(confidence).all()
    assert ((confidence >= 0) & (confidence <= 1)).all()


def test_flat_if_and_only_if_size_is_zero(signals):
    is_flat = signals.direction == "flat"
    is_zero = signals.size == 0.0
    assert (is_flat == is_zero).all().all()


def test_flat_cells_carry_no_confidence(signals):
    # Index with numpy, not a boolean DataFrame: the latter yields NaN for
    # unselected cells, and NaN == 0.0 is False.
    is_flat = (signals.direction == "flat").to_numpy()
    assert (signals.confidence.to_numpy()[is_flat] == 0.0).all()


def test_every_cell_carries_a_reason(signals):
    lengths = signals.reason.map(lambda value: len(str(value)))
    assert (lengths > 0).all().all()


def test_active_signals_clear_the_minimum_size(signals):
    active = (signals.direction != "flat").to_numpy()
    sizes = signals.size.to_numpy()[active]
    assert (sizes >= signals.metadata["min_size"]).all()


def test_signed_size_matches_direction(signals):
    signed = signals.signed_size.to_numpy()
    direction = signals.direction.to_numpy()

    assert (signed[direction == "long"] > 0).all()
    assert (signed[direction == "short"] < 0).all()
    assert (signed[direction == "flat"] == 0).all()
    assert np.allclose(np.abs(signed), signals.size.to_numpy())


# --------------------------------------------------------------------------- #
# Filter behaviour end to end
# --------------------------------------------------------------------------- #
def test_longs_appear_only_in_permitted_regimes(signals, regimes):
    longs = signals.direction == "long"
    permitted = regimes.combined_regime.isin(["risk_on", "expansion"])

    bars_with_longs = longs.any(axis=1)
    assert permitted[bars_with_longs].all()


def test_shorts_are_reachable(signals):
    """The regime that permits shorts requires a correlation breakdown, so a
    shared filter multiplier would make shorts structurally impossible."""
    assert (signals.direction == "short").to_numpy().any()


def test_shorts_can_be_suppressed_by_configuration(analysis, regimes):
    """Suppression can only remove shorts, never add them.

    Whether it removes any depends on whether shorts happen to coincide with
    a correlation breakdown in this fixture; the guarantee under test is the
    subset relation, which holds either way.
    """
    suppressed = generate_signals(
        analysis, regimes, SignalConfig(breakdown_applies_to_shorts=True)
    )
    permissive = generate_signals(analysis, regimes, SignalConfig())

    suppressed_shorts = (suppressed.direction == "short").to_numpy()
    permissive_shorts = (permissive.direction == "short").to_numpy()

    assert suppressed_shorts.sum() <= permissive_shorts.sum()
    # Every surviving short was also a short without the suppression.
    assert not (suppressed_shorts & ~permissive_shorts).any()


def test_equity_longs_are_blocked_in_a_contraction(analysis, regimes):
    signals = generate_signals(analysis, regimes, SignalConfig())
    contraction = regimes.components["growth_regime"].astype(str) == "contraction"

    if contraction.any():
        equity_longs = signals.direction.loc[contraction, ["SPY", "QQQ"]] == "long"
        assert not equity_longs.to_numpy().any()


def test_unfiltered_signals_are_more_active(analysis, regimes):
    filtered = generate_signals(analysis, regimes, SignalConfig())
    unfiltered = generate_signals(analysis, None, SignalConfig())

    assert unfiltered.metadata["active_bars"] > filtered.metadata["active_bars"]
    assert unfiltered.metadata["regime_filtered"] is False


def test_reasons_name_the_blocking_rule(signals):
    reasons = signals.reason.to_numpy().ravel()
    assert any("blocked_long" in str(value) for value in reasons)
    assert any("trend_" in str(value) for value in reasons)


def test_reasons_omit_the_redundant_block_flags(signals):
    """The rule name already says 'blocked_long'; repeating it is noise."""
    reasons = signals.reason.to_numpy().ravel()
    assert not any("regime_blocks_long" in str(value) for value in reasons)


# --------------------------------------------------------------------------- #
# Determinism and purity
# --------------------------------------------------------------------------- #
def test_generation_is_deterministic(analysis, regimes):
    first = generate_signals(analysis, regimes, SignalConfig())
    second = generate_signals(analysis, regimes, SignalConfig())

    pd.testing.assert_frame_equal(first.direction, second.direction)
    pd.testing.assert_frame_equal(first.size, second.size)
    pd.testing.assert_frame_equal(first.confidence, second.confidence)


def test_generation_does_not_mutate_its_inputs(analysis, regimes):
    close_before = analysis.close.copy()
    regimes_before = regimes.to_frame().copy()

    generate_signals(analysis, regimes, SignalConfig())

    pd.testing.assert_frame_equal(analysis.close, close_before)
    pd.testing.assert_frame_equal(regimes.to_frame(), regimes_before)


def test_no_look_ahead(analysis, regimes):
    """Truncating the future must not change the past.

    Every window in the signal path is trailing, so a prefix of the inputs
    must produce identical signals over that prefix.
    """
    cut = 600
    truncated_analysis = replace(
        analysis,
        prices={k: v.iloc[:cut] for k, v in analysis.prices.items()},
        returns={k: v.iloc[:cut] for k, v in analysis.returns.items()},
        volatility={k: v.iloc[:cut] for k, v in analysis.volatility.items()},
        correlations={},
        macro=analysis.macro.iloc[:cut],
        regimes=analysis.regimes.iloc[:cut],
    )
    full = generate_signals(analysis, None, SignalConfig())
    partial = generate_signals(truncated_analysis, None, SignalConfig())

    pd.testing.assert_frame_equal(
        full.direction.iloc[:cut], partial.direction, check_freq=False
    )
    pd.testing.assert_frame_equal(
        full.size.iloc[:cut], partial.size, check_freq=False
    )


def test_ticker_order_does_not_change_the_result(calendar):
    rng = np.random.default_rng(8)
    paths = {t: rng.normal(0.0006, 0.005, BARS) for t in ("AAA", "BBB", "CCC")}
    forward = market_from_returns(calendar, paths)
    reverse = market_from_returns(calendar, dict(reversed(list(paths.items()))))

    config = PreprocessConfig(correlation_window=63)
    lhs = generate_signals(build_analysis_frame(forward, config=config), None)
    rhs = generate_signals(build_analysis_frame(reverse, config=config), None)

    pd.testing.assert_frame_equal(lhs.direction, rhs.direction)
    pd.testing.assert_frame_equal(lhs.size, rhs.size)


# --------------------------------------------------------------------------- #
# Degraded inputs
# --------------------------------------------------------------------------- #
def test_empty_analysis_frame_yields_an_empty_signal_frame():
    signals = generate_signals(build_analysis_frame(pd.DataFrame()), None)
    assert signals.direction.empty
    assert signals.metadata["bars"] == 0


def test_close_only_universe_still_signals(calendar):
    rng = np.random.default_rng(6)
    tidy = market_from_returns(
        calendar, {t: rng.normal(0.0008, 0.004, BARS) for t in ("AAA", "BBB")}
    )
    closes_only = tidy[["close"]].copy()

    signals = generate_signals(
        build_analysis_frame(closes_only, config=PreprocessConfig(correlation_window=63)),
        None,
    )
    assert (signals.direction != "flat").to_numpy().any()
    assert "momentum" in signals.metadata["components"]


def test_no_regime_frame_runs_unfiltered(analysis):
    signals = generate_signals(analysis, None)
    assert signals.metadata["regime_filtered"] is False
    assert (signals.direction != "flat").to_numpy().any()


def test_gross_exposure_cap_is_respected(analysis, regimes):
    signals = generate_signals(
        analysis, regimes, SignalConfig(max_gross_exposure=0.5)
    )
    assert (signals.size.sum(axis=1) <= 0.5 + 1e-9).all()


# --------------------------------------------------------------------------- #
# Views
# --------------------------------------------------------------------------- #
def test_to_long_returns_only_active_signals_by_default(signals):
    tidy = signals.to_long()

    assert list(tidy.columns) == ["direction", "size", "confidence", "reason"]
    assert (tidy["direction"] != "flat").all()
    assert len(tidy) == signals.metadata["active_bars"]


def test_to_long_can_include_flat_cells(signals):
    tidy = signals.to_long(only_active=False)
    assert len(tidy) == signals.direction.size


def test_summary_counts_reconcile(signals):
    summary = signals.summary()
    totals = summary[["long_bars", "short_bars", "flat_bars"]].sum(axis=1)
    assert (totals == len(signals.index)).all()


def test_active_panel_matches_direction(signals):
    assert (signals.active() == (signals.direction != "flat")).all().all()


def test_reason_counts_are_ordered(signals):
    counts = signals.reason_counts(5)
    assert len(counts) <= 5
    assert counts.is_monotonic_decreasing


def test_empty_frame_views_do_not_raise():
    empty = SignalFrame()
    assert empty.to_long().empty
    assert empty.summary().empty
    assert empty.active().empty
    assert empty.signed_size.empty
    assert empty.reason_counts().empty


# --------------------------------------------------------------------------- #
# Holding the stance across a session open
#
# M235 the opening bar acting on a stance formed before the gap
# M236 the rule forcing flat instead of holding, so it closes positions
# M237 the rule applied to daily bars, where it is a lag on everything
# M238 the first bar of the sample losing its signal to a NaN
# --------------------------------------------------------------------------- #
def _intraday_frames(days=4, bars_per_day=5):
    """Hourly bars over several sessions, and a direction panel to match."""
    stamps = []
    for day in range(days):
        start = pd.Timestamp("2026-08-24", tz="UTC") + pd.Timedelta(days=day)
        stamps += [start + pd.Timedelta(hours=13, minutes=30) + pd.Timedelta(hours=h)
                   for h in range(bars_per_day)]
    return pd.DatetimeIndex(stamps, name=TIMESTAMP)


def test_the_opening_bar_takes_the_stance_from_before_it():
    """M235: yesterday's conviction acting at today's price.

    The signal is computed on the last bar that closed, so at a session open
    it was formed the previous evening while the fill lands on a price
    holding the whole overnight move. Entries on that bar won 44.2% against
    51.9% everywhere else (208 trips, t=-2.26, p=0.025).
    """
    from cfdinvest.signals.frame import _hold_across_session_open

    index = _intraday_frames()
    # Flat all through day one, long from the first bar of day two.
    values = ["flat"] * 5 + ["long"] * 15
    direction = pd.DataFrame({"SPY": values}, index=index)

    (held,) = _hold_across_session_open(direction)

    # The bar that opens day two inherits day one's close: still flat.
    assert held["SPY"].iloc[5] == "flat"
    # And the bar after it is free to act.
    assert held["SPY"].iloc[6] == "long"


def test_a_held_position_is_not_closed_by_the_rule():
    """M236: forcing flat at the open instead of holding.

    Flattening every opening bar would close positions the engine still
    wanted, turning a rule about *entering* into an exit nobody asked for --
    and it would do it at the gapped price it exists to avoid.
    """
    from cfdinvest.signals.frame import _hold_across_session_open

    index = _intraday_frames()
    values = ["long"] * 5 + ["flat"] + ["long"] * 14
    direction = pd.DataFrame({"SPY": values}, index=index)

    (held,) = _hold_across_session_open(direction)

    assert held["SPY"].iloc[5] == "long", "the rule closed a position"


def test_daily_bars_are_left_alone():
    """M237: a one-bar lag on everything, wearing the name of a gap rule.

    On daily bars every row opens a session, so holding across the open
    holds across the whole strategy. There is no opening bar to single out
    when the bar *is* the session.
    """
    from cfdinvest.signals.frame import _hold_across_session_open

    index = pd.bdate_range("2026-08-03", periods=10, tz="UTC", name=TIMESTAMP)
    direction = pd.DataFrame({"SPY": ["flat"] * 5 + ["long"] * 5}, index=index)

    (held,) = _hold_across_session_open(direction)

    assert list(held["SPY"]) == list(direction["SPY"])


def test_the_first_bar_of_the_sample_keeps_its_signal():
    """M238: a bar lost to a rule about a gap that has no gap.

    Nothing precedes the first bar, so carrying the previous one in would
    write a NaN over a perfectly good reading.
    """
    from cfdinvest.signals.frame import _hold_across_session_open

    index = _intraday_frames()
    direction = pd.DataFrame({"SPY": ["long"] * 20}, index=index)

    (held,) = _hold_across_session_open(direction)

    assert held["SPY"].iloc[0] == "long"
    assert held["SPY"].notna().all()


def test_the_engine_records_whether_it_held(analysis, regimes):
    """The metadata says which rule produced the frame.

    A signal frame that cannot say whether the rule was on is one nobody can
    reconcile against a config file three months later.
    """
    off = generate_signals(analysis, regimes, SignalConfig())
    on = generate_signals(
        analysis, regimes, replace(SignalConfig(), hold_across_session_open=True)
    )

    assert off.metadata["held_across_session_open"] is False
    assert on.metadata["held_across_session_open"] is True


def test_holding_never_leaves_size_on_a_flat_cell(analysis, regimes):
    """The invariant the execution layer depends on.

    A flat cell carrying size becomes a phantom trade downstream, and the
    hold rewrites direction and size independently -- so flatness has to be
    reconciled after it, not before.
    """
    signals = generate_signals(
        analysis, regimes, replace(SignalConfig(), hold_across_session_open=True)
    )
    flat = signals.direction == "flat"

    assert (signals.size.where(flat).fillna(0.0) == 0.0).to_numpy().all()
