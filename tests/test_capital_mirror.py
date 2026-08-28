"""The account mirrors the shadow book rather than re-deciding from signals.

Both used to read the same signal frame and each work out what to hold --
two decision paths that can disagree. The whole value of running them side
by side is that a difference between them means the *simulation* was wrong,
not that they decided differently, and that only holds if one of them
decides.

M363 the account deriving its own view of the signals again
M364 membership taken from the book but an arm mixed in
M365 a position with no recorded size sized to zero and read as a refusal
M366 an unreadable book letting the account trade on nothing
"""

from __future__ import annotations

import pytest

from cfdinvest.advisor import papertrade
from cfdinvest.schedule import app


def book_row(ticker, cohort="c35", size=0.7, **kw):
    return {
        "event": "open", "at": "2026-08-28T14:00:00+00:00", "ticker": ticker,
        "cohort": cohort, "direction": kw.pop("direction", "long"),
        "quantity": 10.0, "price": 100.0, "notional_gbp": 1000.0,
        "venue": "capital-demo",
        "confidence": 0.8, "signal_size": size, **kw,
    }


def test_the_account_takes_its_positions_from_the_book(tmp_path, monkeypatch):
    """M363: a second decision path to keep in step.

    The book has already applied the entry bar, the cooldown, the earnings
    blackout, the time stop and the last look. Re-deriving any of it in the
    account means the two can hold different things for reasons that are
    nobody's mistake.
    """
    path = tmp_path / "book.jsonl"
    monkeypatch.setattr(papertrade, "_path", lambda config=None, account=None: path)
    papertrade.record([
        papertrade.PaperTrade(**{
            k: v for k, v in book_row("AAPL").items() if k != "event"
        }, event="open"),
    ])

    rows = app._mirror_rows(None, None, "c35")
    assert [r["ticker"] for r in rows] == ["AAPL"]
    assert rows[0]["direction"] == "long"
    assert rows[0]["size"] == pytest.approx(0.7), (
        "the engine's size was not carried, so the account cannot size itself"
    )
    assert rows[0]["confidence"] == pytest.approx(0.8)


def test_only_one_arm_is_mirrored(tmp_path, monkeypatch):
    """M364: four books, one account.

    An account can hold one of four parallel simulations, and mixing them
    would make the comparison meaningless in both directions.
    """
    path = tmp_path / "book.jsonl"
    monkeypatch.setattr(papertrade, "_path", lambda config=None, account=None: path)
    for ticker, arm in (("AAPL", "c35"), ("MSFT", "c55")):
        papertrade.record([papertrade.PaperTrade(**{
            k: v for k, v in book_row(ticker, cohort=arm).items() if k != "event"
        }, event="open")])

    assert [r["ticker"] for r in app._mirror_rows(None, None, "c35")] == ["AAPL"]
    assert [r["ticker"] for r in app._mirror_rows(None, None, "c55")] == ["MSFT"]


def test_a_position_with_no_recorded_size_is_refused_by_name():
    """M365: a gap in the record read as the strategy declining.

    A row with no size is not a zero. Sized to zero it becomes "no signal",
    which is a statement about the strategy rather than about the book, and
    the account would close a position the book still holds.
    """
    from cfdinvest.broker.capital import bridge

    class Market:
        epic = "AAPL"
        bid = 100.0
        offer = 100.2
        min_deal_size = 0.1
        ok = True

    report = bridge.plan_orders(
        [{"ticker": "AAPL", "direction": "long", "size": None, "confidence": 0.9}],
        {"AAPL": Market()},
        {},
        balance=5000.0,
        limits=type("L", (), {"max_exposure_per_instrument_gbp": 2000.0,
                              "max_total_exposure_gbp": 2000.0})(),
        exposure_per_unit_size=1.0,
        rebalance_threshold=0.25,
        session_open={"AAPL": True},
        recheck=False,
    )

    reasons = [i.reason for i in report.intents]
    assert any("no recorded size" in (r or "") for r in reasons), reasons
    assert not any("no signal" in (r or "") for r in reasons), (
        "a missing size was reported as the strategy declining"
    )
    assert report.actionable == (), "an unsized row produced an order"


def test_an_unreadable_book_mirrors_nothing(monkeypatch):
    """M366: the account trading on a book it could not read."""
    def explode(*args, **kwargs):
        raise OSError("no such file")

    monkeypatch.setattr(papertrade, "load_book", explode)
    assert app._mirror_rows(None, None, "c35") == []


def test_the_trade_job_no_longer_reads_the_signal_frame():
    """The seam, asserted against the source.

    Reading both would leave the old path in place beside the new one, and
    whichever ran last would win silently.
    """
    import inspect

    source = inspect.getsource(app._command_trade_capital)
    assert "_mirror_rows(" in source
    assert 'load_frame("signals"' not in source, (
        "the account still derives its own view from the signal frame"
    )
