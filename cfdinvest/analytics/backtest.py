"""Running backtests, sweeping parameters, and scoring what comes out.

Three levels of the same operation: one configuration, a grid of them, or an
explicit list. All of them go through
:func:`cfdinvest.backtest.runner.run_pipeline`, which is the only path that
touches the lake and which this package does not modify. Nothing here changes
how a simulation behaves; it decides *which* simulations to run and what to
record about them.

**Warnings are part of the result, not a log line.** A backtest that ran
cleanly and reported a Sharpe of 3 on eleven trades is not a good result, it
is a result nobody should act on. The conditions that make a number
untrustworthy are enumerable, so they are enumerated: too few trades, an
implausible Sharpe, exits that mostly gapped, leverage against the cap,
currency costs eating the edge. Every :class:`BacktestResult` carries them.

Runs are deterministic given the lake and the configuration.
"""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

from cfdinvest.analytics import attribution as attribution_module
from cfdinvest.analytics import metrics as metrics_module
from cfdinvest.backtest.runner import BacktestConfig, run_pipeline
from cfdinvest.repository import DataRepository

logger = logging.getLogger(__name__)

__all__ = [
    "WarningThresholds",
    "BacktestResult",
    "run_backtest",
    "run_parameter_sweep",
    "run_scenario_set",
    "expand_grid",
    "sweep_frame",
    "config_snapshot",
    "apply_overrides",
]


@dataclass(frozen=True, slots=True)
class WarningThresholds:
    """When a result stops being worth believing.

    Every bound is deliberately generous. These are not quality targets; they
    are the points past which a number is more likely to be an artefact than
    a finding.
    """

    #: Below this, the metrics are describing a handful of coincidences.
    minimum_trades: int = 30
    #: A daily strategy clearing this on a backtest has almost always found
    #: a look-ahead bug rather than an edge.
    implausible_sharpe: float = 3.0
    #: Share of level exits that gapped through. Above this, the backtested
    #: stop distances are fiction.
    maximum_gap_share: float = 0.35
    #: Fraction of the leverage cap the run sat at on average.
    leverage_headroom: float = 0.9
    #: Costs as a share of gross profit.
    maximum_cost_share: float = 0.5
    #: Drawdown past which the equity path matters more than the endpoint.
    severe_drawdown: float = 0.35


@dataclass(frozen=True, slots=True)
class BacktestResult:
    """One run, scored.

    Attributes
    ----------
    label:
        How this run is identified in a sweep. The overrides that produced
        it, rendered short, or ``"base"``.
    config_snapshot:
        Every setting that produced the run, flattened to plain values so it
        can be written to JSON and compared across runs.
    metrics:
        Everything :func:`cfdinvest.analytics.metrics.compute_all_metrics`
        computes.
    attribution:
        The P&L decomposition and the cost breakdown.
    warnings:
        Why not to trust this result, if there is a reason.
    equity_curve, trades:
        Kept by reference so any number can be traced back to the bar that
        produced it.
    """

    label: str = "base"
    config_snapshot: dict[str, Any] = field(default_factory=dict)
    overrides: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    attribution: dict[str, Any] = field(default_factory=dict)
    cost_breakdown: dict[str, Any] = field(default_factory=dict)
    trade_count: int = 0
    period: tuple[Any, Any] = (None, None)
    bars: int = 0
    universe: tuple[str, ...] = ()
    equity_curve: pd.Series = field(
        default_factory=lambda: pd.Series(dtype="float64")
    )
    trades: pd.DataFrame = field(default_factory=pd.DataFrame)
    warnings: tuple[str, ...] = ()
    error: str | None = None

    @property
    def ok(self) -> bool:
        """The run completed. It may still carry warnings."""
        return self.error is None

    @property
    def headline(self) -> dict[str, Any]:
        """The row this result contributes to a sweep table."""
        drawdown = self.metrics.get("drawdown") or {}
        expectancy = self.metrics.get("expectancy") or {}
        return {
            "label": self.label,
            "sharpe_ratio": self.metrics.get("sharpe_ratio", float("nan")),
            "sortino_ratio": self.metrics.get("sortino_ratio", float("nan")),
            "calmar_ratio": self.metrics.get("calmar_ratio", float("nan")),
            "total_return": self.metrics.get("total_return", float("nan")),
            "max_drawdown": drawdown.get("max_drawdown_fraction", float("nan")),
            "win_rate": self.metrics.get("win_rate", float("nan")),
            "expectancy": expectancy.get("expectancy", float("nan")),
            "trades": self.trade_count,
            "warnings": len(self.warnings),
        }


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def config_snapshot(config: BacktestConfig) -> dict[str, Any]:
    """Every setting, flattened to plain values.

    Recorded rather than reconstructed: a sweep is only interpretable if each
    result can say exactly what produced it, and a dataclass repr is not
    something a later comparison can index into.
    """
    from cfdinvest.ui.configview import flatten

    snapshot: dict[str, Any] = {}
    for row in flatten(config, None):
        snapshot[row["setting"]] = row["value"]
    return snapshot


