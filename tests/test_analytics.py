"""The analytics layer: metrics, attribution, sweeps, walk-forward, resampling.

Two kinds of test here. The **identities** — that an attribution sums to the
total, that walk-forward windows do not overlap, that a seed reproduces a
distribution — are properties that must hold for any input, and they are
asserted directly rather than checked against recorded numbers.

The **integration** tests run the real pipeline over a small synthetic lake
built on disk. They are slower than the rest of this suite and they are the
only ones that would catch a change in the pipeline breaking the scoring, so
the lake is built once per session and reused.

No network, and nothing outside the temporary lake is written.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from cfdinvest import storage
from cfdinvest.analytics import attribution, backtest, metrics, montecarlo, store
from cfdinvest.analytics import walkforward as wf
from cfdinvest.analytics import cli
from cfdinvest.backtest.runner import BacktestConfig
from cfdinvest.config import AppConfig, PathConfig
from cfdinvest.execution.frame import ExecutionConfig, ExecutionFrame
from cfdinvest.repository import DataRepository
from cfdinvest.schema import TICKER, TIMESTAMP

TICKERS = ("SPY", "GLD", "TLT")
BARS = 700
INTERVAL = "1d"


# --------------------------------------------------------------------------- #
# A small offline lake
# --------------------------------------------------------------------------- #
def build_lake(root: Path, *, bars: int = BARS, seed: int = 7) -> AppConfig:
    """Write OHLCV parquet plus the manifest the repository reads."""
    calendar = pd.bdate_range("2022-01-03", periods=bars, tz="UTC", name=TIMESTAMP)
    config = AppConfig(paths=PathConfig(data_root=root))
    manifest = {}

    for offset, ticker in enumerate(TICKERS):
        rng = np.random.default_rng(seed + offset)
        close = 100 * np.exp(
            np.cumsum(rng.normal(0.0004 + 0.0002 * offset, 0.011, bars))
        )
        opens = np.r_[close[0], close[:-1]] * (1 + rng.normal(0, 0.001, bars))
        frame = pd.DataFrame(
            {
                "open": opens,
                "high": np.maximum(opens, close)
                * (1 + np.abs(rng.normal(0, 0.004, bars))),
                "low": np.minimum(opens, close)
                * (1 - np.abs(rng.normal(0, 0.004, bars))),
                "close": close,
                "adj_close": close,
                "volume": rng.integers(1e6, 5e6, bars).astype("float64"),
                TICKER: ticker,
            },
            index=calendar,
        )
        storage.write_parquet(
            frame, storage.market_path(config.paths, ticker, INTERVAL)
        )
        # The keys the ingestors really write (see ``IngestionReport``). The
        # invented "rows"/"start"/"end" spelling used to live here, and it
        # meant _window_bounds could read this fixture but never a real lake.
        manifest[ticker] = {
            "status": "ok",
            "rows_total": int(bars),
            "first_timestamp": str(calendar[0].date()),
            "last_timestamp": str(calendar[-1].date()),
            "updated_at": "2026-08-25T12:00:00+00:00",
            "anomalies": {},
        }

    path = storage.metadata_path(config.paths, f"market_{INTERVAL}_manifest")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return config


@pytest.fixture(scope="session")
def lake(tmp_path_factory) -> AppConfig:
    """One lake for the whole session — building it is not free."""
    return build_lake(tmp_path_factory.mktemp("analytics_lake") / "data")


@pytest.fixture(scope="session")
def repository(lake) -> DataRepository:
    return DataRepository(lake)


@pytest.fixture(scope="session")
def universe() -> BacktestConfig:
    return BacktestConfig(tickers=list(TICKERS), interval=INTERVAL)


@pytest.fixture(scope="session")
def scored(universe, repository) -> backtest.BacktestResult:
    """One completed, scored run. Reused: the pipeline is the slow part."""
    return backtest.run_backtest(universe, repository=repository)


@pytest.fixture
def written_lake(tmp_path) -> AppConfig:
    """A throwaway lake for tests that write analytics artefacts."""
    return AppConfig(paths=PathConfig(data_root=tmp_path / "data"))


@pytest.fixture(autouse=True)
def restore_logging():
    """Undo what the command line does to the package logger.

    ``configure_logging`` attaches handlers and sets ``propagate = False``,
    which is right for a process and wrong for a test session: every later
    test that uses ``caplog`` silently sees nothing, and fails somewhere
    unrelated. Exercising a command line means exercising that, so the
    damage is contained here rather than left for another module to trip
    over.
    """
    import logging

    logger = logging.getLogger("cfdinvest")
    handlers = list(logger.handlers)
    level, propagate = logger.level, logger.propagate
    try:
        yield
    finally:
        logger.handlers = handlers
        logger.setLevel(level)
        logger.propagate = propagate


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #
def synthetic_frame(seed: int = 3, bars: int = 400) -> ExecutionFrame:
    """An execution frame with a known equity path and a trade log."""
    rng = np.random.default_rng(seed)
    index = pd.bdate_range("2024-01-01", periods=bars, tz="UTC", name=TIMESTAMP)
    equity = pd.Series(
        100_000 + np.cumsum(rng.normal(20, 300, bars)), index=index, name="equity"
    )
    return ExecutionFrame(
        equity_curve=equity,
        exposure=pd.Series(rng.uniform(0.0, 2.0, bars), index=index),
        metadata={"initial_equity": 100_000.0, "max_leverage": 5.0},
    )


def test_metrics_are_deterministic():
    frame = synthetic_frame()
    assert metrics.compute_sharpe_ratio(frame) == metrics.compute_sharpe_ratio(frame)
    assert metrics.compute_sortino_ratio(frame) == metrics.compute_sortino_ratio(frame)


def test_metrics_delegate_rather_than_reimplement():
    """One Sharpe, not two that can disagree while both claim the name."""
    from cfdinvest.backtest import metrics as base

    frame = synthetic_frame()
    config = metrics.MetricsConfig(risk_free_rate=0.02)

    assert metrics.compute_sharpe_ratio(frame, config=config) == base.sharpe_ratio(
        frame.equity_curve, config
    )
    assert metrics.compute_sortino_ratio(frame, config=config) == base.sortino_ratio(
        frame.equity_curve, config
    )


def test_a_risk_free_rate_lowers_the_sharpe_ratio():
    frame = synthetic_frame()
    assert metrics.compute_sharpe_ratio(frame, 0.05) < metrics.compute_sharpe_ratio(
        frame, 0.0
    )


def test_metrics_accept_an_equity_series_directly():
    frame = synthetic_frame()
    assert metrics.compute_sharpe_ratio(frame.equity_curve) == (
        metrics.compute_sharpe_ratio(frame)
    )


def test_drawdown_is_never_positive():
    result = metrics.compute_max_drawdown(synthetic_frame())
    assert result["max_drawdown_fraction"] <= 0


def test_leverage_stats_report_the_cap_and_its_breaches():
    frame = synthetic_frame()
    stats = metrics.compute_leverage_stats(frame)

    assert stats["leverage_cap"] == 5.0
    assert stats["cap_breaches"] == 0
    assert 0 <= stats["mean_leverage"] <= stats["max_leverage"]


def test_leverage_stats_of_an_empty_frame_do_not_raise():
    stats = metrics.compute_leverage_stats(ExecutionFrame())
    assert np.isnan(stats["mean_leverage"])


def test_gap_through_stats_are_absent_not_zero_on_an_old_trade_log():
    """"Not recorded" is a different claim from "no gap risk"."""
    trades = pd.DataFrame({"reason": ["signal"], "asset": ["SPY"]})
    stats = metrics.compute_gap_through_stats(trades)

    assert stats["available"] is False
    assert np.isnan(stats["gap_share"])


def test_gap_through_stats_count_the_exits_that_gapped():
    trades = pd.DataFrame(
        {
            "reason": ["stop_loss", "stop_loss", "take_profit", "signal"],
            "gap_through": [True, False, False, False],
            "slippage_applied": [1.5, 0.0, 0.0, 0.0],
        }
    )
    stats = metrics.compute_gap_through_stats(trades)

    assert stats["level_exits"] == 3
    assert stats["gap_through_exits"] == 1
    assert stats["gap_share"] == pytest.approx(1 / 3)
    assert stats["total_slippage_applied"] == pytest.approx(1.5)


def test_currency_stats_are_absent_on_a_pre_fx_trade_log():
    stats = metrics.compute_fx_exposure_stats(
        ExecutionFrame(trades=pd.DataFrame({"asset": ["SPY"]}))
    )
    assert stats["available"] is False


def test_currency_stats_measure_the_converted_share():
    frame = ExecutionFrame(
        trades=pd.DataFrame(
            {
                "asset": ["SPY", "UKX"],
                "fx_rate": [0.8, 1.0],
                "notional": [1000.0, 1000.0],
                "fx_conversion_fee": [4.0, 0.0],
                "fx_spread": [0.0, 0.0],
                "fx_slippage": [0.0, 0.0],
            }
        ),
        metadata={"fx_enabled": True, "account_currency": "GBP"},
    )
    stats = metrics.compute_fx_exposure_stats(frame)

    assert stats["converted_trades"] == 1
    assert stats["converted_notional_share"] == pytest.approx(0.5)
    assert stats["total_currency_cost"] == pytest.approx(4.0)


def test_round_trips_are_accepted_as_well_as_a_trade_log():
    """A round-trip frame carries net_pnl; a trade log does not."""
    trips = pd.DataFrame({"net_pnl": [10.0, -5.0, 3.0]})
    assert metrics.compute_win_rate(trips) == pytest.approx(2 / 3)
    assert metrics.as_round_trips(trips) is trips


def test_the_r_distribution_reports_shape_not_only_its_mean():
    trips = pd.DataFrame(
        {"net_pnl": [1.0, -1.0, 5.0], "r_multiple": [0.5, -1.0, 3.0]}
    )
    result = metrics.compute_r_distribution(trips)

    assert result["count"] == 3
    assert result["best"] == pytest.approx(3.0)
    assert result["worst"] == pytest.approx(-1.0)
    assert result["positive"] == 2
    assert set(result["percentiles"]) == {"p5", "p25", "p50", "p75", "p95"}


def test_every_metric_survives_an_empty_run():
    """A fresh install must render, not raise."""
    result = metrics.compute_all_metrics(ExecutionFrame())
    assert result["trade_count"] == 0
    assert np.isnan(result["sharpe_ratio"]) or result["sharpe_ratio"] == 0


# --------------------------------------------------------------------------- #
# Attribution — the identity
# --------------------------------------------------------------------------- #
def pnl_frame(seed: int = 5, bars: int = 200) -> pd.DataFrame:
    """A P&L frame shaped exactly like the engine's."""
    rng = np.random.default_rng(seed)
    index = pd.bdate_range("2024-01-01", periods=bars, tz="UTC", name=TIMESTAMP)
    parts = {
        "gap": rng.normal(5, 80, bars),
        "intraday": rng.normal(3, 60, bars),
        "spread": -np.abs(rng.normal(2, 1, bars)),
        "slippage": -np.abs(rng.normal(0.5, 0.3, bars)),
        "financing": -np.abs(rng.normal(1, 0.5, bars)),
        "fx": -np.abs(rng.normal(0.8, 0.4, bars)),
    }
    frame = pd.DataFrame(parts, index=index)
    frame["total"] = frame.sum(axis=1)
    return frame


