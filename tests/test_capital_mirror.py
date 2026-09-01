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


# --------------------------------------------------------------------------- #
# The engine's exits have to reach the broker
# --------------------------------------------------------------------------- #
# M-live: a position the engine had closed stayed open on the account
# forever. ``intended_positions`` reports what is HELD, so an exited
# instrument is absent from it, and ``plan_orders`` iterates the rows it is
# given -- no row, no intent, no close order, financing accruing daily.
#
# These are functional: they run the real bridge and assert on the orders it
# plans. The version of this that would have passed through the bug was a
# structural one, and this file already carries the scar of a structural
# assertion that held for four days while the account traded a different
# strategy.


def _market(ticker="AAPL", **kw):
    from cfdinvest.broker.capital.client import CapitalMarket

    return CapitalMarket(
        epic=ticker, ticker=ticker, name=ticker, bid=99.5, offer=100.5,
        currency="USD", min_deal_size=0.1, tradable=True, **kw
    )


def _plan(rows, positions, ticker="AAPL"):
    from cfdinvest.broker.capital import bridge
    from cfdinvest.risk import RiskLimits

    return bridge.plan_orders(
        rows, {ticker: _market(ticker)}, positions,
        balance=10_000.0,
        limits=RiskLimits.for_balance(10_000.0, max_leverage=5.0,
                                      concentration=0.20),
        exposure_per_unit_size=4.0, rebalance_threshold=0.5,
        session_open={ticker: True}, placed={}, fx_rate=1.0, recheck=False,
        open_allowed={ticker: True},
    )


class _Held:
    def __init__(self, epic, size=10.0, direction="BUY"):
        self.epic, self.size, self.direction = epic, size, direction
        self.deal_id = f"D-{epic}"


def test_an_exited_position_reaches_the_broker_as_a_close():
    """The bug, end to end: engine holds nothing, account holds AAPL.

    Without the flat row the bridge plans nothing whatsoever -- not a
    refusal, which would at least be visible in the report, but no intent at
    all. Asserted both ways round so the test fails if the synthesis stops
    happening AND if the bridge stops acting on it.
    """
    held = [_Held("AAPL")]
    positions = {"AAPL": {"size": 10.0, "direction": "BUY", "deal_id": "D-AAPL"}}

    # The shape of the bug, kept as the control. This is what the account did.
    bare = _plan([], positions)
    assert not [i for i in bare.intents if i.action == "close"], (
        "the control is wrong: the bridge closed without being told to"
    )

    fixed = _plan(app._with_exits([], held), positions)
    closes = [i for i in fixed.intents if i.action == "close"]
    assert [i.ticker for i in closes] == ["AAPL"]
    assert closes[0].target_units == 0.0
    assert closes[0].held_units == 10.0


def test_a_position_the_engine_still_wants_is_not_closed():
    """The other half, and the one that matters to the owner.

    Nothing is force-closed. A position the engine still holds must survive
    this untouched -- it exits on its own reversal or its own time stop, the
    way it would have if the universe had never changed.
    """
    held = [_Held("AAPL")]
    positions = {"AAPL": {"size": 10.0, "direction": "BUY", "deal_id": "D-AAPL"}}
    rows = [{"ticker": "AAPL", "direction": "long", "size": 0.05}]

    planned = _plan(app._with_exits(rows, held), positions)

    assert not [i for i in planned.intents if i.action == "close"], (
        "a position the engine still wants was closed"
    )
    assert [i.action for i in planned.intents] == ["resize"]


def test_a_departed_name_still_held_by_the_engine_is_left_alone():
    """The run-out rule, which this must not undo.

    A name dropped from the traded list keeps its signals so it can exit on
    its own terms. While the engine still holds it, it is intended -- so no
    flat row is synthesised for it and no close is planned, even though it
    is no longer selectable.
    """
    held = [_Held("GOOGL")]
    rows = [{"ticker": "GOOGL", "direction": "long", "size": 0.05}]

    out = app._with_exits(list(rows), held)

    assert out == rows, "a name the engine still holds was marked for exit"


def test_only_positions_the_broker_actually_holds_are_exited():
    """M: every instrument ever traded got a flat row.

    What may be *closed* is whatever the broker says is open -- the
    broker's fact, not a configuration value and not a history.
    """
    out = app._with_exits([], [_Held("AAPL"), _Held("XOM")])

    assert sorted(r["ticker"] for r in out) == ["AAPL", "XOM"]
    assert all(r["direction"] == "flat" and r["size"] == 0.0 for r in out)
    # No positions at all is not an instruction to close anything.
    assert app._with_exits([], []) == []
    assert app._with_exits([], None) == []


def test_an_unreadable_run_cannot_synthesise_exits():
    """M366, in its sharpest form: this is how you liquidate a book.

    ``intended_positions`` returns None for an unreadable run and [] for a
    run that holds nothing. Collapsing them here would give every open
    position a flat row and close the entire account because an artefact was
    missing. It refuses rather than guessing, and the caller returns before
    reaching it.
    """
    with pytest.raises(ValueError):
        app._with_exits(None, [_Held("AAPL")])

    # And the caller checks. Functional: the flat-row synthesis must sit
    # behind an explicit `latest is None` return, not merely be reachable.
    import inspect

    source = inspect.getsource(app._command_trade_capital)
    guard = source.index("latest is None")
    call = source.index("_with_exits(")
    assert guard < call, "exits are synthesised before the unreadable-run guard"