def apply_overrides(
    config: BacktestConfig, overrides: Mapping[str, Any]
) -> BacktestConfig:
    """A copy of ``config`` with dotted paths replaced.

    ``{"execution.max_leverage": 3.0}`` reaches into the nested
    :class:`~cfdinvest.execution.frame.ExecutionConfig`. Pure: the input is
    never mutated, because a sweep that mutated its base config would make
    every run after the first depend on the ones before it.

    Raises
    ------
    ValueError
        On a path that does not exist. A silently ignored override is a
        sweep that reports the same result under different labels.
    """
    updated = config
    for path, value in overrides.items():
        parts = str(path).split(".")
        updated = _replace_deep(updated, parts, value, path)
    return updated


def _replace_deep(obj, parts: Sequence[str], value: Any, path: str):
    """Rebuild a frozen dataclass chain with one leaf replaced."""
    head, *rest = parts
    if not hasattr(obj, head):
        raise ValueError(
            f"unknown setting {path!r}: {type(obj).__name__} has no {head!r}"
        )
    if not rest:
        return replace(obj, **{head: value})
    return replace(obj, **{head: _replace_deep(getattr(obj, head), rest, value, path)})


def expand_grid(grid: Mapping[str, Sequence[Any]]) -> list[dict[str, Any]]:
    """Every combination of a parameter grid, in a stable order.

    Pure. Key order follows the mapping and value order follows each
    sequence, so the same grid always produces the same list — which is what
    makes a sweep's results comparable between runs.
    """
    if not grid:
        return [{}]
    keys = list(grid)
    return [
        dict(zip(keys, combination))
        for combination in itertools.product(*(list(grid[key]) for key in keys))
    ]


def _label(overrides: Mapping[str, Any]) -> str:
    """A short, stable name for one point in a sweep."""
    if not overrides:
        return "base"
    return ", ".join(
        f"{key.rsplit('.', 1)[-1]}={value}" for key, value in overrides.items()
    )