def test_attribution_sums_to_the_total():
    """The identity this module exists to preserve."""
    frame = ExecutionFrame(pnl=pnl_frame(), metadata={"initial_equity": 100_000.0})
    result = attribution.compute_pnl_attribution(frame)

    assert sum(result[term] for term in attribution.ATTRIBUTION_TERMS) == (
        pytest.approx(result["total_pnl"])
    )
    assert result["residual"] == pytest.approx(0.0, abs=1e-9)


def test_attribution_holds_with_stops_and_currency_in_play(scored):
    """The identity must survive mid-bar exits and conversion, not only a fixture."""
    if not scored.ok:
        pytest.skip(f"pipeline unavailable: {scored.error}")

    result = scored.attribution
    assert sum(result[term] for term in attribution.ATTRIBUTION_TERMS) == (
        pytest.approx(result["total_pnl"], abs=1e-6)
    )


def test_return_attribution_is_the_same_split_over_starting_equity():
    frame = ExecutionFrame(pnl=pnl_frame(), metadata={"initial_equity": 100_000.0})
    amounts = attribution.compute_pnl_attribution(frame)
    returns = attribution.compute_return_attribution(frame)

    assert returns["total_return"] == pytest.approx(amounts["total_pnl"] / 100_000.0)
    assert returns["spread_pnl"] == pytest.approx(amounts["spread_pnl"] / 100_000.0)


