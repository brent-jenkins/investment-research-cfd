"""What is being said about an instrument, alongside what its price did.

This is the one input here that cannot be backtested. yfinance returns a
rolling window of roughly ten headlines covering the last few days; there is
no archive, so a rule built on it can never be checked against the 456
earnings events or the twenty-one years of prices everything else was
measured on. That is a real limitation and it is why nothing in this module
sizes a position or clears a trade. It produces a number to show a person.

Why bother at all: on those 456 events, the actual reported EPS surprise
explains 0.3% of the variance in the reaction, and not significantly
(r=+0.058, t=1.25). A company can beat by 8% and fall 20%. Something
accounts for the rest, it is not arithmetic, and price history demonstrably
does not contain it -- the 15-day run-up predicts the reaction at r=-0.015.
So the space sentiment would have to explain is measurably there and
measurably unfilled.

The tautology this guards against
---------------------------------
Six of sixteen Lululemon headlines over a fortnight were reports of the
price move itself -- "stock falls Tuesday", "dips more than broader
market", "shares gap down". Counting those measures the past and calls it a
forecast: they correlate with yesterday's return by construction and
predict nothing. They are detected and excluded, and the count of what was
excluded is reported, because a filter that quietly drops half its input is
worse than no filter.

Scoring
-------
A lexicon, not a model. Crude, and deliberately so: it is deterministic, it
runs offline, it can be unit tested, and anyone can read why a headline
scored what it did. A cleverer scorer that could not be reproduced would
make the thing it is measuring impossible to audit.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "Headline",
    "Sentiment",
    "NEGATIVE_TERMS",
    "POSITIVE_TERMS",
    "PRICE_REPORT_PATTERNS",
    "is_price_report",
    "score_headline",
    "summarise_headlines",
    "fetch_headlines",
    "sentiment_for",
]

#: Phrases that describe a share's prospects getting worse. Weighted equally:
#: a lexicon that pretends to know an analyst downgrade is 1.4 times worse
#: than a guidance cut is inventing precision it does not have.
NEGATIVE_TERMS: tuple[str, ...] = (
    "downgrade", "downgrades", "downgraded", "cuts outlook", "cut outlook",
    "lowers", "lowered", "slashes", "slashed", "warns", "warning",
    "misses", "missed", "miss", "weak", "weaker", "weakness", "slump",
    "slowdown", "disappointing", "disappoints", "concerns", "pressure",
    "headwind", "headwinds", "loss", "losses", "lawsuit", "probe",
    "investigation", "recall", "resigns", "resignation", "departs",
    "departure", "steps down", "layoffs", "job cuts", "sell rating",
    "reduce", "underperform", "bearish", "short seller", "exits", "exit",
    "reduces stake", "trims stake", "halts", "suspends", "delays",
)

#: And better. "Beats" is here without a qualifier on purpose: a beat that
#: was sold off still says the company beat, and the scorer's job is to
#: report what was said rather than to predict the reaction.
POSITIVE_TERMS: tuple[str, ...] = (
    "upgrade", "upgrades", "upgraded", "raises", "raised", "boosts",
    "boosted", "beats", "beat", "tops", "outperform", "buy rating",
    "overweight", "bullish", "record", "strong", "stronger", "surges",
    "jumps", "rally", "expansion", "expands", "launches", "wins",
    "approval", "approved", "partnership", "buyback", "dividend increase",
    "raises stake", "builds stake", "initiates coverage", "price target raised",
)

#: A headline that is describing the price rather than telling you anything
#: about the company. These are the ones that make a naive count look
#: predictive when it is only reporting what already happened.
PRICE_REPORT_PATTERNS: tuple[str, ...] = (
    r"\bstock (falls|rises|drops|gains|climbs|slides|slips|jumps|dips)\b",
    r"\bshares? (fall|rise|drop|gain|climb|slide|slip|jump|dip|gap)\b",
    r"\b(dips|rises|falls|climbs) (more|less) than\b",
    r"\bstock price (up|down)\b",
    r"\b(up|down) \d+(\.\d+)?%\b",
    r"\bwhat'?s (happening|next)\b",
    r"\bincreases? despite\b",
    r"\bhere'?s what\b",
    r"\bstock market today\b",
)

_PRICE_REPORT = re.compile("|".join(PRICE_REPORT_PATTERNS), re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Headline:
    """One story, and what the lexicon made of it."""

    title: str
    published: Any = None
    publisher: str = ""
    score: int = 0
    #: Whether this is a report of the price move rather than news.
    price_report: bool = False
    matched: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Sentiment:
    """The reading for one instrument, and everything needed to doubt it."""

    ticker: str
    positive: int = 0
    negative: int = 0
    neutral: int = 0
    #: Headlines excluded as reports of the price move.
    excluded: int = 0
    headlines: tuple[Headline, ...] = ()
    #: ``None`` when nothing could be read -- distinct from a balanced read.
    score: float | None = None

    @property
    def counted(self) -> int:
        return self.positive + self.negative + self.neutral

    @property
    def label(self) -> str:
        """A word for the number, or an honest absence."""
        if self.score is None:
            return "no news"
        if self.score <= -0.5:
            return "strongly negative"
        if self.score < -0.15:
            return "negative"
        if self.score <= 0.15:
            return "mixed"
        if self.score < 0.5:
            return "positive"
        return "strongly positive"

    def describe(self) -> str:
        """One line, including what was thrown away.

        The excluded count is part of the reading, not a footnote: eleven to
        four looks decisive until you learn six of the sixteen were the
        price reporting itself.
        """
        if self.score is None:
            return "no news found"
        parts = [f"{self.label} ({self.positive}+ / {self.negative}-"]
        if self.neutral:
            parts.append(f" / {self.neutral} neutral")
        parts.append(")")
        if self.excluded:
            parts.append(f", {self.excluded} price-report headline"
                         f"{'s' if self.excluded != 1 else ''} ignored")
        return "".join(parts)


def is_price_report(title: str) -> bool:
    """Whether a headline is describing the price rather than the company."""
    return bool(_PRICE_REPORT.search(str(title or "")))


def score_headline(title: str) -> tuple[int, tuple[str, ...]]:
    """``(-1, 0, +1)`` and the terms that decided it.

    Returns the terms as well as the score so a reader can see why. A
    sentiment number nobody can interrogate is a number nobody should act
    on, and this one is going in front of a person who has to decide.

    A headline matching both directions scores zero rather than picking a
    winner by count -- "beats expectations but cuts outlook" is genuinely
    ambiguous and pretending otherwise is where a lexicon starts lying.
    """
    text = str(title or "").lower()
    hits_neg = tuple(t for t in NEGATIVE_TERMS if t in text)
    hits_pos = tuple(t for t in POSITIVE_TERMS if t in text)

    if hits_neg and hits_pos:
        return 0, hits_neg + hits_pos
    if hits_neg:
        return -1, hits_neg
    if hits_pos:
        return 1, hits_pos
    return 0, ()


def summarise_headlines(ticker: str, titles: Iterable[Any]) -> Sentiment:
    """Score a set of headlines. Pure, and never raises.

    ``titles`` may be strings or mappings carrying ``title``, ``published``
    and ``publisher``.
    """
    scored: list[Headline] = []
    positive = negative = neutral = excluded = 0

    for raw in titles or ():
        if isinstance(raw, str):
            title, published, publisher = raw, None, ""
        else:
            title = str(raw.get("title") or "")
            published = raw.get("published")
            publisher = str(raw.get("publisher") or "")
        if not title.strip():
            continue

        if is_price_report(title):
            excluded += 1
            scored.append(Headline(title, published, publisher, 0, True, ()))
            continue

        value, matched = score_headline(title)
        scored.append(Headline(title, published, publisher, value, False, matched))
        if value > 0:
            positive += 1
        elif value < 0:
            negative += 1
        else:
            neutral += 1

    counted = positive + negative + neutral
    # Nothing to read is not the same as a balanced read, so it is None.
    score = None if counted == 0 else (positive - negative) / counted

    return Sentiment(
        ticker=str(ticker),
        positive=positive,
        negative=negative,
        neutral=neutral,
        excluded=excluded,
        headlines=tuple(scored),
        score=score,
    )


def fetch_headlines(ticker: str, *, limit: int = 20) -> list[dict]:
    """Recent headlines from yfinance. Never raises; returns [] on failure.

    yfinance is already this project's market-data vendor, so this adds no
    new outbound destination. What it does add is a dependence on a feed
    with no history, which is why the result is only ever displayed.
    """
    try:
        import yfinance
    except ImportError:  # pragma: no cover - yfinance is an install extra
        logger.warning("yfinance is not installed; no headlines fetched")
        return []

    try:
        raw = yfinance.Ticker(str(ticker)).news or []
    except Exception as error:  # noqa: BLE001 - a missing feed is not an outage
        logger.warning("No headlines for %s: %s", ticker, error)
        return []

    stories = []
    for item in raw[:limit]:
        content = item.get("content") if isinstance(item, dict) else None
        body = content if isinstance(content, dict) else item
        if not isinstance(body, dict):
            continue
        title = body.get("title")
        if not title:
            continue
        stories.append({
            "title": str(title),
            "published": body.get("pubDate") or body.get("providerPublishTime"),
            "publisher": str(
                (body.get("provider") or {}).get("displayName", "")
                if isinstance(body.get("provider"), dict)
                else body.get("publisher") or ""
            ),
        })
    return stories


def sentiment_for(ticker: str, *, fetch=None) -> Sentiment:
    """The reading for one instrument. Never raises.

    ``fetch`` is the seam the tests use; by default it is the live feed.
    """
    reader = fetch or fetch_headlines
    try:
        stories = reader(ticker)
    except Exception as error:  # noqa: BLE001
        logger.warning("Could not read sentiment for %s: %s", ticker, error)
        stories = []
    return summarise_headlines(ticker, stories)
