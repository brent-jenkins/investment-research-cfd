"""Execution simulator: signals into realistic CFD trades and P&L.

Reads a :class:`~cfdinvest.signals.SignalFrame` and an
:class:`~cfdinvest.preprocessing.AnalysisFrame`, and writes to neither.

    state       position machine with a two-threshold hysteresis band
    fills       one-bar execution lag, fill prices, spread and slippage
    financing   overnight carry, signed by direction and counted in nights
    pnl         gap / intraday / cost / financing decomposition, equity curve
    engine      the sequential loop, vectorised across assets
    frame       ExecutionFrame, ExecutionConfig, run_backtest

Performance analytics are deliberately absent: this layer's job is to be an
honest simulator, and judging the strategy belongs to its own layer.
"""

from cfdinvest.execution.engine import TRADE_COLUMNS, run_execution
from cfdinvest.execution.fills import fill_prices, shift_for_execution, transaction_costs
from cfdinvest.execution.financing import financing_cost, financing_rates, nights_between
from cfdinvest.execution.frame import ExecutionConfig, ExecutionFrame, run_backtest
from cfdinvest.execution.pnl import PNL_COMPONENTS, bar_pnl, drawdown, equity_curve
from cfdinvest.execution.state import (
    ACTIONS,
    HysteresisThresholds,
    classify_actions,
    next_direction,
    transition_table,
)

__all__ = [
    "ExecutionConfig",
    "ExecutionFrame",
    "run_backtest",
    "run_execution",
    "HysteresisThresholds",
    "transition_table",
    "classify_actions",
    "next_direction",
    "shift_for_execution",
    "fill_prices",
    "transaction_costs",
    "financing_rates",
    "financing_cost",
    "nights_between",
    "bar_pnl",
    "equity_curve",
    "drawdown",
    "ACTIONS",
    "PNL_COMPONENTS",
    "TRADE_COLUMNS",
]