def test_cost_breakdown_reports_costs_as_positive_amounts():
    frame = ExecutionFrame(pnl=pnl_frame())
    costs = attribution.compute_cost_breakdown(frame)

    for name in ("spread", "slippage", "financing", "currency_conversion"):
        assert costs[name] > 0, name
    assert costs["total_cost"] == pytest.approx(
        sum(costs[n] for n in ("spread", "slippage", "financing",
                               "currency_conversion"))
    )


def test_the_currency_split_says_why_it_is_unavailable():
    """A zero would read as "no currency effect", which is a different claim."""
    frame = ExecutionFrame(pnl=pnl_frame(), metadata={"fx_enabled": False})
    split = attribution.compute_currency_attribution(frame, None)

    assert split["available"] is False
    assert "disabled" in split["reason"]


def test_the_currency_split_needs_the_analysis_frame():
    frame = ExecutionFrame(pnl=pnl_frame(), metadata={"fx_enabled": True})
    split = attribution.compute_currency_attribution(frame, None)

    assert split["available"] is False
    assert "analysis frame" in split["reason"]


def test_reconstructed_attribution_also_sums_to_the_total():
    """What the dashboard uses: trades and equity, with no P&L frame."""
    trades = pd.DataFrame(
        {
            "spread_cost": [1.0, 2.0],
            "slippage": [0.5, 0.0],
            "financing": [0.2, 0.3],
            "fx_conversion_fee": [4.0, 0.0],
        }
    )
    equity = pd.Series([100_000.0, 100_500.0, 101_200.0])
    result = attribution.attribution_from_trades(trades, equity)

    assert sum(result[t] for t in attribution.ATTRIBUTION_TERMS) == pytest.approx(
        result["total_pnl"]
    )
    assert result["total_pnl"] == pytest.approx(1_200.0)
    assert result["spread_pnl"] == pytest.approx(-3.0)


def test_reconstructed_attribution_of_an_empty_run():
    result = attribution.attribution_from_trades(pd.DataFrame(), pd.Series(dtype=float))
    assert result["total_pnl"] == 0.0


