"""News-driven trades: fresh company news sets the side for the session.

WHY THIS EXISTS
---------------
Every free price reading on this universe forecasts how FAR a name moves and
not which way (CLAUDE.md, 2026-08-28 to 2026-09-28). The headline labels
(``ingestion.headline_labels``) say what a story reports and whether the
event is good or bad for the company. The owner's decision, 2026-09-30:
trade on them now rather than wait for the pre-registered test -- "we're
looking for good trades; how it balances between Claude and the engine
isn't that important, it's the end result that matters". The veto form is
the fallback if this does not help.

THE RULE
--------
For each name and each bar, take the labelled stories that are about the
company itself, report a real event (not a price move, commentary or
"other"), and were first seen after 10:00 New York on the previous session
day's clock -- the 24 hours before today's 10:00 -- and no later than the
bar's END, which is when the engine can know them. Good counts +1, bad -1.
Where the sum is non-zero the name trades that way on that bar, at
``NEWS_CONFIDENCE``, sized by the engine's own volatility-target size, and
the sign persists bar to bar because the window does. It replaces the trend
signal for that name and bar, including one pointing the other way.

WHY IT LIVES HERE
-----------------
In the engine's signal step, applied by the same function wherever signals
are made -- the run the dealer reads, the last look before an open, and any
backtest handed labels. A rule known only to the dealer is how the account
traded a strategy nobody chose in August (CLAUDE.md, the paper book).

Pure: labels in, panels out. No IO, no network.
"""
from __future__ import annotations

import dataclasses
import logging
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

__all__ = [
    "NEWS_CONFIDENCE", "EXCLUDED_EVENTS", "events_from_labels",
    "news_sign_panel", "apply_news", "exempt_news_from_gate", "veto_gate", "MODES",
]

#: How labelled news may act on the book. ``trade`` opens positions on it;
#: ``veto`` only refuses trend opens it contradicts; ``off`` ignores it.
MODES = ("off", "trade", "veto")

#: Confidence given to a news-driven signal. Above the 0.75 entry gate so it
#: opens, and above the decayed-trim threshold (which is the gate) so a held
#: news position is not trimmed. A value, not a measurement: nothing here has
#: been measured, which is the point of trading it live.
NEWS_CONFIDENCE = 0.80

#: Labels that describe no event, so they carry no side.
EXCLUDED_EVENTS = frozenset({"price_move", "commentary", "other"})

_IMPACT = {"good": 1, "bad": -1}
_NEW_YORK = "America/New_York"


def events_from_labels(
    labels: Iterable[Mapping[str, Any]], *, prompt_version: str
) -> pd.DataFrame:
    """The labels that carry a side, as ``ticker, first_seen, sign``.

    One row per story: a story labelled twice under the same prompt keeps
    its latest label.
    """
    latest: dict[str, Mapping[str, Any]] = {}
    for row in labels or ():
        if str(row.get("prompt_version")) != str(prompt_version):
            continue
        latest[str(row.get("key"))] = row
    rows = []
    for row in latest.values():
        sign = _IMPACT.get(str(row.get("impact")))
        if (not sign or row.get("about") != "company"
                or row.get("event") in EXCLUDED_EVENTS):
            continue
        try:
            seen = pd.Timestamp(row.get("first_seen"))
        except (TypeError, ValueError):
            continue
        if pd.isna(seen):
            continue
        seen = seen.tz_localize("UTC") if seen.tzinfo is None else seen.tz_convert("UTC")
        rows.append({"ticker": str(row.get("ticker")), "first_seen": seen, "sign": sign})
    return pd.DataFrame(rows, columns=["ticker", "first_seen", "sign"])


def _bar_span(index: pd.Index) -> pd.Timedelta:
    """The bar length, from the smallest gap between consecutive bars."""
    gaps = pd.Series(pd.DatetimeIndex(index)).diff().dropna()
    gaps = gaps[gaps > pd.Timedelta(0)]
    return gaps.min() if len(gaps) else pd.Timedelta(hours=1)


