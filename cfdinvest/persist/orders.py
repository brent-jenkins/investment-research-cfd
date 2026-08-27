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
    "ORDER_DIR",
    "record_plan",
    "record_result",
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


def record_result(
    intent, result, config=None, account: str | None = None, *, now=None
) -> int:
    """What the broker said about one intent. Never raises.

    The ``ok`` flag is the broker's verdict, not the app's hope: a deal
    reference alone is not a fill, so an accepted-but-unconfirmed order lands
    here with ``ok`` false and its error intact.
    """
    return _append([{
        "at": str(now or ""),
        "kind": "result",
        "ticker": intent.ticker,
        "epic": intent.epic,
        "action": intent.action,
        "direction": getattr(result, "direction", "") or intent.direction,
        "units": getattr(result, "size", None),
        "level": getattr(result, "level", None),
        "deal_id": getattr(result, "deal_id", "") or intent.deal_id,
        "reference": getattr(result, "reference", ""),
        "status": getattr(result, "status", ""),
        "ok": bool(getattr(result, "ok", False)),
        "error": getattr(result, "error", ""),
        "reason": getattr(result, "reason", ""),
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


def open_orders(config=None, account: str | None = None) -> dict[str, dict[str, Any]]:
    """What this app believes it holds, keyed by epic. Never raises.

    Only *confirmed* results count. An order the broker accepted and never
    confirmed is not a position — believing otherwise would put the record
    ahead of the account, and reconciliation would then report the account as
    missing something that was never there.
    """
    live: dict[str, dict[str, Any]] = {}
    for row in load_orders(config, account):
        if row.get("kind") != "result" or not row.get("ok"):
            continue
        epic = str(row.get("epic") or "")
        if not epic:
            continue
        if row.get("action") == "close":
            live.pop(epic, None)
        else:
            live[epic] = {
                "size": row.get("units"),
                "direction": row.get("direction"),
                "deal_id": row.get("deal_id"),
                "level": row.get("level"),
            }
    return live