# --------------------------------------------------------------------------- #
# Warnings
# --------------------------------------------------------------------------- #
def _warnings(
    metrics: dict, costs: dict, thresholds: WarningThresholds
) -> tuple[str, ...]:
    """Everything about this result that should stop somebody acting on it."""
    found: list[str] = []
    trades = int(metrics.get("trade_count", 0) or 0)

    if trades < thresholds.minimum_trades:
        found.append(
            f"only {trades} completed trades (below {thresholds.minimum_trades}) — "
            "the metrics describe a handful of coincidences, not a strategy"
        )

    sharpe = metrics.get("sharpe_ratio", float("nan"))
    if np.isfinite(sharpe) and sharpe > thresholds.implausible_sharpe:
        found.append(
            f"Sharpe of {sharpe:.2f} on a daily strategy is implausible — look "
            "for look-ahead before celebrating"
        )

    gaps = metrics.get("gap_through") or {}
    share = gaps.get("gap_share", float("nan"))
    if np.isfinite(share) and share > thresholds.maximum_gap_share:
        found.append(
            f"{share:.0%} of level exits gapped through — the backtested stop "
            "distances are not what the strategy actually got"
        )

    leverage = metrics.get("leverage") or {}
    cap = leverage.get("leverage_cap", float("nan"))
    mean = leverage.get("mean_leverage", float("nan"))
    if np.isfinite(cap) and np.isfinite(mean) and cap > 0:
        if mean > thresholds.leverage_headroom * cap:
            found.append(
                f"average leverage {mean:.2f} sits against the {cap:.1f} cap — "
                "the cap is sizing the book, not the signal"
            )
    if int(leverage.get("cap_breaches", 0) or 0) > 0:
        found.append(
            f"{leverage['cap_breaches']} bars exceeded the leverage cap"
        )

    cost_share = costs.get("cost_share_of_gross", float("nan"))
    if np.isfinite(cost_share) and cost_share > thresholds.maximum_cost_share:
        found.append(
            f"costs took {cost_share:.0%} of gross profit — the edge is mostly "
            "in the broker's pocket"
        )

    drawdown = (metrics.get("drawdown") or {}).get(
        "max_drawdown_fraction", float("nan")
    )
    if np.isfinite(drawdown) and abs(drawdown) > thresholds.severe_drawdown:
        found.append(
            f"maximum drawdown of {abs(drawdown):.0%} — the path matters more "
            "than the endpoint here"
        )

    currency = metrics.get("currency") or {}
    if currency.get("available") and np.isfinite(
        currency.get("cost_share_of_gross", float("nan"))
    ):
        if currency["cost_share_of_gross"] > 0.01:
            found.append(
                f"currency conversion cost {currency['cost_share_of_gross']:.1%} "
                "of traded notional — check fx_fee_basis"
            )

    return tuple(found)


# --------------------------------------------------------------------------- #
# Running
# --------------------------------------------------------------------------- #
def run_backtest(
    config: BacktestConfig | None = None,
    *,
    repository: DataRepository | None = None,
    app_config=None,
    overrides: Mapping[str, Any] | None = None,
    metrics_config: metrics_module.MetricsConfig | None = None,
    thresholds: WarningThresholds | None = None,
    label: str | None = None,
) -> BacktestResult:
    """Run one configuration through the pipeline and score it.

    Reads the lake, writes nothing. A run that fails returns a result
    carrying the error rather than raising, so one bad point in a sweep does
    not lose the other ninety-nine.

    Parameters
    ----------
    repository:
        An open :class:`~cfdinvest.repository.DataRepository`. Built from
        ``app_config`` when not supplied.
    overrides:
        Dotted-path settings applied on top of ``config``.
    """
    settings = config or BacktestConfig()
    changes = dict(overrides or {})
    if changes:
        settings = apply_overrides(settings, changes)

    if repository is None:
        from cfdinvest.config import load_config

        repository = DataRepository(app_config or load_config())

    snapshot = config_snapshot(settings)
    name = label or _label(changes)

    try:
        result = run_pipeline(repository, settings)
    except Exception as error:  # noqa: BLE001 - one bad point must not stop a sweep
        logger.exception("Backtest %s failed", name)
        return BacktestResult(
            label=name, config_snapshot=snapshot, overrides=changes,
            error=f"{type(error).__name__}: {error}",
        )

    return score(
        result,
        label=name,
        snapshot=snapshot,
        overrides=changes,
        metrics_config=metrics_config,
        thresholds=thresholds,
    )


