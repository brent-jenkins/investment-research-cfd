"""Which Trading 212 instrument corresponds to a ticker in the lake.

This exists so that a recommendation reading ``QQQ`` tells you what to search
for in the Trading 212 app. It is a **navigational aid and nothing else**. No
value here reaches sizing, the safety engine, or any check: the mapping says
where to look, never what the instrument costs, how it is margined, or what
it may be traded in.

Names are not numbers
---------------------
The project's rule is that broker *numbers* are never invented -- leverage,
precision, financing and spread stay unverified until you fill in
``t212_rules.toml`` from the ticket. A name is a different kind of claim: a
wrong one wastes ten seconds in a search box, where a wrong leverage sizes a
real position. So suggestions ship here, and every one of them is marked
:data:`SUGGESTED` until you confirm it.

The relationship matters more than the name
-------------------------------------------
Almost none of these are the *same* instrument:

``same``
    The same thing under a different name. The four FX pairs.
``tracks``
    A CFD on the index or metal the lake's ETF is designed to follow. SPY and
    Trading 212's US 500 both follow the S&P 500, but you are modelling an
    ETF and trading an index CFD -- the price, the tick and the financing all
    differ, and the signal is the only thing that carries across.
``proxy``
    Related, and not the same market. EWG holds MSCI Germany; Germany 40 is
    the DAX. Directionally similar, and they can disagree for weeks.
``none``
    No Trading 212 CFD corresponds to this at all. Said out loud rather than
    approximated, because a sector or bond ETF nudged onto the nearest index
    is how you end up trading something you did not analyse.

An unmapped instrument is reported unmapped. That is the same rule the rest
of this project follows: absence is named, never inferred.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping

logger = logging.getLogger(__name__)

__all__ = [
    "RELATIONSHIPS",
    "SUGGESTED",
    "InstrumentMapping",
    "mapping_for",
    "describe_mapping",
    "unmapped",
]

#: How closely the Trading 212 instrument corresponds to the lake's ticker.
RELATIONSHIPS: tuple[str, ...] = ("same", "tracks", "proxy", "none")

#: Ticker -> (Trading 212 name, relationship, note).
#:
#: **Every entry is a suggestion to check, not a fact.** Trading 212 publishes
#: no machine-readable instrument list and this project makes no broker calls,
#: so these were written by hand and none has been verified against your
#: account. Confirm each one in the app before you rely on it; the names and
#: the availability both vary by account and jurisdiction.
#:
#: Entries mapped to ``none`` are deliberate. Leaving them out would look like
#: an oversight, and filling them in with the nearest index would be the
#: mistake this table exists to prevent.
SUGGESTED: dict[str, tuple[str, str, str]] = {
    # Index CFDs. The ETF and the index CFD follow the same market; they are
    # not the same instrument and will not fill at the same price.
    "SPY": ("US 500", "tracks", "Both follow the S&P 500."),
    "QQQ": ("US Tech 100", "tracks", "Both follow the Nasdaq 100."),
    "IWM": ("US 2000", "tracks", "Both follow the Russell 2000."),
    "DIA": ("US 30", "tracks", "Both follow the Dow Jones Industrial Average."),
    # Regional. Related indices, genuinely different baskets.
    "EWG": (
        "Germany 40", "proxy",
        "EWG holds MSCI Germany; Germany 40 is the DAX. They diverge.",
    ),
    "EWJ": (
        "Japan 225", "proxy",
        "EWJ holds MSCI Japan; Japan 225 is the Nikkei. They diverge.",
    ),
    # Commodities.
    "GLD": ("Gold", "tracks", "Both follow the spot gold price."),
    "SLV": ("Silver", "tracks", "Both follow the spot silver price."),
    "USO": (
        "Oil - Crude (WTI)", "proxy",
        "USO holds futures and carries roll cost the spot CFD does not.",
    ),
    # Forex. The only genuinely like-for-like entries here.
    "EURUSD=X": ("EURUSD", "same", ""),
    "GBPUSD=X": ("GBPUSD", "same", ""),
    "USDJPY=X": ("USDJPY", "same", ""),
    "AUDUSD=X": ("AUDUSD", "same", ""),
    # No corresponding CFD. Named rather than approximated.
    "XLK": ("", "none", "Sector ETF; no Trading 212 CFD corresponds to it."),
    "XLF": ("", "none", "Sector ETF; no Trading 212 CFD corresponds to it."),
    "XLE": ("", "none", "Sector ETF; no Trading 212 CFD corresponds to it."),
    "XLV": ("", "none", "Sector ETF; no Trading 212 CFD corresponds to it."),
    "EFA": ("", "none", "Broad developed-markets ETF; no single CFD matches."),
    "EEM": ("", "none", "Broad emerging-markets ETF; no single CFD matches."),
    "TLT": ("", "none", "Treasury ETF; no corresponding CFD."),
    "IEF": ("", "none", "Treasury ETF; no corresponding CFD."),
    "HYG": ("", "none", "Credit ETF; no corresponding CFD."),
    "LQD": ("", "none", "Credit ETF; no corresponding CFD."),
    "DBC": ("", "none", "Commodity basket; no single CFD matches."),
    "^VIX": (
        "", "none",
        "An index level, not a tradable instrument. It drives the regime "
        "classification and is never itself a suggested trade.",
    ),
}


@dataclass(frozen=True, slots=True)
class InstrumentMapping:
    """What to search for in Trading 212, and how far to trust the match."""

    ticker: str
    name: str = ""
    relationship: str = "unmapped"
    note: str = ""
    confirmed: bool = False

    @property
    def tradable(self) -> bool:
        """Whether this table names something you could open a position in.

        ``False`` for both "no corresponding CFD" and "not in the table" --
        different reasons, and the same consequence for a reader deciding
        what to do next. :attr:`relationship` distinguishes them.
        """
        return bool(self.name) and self.relationship in ("same", "tracks", "proxy")

    @property
    def exact(self) -> bool:
        return self.relationship == "same"


def mapping_for(
    ticker: str, overrides: Mapping[str, Any] | None = None
) -> InstrumentMapping:
    """The Trading 212 instrument for one ticker. Never raises.

    ``overrides`` is your own table, read from the rules file. Anything you
    supply wins over the suggestion and is marked confirmed, because you
    looked it up in the app and this module did not.
    """
    name = str(ticker or "").strip()
    if not name:
        return InstrumentMapping(ticker=str(ticker))

    entry = (overrides or {}).get(name)
    if entry is not None:
        if isinstance(entry, str):
            return InstrumentMapping(name, entry, "same", "", confirmed=True)
        if isinstance(entry, Mapping):
            relationship = str(entry.get("relationship", "same"))
            return InstrumentMapping(
                ticker=name,
                name=str(entry.get("name", "")),
                relationship=(
                    relationship if relationship in RELATIONSHIPS else "same"
                ),
                note=str(entry.get("note", "")),
                confirmed=True,
            )

    suggestion = SUGGESTED.get(name)
    if suggestion is None:
        return InstrumentMapping(ticker=name)
    t212_name, relationship, note = suggestion
    return InstrumentMapping(name, t212_name, relationship, note, confirmed=False)


def describe_mapping(mapping: InstrumentMapping) -> str:
    """One line fit for the helper's summary or a dashboard cell."""
    if mapping.relationship == "unmapped":
        return (
            f"{mapping.ticker}: no Trading 212 instrument recorded. "
            "Add one to your rules file."
        )
    if mapping.relationship == "none":
        return f"{mapping.ticker}: not tradable on Trading 212. {mapping.note}".strip()

    head = f"Trading 212: {mapping.name}"
    if mapping.relationship == "tracks":
        head += " (tracks the same market, not the same instrument)"
    elif mapping.relationship == "proxy":
        head += " (a proxy, not the same market)"
    if not mapping.confirmed:
        # Never let a hand-written suggestion read as a checked fact.
        head += " — unconfirmed, check it in the app"
    return f"{head}. {mapping.note}".strip()


def unmapped(tickers) -> list[str]:
    """Those with no entry at all, so a page can say what is missing."""
    return sorted(
        str(t) for t in (tickers or ())
        if mapping_for(str(t)).relationship == "unmapped"
    )
