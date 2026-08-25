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
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Mapping

logger = logging.getLogger(__name__)

__all__ = [
    "ASSET_CLASSES",
    "REGULATORY_RETAIL_CEILING",
    "asset_class",
    "RELATIONSHIPS",
    "SUGGESTED",
    "InstrumentMapping",
    "mapping_for",
    "describe_mapping",
    "from_rules",
    "verified_names",
    "display_name",
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
    # US indices.
    "^GSPC": ("US 500", "same", "The S&P 500 itself."),
    "^NDX": ("US Tech 100", "same", "The Nasdaq 100 itself."),
    "^DJI": ("US 30", "same", "The Dow Jones Industrial Average itself."),
    "^RUT": ("US 2000", "same", "The Russell 2000 itself."),
    # European indices.
    "^FTSE": ("UK 100", "same", "The FTSE 100 itself."),
    "^GDAXI": ("Germany 40", "same", "The DAX itself."),
    "^FCHI": ("France 40", "same", "The CAC 40 itself."),
    "^STOXX50E": ("Europe 50", "same", "The Euro Stoxx 50 itself."),
    # Asia-Pacific indices.
    "^N225": ("Japan 225", "same", "The Nikkei 225 itself."),
    "^AXJO": ("Australia 200", "same", "The S&P/ASX 200 itself."),
    "^HSI": ("Hong Kong 50", "same", "The Hang Seng itself."),
    # Metals. Front-month futures, which is what the CFD is priced from --
    # near enough to call the same, and they part company at a roll.
    "GC=F": ("Gold", "same", "Front-month COMEX gold."),
    "SI=F": ("Silver", "same", "Front-month COMEX silver."),
    "HG=F": ("Copper", "same", "Front-month COMEX copper."),
    # Energy.
    "CL=F": ("Oil - Crude (WTI)", "same", "Front-month NYMEX WTI."),
    "BZ=F": ("Oil - Brent", "same", "Front-month ICE Brent."),
    "NG=F": ("Natural Gas", "same", "Front-month NYMEX natural gas."),
    # Forex.
    "EURUSD=X": ("EURUSD", "same", ""),
    "GBPUSD=X": ("GBPUSD", "same", ""),
    "USDJPY=X": ("USDJPY", "same", ""),
    "AUDUSD=X": ("AUDUSD", "same", ""),
    "USDCAD=X": ("USDCAD", "same", ""),
    "USDCHF=X": ("USDCHF", "same", ""),
    "NZDUSD=X": ("NZDUSD", "same", ""),
    "EURGBP=X": ("EURGBP", "same", ""),
    "PL=F": ("Platinum", "same", "Front-month NYMEX platinum."),
    # Single stocks. Trading 212 lists these as stock CFDs under the company
    # name; the ticker is given here because that is what the lake stores,
    # and the search box takes either. Leverage on single stocks is lower
    # than on indices and is one of the numbers still unverified.
    "NVDA": ("NVIDIA", "same", ""),
    "AAPL": ("Apple", "same", ""),
    "MSFT": ("Microsoft", "same", ""),
    "AMZN": ("Amazon", "same", ""),
    "GOOGL": ("Alphabet (Class A)", "same", ""),
    "META": ("Meta Platforms", "same", ""),
    "TSLA": ("Tesla", "same", ""),
    "AMD": ("Advanced Micro Devices", "same", ""),
    "JPM": ("JPMorgan Chase", "same", ""),
    "XOM": ("Exxon Mobil", "same", ""),
    "JNJ": ("Johnson & Johnson", "same", ""),
    "BA": ("Boeing", "same", ""),
    "INTC": ("Intel", "same", ""),
    "PYPL": ("PayPal", "same", ""),
    "NKE": ("Nike", "same", ""),
    "LULU": ("Lululemon Athletica", "same", ""),
    "COST": ("Costco Wholesale", "same", ""),
    "AVGO": ("Broadcom", "same", ""),
    "NFLX": ("Netflix", "same", ""),
    "ADBE": ("Adobe", "same", ""),
    "ORCL": ("Oracle", "same", ""),
    "DIS": ("Walt Disney", "same", ""),
    # Not tradable, and in the universe on purpose. The VIX drives the
    # regime classification; it is an input, never a suggestion. The
    # tradability gate is what keeps it out of the recommendations.
    "^VIX": (
        "", "none",
        "An index level, not a tradable instrument. It feeds the regime "
        "classification and is never itself a suggested trade.",
    ),
    # Retired from the universe, kept so an older artefact still reads. None
    # of these has a Trading 212 CFD, which is why they were dropped.
    "SPY": ("US 500", "tracks", "Retired: an ETF following the S&P 500."),
    "QQQ": ("US Tech 100", "tracks", "Retired: an ETF following the Nasdaq 100."),
    "IWM": ("US 2000", "tracks", "Retired: an ETF following the Russell 2000."),
    "DIA": ("US 30", "tracks", "Retired: an ETF following the Dow."),
    "GLD": ("Gold", "tracks", "Retired: a gold ETF."),
    "SLV": ("Silver", "tracks", "Retired: a silver ETF."),
    "USO": ("Oil - Crude (WTI)", "proxy", "Retired: holds futures, carries roll cost."),
    "EWG": ("Germany 40", "proxy", "Retired: MSCI Germany, not the DAX."),
    "EWJ": ("Japan 225", "proxy", "Retired: MSCI Japan, not the Nikkei."),
    "XLK": ("", "none", "Retired: sector ETF, no corresponding CFD."),
    "XLF": ("", "none", "Retired: sector ETF, no corresponding CFD."),
    "XLE": ("", "none", "Retired: sector ETF, no corresponding CFD."),
    "XLV": ("", "none", "Retired: sector ETF, no corresponding CFD."),
    "EFA": ("", "none", "Retired: broad developed-markets ETF."),
    "EEM": ("", "none", "Retired: broad emerging-markets ETF."),
    "TLT": ("", "none", "Retired: Treasury ETF, no corresponding CFD."),
    "IEF": ("", "none", "Retired: Treasury ETF, no corresponding CFD."),
    "HYG": ("", "none", "Retired: credit ETF, no corresponding CFD."),
    "LQD": ("", "none", "Retired: credit ETF, no corresponding CFD."),
    "DBC": ("", "none", "Retired: commodity basket, no single CFD matches."),
}


