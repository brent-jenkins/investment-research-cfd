"""The latest quote per instrument, on disk. No network, ever.

This module is deliberately importable by the live helper, the risk layer and
monitoring: it opens files and nothing else. :mod:`cfdinvest.livequotes.sources`
is the only module here that can reach the internet, and none of those
packages import it.

Latest-only, and rewritten
--------------------------
This is the one store in the project that is **not** append-only, and the
exception is the same one the account registry gets: it says what is true
*now*. A price five minutes old is not an observation worth keeping -- the
lake holds the daily bar, which is the record. Keeping every poll would build
an intraday history beside the lake, which is exactly what this feature is
not for.

Writes are atomic, through the same helper the rest of the project uses, so a
reader never sees half a file. Reads never raise: a missing or corrupt quote
file means no live price, and no live price means the daily bar, which is
always the safe direction to fall.
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Any, Iterable, Mapping

import pandas as pd

from cfdinvest.persist.storage import account_root, atomic_write_text

logger = logging.getLogger(__name__)

__all__ = [
    "LIVEQUOTES_DIR",
    "LATEST_FILENAME",
    "SCHEMA_VERSION",
    "DEFAULT_MAX_AGE_MINUTES",
    "livequotes_root",
    "latest_path",
    "write_quotes",
    "load_quotes",
    "quote_for",
    "age_minutes",
]

LIVEQUOTES_DIR = "livequotes"
LATEST_FILENAME = "latest.json"
SCHEMA_VERSION = 1

#: How old a quote may be and still be used in place of the daily bar.
#:
#: One owner for this number. The live helper reads it from here and so does
#: monitoring, so the banner and the helper cannot come to different views of
#: whether the same file is stale. Fifteen minutes is three missed five-minute
#: polls: one miss is a hiccup, three is the poller not running.
DEFAULT_MAX_AGE_MINUTES = 15.0


def livequotes_root(config=None, account: str | None = None) -> Path:
    """Where the quote file lives."""
    return account_root(config, account) / LIVEQUOTES_DIR


def latest_path(config=None, account: str | None = None) -> Path:
    return livequotes_root(config, account) / LATEST_FILENAME


def _usable(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def write_quotes(
    quotes: Mapping[str, Any],
    config=None,
    *,
    account: str | None = None,
    now=None,
) -> str | None:
    """Replace the quote file. Returns its path, or ``None`` if it could not.

    Instruments whose price could not be read are **left out** rather than
    written as null. A reader asking "is there a live price for SPX500" wants
    one answer, not two shapes of no; and a null in this file would sit there
    looking like a measurement.
    """
    moment = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="UTC")
    if moment.tz is None:
        moment = moment.tz_localize("UTC")

    priced: dict[str, float] = {}
    skipped: list[str] = []
    for symbol, value in dict(quotes or {}).items():
        price = _usable(getattr(value, "price", value))
        if price is None:
            skipped.append(str(symbol))
        else:
            priced[str(symbol)] = price

    if skipped:
        logger.info(
            "No live price for %s; the daily bar stands for %s",
            ", ".join(sorted(skipped)),
            "them" if len(skipped) > 1 else "it",
        )

    document = {
        "schema_version": SCHEMA_VERSION,
        "timestamp": moment.isoformat(),
        "quotes": priced,
        "unavailable": sorted(skipped),
    }

    path = latest_path(config, account)
    try:
        atomic_write_text(json.dumps(document, indent=2), path)
        return str(path)
    except Exception as error:  # noqa: BLE001 - a failed write is not an outage
        logger.warning("Could not write live quotes to %s: %s", path, error)
        return None


def load_quotes(config=None, account: str | None = None) -> dict[str, Any]:
    """The quote file, or an empty document when there is not one.

    Never raises. Absence of a file, a corrupt file and a file holding no
    quotes all mean the same thing to every caller: no live price.
    """
    empty: dict[str, Any] = {"timestamp": None, "quotes": {}, "unavailable": []}
    path = latest_path(config, account)
    try:
        if not path.is_file():
            return empty
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        logger.warning("Live quotes unreadable at %s: %s", path, error)
        return empty

    if not isinstance(document, dict):
        return empty
    quotes = document.get("quotes")
    document["quotes"] = quotes if isinstance(quotes, dict) else {}
    return document


def age_minutes(document: Mapping[str, Any] | None, now=None) -> float | None:
    """How old the file is, in minutes, or ``None`` if it does not say.

    ``None`` is not "fresh". Every caller treats an unknown age as unusable,
    because a quote that cannot say when it was taken cannot be shown to be
    current -- and this project does not read silence as reassurance.
    """
    stamp = (document or {}).get("timestamp")
    if stamp is None:
        return None
    try:
        moment = pd.Timestamp(stamp)
    except (TypeError, ValueError):
        return None
    if moment.tz is None:
        moment = moment.tz_localize("UTC")
    current = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="UTC")
    if current.tz is None:
        current = current.tz_localize("UTC")
    return float((current - moment).total_seconds() / 60.0)


def quote_for(
    symbol: str,
    config=None,
    *,
    account: str | None = None,
    document: Mapping[str, Any] | None = None,
    max_age_minutes: float | None = None,
    now=None,
) -> tuple[float | None, str]:
    """The live price for one instrument, and why it is or is not usable.

    Returns ``(price, reason)``. ``price`` is ``None`` whenever the daily bar
    should be used instead, and ``reason`` is a short phrase fit to print in
    the helper's summary. A stale file is refused rather than used with a
    caveat: a price that is wrong in a fast market is worse than a price that
    is a day old and known to be.
    """
    limit = (
        DEFAULT_MAX_AGE_MINUTES if max_age_minutes is None else float(max_age_minutes)
    )
    document = load_quotes(config, account) if document is None else document

    quotes = (document or {}).get("quotes") or {}
    if not quotes:
        return None, "no live quotes have been polled"

    price = _usable(quotes.get(str(symbol)))
    if price is None:
        return None, f"no live quote for {symbol}"

    age = age_minutes(document, now)
    if age is None:
        return None, "the live quote file does not say when it was taken"
    if age > limit:
        return None, f"the live quote is {age:.0f} minutes old (limit {limit:.0f})"
    return price, f"live quote, {age:.0f} minutes old"


def stale_symbols(
    document: Mapping[str, Any] | None,
    wanted: Iterable[str] | None = None,
) -> list[str]:
    """Instruments that were asked for and have no usable price."""
    quotes = (document or {}).get("quotes") or {}
    return sorted(
        str(name) for name in (wanted or ())
        if _usable(quotes.get(str(name))) is None
    )
