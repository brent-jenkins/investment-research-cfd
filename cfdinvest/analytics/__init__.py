"""Analytics and backtesting utilities over completed pipeline runs.

Scoring, attribution, parameter sweeps, walk-forward and trade resampling.
Everything here **reads**: it opens the lake through the existing repository,
runs the existing pipeline, and writes only into ``<data_root>/analytics/``.
No execution behaviour changes, and nothing reaches the network.

The maths for Sharpe, Sortino, Calmar, drawdown, win rate, expectancy and R
multiples lives in :mod:`cfdinvest.backtest.metrics` and is *delegated to*,
not reimplemented — a second implementation is not a second opinion, it is a
second number that can disagree with the first while both claim to be the
Sharpe ratio.
"""

from __future__ import annotations

from cfdinvest.analytics.attribution import (
    compute_cost_breakdown,
    compute_currency_attribution,
    compute_instrument_attribution,
    compute_pnl_attribution,
    compute_return_attribution,
)
from cfdinvest.analytics.backtest import (
    BacktestResult,
    WarningThresholds,
    run_backtest,
    run_parameter_sweep,
    run_scenario_set,
    sweep_frame,
)
from cfdinvest.analytics.metrics import (
    MetricsConfig,
    compute_all_metrics,
    compute_avg_r,
    compute_calmar_ratio,
    compute_expectancy,
    compute_fx_exposure_stats,
    compute_gap_through_stats,
    compute_leverage_stats,
    compute_max_drawdown,
    compute_r_distribution,
    compute_sharpe_ratio,
    compute_sortino_ratio,
    compute_trade_duration_stats,
    compute_win_rate,
)
from cfdinvest.analytics.montecarlo import (
    ResampledPath,
    compute_montecarlo_metrics,
    resample_trades,
)
from cfdinvest.analytics.persisted import (
    execution_frame_from_run,
    score_every_run,
    score_run,
)
from cfdinvest.analytics.store import (
    load_montecarlo_summary,
    load_run_attribution,
    load_run_metrics,
    load_walkforward_summary,
)
from cfdinvest.analytics.walkforward import (
    DateWindow,
    WalkforwardWindowResult,
    anchored_date_windows,
    rolling_date_windows,
    run_walkforward,
    stability_summary,
)

__all__ = [
    "MetricsConfig",
    "compute_all_metrics",
    "compute_sharpe_ratio",
    "compute_sortino_ratio",
    "compute_max_drawdown",
    "compute_calmar_ratio",
    "compute_win_rate",
    "compute_expectancy",
    "compute_avg_r",
    "compute_r_distribution",
    "compute_trade_duration_stats",
    "compute_leverage_stats",
    "compute_gap_through_stats",
    "compute_fx_exposure_stats",
    "compute_pnl_attribution",
    "compute_return_attribution",
    "compute_cost_breakdown",
    "compute_currency_attribution",
    "compute_instrument_attribution",
    "BacktestResult",
    "WarningThresholds",
    "run_backtest",
    "run_parameter_sweep",
    "run_scenario_set",
    "sweep_frame",
    "DateWindow",
    "WalkforwardWindowResult",
    "rolling_date_windows",
    "anchored_date_windows",
    "run_walkforward",
    "stability_summary",
    "ResampledPath",
    "resample_trades",
    "compute_montecarlo_metrics",
    "score_run",
    "score_every_run",
    "execution_frame_from_run",
    "load_run_metrics",
    "load_run_attribution",
    "load_walkforward_summary",
    "load_montecarlo_summary",
]
