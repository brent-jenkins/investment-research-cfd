"""Capital.com's terms, in the shape the shadow book already understands.

The book was written against Trading 212 and takes its rules through one
seam: ``open_trade(..., rules=...)``. This builds the same shape from
Capital's figures, so the book prices what the demo account would actually
have been charged without the book learning anything about brokers.

Why this exists
---------------
The shadow book and the demo account are supposed to hold the same intended
positions, so the difference between them measures how wrong the
*simulation* is. While the book charged Trading 212's costs and the account
paid Capital's, that difference measured two things at once -- simulation
error and venue difference -- and neither could be read off it. Pricing both
on Capital leaves only the first, which is the thing nothing else measures.

The spread is measured, not tabled
----------------------------------
Trading 212 publishes a typical spread for six of forty instruments, so the
book estimates the rest from an asset-class table. Capital quotes a live bid
and offer on every instrument on every pass, so the spread here is what was
actually showing at the moment of the decision.

That is a stronger claim than "recorded" and the book must not let the two
look alike, so these entries carry ``spread_source_label = "measured"`` and
the book writes it through. A figure read off a quote and a figure read off
a table are different evidence, and this project's whole cost history is a
record of that distinction mattering.

What is not measured
--------------------
Financing. It comes from ``config/capital_rules.toml``, read from the
broker's API but never confirmed against an actual overnight charge -- the
probe was too small and rounded to zero. ``financing_verified`` is false
there and this module refuses rather than guessing if the file is missing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)

__all__ = [
    "BROKER",
    "UNKNOWN_VENUE",
    "venue_name",
    "CapitalInstrumentRules",
    "CapitalRuleTable",
    "load_capital_terms",
    "rules_from_quotes",
]

#: The broker, without the account. Never written to a row on its own --
#: see :func:`venue_name`.
BROKER = "capital"

#: What goes on a row when the account cannot be identified. Deliberately
#: ugly and greppable: it should never appear, and if it does, somebody
#: should notice rather than read it as a default.
UNKNOWN_VENUE = "capital-unknown"


def venue_name(environment: str | None) -> str:
    """The venue stamp for one account: ``capital-demo`` or ``capital-live``.

    The environment is part of the venue, not a field beside it, so that no
    grouping can accidentally pool the two. A demo fill and a live fill are
    not the same evidence -- the demo book is a simulation the broker happens
    to run, and its fills cost nothing to be wrong about. When this account
    is switched to live, rows either side of the switch must not average
    together, and a reader who groups by venue gets that for free.

    Anything that is not exactly ``live`` is **not** assumed to be demo. An
    unrecognised environment is recorded as unknown, because "we could not
    tell which account this was" and "this was the demo account" are
    different statements and only one of them is reassuring.
    """
    name = str(environment or "").strip().lower()
    if name == "live":
        return f"{BROKER}-live"
    if name == "demo":
        return f"{BROKER}-demo"
    return UNKNOWN_VENUE


@dataclass(frozen=True, slots=True)
class CapitalInstrumentRules:
    """One instrument's Capital terms.

    Duck-types :class:`~cfdinvest.livehelper.t212_rules.InstrumentRules` for
    every field the book reads, and adds ``spread_source_label`` so a
    measured spread is not written into the record as a tabled one.
    """

    ticker: str
    leverage: float | None = None
    #: Both sides carried, and chosen at the moment of the trade rather than
    #: when the table is built. A CFD charges long and short differently --
    #: Capital's long rate is 7.86%/yr against 0.249% short -- and baking
    #: one in would misprice every trade on the other side.
    financing_annual_long: float | None = None
    financing_annual_short: float | None = None
    #: The long rate, for any caller reading the attribute directly. The
    #: dearer of the two, so a caller with no side named is charged the
    #: worse figure rather than the flattering one.
    financing_rate: float | None = None
    spread_typical: float | None = None
    currency: str | None = None
    min_quantity: float | None = None
    fx_fee_applies: bool | None = None
    close_only: bool | None = None
    available: bool | None = None
    #: Capital converts P&L at spot and publishes no separate conversion
    #: fee, unlike Trading 212's 0.5%. Absent rather than zero: nobody has
    #: measured it, and a zero would be a claim that it is free.
    fx_fee_pct: float | None = None
    source: str = "capital-api"
    #: A venue that quotes a live bid and offer on everything it will trade
    #: must not fall back to another broker's asset-class table when it
    #: quotes nothing. A missing Capital quote means the instrument is not
    #: dealable there, which is a refusal, not an invitation to estimate.
    allows_table_estimate: bool = False
    #: Empty by default: an entry with no quote measured nothing, and the
    #: label is only consulted where a spread exists.
    spread_source_label: str = ""
    financing_verified: bool = False

    def annual_financing(self, direction: str | None = None) -> float | None:
        """The annual financing fraction for one side.

        ``None`` means not recorded. Zero would say the carry is free, which
        is a measurement nobody made -- and on Trading 212 carry was the
        largest cost line in the whole backtest.
        """
        side = str(direction or "").strip().lower()
        if side == "short":
            return self.financing_annual_short
        if side == "long":
            return self.financing_annual_long
        return self.financing_rate


@dataclass(frozen=True, slots=True)
class CapitalRuleTable:
    """Every instrument's Capital terms, with the ``get`` contract the book expects."""

    rules: dict[str, CapitalInstrumentRules]
    financing_verified: bool = False
    read_on: str = ""

    def __len__(self) -> int:
        return len(self.rules)

    def __contains__(self, ticker: object) -> bool:
        return str(ticker).upper() in self.rules

    def get(self, ticker: str) -> CapitalInstrumentRules:
        """Never ``None``.

        An empty entry makes every downstream check report "not known"
        rather than "no constraint" -- the same contract ``RuleTable.get``
        keeps, and for the same reason: a caller that had to check would
        eventually forget, and the forgetting reads as permission.
        """
        name = str(ticker).upper()
        return self.rules.get(name, CapitalInstrumentRules(ticker=name))


