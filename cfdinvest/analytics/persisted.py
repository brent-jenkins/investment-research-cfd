"""Scoring a completed run without running anything.

Once the pipeline persists its trades, equity and per-bar decomposition,
scoring a past run is a read. This module does that read and hands back
something the metrics and attribution functions already understand, so a
question about a run from three months ago costs a few parquet files rather
than a full simulation.

**It reconstructs nothing.** Whatever the run wrote is what you get. A run
from before the persistence layer carries trades and equity but no
decomposition, and the attribution then falls back to the reconstruction —
which says so, rather than quietly producing a different set of numbers under
the same name.

Where you have confirmed real fills, those positions are scored in preference
to the simulated ones, and ``metadata["positions_source"]`` says which was
used. The two are genuinely different claims about the same run.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

from cfdinvest.analytics import attribution as attribution_module
from cfdinvest.analytics import metrics as metrics_module
from cfdinvest.analytics.backtest import BacktestResult, WarningThresholds, _warnings
from cfdinvest.execution.frame import ExecutionFrame

logger = logging.getLogger(__name__)

__all__ = [
    "execution_frame_from_run",
    "score_run",
    "score_every_run",
]


def execution_frame_from_run(config, run_id: str | None = None) -> ExecutionFrame:
    """Rebuild an :class:`ExecutionFrame` from what a run persisted.

    Only the fields that were actually written are populated. The rest keep
    their empty defaults, which is what lets the metric functions report
    "not computable" instead of a number nobody stored — ``exposure``, for
    instance, is not persisted, so leverage statistics come back as NaN
    rather than as zero.
    """
    from cfdinvest.persist import readers

    chosen = run_id or readers.load_latest_run(config)
    if chosen is None:
        return ExecutionFrame()

    parts = readers.load_execution_frame(chosen, config)
    equity = parts.get("equity_curve")
    if equity is None or equity.empty:
        equity = pd.Series(dtype="float64", name="equity")

    metadata = dict(readers.load_execution_metadata(chosen, config) or {})
    metadata.setdefault("run_id", chosen)
    # Which book was scored. The reader prefers your confirmed fills where you
    # recorded any, and a metric computed on confirmed positions is a
    # different claim from the same metric on simulated ones -- so the answer
    # carries which it was rather than leaving the caller to guess.
    metadata["positions_source"] = parts.get("positions_source", "simulated")

    return ExecutionFrame(
        positions=parts.get("positions", pd.DataFrame()),
        trades=parts.get("trades", pd.DataFrame()),
        stop_events=parts.get("stop_events", pd.DataFrame()),
        pnl=readers.load_pnl_frame(chosen, config),
        equity_curve=equity,
        metadata=metadata,
    )


def score_run(
    config,
    run_id: str | None = None,
    *,
    metrics_config: metrics_module.MetricsConfig | None = None,
    thresholds: WarningThresholds | None = None,
) -> BacktestResult:
    """Score a completed run from disk. Runs no pipeline and writes nothing.

    The same :class:`~cfdinvest.analytics.backtest.BacktestResult` a live run
    produces, so a stored run and a fresh one are interchangeable everywhere
    downstream — a sweep table, the dashboard, a comparison.
    """
    from cfdinvest.persist import readers

    chosen = run_id or readers.load_latest_run(config)
    if chosen is None:
        return BacktestResult(
            label="persisted",
            error="no run has been recorded yet",
        )

    execution = execution_frame_from_run(config, chosen)
    if execution.equity_curve.empty:
        return BacktestResult(
            label=chosen,
            error=f"run {chosen} persisted no equity curve",
        )

    metadata = readers.load_run_metadata(chosen, config) or {}
    computed = metrics_module.compute_all_metrics(execution, config=metrics_config)

    # The engine's own decomposition where the run stored one; the
    # reconstruction from the trade log where it did not. Both sum to the
    # total exactly -- the difference is whether the overnight gap can be
    # separated from the intraday move.
    if execution.pnl is not None and not execution.pnl.empty:
        pnl_attribution = attribution_module.compute_pnl_attribution(execution)
        costs = attribution_module.compute_cost_breakdown(execution)
    else:
        pnl_attribution = attribution_module.attribution_from_trades(
            execution.trades, execution.equity_curve
        )
        costs = attribution_module.cost_breakdown_from_trades(
            execution.trades, execution.equity_curve
        )

    equity = execution.equity_curve
    return BacktestResult(
        label=chosen,
        config_snapshot=dict(metadata.get("config_snapshot") or {}),
        metrics=computed,
        attribution=pnl_attribution,
        cost_breakdown=costs,
        trade_count=int(computed.get("trade_count", 0) or 0),
        period=(
            equity.index.min() if len(equity) else None,
            equity.index.max() if len(equity) else None,
        ),
        bars=int(len(equity)),
        universe=tuple(execution.positions.columns),
        equity_curve=equity,
        trades=execution.trades,
        warnings=tuple(
            [*_warnings(computed, costs, thresholds or WarningThresholds()),
             *(metadata.get("warnings") or [])]
        ),
    )


def score_every_run(
    config,
    *,
    limit: int | None = 20,
    metrics_config: metrics_module.MetricsConfig | None = None,
) -> pd.DataFrame:
    """Score the stored runs and put them in one table, newest first.

    How a strategy's *recorded* results have moved over time, as opposed to
    how one backtest scores today. Runs that persisted nothing usable appear
    with their error rather than being dropped: a table that silently omits
    the broken runs hides the shape of the breakage.
    """
    from cfdinvest.persist import readers

    runs = readers.list_runs(config)
    if limit is not None:
        runs = runs[:limit]
    if not runs:
        return pd.DataFrame(columns=["run", "sharpe_ratio", "trades", "error"])

    rows = []
    for run_id in runs:
        result = score_run(config, run_id, metrics_config=metrics_config)
        row = dict(result.headline)
        row["run"] = run_id
        row["error"] = result.error
        row["period_start"], row["period_end"] = result.period
        rows.append(row)

    return pd.DataFrame(rows).set_index("run")
