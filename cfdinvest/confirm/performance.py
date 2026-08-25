"""What your recorded trades actually returned, after everything.

Every performance figure in this project has until now come from the
**simulated** book: the backtest's own trades, scored by the analytics
layer. That is the right way to judge a strategy and the wrong way to judge
your account, because it never paid your costs.

This measures the other thing. It pairs the confirmations you recorded into
completed round trips and values each one on the terms you recorded it with
-- the exchange rate, the leverage, the financing rate -- so the number is
what reached your account rather than the price difference.

Why that gap matters here specifically: on the backtest, financing was the
single largest cost line, larger than spread. A performance figure computed
from price movement alone flatters a leveraged, multi-week strategy by
exactly the amount it costs to carry.

How positions are paired
------------------------
The same rule the rest of the project uses: a confirmation states what the
position **is** after the fill, not a delta, and the replay is last-write-
wins per instrument. So a second ``open`` on an instrument already open is
read as a *correction* to the same position, not a second position -- which
is what makes correcting a mistyped size work rather than inventing a trade
that never happened.

A round trip completes on ``close``, or on ``flip``, which closes and
reopens in one action.

Absences are named, never assumed. A trip whose terms were not recorded
reports the figures it can and lists what it could not compute, rather than
valuing it at an exchange rate nobody supplied.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

logger = logging.getLogger(__name__)

__all__ = ["RealisedTrade", "realised_trades", "summarise"]


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


@dataclass(frozen=True, slots=True)
class RealisedTrade:
    """One completed round trip, valued on the terms it was opened under."""

    ticker: str
    direction: str
    quantity: float
    open_price: float | None = None
    close_price: float | None = None
    opened_at: Any = None
    closed_at: Any = None
    days_held: float = 0.0

    gross_pnl: float | None = None
    gross_pnl_gbp: float | None = None
    fx_fee_gbp: float | None = None
    financing_gbp: float | None = None
    net_pnl_gbp: float | None = None
    missing: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return self.net_pnl_gbp is not None

    @property
    def won(self) -> bool | None:
        """``None`` when it could not be valued -- not ``False``."""
        return None if self.net_pnl_gbp is None else self.net_pnl_gbp > 0


def _days_between(opened, closed) -> float:
    import pandas as pd

    try:
        start, end = pd.Timestamp(opened), pd.Timestamp(closed)
    except (TypeError, ValueError):
        return 0.0
    if pd.isna(start) or pd.isna(end):
        return 0.0
    return max((end - start).total_seconds() / 86_400.0, 0.0)


def _value(entry, close_price, closed_at) -> RealisedTrade:
    """Value one completed trip through the ticket calculator."""
    from cfdinvest.livehelper.ticket import build_ticket, close_out

    quantity = float(entry["quantity"])
    direction = "short" if quantity < 0 else "long"
    days = _days_between(entry.get("at"), closed_at)

    ticket = build_ticket(
        entry["ticker"], direction, abs(quantity), entry.get("price"),
        currency=entry.get("currency") or "GBP",
        fx_rate=entry.get("fx_rate"),
        leverage=entry.get("leverage"),
        financing_rate=entry.get("financing_rate"),
        fx_fee_applies=entry.get("fx_fee_applies"),
    )
    result = close_out(ticket, close_price, days_held=days)

    # Leverage does not enter the P&L -- it decides the margin, not the
    # result -- so a trip missing only that is still fully valued.
    missing = tuple(name for name in result.missing if name != "leverage")

    return RealisedTrade(
        ticker=entry["ticker"],
        direction=direction,
        quantity=abs(quantity),
        open_price=_finite(entry.get("price")),
        close_price=_finite(close_price),
        opened_at=entry.get("at"),
        closed_at=closed_at,
        days_held=days,
        gross_pnl=result.gross_pnl,
        gross_pnl_gbp=result.gross_pnl_gbp,
        fx_fee_gbp=result.fx_fee_gbp,
        financing_gbp=result.financing_gbp,
        net_pnl_gbp=result.net_pnl_gbp,
        missing=missing,
    )


def realised_trades(confirmations: Iterable[Any]) -> list[RealisedTrade]:
    """Completed round trips, oldest first. Never raises.

    Open positions are not included: an unrealised move is not a result, and
    counting it as one is how a losing book reports a win rate.
    """
    import pandas as pd

    try:
        ordered = sorted(confirmations or (), key=lambda c: c.timestamp)
    except Exception as error:  # noqa: BLE001
        logger.warning("Could not order confirmations: %s", error)
        return []

    live: dict[str, dict[str, Any]] = {}
    done: list[RealisedTrade] = []

    for record in ordered:
        action = getattr(record, "action", "")
        ticker = getattr(record, "ticker", "")
        if action == "ignore" or not ticker:
            continue

        terms = {
            "ticker": ticker,
            "quantity": float(getattr(record, "quantity", 0.0) or 0.0),
            "price": getattr(record, "price", None),
            "at": getattr(record, "timestamp", None),
            "currency": getattr(record, "currency", None),
            "fx_rate": getattr(record, "fx_rate", None),
            "leverage": getattr(record, "leverage", None),
            "financing_rate": getattr(record, "financing_rate", None),
            "fx_fee_applies": getattr(record, "fx_fee_applies", None),
        }

        if action == "close":
            held = live.pop(ticker, None)
            if held is not None:
                done.append(_value(
                    held, getattr(record, "price", None),
                    getattr(record, "timestamp", None),
                ))
            continue

        if action == "flip":
            held = live.pop(ticker, None)
            if held is not None:
                # The closing leg's own price when the ticket recorded one,
                # otherwise the price the new side opened at.
                exit_price = (
                    getattr(record, "close_price", None)
                    or getattr(record, "price", None)
                )
                done.append(_value(
                    held, exit_price, getattr(record, "timestamp", None)
                ))
            if terms["quantity"]:
                live[ticker] = terms
            continue

        # "open": states what the position is now. A second one on an
        # instrument already open is a correction to the same position, not
        # a new one -- so the entry is replaced and no trip completes.
        if terms["quantity"]:
            live[ticker] = terms
        else:
            live.pop(ticker, None)

    return done


def summarise(trades: Sequence[RealisedTrade]) -> dict[str, Any]:
    """Totals over completed trips, and what could not be counted.

    ``unvalued`` is reported rather than quietly excluded. A win rate over
    the trades that happened to carry an exchange rate is not a win rate,
    and the difference is invisible unless it is stated.
    """
    rows = list(trades or ())
    valued = [trade for trade in rows if trade.complete]
    unvalued = [trade for trade in rows if not trade.complete]

    if not valued:
        return {
            "trades": len(rows), "valued": 0, "unvalued": len(unvalued),
            "net_pnl_gbp": None, "gross_pnl_gbp": None, "costs_gbp": None,
            "win_rate": None, "expectancy_gbp": None,
            "missing": sorted({name for trade in unvalued for name in trade.missing}),
        }

    net = sum(trade.net_pnl_gbp for trade in valued)
    gross = sum(trade.gross_pnl_gbp or 0.0 for trade in valued)
    fees = sum(trade.fx_fee_gbp or 0.0 for trade in valued)
    carry = sum(trade.financing_gbp or 0.0 for trade in valued)
    wins = sum(1 for trade in valued if trade.won)

    return {
        "trades": len(rows),
        "valued": len(valued),
        "unvalued": len(unvalued),
        "net_pnl_gbp": net,
        "gross_pnl_gbp": gross,
        "fx_fees_gbp": fees,
        "financing_gbp": carry,
        "costs_gbp": fees + carry,
        "win_rate": wins / len(valued),
        # Per completed trade, after costs. The figure that says whether
        # doing this again is worth it.
        "expectancy_gbp": net / len(valued),
        "missing": sorted({name for trade in unvalued for name in trade.missing}),
    }
