"""Historical bars from Capital.com, paginated, with the spread attached.

Read-only, like everything beside it except ``dealing``.

Why this exists when there is already a lake
--------------------------------------------
The lake holds yfinance bars: one consolidated print per bar, the regular
session only, about 730 days at hourly. Capital's history is different in
three ways that matter, and each of them is something the cost model
currently guesses at.

**Every bar carries a bid and an ask.** Spread is 46.6% of modelled cost and
is presently one constant of 10bp for every instrument in every condition,
against a measured median of 5.3bp and a range of ten to one. Here it is a
series: what was actually quoted, per instrument, per bar.

**The bars cover 08:00 to midnight UTC**, not 13:30 to 20:00. Fifty-five per
cent of them fall outside the US session. That is the data the overnight
question could not be answered with — whether a position should be closed in
extended hours was unanswerable because the lake had no extended-hours bars
to answer it from.

**They are the prices this account would deal at.** An exchange print is
what the market did; a broker's quote is what you would have paid. The IG
episode is the argument for preferring the second.

What it deliberately does not do
--------------------------------
It does not replace the lake. The universe screen ranks on dollar turnover
and Capital reports *its own* volume — a few hundred contracts on an AAPL
hourly bar, which is its clients rather than the market. Ranking instruments
by how much one broker's customers trade them would be a different screen
wearing the same name.

OHLC is stored as the **mid** so indicators see a clean price, with bid, ask
and the spread kept alongside. Folding the spread into the price would make
every indicator read a cost as a move.
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.parse
from typing import Any, Mapping

logger = logging.getLogger(__name__)

__all__ = ["fetch_prices", "RESOLUTIONS", "MAX_BARS_PER_CALL"]

#: What the API accepts, mapped from the labels this project uses.
RESOLUTIONS: dict[str, str] = {
    "1h": "HOUR",
    "1d": "DAY",
    "15m": "MINUTE_15",
    "5m": "MINUTE_5",
    "1m": "MINUTE",
}

#: The server's cap. Asking for more returns this many; asking for a range
#: wider than this many bars is refused outright with
#: ``error.invalid.max.daterange`` rather than truncated, which is why this
#: walks forward from a start rather than requesting a span.
MAX_BARS_PER_CALL = 1000

#: How long a REST session lasts before the server stops honouring it.
#: Documented as ten minutes. A full backfill runs for an hour or more, so
#: this is not a corner case: it is certain to be hit, and a walk that did
#: not renew would return short histories for every instrument after the
#: first ten minutes -- short, not empty, which is the dangerous kind.
SESSION_MINUTES = 10


def _mid(price: Mapping[str, Any] | None) -> float | None:
    """The mid of a bid/ask pair, or None when either side is missing."""
    if not isinstance(price, Mapping):
        return None
    bid, ask = price.get("bid"), price.get("ask")
    try:
        bid, ask = float(bid), float(ask)
    except (TypeError, ValueError):
        return None
    if not (bid > 0 and ask > 0):
        return None
    return (bid + ask) / 2.0


def fetch_prices(
    epic: str,
    session,
    config=None,
    *,
    resolution: str = "1h",
    start: str,
    end: str | None = None,
    opener=None,
    sleep=None,
    renew=None,
    max_calls: int = 40,
) -> list[dict[str, Any]]:
    """Bars from ``start`` forward, following the server's pagination.

    Walks forward rather than requesting a span, because a range wider than
    a thousand bars is refused rather than truncated. Each call starts from
    the last bar the previous one returned, so a window that returns nothing
    new ends the walk — a server that repeated itself would otherwise loop
    until ``max_calls``.

    ``renew`` is called to obtain a fresh session when the server stops
    honouring the current one. A ten-minute session against an hour-long
    backfill means expiry is the expected case rather than a fault, and a
    walk without this would quietly stop at whatever bar the clock reached.
    Without a ``renew`` an expired session ends the walk, which is honest but
    short.

    Returns a list of rows, oldest first, deduplicated by timestamp. Never
    raises: a failed page ends the walk and keeps what was already read,
    because half a history is worth having and an exception here would take
    down a backfill of twenty instruments over one bad response.
    """
    import time

    import pandas as pd

    from cfdinvest.broker.capital.client import (
        REQUEST_INTERVAL_SECONDS,
        CapitalConfig,
        _read,
    )

    settings = config or CapitalConfig.from_env()
    if not session.ok:
        logger.warning("No session; cannot read prices for %s", epic)
        return []

    wanted = RESOLUTIONS.get(str(resolution), str(resolution)).upper()
    read = opener or _read
    pause = sleep if sleep is not None else time.sleep
    recoveries = 0

    stop_at = pd.Timestamp(end, tz="UTC") if end else None
    cursor = pd.Timestamp(start)
    if cursor.tz is None:
        cursor = cursor.tz_localize("UTC")

    seen: dict[str, dict[str, Any]] = {}
    pages = 0
    while pages < max_calls:
        query = urllib.parse.urlencode({
            "resolution": wanted,
            "from": cursor.strftime("%Y-%m-%dT%H:%M:%S"),
            "max": MAX_BARS_PER_CALL,
        })
        url = f"{session.host}/prices/{urllib.parse.quote(str(epic), safe='')}?{query}"
        try:
            document, _ = read(url, headers=session.headers(settings.api_key))
        except urllib.error.HTTPError as error:
            if error.code in (401, 403) and renew is not None and recoveries < 20:
                # The expected end of a ten-minute session, not a fault.
                recoveries += 1
                fresh = renew()
                if getattr(fresh, "ok", False):
                    logger.debug("%s: session renewed mid-walk", epic)
                    session = fresh
                    pause(REQUEST_INTERVAL_SECONDS)
                    continue
                logger.warning("%s: could not renew the session; stopping", epic)
                break
            if error.code == 429:
                # Being refused means the pacing was already too fast, so
                # back off rather than retry at the same rate.
                recoveries += 1
                if recoveries >= 20:
                    logger.warning("%s: still rate limited; stopping", epic)
                    break
                logger.warning("%s: rate limited, backing off", epic)
                pause(REQUEST_INTERVAL_SECONDS * 30)
                continue
            logger.warning("%s: HTTP %s reading prices from %s", epic, error.code, cursor)
            break
        except Exception as error:  # noqa: BLE001
            logger.warning("%s: %s reading prices from %s", epic, error, cursor)
            break

        pages += 1
        page = (document or {}).get("prices") or []
        if not page:
            break

        newest = cursor
        added = 0
        for bar in page:
            stamp = bar.get("snapshotTimeUTC") or bar.get("snapshotTime")
            if not stamp:
                continue
            when = pd.Timestamp(stamp)
            if when.tz is None:
                when = when.tz_localize("UTC")
            if stop_at is not None and when > stop_at:
                newest = when
                continue
            key = when.isoformat()
            if key in seen:
                continue

            close = bar.get("closePrice") or {}
            mid = _mid(close)
            if mid is None:
                continue
            bid, ask = float(close.get("bid")), float(close.get("ask"))
            seen[key] = {
                "timestamp": when,
                "open": _mid(bar.get("openPrice")),
                "high": _mid(bar.get("highPrice")),
                "low": _mid(bar.get("lowPrice")),
                "close": mid,
                "bid": bid,
                "ask": ask,
                "spread_bp": (ask - bid) / mid * 10_000.0 if mid > 0 else None,
                "volume": bar.get("lastTradedVolume"),
            }
            added += 1
            newest = max(newest, when)

        if stop_at is not None and newest > stop_at:
            break
        if added == 0 or newest <= cursor:
            # No progress. A server repeating its last page would otherwise
            # spin here until max_calls, quietly costing a rate limit.
            break
        # One second PAST the newest bar, not on it. At minute resolution the
        # server ends a page at a session's close -- 60 bars from 20:00 stop
        # at 20:59 -- and asked again FROM 20:59 it returns that one bar and
        # nothing else. The walk read that as the end of history. Asked from
        # 20:59:01 it answers with the next session, because a start inside a
        # gap is served from the next bar there is. Measured on 2026-09-27.
        #
        # So every nightly minute ingest resumed from its tail, hit the first
        # overnight gap, added nothing and reported success: most of the 66
        # capital_1m series stopped on 2026-04-02 and MU on 2026-08-07, while
        # the job ran and its files were rewritten every night. This project
        # rules that a series once collected is never stopped; this one was
        # stopped for six months by one line, with nothing saying so.
        cursor = newest + pd.Timedelta(seconds=1)
        pause(REQUEST_INTERVAL_SECONDS)

    rows = [seen[key] for key in sorted(seen)]
    logger.debug("%s: %d bars from %s", epic, len(rows), start)
    return rows