def test_per_instrument_attribution_groups_the_trade_log():
    frame = ExecutionFrame(
        trades=pd.DataFrame(
            {
                "asset": ["SPY", "SPY", "GLD"],
                "realized_pnl": [10.0, -4.0, 7.0],
                "spread_cost": [1.0, 1.0, 0.5],
            }
        )
    )
    result = attribution.compute_instrument_attribution(frame)

    assert list(result.index) == ["GLD", "SPY"] or list(result.index) == ["SPY", "GLD"]
    assert result.loc["SPY", "trades"] == 2
    assert result.loc["SPY", "realized_pnl"] == pytest.approx(6.0)


# --------------------------------------------------------------------------- #
# Backtest
# --------------------------------------------------------------------------- #
def test_a_run_produces_metrics_and_an_equity_curve(scored):
    if not scored.ok:
        pytest.skip(f"pipeline unavailable: {scored.error}")

    assert scored.bars > 0
    assert not scored.equity_curve.empty
    assert set(scored.universe) == set(TICKERS)
    assert "sharpe_ratio" in scored.metrics


def test_the_same_configuration_produces_the_same_result(universe, repository):
    """Determinism: the whole point of an offline lake."""
    first = backtest.run_backtest(universe, repository=repository)
    second = backtest.run_backtest(universe, repository=repository)

    assert first.metrics["sharpe_ratio"] == second.metrics["sharpe_ratio"]
    assert first.trade_count == second.trade_count
    pd.testing.assert_series_equal(first.equity_curve, second.equity_curve)


def test_overrides_reach_the_nested_configuration(universe):
    changed = backtest.apply_overrides(universe, {"execution.max_leverage": 2.5})

    assert changed.execution.max_leverage == 2.5
    assert universe.execution.max_leverage != 2.5, "the base config was mutated"


def test_an_unknown_override_is_refused(universe):
    """A silently ignored override reports the same result under two labels."""
    with pytest.raises(ValueError, match="unknown setting"):
        backtest.apply_overrides(universe, {"execution.not_a_setting": 1})
    with pytest.raises(ValueError, match="unknown setting"):
        backtest.apply_overrides(universe, {"nonsense.thing": 1})


def test_the_grid_expands_in_a_stable_order():
    grid = {"a": [1, 2], "b": ["x", "y"]}
    assert backtest.expand_grid(grid) == backtest.expand_grid(grid)
    assert len(backtest.expand_grid(grid)) == 4
    assert backtest.expand_grid({}) == [{}]


def test_a_sweep_runs_every_combination(universe, repository):
    results = backtest.run_parameter_sweep(
        universe,
        {"execution.default_spread_bps": [1.0, 8.0]},
        repository=repository,
    )
    assert len(results) == 2
    assert all(r.ok for r in results)

    table = backtest.sweep_frame(results)
    assert len(table) == 2
    assert "sharpe_ratio" in table.columns


def test_a_wider_spread_costs_more(universe, repository):
    """A sanity check that the sweep is actually changing the simulation."""
    results = backtest.run_parameter_sweep(
        universe,
        {"execution.default_spread_bps": [1.0, 20.0]},
        repository=repository,
    )
    cheap, dear = results[0], results[1]
    assert cheap.cost_breakdown["spread"] < dear.cost_breakdown["spread"]


def test_a_scenario_set_can_be_named(universe, repository):
    results = backtest.run_scenario_set(
        {
            "costs_on": universe,
            "costs_off": backtest.apply_overrides(
                universe, {"execution.default_spread_bps": 0.0}
            ),
        },
        repository=repository,
    )
    assert [r.label for r in results] == ["costs_on", "costs_off"]


def test_a_failing_run_returns_an_error_rather_than_raising(repository):
    """One bad point in a sweep must not lose the other ninety-nine."""
    result = backtest.run_backtest(
        BacktestConfig(tickers=["NOT_IN_THE_LAKE"]), repository=repository
    )
    assert not result.ok
    assert result.error


def test_too_few_trades_is_a_warning():
    warnings = backtest._warnings(
        {"trade_count": 3, "sharpe_ratio": 1.0},
        {"cost_share_of_gross": 0.1},
        backtest.WarningThresholds(),
    )
    assert any("completed trades" in w for w in warnings)


def test_an_implausible_sharpe_is_a_warning():
    warnings = backtest._warnings(
        {"trade_count": 500, "sharpe_ratio": 6.0},
        {"cost_share_of_gross": 0.1},
        backtest.WarningThresholds(),
    )
    assert any("look-ahead" in w for w in warnings)


def test_heavy_gapping_is_a_warning():
    warnings = backtest._warnings(
        {"trade_count": 500, "sharpe_ratio": 1.0,
         "gap_through": {"gap_share": 0.8}},
        {"cost_share_of_gross": 0.1},
        backtest.WarningThresholds(),
    )
    assert any("gapped through" in w for w in warnings)


def test_a_clean_result_carries_no_warnings():
    warnings = backtest._warnings(
        {
            "trade_count": 500,
            "sharpe_ratio": 1.2,
            "gap_through": {"gap_share": 0.05},
            "leverage": {"leverage_cap": 5.0, "mean_leverage": 1.0,
                         "cap_breaches": 0},
            "drawdown": {"max_drawdown_fraction": -0.1},
        },
        {"cost_share_of_gross": 0.1},
        backtest.WarningThresholds(),
    )
    assert warnings == ()