def test_an_unreadable_run_leaves_the_book_untouched():
    """None and [] are different, at the one call site that places orders.

    ``if not latest`` is true for both, which is the collapse ``intent``
    exists to prevent. An unreadable run must return before anything is
    planned.
    """
    import inspect

    source = inspect.getsource(app._command_trade_capital)
    block = source[source.index("latest is None"):]
    block = block[:block.index("if not latest")]
    assert "return EXIT_OK" in block, (
        "an unreadable run does not return early, so it can still trade"
    )


# --------------------------------------------------------------------------- #
# The engine and the account must agree about when trading starts
# --------------------------------------------------------------------------- #
# open_delay_minutes = 30 lived only in schedule/app.py. The daemon refused
# to open a position in the first half hour and the engine modelled none of
# it, so the backtest took trades at 13:00 UTC that production would never
# place -- and every figure it produced described a strategy nobody ran.
# That is the shadow-book failure in a smaller costume.


def _delay_frames(bars_per_day=4, days=6, delay=30):
    """A session-restricted hourly panel with a persistent long signal."""
    import numpy as np
    import pandas as pd

    stamps = []
    for day in range(days):
        base = pd.Timestamp("2026-01-05", tz="UTC") + pd.Timedelta(days=day)
        stamps += [base + pd.Timedelta(hours=13 + n) for n in range(bars_per_day)]
    index = pd.DatetimeIndex(stamps)
    # A price that only ever rises, so a long signal is always profitable and
    # any refusal to open shows up as a missing position rather than a loss.
    close = pd.DataFrame({"AAA": np.linspace(100.0, 140.0, len(index))}, index=index)
    return index, close


def test_the_engine_refuses_to_open_inside_the_opening_window():
    """The reported gap, asserted on what the engine holds.

    The scenario has to go FLAT before each session, or there is no entry
    for the gate to refuse -- a position carried overnight is simply held,
    and the first draft of this test asserted against a book that never
    re-entered. That is the inert-arm failure in test form.
    """
    import numpy as np
    import pandas as pd

    from cfdinvest.execution import run_backtest
    from cfdinvest.execution.frame import ExecutionConfig
    from cfdinvest.preprocessing.frame import AnalysisFrame
    from cfdinvest.signals.frame import SignalFrame

    bars, days = 4, 4
    stamps = []
    for day in range(days):
        base = pd.Timestamp("2026-01-05", tz="UTC") + pd.Timedelta(days=day)
        stamps += [base + pd.Timedelta(hours=13 + n) for n in range(bars)]
    index = pd.DatetimeIndex(stamps)
    close = pd.DataFrame({"AAA": np.linspace(100.0, 140.0, len(index))},
                         index=index)

    # Long on the first two bars of each session and flat afterwards, run with
    # execution_lag = 0 so the fill lands on the bar the signal is on. The
    # lag is removed deliberately: with it, an entry cannot land on an
    # opening bar unless a signal happens to turn over exactly at the
    # previous close, and a test that depends on that coincidence is testing
    # the coincidence. Two bars rather than one because a signal lasting a
    # single bar never opens at all -- the control has to actually trade
    # or the assertion below proves nothing. Here the control opens on
    # every opening bar and the gate is the only thing that can stop it.
    want = ["flat"] * len(index)
    size = [0.0] * len(index)
    for n in range(len(index)):
        if (n % bars) in (0, 1):
            want[n], size[n] = "long", 0.1
    frame = lambda values: pd.DataFrame({"AAA": values}, index=index)
    signals = SignalFrame(
        direction=frame(want), size=frame(size),
        confidence=frame([0.9] * len(index)), reason=frame([""] * len(index)),
    )
    analysis = AnalysisFrame(
        prices={"close": close, "open": close, "high": close, "low": close},
        returns=close.pct_change().fillna(0.0),
    )

    without = run_backtest(signals, analysis,
                           ExecutionConfig(open_delay_minutes=0, execution_lag=0))
    within = run_backtest(signals, analysis,
                          ExecutionConfig(open_delay_minutes=30, execution_lag=0))

    open_bars = index[[stamp.hour == 13 for stamp in index]]
    held_without = without.positions["AAA"].fillna(0.0).reindex(open_bars)
    held_within = within.positions["AAA"].fillna(0.0).reindex(open_bars)

    assert (held_without != 0).any(), (
        "the control never opened on an opening bar, so this proves nothing"
    )
    assert (held_within == 0).all(), (
        f"a position was opened inside the opening window: {dict(held_within)}. "
        "The live dealing path would have refused it."
    )
    # And the delayed run must still trade -- refusing everything would pass
    # the assertion above while being useless.
    assert (within.positions["AAA"].fillna(0.0) != 0).any()


