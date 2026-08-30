"""The shadow book beside what the account actually did.

Two append-only stores, one writer each, joined for reading. The book says
what the simulation decided and at what modelled cost; the order store says
what the broker was asked for and what it answered. Neither is edited to
agree with the other, which is why the join has to happen here rather than
by adding a field to one of them.

What the difference means
-------------------------
The two are meant to hold the same positions. Since 2026-08-28 the account
mirrors the book rather than deriving its own view, and both are priced on
the same venue -- so a difference is no longer a difference of *opinion*.
What is left is the thing nothing else measures: how wrong the simulation's
fills and costs are.

Three kinds of row come out of it, and the last two are the interesting ones:

``matched``
    The book holds it and the account holds it. The pair carries both
    prices, so the fill difference can be read off directly.
``book_only``
    The book holds it and the account does not. Every one of these has a
    reason in the order store -- refused below a minimum, outside the
    session, rejected by the broker -- and a `book_only` with no such row is
    the case worth chasing: the account silently failed to follow.
``account_only``
    The account holds something the book does not. This should not happen
    while the account mirrors the book, and the bridge halts on it. It is
    reported rather than corrected: acting on an account you have just shown
    you do not understand is how one bad read becomes several bad trades.

Nothing here writes. It reads two stores and returns a view.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Mapping

logger = logging.getLogger(__name__)

__all__ = ["compare", "reconciliation_summary"]


def _last_result_by_ticker(rows: Iterable[Mapping[str, Any]]) -> dict[str, dict]:
    """The most recent order-store row per instrument, whatever it said.

    Includes refusals and failures deliberately. The store's replay
    (``open_orders``) keeps only confirmed positions, which is right for
    "what do we hold" and wrong here: a refusal is the explanation this
    join exists to surface.
    """
    latest: dict[str, dict] = {}
    for row in rows or ():
        ticker = str(row.get("ticker") or "")
        if ticker:
            latest[ticker] = dict(row)
    return latest


def compare(
    book: Iterable[Mapping[str, Any]] | None = None,
    orders: Iterable[Mapping[str, Any]] | None = None,
    *,
    config=None,
    account: str | None = None,
) -> dict[str, Any]:
    """Join the shadow book against the order store. Never raises.

    Both stores are read when not supplied, so a caller with neither gets
    the live picture and a caller with fixtures gets a pure function.
    """
    from cfdinvest.persist import intent
    from cfdinvest.persist import orders as order_store

    try:
        rows = list(book) if book is not None else intent.intended_positions(
            config, account
        )
    except Exception as error:  # noqa: BLE001
        logger.warning("The engine's intent is unreadable: %s", error)
        rows = []
    try:
        order_rows = (
            list(orders) if orders is not None
            else order_store.load_orders(config, account)
        )
    except Exception as error:  # noqa: BLE001
        logger.warning("Order store unreadable: %s", error)
        order_rows = []

    # What the engine intends, keyed by instrument. Previously the shadow
    # book's open positions; the book applied a different rule set, so a
    # reconciliation against it compared orders to a strategy nobody chose.
    held = {str(r["ticker"]): r for r in rows if r.get("ticker")}
    live = order_store.open_orders(config, account) if orders is None else _replay(order_rows)
    explanations = _last_result_by_ticker(order_rows)

    matched, book_only, account_only = [], [], []

    for ticker, position in sorted(held.items()):
        account_side = live.get(ticker)
        if account_side is None:
            last = explanations.get(ticker, {})
            book_only.append({
                "ticker": ticker,
                "direction": position.get("direction"),
                "book_quantity": position.get("quantity"),
                "book_price": position.get("price"),
                "venue": position.get("venue"),
                "live_armed": position.get("live_armed"),
                # Why the account does not hold it. Absent is the finding:
                # it means nothing in the order store even mentions the
                # instrument, so the account did not refuse it -- it never
                # considered it.
                "reason": last.get("reason") or last.get("error") or None,
                "last_action": last.get("action"),
            })
            continue
        matched.append({
            "ticker": ticker,
            "direction": position.get("direction"),
            "book_quantity": position.get("quantity"),
            "account_quantity": account_side.get("size"),
            "book_price": position.get("price"),
            "account_price": account_side.get("level"),
            "venue": position.get("venue"),
            "deal_id": account_side.get("deal_id"),
        })

    for ticker, side in sorted(live.items()):
        if ticker not in held:
            account_only.append({
                "ticker": ticker,
                "direction": side.get("direction"),
                "account_quantity": side.get("size"),
                "account_price": side.get("level"),
                "deal_id": side.get("deal_id"),
            })

    return {
        "matched": matched,
        "book_only": book_only,
        "account_only": account_only,
        # Named rather than derived by the caller, because "the account
        # silently failed to follow" is the one case here that needs a
        # person and it must not have to be spotted.
        "unexplained_book_only": [
            row for row in book_only if not row.get("reason")
        ],
    }


def _replay(rows: Iterable[Mapping[str, Any]]) -> dict[str, dict]:
    """``open_orders``' replay over supplied rows, for a pure call."""
    live: dict[str, dict] = {}
    for row in rows or ():
        if row.get("kind") != "result" or not row.get("ok"):
            continue
        ticker = str(row.get("ticker") or "")
        if not ticker:
            continue
        if row.get("action") == "close":
            live.pop(ticker, None)
        else:
            live[ticker] = {
                "size": row.get("units"), "direction": row.get("direction"),
                "deal_id": row.get("deal_id"), "level": row.get("level"),
            }
    return live


def reconciliation_summary(comparison: Mapping[str, Any]) -> dict[str, Any]:
    """Counts and the median fill difference, or an explicit absence.

    The difference is reported in basis points of the book's price and only
    where both sides priced. Below a single pair no median is claimed --
    "not measured" and "measured and fine" are different statements.
    """
    matched = list(comparison.get("matched") or ())
    differences = []
    for row in matched:
        book_price, account_price = row.get("book_price"), row.get("account_price")
        try:
            book_price, account_price = float(book_price), float(account_price)
        except (TypeError, ValueError):
            continue
        if book_price > 0:
            differences.append((account_price - book_price) / book_price * 10_000.0)

    differences.sort()
    median = None
    if differences:
        middle = len(differences) // 2
        median = (
            differences[middle] if len(differences) % 2
            else (differences[middle - 1] + differences[middle]) / 2.0
        )

    return {
        "matched": len(matched),
        "book_only": len(comparison.get("book_only") or ()),
        "account_only": len(comparison.get("account_only") or ()),
        "unexplained": len(comparison.get("unexplained_book_only") or ()),
        "priced_pairs": len(differences),
        "median_fill_difference_bp": median,
    }