#: Which asset class each ticker belongs to.
#:
#: A fact about the instrument, not a number from the broker -- an index is
#: an index whoever you trade it with. It earns its place because **leverage
#: is applied automatically by Trading 212 and differs by class**, so this is
#: what lets the rules template be grouped and the right regulatory ceiling
#: quoted beside each entry.
#:
#: It is used for grouping and for the template. It is never used *as* a
#: leverage: the figure still has to come from the app, per instrument.
ASSET_CLASSES: dict[str, str] = {
    "index": "Index",
    "commodity": "Commodity",
    "fx": "Forex",
    "share": "Share",
    "volatility": "Volatility index",
}

#: The **regulatory** retail ceiling per class, under FCA/ESMA rules, as a
#: leverage multiple.
#:
#: These are not Trading 212's numbers and must never be used as though they
#: were. They are the maximum a retail account may be given, so they are a
#: sanity check on what you read in the app: a figure above these means the
#: account is classified professional, and a figure below them is ordinary --
#: brokers routinely offer less. The actual value still comes from the ticket.
REGULATORY_RETAIL_CEILING: dict[str, float] = {
    "fx": 30.0,        # major pairs; non-majors are 20
    "index": 20.0,     # major indices; minor indices are 10
    "commodity": 20.0,  # gold is 20; other commodities are 10
    "share": 5.0,
    "volatility": 0.0,  # not tradable
}


def asset_class(ticker: str) -> str:
    """Which class a ticker belongs to. ``"unknown"`` when unclassified."""
    return _CLASS_BY_TICKER.get(str(ticker or "").strip(), "unknown")


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


