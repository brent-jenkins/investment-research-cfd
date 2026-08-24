"""Walk-forward evaluation: does the result hold up out of sample?

**No parameters are fitted here.** This is a stability report, not an
optimiser. The windows exist to answer "was the headline number produced by
one lucky stretch?", which is a different question from "what settings would
have worked best" — and answering the second inside the analytics layer is
exactly how a backtest ends up describing its own tuning.

The ``train`` window is carried through and reported so that an optimiser
could be dropped in later without restructuring anything, but nothing in
this module reads it.

Preprocessing and regime classification run **once** over the full history:
both are causal, so a value computed at bar *t* is identical whether or not
the data after *t* exists — a property the preprocessing suite asserts
directly. Only execution is re-run per window, because it is the only stage
that carries state.

Each test window starts **flat, with fresh equity**. Carrying a position or a
running balance across the boundary would make window two's result depend on
window one's, which defeats the point of splitting them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

from cfdinvest.backtest.metrics import MetricsConfig, compute_metrics
from cfdinvest.execution import run_backtest
from cfdinvest.signals import SignalFrame

logger = logging.getLogger(__name__)

__all__ = [
    "Window",
    "rolling_windows",
    "anchored_windows",
    "walk_forward",
    "stability_report",
]


@dataclass(frozen=True, slots=True)
class Window:
    """One train/test split, as positional slices into the calendar."""

    number: int
    train_start: int
    train_end: int
    test_start: int
    test_end: int

    @property
    def train_slice(self) -> slice:
        """Positional slice of the in-sample stretch."""
        return slice(self.train_start, self.train_end)

    @property
    def test_slice(self) -> slice:
        """Positional slice of the out-of-sample stretch."""
        return slice(self.test_start, self.test_end)

    @property
    def test_bars(self) -> int:
        """Length of the evaluation stretch."""
        return self.test_end - self.test_start


def rolling_windows(
    n_bars: int, train_bars: int, test_bars: int, step: int | None = None
) -> list[Window]:
    """Split a calendar into consecutive rolling train/test pairs.

    Pure. The train window slides forward with the test window, so each
    evaluation uses only the ``train_bars`` immediately preceding it.

    Parameters
    ----------
    step:
        Bars between successive windows. Defaults to ``test_bars``, giving
        non-overlapping evaluation stretches — overlapping ones would count
        the same bar's outcome more than once and understate the variance
        the report exists to show.

    Raises
    ------
    ValueError
        On non-positive window lengths.
    """
    if train_bars <= 0 or test_bars <= 0:
        raise ValueError(
            f"train_bars and test_bars must be > 0, got {train_bars}, {test_bars}"
        )
    stride = test_bars if step is None else step
    if stride <= 0:
        raise ValueError(f"step must be > 0, got {stride}")

    windows = []
    start = 0
    number = 0
    while start + train_bars + test_bars <= n_bars:
        windows.append(
            Window(
                number=number,
                train_start=start,
                train_end=start + train_bars,
                test_start=start + train_bars,
                test_end=start + train_bars + test_bars,
            )
        )
        start += stride
        number += 1
    return windows


def anchored_windows(n_bars: int, train_bars: int, test_bars: int) -> list[Window]:
    """Split with an expanding train window anchored at the start.

    Pure. Each evaluation sees every bar before it rather than a fixed
    lookback — closer to how a strategy is actually run forward in time.
    """
    if train_bars <= 0 or test_bars <= 0:
        raise ValueError(
            f"train_bars and test_bars must be > 0, got {train_bars}, {test_bars}"
        )

    windows = []
    test_start = train_bars
    number = 0
    while test_start + test_bars <= n_bars:
        windows.append(
            Window(
                number=number,
                train_start=0,
                train_end=test_start,
                test_start=test_start,
                test_end=test_start + test_bars,
            )
        )
        test_start += test_bars
        number += 1
    return windows


def _slice_signals(signals: SignalFrame, window: Window) -> SignalFrame:
    """Cut a SignalFrame down to a window's test stretch."""
    cut = window.test_slice
    return SignalFrame(
        direction=signals.direction.iloc[cut],
        size=signals.size.iloc[cut],
        confidence=signals.confidence.iloc[cut],
        reason=signals.reason.iloc[cut],
        diagnostics={k: v.iloc[cut] for k, v in signals.diagnostics.items()},
        metadata=dict(signals.metadata),
    )


