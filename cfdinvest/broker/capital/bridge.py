"""From a signal to an order, and back again to check it arrived.

This is the piece that makes the demo account trade. It reads what the
engine wants, sizes it against the balance the broker reports, applies every
limit, and places what survives. It also reads the account back and refuses
to do anything at all when what is there disagrees with what it placed.

The rules it follows are the engine's, not new ones
---------------------------------------------------
Position size is ``signal size x exposure_per_unit_size x balance``, the
same expression the backtest uses, and the same ``needs_resize`` hysteresis
decides when to adjust. Inventing a sizing rule here would make the demo
account measure that rule rather than the strategy -- and the strategy's
recorded return came from a book sized this way.

**It resizes.** The paper book only opens and closes because Trading 212
cannot resize a CFD; that is a venue's constraint and this is a different
venue. The owner's call, and the right one: 62% of the engine's actions are
resizes, so a book that skipped them would be a different strategy wearing
the same name.

What it refuses
---------------
**Anything outside the exchange session.** Measured on 2026-08-27, spreads
widen a median of 1.85x once the session closes and JNJ by 14.9x, against a
strategy that breaks even near 6bp of friction. Capital.com will happily
deal at those prices -- it reported eighteen of twenty as tradeable
forty-six minutes after the close -- so the calendar decides, never the
broker's flag.

**Any size below the instrument's minimum.** Rounded *down* to the dealing
increment, always: a size rounded up can breach the limit it was computed
from. Below the minimum it is refused rather than raised, because a zero is
the sizing step declining and rounding it up would override that decision.

**Everything, when the account holds a position this app never opened.**
That is money at risk nobody chose, and the most likely cause of it is the
thing that would make the next order wrong too. It alerts and stops
opening. It does not try to correct: acting on an account you have just
demonstrated you do not understand is how one bad read becomes several bad
trades.

**The other direction is not the same fault, and treating it as one cost a
session.** A position the RECORD holds and the account does not is simply
gone -- the account is the truth about what is held. On 2026-09-10 the
owner closed TGT by hand, which is a permitted action, no result row was
written, and this bridge then refused every dealing pass for the rest of
the day over a disagreement that had a benign and known cause. A permitted
action jammed the dealer and nothing in the app could clear it.

The caller now reconciles that direction before it asks -- see
`persist.orders.record_departure` -- so by the time `plan_orders` runs, a
departed position has been recorded as closed and announced. This module's
rule is unchanged: it still refuses to act on any disagreement it is
handed.

Every decision is recorded, including the refusals. A refusal is the more
interesting record: it is the only evidence of the limits doing their job.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "Intent",
    "BridgeReport",
    "plan_orders",
    "reconcile",
]


@dataclass(frozen=True, slots=True)
class Intent:
    """One instrument: what is held, what is wanted, and what to do."""

    ticker: str
    epic: str = ""
    direction: str = "flat"
    held_units: float = 0.0
    target_units: float = 0.0
    action: str = "none"          # open | resize | close | none | refused
    reason: str = ""
    price: float | None = None
    notional: float | None = None
    deal_id: str = ""
    #: Completed bars since the ENGINE entered this position, carried from
    #: `persist.intent`. Zero means the engine opened it on the newest
    #: completed bar -- a fresh opportunity. Anything larger means the
    #: engine has been holding while the account has not, so an open planned
    #: from this row is a chase rather than a new decision.
    #:
    #: Recorded, never acted on. Measuring the stale-intent loop on
    #: 2026-09-19 required joining refusals to the journal by timestamp,
    #: because this number reached the log and not the record. Carrying it
    #: here makes that reproducible from the order store, which is what the
    #: store is for.
    entered_bars_ago: int | None = None
    #: What the last look judged, and the bar it judged against. All three
    #: together, because a reading without its threshold cannot be read
    #: later: the gate moved from 0.35 to 0.75 on 2026-09-04 and every
    #: refusal figure taken before it had to be retracted on 2026-09-19 for
    #: being quoted against the wrong one. A row that carries its own bar
    #: cannot be misread that way.
    engine_confidence: float | None = None
    live_confidence: float | None = None
    confidence_bar: float | None = None

    @property
    def acts(self) -> bool:
        return self.action in ("open", "resize", "close")


@dataclass(frozen=True, slots=True)
class BridgeReport:
    """What one pass decided, and whether it was allowed to act at all."""

    intents: tuple[Intent, ...] = ()
    halted: bool = False
    halt_reason: str = ""
    balance: float | None = None
    gross_target: float | None = None
    unexplained: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()

    #: What the dealing pass places, and in what order.
    #:
    #: **Closes first, then resizes, then opens.** The planning stage already
    #: knows about every intent at once, so this changes nothing about what
    #: is decided -- only the sequence in which the account is moved, and
    #: that sequence matters at the broker.
    #:
    #: A close frees margin; an open commits it. Placed in build order --
    #: roughly ticker order -- an open could be sent before a close that
    #: would have funded it, and Capital would reject it for margin. The
    #: rejection would read as a broker error rather than as an ordering
    #: mistake, which is the kind of fault that survives a long time.
    #:
    #: It has never bitten, because `max_margin_usage` is unset and the
    #: gross ceiling has never fired on the account. That is correct by
    #: coincidence rather than by design: the day either limit is switched
    #: on, the coincidence ends. Resizes sit in the middle because a resize
    #: may release margin or commit it, and the delta is usually small.
    _ORDER = {"close": 0, "resize": 1, "open": 2}

    @property
    def actionable(self) -> tuple[Intent, ...]:
        acting = [i for i in self.intents if i.acts]

        # **A halt stops OPENING. It does not stop closing.**
        #
        # This returned nothing at all, and on 2026-09-10 that turned a
        # guard into the problem it was guarding against. The record and the
        # account disagreed about SPGI -- correctly, one close row had
        # removed an instrument that still had two legs open -- and the
        # bridge refused to act. It halts BEFORE it plans, so no close
        # intent existed either, and a fix deployed seventeen minutes
        # earlier that would have flattened both legs never ran. Half an
        # hour before the bell, in a configuration whose whole purpose is
        # being flat overnight.
        #
        # The margin guard already gets this right, and says why in this
        # same module: closes are never refused, because "a book short of
        # margin is exactly the one that most needs to reduce". A book the
        # app does not understand is exactly the one that most needs to be
        # reducible.
        #
        # Closing is also the safe direction on its own terms. It commits no
        # capital, needs no sizing, cannot breach a limit, and acts on a
        # deal id the BROKER supplied rather than on anything the record
        # believes. Every argument for halting -- "acting on an account you
        # have just demonstrated you do not understand" -- is an argument
        # about taking on exposure.
        if self.halted:
            acting = [i for i in acting if i.action == "close"]

        # Stable, so instruments keep their relative order inside each group
        # and a pass remains reproducible from its own record.
        return tuple(sorted(acting, key=lambda i: self._ORDER.get(i.action, 9)))


def _round_down(value: float, increment: float) -> float:
    """Down to the dealing increment, never up.

    A size rounded up can breach the limit it was computed from, which would
    make this module the cause of the problem it exists to prevent.
    """
    if not increment or increment <= 0:
        return float(value)
    steps = int(abs(value) / increment)
    return float(steps * increment) * (1.0 if value >= 0 else -1.0)


def reconcile(
    held: Mapping[str, Mapping[str, Any]],
    placed: Mapping[str, Mapping[str, Any]],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Compare the account against the record. Returns (unexplained, missing).

    ``held`` is what the broker says, keyed by epic; ``placed`` is what this
    bridge believes it opened. Both directions matter and they fail
    differently: an unexplained position is money at risk nobody chose,
    while a missing one is the record being behind the account.

    **RETRACTED 2026-09-10:** this said a missing position means "the engine
    is reasoning about a position that does not exist". It does not, and the
    claim was load-bearing for a halt that cost a full session. ``placed`` is
    read HERE and nowhere else in ``plan_orders``; every position figure --
    sizing, exposure, the leverage and concentration caps -- comes from
    ``positions``, which is the broker's own answer. A record behind the
    account cannot produce a wrong order. It can only stop the account
    trading.

    The asymmetry that survives is the real one: an unexplained position
    must halt everything, and a missing one must be recorded as closed by
    the caller and announced, because its causes range from a hand closure
    to an order path recording fills it is not getting.
    """
    in_account = {str(k) for k in (held or {})}
    in_record = {str(k) for k in (placed or {})}
    return tuple(sorted(in_account - in_record)), tuple(sorted(in_record - in_account))


