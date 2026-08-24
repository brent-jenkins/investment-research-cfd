"""Pipeline runner: lake to equity curve in one call.

Orchestration only. Every stage is the layer that owns it, called through
its public entry point; this module adds no modelling of its own, which is
what keeps the pipeline honest — there is nowhere here to quietly adjust a
threshold because the result looked disappointing.

    repository -> AnalysisFrame -> RegimeFrame -> SignalFrame -> ExecutionFrame

The split between :func:`run_pipeline` and :func:`run_from_analysis` matters
more than it looks. Preprocessing and regime classification are causal but
expensive; execution is path-dependent and cheap. Walk-forward and parameter
sweeps therefore run the first half once and the second half many times,
which is only possible because the boundary is a function argument rather
than a step inside one big procedure.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Sequence

import pandas as pd

from cfdinvest.execution import ExecutionConfig, run_backtest
from cfdinvest.preprocessing import (
    PreprocessConfig,
    analysis_frame_from_repository,
    build_analysis_frame,
)
from cfdinvest.regimes import RegimeConfig, classify_regimes
from cfdinvest.signals import SignalConfig, generate_signals
from cfdinvest.backtest.metrics import MetricsConfig

logger = logging.getLogger(__name__)

__all__ = ["BacktestConfig", "PipelineResult", "run_pipeline", "run_from_analysis"]


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    """Universe, date range and every layer's settings in one object."""

    # -- what to run over -------------------------------------------------- #
    tickers: Sequence[str] | None = None
    series_ids: Sequence[str] | None = None
    interval: str | None = None
    start: str | pd.Timestamp | None = None
    end: str | pd.Timestamp | None = None

    # -- per-layer settings ------------------------------------------------ #
    preprocess: PreprocessConfig = field(default_factory=PreprocessConfig)
    regime: RegimeConfig = field(default_factory=RegimeConfig)
    signal: SignalConfig = field(default_factory=SignalConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    metrics: MetricsConfig = field(default_factory=MetricsConfig)

    #: Run without regime filtering. Useful for isolating the trend engine;
    #: not the production path, and recorded in metadata so a result cannot
    #: be mistaken for a filtered one.
    use_regimes: bool = True


@dataclass(frozen=True, slots=True)
class PipelineResult:
    """The four frames a pipeline run produces."""

    analysis: Any = None
    regimes: Any = None
    signals: Any = None
    execution: Any = None


def run_from_analysis(
    analysis_frame,
    config: BacktestConfig | None = None,
) -> PipelineResult:
    """Run regimes, signals and execution over an existing AnalysisFrame.

    Pure with respect to its input: the analysis frame is read, never
    mutated. This is the entry point walk-forward and tests use, because it
    skips the expensive causal stages that only need computing once.

    Returns
    -------
    PipelineResult
    """
    settings = config or BacktestConfig()

    regimes = (
        classify_regimes(analysis_frame, settings.regime)
        if settings.use_regimes
        else None
    )
    signals = generate_signals(analysis_frame, regimes, settings.signal)
    execution = run_backtest(signals, analysis_frame, settings.execution)

    return PipelineResult(
        analysis=analysis_frame,
        regimes=regimes,
        signals=signals,
        execution=execution,
    )


def run_pipeline(repository, config: BacktestConfig | None = None) -> PipelineResult:
    """Run the whole pipeline from the Parquet lake.

    The only function in this package that performs I/O. It reads through
    :class:`~cfdinvest.repository.DataRepository`'s public API and writes
    nothing.

    Parameters
    ----------
    repository:
        A :class:`~cfdinvest.repository.DataRepository`.
    config:
        Universe, date range and per-layer settings.

    Returns
    -------
    PipelineResult
    """
    settings = config or BacktestConfig()

    analysis = analysis_frame_from_repository(
        repository,
        settings.tickers,
        settings.series_ids,
        interval=settings.interval,
        start=settings.start,
        end=settings.end,
        config=settings.preprocess,
    )

    if analysis.close.empty:
        logger.warning(
            "No market data for %s in [%s, %s]; the pipeline has nothing to run",
            settings.tickers or "the configured universe",
            settings.start,
            settings.end,
        )

    return run_from_analysis(analysis, settings)


def pipeline_metadata(result: PipelineResult, config: BacktestConfig) -> dict:
    """Summarise what a run actually covered.

    Recorded because the alternative is inferring it from the numbers later,
    and a metric computed over a different universe or a shorter window is
    not comparable to one that was not.
    """
    analysis = result.analysis
    execution = result.execution

    metadata: dict[str, Any] = {
        "tickers": tuple(analysis.close.columns) if analysis is not None else (),
        "series_ids": analysis.metadata.get("series_ids", ()) if analysis else (),
        "interval": analysis.metadata.get("interval") if analysis else None,
        "requested_start": config.start,
        "requested_end": config.end,
        "use_regimes": config.use_regimes,
    }

    if analysis is not None and len(analysis.calendar):
        metadata["calendar_start"] = analysis.calendar.min()
        metadata["calendar_end"] = analysis.calendar.max()
        metadata["bars"] = len(analysis.calendar)

    if execution is not None:
        metadata["trades"] = len(execution.trades)
        metadata["execution_lag"] = execution.metadata.get("execution_lag")
        metadata["fill_mode"] = execution.metadata.get("fill_mode")

    if result.signals is not None:
        metadata["active_signal_bars"] = result.signals.metadata.get("active_bars")

    return metadata