def score(
    pipeline_result,
    *,
    label: str = "base",
    snapshot: dict | None = None,
    overrides: Mapping[str, Any] | None = None,
    metrics_config: metrics_module.MetricsConfig | None = None,
    thresholds: WarningThresholds | None = None,
) -> BacktestResult:
    """Turn a pipeline result into a scored one, without re-running it.

    Separated from :func:`run_backtest` so a completed run can be re-scored
    under different metric conventions — a different risk-free rate, a
    different R basis — without paying for the simulation again, and with no
    possibility of the scoring changing what was simulated.
    """
    execution = getattr(pipeline_result, "execution", None)
    analysis = getattr(pipeline_result, "analysis", None)

    if execution is None or execution.equity_curve.empty:
        return BacktestResult(
            label=label, config_snapshot=snapshot or {},
            overrides=dict(overrides or {}),
            error="the pipeline produced no equity curve; check the universe "
            "and the date range against what the lake holds",
        )

    metrics = metrics_module.compute_all_metrics(execution, config=metrics_config)
    pnl_attribution = attribution_module.compute_pnl_attribution(
        execution, analysis_frame=analysis
    )
    costs = attribution_module.compute_cost_breakdown(execution)
    equity = execution.equity_curve

    return BacktestResult(
        label=label,
        config_snapshot=snapshot or {},
        overrides=dict(overrides or {}),
        metrics=metrics,
        attribution=pnl_attribution,
        cost_breakdown=costs,
        trade_count=int(metrics.get("trade_count", 0) or 0),
        period=(
            equity.index.min() if len(equity) else None,
            equity.index.max() if len(equity) else None,
        ),
        bars=int(len(equity)),
        universe=tuple(execution.positions.columns),
        equity_curve=equity,
        trades=execution.trades,
        warnings=_warnings(metrics, costs, thresholds or WarningThresholds()),
    )


def run_parameter_sweep(
    base_config: BacktestConfig,
    param_grid: Mapping[str, Sequence[Any]],
    *,
    repository: DataRepository | None = None,
    app_config=None,
    metrics_config: metrics_module.MetricsConfig | None = None,
    thresholds: WarningThresholds | None = None,
) -> list[BacktestResult]:
    """Every combination in the grid, run and scored.

    The lake is opened once and reused, because a sweep of fifty points
    otherwise re-reads the same parquet fifty times.

    **A sweep is a multiple-comparisons machine.** Run enough points and one
    will look excellent by luck alone; the best row in a sweep table is an
    upper bound on what the strategy does, not an estimate of it. Walk-forward
    exists for the same reason and answers a different question.
    """
    if repository is None:
        from cfdinvest.config import load_config

        repository = DataRepository(app_config or load_config())

    combinations = expand_grid(param_grid)
    logger.info("Parameter sweep: %d combinations", len(combinations))

    return [
        run_backtest(
            base_config,
            repository=repository,
            overrides=overrides,
            metrics_config=metrics_config,
            thresholds=thresholds,
        )
        for overrides in combinations
    ]


def run_scenario_set(
    configs: Iterable[BacktestConfig] | Mapping[str, BacktestConfig],
    *,
    repository: DataRepository | None = None,
    app_config=None,
    metrics_config: metrics_module.MetricsConfig | None = None,
    thresholds: WarningThresholds | None = None,
) -> list[BacktestResult]:
    """Run an explicit list of configurations.

    For scenarios that are not a grid: costs on versus off, stops on versus
    off, one universe against another. Accepts a mapping to name them.
    """
    if repository is None:
        from cfdinvest.config import load_config

        repository = DataRepository(app_config or load_config())

    if isinstance(configs, Mapping):
        pairs = list(configs.items())
    else:
        pairs = [(f"scenario_{n}", config) for n, config in enumerate(configs)]

    return [
        run_backtest(
            config,
            repository=repository,
            metrics_config=metrics_config,
            thresholds=thresholds,
            label=name,
        )
        for name, config in pairs
    ]


def sweep_frame(results: Sequence[BacktestResult]) -> pd.DataFrame:
    """A sweep's results as one table, best Sharpe first.

    Failed runs are kept, with their error: a sweep table that silently drops
    the points that broke hides the shape of the failure, which is usually
    the most informative thing in it.
    """
    if not results:
        return pd.DataFrame(
            columns=["label", "sharpe_ratio", "trades", "warnings", "error"]
        )

    rows = []
    for result in results:
        row = dict(result.headline)
        row["error"] = result.error
        row.update({f"param_{k.rsplit('.', 1)[-1]}": v
                    for k, v in result.overrides.items()})
        rows.append(row)

    frame = pd.DataFrame(rows).set_index("label")
    return frame.sort_values("sharpe_ratio", ascending=False, na_position="last")