def _window_starts(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """10:00 New York on each bar's session day, minus 24 hours, in UTC."""
    local = index.tz_convert(_NEW_YORK)
    ten = local.normalize() + pd.Timedelta(hours=10)
    return (ten - pd.Timedelta(hours=24)).tz_convert("UTC")


def news_sign_panel(
    events: pd.DataFrame | None,
    index: pd.Index,
    columns: Iterable[str],
) -> pd.DataFrame:
    """-1, 0 or +1 per bar and name: the net side of fresh company news."""
    columns = list(columns)
    panel = pd.DataFrame(0, index=index, columns=columns, dtype=int)
    if events is None or events.empty or not len(index):
        return panel
    stamps = pd.DatetimeIndex(index)
    stamps = stamps.tz_localize("UTC") if stamps.tz is None else stamps.tz_convert("UTC")
    ends = (stamps + _bar_span(stamps)).asi8
    starts = _window_starts(stamps).asi8
    for ticker, group in events.groupby("ticker"):
        if ticker not in panel.columns:
            continue
        group = group.sort_values("first_seen")
        seen = pd.DatetimeIndex(group["first_seen"]).asi8
        cumulative = np.concatenate([[0], np.cumsum(group["sign"].to_numpy())])
        # Events first seen in (start, end]: cumulative sum up to end minus
        # the sum up to and including start.
        net = (cumulative[np.searchsorted(seen, ends, side="right")]
               - cumulative[np.searchsorted(seen, starts, side="right")])
        panel[ticker] = np.sign(net).astype(int)
    return panel


def apply_news(signals, sign: pd.DataFrame, *, min_size: float):
    """The signal frame with news-driven bars written over the trend's.

    A news bar takes the news side at ``NEWS_CONFIDENCE`` and the engine's
    own volatility-target size. Where that size is missing or below
    ``min_size`` the bar is left as the trend made it: an unsized position
    is not one the engine could have taken.
    """
    if sign is None or sign.empty or signals.direction.empty:
        return signals
    sign = sign.reindex(index=signals.direction.index,
                        columns=signals.direction.columns).fillna(0).astype(int)
    base = signals.diagnostics.get("base_size") if signals.diagnostics else None
    if base is None or getattr(base, "empty", True):
        logger.warning("No base size to size news trades by; news not applied")
        return signals
    base = base.reindex_like(signals.size)
    sized = base.where(base >= min_size)
    active = (sign != 0) & sized.notna()
    if not active.to_numpy().any():
        return dataclasses.replace(
            signals, diagnostics={**signals.diagnostics, "news_sign": sign.where(active, 0)}
        )

    side = sign.where(active, 0)
    direction = signals.direction.mask(side > 0, "long").mask(side < 0, "short")
    size = signals.size.mask(active, sized)
    same = active & (signals.direction == direction)
    confidence = signals.confidence.mask(active, NEWS_CONFIDENCE).mask(
        same, np.maximum(signals.confidence, NEWS_CONFIDENCE)
    )
    reason = signals.reason.mask(active, "news") if not signals.reason.empty else signals.reason
    logger.info(
        "News sets the side on %d bar(s) across %d name(s)",
        int(active.to_numpy().sum()), int(active.any().sum()),
    )
    return dataclasses.replace(
        signals, direction=direction, size=size, confidence=confidence, reason=reason,
        diagnostics={**signals.diagnostics, "news_sign": side},
    )


def veto_gate(sign: pd.DataFrame, direction: pd.DataFrame) -> pd.DataFrame:
    """An entry gate that refuses a trend OPEN the fresh news contradicts.

    The veto form, switched to by the owner on 2026-09-30 after the filing
    backtest found a company's own 8-K did not call the rest of its day. It
    can only stop a trade, never start one: news agreeing with the engine,
    or no news, leaves the engine alone, and a position already held is
    never closed by it -- the engine applies entry gates to opens only.
    """
    sign = sign.reindex(index=direction.index, columns=direction.columns).fillna(0)
    against = ((sign > 0) & (direction == "short")) | ((sign < 0) & (direction == "long"))
    return ~against


def exempt_news_from_gate(mask, signals):
    """A breakout mask that permits every news-driven bar.

    The breakout gate asks whether a TREND has confirmed; a news trade is
    not a trend trade, so the question does not apply to it. Only the
    breakout mask is relaxed -- the screen's membership, the opening and
    closing windows and every cap still bind.
    """
    news = (signals.diagnostics or {}).get("news_sign")
    if mask is None or news is None or getattr(news, "empty", True):
        return mask
    active = (news.reindex(index=mask.index, columns=mask.columns).fillna(0) != 0)
    return mask | active
