"""Walk-forward evaluation over date ranges.

A backtest scored on the data that chose its parameters is a description of
that data. Walk-forward asks the only question that matters: having chosen on
what came before, what happened next.

**The windows are strictly partitioned.** A test stretch begins after its
training stretch ends, never overlaps it, and is never used to select
anything. A test asserts both properties over generated windows rather than
trusting the arithmetic.

**Each window runs once, over the whole span, and is then sliced.** The
alternative — running the test range in isolation — would warm every
indicator up *inside* the test window, so the first fifty bars of each test
would be scored on a half-built ATR and a correlation matrix that does not
exist yet. Running from the training start and slicing afterwards gives the
test period the indicator state it would genuinely have had, and costs
nothing in look-ahead because every window in this system is trailing-only.

Deterministic given the lake and the configuration.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from cfdinvest.analytics import backtest as backtest_module
from cfdinvest.analytics import metrics as metrics_module
from cfdinvest.backtest.runner import BacktestConfig, run_pipeline
from cfdinvest.repository import DataRepository

logger = logging.getLogger(__name__)

__all__ = [
    "DateWindow",
    "WalkforwardWindowResult",
    "rolling_date_windows",
    "anchored_date_windows",
    "run_walkforward",
    "stability_summary",
    "slice_execution",
]

#: Metrics tracked across windows for drift. Ratios and fractions only —
#: comparing absolute P&L across windows of different lengths says nothing.
STABILITY_METRICS: tuple[str, ...] = (
    "sharpe_ratio",
    "sortino_ratio",
    "total_return",
    "win_rate",
    "max_drawdown",
    "expectancy",
)


def _utc(value) -> pd.Timestamp:
    """Any timestamp-like value, as a UTC-aware Timestamp."""
    stamp = pd.Timestamp(value)
    return stamp.tz_localize("UTC") if stamp.tz is None else stamp.tz_convert("UTC")


@dataclass(frozen=True, slots=True)
class DateWindow:
    """One train/test split, as half-open date ranges.

    ``train`` is ``[train_start, train_end)`` and ``test`` is
    ``[test_start, test_end)``. Half-open so that a bar belongs to exactly
    one side of a boundary, which is what makes "no overlap" checkable
    rather than a matter of convention.
    """

    number: int
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp

    def __post_init__(self) -> None:
        # Normalised to UTC here, once, because every frame these windows
        # slice is UTC-aware and pandas refuses to compare a naive timestamp
        # with an aware one -- a TypeError deep inside a slice rather than a
        # clear complaint at the boundary.
        for field_name in ("train_start", "train_end", "test_start", "test_end"):
            object.__setattr__(self, field_name, _utc(getattr(self, field_name)))

        if self.train_end <= self.train_start:
            raise ValueError(f"window {self.number}: empty training range")
        if self.test_end <= self.test_start:
            raise ValueError(f"window {self.number}: empty test range")
        if self.test_start < self.train_end:
            raise ValueError(
                f"window {self.number}: the test range starts at "
                f"{self.test_start} but training runs to {self.train_end}; "
                "an overlapping split scores a strategy on what selected it"
            )

    @property
    def train_period(self) -> tuple[pd.Timestamp, pd.Timestamp]:
        return (self.train_start, self.train_end)

    @property
    def test_period(self) -> tuple[pd.Timestamp, pd.Timestamp]:
        return (self.test_start, self.test_end)

    @property
    def span(self) -> tuple[pd.Timestamp, pd.Timestamp]:
        """The whole stretch the pipeline must run over for this window."""
        return (self.train_start, self.test_end)


@dataclass(frozen=True, slots=True)
class WalkforwardWindowResult:
    """One window, evaluated.

    ``drift`` is ``test − train`` for each stability metric. Negative drift on
    Sharpe is the expected case — in-sample is always kinder — and the
    magnitude is the finding: a strategy that loses a tenth of its Sharpe out
    of sample is a different proposition from one that loses all of it.
    """

    window: DateWindow
    train_metrics: dict[str, Any] = field(default_factory=dict)
    test_metrics: dict[str, Any] = field(default_factory=dict)
    parameter_set: dict[str, Any] = field(default_factory=dict)
    drift: dict[str, float] = field(default_factory=dict)
    train_trades: int = 0
    test_trades: int = 0
    warnings: tuple[str, ...] = ()
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def row(self) -> dict[str, Any]:
        """One row of the stability table."""
        row: dict[str, Any] = {
            "window": self.window.number,
            "train_start": self.window.train_start,
            "train_end": self.window.train_end,
            "test_start": self.window.test_start,
            "test_end": self.window.test_end,
            "train_trades": self.train_trades,
            "test_trades": self.test_trades,
        }
        for metric in STABILITY_METRICS:
            row[f"train_{metric}"] = _flat(self.train_metrics, metric)
            row[f"test_{metric}"] = _flat(self.test_metrics, metric)
            row[f"drift_{metric}"] = self.drift.get(metric, float("nan"))
        if self.parameter_set:
            row.update(
                {f"param_{k.rsplit('.', 1)[-1]}": v
                 for k, v in self.parameter_set.items()}
            )
        row["error"] = self.error
        return row


def _flat(metrics: Mapping[str, Any], name: str) -> float:
    """Pull a stability metric out of the nested metrics dictionary."""
    if not metrics:
        return float("nan")
    if name == "max_drawdown":
        return float(
            (metrics.get("drawdown") or {}).get(
                "max_drawdown_fraction", float("nan")
            )
        )
    if name == "expectancy":
        return float(
            (metrics.get("expectancy") or {}).get("expectancy", float("nan"))
        )
    value = metrics.get(name, float("nan"))
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


# --------------------------------------------------------------------------- #
# Window generation
# --------------------------------------------------------------------------- #
def rolling_date_windows(
    start, end, *, train: str | pd.Timedelta, test: str | pd.Timedelta,
    step: str | pd.Timedelta | None = None,
) -> list[DateWindow]:
    """Fixed-length training windows that roll forward.

    The training stretch is always the same length, so every window is
    selected on the same amount of history — which makes their results
    comparable. ``step`` defaults to the test length, giving contiguous,
    non-overlapping test stretches that tile the period exactly once.
    """
    begin, finish = _utc(start), _utc(end)
    train_span = pd.Timedelta(train)
    test_span = pd.Timedelta(test)
    stride = pd.Timedelta(step) if step is not None else test_span

    windows: list[DateWindow] = []
    train_start = begin
    number = 0
    while True:
        train_end = train_start + train_span
        test_end = train_end + test_span
        if test_end > finish:
            break
        windows.append(
            DateWindow(number, train_start, train_end, train_end, test_end)
        )
        number += 1
        train_start = train_start + stride
    return windows


def anchored_date_windows(
    start, end, *, train: str | pd.Timedelta, test: str | pd.Timedelta,
    step: str | pd.Timedelta | None = None,
) -> list[DateWindow]:
    """Training windows that all begin at ``start`` and grow.

    Closer to how a strategy is actually run — you do not throw away last
    year because this year arrived — at the cost of later windows being
    selected on more data than earlier ones.
    """
    begin, finish = _utc(start), _utc(end)
    test_span = pd.Timedelta(test)
    stride = pd.Timedelta(step) if step is not None else test_span

    windows: list[DateWindow] = []
    train_end = begin + pd.Timedelta(train)
    number = 0
    while train_end + test_span <= finish:
        windows.append(
            DateWindow(number, begin, train_end, train_end, train_end + test_span)
        )
        number += 1
        train_end = train_end + stride
    return windows


# --------------------------------------------------------------------------- #
# Slicing a completed run
# --------------------------------------------------------------------------- #
def slice_execution(execution, start, end):
    """A copy of an ExecutionFrame restricted to ``[start, end)``.

    Half-open, matching :class:`DateWindow`. The equity curve is left as it
    is rather than rebased: every metric that matters here is computed from
    *returns*, which are unaffected by the level, and rebasing would quietly
    change the drawdown denominator.
    """
    begin, finish = _utc(start), _utc(end)

    def window(frame):
        if frame is None or len(frame) == 0:
            return frame
        if not isinstance(frame.index, pd.DatetimeIndex):
            return frame
        return frame.loc[(frame.index >= begin) & (frame.index < finish)]

    trades = getattr(execution, "trades", pd.DataFrame())
    if trades is not None and not trades.empty and "timestamp" in trades.columns:
        stamps = pd.to_datetime(trades["timestamp"], utc=True, errors="coerce")
        trades = trades[(stamps >= begin) & (stamps < finish)]

    return replace(
        execution,
        positions=window(execution.positions),
        notionals=window(execution.notionals),
        pnl=window(execution.pnl),
        equity_curve=window(execution.equity_curve),
        exposure=window(execution.exposure),
        trades=trades,
        stop_events=_window_events(getattr(execution, "stop_events", None), begin, finish),
    )


def _window_events(events, begin, finish):
    """Restrict the stop-event log, which is timestamped by column."""
    if events is None or events.empty or "timestamp" not in events.columns:
        return events
    stamps = pd.to_datetime(events["timestamp"], utc=True, errors="coerce")
    return events[(stamps >= begin) & (stamps < finish)]


# --------------------------------------------------------------------------- #
# Running
# --------------------------------------------------------------------------- #
def run_walkforward(
    config: BacktestConfig | None = None,
    windows: Sequence[DateWindow] | None = None,
    *,
    repository: DataRepository | None = None,
    app_config=None,
    param_grid: Mapping[str, Sequence[Any]] | None = None,
    objective: str = "sharpe_ratio",
    metrics_config: metrics_module.MetricsConfig | None = None,
) -> list[WalkforwardWindowResult]:
    """Evaluate every window, selecting on train and scoring on test.

    With ``param_grid``, each window's parameters are chosen by running the
    grid over the *training* range only and keeping the best ``objective``.
    Without it, the same configuration is evaluated in both halves, which
    measures how stable the strategy is rather than how well the selection
    generalises.

    A window that fails returns a result carrying the error; the rest still
    run.
    """
    settings = config or BacktestConfig()
    if not windows:
        raise ValueError("walk-forward needs at least one window")

    if repository is None:
        from cfdinvest.config import load_config

        repository = DataRepository(app_config or load_config())

    results: list[WalkforwardWindowResult] = []
    for window in windows:
        try:
            results.append(
                _run_window(
                    window, settings, repository, param_grid, objective,
                    metrics_config,
                )
            )
        except Exception as error:  # noqa: BLE001 - one window must not stop the rest
            logger.exception("Walk-forward window %d failed", window.number)
            results.append(
                WalkforwardWindowResult(
                    window=window, error=f"{type(error).__name__}: {error}"
                )
            )
    return results


def _run_window(
    window: DateWindow,
    config: BacktestConfig,
    repository: DataRepository,
    param_grid: Mapping[str, Sequence[Any]] | None,
    objective: str,
    metrics_config,
) -> WalkforwardWindowResult:
    """One window: select on train, score on test."""
    chosen = config
    parameters: dict[str, Any] = {}

    if param_grid:
        parameters, chosen = _select_on_train(
            window, config, repository, param_grid, objective, metrics_config
        )

    # One run over the whole span, then sliced. The test half therefore
    # inherits indicator state warmed on the training half, exactly as a live
    # run would, and no window looks forward because every rolling window in
    # this system is trailing-only.
    span = replace(
        chosen, start=window.train_start, end=window.test_end
    )
    result = run_pipeline(repository, span)
    execution = getattr(result, "execution", None)
    if execution is None or execution.equity_curve.empty:
        return WalkforwardWindowResult(
            window=window,
            parameter_set=parameters,
            error="the pipeline produced no equity curve over this window",
        )

    train = slice_execution(execution, window.train_start, window.train_end)
    test = slice_execution(execution, window.test_start, window.test_end)

    train_metrics = metrics_module.compute_all_metrics(train, config=metrics_config)
    test_metrics = metrics_module.compute_all_metrics(test, config=metrics_config)

    drift = {
        metric: _flat(test_metrics, metric) - _flat(train_metrics, metric)
        for metric in STABILITY_METRICS
    }

    return WalkforwardWindowResult(
        window=window,
        train_metrics=train_metrics,
        test_metrics=test_metrics,
        parameter_set=parameters,
        drift=drift,
        train_trades=int(train_metrics.get("trade_count", 0) or 0),
        test_trades=int(test_metrics.get("trade_count", 0) or 0),
        warnings=_window_warnings(window, train_metrics, test_metrics, drift),
    )


def _select_on_train(
    window: DateWindow,
    config: BacktestConfig,
    repository: DataRepository,
    param_grid: Mapping[str, Sequence[Any]],
    objective: str,
    metrics_config,
) -> tuple[dict[str, Any], BacktestConfig]:
    """Pick the best parameters using the training range and nothing else."""
    train_only = replace(config, start=window.train_start, end=window.train_end)
    candidates = backtest_module.run_parameter_sweep(
        train_only, param_grid, repository=repository, metrics_config=metrics_config
    )

    usable = [c for c in candidates if c.ok and np.isfinite(
        c.metrics.get(objective, float("nan"))
    )]
    if not usable:
        return {}, config

    best = max(usable, key=lambda c: c.metrics[objective])
    return dict(best.overrides), backtest_module.apply_overrides(
        config, best.overrides
    )


def _window_warnings(
    window: DateWindow, train: dict, test: dict, drift: dict
) -> tuple[str, ...]:
    """What is worth saying about one window."""
    found: list[str] = []
    test_trades = int(test.get("trade_count", 0) or 0)

    if test_trades == 0:
        found.append("the test range produced no completed trades")
    elif test_trades < 5:
        found.append(f"only {test_trades} test trades — this window says little")

    sharpe_drift = drift.get("sharpe_ratio", float("nan"))
    train_sharpe = _flat(train, "sharpe_ratio")
    if np.isfinite(sharpe_drift) and np.isfinite(train_sharpe) and train_sharpe > 0:
        if sharpe_drift < -train_sharpe:
            found.append(
                "Sharpe went from positive in training to negative out of "
                "sample — the in-sample result did not survive"
            )
    return tuple(found)


# --------------------------------------------------------------------------- #
# Summarising
# --------------------------------------------------------------------------- #
def stability_summary(results: Sequence[WalkforwardWindowResult]) -> pd.DataFrame:
    """How much each metric moved between train and test, across all windows.

    The column to read is ``mean_drift``: consistently negative means the
    in-sample result does not survive, and a large ``std_test`` means it
    survives sometimes, which is worse than a small consistent decay because
    it cannot be planned around.
    """
    usable = [r for r in results if r.ok]
    if not usable:
        return pd.DataFrame(
            columns=["mean_train", "mean_test", "mean_drift", "std_test", "windows"]
        )

    rows = {}
    for metric in STABILITY_METRICS:
        train = np.array([_flat(r.train_metrics, metric) for r in usable], dtype=float)
        test = np.array([_flat(r.test_metrics, metric) for r in usable], dtype=float)
        drift = test - train
        rows[metric] = {
            "mean_train": float(np.nanmean(train)) if train.size else float("nan"),
            "mean_test": float(np.nanmean(test)) if test.size else float("nan"),
            "mean_drift": float(np.nanmean(drift)) if drift.size else float("nan"),
            "std_test": float(np.nanstd(test)) if test.size else float("nan"),
            "windows": int(len(usable)),
        }
    return pd.DataFrame(rows).T


def windows_frame(results: Sequence[WalkforwardWindowResult]) -> pd.DataFrame:
    """Every window as a row, in order."""
    if not results:
        return pd.DataFrame()
    return pd.DataFrame([r.row() for r in results]).set_index("window")
