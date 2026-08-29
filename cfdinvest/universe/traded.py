"""What the daemon trades, and where that answer came from.

One owner for a question that had two: ``market.tickers`` said what to
trade, the screen recorded what it would have chosen, and nothing connected
them because connecting them meant rewriting configuration and the project
forbids that outright.

The connection is the other way round. Configuration is never written; the
*selection* is read. ``market.tickers`` remains exactly as a person left it
and is what this returns whenever the screen has nothing to say -- which is
the normal state, since ``follow_screen`` is off by default.

Absence is named rather than inferred, which here means the caller is told
which source answered and how old it is. A daemon quietly trading a list
chosen four months ago because the screening job has been failing is the
failure this exists to make visible, and it is not hypothetical: three
separate mechanisms on this deployment reported success while doing nothing
in the space of one day.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

__all__ = ["TradedUniverse", "traded_universe", "STALE_AFTER_DAYS"]

#: A selection older than this is used anyway, and said out loud.
#:
#: Not a fallback to the configured list, which is the tempting design and
#: the wrong one: a scheduler outage would swap the entire book back to
#: whatever `market.tickers` happens to hold, closing and opening eight
#: positions for a reason that has nothing to do with any instrument. An
#: old decision is still a decision somebody can look at. Silence about its
#: age is the only part that is unacceptable.
STALE_AFTER_DAYS = 45


@dataclass(frozen=True, slots=True)
class TradedUniverse:
    """The list, and its provenance."""

    names: tuple[str, ...]
    source: str
    as_of: str | None = None
    stale: bool = False

    def describe(self) -> str:
        age = f", chosen {self.as_of[:10]}" if self.as_of else ""
        warning = " -- STALE" if self.stale else ""
        return f"{len(self.names)} instruments from {self.source}{age}{warning}"


def traded_universe(app_config, account: str | None = None) -> TradedUniverse:
    """The instruments to trade, from the screen if permitted or the config.

    Never raises and never returns nothing when the configuration holds
    something: an unreadable selection store falls back to the configured
    list rather than to an empty book, because "trade nothing" and "the
    record could not be read" are different statements and only one of them
    is an instruction.
    """
    configured = tuple(
        str(t) for t in (getattr(getattr(app_config, "market", None), "tickers", ()) or ())
        if str(t)
    )
    universe = getattr(app_config, "universe", None)
    if not getattr(universe, "follow_screen", False):
        return TradedUniverse(names=configured, source="config")

    try:
        from cfdinvest.universe.screen import load_screens

        rows = load_screens(app_config, account)
    except Exception as error:  # noqa: BLE001
        logger.warning(
            "Selections unreadable (%s); trading the configured list", error
        )
        return TradedUniverse(names=configured, source="config (selections unreadable)")

    chosen = None
    for row in reversed(rows or ()):
        names = tuple(str(t) for t in ((row or {}).get("selected") or ()) if str(t))
        if names:
            chosen = (names, str((row or {}).get("as_of") or ""))
            break

    if chosen is None:
        # Not an error. A deployment that has never screened has nothing
        # recorded, and the configured list is the right answer -- said out
        # loud, because "following the screen" and "there is no screen to
        # follow" look identical from the outside.
        logger.info(
            "follow_screen is on but nothing has been recorded yet; "
            "trading the configured list"
        )
        return TradedUniverse(names=configured, source="config (no selection recorded)")

    names, as_of = chosen

    # A selection is also stale when it was made under a different
    # configuration, and that kind of staleness has nothing to do with age.
    #
    # The screen's target derives from ``len(market.tickers)`` at the moment
    # it runs. Narrowing the traded list from twenty names to eight left a
    # two-day-old record holding twenty -- ten of them instruments the
    # current strategy has never been measured on. Following it would have
    # tripled the book on an account of a few thousand pounds, and every
    # age-based check would have called it fresh.
    #
    # Refused rather than truncated. Taking the first eight of a twenty-name
    # ranking is a different selection from the one the screen would make
    # asking for eight, because the sector cap and the profile demotion both
    # bind differently at different sizes.
    if configured and len(names) != len(configured):
        logger.warning(
            "The recorded selection holds %d instruments and %d are "
            "configured, so it was made under a different configuration; "
            "trading the configured list until the next screen",
            len(names), len(configured),
        )
        return TradedUniverse(
            names=configured,
            source=f"config (selection is for {len(names)} instruments)",
        )

    stale = _is_stale(as_of)
    if stale:
        logger.warning(
            "The traded list comes from a selection dated %s, more than %d "
            "days old -- the screening job may not be running",
            as_of[:10], STALE_AFTER_DAYS,
        )
    return TradedUniverse(
        names=names, source="screen", as_of=as_of or None, stale=stale
    )


def _is_stale(as_of: str) -> bool:
    """Older than the limit. An unparseable date is *not* called fresh."""
    if not as_of:
        return True
    try:
        import pandas as pd

        stamp = pd.Timestamp(as_of)
        if stamp.tzinfo is None:
            stamp = stamp.tz_localize("UTC")
        return (pd.Timestamp.now(tz="UTC") - stamp).days > STALE_AFTER_DAYS
    except Exception:  # noqa: BLE001
        return True
