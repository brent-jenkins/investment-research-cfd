"""P&L decomposition and the equity curve.

A bar's P&L is split into pieces that must sum to the change in equity. If
they do not, something has been double-counted or lost, and the test suite
asserts the identity directly rather than trusting it.

The decomposition, for each bar:

``gap``
    The move from the previous close to this bar's fill price, earned on the
    position **held into** the bar. This is the overnight gap, and it belongs
    to the old position — the new one did not exist yet.
``intraday``
    The move from the fill price to this bar's close, earned on the position
    held **out of** the bar.
``spread`` / ``slippage``
    Transaction costs, charged on the change in position.
``financing``
    Overnight carry on the position held into the bar.
``fx``
    Currency conversion on the part of the position opened and closed this
    bar. Zero unless FX is enabled, so the identity is unchanged for every
    single-currency run.

Splitting at the fill price rather than marking everything close-to-close is
what makes a mid-bar trade accounted correctly: without it, a position opened
at today's open would be credited with the overnight gap it was not there
for.

``realized`` and ``unrealized`` are reported separately for the trade log,
but the equity curve is built from the total, because a mark-to-market
account does not care which bucket a move sits in.

All functions are pure and vectorised.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from cfdinvest.execution import fx as fx_module

__all__ = [
    "PNL_COMPONENTS",
    "bar_pnl",
    "fx_close_costs",
    "realized_on_close",
    "equity_curve",
    "drawdown",
    "summarise",
]

#: The components that must sum to the total.
PNL_COMPONENTS: tuple[str, ...] = (
    "gap",
    "intraday",
    "spread",
    "slippage",
    "financing",
    "fx",
)


def bar_pnl(
    previous_units: np.ndarray,
    new_units: np.ndarray,
    previous_close: np.ndarray,
    fill_price: np.ndarray,
    close: np.ndarray,
    spread_cost: np.ndarray,
    slippage_cost: np.ndarray,
    financing: np.ndarray,
    fx_cost: np.ndarray | float = 0.0,
) -> dict[str, np.ndarray]:
    """Decompose one bar's P&L per asset.

    Pure and vectorised. Costs, financing and FX arrive as positive numbers
    and are negated here, so every returned component is signed the same
    way: positive helps the account.

    ``fx_cost`` defaults to zero, which is what makes every pre-FX caller
    and every single-currency run produce exactly the numbers it did before.

    Returns
    -------
    dict
        The six :data:`PNL_COMPONENTS` plus ``total``.
    """
    gap = np.where(
        np.isfinite(previous_close) & np.isfinite(fill_price),
        previous_units * (fill_price - previous_close),
        0.0,
    )
    intraday = np.where(
        np.isfinite(fill_price) & np.isfinite(close),
        new_units * (close - fill_price),
        0.0,
    )

    components = {
        "gap": gap,
        "intraday": intraday,
        "spread": -spread_cost,
        "slippage": -slippage_cost,
        "financing": -financing,
        "fx": -np.broadcast_to(
            np.asarray(fx_cost, dtype="float64"), np.shape(gap)
        ).copy(),
    }
    components["total"] = sum(components.values())
    return components


def fx_close_costs(
    reference_price: np.ndarray,
    closed_units: np.ndarray,
    fx_rates: np.ndarray,
    fx_config,
    *,
    realized_pnl=None,
    financing=None,
    converts=None,
) -> dict[str, np.ndarray]:
    """The cost of converting back into the account currency on a close.

    The integration point named in the design: repatriation is a P&L event,
    so it lives beside the rest of the P&L. A thin wrapper over
    :func:`cfdinvest.execution.fx.apply_fx_conversion_close`.

    ``reference_price`` is expected already converted to the account
    currency; it is turned back into the instrument's own currency here so
    that the FX module works in the units it documents.
    """
    return fx_module.apply_fx_conversion_close(
        fx_module.convert_from_account_currency(reference_price, fx_rates),
        closed_units,
        fx_rates,
        fx_config,
        realized_pnl=realized_pnl,
        financing=financing,
        converts=converts,
    )


def realized_on_close(
    previous_units: np.ndarray,
    new_units: np.ndarray,
    entry_price: np.ndarray,
    fill_price: np.ndarray,
) -> np.ndarray:
    """P&L crystallised by the portion of a position that was closed.

    Only the *reduced* part realises. A position cut from 10 to 4 units
    realises on 6; one increased from 4 to 10 realises nothing. A flip from
    +10 to -3 realises the whole 10, because the original position is gone.

    Pure and vectorised.

    Parameters
    ----------
    entry_price:
        Weighted-average entry of the position being closed.

    Returns
    -------
    numpy.ndarray
        Signed P&L, zero where nothing closed.
    """
    reduced_by_flip = np.sign(previous_units) != np.sign(new_units)
    shrunk = np.abs(new_units) < np.abs(previous_units)

    closed_units = np.where(
        reduced_by_flip & (previous_units != 0),
        previous_units,
        np.where(shrunk, previous_units - new_units, 0.0),
    )

    valid = np.isfinite(entry_price) & np.isfinite(fill_price)
    return np.where(valid, closed_units * (fill_price - entry_price), 0.0)


def equity_curve(total_pnl: pd.Series, initial_equity: float) -> pd.Series:
    """Accumulate bar P&L into a mark-to-market equity path.

    Pure.

    Raises
    ------
    ValueError
        If the starting equity is not positive; every ratio downstream
        (leverage, drawdown, stop-out) divides by it.
    """
    if initial_equity <= 0:
        raise ValueError(f"initial_equity must be > 0, got {initial_equity}")
    if total_pnl.empty:
        return pd.Series(dtype="float64", name="equity")

    equity = initial_equity + total_pnl.fillna(0.0).cumsum()
    equity.name = "equity"
    return equity


def drawdown(equity: pd.Series) -> pd.DataFrame:
    """Peak, drawdown and fractional drawdown at every point.

    Pure. The peak is a *running* maximum, so the result is causal and can
    be used inside the engine's stop-out check without look-ahead.

    Returns
    -------
    pandas.DataFrame
        Columns ``peak``, ``drawdown`` (negative or zero) and
        ``drawdown_fraction``.
    """
    if equity.empty:
        return pd.DataFrame(columns=["peak", "drawdown", "drawdown_fraction"])

    peak = equity.cummax()
    absolute = equity - peak
    return pd.DataFrame(
        {
            "peak": peak,
            "drawdown": absolute,
            "drawdown_fraction": absolute / peak.where(peak > 0),
        }
    )


def summarise(
    pnl: pd.DataFrame, equity: pd.Series, initial_equity: float
) -> dict[str, float]:
    """Headline figures for a run.

    Deliberately minimal: Sharpe, Sortino and the rest belong to the
    analytics layer, which is not built yet. What is here is what a caller
    needs to see whether the *simulation* behaved, not whether the strategy
    did.
    """
    if equity.empty:
        return {}

    total = float(equity.iloc[-1] - initial_equity)
    curve = drawdown(equity)

    return {
        "initial_equity": float(initial_equity),
        "final_equity": float(equity.iloc[-1]),
        "total_pnl": total,
        "total_return": total / initial_equity,
        "max_drawdown": float(curve["drawdown"].min()),
        "max_drawdown_fraction": float(curve["drawdown_fraction"].min()),
        "gross_gap_pnl": float(pnl["gap"].sum()) if "gap" in pnl else 0.0,
        "gross_intraday_pnl": float(pnl["intraday"].sum()) if "intraday" in pnl else 0.0,
        "total_spread": float(-pnl["spread"].sum()) if "spread" in pnl else 0.0,
        "total_slippage": float(-pnl["slippage"].sum()) if "slippage" in pnl else 0.0,
        "total_financing": float(-pnl["financing"].sum()) if "financing" in pnl else 0.0,
        "total_fx": float(-pnl["fx"].sum()) if "fx" in pnl else 0.0,
    }