def _slice_analysis(analysis_frame, window: Window):
    """Cut an AnalysisFrame down to a window's test stretch."""
    from dataclasses import replace

    cut = window.test_slice
    return replace(
        analysis_frame,
        prices={k: v.iloc[cut] for k, v in analysis_frame.prices.items()},
        returns={k: v.iloc[cut] for k, v in analysis_frame.returns.items()},
        volatility={k: v.iloc[cut] for k, v in analysis_frame.volatility.items()},
        # Rolling correlations are MultiIndexed by (timestamp, ticker) and
        # nothing downstream of execution reads them, so they are dropped
        # rather than sliced with a second index convention.
        correlations={},
        macro=analysis_frame.macro.iloc[cut] if not analysis_frame.macro.empty
        else analysis_frame.macro,
        regimes=analysis_frame.regimes.iloc[cut],
    )


def walk_forward(
    analysis_frame,
    signals: SignalFrame,
    windows: list[Window],
    execution_config,
    metrics_config: MetricsConfig | None = None,
) -> pd.DataFrame:
    """Re-run execution over each test window and evaluate it.

    Each window starts flat with fresh equity, so the windows are
    independent samples of the strategy's behaviour rather than one path cut
    into pieces.

    Returns
    -------
    pandas.DataFrame
        One row per window: its bounds, plus every metric.
    """
    if not windows:
        logger.warning("No windows to evaluate; history is shorter than one split")
        return pd.DataFrame()

    settings = metrics_config or MetricsConfig()
    calendar = analysis_frame.calendar

    rows = []
    for window in windows:
        window_analysis = _slice_analysis(analysis_frame, window)
        window_signals = _slice_signals(signals, window)
        execution = run_backtest(window_signals, window_analysis, execution_config)

        row = {
            "window": window.number,
            "train_start": calendar[window.train_start],
            "train_end": calendar[window.train_end - 1],
            "test_start": calendar[window.test_start],
            "test_end": calendar[window.test_end - 1],
            "test_bars": window.test_bars,
        }
        row.update(compute_metrics(execution, settings))
        rows.append(row)

    return pd.DataFrame(rows)


def stability_report(
    window_metrics: pd.DataFrame,
    columns: tuple[str, ...] = ("total_return", "sharpe", "max_drawdown_fraction"),
) -> pd.DataFrame:
    """Summarise how much each metric moved between windows.

    ``positive_share`` is usually the most informative column: a strategy
    with a good mean and a 40% positive share is a strategy whose headline
    number came from a handful of windows.

    Pure.

    Returns
    -------
    pandas.DataFrame
        One row per metric: ``windows``, ``mean``, ``median``, ``std``,
        ``min``, ``max``, ``positive_share``.
    """
    if window_metrics is None or window_metrics.empty:
        return pd.DataFrame(
            columns=["windows", "mean", "median", "std", "min", "max", "positive_share"]
        )

    rows = []
    for column in columns:
        if column not in window_metrics.columns:
            continue
        values = window_metrics[column].replace([np.inf, -np.inf], np.nan).dropna()
        if values.empty:
            continue
        rows.append(
            {
                "metric": column,
                "windows": int(len(values)),
                "mean": float(values.mean()),
                "median": float(values.median()),
                "std": float(values.std(ddof=1)) if len(values) > 1 else 0.0,
                "min": float(values.min()),
                "max": float(values.max()),
                "positive_share": float((values > 0).mean()),
            }
        )

    if not rows:
        return pd.DataFrame(
            columns=["windows", "mean", "median", "std", "min", "max", "positive_share"]
        )
    return pd.DataFrame(rows).set_index("metric")