# --------------------------------------------------------------------------- #
# Walk-forward
# --------------------------------------------------------------------------- #
def test_generated_windows_never_overlap():
    """The property the whole method rests on."""
    windows = wf.rolling_date_windows(
        "2022-01-01", "2025-01-01", train="365D", test="90D"
    )
    assert windows

    for window in windows:
        assert window.train_end <= window.test_start
        assert window.train_start < window.train_end
        assert window.test_start < window.test_end

    for earlier, later in zip(windows, windows[1:]):
        assert later.test_start >= earlier.test_start


def test_rolling_test_ranges_tile_without_gaps_or_overlap():
    windows = wf.rolling_date_windows(
        "2022-01-01", "2025-01-01", train="365D", test="90D"
    )
    for earlier, later in zip(windows, windows[1:]):
        assert later.test_start == earlier.test_end


def test_anchored_windows_share_a_start():
    windows = wf.anchored_date_windows(
        "2022-01-01", "2025-01-01", train="365D", test="90D"
    )
    assert len({w.train_start for w in windows}) == 1
    assert windows[-1].train_end > windows[0].train_end


def test_an_overlapping_window_is_refused():
    """Scoring on what selected it is the failure this method prevents."""
    with pytest.raises(ValueError, match="overlapping split"):
        wf.DateWindow(
            0,
            pd.Timestamp("2024-01-01"),
            pd.Timestamp("2024-06-01"),
            pd.Timestamp("2024-03-01"),   # inside the training range
            pd.Timestamp("2024-09-01"),
        )


def test_an_empty_range_is_refused():
    with pytest.raises(ValueError, match="empty training range"):
        wf.DateWindow(
            0, pd.Timestamp("2024-06-01"), pd.Timestamp("2024-01-01"),
            pd.Timestamp("2024-06-01"), pd.Timestamp("2024-09-01"),
        )


def test_slicing_keeps_only_the_requested_range():
    frame = synthetic_frame(bars=300)
    start = frame.equity_curve.index[100]
    end = frame.equity_curve.index[200]
    sliced = wf.slice_execution(frame, start, end)

    assert sliced.equity_curve.index.min() >= start
    assert sliced.equity_curve.index.max() < end
    assert len(sliced.equity_curve) == 100


def test_walkforward_evaluates_every_window(universe, repository):
    windows = wf.rolling_date_windows(
        "2022-01-03", "2024-06-01", train="400D", test="200D"
    )
    assert windows, "the fixture lake must span enough time"

    results = wf.run_walkforward(universe, windows, repository=repository)

    assert len(results) == len(windows)
    assert any(r.ok for r in results)
    for result in results:
        if result.ok:
            assert "sharpe_ratio" in result.train_metrics
            assert "sharpe_ratio" in result.test_metrics


def test_walkforward_needs_windows(universe, repository):
    with pytest.raises(ValueError, match="at least one window"):
        wf.run_walkforward(universe, [], repository=repository)


def test_the_stability_summary_reports_drift(universe, repository):
    windows = wf.rolling_date_windows(
        "2022-01-03", "2024-06-01", train="400D", test="200D"
    )
    results = wf.run_walkforward(universe, windows, repository=repository)
    summary = wf.stability_summary(results)

    assert not summary.empty
    assert "mean_drift" in summary.columns
    assert "sharpe_ratio" in summary.index


# --------------------------------------------------------------------------- #
# Monte Carlo
# --------------------------------------------------------------------------- #
TRIPS = pd.DataFrame({"net_pnl": [120.0, -80.0, 45.0, -30.0, 200.0, -110.0, 60.0]})


def test_resampling_is_reproducible_with_a_seed():
    first = montecarlo.resample_trades(TRIPS, n_paths=50, seed=42)
    second = montecarlo.resample_trades(TRIPS, n_paths=50, seed=42)

    assert len(first) == len(second) == 50
    for a, b in zip(first, second):
        assert np.array_equal(a.pnl, b.pnl)
        assert np.array_equal(a.equity, b.equity)


def test_a_different_seed_gives_different_paths():
    first = montecarlo.resample_trades(TRIPS, n_paths=20, seed=1)
    second = montecarlo.resample_trades(TRIPS, n_paths=20, seed=2)
    assert not np.array_equal(first[0].pnl, second[0].pnl)


def test_every_path_has_the_same_number_of_trades():
    paths = montecarlo.resample_trades(TRIPS, n_paths=10, seed=7)
    assert all(path.pnl.size == len(TRIPS) for path in paths)


def test_shuffling_preserves_the_total_but_not_the_path():
    """The question "was the drawdown bad luck in the ordering?" needs this."""
    paths = montecarlo.resample_trades(TRIPS, n_paths=25, seed=3, replace=False)
    expected = float(TRIPS["net_pnl"].sum())

    for path in paths:
        assert path.pnl.sum() == pytest.approx(expected)
    assert len({tuple(p.pnl) for p in paths}) > 1


