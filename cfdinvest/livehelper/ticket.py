"""What the Trading 212 ticket will show, worked out before you type it in.

Six numbers appear on a CFD ticket and only two of them are yours to choose:
the price you accept and the size you take. The rest -- margin, the value in
your own currency, the overnight interest, the FX fee at close -- fall out of
those two plus three facts about the instrument. This computes them, so the
arithmetic happens here rather than in your head at the moment you commit.

What is derived and what must be supplied
-----------------------------------------
Derived: notional, margin, position value in GBP, financing per day, the
0.5% FX fee at close, and the net result of a round trip.

Supplied, and **never guessed**:

``leverage``
    Trading 212 sets it per instrument and it differs by asset class. A
    wrong one misstates the margin by a multiple.
``fx_rate``
    GBP per one unit of the instrument's currency. A US share CFD is priced
    in dollars and settles against a sterling account; without the rate the
    GBP figures are unknowable, not approximately right.
``financing_rate``
    The annual rate on the notional. Nothing publishes these.

Anything that depends on a missing input is reported ``None`` and named as
absent. That is the rule this project runs on: "not measured" and "measured
and fine" are different statements, and a margin figure computed from an
assumed leverage would look exactly as authoritative as a real one.

The 0.5% fee is charged on the **result**, not the notional -- Trading 212
applies it to the profit or loss when the position closes. That is the
``"result"`` basis in :mod:`cfdinvest.execution.fx`, and the difference
between the two bases is worth an order of magnitude.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "DAYS_PER_YEAR",
    "TradeTicket",
    "RoundTrip",
    "build_ticket",
    "close_out",
]

#: Trading 212 charges financing on calendar days, not trading days -- a
#: position held over a weekend pays for the weekend.
DAYS_PER_YEAR = 365.0


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _positive(value: Any) -> float | None:
    number = _finite(value)
    return number if number is not None and number > 0 else None


@dataclass(frozen=True, slots=True)
class TradeTicket:
    """One position as the ticket will show it, in both currencies."""

    ticker: str
    direction: str
    quantity: float
    price: float
    currency: str = "GBP"
    fx_rate: float | None = None
    leverage: float | None = None
    financing_rate: float | None = None
    #: Whether Trading 212 charges its 0.5% conversion fee on the result.
    #:
    #: Recorded, never inferred from the currency. USA 500 is bought in GBP
    #: and still charges it, because the market underneath is American --
    #: while UK 100 is bought in GBP and does not. Inferring it from the
    #: quoted currency got that pair exactly backwards.
    fx_fee_applies: bool | None = None

    notional: float | None = None
    notional_gbp: float | None = None
    margin_gbp: float | None = None
    financing_per_day_gbp: float | None = None
    missing: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        """Whether every figure on the ticket could be worked out."""
        return not self.missing

    def describe(self) -> dict[str, str]:
        """The ticket as labelled text, absences included."""
        dash = "—"

        def money(value, symbol="£"):
            return dash if value is None else f"{symbol}{value:,.2f}"

        return {
            "Direction": self.direction,
            "Units": f"{self.quantity:,.4f}",
            "Price": f"{self.price:,.4f} {self.currency}",
            "Position value": money(self.notional, "" if self.currency == "GBP" else "")
            + f" {self.currency}",
            "Value in GBP": money(self.notional_gbp),
            "Leverage": dash if self.leverage is None else f"{self.leverage:,.0f}:1",
            "Margin required": money(self.margin_gbp),
            "Overnight interest": (
                dash if self.financing_per_day_gbp is None
                else f"{money(self.financing_per_day_gbp)} per day"
            ),
        }


@dataclass(frozen=True, slots=True)
class RoundTrip:
    """What a closed position actually returned, after everything."""

    gross_pnl: float | None = None
    gross_pnl_gbp: float | None = None
    fx_fee_gbp: float | None = None
    financing_gbp: float | None = None
    net_pnl_gbp: float | None = None
    days_held: float = 0.0
    missing: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.missing


def build_ticket(
    ticker: str,
    direction: str,
    quantity: Any,
    price: Any,
    *,
    currency: str = "GBP",
    fx_rate: Any = None,
    leverage: Any = None,
    financing_rate: Any = None,
    fx_fee_applies: bool | None = None,
) -> TradeTicket:
    """Work out the ticket. Never raises; absences are reported, not filled.

    ``fx_rate`` is account currency per one unit of instrument currency --
    0.79 GBP/USD, not 1.27 USD/GBP. Getting it upside down is the classic
    error here and it misstates every GBP figure by the square of the rate,
    so a GBP instrument ignores it entirely rather than accepting a rate it
    would then apply.
    """
    units = _finite(quantity)
    unit_price = _finite(price)
    missing: list[str] = []

    if units is None or unit_price is None:
        return TradeTicket(
            ticker=str(ticker), direction=str(direction).lower(),
            quantity=units or 0.0, price=unit_price or 0.0, currency=str(currency),
            fx_fee_applies=fx_fee_applies,
            missing=("quantity or price",),
        )

    notional = abs(units) * unit_price

    same_currency = str(currency).upper() == "GBP"
    rate = 1.0 if same_currency else _positive(fx_rate)
    if rate is None:
        missing.append("fx_rate")
    notional_gbp = None if rate is None else notional * rate

    lever = _positive(leverage)
    if lever is None:
        missing.append("leverage")
    margin_gbp = (
        None if (lever is None or notional_gbp is None) else notional_gbp / lever
    )

    annual = _finite(financing_rate)
    if annual is None:
        missing.append("financing_rate")
    per_day = (
        None if (annual is None or notional_gbp is None)
        else notional_gbp * annual / DAYS_PER_YEAR
    )

    return TradeTicket(
        ticker=str(ticker),
        direction=str(direction).lower(),
        quantity=units,
        price=unit_price,
        currency=str(currency).upper(),
        fx_rate=rate,
        leverage=lever,
        financing_rate=annual,
        notional=notional,
        notional_gbp=notional_gbp,
        margin_gbp=margin_gbp,
        financing_per_day_gbp=per_day,
        fx_fee_applies=fx_fee_applies,
        missing=tuple(missing),
    )


def close_out(
    ticket: TradeTicket,
    close_price: Any,
    *,
    days_held: Any = 0.0,
    fx_fee_pct: float | None = None,
) -> RoundTrip:
    """What the round trip returned, after financing and the FX fee.

    The fee is charged on the **result**: 0.5% of the profit or loss, not of
    the notional. It is charged on a loss too -- it is a conversion cost, not
    a tax on winning -- so it is applied to the absolute result and always
    subtracted.
    """
    from cfdinvest.execution.fx import FXConfig

    exit_price = _finite(close_price)
    if exit_price is None or ticket.price is None:
        return RoundTrip(missing=("close_price",), days_held=_finite(days_held) or 0.0)

    fee_pct = (
        FXConfig().fx_conversion_fee_pct if fx_fee_pct is None else float(fx_fee_pct)
    )
    held = max(_finite(days_held) or 0.0, 0.0)
    missing = list(ticket.missing)

    sign = -1.0 if ticket.direction == "short" else 1.0
    gross = (exit_price - ticket.price) * abs(ticket.quantity) * sign

    rate = ticket.fx_rate
    gross_gbp = None if rate is None else gross * rate

    # Recorded per instrument, and no longer guessed from the currency.
    #
    # The guess was that a non-GBP instrument converts and therefore pays.
    # Real tickets say otherwise, in both directions: USA 500 is funded in
    # sterling and charges it, while Germany 40 and France 40 are priced in
    # euros and do not. Currency turned out to predict nothing, so an
    # unrecorded flag is now unknown rather than assumed -- the fee is
    # reported absent and the net result withheld, which is the same rule
    # every other missing broker figure follows here.
    if ticket.fx_fee_applies is None:
        fx_fee = None
        if "fx_fee_applies" not in missing:
            missing.append("fx_fee_applies")
    elif not ticket.fx_fee_applies:
        fx_fee = 0.0
    elif gross_gbp is None:
        fx_fee = None
    else:
        fx_fee = abs(gross_gbp) * fee_pct

    financing = (
        None if ticket.financing_per_day_gbp is None
        else ticket.financing_per_day_gbp * held
    )

    net = (
        None if (gross_gbp is None or fx_fee is None or financing is None)
        else gross_gbp - fx_fee - financing
    )
    if net is None and "close_price" not in missing:
        for name, value in (("fx_rate", gross_gbp), ("financing_rate", financing)):
            if value is None and name not in missing:
                missing.append(name)

    return RoundTrip(
        gross_pnl=gross,
        gross_pnl_gbp=gross_gbp,
        fx_fee_gbp=fx_fee,
        financing_gbp=financing,
        net_pnl_gbp=net,
        days_held=held,
        missing=tuple(missing),
    )
