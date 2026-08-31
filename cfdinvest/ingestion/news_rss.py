"""Headlines from a news RSS feed, with history.

The reason this exists
----------------------
``advisor.sentiment.fetch_headlines`` reads yfinance, which returns ten
items covering about two days per instrument and silently ignores any count
asked of it -- measured on 2026-08-31: ``get_news(count=200)`` returns ten.
There is no historical endpoint, so an archive built on it can only
accumulate forward, and sentiment could not be tested against a single past
trade.

Google News RSS honours ``after:`` and ``before:`` in the query and returns
up to a hundred items per window. Six months therefore backfills as six
monthly queries per instrument, which is the difference between testing
sentiment in four weeks and testing it today.

What this deliberately does not do
----------------------------------
It does not score anything. The title is the observation; scoring it is an
interpretation that should be recomputed from the record rather than frozen
into it -- the same rule ``persist.headlines`` already follows.

Nor does it fetch article bodies. A headline is what the feed publishes and
what a reader sees; following links would be a different activity with
different manners.

Honesty about the query
-----------------------
A ticker-based search can match the wrong thing. "CAT stock" is Caterpillar
and almost certainly not a cat, but the risk is real for short or ambiguous
symbols, so **the query is stored with every story**. A later reader can
see what was asked rather than inferring it, and a bad query becomes
visible as a bad query rather than as mysterious noise in a model.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)

__all__ = ["RETENTION_DAYS", "fetch_window", "monthly_windows", "headline_query"]

#: How much history is kept per instrument. Six months is the owner's
#: figure and matches what the feed will serve: long enough that a model has
#: a usable sample, short enough that the store stays small and a rolling
#: window can be pruned rather than grown for ever.
RETENTION_DAYS = 183

#: Politeness. A public feed asked for six hundred windows in a burst is a
#: public feed being abused; this is a one-off backfill, not a hot path.
_PAUSE_SECONDS = 1.0

_ENDPOINT = "https://news.google.com/rss/search"
_AGENT = "Mozilla/5.0 (compatible; cfdinvest/1.0; research)"


def headline_query(ticker: str, since: dt.date | None = None,
                   until: dt.date | None = None) -> str:
    """The search a window is fetched with. Stored with every story.

    ``stock`` is appended to bias the match towards the listed company
    rather than the word. It is not a guarantee -- see the module note --
    which is precisely why the query is recorded rather than assumed.
    """
    parts = [f"{ticker} stock"]
    if since is not None:
        parts.append(f"after:{since.isoformat()}")
    if until is not None:
        parts.append(f"before:{until.isoformat()}")
    return " ".join(parts)


def monthly_windows(days: int = RETENTION_DAYS, *, today: dt.date | None = None
                    ) -> list[tuple[dt.date, dt.date]]:
    """Consecutive month-ish windows covering the retention period.

    Windows rather than one large request because the feed caps at about a
    hundred items however much is asked for: a single six-month query
    returns the most recent hundred and silently drops the rest, which
    would look like a successful backfill of a sparse period.
    """
    end = today or dt.date.today()
    start = end - dt.timedelta(days=days)
    windows: list[tuple[dt.date, dt.date]] = []
    cursor = start
    step = dt.timedelta(days=30)
    while cursor < end:
        stop = min(cursor + step, end)
        windows.append((cursor, stop))
        cursor = stop
    return windows


def _published(item) -> str | None:
    raw = item.findtext("pubDate")
    if not raw:
        return None
    for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z"):
        try:
            stamp = dt.datetime.strptime(raw.strip(), fmt)
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=dt.timezone.utc)
            return stamp.astimezone(dt.timezone.utc).isoformat()
        except ValueError:
            continue
    logger.debug("Unparsed pubDate %r", raw)
    return None


def fetch_window(ticker: str, since: dt.date | None = None,
                 until: dt.date | None = None, *, timeout: float = 25.0,
                 opener=None) -> list[dict]:
    """Stories for one instrument over one window. Never raises.

    Returns rows in ``persist.headlines`` shape, plus the ``query`` that
    produced them and the ``source`` feed. A failed window is an empty list
    and a warning: one bad window must not lose the other five.
    """
    query = headline_query(ticker, since, until)
    url = f"{_ENDPOINT}?{urllib.parse.urlencode({'q': query, 'hl': 'en-US', 'gl': 'US', 'ceid': 'US:en'})}"
    try:
        request = urllib.request.Request(url, headers={"User-Agent": _AGENT})
        fetch = opener or urllib.request.urlopen
        with fetch(request, timeout=timeout) as response:
            payload = response.read()
    except Exception as error:  # noqa: BLE001 - one window is not an outage
        logger.warning("No RSS for %s (%s): %s", ticker, query, error)
        return []

    try:
        root = ET.fromstring(payload)
    except ET.ParseError as error:
        logger.warning("Unparsable RSS for %s: %s", ticker, error)
        return []

    stories = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        published = _published(item)
        if published is None:
            # Undated is unusable: a headline whose moment is unknown cannot
            # be aligned to a bar, and guessing "now" would date six-month-old
            # news to today.
            continue
        publisher = ""
        source = item.find("source")
        if source is not None and (source.text or "").strip():
            publisher = source.text.strip()
        stories.append({
            "ticker": str(ticker),
            "title": title,
            "published": published,
            "publisher": publisher,
            "query": query,
            "source": "google-news-rss",
            # Flagged, not dropped -- see ``classify``. A reader filters on
            # this; the record keeps everything the feed said.
            "noise": list(classify(title, ticker, names=company_names(ticker))),
        })
    return stories


def fetch_history(ticker: str, *, days: int = RETENTION_DAYS,
                  today: dt.date | None = None, pause: float = _PAUSE_SECONDS,
                  opener=None) -> list[dict]:
    """Every window across the retention period, for one instrument."""
    rows: list[dict] = []
    windows = monthly_windows(days, today=today)
    for position, (since, until) in enumerate(windows):
        rows.extend(fetch_window(ticker, since, until, opener=opener))
        if pause and position + 1 < len(windows):
            time.sleep(pause)
    return rows


#: Titles that carry no news, matched case-insensitively as substrings.
#:
#: Calibrated on 235 real stories across three instruments rather than
#: imagined. The dominant noise is not clickbait but auto-generated
#: boilerplate: quote pages, forum indexes and "stock price, news & history"
#: stubs, three aggregators producing over half the volume between them.
BOILERPLATE = (
    "stock price, quote",
    "stock price, news",
    "quote, news",
    "forum and discussion",
    "stock forum",
    "message board",
    "stock price history",
    "share price, quote",
    "news & history",
)

#: Round-ups, where the instrument is one of a list rather than the subject.
LISTICLE = (
    "top 10", "top 5", "top 3", "best stocks", "stocks to buy",
    "stocks to watch", "worst stocks", "biggest movers",
)


def classify(title: str, ticker: str, *, names=None) -> tuple[str, ...]:
    """Why a headline might be noise. Empty means nothing was found.

    **Flagged, never dropped.** The title is the observation; whether it is
    noise is an interpretation, and this project's standing rule is that
    interpretations are recomputed from the record rather than frozen into
    it. These rules are a first attempt calibrated on one sample and will be
    wrong in ways nobody has thought of yet -- and a headline deleted at
    collection cannot be recovered once it leaves the feed's six-month
    window. Flagging costs a field; deleting costs the data.

    ``incidental`` needs the company name to be safe: "Advanced Micro
    Devices Stock Ran The Roadmap" is real news that a ticker-only rule
    would bin, and 22% of a real sample names the company rather than the
    symbol.
    """
    reasons: list[str] = []
    lowered = (title or "").lower()
    if not lowered:
        return ("empty",)

    if any(phrase in lowered for phrase in BOILERPLATE):
        reasons.append("boilerplate")
    if any(phrase in lowered for phrase in LISTICLE):
        reasons.append("listicle")

    symbol = str(ticker or "").lower()
    aliases = [symbol] + [str(n).lower() for n in (names or ()) if n]
    if symbol and not any(alias and alias in lowered for alias in aliases):
        reasons.append("incidental")
    return tuple(reasons)


def company_names(ticker: str) -> tuple[str, ...]:
    """Known aliases for an instrument, for the incidental check.

    Read from the broker instrument table, which already maps symbol to
    company name. Absent is fine: the incidental rule then falls back to the
    symbol alone, which over-flags rather than under-flags -- the safe
    direction, since a flag is reversible and a deletion is not.
    """
    try:
        from cfdinvest.livehelper import t212_instruments

        # SUGGESTED, not INSTRUMENTS. Guessing the attribute name returned
        # an empty map silently, and the incidental rule then flagged
        # "Advanced Micro Devices Stock Ran The Roadmap" as an incidental
        # mention of AMD -- exactly the false positive the name lookup
        # exists to prevent, produced by the lookup failing quietly.
        table = getattr(t212_instruments, "SUGGESTED", None) or {}
        entry = table.get(str(ticker))
        if isinstance(entry, (tuple, list)) and entry:
            return (str(entry[0]),)
    except Exception as error:  # noqa: BLE001 - a missing table is not an outage
        logger.debug("No instrument table for %s: %s", ticker, error)
    return ()