def test_sampling_with_replacement_varies_the_total():
    paths = montecarlo.resample_trades(TRIPS, n_paths=40, seed=3, replace=True)
    totals = {round(float(p.pnl.sum()), 6) for p in paths}
    assert len(totals) > 1


def test_the_summary_reports_a_tail_loss():
    paths = montecarlo.resample_trades(
        TRIPS, n_paths=200, seed=11, initial_equity=10_000.0
    )
    summary = montecarlo.compute_montecarlo_metrics(paths)

    assert summary["paths"] == 200
    assert summary["trades_per_path"] == len(TRIPS)
    assert np.isfinite(summary["tail_loss"])
    assert summary["tail_loss"] <= summary["median_return"]
    assert 0.0 <= summary["probability_of_loss"] <= 1.0


def test_the_summary_is_reproducible():
    first = montecarlo.compute_montecarlo_metrics(
        montecarlo.resample_trades(TRIPS, n_paths=100, seed=5)
    )
    second = montecarlo.compute_montecarlo_metrics(
        montecarlo.resample_trades(TRIPS, n_paths=100, seed=5)
    )
    assert first["tail_loss"] == second["tail_loss"]
    assert first["median_drawdown"] == second["median_drawdown"]


def test_drawdown_is_never_positive_on_any_path():
    paths = montecarlo.resample_trades(TRIPS, n_paths=50, seed=9)
    assert all(p.metrics["max_drawdown_fraction"] <= 0 for p in paths)


def test_no_trades_produces_no_paths():
    assert montecarlo.resample_trades(pd.DataFrame(), n_paths=10) == []


def test_a_non_positive_path_count_is_refused():
    with pytest.raises(ValueError, match="n_paths"):
        montecarlo.resample_trades(TRIPS, n_paths=0)


def test_an_empty_summary_does_not_raise():
    assert montecarlo.compute_montecarlo_metrics([])["paths"] == 0


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #
def test_results_are_written_and_read_back(written_lake, scored):
    if not scored.ok:
        pytest.skip(f"pipeline unavailable: {scored.error}")

    store.save_backtest_result(written_lake, "RUN001", scored)

    assert store.has_analytics(written_lake, "RUN001")
    assert store.list_analytics_runs(written_lake) == ["RUN001"]

    read = store.load_run_metrics(written_lake, "RUN001")
    assert read["trade_count"] == scored.trade_count
    assert read["metrics"]["sharpe_ratio"] == pytest.approx(
        scored.metrics["sharpe_ratio"]
    )


def test_reading_a_run_with_no_analytics_returns_none(written_lake):
    assert store.load_run_metrics(written_lake, "NOPE") is None
    assert store.load_montecarlo_summary(written_lake, "NOPE") is None
    assert store.load_sweep(written_lake, "NOPE").empty


def test_a_truncated_document_reads_as_absent_not_as_a_crash(written_lake):
    directory = store.run_directory(written_lake, "BROKEN", create=True)
    (directory / store.ARTEFACTS["metrics"]).write_text("{not json", encoding="utf-8")

    assert store.load_run_metrics(written_lake, "BROKEN") is None


def test_numpy_and_pandas_values_serialise():
    """A metrics document must not fail over one numpy float."""
    payload = store.jsonable(
        {
            "a": np.float64(1.5),
            "b": np.int64(3),
            "c": np.array([1.0, 2.0]),
            "d": pd.Timestamp("2026-01-01", tz="UTC"),
            "e": float("nan"),
            "f": float("inf"),
        }
    )
    assert payload["a"] == 1.5
    assert payload["b"] == 3
    assert payload["c"] == [1.0, 2.0]
    assert payload["d"].startswith("2026-01-01")
    assert payload["e"] is None, "NaN has no JSON spelling"
    assert payload["f"] is None
    json.dumps(payload)


def test_monte_carlo_results_round_trip(written_lake):
    paths = montecarlo.resample_trades(TRIPS, n_paths=30, seed=4)
    summary = montecarlo.compute_montecarlo_metrics(paths)
    store.save_montecarlo(written_lake, "MC001", summary, paths)

    read = store.load_montecarlo_summary(written_lake, "MC001")
    assert read["paths"] == 30
    assert not store.load_montecarlo_paths(written_lake, "MC001").empty


def test_analytics_never_writes_into_a_run_directory(written_lake, scored):
    """A pipeline run is an immutable record of what happened."""
    if not scored.ok:
        pytest.skip(f"pipeline unavailable: {scored.error}")

    store.save_backtest_result(written_lake, "RUN002", scored)
    runs = Path(written_lake.paths.data_root) / "runs"
    assert not runs.exists()


# --------------------------------------------------------------------------- #
# Scoring what is already on disk
# --------------------------------------------------------------------------- #
@pytest.fixture
def recorded_run(tmp_path):
    """A lake with one run persisted by the real scheduler."""
    from cfdinvest.backtest.runner import BacktestConfig as Cfg
    from cfdinvest.schedule.runner import RunConfig
    from cfdinvest.schedule.runner import run_pipeline as run_scheduled

    config = build_lake(tmp_path / "data", bars=260)
    summary = run_scheduled(
        list(TICKERS),
        RunConfig(backtest=Cfg(interval=INTERVAL)),
        repository=DataRepository(config),
        now=pd.Timestamp("2026-08-25 07:00", tz="UTC"),
    )
    assert summary.status == "ok", summary.error
    return config, summary.run_id


