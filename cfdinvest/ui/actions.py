"""What to do now: the suggestions worth acting on, and the positions to watch.

Two different clocks, and the distinction is the honest part of this module.

**Suggestions change once a day.** They come from signals computed on daily
bars by the 03:05 UTC pipeline. Nothing here can produce a new suggestion
intraday, because the strategy has no intraday input -- and it has none
deliberately: ingesting intraday bars is excluded, so a second source of
truth for the same instrument cannot appear.

**Open positions are watched every five minutes.** The quote poller prices
what you actually hold, so unrealised P&L, distance to a stop and the value
at risk move with the market. That is where "watching in real time" is real.

Saying that plainly on the page matters more than it sounds: a suggestion
list that looks live but refreshes daily is worse than one labelled daily,
because you would read a stale absence as an all-clear.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_CONFIDENCE",
    "Suggestion",
    "Watch",
    "suggested_trades",
    "positions_to_watch",
]

#: The bar a suggestion must clear to be shown at all.
#:
#: Confidence is ``conviction x component agreement x environment support``,
#: three factors in [0, 1] multiplied -- so 0.6 needs all three strong, and
#: most days nothing clears it. That is the point. It is **not** a probability
#: of profit and must never be labelled as one.
DEFAULT_CONFIDENCE = 0.60


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


@dataclass(frozen=True, slots=True)
class Suggestion:
    """One trade worth considering, with the amount to place."""

    ticker: str
    direction: str
    confidence: float
    size_fraction: float
    price: float | None = None
    price_source: str = ""
    cash: float | None = None
    quantity: float | None = None
    reason: str = ""
    t212_name: str = ""
    tradable: bool = True

    @property
    def actionable(self) -> bool:
        """Whether there is enough here to place the trade."""
        return self.tradable and self.quantity is not None and self.quantity > 0


@dataclass(frozen=True, slots=True)
class Watch:
    """One open position, priced now, and whether it wants attention."""

    ticker: str
    quantity: float
    entry: float | None = None
    price: float | None = None
    price_source: str = ""
    pnl: float | None = None
    pnl_fraction: float | None = None
    stop: float | None = None
    distance_to_stop: float | None = None
    severity: str = "pass"
    note: str = ""

    @property
    def needs_attention(self) -> bool:
        return self.severity in ("warning", "critical")


def suggested_trades(
    latest: Sequence[Mapping[str, Any]] | Any,
    *,
    balance: float,
    threshold: float = DEFAULT_CONFIDENCE,
    prices: Mapping[str, Any] | None = None,
    mapping=None,
) -> tuple[list[Suggestion], int]:
    """The suggestions clearing ``threshold``, and how many were below it.

    Returns ``(kept, below)``. The count of those below is returned rather
    than the rows: you asked not to see trades that are not worth taking, and
    a number is enough to know the engine is running and simply unconvinced.

    A signal's ``size`` is a **share of equity**, not a unit count -- the
    conversion is ``cash = size x balance`` and ``quantity = cash / price``.
    Getting that wrong once made a whole simulation a no-op, so it is done in
    one place and only here.
    """
    rows = list(latest or [])
    kept: list[Suggestion] = []
    below = 0

    for row in rows:
        direction = str(row.get("direction") or "flat").lower()
        if direction == "flat":
            continue

        confidence = _finite(row.get("confidence")) or 0.0
        if confidence < float(threshold):
            below += 1
            continue

        ticker = str(row.get("ticker") or row.get("Ticker") or "").strip()
        size = _finite(row.get("size")) or 0.0
        price, source = None, ""
        if prices is not None:
            found = prices.get(ticker)
            if isinstance(found, tuple):
                price, source = _finite(found[0]), str(found[1])
            else:
                price = _finite(found)

        cash = None if not balance else abs(size) * float(balance)
        quantity = None if (price is None or not cash or price <= 0) else cash / price

        name, tradable = "", True
        if mapping is not None:
            entry = mapping(ticker)
            name = getattr(entry, "name", "") or ""
            tradable = bool(getattr(entry, "tradable", True))

        kept.append(Suggestion(
            ticker=ticker,
            direction=direction,
            confidence=confidence,
            size_fraction=abs(size),
            price=price,
            price_source=source,
            cash=cash,
            quantity=quantity,
            reason=str(row.get("reason") or ""),
            t212_name=name,
            tradable=tradable,
        ))

    kept.sort(key=lambda s: s.confidence, reverse=True)
    return kept, below


def positions_to_watch(
    book: Mapping[str, Mapping[str, Any]] | None,
    *,
    prices: Mapping[str, Any] | None = None,
    stops: Mapping[str, Any] | None = None,
    near_stop_fraction: float = 0.20,
) -> list[Watch]:
    """Every open position, priced now, worst first.

    ``near_stop_fraction`` is how close to the stop counts as worth saying --
    within a fifth of the distance from entry to stop. A position that has
    travelled most of the way to its stop is the one thing on this page that
    genuinely wants a decision today.

    A position whose price could not be read is reported ``unchecked``, never
    calm: not knowing what something is worth is not the same as it being
    fine, and this is the page you would look at to decide you need do
    nothing.
    """
    watches: list[Watch] = []

    for ticker, entry in dict(book or {}).items():
        quantity = _finite((entry or {}).get("quantity")) or 0.0
        if not quantity:
            continue

        opened = _finite((entry or {}).get("price"))
        price, source = None, ""
        found = (prices or {}).get(str(ticker))
        if isinstance(found, tuple):
            price, source = _finite(found[0]), str(found[1])
        else:
            price = _finite(found)

        stop = _finite((stops or {}).get(str(ticker)))
        pnl = pnl_fraction = distance = None
        severity, note = "pass", ""

        if price is None:
            severity = "unchecked"
            note = "No price available, so this position is not being valued."
        else:
            if opened is not None:
                pnl = (price - opened) * quantity
                if opened:
                    pnl_fraction = (price - opened) / opened * (
                        1.0 if quantity > 0 else -1.0
                    )
            if stop is not None and opened is not None and opened != stop:
                travelled = (opened - price) / (opened - stop)
                distance = 1.0 - travelled
                if distance <= 0:
                    severity = "critical"
                    note = "Past its stop. This is the one to deal with now."
                elif distance <= near_stop_fraction:
                    severity = "warning"
                    note = f"Within {distance:.0%} of its stop."

        watches.append(Watch(
            ticker=str(ticker), quantity=quantity, entry=opened, price=price,
            price_source=source, pnl=pnl, pnl_fraction=pnl_fraction, stop=stop,
            distance_to_stop=distance, severity=severity, note=note,
        ))

    order = {"critical": 0, "warning": 1, "unchecked": 2, "pass": 3}
    watches.sort(key=lambda w: (order.get(w.severity, 9), w.ticker))
    return watches
