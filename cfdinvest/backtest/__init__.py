"""Backtesting and analytics: run the pipeline, then judge the result.

The two halves are kept apart on purpose. :mod:`runner` orchestrates and
owns no modelling; :mod:`metrics` evaluates and touches no simulator
parameter. Analytics that can reach back into the simulator stop being
analytics and become curve-fitting with extra steps.

    runner       lake -> AnalysisFrame -> RegimeFrame -> SignalFrame -> ExecutionFrame
    metrics      equity, risk-adjusted and trade-level figures
    walkforward  per-window re-execution and a stability report
    montecarlo   seeded resampling of trades or returns
    frame        BacktestFrame, the single output
"""

from cfdinvest.backtest.frame import (
    BacktestFrame,
    evaluate,
    run_backtest_from_analysis,
    run_backtest_pipeline,
)
from cfdinvest.backtest.metrics import (
    MetricsConfig,
    compute_metrics,
    equity_metrics,
    round_trips,
    trade_metrics,
)
from cfdinvest.backtest.montecarlo import (
    MonteCarloConfig,
    resample_returns,
    resample_trades,
    summarise_simulations,
)
from cfdinvest.backtest.runner import (
    BacktestConfig,
    PipelineResult,
    run_from_analysis,
    run_pipeline,
)
from cfdinvest.backtest.walkforward import (
    Window,
    anchored_windows,
    rolling_windows,
    stability_report,
    walk_forward,
)

__all__ = [
    "BacktestConfig",
    "BacktestFrame",
    "MetricsConfig",
    "MonteCarloConfig",
    "PipelineResult",
    "Window",
    "run_backtest_pipeline",
    "run_backtest_from_analysis",
    "run_pipeline",
    "run_from_analysis",
    "evaluate",
    "compute_metrics",
    "equity_metrics",
    "trade_metrics",
    "round_trips",
    "rolling_windows",
    "anchored_windows",
    "walk_forward",
    "stability_report",
    "resample_trades",
    "resample_returns",
    "summarise_simulations",
]