def plan_orders(
    signals: Sequence[Mapping[str, Any]],
    markets: Mapping[str, Any],
    positions: Mapping[str, Mapping[str, Any]],
    *,
    balance: float,
    limits,
    exposure_per_unit_size: float,
    rebalance_threshold: float,
    session_open: Mapping[str, bool] | None = None,
    placed: Mapping[str, Mapping[str, Any]] | None = None,
    fx_rate: float = 1.0,
    recheck: bool = True,
    rechecked: Mapping[str, Mapping[str, Any]] | None = None,
    entry_confidence: float = 0.35,
    open_allowed: Mapping[str, bool] | None = None,
    max_opens_per_pass: int | None = None,
    available: float | None = None,
    max_margin_usage: float | None = None,
    closing_soon: Mapping[str, bool] | None = None,
    session_ending: Mapping[str, bool] | None = None,
    flatten_before_close: bool = False,
    opens_paused: bool = False,
) -> BridgeReport:
    """Decide what to place. Pure: it reads and returns, and places nothing.

    Separated from the dealing so the decision can be tested without a
    network and without a gate — the thing that decides and the thing that
    acts are different concerns, and only one of them can lose money.
    """
    from cfdinvest.advisor.livecheck import holds
    from cfdinvest.execution.state import needs_resize
    import numpy as np

    unexplained, missing = reconcile(
        positions, placed if placed is not None else positions
    )
    halt_reason = ""
    if unexplained or missing:
        halt_reason = (
            "the account and the record disagree — "
            + (f"unexplained: {', '.join(unexplained)}. " if unexplained else "")
            + (f"missing: {', '.join(missing)}. " if missing else "")
            + "Opening nothing until a person has looked."
        )

    if not balance or balance <= 0:
        return BridgeReport(
            halted=True,
            halt_reason="the balance could not be read, so nothing can be sized",
        )

    open_now = dict(session_open or {})
    intents: list[Intent] = []
    gross = 0.0

    # Margin already committed, and the headroom left. `available` is what
    # remains after committed margin, so the difference is what the book is
    # currently using -- the number that decides whether a drawdown is
    # survivable or terminal, and the one nothing here has ever read.
    #
    # Capital varies its margin factors: tiered by size, raised in volatile
    # conditions and around events. A factor rising under an open position
    # increases required margin without the book changing at all, so the
    # headroom has to be read each pass rather than inferred from what we
    # committed when we opened.
    margin_used = None
    if max_margin_usage is not None and balance and available is not None:
        try:
            margin_used = float(balance) - float(available)
        except (TypeError, ValueError):
            margin_used = None
    margin_ceiling = (
        float(max_margin_usage) * float(balance)
        if max_margin_usage is not None and balance else None
    )

    for row in signals or ():
        ticker = str(row.get("ticker") or "")
        if not ticker:
            continue
        market = markets.get(ticker)
        position = dict(positions.get(getattr(market, "epic", ticker), {}) or {})
        held_units = float(position.get("size") or 0.0)
        if str(position.get("direction") or "").upper() in ("SELL", "SHORT"):
            held_units = -held_units

        epic = getattr(market, "epic", "") or ticker
        price = getattr(market, "offer", None) or getattr(market, "bid", None)
        direction = str(row.get("direction") or "flat").lower()

        age = row.get("entered_bars_ago")
        try:
            age = None if age is None else int(age)
        except (TypeError, ValueError):
            # An unreadable age is absent, never zero: zero is the claim
            # "this is a fresh entry", and inferring it would report a chase
            # as a new decision.
            age = None

        try:
            engine_confidence = float(row.get("confidence"))
        except (TypeError, ValueError):
            engine_confidence = None

        def refuse(why: str, live: float | None = None) -> Intent:
            return Intent(
                ticker=ticker, epic=epic, direction=direction,
                held_units=held_units, target_units=held_units,
                action="refused", reason=why, price=price,
                entered_bars_ago=age,
                engine_confidence=engine_confidence,
                live_confidence=live,
                confidence_bar=float(entry_confidence),
            )

        if market is None or price is None or price <= 0:
            intents.append(refuse("no price"))
            continue
        # Inside the opening window a *new* position is refused; anything
        # already held may still be closed or resized. The bell is the
        # dearest moment of the day to enter and the cheapest thing to skip.
        #
        # **The refusal names the window that blocked it**, from 2026-09-25.
        # `open_allowed` is False for two unrelated reasons -- too soon after
        # the open, or too near the close -- and until today both wrote
        # "within the opening window". Friday's store held 167 such rows, and
        # the seventy at 17:00-19:00 UTC were the CLOSING window wearing the
        # opening window's name. Seventy rows a day mislabelled, in the store
        # this project reads its refusal rates out of.
        #
        # Told apart by the calendar rather than by the hour. The hour
        # separates them today and stops doing so on 1 November, when US
        # clocks change and the session moves by an hour in UTC -- so a
        # heuristic would mis-assign silently within five weeks of being
        # written. `session_ending` is the same calendar answer that closed
        # the window, handed through rather than re-derived.
        #
        # Neither reason is in `persist.orders.LAST_LOOK_REASONS`: both are
        # limits, not the gate, and `refusal_summary` must not count them.
        if (
            open_allowed is not None
            and held_units == 0.0
            and not open_allowed.get(ticker, False)
        ):
            intents.append(refuse(
                "too near the session close to open"
                if (session_ending or {}).get(ticker, False)
                else "within the opening window"
            ))
            continue

        if not open_now.get(ticker, False):
            # Closing is refused too. A close outside the session pays the
            # same widened spread as an open, and the engine's own exit will
            # act on the next in-session bar.
            intents.append(refuse("outside the exchange session"))
            continue

        # The engine's own sizing, in the engine's own terms.
        #
        # A row with no size at all is not a zero. It means the book holds
        # this instrument but never recorded what the engine wanted, which
        # is a gap in the record rather than an instruction to hold nothing
        # -- so it is refused by name instead of being sized to zero and
        # read later as the strategy declining.
        if row.get("size") is None and direction in ("long", "short"):
            intents.append(refuse("no recorded size to mirror"))
            continue
        share = 0.0
        try:
            share = float(row.get("size") or 0.0)
        except (TypeError, ValueError):
            share = 0.0
        if direction not in ("long", "short"):
            share = 0.0

        wanted_notional = share * float(exposure_per_unit_size) * float(balance)
        cap = float(getattr(limits, "max_exposure_per_instrument_gbp", 0.0) or 0.0)
        capped = False
        if cap and wanted_notional > cap:
            wanted_notional, capped = cap, True

        # Notional is in account currency; the instrument is not.
        units = wanted_notional / (float(price) * float(fx_rate or 1.0))
        if direction == "short":
            units = -units

        # The session is ending: close what is held rather than carry it.
        #
        # The strategy carried 94% of its entries overnight, structurally --
        # the time stop is 6.96 hours against a six-hour session, so nothing
        # could reach its stop before the bell. Flattening is worth +55.4pp
        # with drawdown roughly halved, and it earns that on LESS leverage,
        # so it is not exposure in disguise.
        #
        # Before sizing deliberately: a position being closed needs no
        # target, and computing one first would let the minimum-deal-size
        # branch below refuse a CLOSE for being too small to OPEN.
        if flatten_before_close and (closing_soon or {}).get(ticker, False):
            if held_units != 0.0:
                intents.append(Intent(
                    ticker=ticker, epic=epic, direction=direction,
                    held_units=held_units, target_units=0.0, action="close",
                    reason="flattened before the session close",
                    price=price, deal_id=str(position.get("deal_id") or ""),
                ))
            else:
                intents.append(refuse("the session is ending"))
            continue

        increment = getattr(market, "min_deal_size", None) or 0.0
        target = _round_down(units, increment)
        minimum = float(increment or 0.0)

        if target == 0.0 and held_units == 0.0:
            intents.append(refuse(
                f"below the minimum deal size of {minimum:g}"
                if share > 0 else "no signal"
            ))
            continue

        if target == 0.0 and held_units != 0.0:
            intents.append(Intent(
                ticker=ticker, epic=epic, direction=direction,
                held_units=held_units, target_units=0.0, action="close",
                # The engine's own word for it -- time_stop, stop_loss,
                # take_profit, stop_out or signal -- when the row carries
                # one. "The signal is flat" is true of all of them and
                # distinguishes none, which is why every close in the order
                # store read the same.
                reason=(
                    str(row.get("reason") or "").strip()
                    or ("the signal is flat" if share == 0 else "below the minimum")
                ),
                price=price, deal_id=str(position.get("deal_id") or ""),
            ))
            continue

        if held_units == 0.0:
            # What the last look READ, carried to the approval as well as to
            # the refusal.
            #
            # Set here rather than at the top of the loop so it cannot carry
            # from the previous instrument: a reading that leaks across names
            # is worse than no reading, because it looks measured.
            #
            # ``None`` when the check did not run. Only the open path
            # consults the last look, so a close, a resize and a hold have
            # nothing to report and say so rather than reporting a zero.
            live_reading: float | None = None

            # The last look, on opens only.
            #
            # It is what the paper book already does, on the same evidence:
            # requiring the signal to still hold against the live price
            # refused 197 of 2,139 round trips, and those refused won 36.0%
            # against 50.4% for the ones kept (t=+4.18, p=0.000038).
            #
            # Opens only, deliberately. The measurement was about entries. A
            # resize acts on a position already justified and the hysteresis
            # already demands a 25% drift; gating it would be inventing a
            # rule no evidence covers, and gating a *close* would refuse an
            # exit, which is never the safe direction.
            if recheck:
                if rechecked is None:
                    intents.append(refuse(
                        "no re-evaluation was supplied, and an unchecked "
                        "signal is not an approved one"
                    ))
                    continue
                still, why, live = holds(
                    rechecked, ticker, direction, entry_confidence
                )
                live_reading = live
                if not still:
                    intents.append(refuse(why, live))
                    continue
            elif rechecked is not None:
                # REPORTED, NOT ENFORCED -- the house pattern, applied to the
                # last look itself from 2026-09-23.
                #
                # The check is switched off because it disagrees with the
                # engine by a third at and above the gate, which is the
                # 2026-09-10 signature. But switching it off would also stop
                # the readings that say whether that is true, and then
                # nothing could ever judge the decision to switch it off --
                # the same trap as silencing the stale-intent loop, which
                # this project declined for exactly that reason.
                #
                # So the reading is still taken and still recorded on the
                # row. It simply decides nothing.
                _, _, live_reading = holds(
                    rechecked, ticker, direction, entry_confidence
                )

            # The margin this open would commit, against what is left.
            #
            # Fails closed, like the last look above it: a ceiling that is
            # configured but silently skipped because a field was missing is
            # the inert-gate failure this project keeps finding, and a margin
            # guard that disables itself is worse than none because it reads
            # as protection. "Not checked" is not "within the limit".
            if max_margin_usage is not None:
                if margin_used is None:
                    intents.append(refuse(
                        "margin headroom could not be read, and an unchecked "
                        "margin limit is not a satisfied one"
                    ))
                    continue
                # The FRACTION, not the percentage the broker states. These
                # differ by 100x and the wrong one refuses every open.
                factor = getattr(market, "margin_fraction", None)
                if factor is None:
                    intents.append(refuse(
                        "the broker did not quote a margin factor for this "
                        "instrument, so the margin it needs is unknown"
                    ))
                    continue
                needed = abs(target) * price * float(fx_rate or 1.0) * float(factor)
                if margin_ceiling is not None and margin_used + needed > margin_ceiling:
                    intents.append(refuse(
                        f"margin: {needed:,.0f} more would take usage past "
                        f"{100 * float(max_margin_usage):.0f}% of balance"
                    ))
                    continue
                margin_used += needed

            gross += abs(target) * price * float(fx_rate or 1.0)
            # The three numbers go on the APPROVAL too, added 2026-09-23.
            #
            # Recording them only on refusals described the left tail of the
            # live/engine gap and called it the whole distribution. Measured
            # on the first three days of readings, refused names in the
            # >=0.75 band read 0.448 against the engine's 0.796 -- the
            # 2026-09-10 signature -- while every band below agreed to a
            # thousandth. But names that PASSED recorded nothing, so whether
            # the gap is bimodal (agrees, or collapses by a third) or simply
            # the left tail of a noisy check could not be told apart, and
            # those mean different things.
            #
            # `test_the_refusal_records_how_far` already warned about exactly
            # this censoring, in the docstring of the test asserting that
            # `holds` reports a reading on approval -- and then only the
            # refusal path was wired. A test that names a risk it does not
            # cover is the shape this project keeps rediscovering.
            intents.append(Intent(
                ticker=ticker, epic=epic, direction=direction,
                held_units=0.0, target_units=target, action="open",
                reason="capped by the per-instrument limit" if capped else "",
                price=price,
                notional=abs(target) * price * float(fx_rate or 1.0),
                entered_bars_ago=age,
                engine_confidence=engine_confidence,
                live_confidence=live_reading,
                confidence_bar=float(entry_confidence),
            ))
            continue

        if (held_units > 0) != (target > 0):
            # A reversal is a close and an open, never one order: the venue
            # would net them, and the record would show neither.
            intents.append(Intent(
                ticker=ticker, epic=epic, direction=direction,
                held_units=held_units, target_units=0.0, action="close",
                reason="direction reversed", price=price,
                deal_id=str(position.get("deal_id") or ""),
            ))
            continue

        drifted = bool(
            needs_resize(
                np.array([held_units]), np.array([target]),
                threshold=float(rebalance_threshold),
            )[0]
        )
        gross += abs(target if drifted else held_units) * price * float(fx_rate or 1.0)
        if not drifted:
            intents.append(Intent(
                ticker=ticker, epic=epic, direction=direction,
                held_units=held_units, target_units=held_units, action="none",
                reason="inside the rebalance threshold", price=price,
            ))
            continue

        intents.append(Intent(
            ticker=ticker, epic=epic, direction=direction,
            held_units=held_units, target_units=target, action="resize",
            reason="capped by the per-instrument limit" if capped else "",
            price=price, notional=abs(target) * price * float(fx_rate or 1.0),
            deal_id=str(position.get("deal_id") or ""),
        ))

    # **Paused: nothing may add risk, everything may remove it.** Owner's
    # decision, 2026-10-01: the strategy is measured to lose, and the demo
    # balance cannot be reset, so it is treated as money that runs out.
    #
    # Applied AFTER the loop rather than at the top, so every intent is still
    # formed and the last look still reads -- the record keeps saying what
    # the engine would have done, which is the only evidence a paused account
    # can still produce. An open is refused, and so is a resize that GROWS a
    # position; a close, a shrinking resize and the flatten pass untouched,
    # because a book that may not reduce is the failure the reconciliation
    # halt taught on 2026-09-10. A reversal is already a close alone.
    if opens_paused:
        from cfdinvest.persist.orders import PAUSED_REASON

        intents = [
            Intent(
                ticker=i.ticker, epic=i.epic, direction=i.direction,
                held_units=i.held_units, target_units=i.held_units,
                action="refused", reason=PAUSED_REASON, price=i.price,
                entered_bars_ago=i.entered_bars_ago,
                engine_confidence=i.engine_confidence,
                live_confidence=i.live_confidence,
                confidence_bar=i.confidence_bar,
            )
            if i.action == "open"
            or (i.action == "resize" and abs(i.target_units) > abs(i.held_units))
            else i
            for i in intents
        ]

    # Only so many new positions per pass, highest conviction first.
    #
    # Applied before the gross cap so the survivors are sized against the
    # room the refusals freed, rather than every position being shrunk to
    # fit a book that is then cut anyway. Opens only: a close is never
    # deferred, and a resize is an adjustment to something already held.
    if max_opens_per_pass is not None:
        opening = [i for i in intents if i.action == "open"]
        if len(opening) > int(max_opens_per_pass):
            def conviction(intent):
                row = (rechecked or {}).get(intent.ticker) or {}
                return float(row.get("confidence") or 0.0)

            keep = {
                id(i) for i in
                sorted(opening, key=conviction, reverse=True)[:int(max_opens_per_pass)]
            }
            deferred = len(opening) - len(keep)
            intents = [
                i if (i.action != "open" or id(i) in keep) else Intent(
                    ticker=i.ticker, epic=i.epic, direction=i.direction,
                    held_units=i.held_units, target_units=i.held_units,
                    action="refused", price=i.price,
                    reason=(
                        f"deferred: {max_opens_per_pass} opens already this "
                        f"pass, {deferred} waiting"
                    ),
                )
                for i in intents
            ]

    # The book-wide cap, applied last -- after the per-pass deferral above,
    # so the survivors are sized against the room the refusals freed.
    #
    # This used to claim it matched "the engine, which applies it after the
    # keep rather than before". That was false: until 2026-09-07 the engine
    # capped and *then* deferred, the reverse of here, while its own comment
    # cited this one as its authority. Two comments each pointing at the
    # other, and the order between them inverted. The engine has been moved
    # to match this, and tests/test_engine_models_dealing.py now asserts the
    # correspondence rather than leaving it to prose.
    #
    # Note what this does NOT do: only `open` and `resize` intents are
    # shrunk, so a held position sitting at `none` passes through untouched
    # and a book that drifts above the ceiling stays there. That is the same
    # deliberate choice as max_open_positions -- never liquidate into a
    # drawdown -- and it means the ceiling bounds NEW exposure rather than
    # total. CLAUDE.md records the consequence: the gross limit has never
    # fired on the live account.
    ceiling = float(getattr(limits, "max_total_exposure_gbp", 0.0) or 0.0)
    if ceiling and gross > ceiling:
        scale = ceiling / gross
        scaled: list[Intent] = []
        for intent in intents:
            if intent.action in ("open", "resize"):
                shrunk = _round_down(
                    intent.target_units * scale,
                    getattr(markets.get(intent.ticker), "min_deal_size", None) or 0.0,
                )
                if shrunk == 0.0:
                    scaled.append(Intent(
                        ticker=intent.ticker, epic=intent.epic,
                        direction=intent.direction, held_units=intent.held_units,
                        target_units=intent.held_units, action="refused",
                        reason="no headroom under the gross exposure limit",
                        price=intent.price,
                    ))
                    continue
                scaled.append(Intent(
                    ticker=intent.ticker, epic=intent.epic,
                    direction=intent.direction, held_units=intent.held_units,
                    target_units=shrunk, action=intent.action,
                    reason=(intent.reason + "; scaled to the gross limit").strip("; "),
                    price=intent.price,
                    notional=abs(shrunk) * (intent.price or 0.0) * float(fx_rate or 1.0),
                    deal_id=intent.deal_id,
                ))
            else:
                scaled.append(intent)
        intents = scaled
        gross = ceiling

    # The intents survive a halt deliberately. Withholding them as well
    # would leave nobody able to see what the pass would have done, and
    # "here is what I wanted and here is why I did not" is the useful
    # refusal -- ``actionable`` is what withholds the orders.
    return BridgeReport(
        intents=tuple(intents), balance=balance, gross_target=gross,
        halted=bool(halt_reason), halt_reason=halt_reason,
        unexplained=unexplained, missing=missing,
    )
