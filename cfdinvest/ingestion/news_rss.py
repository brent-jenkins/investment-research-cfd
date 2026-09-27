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
PAUSE_SECONDS = 1.0
_PAUSE_SECONDS = PAUSE_SECONDS

_ENDPOINT = "https://news.google.com/rss/search"
_AGENT = "Mozilla/5.0 (compatible; cfdinvest/1.0; research)"


def headline_query(ticker: str, since: dt.date | None = None,
                   until: dt.date | None = None, *, name: str | None = None) -> str:
    """The search a window is fetched with. Stored with every story.

    **The company name, quoted, wherever one is known.** Searching the
    symbol looked reasonable and was not: measured across a six-month
    backfill, "GILD stock" returned gold-mining stories -- New Gold, Coeur
    Mining, "Why Is Gold Getting Cheaper" -- because the feed fuzzy-matches
    GILD to gold. "SHOP stock" returned CrowdStrike, Marvell and municipal
    bin collections. Eighty-seven per cent of Gilead's fetched headlines
    were about a different industry.

    That was found because the query is stored with every story, which is
    the thing this docstring claimed it was for: "a bad search becomes
    visible as a bad search rather than as mysterious noise in a model".

    The quoted name loses nothing. ``"Gilead Sciences" stock`` still
    returns headlines that name only the symbol, because the feed matches
    the article rather than the literal title.

    ``stock`` remains appended to bias towards the listed company over the
    word -- "Target" and "Prologis" are both ordinary English otherwise.
    """
    label = f'"{name}"' if name else str(ticker)
    parts = [f"{label} stock"]
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
                 opener=None, config=None) -> list[dict]:
    """Stories for one instrument over one window. Never raises.

    Returns rows in ``persist.headlines`` shape, plus the ``query`` that
    produced them and the ``source`` feed. A failed window is an empty list
    and a warning: one bad window must not lose the other five.
    """
    names = company_names(ticker, config=config)
    query = headline_query(ticker, since, until,
                           name=names[0] if names else None)
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
            "noise": list(classify(title, ticker, names=names)),
        })
    return stories


def fetch_history(ticker: str, *, days: int = RETENTION_DAYS,
                  today: dt.date | None = None, pause: float = _PAUSE_SECONDS,
                  opener=None, config=None) -> list[dict]:
    """Every window across the retention period, for one instrument."""
    # Warm the name once: the query depends on it, and resolving per window
    # would ask the vendor seven times for one answer.
    company_names(ticker, config=config, resolve=True)
    rows: list[dict] = []
    windows = monthly_windows(days, today=today)
    for position, (since, until) in enumerate(windows):
        rows.extend(fetch_window(ticker, since, until, opener=opener, config=config))
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


#: Where resolved company names are cached, under the lake's metadata dir.
NAME_CACHE = "instrument_names.json"


def company_names(ticker: str, *, config=None, resolve: bool = False
                  ) -> tuple[str, ...]:
    """Known aliases for an instrument, for the incidental check.

    Three sources in order: the broker instrument table, a cached lookup,
    and -- only when ``resolve`` is set -- the market-data vendor.

    **The table alone is not enough, and the measurement says so.** It
    covers 23 of the 104 instruments collected for. Where a name is known
    the incidental rule fires on 10.9% of stories; where it is not, 38.9% --
    and 87% for GILD, whose headlines say "Gilead Sciences" and never
    "GILD". A symbol-only fallback does not "over-flag slightly", it
    discards most of the coverage for four instruments in five.

    ``resolve`` is off by default because this is called per story. The
    backfill and the collection job warm the cache once; everything else
    reads it.
    """
    symbol = str(ticker)
    try:
        from cfdinvest.livehelper import t212_instruments

        # SUGGESTED, not INSTRUMENTS. Guessing the attribute name returned
        # an empty map silently, and the incidental rule then flagged
        # "Advanced Micro Devices Stock Ran The Roadmap" as an incidental
        # mention of AMD -- exactly the false positive the name lookup
        # exists to prevent, produced by the lookup failing quietly.
        table = getattr(t212_instruments, "SUGGESTED", None) or {}
        entry = table.get(symbol)
        if isinstance(entry, (tuple, list)) and entry:
            return (str(entry[0]),)
    except Exception as error:  # noqa: BLE001 - a missing table is not an outage
        logger.debug("No instrument table for %s: %s", symbol, error)

    cached = _cached_names(config).get(symbol)
    if cached:
        return (str(cached),)
    if not resolve:
        return ()
    name = _resolve_name(symbol)
    if name:
        _remember_name(symbol, name, config)
        return (name,)
    return ()


def _cache_path(config):
    from pathlib import Path

    root = Path(getattr(getattr(config, "paths", None), "data_root", "data"))
    return root / "metadata" / NAME_CACHE


def _cached_names(config) -> dict:
    import json

    try:
        path = _cache_path(config)
        if not path.is_file():
            return {}
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except Exception as error:  # noqa: BLE001
        logger.debug("Name cache unreadable: %s", error)
        return {}


def _remember_name(ticker: str, name: str, config) -> None:
    import json

    try:
        path = _cache_path(config)
        path.parent.mkdir(parents=True, exist_ok=True)
        held = _cached_names(config)
        held[str(ticker)] = str(name)
        path.write_text(json.dumps(held, indent=2, sort_keys=True), encoding="utf-8")
    except Exception as error:  # noqa: BLE001 - a cache miss is not an outage
        logger.debug("Could not cache the name for %s: %s", ticker, error)


def _resolve_name(ticker: str) -> str:
    """The company's name from the market-data vendor, or ``""``.

    Trimmed of the corporate suffixes a headline never uses: nobody writes
    "Gilead Sciences, Inc." in a title, so matching on it would fail for the
    same reason the symbol does.
    """
    try:
        import yfinance

        info = yfinance.Ticker(str(ticker)).info or {}
    except Exception as error:  # noqa: BLE001 - a missing name is not an outage
        logger.debug("No vendor name for %s: %s", ticker, error)
        return ""
    name = str(info.get("shortName") or info.get("longName") or "").strip()
    for suffix in (", Inc.", " Inc.", " Inc", ", Ltd.", " Ltd.", " Ltd",
                   " Corporation", " Corp.", " Corp", " Company", " Co.",
                   " plc", " PLC", " Group", " Holdings", " Incorporated"):
        if name.endswith(suffix):
            name = name[: -len(suffix)].strip()
    return name
