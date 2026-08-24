"""The :class:`BacktestFrame`: a run and its evaluation, together.

Holds references to every frame the pipeline produced, so a metric can
always be traced back to the trades and the signals that caused it. That
traceability is the point — a Sharpe ratio with no path back to the trade
log is a number you have to take on faith.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from cfdinvest.backtest import montecarlo as montecarlo_module
from cfdinvest.backtest import walkforward as walkforward_module
from cfdinvest.backtest.metrics import (
    MetricsConfig,
    attribute_financing,
    compute_metrics,
    round_trips,
)
from cfdinvest.backtest.montecarlo import MonteCarloConfig
from cfdinvest.backtest.runner import (
    BacktestConfig,
    PipelineResult,
    pipeline_metadata,
    run_from_analysis,
    run_pipeline,
)

logger = logging.getLogger(__name__)

__all__ = ["BacktestFrame", "run_backtest_pipeline", "evaluate"]


@dataclass(frozen=True, slots=True)
class BacktestFrame:
    """A completed run: its metrics, its windows, and the frames behind them.

    Attributes
    ----------
    metrics:
        Headline figures over the whole run.
    window_metrics:
        One row per walk-forward window; empty when walk-forward was not
        requested.
    stability:
        How much each metric moved across windows.
    monte_carlo:
        Percentiles of the resampled outcome distribution; empty when not
        requested.
    analysis, regimes, signals, execution:
        The frames the run produced, kept so any number can be traced back.
    metadata:
        Universe, calendar bounds and the settings that produced the run.
    """

    metrics: dict[str, Any] = field(default_factory=dict)
    window_metrics: pd.DataFrame = field(default_factory=pd.DataFrame)
    stability: pd.DataFrame = field(default_factory=pd.DataFrame)
    monte_carlo: pd.DataFrame = field(default_factory=pd.DataFrame)
    analysis: Any = None
    regimes: Any = None
    signals: Any = None
    execution: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)

    # -- views ------------------------------------------------------------ #
    @property
    def equity_curve(self) -> pd.Series:
        """The simulated account value."""
        if self.execution is None:
            return pd.Series(dtype="float64", name="equity")
        return self.execution.equity_curve

    @property
    def trades(self) -> pd.DataFrame:
        """The execution trade log."""
        if self.execution is None:
            return pd.DataFrame()
        return self.execution.trades

    def round_trips(self) -> pd.DataFrame:
        """Completed entry-to-exit trades, net of apportioned financing.

        The financing column is the portfolio total spread across trips by
        notional-days; see
        :func:`~cfdinvest.backtest.metrics.attribute_financing`.
        """
        if self.execution is None:
            return pd.DataFrame()

        trips = round_trips(self.execution.trades, self.equity_curve.index)
        pnl = self.execution.pnl
        if not pnl.empty and not trips.empty:
            trips = attribute_financing(trips, float(-pnl["financing"].sum()))
        return trips

    def summary(self) -> pd.Series:
        """The headline metrics as a Series, for printing."""
        headline = (
            "total_return", "cagr", "annualised_volatility", "sharpe", "sortino",
            "calmar", "max_drawdown_fraction", "round_trips", "win_rate",
            "expectancy", "average_r", "median_bars_held", "cost_drag",
        )
        return pd.Series(
            {name: self.metrics.get(name) for name in headline if name in self.metrics},
            dtype="object",
        )

    def report(self) -> str:
        """A short human-readable summary of the run."""
        if not self.metrics:
            return "empty backtest"

        parts = [
            f"{self.metadata.get('bars', 0)} bars",
            f"{self.metrics.get('round_trips', 0)} round trips",
            f"return {self.metrics.get('total_return', float('nan')):.2%}",
            f"CAGR {self.metrics.get('cagr', float('nan')):.2%}",
            f"Sharpe {self.metrics.get('sharpe', float('nan')):.2f}",
            f"maxDD {self.metrics.get('max_drawdown_fraction', float('nan')):.2%}",
        ]
        return " | ".join(parts)


def evaluate(
    result: PipelineResult,
    config: BacktestConfig | None = None,
    *,
    windows: list | None = None,
    monte_carlo: MonteCarloConfig | None = None,
) -> BacktestFrame:
    """Evaluate a pipeline result without re-running it.

    Pure with respect to the frames it reads. Separated from the runner so a
    result can be re-scored under different metric conventions — a different
    risk-free rate, a different ``r_basis`` — without paying for the
    simulation again, and without any possibility of the scoring changing
    what was simulated.

    Parameters
    ----------
    result:
        Output of :func:`~cfdinvest.backtest.runner.run_pipeline`.
    windows:
        Optional walk-forward splits from
        :mod:`~cfdinvest.backtest.walkforward`.
    monte_carlo:
        Optional resampling settings. ``None`` skips it.

    Returns
    -------
    BacktestFrame
    """
    settings = config or BacktestConfig()

    metrics = (
        compute_metrics(result.execution, settings.metrics)
        if result.execution is not None
        else {}
    )

    window_metrics = pd.DataFrame()
    stability = pd.DataFrame()
    if windows:
        window_metrics = walkforward_module.walk_forward(
            result.analysis,
            result.signals,
            windows,
            settings.execution,
            settings.metrics,
        )
        stability = walkforward_module.stability_report(window_metrics)

    simulations = pd.DataFrame()
    if monte_carlo is not None and result.execution is not None:
        trips = round_trips(
            result.execution.trades, result.execution.equity_curve.index
        )
        # Resample P&L that is net of carry. Bootstrapping gross-of-financing
        # trades would describe a strategy that never paid to hold anything,
        # and can put the probability of profit near 1 on a run that barely
        # broke even.
        pnl = result.execution.pnl
        if not pnl.empty and not trips.empty:
            trips = attribute_financing(trips, float(-pnl["financing"].sum()))
        initial = settings.execution.initial_equity
        simulations = montecarlo_module.summarise_simulations(
            montecarlo_module.resample_trades(trips, initial, monte_carlo),
            monte_carlo,
        )

    metadata = pipeline_metadata(result, settings)
    metadata["walk_forward_windows"] = len(window_metrics)
    metadata["monte_carlo_simulations"] = (
        monte_carlo.simulations if monte_carlo is not None else 0
    )
    metadata["metrics_convention"] = {
        "risk_free_rate": settings.metrics.risk_free_rate,
        "target_return": settings.metrics.target_return,
        "r_basis": settings.metrics.r_basis,
        "risk_fraction": settings.metrics.risk_fraction,
    }

    return BacktestFrame(
        metrics=metrics,
        window_metrics=window_metrics,
        stability=stability,
        monte_carlo=simulations,
        analysis=result.analysis,
        regimes=result.regimes,
        signals=result.signals,
        execution=result.execution,
        metadata=metadata,
    )


def run_backtest_pipeline(
    repository,
    config: BacktestConfig | None = None,
    *,
    windows: list | None = None,
    monte_carlo: MonteCarloConfig | None = None,
) -> BacktestFrame:
    """Run the whole pipeline from the lake and evaluate it.

    The one-call entry point. Reads through the repository's public API and
    writes nothing.

    Returns
    -------
    BacktestFrame
    """
    settings = config or BacktestConfig()
    result = run_pipeline(repository, settings)
    return evaluate(result, settings, windows=windows, monte_carlo=monte_carlo)


def run_backtest_from_analysis(
    analysis_frame,
    config: BacktestConfig | None = None,
    *,
    windows: list | None = None,
    monte_carlo: MonteCarloConfig | None = None,
) -> BacktestFrame:
    """Run and evaluate from an existing AnalysisFrame, without I/O."""
    settings = config or BacktestConfig()
    result = run_from_analysis(analysis_frame, settings)
    return evaluate(result, settings, windows=windows, monte_carlo=monte_carlo)
