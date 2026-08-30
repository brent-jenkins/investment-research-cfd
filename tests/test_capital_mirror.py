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


def test_a_name_that_left_the_traded_list_can_still_be_closed():
    """M-live: nine positions stranded on a funded demo account.

    Narrowing the universe to eight names left eleven book positions
    outside it. Market detail was fetched for ``market.tickers`` only, so
    the bridge found no price for the other nine, refused them, and could
    never close them. The run-out rule worked -- they were not liquidated
    on departure -- but the other half of it did not: they could not exit
    on their own merits either, while financing accrued daily.

    The property: what may be *opened* comes from the traded list; what may
    be *closed* is whatever is actually held.
    """
    class Position:
        def __init__(self, epic):
            self.epic = epic

    traded = ["MU", "AMD"]
    book = [{"ticker": "MU"}, {"ticker": "GOOGL"}]
    held = [Position("GOOGL"), Position("XOM")]

    priced = app._dealing_tickers(traded, book, held)

    assert set(priced) == {"MU", "AMD", "GOOGL", "XOM"}
    for departed in ("GOOGL", "XOM"):
        assert departed in priced, (
            f"{departed} is held but would not be priced, so nothing could "
            "ever close it"
        )
    # The traded list is still fully covered -- this widens, never narrows.
    assert set(traded) <= set(priced)


def test_the_dealing_set_tolerates_an_empty_book_and_no_positions():
    """Absence named, not inferred: no book and no positions is the traded
    list exactly, not an empty set that would stop all dealing."""
    assert app._dealing_tickers(["MU"], [], []) == ["MU"]
    assert app._dealing_tickers(["MU"], None, None) == ["MU"]
    assert app._dealing_tickers([], [], []) == []


def test_market_detail_is_fetched_for_the_held_set_not_the_traded_list():
    """Structural: the fix is easy to undo by passing `tickers` again."""
    import inspect

    source = inspect.getsource(app._command_trade_capital)
    fetch = source[source.index("fetch_market_details"):]
    fetch = fetch[:fetch.index(")")]
    assert "dealing_tickers" in fetch, (
        "market detail is fetched for the traded list again -- a held name "
        "outside it becomes unclosable"
    )
    # Both calendar lookups, checked by absence rather than presence: an
    # assertion that `describe(dealing_tickers` appears is satisfied by
    # either call, so reverting one of them passed a mutation.
    assert "describe(tickers" not in source, (
        "a session calendar is built from the traded list, so a held name "
        "outside it is refused for a session state nobody looked up"
    )
    assert source.count("describe(dealing_tickers") == 2, (
        "expected both the session calendar and the opening-window calendar "
        "to cover the held set"
    )


def test_intraday_ingestion_covers_what_is_still_held():
    """M-live: a departing name's bars stopped arriving.

    The run-out rule keeps a departed instrument's signals alive so it can
    exit on its own terms, and the dealing path can now price it. Both read
    the lake. The intraday top-up fetched ``market.tickers`` alone, so the
    exit would have been decided on bars that stopped the day the name left
    the list -- a stale signal, which is worse than an absent one because
    nothing reports it as missing.
    """
    import inspect

    from cfdinvest.schedule import app as schedule_app

    source = inspect.getsource(schedule_app.run_daemon_job)
    block = source[source.index('elif name == "ingest-intraday"'):]
    block = block[:block.index('elif name == "run-intraday"')]
    assert "_with_held(" in block, (
        "the intraday top-up fetches only the traded list, so a held name "
        "that left it stops receiving bars and exits on stale data"
    )


def test_data_collection_outlives_universe_membership(tmp_path, monkeypatch):
    """A name that leaves the list keeps accruing history.

    The vendor serves what it serves today: a bar not fetched while the
    instrument was interesting is not recoverable later. So the intraday
    top-up covers everything ever selected, not merely what is held now --
    otherwise a name dropped in March and reselected in June is ranked on,
    and traded from, a series with a hole in the middle.
    """
    from cfdinvest.schedule import app as schedule_app
    from cfdinvest.universe import screen as screen_module

    rows = [
        {"as_of": "2026-03-01T00:00:00Z", "selected": ["OLD1", "OLD2", "MU"]},
        {"as_of": "2026-04-01T00:00:00Z", "selected": ["MU", "AMD"]},
    ]
    monkeypatch.setattr(screen_module, "load_screens",
                        lambda config=None, account=None: rows)

    ever = schedule_app._ever_selected(None, None)

    assert set(ever) == {"OLD1", "OLD2", "MU", "AMD"}
    assert ever[:3] == ["OLD1", "OLD2", "MU"], "recorded order was not preserved"
    assert len(ever) == len(set(ever)), "duplicates would be fetched twice"


def test_an_unreadable_history_does_not_stop_ingestion(monkeypatch):
    """Best-effort: collection is an enhancement, and losing it must not
    take the fetch of the current universe down with it."""
    from cfdinvest.schedule import app as schedule_app
    from cfdinvest.universe import screen as screen_module

    def explode(config=None, account=None):
        raise OSError("store gone")

    monkeypatch.setattr(screen_module, "load_screens", explode)
    assert schedule_app._ever_selected(None, None) == []


def test_the_intraday_job_fetches_the_history_not_just_the_holdings():
    """Structural: the realistic regression is someone simplifying this
    back to the traded list while tidying."""
    import inspect

    from cfdinvest.schedule import app as schedule_app

    source = inspect.getsource(schedule_app.run_daemon_job)
    block = source[source.index('elif name == "ingest-intraday"'):]
    block = block[:block.index('elif name == "run-intraday"')]
    assert "_ever_selected(" in block, (
        "the intraday top-up no longer covers departed names, so their "
        "history stops the day they leave the list"
    )
    assert "_with_held(" in block
