"""When each company reports, so a position need not be open when it does.

The reason this exists is a measurement rather than a theory. Across the
nineteen shares in the universe, twenty-four reporting dates each:

* an earnings session moves **3.8x** as far as an ordinary one
* those sessions are **0.5%** of all trading days
* they sum to **-282%** of log return across the universe, while every
  other day together sums to **+7,161%**

Thirteen of the nineteen have a negative total across their own reporting
days. Intel's twenty-four earnings sessions cost 114% between them while
its other 4,800 sessions made 302%. This is not a subtle effect and it is
not evenly spread: Johnson & Johnson's reporting days average a 0.64% move
and Meta's 10.03%.

It is also the one part of "news" that can be tested. Sentiment from a feed
is a rolling three-day window with no archive, so a rule built on it can
never be checked against history. Reporting *dates* go back six years, are
published in advance, and are a fact rather than an interpretation.

What this module does and does not do
-------------------------------------
It reads dates from yfinance and writes them to the lake. It does not
decide anything: the blackout rule lives in the execution config, and an
instrument with no recorded dates is simply never blacked out rather than
being guessed at.

Only equities have earnings. Indices, commodities and FX pairs return
nothing here, which is correct and not a gap -- though they have scheduled
events of their own (CPI, rate decisions, inventory reports) that this
module makes no attempt to cover.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Iterable, Mapping

logger = logging.getLogger(__name__)

__all__ = [
    "EARNINGS_DIR",
    "fetch_earnings_dates",
    "store_earnings_dates",
    "load_earnings_dates",
]

#: Where the dates live under the account root.
EARNINGS_DIR = "earnings"

SCHEMA_VERSION = 1


def fetch_earnings_dates(tickers: Iterable[str], *, limit: int = 40) -> dict[str, list[str]]:
    """``{ticker: [ISO date, ...]}`` from yfinance. Never raises.

    Both past and future dates are returned. The future ones are the point
    of the exercise -- a blackout has to know a print is coming, not learn
    it afterwards -- and the past ones are what makes the rule testable.

    A ticker that yfinance has nothing for is **omitted rather than
    recorded as empty**, so a lookup failure and a genuine absence of
    earnings stay distinguishable. An index has no earnings; a share whose
    fetch failed might.
    """
    try:
        import yfinance
    except ImportError:  # pragma: no cover - yfinance is an install extra
        logger.warning("yfinance is not installed; no earnings dates fetched")
        return {}

    found: dict[str, list[str]] = {}
    for ticker in tickers:
        symbol = str(ticker).strip()
        if not symbol:
            continue
        try:
            frame = yfinance.Ticker(symbol).earnings_dates
        except Exception as error:  # noqa: BLE001 - one bad symbol is not an outage
            logger.warning("No earnings dates for %s: %s", symbol, error)
            continue
        if frame is None or not len(frame):
            continue

        try:
            import pandas as pd

            index = pd.DatetimeIndex(frame.index)
            if index.tz is not None:
                index = index.tz_convert("UTC")
            else:
                index = index.tz_localize("UTC")
            dates = sorted({stamp.date().isoformat() for stamp in index})
        except Exception as error:  # noqa: BLE001
            logger.warning("Could not read earnings dates for %s: %s", symbol, error)
            continue

        if dates:
            found[symbol] = dates[-limit:]
    return found


def store_earnings_dates(
    dates: Mapping[str, Iterable[str]], config=None, account: str | None = None
) -> bool:
    """Rewrite the stored dates. Never raises.

    Rewritten rather than appended, unlike most artefacts here. A reporting
    date is a scheduled future event that moves -- companies reschedule --
    so the file says when they are expected *now*, which is the same reason
    the account registry is the other exception to append-only.
    """
    from cfdinvest.persist.storage import atomic_write_text

    path = _path(config, account)
    document = {
        "schema_version": SCHEMA_VERSION,
        "dates": {str(k): sorted(set(v)) for k, v in (dates or {}).items()},
    }
    try:
        atomic_write_text(json.dumps(document, indent=2, sort_keys=True), path)
        return True
    except Exception as error:  # noqa: BLE001
        logger.warning("Could not write earnings dates to %s: %s", path, error)
        return False


def load_earnings_dates(config=None, account: str | None = None) -> dict[str, list[str]]:
    """What was stored, or an empty mapping. Never raises.

    Empty means no instrument is blacked out, which is the behaviour before
    this existed. A blackout that silently failed open on a missing file is
    a worse outcome than one that never ran, but only marginally -- so the
    absence is logged.
    """
    path = _path(config, account)
    try:
        if not path.is_file():
            logger.debug("No earnings dates recorded at %s", path)
            return {}
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        logger.warning("Earnings dates unreadable at %s: %s", path, error)
        return {}

    dates = document.get("dates") if isinstance(document, Mapping) else None
    if not isinstance(dates, Mapping):
        return {}
    return {str(k): [str(d) for d in (v or [])] for k, v in dates.items()}


def _path(config=None, account: str | None = None) -> Path:
    from cfdinvest.persist.storage import account_root

    return account_root(config, account) / EARNINGS_DIR / "dates.json"
