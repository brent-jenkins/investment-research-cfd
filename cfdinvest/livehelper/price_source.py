"""Which price the helper is about to reason with, and where it came from.

Three sources, in this order:

1. **What you typed.** ``--last-price`` always wins. You are looking at the
   ticket; nothing here knows better than that.
2. **A fresh live quote**, if the poller has taken one recently.
3. **The daily bar**, which is what this project had before live quotes and
   is always available.

The order is not negotiable and the fallback direction is always *down* --
towards the older, duller, better-understood number. A live quote is used
only when it can be shown to be current; a stale one is refused rather than
used with a caveat, because a price that is wrong in a fast market does more
damage than a price that is a day old and known to be.

This module reads :mod:`cfdinvest.livequotes.storage`, which opens files and
nothing else. It must never import ``livequotes.sources``: the live helper
sits behind a structural test that forbids it a network, and that test is
worth more than the convenience of fetching a quote on demand. A price the
helper had to go and get is a price it could block on.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["PriceSource", "resolve_price"]

#: What the summary prints for each origin.
NOTES = {
    "given": "Using the price you supplied",
    "live": "Using live quote",
    "bar": "Using daily bar",
    "none": "No price available",
}


class PriceSource:
    """One price, its origin, and the sentence explaining the choice."""

    __slots__ = ("price", "origin", "detail")

    def __init__(self, price: float | None, origin: str, detail: str = "") -> None:
        self.price = price
        self.origin = origin
        self.detail = detail

    @property
    def note(self) -> str:
        """The one-line note for the helper's summary."""
        head = NOTES.get(self.origin, NOTES["none"])
        return f"{head} — {self.detail}" if self.detail else head

    @property
    def is_live(self) -> bool:
        return self.origin == "live"

    def __repr__(self) -> str:  # pragma: no cover - debugging only
        return f"PriceSource({self.price!r}, {self.origin!r})"


def _daily_bar(ticker: str, app_config=None) -> tuple[float | None, str]:
    """The most recent close in the lake, or ``None`` with the reason."""
    try:
        from cfdinvest.repository import DataRepository

        panel = DataRepository(app_config).load_panel("close")
    except Exception as error:  # noqa: BLE001 - no lake is not a crash
        return None, f"the lake could not be read ({type(error).__name__})"

    if panel is None or getattr(panel, "empty", True):
        return None, "the lake holds no closes"
    if ticker not in panel.columns:
        return None, f"{ticker} is not in the lake"

    series = panel[ticker].dropna()
    if series.empty:
        return None, f"{ticker} has no close in the lake"
    return float(series.iloc[-1]), f"close of {series.index[-1]:%d/%m/%Y}"


def resolve_price(
    ticker: str,
    *,
    given: float | None = None,
    app_config=None,
    account: str | None = None,
    max_age_minutes: float | None = None,
    now=None,
) -> PriceSource:
    """The price to reason with, and where it came from. Never raises.

    A ``PriceSource`` whose ``price`` is ``None`` is a real answer, not a
    failure: the caller reports the absence, and the safety engine already
    refuses to reach ``SAFE`` without a price.
    """
    if given is not None:
        return PriceSource(float(given), "given")

    name = str(ticker or "").strip()
    if not name:
        return PriceSource(None, "none", "no instrument was named")

    if app_config is None:
        # Both lookups below need a real config to find the data root, and
        # None reaches DataRepository as an AttributeError rather than as a
        # missing lake. Load the default rather than report a fault.
        try:
            from cfdinvest.config import load_config

            app_config = load_config()
        except Exception as error:  # noqa: BLE001
            return PriceSource(
                None, "none", f"no configuration ({type(error).__name__})"
            )

    try:
        from cfdinvest.livequotes import storage as quote_store

        price, reason = quote_store.quote_for(
            name,
            app_config,
            account=account,
            max_age_minutes=max_age_minutes,
            now=now,
        )
    except Exception as error:  # noqa: BLE001 - quotes are an optimisation
        price, reason = None, f"live quotes unreadable ({type(error).__name__})"

    if price is not None:
        return PriceSource(price, "live", reason)

    bar, bar_reason = _daily_bar(name, app_config)
    if bar is not None:
        # Both halves are reported: which price is being used, and why the
        # better one was not. "Using daily bar" alone leaves the reader
        # wondering whether live quotes are broken or simply not running.
        return PriceSource(bar, "bar", f"{bar_reason}; {reason}")
    return PriceSource(None, "none", f"{bar_reason}; {reason}")