_VERIFIED_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def verified_names(path=None) -> dict[str, Any]:
    """Names you have confirmed in the app, from the rules file.

    Cached against the file's modification time, so a table read once per
    row costs one stat rather than one parse. Never raises: an unreadable
    rules file means the shipped suggestions stand, which is the state
    everything ran in before you began filling it in.
    """
    from cfdinvest.livehelper.t212_rules import DEFAULT_RULES_PATH, load_rules

    target = Path(str(path or DEFAULT_RULES_PATH)).expanduser()
    try:
        stamp = target.stat().st_mtime if target.is_file() else 0.0
    except OSError:
        stamp = 0.0

    key = str(target)
    cached = _VERIFIED_CACHE.get(key)
    if cached is not None and cached[0] == stamp:
        return cached[1]

    try:
        overrides = from_rules(load_rules(target))
    except Exception as error:  # noqa: BLE001
        logger.warning("Could not read verified names from %s: %s", target, error)
        overrides = {}
    _VERIFIED_CACHE[key] = (stamp, overrides)
    return overrides


def from_rules(table) -> dict[str, Any]:
    """Verified names out of a rules table, in the override shape.

    What you read in the app beats what was written here by hand. Only
    entries that actually carry a name are returned -- a rules entry
    recording only a leverage says nothing about what the instrument is
    called, and treating it as confirmation would launder a guess.
    """
    overrides: dict[str, Any] = {}
    for ticker, rule in getattr(table, "rules", {}).items():
        name = getattr(rule, "t212_name", None)
        available = getattr(rule, "available", None)
        if not name and available is None:
            continue
        overrides[str(ticker)] = {
            "name": str(name or ""),
            "relationship": "none" if available is False else "same",
            "note": "" if available is not False else
                    "Recorded as not offered by Trading 212.",
        }
    return overrides


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


def display_name(ticker: str, overrides=None) -> str:
    """``UK 100 (^FTSE)`` -- what to search for, and what the lake calls it.

    The Trading 212 name leads because that is what you type into the app;
    the lake's symbol follows in brackets because it is what every other
    page, log line and artefact here calls the same instrument. Showing only
    one of them means translating in your head at the moment you are placing
    a trade, which is where mistakes get expensive.

    An instrument with no Trading 212 name is shown as its ticker alone
    rather than as an empty bracket -- there is nothing to translate to.
    """
    name = str(ticker or "").strip()
    if not name:
        return ""
    # What you confirmed in the app wins over what was written here by hand,
    # and it does so without every caller having to remember to ask.
    found = mapping_for(name, verified_names() if overrides is None else overrides)
    if not found.tradable or not found.name:
        return name
    return f"{found.name} ({name})"


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


#: Ticker -> asset class. Written out rather than inferred from the symbol:
#: "^" means an index on Yahoo and "=F" a future, but "GC=F" is a commodity
#: and "^VIX" is not tradable at all, so the pattern would have to carry
#: exceptions anyway.
_CLASS_BY_TICKER: dict[str, str] = {
    **{t: "index" for t in (
        "^GSPC", "^NDX", "^DJI", "^RUT", "^FTSE", "^GDAXI", "^FCHI",
        "^STOXX50E", "^N225", "^HSI",
    )},
    **{t: "commodity" for t in ("GC=F", "SI=F", "HG=F", "PL=F", "CL=F", "BZ=F", "NG=F")},
    **{t: "fx" for t in (
        "EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X",
        "USDCAD=X", "USDCHF=X", "NZDUSD=X", "EURGBP=X",
    )},
    **{t: "share" for t in (
        "NVDA", "AAPL", "MSFT", "AMZN", "GOOGL", "META", "TSLA", "AMD",
        "JPM", "XOM", "JNJ", "BA", "INTC", "PYPL", "NKE", "LULU", "DIS",
        "COST", "AVGO", "NFLX", "ADBE", "ORCL",
    )},
    "^VIX": "volatility",
}
