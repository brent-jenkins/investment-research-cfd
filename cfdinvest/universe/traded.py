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

__all__ = ["TradedUniverse", "traded_universe", "resolve_target",
           "STALE_AFTER_DAYS"]

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

    @property
    def screen_failed(self) -> bool:
        """The screen was meant to answer and could not.

        Distinct from simply using the configured list. With
        ``follow_screen`` off, ``market.tickers`` is a deliberate human
        choice and trading it is correct. With it ON, a fall back to that
        list means the screen has failed -- and continuing to OPEN positions
        from a list somebody set at some unremembered point is inferring
        that it is still the right one, which is precisely what this module
        exists to stop being done silently.

        Closing is unaffected: what may be closed is whatever the broker
        says is open, and refusing an exit is never the safe direction.
        """
        return self.source.startswith("config (")

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
    # The intended size, which is not always the ticker count. The screen's
    # target defaults to len(market.tickers) but can be set explicitly, and
    # comparing against the ticker count then refuses every selection the
    # screen makes -- the guard would fire on the configuration working as
    # designed rather than on the drift it exists to catch.
    intended = resolve_target(app_config, len(configured))
    if intended and len(names) != intended:
        logger.warning(
            "The recorded selection holds %d instruments and %d are "
            "intended, so it was made under a different configuration; "
            "trading the configured list until the next screen",
            len(names), intended,
        )
        # `as_of` is carried through deliberately. Without it the halt mail
        # reported "Last usable selection: none on record" for a selection
        # recorded minutes earlier -- conflating "the screen has never run"
        # with "it ran and the size no longer matches", and sending the
        # reader to check a job that is working perfectly.
        return TradedUniverse(
            names=configured,
            source=f"config (selection is for {len(names)} instruments, "
                   f"{intended} intended)",
            as_of=as_of or None,
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


def _viability_ceiling(app_config, fallback: int) -> tuple[int | None, str]:
    """How many slots this balance can actually size, from observations.

    The resolver used to pass ``ceiling=None`` with a comment saying the
    dealing path should supply it, because that is the only layer holding a
    balance. It never did, so the curve chose alone and a size the account
    could not deal was as eligible as one it could.

    Both inputs are already recorded, so nothing has to be passed down and
    nothing reaches a network from here: ``persist.margin`` writes the
    balance on every dealing pass, and ``persist.spreads`` writes
    ``min_deal_size`` beside a bid and an offer. This reads them.

    Returns ``(None, reason)`` when it cannot be computed, and the reason is
    logged rather than swallowed -- an uncomputed ceiling and a generous one
    are different states, and only one of them should read as permission.

    Capped by ``universe.max_target``. The curve peaks at 70 and is still
    worth 209.7% at 100, a gap inside the 9.3pp floor, so "largest within the
    floor" would otherwise select the entire candidate pool.

    **An uncomputable ceiling falls back to what is held now, not to the
    maximum.** The first version returned the cap when it could not read a
    balance, so a missing input read as permission to hold seventy names --
    which is this project's most-named failure appearing in the guard written
    to prevent a different one. Not knowing how much the account can size is
    a reason to keep the book where it is.
    """
    cap = getattr(app_config.universe, "max_target", None)
    cap = int(cap) if cap else None
    try:
        from cfdinvest.persist import margin as margin_store
        from cfdinvest.persist import spreads as spread_store
        from cfdinvest.risk.capacity import minimum_tickets, position_capacity
    except Exception as error:  # noqa: BLE001 - a resolver must not explode
        return fallback, f"capacity layer unavailable ({error})"

    balance = None
    try:
        usage = [row for row in margin_store.load_margin(app_config)
                 if str(row.get("kind") or "") == "usage" and row.get("balance")]
        if usage:
            balance = float(usage[-1]["balance"])
    except Exception as error:  # noqa: BLE001
        return fallback, f"balance unreadable ({error})"
    if not balance:
        return fallback, ("no balance recorded yet; margin readings start "
                          "on the first dealing pass")

    try:
        tickets = minimum_tickets(spread_store.load_spreads(app_config))
    except Exception as error:  # noqa: BLE001
        return fallback, f"minimum deal sizes unreadable ({error})"
    if not tickets:
        return fallback, "no minimum deal sizes recorded"

    strategy = getattr(app_config, "strategy", None)
    capacity = position_capacity(
        balance, tickets,
        max_leverage=getattr(strategy, "max_leverage", None),
        max_instrument_concentration=getattr(
            strategy, "max_instrument_concentration", None),
        universe_size=cap,
    )
    if capacity.slots is None:
        return fallback, f"not computed ({capacity.note})"
    return int(capacity.slots), (
        f"balance {balance:,.0f}, {len(tickets)} minimum sizes, "
        f"bound by {capacity.binding}"
    )


def resolve_target(app_config, fallback: int) -> int:
    """How many to hold, derived rather than asserted.

    Three inputs. The *viability ceiling* -- how many positions this balance
    can size at all, given the broker's minimum deal sizes; below that a
    slot cannot be sized by conviction and every decision quantises to
    nothing. The *measured return curve*. And the *noise floor*, which
    decides when a difference between two sizes is real: changing only the
    day of the month a screen runs on moves CAGR by 14.6pp, so anything
    smaller than that is not a preference, it is sampling error.

    Falls back to the traded count when the curve is absent, and says so.
    A number computed from no measurement is the kind that looks derived
    and is not.
    """
    from cfdinvest.risk.capacity import operating_size

    # An explicit target wins. The override check used to live at the call
    # site, so moving the resolver here silently dropped it -- and the guard,
    # which now shares this function, would have ignored a pinned value.
    explicit = getattr(app_config.universe, "target", None)
    if explicit:
        return int(explicit)

    curve = {int(k): float(v)
             for k, v in (getattr(app_config.universe, "size_curve", {}) or {}).items()}
    if not curve:
        logger.info("No size curve configured; holding the traded count (%d)", fallback)
        return fallback

    ceiling, why = _viability_ceiling(app_config, fallback)
    if ceiling is None:
        logger.info("No viability ceiling (%s); the curve decides alone", why)
    else:
        logger.info("Viability ceiling %d slots (%s)", ceiling, why)

    chosen = operating_size(
        curve, ceiling=ceiling,
        prefer=str(getattr(app_config.universe, "size_preference", "largest")),
    )
    if chosen.slots is None:
        logger.warning("Size not computed (%s); holding %d", chosen.note, fallback)
        return fallback
    logger.info("Universe size computed: %s -- %s", chosen.describe(), chosen.note)
    return int(chosen.slots)
