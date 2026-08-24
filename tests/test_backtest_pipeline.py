"""Pipeline runner, walk-forward, Monte Carlo and the BacktestFrame.

Includes the reconciliation test that ties trade-level P&L back to the
equity curve — the one that catches an accounting slip anywhere upstream.
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from cfdinvest.backtest import (
    BacktestConfig,
    BacktestFrame,
    MetricsConfig,
    MonteCarloConfig,
    anchored_windows,
    evaluate,
    resample_returns,
    resample_trades,
    rolling_windows,
    run_backtest_from_analysis,
    run_from_analysis,
    run_pipeline,
    stability_report,
    summarise_simulations,
    walk_forward,
)
from cfdinvest.backtest.metrics import equity_returns
from cfdinvest.execution import ExecutionConfig
from cfdinvest.preprocessing import PreprocessConfig, build_analysis_frame
from cfdinvest.signals import SignalConfig
from cfdinvest.schema import TICKER, TIMESTAMP

BARS = 1200


@pytest.fixture
def calendar() -> pd.DatetimeIndex:
    return pd.bdate_range("2016-01-04", periods=BARS, tz="UTC", name=TIMESTAMP)


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
    rng = np.random.default_rng(52)
    common = rng.normal(0, 1, BARS)
    scale = np.where(np.arange(BARS) < 800, 0.007, 0.024)
    drift = np.where(np.arange(BARS) < 800, 0.0008, -0.002)

    returns = {
        "SPY": common * scale + drift + rng.normal(0, 0.003, BARS),
        "QQQ": 1.15 * common * scale + drift + rng.normal(0, 0.003, BARS),
        "TLT": -0.5 * common * scale + 0.0002 + rng.normal(0, 0.002, BARS),
        "GLD": 0.2 * common * scale + 0.0004 + rng.normal(0, 0.004, BARS),
    }
    return build_analysis_frame(
        market(calendar, returns),
        interval="1d",
        config=PreprocessConfig(correlation_window=63),
    )


@pytest.fixture
def backtest(analysis):
    return run_backtest_from_analysis(analysis, BacktestConfig())


# --------------------------------------------------------------------------- #
# Pipeline runner
# --------------------------------------------------------------------------- #
def test_pipeline_produces_every_frame(analysis):
    result = run_from_analysis(analysis, BacktestConfig())

    assert result.analysis is analysis
    assert result.regimes is not None
    assert result.signals is not None
    assert result.execution is not None


def test_pipeline_can_skip_regimes(analysis):
    result = run_from_analysis(analysis, BacktestConfig(use_regimes=False))

    assert result.regimes is None
    assert result.signals.metadata["regime_filtered"] is False


def test_skipping_regimes_changes_the_result(analysis):
    filtered = run_backtest_from_analysis(analysis, BacktestConfig())
    unfiltered = run_backtest_from_analysis(
        analysis, BacktestConfig(use_regimes=False)
    )
    assert filtered.metrics["round_trips"] != unfiltered.metrics["round_trips"]


def test_pipeline_does_not_mutate_the_analysis_frame(analysis):
    close_before = analysis.close.copy()
    regimes_before = analysis.regimes.copy()

    run_from_analysis(analysis, BacktestConfig())

    pd.testing.assert_frame_equal(analysis.close, close_before)
    pd.testing.assert_frame_equal(analysis.regimes, regimes_before)


def test_pipeline_is_deterministic(analysis):
    first = run_backtest_from_analysis(analysis, BacktestConfig())
    second = run_backtest_from_analysis(analysis, BacktestConfig())

    assert np.allclose(first.equity_curve, second.equity_curve)
    assert first.metrics["round_trips"] == second.metrics["round_trips"]


def test_config_flows_through_to_every_layer(analysis):
    tight = run_backtest_from_analysis(
        analysis,
        BacktestConfig(
            signal=SignalConfig(entry_threshold=0.9),
            execution=ExecutionConfig(default_spread_bps=50.0),
        ),
    )
    loose = run_backtest_from_analysis(analysis, BacktestConfig())

    assert tight.metrics["round_trips"] < loose.metrics["round_trips"]


# --------------------------------------------------------------------------- #
# Reconciliation
# --------------------------------------------------------------------------- #
def test_trade_pnl_reconciles_with_the_equity_curve(backtest):
    """The invariant that catches an accounting slip anywhere upstream.

    Every completed trip's net P&L, once financing is apportioned, must sum
    to the change in equity — provided nothing was left open at the end.
    """
    trips = backtest.round_trips()
    open_at_end = (backtest.execution.positions.iloc[-1] != 0).any()

    total_pnl = backtest.metrics["final_equity"] - backtest.metrics["initial_equity"]
    if not open_at_end:
        assert trips["net_pnl"].sum() == pytest.approx(total_pnl, rel=1e-6)


def test_expectancy_times_trades_equals_total_pnl(backtest):
    trips = backtest.round_trips()
    open_at_end = (backtest.execution.positions.iloc[-1] != 0).any()

    if not open_at_end and backtest.metrics["round_trips"]:
        implied = backtest.metrics["expectancy"] * backtest.metrics["round_trips"]
        actual = backtest.metrics["final_equity"] - backtest.metrics["initial_equity"]
        assert implied == pytest.approx(actual, rel=1e-6)


def test_metrics_agree_with_the_execution_frame(backtest):
    assert backtest.metrics["final_equity"] == pytest.approx(
        backtest.execution.equity_curve.iloc[-1]
    )
    assert backtest.metrics["total_financing"] == pytest.approx(
        backtest.execution.metadata["total_financing"]
    )


def test_cost_drag_sums_its_parts(backtest):
    expected = (
        backtest.metrics["total_spread"]
        + backtest.metrics["total_slippage"]
        + backtest.metrics["total_financing"]
    )
    assert backtest.metrics["cost_drag"] == pytest.approx(expected)


# --------------------------------------------------------------------------- #
# BacktestFrame
# --------------------------------------------------------------------------- #
def test_frame_exposes_the_specified_members(backtest):
    for member in (
        "metrics", "window_metrics", "monte_carlo", "analysis", "regimes",
        "signals", "execution", "metadata",
    ):
        assert hasattr(backtest, member)


def test_frame_keeps_references_to_every_stage(backtest):
    assert backtest.analysis is not None
    assert backtest.regimes is not None
    assert backtest.signals is not None
    assert backtest.execution is not None


def test_metadata_records_the_metric_conventions(backtest):
    convention = backtest.metadata["metrics_convention"]
    assert convention["r_basis"] == "equity_fraction"
    assert convention["risk_free_rate"] == 0.0


def test_metadata_records_the_run_scope(backtest, analysis):
    assert backtest.metadata["bars"] == len(analysis.calendar)
    assert backtest.metadata["tickers"] == tuple(analysis.close.columns)
    assert backtest.metadata["use_regimes"] is True


def test_summary_and_report(backtest):
    summary = backtest.summary()
    assert "sharpe" in summary.index
    assert "total_return" in summary.index
    assert "round trips" in backtest.report()


def test_empty_frame_views_do_not_raise():
    empty = BacktestFrame()
    assert empty.equity_curve.empty
    assert empty.trades.empty
    assert empty.round_trips().empty
    assert empty.report() == "empty backtest"


def test_evaluate_can_rescore_without_rerunning(analysis):
    result = run_from_analysis(analysis, BacktestConfig())

    default = evaluate(result, BacktestConfig())
    with_rf = evaluate(
        result, BacktestConfig(metrics=MetricsConfig(risk_free_rate=0.05))
    )

    # Same simulation, different scoring convention.
    assert np.allclose(default.equity_curve, with_rf.equity_curve)
    assert default.metrics["sharpe"] != with_rf.metrics["sharpe"]


# --------------------------------------------------------------------------- #
# Walk-forward
# --------------------------------------------------------------------------- #
def test_rolling_windows_do_not_overlap_in_test():
    windows = rolling_windows(1000, train_bars=200, test_bars=100)

    assert len(windows) == 8
    for earlier, later in zip(windows, windows[1:]):
        assert earlier.test_end <= later.test_start


def test_rolling_windows_slide_the_train_window():
    windows = rolling_windows(1000, train_bars=200, test_bars=100)
    assert windows[0].train_start == 0
    assert windows[1].train_start == 100


def test_anchored_windows_expand_the_train_window():
    windows = anchored_windows(1000, train_bars=200, test_bars=100)

    assert all(window.train_start == 0 for window in windows)
    assert windows[1].train_end > windows[0].train_end


def test_windows_reject_bad_lengths():
    with pytest.raises(ValueError, match="must be > 0"):
        rolling_windows(1000, train_bars=0, test_bars=100)
    with pytest.raises(ValueError, match="step must be > 0"):
        rolling_windows(1000, 200, 100, step=0)


def test_no_windows_when_history_is_too_short():
    assert rolling_windows(100, train_bars=200, test_bars=100) == []


def test_walk_forward_evaluates_each_window(analysis):
    result = run_from_analysis(analysis, BacktestConfig())
    windows = rolling_windows(len(analysis.calendar), 400, 200)

    metrics = walk_forward(
        analysis, result.signals, windows, ExecutionConfig(), MetricsConfig()
    )

    assert len(metrics) == len(windows)
    assert "total_return" in metrics.columns
    assert "sharpe" in metrics.columns
    assert (metrics["test_bars"] == 200).all()


def test_each_window_starts_flat_with_fresh_equity(analysis):
    """Windows must be independent samples, not one path cut into pieces."""
    result = run_from_analysis(analysis, BacktestConfig())
    windows = rolling_windows(len(analysis.calendar), 400, 200)

    metrics = walk_forward(
        analysis, result.signals, windows, ExecutionConfig(), MetricsConfig()
    )
    assert (metrics["initial_equity"] == 100_000.0).all()


def test_walk_forward_windows_are_ordered(analysis):
    result = run_from_analysis(analysis, BacktestConfig())
    windows = rolling_windows(len(analysis.calendar), 400, 200)
    metrics = walk_forward(analysis, result.signals, windows, ExecutionConfig())

    assert metrics["test_start"].is_monotonic_increasing


def test_walk_forward_of_no_windows(analysis):
    result = run_from_analysis(analysis, BacktestConfig())
    assert walk_forward(analysis, result.signals, [], ExecutionConfig()).empty


def test_stability_report_summarises_dispersion():
    metrics = pd.DataFrame(
        {"total_return": [0.1, -0.05, 0.2, 0.0], "sharpe": [1.0, -0.5, 2.0, 0.1]}
    )
    report = stability_report(metrics)

    assert report.loc["total_return", "windows"] == 4
    assert report.loc["total_return", "mean"] == pytest.approx(0.0625)
    assert report.loc["total_return", "positive_share"] == pytest.approx(0.5)


def test_stability_report_ignores_infinities():
    metrics = pd.DataFrame({"sharpe": [1.0, np.inf, 2.0, np.nan]})
    report = stability_report(metrics, columns=("sharpe",))
    assert report.loc["sharpe", "windows"] == 2


def test_stability_report_of_nothing():
    assert stability_report(pd.DataFrame()).empty


def test_walk_forward_runs_end_to_end(analysis):
    windows = rolling_windows(len(analysis.calendar), 400, 200)
    frame = run_backtest_from_analysis(analysis, BacktestConfig(), windows=windows)

    assert len(frame.window_metrics) == len(windows)
    assert not frame.stability.empty
    assert frame.metadata["walk_forward_windows"] == len(windows)


# --------------------------------------------------------------------------- #
# Monte Carlo
# --------------------------------------------------------------------------- #
@pytest.fixture
def trips() -> pd.DataFrame:
    rng = np.random.default_rng(11)
    return pd.DataFrame(
        {
            "net_pnl": rng.normal(50.0, 400.0, 120),
            "entry_notional": np.full(120, 10_000.0),
            "days_held": np.full(120, 5.0),
        }
    )


def test_trade_resampling_is_deterministic(trips):
    first = resample_trades(trips, 100_000.0, MonteCarloConfig(simulations=200, seed=1))
    second = resample_trades(trips, 100_000.0, MonteCarloConfig(simulations=200, seed=1))
    pd.testing.assert_frame_equal(first, second)


def test_a_different_seed_gives_a_different_distribution(trips):
    first = resample_trades(trips, 100_000.0, MonteCarloConfig(simulations=200, seed=1))
    second = resample_trades(trips, 100_000.0, MonteCarloConfig(simulations=200, seed=2))
    assert first["final_equity"].mean() != pytest.approx(second["final_equity"].mean())


def test_resampling_produces_one_row_per_simulation(trips):
    simulations = resample_trades(
        trips, 100_000.0, MonteCarloConfig(simulations=500, seed=3)
    )
    assert len(simulations) == 500
    assert list(simulations.columns) == [
        "final_equity", "total_return", "max_drawdown_fraction"
    ]


def test_the_resampled_mean_centres_on_the_actual_total(trips):
    """Bootstrapping preserves the mean; only the ordering varies."""
    simulations = resample_trades(
        trips, 100_000.0, MonteCarloConfig(simulations=4000, seed=5)
    )
    expected = 100_000.0 + trips["net_pnl"].sum()
    assert simulations["final_equity"].mean() == pytest.approx(expected, rel=0.02)


def test_resampled_drawdowns_are_never_positive(trips):
    simulations = resample_trades(
        trips, 100_000.0, MonteCarloConfig(simulations=200, seed=6)
    )
    assert (simulations["max_drawdown_fraction"] <= 0).all()


def test_resample_trades_of_nothing():
    assert resample_trades(pd.DataFrame(), 100_000.0).empty


def test_return_resampling_uses_blocks():
    rng = np.random.default_rng(8)
    returns = pd.Series(rng.normal(0.0005, 0.01, 500))

    simulations = resample_returns(
        returns, 100_000.0, MonteCarloConfig(simulations=300, seed=2, block_size=10)
    )
    assert len(simulations) == 300
    assert simulations["final_equity"].notna().all()


def test_return_resampling_is_deterministic():
    returns = pd.Series(np.random.default_rng(1).normal(0, 0.01, 200))
    config = MonteCarloConfig(simulations=100, seed=4)

    pd.testing.assert_frame_equal(
        resample_returns(returns, 100_000.0, config),
        resample_returns(returns, 100_000.0, config),
    )


def test_larger_blocks_preserve_more_clustering():
    """A block bootstrap should not look identical to an i.i.d. shuffle."""
    rng = np.random.default_rng(12)
    clustered = pd.Series(
        np.r_[rng.normal(0, 0.002, 250), rng.normal(0, 0.03, 250)]
    )
    iid = resample_returns(
        clustered, 100_000.0, MonteCarloConfig(simulations=400, seed=1, block_size=1)
    )
    blocked = resample_returns(
        clustered, 100_000.0, MonteCarloConfig(simulations=400, seed=1, block_size=50)
    )
    assert blocked["max_drawdown_fraction"].std() != pytest.approx(
        iid["max_drawdown_fraction"].std(), rel=0.05
    )


def test_monte_carlo_config_validates():
    with pytest.raises(ValueError, match="simulations must be > 0"):
        MonteCarloConfig(simulations=0)
    with pytest.raises(ValueError, match="block_size must be > 0"):
        MonteCarloConfig(block_size=0)


def test_summary_reports_percentiles(trips):
    simulations = resample_trades(
        trips, 100_000.0, MonteCarloConfig(simulations=500, seed=9)
    )
    summary = summarise_simulations(simulations)

    assert "p5" in summary.columns and "p95" in summary.columns
    assert "probability_positive" in summary.columns
    assert summary.loc["total_return", "p5"] < summary.loc["total_return", "p95"]


def test_summary_of_nothing():
    assert summarise_simulations(pd.DataFrame()).empty


def test_monte_carlo_runs_end_to_end(analysis):
    frame = run_backtest_from_analysis(
        analysis, BacktestConfig(), monte_carlo=MonteCarloConfig(simulations=300, seed=1)
    )
    assert not frame.monte_carlo.empty
    assert frame.metadata["monte_carlo_simulations"] == 300


def test_monte_carlo_median_tracks_the_actual_run(analysis):
    """Net-of-financing resampling must centre on what actually happened."""
    frame = run_backtest_from_analysis(
        analysis, BacktestConfig(), monte_carlo=MonteCarloConfig(simulations=3000, seed=2)
    )
    open_at_end = (frame.execution.positions.iloc[-1] != 0).any()

    if not open_at_end and frame.metrics["round_trips"] > 20:
        median = frame.monte_carlo.loc["total_return", "p50"]
        assert median == pytest.approx(frame.metrics["total_return"], abs=0.03)


# --------------------------------------------------------------------------- #
# Degraded inputs
# --------------------------------------------------------------------------- #
def test_empty_analysis_frame_is_handled():
    frame = run_backtest_from_analysis(build_analysis_frame(pd.DataFrame()))
    assert frame.equity_curve.empty
    assert frame.metrics.get("bars", 0) == 0


def test_a_strategy_that_never_trades_reports_zero_return(analysis):
    """Built from explicitly flat signals.

    Note an entry_threshold of 1.0 is *not* enough on its own: strength is
    capped at 1.0 and reaches it whenever all three trend components agree,
    so the threshold is still attainable.
    """
    from cfdinvest.backtest import evaluate
    from cfdinvest.backtest.runner import PipelineResult
    from cfdinvest.execution import run_backtest
    from cfdinvest.signals import SignalFrame

    calendar, columns = analysis.calendar, analysis.close.columns
    flat = SignalFrame(
        direction=pd.DataFrame("flat", index=calendar, columns=columns),
        size=pd.DataFrame(0.0, index=calendar, columns=columns),
        confidence=pd.DataFrame(0.0, index=calendar, columns=columns),
        reason=pd.DataFrame("flat", index=calendar, columns=columns),
    )
    execution = run_backtest(flat, analysis, ExecutionConfig())
    frame = evaluate(
        PipelineResult(analysis=analysis, signals=flat, execution=execution),
        BacktestConfig(),
    )

    assert frame.metrics["round_trips"] == 0
    assert frame.metrics["total_return"] == pytest.approx(0.0)
    assert np.isnan(frame.metrics["sharpe"])      # no variance, no ratio


def test_a_high_entry_threshold_trades_less(analysis):
    picky = run_backtest_from_analysis(
        analysis, BacktestConfig(signal=SignalConfig(entry_threshold=0.95))
    )
    relaxed = run_backtest_from_analysis(
        analysis, BacktestConfig(signal=SignalConfig(entry_threshold=0.2))
    )
    assert picky.metrics["round_trips"] < relaxed.metrics["round_trips"]


def test_pipeline_from_a_repository(config, tmp_path):
    """The I/O path, exercised against a real Parquet lake built offline."""
    from cfdinvest.ingestion.market import MarketDataIngestor
    from cfdinvest.repository import DataRepository
    from tests.conftest import make_yf_frame

    sessions = pd.bdate_range("2020-01-01", periods=400, name="Date")

    def downloader(tickers, **kwargs):
        return pd.concat(
            {t: make_yf_frame(sessions, 100.0 + 20 * i) for i, t in enumerate(tickers)},
            axis=1,
        )

    MarketDataIngestor(config, downloader=downloader).ingest(["AAA", "BBB"])
    repository = DataRepository(config)

    result = run_pipeline(
        repository, BacktestConfig(preprocess=PreprocessConfig(correlation_window=63))
    )
    assert result.analysis is not None
    assert tuple(result.analysis.close.columns) == ("AAA", "BBB")
    assert result.execution is not None