def test_a_stored_run_scores_without_running_anything(recorded_run):
    from cfdinvest.analytics import persisted

    config, run_id = recorded_run
    result = persisted.score_run(config, run_id)

    assert result.ok, result.error
    assert result.label == run_id
    assert result.bars > 0
    assert "sharpe_ratio" in result.metrics


def test_scoring_a_stored_run_matches_running_it(recorded_run):
    """Reading and running must not give two different answers."""
    from cfdinvest.analytics import persisted

    config, run_id = recorded_run
    stored = persisted.score_run(config, run_id)
    live = backtest.run_backtest(
        BacktestConfig(tickers=list(TICKERS), interval=INTERVAL),
        repository=DataRepository(config),
    )

    assert stored.metrics["sharpe_ratio"] == pytest.approx(
        live.metrics["sharpe_ratio"]
    )
    assert stored.trade_count == live.trade_count
    assert stored.attribution["total_pnl"] == pytest.approx(
        live.attribution["total_pnl"], abs=1e-6
    )


def test_a_stored_run_uses_the_real_decomposition(recorded_run):
    """Not the reconstruction: the run persisted its own P&L frame."""
    from cfdinvest.analytics import persisted

    config, run_id = recorded_run
    result = persisted.score_run(config, run_id)

    assert not result.attribution.get("reconstructed", False)
    assert sum(
        result.attribution[term] for term in attribution.ATTRIBUTION_TERMS
    ) == pytest.approx(result.attribution["total_pnl"], abs=1e-6)


def test_scoring_a_run_that_does_not_exist(written_lake):
    from cfdinvest.analytics import persisted

    result = persisted.score_run(written_lake, "NOPE")
    assert not result.ok
    assert result.error


def test_scoring_with_no_runs_at_all(written_lake):
    from cfdinvest.analytics import persisted

    result = persisted.score_run(written_lake)
    assert not result.ok
    assert "no run" in result.error


def test_every_stored_run_scores_into_one_table(recorded_run):
    from cfdinvest.analytics import persisted

    config, run_id = recorded_run
    table = persisted.score_every_run(config)

    assert run_id in table.index
    assert "sharpe_ratio" in table.columns


def test_an_execution_frame_rebuilt_from_disk_reports_absences(recorded_run):
    """Exposure is not persisted, so leverage must read as unknown."""
    from cfdinvest.analytics import persisted

    config, run_id = recorded_run
    frame = persisted.execution_frame_from_run(config, run_id)

    assert not frame.trades.empty
    assert not frame.pnl.empty
    assert frame.exposure.empty
    assert np.isnan(metrics.compute_leverage_stats(frame)["mean_leverage"])


# --------------------------------------------------------------------------- #
# Sharing one analysis frame across a sweep
# --------------------------------------------------------------------------- #
def test_a_grid_of_execution_settings_can_share_the_analysis():
    assert backtest.shares_analysis({"execution.max_leverage": [2, 5]})
    assert backtest.shares_analysis({"signal.min_size": [0.1, 0.2]})
    assert backtest.shares_analysis({})


def test_a_grid_that_changes_the_analysis_cannot_share_it():
    """Reusing it would score every point against the first point's data."""
    assert not backtest.shares_analysis({"preprocess.volatility_window": [21, 63]})
    assert not backtest.shares_analysis({"tickers": [["SPY"], ["GLD"]]})
    assert not backtest.shares_analysis({"start": ["2022-01-01", "2023-01-01"]})
    assert not backtest.shares_analysis({"interval": ["1d", "1h"]})


def test_sharing_the_analysis_gives_identical_results(universe, repository):
    """The optimisation must be invisible in the numbers."""
    grid = {"execution.default_spread_bps": [1.0, 8.0]}
    shared = backtest.run_parameter_sweep(universe, grid, repository=repository)

    slow = [
        backtest.run_backtest(universe, repository=repository, overrides=point)
        for point in backtest.expand_grid(grid)
    ]

    for fast, full in zip(shared, slow):
        assert fast.metrics["sharpe_ratio"] == pytest.approx(
            full.metrics["sharpe_ratio"]
        )
        assert fast.trade_count == full.trade_count


def test_the_analysis_is_only_shared_when_the_grid_allows_it(
    universe, repository, monkeypatch
):
    """The guard, tested where it acts rather than by its downstream effect.

    Whether two preprocessing settings happen to produce different trades is
    a property of the strategy, not of this optimisation — so asserting on
    that would pass or fail for reasons unrelated to the guard. What matters
    is that the shared frame is not built at all when the grid varies what
    the frame is made of.
    """
    built: list[str] = []
    real = backtest._analysis_for

    def watched(repo, config):
        built.append("built")
        return real(repo, config)

    monkeypatch.setattr(backtest, "_analysis_for", watched)

    backtest.run_parameter_sweep(
        universe, {"execution.default_spread_bps": [1.0, 8.0]},
        repository=repository,
    )
    assert built == ["built"], "an execution grid should share one frame"

    built.clear()
    backtest.run_parameter_sweep(
        universe, {"preprocess.volatility_window": [21, 84]},
        repository=repository,
    )
    assert built == [], "a preprocessing grid must not share a frame"


