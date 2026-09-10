"""What the bridge decided and what the broker did about it.

This is the record reconciliation compares the account against. Without it
there is no way to tell a position the app opened from one that arrived some
other way — a hand-placed trade, a probe, a stop the venue filled — and
"there is a position in NFLX" says nothing about whether that is expected.

**Append-only.** Every pass writes what it decided, including the refusals,
and nothing is ever rewritten. The refusals are the more interesting half:
they are the only evidence that a limit did anything, and a store that kept
only the orders would show a system that never declined.

**A plan and a result are separate rows.** The plan is what the bridge
intended; the result is what the broker said. They differ exactly when
something went wrong, and a store that merged them would lose the case it
exists to catch — an order placed and not confirmed, which is the one state
that needs a person.

**Open positions are derived, never tracked separately.** ``open_orders``
replays the rows the same way the confirm layer replays confirmations: last
event per instrument wins. A second copy of "what is open" would be a second
thing to keep true, and the day the two disagreed neither would be trusted.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)

__all__ = [
    "completed_trips",
    "LAST_LOOK_REASONS",
    "last_look_refusals_today",
    "refusal_summary",
    "ORDER_DIR",
    "record_plan",
    "record_result",
    "record_departure",
    "load_orders",
    "open_orders",
]

ORDER_DIR = "orders"


def _path(config=None, account: str | None = None) -> Path:
    from cfdinvest.persist.storage import account_root

    return Path(account_root(config, account)) / ORDER_DIR / "orders.jsonl"


def _append(rows, config=None, account: str | None = None) -> int:
    written = 0
    try:
        path = _path(config, account)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")
                written += 1
    except Exception as error:  # noqa: BLE001 - a lost record is not an outage
        logger.warning("Could not record orders: %s", error)
    return written


def record_plan(report, config=None, account: str | None = None, *, now=None) -> int:
    """One row per intent, refusals included. Never raises."""
    rows = []
    for intent in getattr(report, "intents", ()) or ():
        rows.append({
            "at": str(now or ""),
            "kind": "plan",
            "ticker": intent.ticker,
            "epic": intent.epic,
            "action": intent.action,
            "direction": intent.direction,
            "held_units": intent.held_units,
            "target_units": intent.target_units,
            "price": intent.price,
            "notional": intent.notional,
            "reason": intent.reason,
            "halted": bool(getattr(report, "halted", False)),
            "halt_reason": getattr(report, "halt_reason", ""),
            "balance": getattr(report, "balance", None),
        })
    return _append(rows, config, account)


def record_departure(
    epic: str, prior, config=None, account: str | None = None, *,
    now=None, reason: str = "",
) -> int:
    """Record that a position the app believed it held is no longer there.

    The account is the truth about what is held; the record is only this
    app's belief about it. When they disagree in this direction -- the
    record says held, the broker says no -- the position is gone, and the
    record has to learn that or it will disagree forever.

    **Why this exists.** On 2026-09-10 TGT was closed outside the dealing
    path -- an opposing BUY placed through the API on the owner's
    instruction to close it, rather than through this app's own dealing
    pass. No result row was written, so `open_orders` went on reporting TGT
    held, `reconcile` reported it missing, and `plan_orders` halted **every
    dealing pass for the rest of the session**.

    Two causes produce that signature and both are benign: the owner's
    stated operating mode includes closing by hand in an emergency, and an
    out-of-band close like the one above. Neither leaves a record, and
    nothing in the app could clear the jam either way.

    **The exit price is not known and is not invented.** This app did not
    place the close, so it has no fill. `level` stays `None`, which
    `trips()` already reads as "cannot be paired" and skips -- an unknown
    exit yields no P&L rather than a P&L computed from a zero. The row
    carries `reconciled: True` so it can never be mistaken for a close this
    app placed, and so a later reader can count them: a rising number of
    departures is an order path reporting fills it is not getting, which is
    a different and worse problem than a hand closure.

    Deliberately NOT used for the other direction. A position on the account
    the app never opened is money at risk nobody chose, and the bridge still
    refuses to act at all on that -- healing it would be adopting a position
    rather than reconciling a record.
    """
    size = direction = deal_id = None
    try:
        size = (prior or {}).get("size")
        direction = (prior or {}).get("direction")
        deal_id = (prior or {}).get("deal_id")
    except Exception:  # noqa: BLE001 - a malformed record still needs closing
        pass

    return _append([{
        "at": str(now or ""),
        "kind": "result",
        "ticker": str(epic),
        "epic": str(epic),
        "action": "close",
        # The broker's own word for a closed position, so `_confirmed` reads
        # this row from the observation like every other.
        "status": "CLOSED",
        "ok": True,
        # Absent, not zero. This app did not see the fill.
        "level": None,
        "units": size,
        "direction": direction,
        "position_units": 0.0,
        "position_direction": "",
        "deal_id": deal_id,
        "reconciled": True,
        "reason": reason or "absent from the account; recorded to reconcile",
    }], config, account)


def _resulting_position(intent, result):
    """The position after an order: its magnitude and side.

    Returns ``(None, "")`` when the fill size is unreadable, so an unknown
    result stays unknown rather than being recorded as a position of zero --
    which would read as "closed" and is the dangerous way to be wrong.
    """
    action = str(getattr(intent, "action", "") or "")
    if action == "close":
        return 0.0, ""
    filled = getattr(result, "size", None)
    try:
        size = abs(float(filled))
    except (TypeError, ValueError):
        return None, ""
    try:
        held = float(getattr(intent, "held_units", 0.0) or 0.0)
        target = float(getattr(intent, "target_units", 0.0) or 0.0)
    except (TypeError, ValueError):
        return None, ""

    if action == "resize":
        # The order moved the position by `size` in the direction of travel.
        # Using the filled size rather than the target means a partial fill
        # records what actually happened.
        resulting = held + (size if target > held else -size)
    else:
        resulting = size if target >= 0 else -size
    return abs(resulting), ("SELL" if resulting < 0 else "BUY")


def record_result(
    intent, result, config=None, account: str | None = None, *, now=None
) -> int:
    """What the broker said about one intent. Never raises.

    The ``ok`` flag is the broker's verdict, not the app's hope: a deal
    reference alone is not a fill, so an accepted-but-unconfirmed order lands
    here with ``ok`` false and its error intact.
    """
    # What the ORDER was, and separately what the POSITION now is. They are
    # the same thing for an open and different for a resize, and recording
    # only the order was wrong in both fields.
    #
    # The account is in netting mode ("trades in different direction will
    # close existing trade first"), confirmed by the owner on 2026-09-03, so
    # a reduction goes as an opposing order: trimming a long sends SELL for
    # the DELTA. Recorded naively that made the record say the position was
    # now SHORT and 40 units, when the account held 60 units LONG. Wrong in
    # size and inverted in direction, and `reconcile` could not catch it
    # because it compares which epics are held, not how much.
    #
    # Latent: rebalance_threshold = 3.0 disables resizing, so no resize has
    # ever been recorded. Fixed here rather than when it is enabled, because
    # the record is what reconciliation trusts.
    position_units, position_direction = _resulting_position(intent, result)
    return _append([{
        "at": str(now or ""),
        "kind": "result",
        "ticker": intent.ticker,
        "epic": intent.epic,
        "action": intent.action,
        "direction": getattr(result, "direction", "") or intent.direction,
        "units": getattr(result, "size", None),
        #: The position after this order, not the order itself. Preferred by
        #: `open_orders`; absent on rows written before 2026-09-03, where the
        #: order and the position agreed anyway.
        "position_units": position_units,
        "position_direction": position_direction,
        "level": getattr(result, "level", None),
        "deal_id": getattr(result, "deal_id", "") or intent.deal_id,
        "reference": getattr(result, "reference", ""),
        "status": getattr(result, "status", ""),
        "ok": bool(getattr(result, "ok", False)),
        "error": getattr(result, "error", ""),
        # The broker's reason on success is empty, so falling through to the
        # intent's is what makes a close self-explaining. Recording the
        # broker's alone is why every row read "(none recorded)".
        "reason": (
            str(getattr(result, "reason", "") or "").strip()
            or str(getattr(intent, "reason", "") or "").strip()
        ),
    }], config, account)


def load_orders(config=None, account: str | None = None) -> list[dict]:
    """Every row, oldest first. Never raises; skips a truncated line."""
    rows: list[dict] = []
    try:
        path = _path(config, account)
        if not path.is_file():
            return rows
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("Skipping a malformed order row in %s", path)
    except OSError as error:
        logger.warning("Order rows unreadable at %s: %s", path, error)
    return rows


def completed_trips(config=None, account: str | None = None) -> list[dict]:
    """Round trips reconstructed from the order store: open paired to close.

    The store records orders, not trips, and the question "how is it doing"
    is asked of trips. Pairing them here rather than at each call site keeps
    one answer to what counts as a completed trade.

    P&L is computed from the broker's own confirmed fill levels rather than
    from the account balance, because the balance also moves with unrealised
    marks, financing and deposits. It is in the INSTRUMENT's currency and is
    deliberately not converted: nothing here needs the absolute figure, only
    the sign, and inventing an exchange rate to make it look like sterling
    would put a second FX owner in the project.

    FIFO within an instrument. Never raises.
    """
    try:
        rows = [
            row for row in load_orders(config, account)
            if row.get("kind") == "result" and _confirmed(row)
        ]
    except Exception as error:  # noqa: BLE001
        logger.warning("Could not read the order store for trips: %s", error)
        return []
    rows.sort(key=lambda r: str(r.get("at") or ""))

    waiting: dict[str, list[dict]] = {}
    trips: list[dict] = []
    for row in rows:
        epic = str(row.get("epic") or "")
        if not epic:
            continue
        action = str(row.get("action") or "")
        if action == "open":
            waiting.setdefault(epic, []).append(row)
        elif action == "close" and waiting.get(epic):
            opened = waiting[epic].pop(0)
            try:
                units = float(opened.get("units") or 0.0)
                entry = float(opened.get("level") or 0.0)
                exit_ = float(row.get("level") or 0.0)
            except (TypeError, ValueError):
                continue
            if not units or not entry or not exit_:
                continue
            side = 1.0 if str(opened.get("direction") or "").upper() == "BUY" else -1.0
            trips.append({
                "ticker": epic,
                "opened_at": opened.get("at"),
                "closed_at": row.get("at"),
                "units": units,
                "entry": entry,
                "exit": exit_,
                "pnl": (exit_ - entry) * units * side,
                # Why the engine let it go, where the record has it.
                "reason": str(row.get("reason") or ""),
            })
    return trips


def _confirmed(row: Mapping[str, Any]) -> bool:
    """Did the broker confirm this row did what it was asked to do?

    Read from the *observed* facts the row carries -- the broker's own
    ``status`` and the error text -- rather than from the stored ``ok``
    boolean beside them.

    ``ok`` is derived, and a derived field persisted next to the facts it
    came from is a hostage to the version of the code that wrote it. That is
    not hypothetical. On 2026-08-31 ``DealResult.ok`` read ``status ==
    "OPEN" and level is not None``, which is correct for an open and false
    for every successful close, so eight closes were written with
    ``ok: false`` beside ``status: "CLOSED"`` and an empty error. This
    function skipped them, the record went on believing eight closed
    positions were still held, reconciliation found them missing from the
    account, and the bridge halted and stopped opening anything.

    So the bug's worst consequence was not the wrong mail it sent. It was
    that the record it poisoned is the gate on all future dealing, and
    fixing ``ok`` could not unpoison rows already on disk. Deriving from the
    observation at read time is what makes an old record survive a change in
    the code that wrote it.

    ``ok`` is still honoured for a row carrying no status at all, which is
    the only case where the observation is genuinely absent rather than
    merely disagreeing.
    """
    if row.get("error"):
        return False
    status = str(row.get("status") or "").upper()
    if not status:
        return bool(row.get("ok"))
    if str(row.get("action") or "") == "close":
        return status == "CLOSED"
    # An open needs a fill price. A deal reference the broker never turned
    # into a position is not a holding, and recording it as one puts the
    # record ahead of the account -- which is the failure this whole module
    # is shaped around.
    return status == "OPEN" and row.get("level") is not None


def open_orders(config=None, account: str | None = None) -> dict[str, dict[str, Any]]:
    """What this app believes it holds, keyed by epic. Never raises.

    Only *confirmed* results count. An order the broker accepted and never
    confirmed is not a position — believing otherwise would put the record
    ahead of the account, and reconciliation would then report the account as
    missing something that was never there.
    """
    live: dict[str, dict[str, Any]] = {}
    for row in load_orders(config, account):
        if row.get("kind") != "result" or not _confirmed(row):
            continue
        epic = str(row.get("epic") or "")
        if not epic:
            continue
        if row.get("action") == "close":
            live.pop(epic, None)
        else:
            # The position, not the order. `units` is what was SENT, which
            # for a resize is the delta and for a reduction carries the
            # opposing side; `position_units` is what the account now holds.
            # Older rows have neither, and for them the two agreed.
            size = row.get("position_units")
            direction = row.get("position_direction") or row.get("direction")
            if size is None:
                size, direction = row.get("units"), row.get("direction")
            live[epic] = {
                "size": size,
                "direction": direction,
                "deal_id": row.get("deal_id"),
                "level": row.get("level"),
            }
    return live

#: The refusals that come from the last look, as opposed to a limit.
#:
#: One owner. ``advisor.livecheck.holds`` produces these strings and this is
#: the only place that says which of the bridge's refusals mean "the decision
#: no longer stands" rather than "a cap was in the way". A second copy would
#: drift, and the day it drifted the page would report a different gate from
#: the email.
#:
#: ``not re-checked`` belongs here and is the important one: it is what the
#: gate returns when it could not compute an opinion at all, and the bridge
#: treats it as a refusal because unchecked is not approved. A run where it
#: dominates is a broken check, not a selective one, and the two must be
#: distinguishable on sight.
LAST_LOOK_REASONS = frozenset({
    "no longer a signal",
    "no longer clears the bar",
    "direction reversed since the signal",
    "not re-checked",
})


def refusal_summary(
    config=None, account: str | None = None, *, days: int = 7, now=None
) -> dict:
    """What the last look refused, collapsed to instrument-days.

    The dealing pass runs every fifteen minutes and records one row per
    intent, so a name refused at 14:00, again at 14:15 and opened at 14:30
    is three rows describing one decision. Counting rows triples the refusal
    rate and makes a gate that fired once look like a gate firing constantly.
    Collapsed here, once, so the page and the mail cannot disagree about it.

    Absence is named rather than inferred. ``checked`` is False when there is
    no order record at all -- a system that has never dealt and a system whose
    gate never fires are different statements, and only one of them is
    reassuring. A returned zero means the gate ran and refused nothing.
    """
    rows = load_orders(config, account)
    summary = {
        "checked": bool(rows),
        "days": int(days),
        "considered": 0,
        "blocked": 0,
        "opened_anyway": 0,
        "reasons": {},
        "recent": [],
    }
    if not rows:
        return summary

    cutoff = None
    if days and days > 0:
        try:
            import pandas as pd

            end = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="UTC")
            if end.tz is None:
                end = end.tz_localize("UTC")
            cutoff = (end - pd.Timedelta(days=int(days))).isoformat()
        except Exception:  # noqa: BLE001 - a bad clock must not blank the panel
            cutoff = None

    opened: set[tuple[str, str]] = set()
    refused: dict[tuple[str, str], set[str]] = {}
    for row in rows:
        if str(row.get("kind") or "") != "plan":
            continue
        stamp = str(row.get("at") or "")
        if cutoff and stamp and stamp < cutoff:
            continue
        ticker = str(row.get("ticker") or "")
        if not ticker:
            continue
        key = (stamp[:10], ticker)
        action = str(row.get("action") or "")
        if action == "open":
            opened.add(key)
        elif action == "refused":
            why = str(row.get("reason") or "")
            if why in LAST_LOOK_REASONS:
                refused.setdefault(key, set()).add(why)

    blocked = set(refused) - opened
    summary["considered"] = len(set(opened) | set(refused))
    summary["blocked"] = len(blocked)
    summary["opened_anyway"] = len(set(refused) & opened)
    counts: dict[str, int] = {}
    for key, reasons in refused.items():
        for why in reasons:
            counts[why] = counts.get(why, 0) + 1
    summary["reasons"] = dict(sorted(counts.items(), key=lambda kv: -kv[1]))
    summary["recent"] = sorted(
        (
            {
                "day": day, "ticker": ticker,
                "reason": ", ".join(sorted(reasons)),
                "opened_anyway": (day, ticker) in opened,
            }
            for (day, ticker), reasons in refused.items()
        ),
        key=lambda entry: (entry["day"], entry["ticker"]),
        reverse=True,
    )
    return summary


def last_look_refusals_today(
    intents, config=None, account: str | None = None, *, now=None
) -> list:
    """Of this pass's last-look refusals, the ones not already reported today.

    The dealing mail is suppressed when a pass places nothing, which is right
    for a quiet pass and wrong for a pass where the gate refused everything:
    the account decided not to trade, and that is the decision worth hearing
    about. Reporting it every fifteen minutes would be noise, so a name is
    mailed the first time it is refused on a given day and not again.

    Reads the store rather than holding state, because the store is already
    the record and a second one would be a second thing to keep true.
    """
    fresh = [
        intent for intent in intents or ()
        if str(getattr(intent, "reason", "") or "") in LAST_LOOK_REASONS
    ]
    if not fresh:
        return []
    try:
        import pandas as pd

        stamp = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="UTC")
        today = str(stamp.date())
    except Exception:  # noqa: BLE001 - an unusable clock reports everything
        return fresh

    seen: set[tuple[str, str]] = set()
    for row in load_orders(config, account):
        if str(row.get("kind") or "") != "plan":
            continue
        if str(row.get("at") or "")[:10] != today:
            continue
        if str(row.get("action") or "") != "refused":
            continue
        why = str(row.get("reason") or "")
        if why in LAST_LOOK_REASONS:
            seen.add((str(row.get("ticker") or ""), why))
    return [
        intent for intent in fresh
        if (str(intent.ticker), str(intent.reason or "")) not in seen
    ]
