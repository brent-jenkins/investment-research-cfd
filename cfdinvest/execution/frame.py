"""The :class:`ExecutionFrame`: positions, trades, P&L and equity.

The end of the pipeline as it stands. Reads a
:class:`~cfdinvest.signals.SignalFrame` and an
:class:`~cfdinvest.preprocessing.AnalysisFrame`, and writes to neither.

Performance *analytics* — Sharpe, Sortino, win rate, expectancy — are
deliberately absent. This layer's job is to be an honest simulator; judging
the strategy is a separate concern with its own layer, and mixing the two
makes it tempting to tune the simulator until the numbers look good.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from cfdinvest.execution import engine as engine_module
from cfdinvest.execution import pnl as pnl_module
from cfdinvest.execution.state import HysteresisThresholds
from cfdinvest.schema import TIMESTAMP

logger = logging.getLogger(__name__)

__all__ = ["ExecutionConfig", "ExecutionFrame", "run_backtest"]


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    """Fill, cost, financing and risk settings for a simulation."""

    # -- account ---------------------------------------------------------- #
    initial_equity: float = 100_000.0
    #: Notional per unit of signal size, as a multiple of equity. At the
    #: default, a signal of size 1.0 asks for a position equal to equity.
    exposure_per_unit_size: float = 1.0

    # -- fills ------------------------------------------------------------ #
    #: Bars between a signal and the fill it produces. ``1`` means a signal
    #: from bar t is executed on bar t+1 — the only setting free of
    #: look-ahead, and the reason it is the default.
    execution_lag: int = 1
    #: ``"open"`` (default), ``"mid"`` or ``"close"``. See
    #: :func:`~cfdinvest.execution.fills.fill_prices` for why anything other
    #: than ``"open"`` is mildly optimistic.
    fill_mode: str = "open"
    slippage_fraction: float = 0.0
    default_spread_bps: float = 5.0
    #: Per-asset half-spread overrides, in basis points of price.
    spread_bps: dict[str, float] | None = None
    asset_classes: dict[str, str] | None = None
    #: Trades smaller than this in units are ignored as noise.
    minimum_trade_units: float = 1e-9

    # -- hysteresis ------------------------------------------------------- #
    thresholds: HysteresisThresholds = field(default_factory=HysteresisThresholds)
    #: Relative drift required before an existing position is resized.
    rebalance_threshold: float = 0.25

    # -- financing -------------------------------------------------------- #
    benchmark_rate: float = 0.05
    financing_markup: float = 0.03
    day_count: float = 365.0
    #: Per-asset ``{ticker: (long_rate, short_rate)}`` overrides.
    financing_rates: dict[str, tuple[float, float]] | None = None

    # -- risk ------------------------------------------------------------- #
    #: Cap on gross notional divided by equity.
    max_leverage: float = 5.0
    #: Flatten everything when equity falls to this fraction of its peak.
    #: ``None`` disables the stop-out.
    stop_out_level: float | None = None
    #: Whether trading resumes after a stop-out, or the run stays flat.
    resume_after_stop_out: bool = True

    def __post_init__(self) -> None:
        if self.initial_equity <= 0:
            raise ValueError(f"initial_equity must be > 0, got {self.initial_equity}")
        if self.max_leverage <= 0:
            raise ValueError(f"max_leverage must be > 0, got {self.max_leverage}")
        if self.slippage_fraction < 0:
            raise ValueError(
                f"slippage_fraction must be >= 0, got {self.slippage_fraction}"
            )
        if self.stop_out_level is not None and not 0 < self.stop_out_level < 1:
            raise ValueError(
                f"stop_out_level must be in (0, 1), got {self.stop_out_level}"
            )
        if self.execution_lag == 0:
            logger.warning(
                "execution_lag=0 fills on the same bar the signal was computed "
                "from, which is look-ahead. Results are not achievable."
            )


@dataclass(frozen=True, slots=True)
class ExecutionFrame:
    """The result of a simulation.

    Attributes
    ----------
    positions:
        ``timestamp x ticker`` of signed units held.
    notionals:
        The same positions marked at each bar's close.
    trades:
        One row per executed change; see
        :data:`~cfdinvest.execution.engine.TRADE_COLUMNS`.
    pnl:
        Per-bar decomposition. The five components sum to ``total``.
    equity_curve:
        Mark-to-market account value.
    exposure:
        Gross notional divided by equity, per bar.
    metadata:
        Configuration, calendar bounds and headline figures.
    """

    positions: pd.DataFrame = field(default_factory=pd.DataFrame)
    notionals: pd.DataFrame = field(default_factory=pd.DataFrame)
    trades: pd.DataFrame = field(default_factory=pd.DataFrame)
    pnl: pd.DataFrame = field(default_factory=pd.DataFrame)
    equity_curve: pd.Series = field(default_factory=lambda: pd.Series(dtype="float64"))
    exposure: pd.Series = field(default_factory=lambda: pd.Series(dtype="float64"))
    metadata: dict[str, Any] = field(default_factory=dict)

    # -- views ------------------------------------------------------------ #
    @property
    def index(self) -> pd.Index:
        """The calendar every panel shares."""
        return self.equity_curve.index

    def drawdown(self) -> pd.DataFrame:
        """Running peak, drawdown and drawdown fraction."""
        return pnl_module.drawdown(self.equity_curve)

    def active_positions(self) -> pd.DataFrame:
        """Boolean panel of bars holding a position."""
        if self.positions.empty:
            return pd.DataFrame()
        return self.positions != 0

    def trade_summary(self) -> pd.DataFrame:
        """Counts and totals per action, for a sanity check on churn."""
        if self.trades.empty:
            return pd.DataFrame(
                columns=["trades", "realized_pnl", "spread_cost", "slippage"]
            )
        grouped = self.trades.groupby("action")
        return pd.DataFrame(
            {
                "trades": grouped.size(),
                "realized_pnl": grouped["realized_pnl"].sum(),
                "spread_cost": grouped["spread_cost"].sum(),
                "slippage": grouped["slippage"].sum(),
            }
        ).sort_index()

    def per_asset(self) -> pd.DataFrame:
        """Trade count, costs and realised P&L per instrument."""
        if self.trades.empty:
            return pd.DataFrame(columns=["trades", "realized_pnl", "cost"])
        grouped = self.trades.groupby("asset")
        summary = pd.DataFrame(
            {
                "trades": grouped.size(),
                "realized_pnl": grouped["realized_pnl"].sum(),
                "cost": grouped["spread_cost"].sum() + grouped["slippage"].sum(),
            }
        )
        return summary.sort_index()

    def holding_periods(self) -> pd.Series:
        """Bars held per completed position, as a churn diagnostic.

        A median of one or two means the hysteresis band is too narrow and
        the strategy is paying the spread to stand still.
        """
        if self.positions.empty:
            return pd.Series(dtype="float64", name="bars_held")

        spans = []
        for ticker in self.positions.columns:
            column = self.positions[ticker]
            active = column != 0
            if not active.any():
                continue
            groups = (active != active.shift()).cumsum()
            for _, run in column.groupby(groups):
                if run.iloc[0] != 0:
                    spans.append(len(run))
        return pd.Series(spans, dtype="float64", name="bars_held")


def run_backtest(
    signal_frame,
    analysis_frame,
    config: ExecutionConfig | None = None,
) -> ExecutionFrame:
    """Simulate a strategy end to end.

    Parameters
    ----------
    signal_frame:
        Target positions from the signal layer.
    analysis_frame:
        The prices those signals were built from.
    config:
        Fill, cost, financing and risk settings.

    Returns
    -------
    ExecutionFrame
    """
    settings = config or ExecutionConfig()
    result = engine_module.run_execution(signal_frame, analysis_frame, settings)

    equity = result["equity_curve"]
    summary = (
        pnl_module.summarise(result["pnl"], equity, settings.initial_equity)
        if not equity.empty
        else {}
    )

    metadata = {
        "bars": len(equity),
        "calendar_start": equity.index.min() if len(equity) else None,
        "calendar_end": equity.index.max() if len(equity) else None,
        "tickers": tuple(result["positions"].columns),
        "trades": len(result["trades"]),
        "execution_lag": settings.execution_lag,
        "fill_mode": settings.fill_mode,
        "max_leverage": settings.max_leverage,
        "stop_out_level": settings.stop_out_level,
        "thresholds": {
            "entry_size": settings.thresholds.entry_size,
            "exit_size": settings.thresholds.exit_size,
            "entry_confidence": settings.thresholds.entry_confidence,
            "exit_confidence": settings.thresholds.exit_confidence,
        },
        "financing": {
            "benchmark_rate": settings.benchmark_rate,
            "markup": settings.financing_markup,
        },
        **summary,
    }

    return ExecutionFrame(
        positions=result["positions"],
        notionals=result["notionals"],
        trades=result["trades"],
        pnl=result["pnl"],
        equity_curve=equity,
        exposure=result["exposure"],
        metadata=metadata,
    )
