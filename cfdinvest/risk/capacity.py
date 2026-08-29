"""How many positions an account can actually hold.

The number was fixed at eight, which was right for one balance and wrong
for every other. An account of five thousand pounds and one of fifty
thousand do not have the same capacity, and nothing about the strategy says
they should.

What sets the ceiling is not the strategy at all -- it is **granularity**.
Capital quotes a minimum deal size per instrument, and at the observed
prices those minimum tickets run from about £2 to £137. Exposure split
across too many slots leaves each one worth barely more than a single
minimum ticket, and a position that can only be held in one indivisible
lump cannot be sized by conviction, cannot be trimmed, and quantises every
sizing decision to nothing. The limit binds long before diversification
stops helping.

So the ceiling is derived: how many slots leave each one worth a
comfortable multiple of what the instruments in the universe actually cost
to enter. That figure comes from the broker's own quotes, which is the
point -- it moves with prices and with the balance rather than being
asserted once in a config file.

**Absence is named.** With no ticket readings this returns ``None`` rather
than a guess, and the caller keeps whatever it was already using. A capacity
computed from no data is the kind of number that looks measured and is not.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Mapping, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "Capacity",
    "position_capacity",
    "GRANULARITY_MULTIPLE",
    "TICKET_PERCENTILE",
]

#: How many minimum tickets a slot must be worth before it counts as
#: sizable. **A judgement, not a measurement, and recorded as one.**
#:
#: At 1 a position can only be held in exactly one lump: no conviction
#: sizing, no trimming, and any change is all-or-nothing. At 4 the smallest
#: expressible change is a quarter of the position, which is roughly the
#: granularity the sizing logic already works in. Higher would be safer and
#: costs capacity directly, so it is not free.
GRANULARITY_MULTIPLE = 4.0

#: Which instrument the slot must be able to afford. Not the dearest: one
#: expensive name would otherwise collapse capacity for the whole book,
#: and the screen may well not select it. At the 90th percentile a slot can
#: still enter nine names in ten.
TICKET_PERCENTILE = 90.0


@dataclass(frozen=True, slots=True)
class Capacity:
    """How many slots, and what decided it."""

    #: ``None`` when it could not be computed. Never a fallback number.
    slots: int | None
    #: What set the answer: ``granularity``, ``concentration``, ``universe``
    #: or ``unmeasured``.
    binding: str
    notional_per_slot: float | None = None
    ticket: float | None = None
    note: str = ""

    def describe(self) -> str:
        if self.slots is None:
            return f"capacity not computed ({self.note or self.binding})"
        per = f", £{self.notional_per_slot:,.0f} per slot" if self.notional_per_slot else ""
        return f"{self.slots} slots, limited by {self.binding}{per}"


def minimum_tickets(readings: Sequence[Mapping]) -> dict[str, float]:
    """What it costs to open the smallest permitted position, per instrument.

    ``min_deal_size`` units at the offer. Readings taken with the exchange
    shut are kept here deliberately -- unlike a spread, a minimum deal size
    is a contract term rather than a market condition, and it does not widen
    overnight. What is dropped is a reading missing either half, because a
    ticket computed from a default is not a measurement.
    """
    out: dict[str, float] = {}
    for row in readings or ():
        ticker = str((row or {}).get("ticker") or "")
        size = (row or {}).get("min_deal_size")
        price = (row or {}).get("offer") or (row or {}).get("bid")
        if not ticker or not size or not price:
            continue
        try:
            ticket = float(size) * float(price)
        except (TypeError, ValueError):
            continue
        if ticket > 0:
            # Latest wins: prices move and the newest reading is the one
            # that describes what a trade would cost now.
            out[ticker] = ticket
    return out


def position_capacity(
    balance: float | None,
    tickets: Mapping[str, float] | None,
    *,
    max_leverage: float | None = None,
    max_instrument_concentration: float | None = None,
    universe_size: int | None = None,
) -> Capacity:
    """Slots this balance can hold, or ``None`` if it cannot be computed."""
    if not balance or balance <= 0:
        return Capacity(None, "unmeasured", note="no balance to size against")
    if not tickets:
        return Capacity(None, "unmeasured", note="no minimum deal sizes recorded")

    leverage = float(max_leverage or 1.0)
    exposure = float(balance) * (leverage if leverage > 0 else 1.0)
    ticket = _percentile(sorted(tickets.values()), TICKET_PERCENTILE)
    if ticket <= 0:
        return Capacity(None, "unmeasured", note="minimum deal sizes are zero")

    # The ceiling: every slot must be worth several minimum tickets.
    by_granularity = int(exposure // (GRANULARITY_MULTIPLE * ticket))

    # The floor: with a cap of 20% on any one instrument, fewer than five
    # positions cannot deploy the book however large the balance is. This
    # is a floor on *useful* capacity, so it is only applied where
    # granularity allows it -- raising it past what the account can size
    # would be asserting capacity rather than computing it.
    floor = 0
    concentration = float(max_instrument_concentration or 0.0)
    if 0 < concentration < 1:
        floor = math.ceil(1.0 / concentration)

    slots, binding = by_granularity, "granularity"
    if floor and by_granularity >= floor and slots < floor:
        slots, binding = floor, "concentration"
    if universe_size and slots > int(universe_size):
        slots, binding = int(universe_size), "universe"

    if slots <= 0:
        return Capacity(
            None, "unmeasured",
            ticket=ticket,
            note=(
                f"a balance of {balance:,.0f} at {leverage:g}x cannot fund one "
                f"slot worth {GRANULARITY_MULTIPLE:g} minimum tickets of "
                f"{ticket:,.0f}"
            ),
        )
    return Capacity(
        slots=slots, binding=binding,
        notional_per_slot=exposure / slots, ticket=ticket,
    )


def _percentile(values: Sequence[float], pct: float) -> float:
    """Linear-interpolated percentile. Small samples are the normal case."""
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    position = (len(values) - 1) * (pct / 100.0)
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return float(values[int(position)])
    return float(values[low] + (values[high] - values[low]) * (position - low))