def load_capital_terms(path=None) -> dict[str, Any]:
    """Read ``config/capital_rules.toml``.

    Raises rather than defaulting. A financing rate silently falling back to
    the engine's generic house rate would produce a plausible cost
    attributable to nothing, and the point of pricing on Capital is that
    every figure traces to the broker.
    """
    import tomllib

    location = Path(path) if path else (
        Path(__file__).resolve().parents[2] / "config" / "capital_rules.toml"
    )
    if not location.is_file():
        raise FileNotFoundError(
            f"no Capital terms at {location}; refusing to invent financing rates"
        )
    with location.open("rb") as handle:
        return tomllib.load(handle)


def rules_from_quotes(
    quotes: Mapping[str, Mapping[str, Any]],
    terms: Mapping[str, Any] | None = None,
) -> CapitalRuleTable:
    """Build the table from live bid/offer plus the recorded financing terms.

    ``quotes`` is ``{ticker: {"bid": float, "offer": float, "currency": str}}``
    as :func:`cfdinvest.broker.capital.client.fetch_market_details` returns.

    Both financing sides are carried on every entry and chosen when the
    trade is struck, so a short is never charged the long rate.

    An instrument with no usable quote gets an entry with **every** term
    absent -- not merely the spread. A half-populated entry would let the
    book charge Capital's financing beside a spread estimated from Trading
    212's class table, which is one row struck on two venues.
    """
    document = terms if terms is not None else load_capital_terms()
    financing = document.get("financing", {}) or {}
    margin = document.get("margin", {}) or {}
    minimums = document.get("min_deal_size", {}) or {}

    long_rate = float(financing.get("long_annual", 0.0)) or None
    short_rate = float(financing.get("short_annual", 0.0)) or None
    verified = bool(financing.get("financing_verified", False))
    leverage = float(margin.get("implied_leverage", 0.0)) or None

    built: dict[str, CapitalInstrumentRules] = {}
    for ticker, quote in (quotes or {}).items():
        name = str(ticker).upper()

        spread = None
        bid, offer = (quote or {}).get("bid"), (quote or {}).get("offer")
        try:
            bid, offer = float(bid), float(offer)
            if bid > 0 and offer > 0 and offer >= bid:
                spread = offer - bid
        except (TypeError, ValueError):
            spread = None
        if spread is None:
            # Everything absent, not just the spread. Leaving leverage and
            # financing populated would let the book strike the trade on
            # Capital's carry and a Trading 212 *estimated* spread -- one
            # row, two venues, which is the precise confusion this module
            # exists to remove. The caller also filters these out for want
            # of a mark; this is the second lock on the same door.
            logger.debug("%s: no usable Capital quote; every term left absent", name)
            built[name] = CapitalInstrumentRules(
                ticker=name, available=False, financing_verified=verified,
            )
            continue

        built[name] = CapitalInstrumentRules(
            ticker=name,
            leverage=leverage,
            financing_annual_long=long_rate,
            financing_annual_short=short_rate,
            financing_rate=long_rate,
            spread_typical=spread,
            currency=(quote or {}).get("currency") or None,
            min_quantity=_number(minimums.get(name)),
            # Capital converts at spot with no published conversion fee.
            # False is a measurement of the published terms, not an
            # assumption that conversion is free -- fx_fee_pct stays absent.
            fx_fee_applies=False,
            available=spread is not None,
            spread_source_label="measured" if spread is not None else "",
            financing_verified=verified,
        )

    return CapitalRuleTable(
        rules=built, financing_verified=verified,
        read_on=str((document.get("source", {}) or {}).get("read_on", "")),
    )


def _number(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
