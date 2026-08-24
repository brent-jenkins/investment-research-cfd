"""The execution loop end to end: accounting identities, lag, risk, churn.

The accounting tests matter most. If the P&L components stop summing to the
change in equity, something is being double-counted or lost, and every
number the analytics layer eventually reports inherits the error.
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from cfdinvest.execution import (
    ExecutionConfig,
    ExecutionFrame,
    HysteresisThresholds,
    run_backtest,
)
from cfdinvest.execution.engine import TRADE_COLUMNS
from cfdinvest.preprocessing import PreprocessConfig, build_analysis_frame
from cfdinvest.regimes import RegimeConfig, classify_regimes
from cfdinvest.signals import SignalConfig, SignalFrame, generate_signals
from cfdinvest.schema import TICKER, TIMESTAMP

BARS = 700


@pytest.fixture
def calendar() -> pd.DatetimeIndex:
    return pd.bdate_range("2019-01-02", periods=BARS, tz="UTC", name=TIMESTAMP)


def market(calendar, returns: dict[str, np.ndarray]) -> pd.DataFrame:
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


@pytest.fixture
def analysis(calendar):
    rng = np.random.default_rng(31)
    common = rng.normal(0, 1, BARS)
    scale = np.where(np.arange(BARS) < 450, 0.006, 0.026)
    drift = np.where(np.arange(BARS) < 450, 0.0009, -0.0028)

    returns = {
        "SPY": common * scale + drift + rng.normal(0, 0.003, BARS),
        "QQQ": 1.1 * common * scale + drift + rng.normal(0, 0.003, BARS),
        "TLT": -0.5 * common * scale + 0.0002 + rng.normal(0, 0.002, BARS),
        "GLD": 0.2 * common * scale + 0.0004 + rng.normal(0, 0.004, BARS),
    }
    return build_analysis_frame(
        market(calendar, returns),
        interval="1d",
        config=PreprocessConfig(correlation_window=63),
    )


@pytest.fixture
def signals(analysis):
    regimes = classify_regimes(analysis, RegimeConfig(volatility_window=252))
    return generate_signals(analysis, regimes, SignalConfig())


@pytest.fixture
def execution(signals, analysis):
    return run_backtest(signals, analysis, ExecutionConfig())


# --------------------------------------------------------------------------- #
# Structure
# --------------------------------------------------------------------------- #
def test_frame_exposes_the_specified_members(execution):
    for member in ("positions", "trades", "pnl", "equity_curve", "metadata"):
        assert hasattr(execution, member)


def test_panels_share_the_calendar(execution, analysis):
    assert execution.positions.index.equals(analysis.calendar)
    assert execution.pnl.index.equals(analysis.calendar)
    assert execution.equity_curve.index.equals(analysis.calendar)


def test_trade_log_has_the_specified_columns(execution):
    assert list(execution.trades.columns) == list(TRADE_COLUMNS)
    for column in ("timestamp", "asset", "action", "size" if False else "units",
                   "price", "slippage", "financing", "realized_pnl"):
        assert column in execution.trades.columns


def test_trade_actions_are_from_the_declared_set(execution):
    from cfdinvest.execution.state import ACTIONS

    assert set(execution.trades["action"]) <= set(ACTIONS)


def test_metadata_records_provenance(execution, analysis):
    metadata = execution.metadata
    assert metadata["bars"] == len(analysis.calendar)
    assert metadata["execution_lag"] == 1
    assert metadata["fill_mode"] == "open"
    assert metadata["trades"] == len(execution.trades)
    assert "thresholds" in metadata and "financing" in metadata


# --------------------------------------------------------------------------- #
# Accounting identities
# --------------------------------------------------------------------------- #
def test_components_sum_to_total_every_bar(execution):
    from cfdinvest.execution.pnl import PNL_COMPONENTS

    parts = execution.pnl[list(PNL_COMPONENTS)].sum(axis=1)
    assert np.allclose(parts, execution.pnl["total"])


def test_equity_is_the_cumulative_total(execution):
    expected = execution.metadata["initial_equity"] + execution.pnl["total"].cumsum()
    assert np.allclose(execution.equity_curve, expected)


def test_final_equity_matches_the_summary(execution):
    assert execution.equity_curve.iloc[-1] == pytest.approx(
        execution.metadata["final_equity"]
    )


def test_total_return_is_consistent(execution):
    initial = execution.metadata["initial_equity"]
    expected = (execution.metadata["final_equity"] - initial) / initial
    assert execution.metadata["total_return"] == pytest.approx(expected)


def test_costs_are_never_a_benefit(execution):
    assert (execution.pnl["spread"] <= 0).all()
    assert (execution.pnl["slippage"] <= 0).all()


def test_reported_costs_match_the_trade_log(execution):
    assert execution.metadata["total_spread"] == pytest.approx(
        execution.trades["spread_cost"].sum(), rel=1e-9
    )


def test_equity_has_no_missing_values(execution):
    assert execution.equity_curve.notna().all()
    assert np.isfinite(execution.equity_curve.to_numpy()).all()


def test_positions_are_finite(execution):
    assert np.isfinite(execution.positions.to_numpy()).all()


# --------------------------------------------------------------------------- #
# The execution lag
# --------------------------------------------------------------------------- #
def test_no_trade_before_the_first_signal(signals, analysis):
    execution = run_backtest(signals, analysis, ExecutionConfig())
    first_signal = signals.direction.ne("flat").any(axis=1).idxmax()
    first_trade = execution.trades["timestamp"].min()

    assert first_trade > first_signal


def test_a_position_lags_its_signal_by_exactly_one_bar(calendar):
    """Constructed so the answer is unambiguous: one long signal, one bar."""
    prices = market(calendar, {"AAA": np.full(BARS, 0.0005)})
    analysis = build_analysis_frame(prices, config=PreprocessConfig())

    direction = pd.DataFrame("flat", index=analysis.calendar, columns=["AAA"])
    size = pd.DataFrame(0.0, index=analysis.calendar, columns=["AAA"])
    confidence = pd.DataFrame(0.0, index=analysis.calendar, columns=["AAA"])
    direction.iloc[100:] = "long"
    size.iloc[100:] = 0.5
    confidence.iloc[100:] = 0.9

    frame = SignalFrame(
        direction=direction, size=size, confidence=confidence,
        reason=pd.DataFrame("test", index=analysis.calendar, columns=["AAA"]),
    )
    execution = run_backtest(frame, analysis, ExecutionConfig())

    assert execution.positions["AAA"].iloc[100] == 0.0     # signal bar: not yet
    assert execution.positions["AAA"].iloc[101] != 0.0     # next bar: filled


def test_zero_lag_changes_the_result(signals, analysis):
    """Look-ahead is available but must not be silently identical."""
    honest = run_backtest(signals, analysis, ExecutionConfig(execution_lag=1))
    cheating = run_backtest(signals, analysis, ExecutionConfig(execution_lag=0))

    assert not np.allclose(honest.equity_curve, cheating.equity_curve)


def test_truncating_the_future_does_not_change_the_past(signals, analysis):
    cut = 500
    truncated_analysis = replace(
        analysis,
        prices={k: v.iloc[:cut] for k, v in analysis.prices.items()},
        returns={k: v.iloc[:cut] for k, v in analysis.returns.items()},
        volatility={k: v.iloc[:cut] for k, v in analysis.volatility.items()},
        correlations={},
        macro=analysis.macro.iloc[:cut] if not analysis.macro.empty else analysis.macro,
        regimes=analysis.regimes.iloc[:cut],
    )
    truncated_signals = SignalFrame(
        direction=signals.direction.iloc[:cut],
        size=signals.size.iloc[:cut],
        confidence=signals.confidence.iloc[:cut],
        reason=signals.reason.iloc[:cut],
    )

    full = run_backtest(signals, analysis, ExecutionConfig())
    partial = run_backtest(truncated_signals, truncated_analysis, ExecutionConfig())

    assert np.allclose(full.equity_curve.iloc[:cut], partial.equity_curve)
    pd.testing.assert_frame_equal(
        full.positions.iloc[:cut], partial.positions, check_freq=False
    )


# --------------------------------------------------------------------------- #
# Hysteresis reduces churn
# --------------------------------------------------------------------------- #
def test_a_wider_band_trades_less(signals, analysis):
    narrow = run_backtest(
        signals, analysis,
        ExecutionConfig(
            thresholds=HysteresisThresholds(
                entry_size=0.05, exit_size=0.05,
                entry_confidence=0.2, exit_confidence=0.2,
            )
        ),
    )
    wide = run_backtest(
        signals, analysis,
        ExecutionConfig(
            thresholds=HysteresisThresholds(
                entry_size=0.30, exit_size=0.02,
                entry_confidence=0.60, exit_confidence=0.05,
            )
        ),
    )
    assert len(wide.trades) < len(narrow.trades)


def test_a_wider_band_holds_longer(signals, analysis):
    narrow = run_backtest(
        signals, analysis,
        ExecutionConfig(
            thresholds=HysteresisThresholds(
                entry_size=0.05, exit_size=0.05,
                entry_confidence=0.2, exit_confidence=0.2,
            )
        ),
    )
    wide = run_backtest(
        signals, analysis,
        ExecutionConfig(
            thresholds=HysteresisThresholds(
                entry_size=0.30, exit_size=0.02,
                entry_confidence=0.60, exit_confidence=0.05,
            )
        ),
    )
    assert wide.holding_periods().mean() > narrow.holding_periods().mean()


def test_rebalance_threshold_reduces_resizes(signals, analysis):
    eager = run_backtest(signals, analysis, ExecutionConfig(rebalance_threshold=0.0))
    patient = run_backtest(signals, analysis, ExecutionConfig(rebalance_threshold=5.0))

    eager_resizes = (eager.trades["action"] == "resize").sum()
    patient_resizes = (patient.trades["action"] == "resize").sum()
    assert patient_resizes < eager_resizes


def test_holding_periods_are_positive(execution):
    periods = execution.holding_periods()
    assert (periods > 0).all()


# --------------------------------------------------------------------------- #
# Risk engine
# --------------------------------------------------------------------------- #
def test_gross_leverage_respects_the_cap(signals, analysis):
    execution = run_backtest(signals, analysis, ExecutionConfig(max_leverage=0.5))
    assert execution.exposure.max() <= 0.5 + 1e-6


def test_a_tighter_cap_reduces_exposure(signals, analysis):
    loose = run_backtest(signals, analysis, ExecutionConfig(max_leverage=5.0))
    tight = run_backtest(signals, analysis, ExecutionConfig(max_leverage=0.2))

    assert tight.exposure.max() < loose.exposure.max()


def test_exposure_scaling_preserves_relative_sizing(signals, analysis):
    """Capping the book must not change which asset is the bigger position."""
    loose = run_backtest(signals, analysis, ExecutionConfig(max_leverage=100.0))
    tight = run_backtest(signals, analysis, ExecutionConfig(max_leverage=0.1))

    bar = tight.notionals.abs().sum(axis=1).idxmax()
    loose_row = loose.notionals.loc[bar].abs()
    tight_row = tight.notionals.loc[bar].abs()

    if loose_row.sum() > 0 and tight_row.sum() > 0:
        assert loose_row.idxmax() == tight_row.idxmax()


def test_stop_out_flattens_the_book(calendar):
    """A relentless decline must trigger the stop and stay flat."""
    falling = market(calendar, {"AAA": np.full(BARS, -0.01)})
    analysis = build_analysis_frame(falling, config=PreprocessConfig())

    direction = pd.DataFrame("long", index=analysis.calendar, columns=["AAA"])
    size = pd.DataFrame(1.0, index=analysis.calendar, columns=["AAA"])
    confidence = pd.DataFrame(1.0, index=analysis.calendar, columns=["AAA"])
    frame = SignalFrame(
        direction=direction, size=size, confidence=confidence,
        reason=pd.DataFrame("test", index=analysis.calendar, columns=["AAA"]),
    )

    execution = run_backtest(
        frame, analysis,
        ExecutionConfig(stop_out_level=0.8, resume_after_stop_out=False),
    )
    assert (execution.trades["reason"] == "stop_out").any()
    assert execution.positions["AAA"].iloc[-1] == 0.0


def test_no_stop_out_when_disabled(calendar):
    falling = market(calendar, {"AAA": np.full(BARS, -0.01)})
    analysis = build_analysis_frame(falling, config=PreprocessConfig())
    frame = SignalFrame(
        direction=pd.DataFrame("long", index=analysis.calendar, columns=["AAA"]),
        size=pd.DataFrame(1.0, index=analysis.calendar, columns=["AAA"]),
        confidence=pd.DataFrame(1.0, index=analysis.calendar, columns=["AAA"]),
        reason=pd.DataFrame("test", index=analysis.calendar, columns=["AAA"]),
    )
    execution = run_backtest(frame, analysis, ExecutionConfig(stop_out_level=None))
    assert not (execution.trades["reason"] == "stop_out").any()


def test_invalid_config_is_refused():
    with pytest.raises(ValueError, match="initial_equity"):
        ExecutionConfig(initial_equity=0)
    with pytest.raises(ValueError, match="max_leverage"):
        ExecutionConfig(max_leverage=0)
    with pytest.raises(ValueError, match="slippage_fraction"):
        ExecutionConfig(slippage_fraction=-0.1)
    with pytest.raises(ValueError, match="stop_out_level"):
        ExecutionConfig(stop_out_level=1.5)


# --------------------------------------------------------------------------- #
# Costs and financing bite
# --------------------------------------------------------------------------- #
def test_wider_spreads_cost_more(signals, analysis):
    cheap = run_backtest(signals, analysis, ExecutionConfig(default_spread_bps=1.0))
    dear = run_backtest(signals, analysis, ExecutionConfig(default_spread_bps=50.0))

    assert dear.metadata["total_spread"] > cheap.metadata["total_spread"]
    assert dear.equity_curve.iloc[-1] < cheap.equity_curve.iloc[-1]


def test_slippage_reduces_returns(signals, analysis):
    clean = run_backtest(signals, analysis, ExecutionConfig(slippage_fraction=0.0))
    dirty = run_backtest(signals, analysis, ExecutionConfig(slippage_fraction=0.002))

    assert clean.metadata["total_slippage"] == 0.0
    assert dirty.metadata["total_slippage"] > 0.0
    assert dirty.equity_curve.iloc[-1] < clean.equity_curve.iloc[-1]


def test_financing_accrues_on_held_positions(signals, analysis):
    free = run_backtest(
        signals, analysis, ExecutionConfig(benchmark_rate=0.0, financing_markup=0.0)
    )
    expensive = run_backtest(
        signals, analysis, ExecutionConfig(benchmark_rate=0.10, financing_markup=0.05)
    )

    assert free.metadata["total_financing"] == pytest.approx(0.0)
    assert expensive.metadata["total_financing"] > 0.0


def test_zero_cost_run_is_the_upper_bound(signals, analysis):
    frictionless = run_backtest(
        signals, analysis,
        ExecutionConfig(
            default_spread_bps=0.0, slippage_fraction=0.0,
            benchmark_rate=0.0, financing_markup=0.0,
        ),
    )
    realistic = run_backtest(signals, analysis, ExecutionConfig())
    assert frictionless.equity_curve.iloc[-1] > realistic.equity_curve.iloc[-1]


# --------------------------------------------------------------------------- #
# Determinism and purity
# --------------------------------------------------------------------------- #
def test_execution_is_deterministic(signals, analysis):
    first = run_backtest(signals, analysis, ExecutionConfig())
    second = run_backtest(signals, analysis, ExecutionConfig())

    assert np.allclose(first.equity_curve, second.equity_curve)
    pd.testing.assert_frame_equal(first.positions, second.positions)
    pd.testing.assert_frame_equal(first.trades, second.trades)


def test_execution_does_not_mutate_its_inputs(signals, analysis):
    close_before = analysis.close.copy()
    direction_before = signals.direction.copy()

    run_backtest(signals, analysis, ExecutionConfig())

    pd.testing.assert_frame_equal(analysis.close, close_before)
    pd.testing.assert_frame_equal(signals.direction, direction_before)


# --------------------------------------------------------------------------- #
# Degraded inputs
# --------------------------------------------------------------------------- #
def test_flat_signals_produce_no_trades(analysis):
    calendar = analysis.calendar
    columns = analysis.close.columns
    frame = SignalFrame(
        direction=pd.DataFrame("flat", index=calendar, columns=columns),
        size=pd.DataFrame(0.0, index=calendar, columns=columns),
        confidence=pd.DataFrame(0.0, index=calendar, columns=columns),
        reason=pd.DataFrame("flat", index=calendar, columns=columns),
    )
    execution = run_backtest(frame, analysis, ExecutionConfig())

    assert execution.trades.empty
    assert (execution.positions == 0).all().all()
    assert np.allclose(execution.equity_curve, 100_000.0)


def test_empty_signals_yield_an_empty_frame(analysis):
    execution = run_backtest(SignalFrame(), analysis, ExecutionConfig())
    assert execution.equity_curve.empty
    assert execution.trades.empty


def test_empty_analysis_frame_is_handled(signals):
    execution = run_backtest(signals, build_analysis_frame(pd.DataFrame()))
    assert execution.equity_curve.empty


def test_close_only_lake_still_executes(calendar):
    rng = np.random.default_rng(4)
    tidy = market(calendar, {t: rng.normal(0.0006, 0.005, BARS) for t in ("AAA", "BBB")})
    closes_only = tidy[["close"]].copy()
    analysis = build_analysis_frame(
        closes_only, config=PreprocessConfig(correlation_window=63)
    )
    frame = generate_signals(analysis, None, SignalConfig())

    execution = run_backtest(frame, analysis, ExecutionConfig())
    assert not execution.trades.empty


# --------------------------------------------------------------------------- #
# Views
# --------------------------------------------------------------------------- #
def test_trade_summary_reconciles_with_the_log(execution):
    summary = execution.trade_summary()
    assert summary["trades"].sum() == len(execution.trades)


def test_per_asset_summary_covers_traded_names(execution):
    summary = execution.per_asset()
    assert set(summary.index) <= set(execution.positions.columns)


def test_drawdown_view(execution):
    curve = execution.drawdown()
    assert list(curve.columns) == ["peak", "drawdown", "drawdown_fraction"]
    assert (curve["drawdown"] <= 0).all()


def test_active_positions_matches_positions(execution):
    assert (execution.active_positions() == (execution.positions != 0)).all().all()


def test_empty_frame_views_do_not_raise():
    empty = ExecutionFrame()
    assert empty.drawdown().empty
    assert empty.trade_summary().empty
    assert empty.per_asset().empty
    assert empty.holding_periods().empty
    assert empty.active_positions().empty
