"""Every number the system recommended, gathered in one place.

The point of this module is a single line in the spec: **the user never
performs calculations.** Before confirming a fill you should be able to see
what was suggested — quantity, entry, stop, take-profit, exposure, leverage —
without opening three pages and multiplying anything.

Two things make that harder than reading one artefact.

**The run is not your account.** The simulation runs at whatever equity it was
configured with; you hold something else. A recommended 305 units at a
simulated £100,000 is 6.1 units at a real £2,000, and asking a person to do
that division at the moment they are typing a live order is asking for the
mistake. Both figures are reported, with the ratio that connects them.

**The recommendation is spread across artefacts.** Units come from
``positions.parquet``, the intended fill and the stop and target from
``trades.parquet``, the reasoning from ``signals.parquet``. None is complete
alone.

Nothing is inferred. Every field that could not be read is named in
``absent``, because a confirmation screen showing a blank stop is telling you
something quite different from one showing no stop was recommended.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

__all__ = ["recommendation_for", "recommended_tickers", "account_scale"]


def _finite(value: Any) -> bool:
    try:
        return value is not None and np.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def account_scale(run_initial_equity: Any, account_balance: Any) -> float | None:
    """How much of the simulated size your account is good for.

    ``None`` when either figure is missing — a scale of 1.0 would silently
    hand you the simulation's size, which at a hundredfold difference is not a
    rounding error.
    """
    if not (_finite(run_initial_equity) and _finite(account_balance)):
        return None
    equity, balance = float(run_initial_equity), float(account_balance)
    if equity <= 0 or balance <= 0:
        return None
    return balance / equity


def recommended_tickers(config=None, run_id=None, account=None) -> list[str]:
    """Instruments the latest run holds a position in, plus any it traded."""
    from cfdinvest.persist import readers

    names: set[str] = set()
    positions = readers.load_frame("positions", run_id, config, account)
    if not positions.empty:
        latest = positions.iloc[-1]
        names |= {
            str(name)
            for name, value in latest.items()
            if _finite(value) and float(value) != 0.0
        }
    trades = readers.load_frame("trades", run_id, config, account)
    if not trades.empty and "asset" in trades.columns:
        names |= {str(name) for name in trades["asset"].dropna().unique()}
    return sorted(names)


def recommendation_for(
    ticker: str,
    config=None,
    *,
    run_id: str | None = None,
    account: str | None = None,
    account_balance: float | None = None,
) -> dict[str, Any]:
    """What the system suggested for one instrument, with nothing computed.

    ``scaled_quantity`` is the figure to type into the ticket when
    ``account_balance`` is supplied; ``quantity`` is what the simulation held.
    They are both reported, always, so it is visible which one is which.
    """
    from cfdinvest.persist import readers

    absent: list[str] = []
    chosen = run_id or readers.load_latest_run(config)
    result: dict[str, Any] = {
        "ticker": ticker,
        "run_id": chosen,
        "available": False,
        "flat": True,
        "direction": None,
        "quantity": None,
        "scaled_quantity": None,
        "scale": None,
        "entry_price": None,
        "stop_level": None,
        "take_profit_level": None,
        "exposure": None,
        "scaled_exposure": None,
        "leverage": None,
        "fx_rate": None,
        "costs": {},
        "reason": None,
        "confidence": None,
        "as_of": None,
        "absent": ("run",),
    }
    if chosen is None:
        return result

    metadata = readers.load_execution_metadata(chosen, config, account) or {}
    scale = account_scale(metadata.get("initial_equity"), account_balance)
    if scale is None and account_balance is not None:
        absent.append("account scale")

    positions = readers.load_frame("positions", chosen, config, account)
    quantity = None
    as_of = None
    if not positions.empty and ticker in positions.columns:
        as_of = positions.index[-1]
        raw = positions.iloc[-1][ticker]
        quantity = float(raw) if _finite(raw) else None
    if quantity is None:
        absent.append("position")

    trades = readers.load_frame("trades", chosen, config, account)
    entry = stop = target = fx_rate = None
    costs: dict[str, float] = {}
    if not trades.empty and "asset" in trades.columns:
        mine = trades[trades["asset"].astype(str) == ticker]
        if not mine.empty:
            last = mine.iloc[-1]

            def value(column: str):
                raw = last.get(column)
                return float(raw) if _finite(raw) else None

            entry = value("fill_price") or value("price")
            stop = value("stop_level")
            target = value("take_profit_level")
            fx_rate = value("fx_rate")
            costs = {
                name: value(name)
                for name in (
                    "spread_cost", "slippage", "financing",
                    "fx_spread", "fx_conversion_fee", "fx_slippage",
                )
                if value(name) is not None
            }
    for name, found in (
        ("entry price", entry), ("stop", stop), ("take-profit", target)
    ):
        if found is None:
            absent.append(name)

    direction = None
    reason = confidence = None
    signals = readers.load_frame("signals", chosen, config, account)
    if not signals.empty:
        try:
            mine = signals.xs(ticker, level="ticker")
        except (KeyError, TypeError):
            mine = signals[signals.index.get_level_values(-1) == ticker] \
                if isinstance(signals.index, pd.MultiIndex) else signals.iloc[0:0]
        if not mine.empty:
            last = mine.iloc[-1]
            direction = str(last.get("direction")) if last.get("direction") else None
            reason = str(last.get("reason")) if last.get("reason") else None
            raw = last.get("confidence")
            confidence = float(raw) if _finite(raw) else None
    if direction is None and quantity is not None and quantity != 0:
        direction = "long" if quantity > 0 else "short"
    if direction is None:
        absent.append("direction")

    exposure = (
        abs(quantity * entry)
        if quantity is not None and entry is not None
        else None
    )

    result.update(
        {
            "available": quantity is not None or entry is not None,
            # The signal can say "long" while the position is flat -- the run
            # closed out. Reporting only the direction would read as an
            # instruction to open one.
            "flat": quantity is None or quantity == 0.0,
            "direction": direction,
            "quantity": quantity,
            "scaled_quantity": (
                quantity * scale if quantity is not None and scale else None
            ),
            "scale": scale,
            "entry_price": entry,
            "stop_level": stop,
            "take_profit_level": target,
            "exposure": exposure,
            "scaled_exposure": (
                exposure * scale if exposure is not None and scale else None
            ),
            "leverage": metadata.get("max_leverage"),
            "fx_rate": fx_rate,
            "costs": costs,
            "reason": reason,
            "confidence": confidence,
            "as_of": as_of,
            "absent": tuple(dict.fromkeys(absent)),
        }
    )
    return result
