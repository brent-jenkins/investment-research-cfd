"""The shadow book beside what the account actually did.

The two are meant to hold the same positions: the account mirrors the book
and both are priced on the same venue, so a difference is no longer a
difference of opinion. What is left is how wrong the simulation's fills are
-- which nothing else measures.

M358 the account mirroring a stale book, acting a cycle late
M359 a book position the account silently failed to follow, unnamed
M360 refusals dropped, so book_only rows lose their explanation
M361 a fill difference claimed with nothing to measure it from
M362 the join editing either store to make them agree
"""

from __future__ import annotations

import pytest

from cfdinvest.persist import reconcile


def book_row(ticker, quantity=10.0, price=100.0, event="open", cohort="c35", **kw):
    return {
        "event": event, "at": kw.pop("at", "2026-08-28T14:00:00+00:00"),
        "ticker": ticker, "cohort": cohort, "direction": kw.pop("direction", "long"),
        "quantity": quantity, "price": price, "venue": "capital-demo",
        "live_armed": True, **kw,
    }


def result_row(ticker, ok=True, units=9.0, level=100.4, action="open", **kw):
    return {
        "kind": "result", "ticker": ticker, "epic": ticker, "action": action,
        "ok": ok, "units": units, "level": level, "direction": "BUY",
        "deal_id": f"D-{ticker}", **kw,
    }


def test_a_matched_position_carries_both_prices():
    """The point of the join: the fill difference is readable directly."""
    out = reconcile.compare([book_row("AAPL")], [result_row("AAPL")], cohort="c35")

    assert len(out["matched"]) == 1
    row = out["matched"][0]
    assert row["book_price"] == 100.0
    assert row["account_price"] == 100.4
    assert row["book_quantity"] == 10.0
    assert row["account_quantity"] == 9.0
    assert out["book_only"] == [] and out["account_only"] == []


def test_a_book_position_the_account_refused_carries_its_reason():
    """M360: the explanation dropped, leaving an unexplained gap.

    ``open_orders`` keeps only confirmed positions, which is right for "what
    do we hold" and wrong here -- a refusal is precisely what this join
    exists to surface.
    """
    orders = [
        result_row("INTC", ok=False, action="refused",
                   reason="below the minimum deal size"),
    ]
    out = reconcile.compare([book_row("INTC")], orders, cohort="c35")

    assert len(out["book_only"]) == 1
    assert out["book_only"][0]["reason"] == "below the minimum deal size"
    assert out["unexplained_book_only"] == [], (
        "a refused position was reported as unexplained"
    )


def test_a_book_position_with_no_order_row_at_all_is_named_unexplained():
    """M359: the account silently failing to follow.

    A refusal is a decision and is fine. Nothing at all means the account
    never considered the instrument, and that is the case needing a person.
    """
    out = reconcile.compare([book_row("NKE")], [], cohort="c35")

    assert len(out["book_only"]) == 1
    assert out["book_only"][0]["reason"] is None
    assert [r["ticker"] for r in out["unexplained_book_only"]] == ["NKE"], (
        "a position the account never considered was not flagged"
    )


def test_a_position_only_the_account_holds_is_reported_not_corrected():
    out = reconcile.compare([], [result_row("TSLA")], cohort="c35")

    assert [r["ticker"] for r in out["account_only"]] == ["TSLA"]
    assert out["matched"] == [] and out["book_only"] == []


def test_a_closed_order_leaves_no_account_position():
    orders = [result_row("AAPL"), result_row("AAPL", action="close")]
    out = reconcile.compare([book_row("AAPL")], orders, cohort="c35")

    assert out["matched"] == []
    assert [r["ticker"] for r in out["book_only"]] == ["AAPL"]


def test_an_unconfirmed_order_is_not_an_account_position():
    """The record must not run ahead of the account."""
    out = reconcile.compare(
        [book_row("AAPL")],
        [result_row("AAPL", ok=False, error="PLACED BUT UNCONFIRMED")],
        cohort="c35",
    )
    assert out["matched"] == []
    assert out["book_only"][0]["reason"] == "PLACED BUT UNCONFIRMED"


def test_only_the_named_arm_is_compared():
    """Four arms run side by side and an account holds one of them."""
    book = [book_row("AAPL", cohort="c35"), book_row("MSFT", cohort="c55")]
    out = reconcile.compare(book, [result_row("AAPL")], cohort="c35")

    assert [r["ticker"] for r in out["matched"]] == ["AAPL"]
    assert out["book_only"] == [], "another arm's position leaked into the join"


def test_the_summary_withholds_a_median_it_cannot_compute():
    """M361: a fill difference claimed with nothing behind it."""
    empty = reconcile.reconciliation_summary({"matched": []})
    assert empty["median_fill_difference_bp"] is None
    assert empty["priced_pairs"] == 0

    unpriced = reconcile.reconciliation_summary(
        {"matched": [{"book_price": 100.0, "account_price": None}]}
    )
    assert unpriced["matched"] == 1
    assert unpriced["priced_pairs"] == 0
    assert unpriced["median_fill_difference_bp"] is None, (
        "a median was reported from a pair that never priced"
    )


def test_the_summary_measures_the_fill_difference_in_basis_points():
    out = reconcile.compare(
        [book_row("AAPL", price=100.0), book_row("MSFT", price=200.0)],
        [result_row("AAPL", level=100.1), result_row("MSFT", level=200.6)],
        cohort="c35",
    )
    summary = reconcile.reconciliation_summary(out)

    assert summary["matched"] == 2 and summary["priced_pairs"] == 2
    # 10bp and 30bp; the median of two is their mean.
    assert summary["median_fill_difference_bp"] == pytest.approx(20.0, rel=1e-9)


def test_the_join_writes_to_neither_store():
    """M362: a reader that edits one store to agree with the other.

    Both are append-only records of moments that do not come back. The join
    is a view.
    """
    import ast
    import inspect

    source = inspect.getsource(reconcile)
    for forbidden in ("record(", "record_plan", "record_result", ".write_text",
                      "open(", "_append"):
        assert forbidden not in source, f"the join writes ({forbidden})"

    # And it holds no import that could deal.
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert "dealing" not in node.module


def test_unreadable_stores_produce_an_empty_view_not_an_exception():
    class Exploding:
        def __iter__(self):
            raise OSError("no such file")

    out = reconcile.compare(Exploding(), Exploding(), cohort="c35")
    assert out["matched"] == [] and out["book_only"] == []
    assert out["account_only"] == []