def test_the_delay_never_blocks_an_exit():
    """Refusing an exit is never the safe direction.

    The live path draws the same line: inside the opening window a new
    position is refused while anything already held may still be closed.
    """
    import inspect

    from cfdinvest.execution import engine

    source = inspect.getsource(engine)
    block = source[source.index("delay = int(getattr(config"):]
    block = block[:block.index("fx_config")]
    assert "entry_gate" in block, (
        "the opening window is applied somewhere other than the entry gate, "
        "so it may be blocking exits as well as entries"
    )


def test_the_engine_and_the_dealing_path_read_the_same_delay():
    """One owner. The backtest and the account must not disagree about when
    trading starts, which is the whole reason this field exists."""
    import inspect

    from cfdinvest.config import load_config
    from cfdinvest.schedule import app as schedule_app

    config = load_config()
    execution = schedule_app._execution_config(
        ["AAPL"], config.strategy, config
    )
    assert execution.open_delay_minutes == config.strategy.open_delay_minutes

    # Every ExecutionConfig this function can return must carry it, including
    # the fallback taken when the broker table cannot be read -- a fallback
    # that quietly dropped it would model a different strategy exactly when
    # something else had already gone wrong.
    source = inspect.getsource(schedule_app._execution_config)
    assert source.count("ExecutionConfig(") == source.count("open_delay_minutes=getattr")


# --------------------------------------------------------------------------- #
# A screen that cannot answer must stop opening, not fall back
# --------------------------------------------------------------------------- #
# traded_universe fell through to market.tickers whenever the screen had
# nothing usable, and the daemon traded it. That list is a deliberate human
# choice with follow_screen OFF and something else entirely when the screen
# was meant to answer and could not: opening from a list somebody set at an
# unremembered point is inferring it is still right, which is the failure
# universe/traded.py's own docstring warns about.
#
# Owner's call, 2026-09-01: run autonomously, and say so when it cannot.


def test_a_deliberate_config_list_is_not_a_screen_failure():
    """With follow_screen off, market.tickers IS the answer.

    Conflating the two would refuse to trade a perfectly intentional
    configuration, which is the mirror error and just as wrong.
    """
    from cfdinvest.universe.traded import TradedUniverse

    assert not TradedUniverse(names=("MU",), source="config").screen_failed
    assert not TradedUniverse(names=("MU",), source="screen").screen_failed


def test_a_screen_that_could_not_answer_is_flagged():
    """The two ways it fails, both of which used to trade the fallback."""
    from cfdinvest.universe.traded import TradedUniverse

    for source in ("config (selections unreadable)",
                   "config (no selection recorded)"):
        assert TradedUniverse(names=("MU",), source=source).screen_failed, source


def test_the_dealing_path_opens_nothing_when_the_screen_fails():
    """Structural, and stated as such: there is no functional route into
    _command_trade_capital without a broker session.

    The assertion is on the pairing -- the openable list must be emptied AND
    the engine's rows trimmed to what is held. Emptying the list alone would
    not be enough, because _dealing_tickers widens the priced set from the
    engine's own intent, so an unheld name would still be priced and opened.
    """
    import inspect

    source = inspect.getsource(app._command_trade_capital)

    assert "screen_failed = universe.screen_failed" in source
    assert "tickers = [] if screen_failed else list(universe.names)" in source
    # And the intent is trimmed, or the bridge opens from it anyway.
    trim = source[source.index("if screen_failed:"):]
    # The FILTER, not merely the set it filters against. A first version of
    # this asserted that `open_now` appeared, and a mutation removing the
    # filter while leaving the assignment sailed through it -- the set is
    # built and then ignored, which is exactly the shape of an inert guard.
    assert "latest = [r for r in latest" in trim, (
        "the openable list is emptied but the engine's rows are not trimmed, "
        "so the bridge would still open a name the screen cannot vouch for"
    )
    assert "open_now" in trim


def test_a_screen_failure_still_lets_positions_close():
    """The half that must not break.

    Stopping dead would strand whatever is open -- the failure this project
    already fixed once, when nine positions sat unclosable on a funded demo
    account. Closing is gated on what the broker holds, never on the screen.
    """
    import inspect

    source = inspect.getsource(app._command_trade_capital)
    # The early return must not fire on a screen failure, or the pass ends
    # before anything can be closed.
    guard = source[source.index("if not tickers and not screen_failed:"):]
    assert guard.startswith("if not tickers and not screen_failed:"), guard[:80]

    # Held instruments are still priced, which is what makes a close possible.
    assert "_dealing_tickers(tickers, latest, held)" in source


def test_a_screen_failure_raises_a_notification():
    """Silence is the one unacceptable response.

    The owner asked to be told when the system cannot decide for itself, and
    a system that quietly narrows to closing-only looks identical to a quiet
    market.
    """
    import inspect

    source = inspect.getsource(app._command_trade_capital)
    assert "_send_screen_halt" in source

    mail = inspect.getsource(app._send_screen_halt)
    # It must say what is still happening, or it reads as a full stop.
    assert "still closed" in mail or "still priced" in mail
    assert "no new position" in mail.lower()
