"""The account takes its positions from the engine, and prices what it holds.

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

from cfdinvest.schedule import app


def book_row(ticker, cohort="c35", size=0.7, **kw):
    return {
        "event": "open", "at": "2026-08-28T14:00:00+00:00", "ticker": ticker,
        "cohort": cohort, "direction": kw.pop("direction", "long"),
        "quantity": 10.0, "price": 100.0, "notional_gbp": 1000.0,
        "venue": "capital-demo",
        "confidence": 0.8, "signal_size": size, **kw,
    }


def test_the_account_takes_its_positions_from_the_engine():
    """There is one decision path, and the account reads it rather than
    deciding again.

    This used to assert the account read the *shadow book* instead of the
    signal frame, on the reasoning that the book had already applied the
    rules. The reasoning was right and the premise was wrong: the book
    applies the time stop, cooldown, blackout and confidence gate, and none
    of the breakout gate, size hysteresis, decayed trim, leverage cap or
    concentration cap. So the second decision path existed anyway and the
    account was trading it -- 163.7% at -52.8% against the engine's 130.5%
    at -40.3%.

    Asserted functionally, on what reaches the bridge, rather than by
    grepping the source: the previous version of this test passed while the
    account traded a different strategy, because the string it looked for
    was still there and still wrong.
    """
    import pandas as pd

    positions = pd.DataFrame(
        {"AAA": [0.0, 4.0], "BBB": [0.0, -2.0], "GBPUSD=X": [0.0, 9.0]},
        index=pd.date_range("2026-08-28 14:00", periods=2, freq="1h", tz="UTC"),
    )
    equity = pd.DataFrame(
        {"equity": [100_000.0, 125_000.0]}, index=positions.index
    )

    class Cfg:
        strategy = type("S", (), {"exposure_per_unit_size": 4.0})()

    import cfdinvest.persist.readers as readers

    real_exec, real_frame = readers.load_execution_frame, readers.load_frame
    readers.load_execution_frame = lambda *a, **k: {
        "positions": positions, "equity": equity
    }
    readers.load_frame = lambda *a, **k: pd.DataFrame()
    try:
        rows = app._engine_rows(Cfg())
    finally:
        readers.load_execution_frame, readers.load_frame = real_exec, real_frame

    by_ticker = {r["ticker"]: r for r in rows}
    assert set(by_ticker) == {"AAA", "BBB"}, (
        "the reference currency pair was treated as tradeable"
    )
    assert by_ticker["AAA"]["direction"] == "long"
    assert by_ticker["BBB"]["direction"] == "short", "the short lost its sign"
    assert by_ticker["AAA"]["size"] is None, "sized before anything was priced"

    # share = units x price / (equity x exposure_per_unit_size)
    app._size_engine_rows(rows, {"AAA": 250.0, "BBB": 50.0})
    assert by_ticker["AAA"]["size"] == pytest.approx(
        4.0 * 250.0 / (125_000.0 * 4.0)
    )
    assert by_ticker["BBB"]["size"] == pytest.approx(
        2.0 * 50.0 / (125_000.0 * 4.0)
    )


def test_an_unpriced_instrument_is_left_unsized_rather_than_guessed():
    """"Not priced" and "priced at nothing" are different statements, and the
    bridge already refuses an unsized row by name."""
    rows = [{"ticker": "AAA", "units": 3.0, "equity": 100_000.0,
             "exposure_per_unit_size": 4.0, "size": None}]
    app._size_engine_rows(rows, {})
    assert rows[0]["size"] is None, "an unpriced position was given a size"


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


# --------------------------------------------------------------------------- #
# Capital's feed carries hours yfinance's does not
# --------------------------------------------------------------------------- #
def test_capital_bars_are_restricted_to_the_session_when_loaded():
    """M-live: the density trap, closed at the boundary.

    Capital quotes from 04:00 New York, so its raw feed carries pre-market
    and after-hours bars: 11.5 a day against yfinance's 6.9. Lookbacks in
    this engine are counted in *bars*, so handing it the raw feed rescales
    every window at once -- trend averages, volatility windows, the breakout
    lookback, the time stop. Nothing fails. The strategy simply becomes a
    different one while every test still passes.

    Filtered once at the repository rather than by each caller, because
    every research sweep this weekend filtered by hand and the production
    path did not filter at all.
    """
    import pandas as pd

    from cfdinvest.ingestion.capital_prices import restrict_to_session

    index = pd.MultiIndex.from_product(
        [pd.date_range("2026-06-01 08:00", periods=18, freq="h", tz="UTC"), ["AAA"]],
        names=["timestamp", "ticker"],
    )
    frame = pd.DataFrame({"close": range(len(index))}, index=index)

    kept = restrict_to_session(frame)
    hours = sorted(
        set(
            pd.DatetimeIndex(kept.index.get_level_values("timestamp"))
            .tz_convert("America/New_York").hour
        )
    )
    assert hours and min(hours) >= 9 and max(hours) <= 15, (
        f"pre-market or after-hours bars survived: {hours}"
    )
    assert len(kept) < len(frame), "nothing was filtered at all"


def test_the_session_filter_is_idempotent():
    """A caller that already filtered loses nothing by being filtered again
    -- research scripts do it by hand and must keep working."""
    import pandas as pd

    from cfdinvest.ingestion.capital_prices import restrict_to_session

    index = pd.date_range("2026-06-01 08:00", periods=18, freq="h", tz="UTC")
    frame = pd.DataFrame({"close": range(len(index))}, index=index)
    once = restrict_to_session(frame)
    assert len(restrict_to_session(once)) == len(once)


def test_only_capital_intervals_are_filtered():
    """yfinance bars are already session-only. Filtering them would be
    harmless but the intent matters: this is a correction for one feed's
    coverage, not a general rule about what hours to trade."""
    from cfdinvest.ingestion.capital_prices import is_capital_interval

    assert is_capital_interval("capital_1h")
    assert is_capital_interval("capital_5m")
    assert not is_capital_interval("1h")
    assert not is_capital_interval("1d")