def test_a_single_point_sweep_does_not_bother_sharing(
    universe, repository, monkeypatch
):
    """Building a frame to use once costs more than it saves."""
    built: list[str] = []
    monkeypatch.setattr(
        backtest, "_analysis_for", lambda *a: built.append("built") or None
    )
    backtest.run_parameter_sweep(
        universe, {"execution.default_spread_bps": [5.0]}, repository=repository
    )
    assert built == []


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #
def test_the_parser_accepts_options_after_the_subcommand():
    """Every example of this tool writes them that way."""
    args = cli.build_parser().parse_args(
        ["run-once", "--tickers", "SPY,GLD", "--run-id", "X1"]
    )
    assert args.command == "run-once"
    assert args.tickers == "SPY,GLD"
    assert args.run_id == "X1"


def test_the_parser_also_accepts_options_before_it():
    """A subparser default must not overwrite what the parent already parsed."""
    args = cli._with_defaults(
        cli.build_parser().parse_args(["--tickers", "SPY", "run-once"])
    )
    assert args.tickers == "SPY"


def test_unspecified_options_still_get_their_defaults():
    args = cli._with_defaults(cli.build_parser().parse_args(["run-once"]))
    assert args.log_level == "INFO"
    assert args.tickers is None
    assert args.json is False


def test_a_run_id_is_a_utc_timestamp():
    assert cli.generate_run_id(pd.Timestamp("2026-08-24 09:00", tz="UTC")) == (
        "20260824T090000Z"
    )


def test_run_once_scores_a_lake(lake, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: lake)
    code = cli.main(
        ["run-once", "--tickers", ",".join(TICKERS), "--run-id", "CLI001",
         "--log-level", "ERROR"]
    )
    assert code == 0
    assert "CLI001" in capsys.readouterr().out
    assert store.load_run_metrics(lake, "CLI001") is not None


def test_sweep_writes_a_table(lake, monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: lake)
    code = cli.main(
        ["sweep", "--tickers", ",".join(TICKERS), "--run-id", "CLI002",
         "--grid", '{"execution.default_spread_bps": [1.0, 5.0]}',
         "--log-level", "ERROR"]
    )
    assert code == 0
    assert len(store.load_sweep(lake, "CLI002")) == 2


def test_montecarlo_writes_a_distribution(lake, monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: lake)
    code = cli.main(
        ["montecarlo", "--tickers", ",".join(TICKERS), "--run-id", "CLI003",
         "--paths", "50", "--log-level", "ERROR"]
    )
    assert code == 0
    summary = store.load_montecarlo_summary(lake, "CLI003")
    assert summary["paths"] == 50


def test_walkforward_writes_windows(lake, monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: lake)
    code = cli.main(
        ["walkforward", "--tickers", ",".join(TICKERS), "--run-id", "CLI004",
         "--train", "400D", "--test", "200D", "--log-level", "ERROR"]
    )
    assert code == 0
    assert not store.load_walkforward_windows(lake, "CLI004").empty


def test_a_bad_grid_is_refused(lake, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: lake)
    code = cli.main(["sweep", "--grid", "[1, 2]", "--log-level", "ERROR"])
    assert code == 2
    assert "JSON object" in capsys.readouterr().err


def test_an_unknown_universe_exits_non_zero(lake, monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: lake)
    assert cli.main(
        ["run-once", "--tickers", "NOT_REAL", "--log-level", "ERROR"]
    ) == 1


# --------------------------------------------------------------------------- #
# The read-only contract
# --------------------------------------------------------------------------- #
def test_analytics_writes_nowhere_but_its_own_directory(lake):
    """Everything the tool produced this session lives under analytics/."""
    root = Path(lake.paths.data_root)
    assert (root / "analytics").is_dir()
    assert not (root / "runs").exists()


def test_window_bounds_come_from_the_lake_not_from_nothing(lake, repository):
    """The sweep window must read the lake's real span.

    ``_period`` used to look for manifest columns named "start" and "end".
    No ingestor writes either, so on a real lake both lookups missed and it
    returned ``(None, None)`` -- and because "no bounds" is an acceptable
    answer here, nothing ever complained. The windows were simply built over
    a range nobody chose.
    """
    args = cli._with_defaults(cli.build_parser().parse_args(["run-once"]))
    args.start = None
    args.end = None

    first, last = cli._period(args, repository, lake.market)

    assert first is not None and last is not None, (
        "the lake has a date span and _period returned nothing"
    )
    assert first < last
    # And it must agree with the repository, which owns this.
    assert (first, last) == repository.coverage_span(lake.market.interval)
