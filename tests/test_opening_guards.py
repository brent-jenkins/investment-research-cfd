"""Not at the bell, and not all at once.

Two guards on opening a position. Both refuse rather than place, so a fault
costs trades and never makes them, and both apply to opens alone -- being
unable to leave is a worse failure than entering badly.

M373 dealing into the opening auction, the dearest moment of the day
M374 the delay measured against a calendar that counts pre-market as open
M375 a close or resize blocked by either guard
M376 a cold start opening the whole book in one pass
M377 the deferred opens chosen arbitrarily rather than by conviction
"""

from __future__ import annotations

import pandas as pd
import pytest

from cfdinvest.broker.capital import bridge


class Market:
    def __init__(self, epic, price=100.0):
        self.epic = epic
        self.bid = price
        self.offer = price * 1.0006
        self.min_deal_size = 0.1
        self.ok = True


class Limits:
    max_exposure_per_instrument_gbp = 3500.0
    max_total_exposure_gbp = 17500.0


def plan(rows, *, positions=None, open_allowed=None, max_opens=None, rechecked=None):
    markets = {r["ticker"]: Market(r["ticker"]) for r in rows}
    return bridge.plan_orders(
        rows, markets, positions or {},
        balance=5000.0, limits=Limits(),
        exposure_per_unit_size=1.0, rebalance_threshold=0.25,
        session_open={r["ticker"]: True for r in rows},
        open_allowed=open_allowed,
        max_opens_per_pass=max_opens,
        recheck=rechecked is not None,
        rechecked=rechecked,
    )


def row(ticker, size=0.5, direction="long", confidence=0.8):
    return {"ticker": ticker, "direction": direction, "size": size,
            "confidence": confidence}


def test_no_new_position_inside_the_opening_window():
    """M373: entering into the auction.

    The opening hour's median spread is 9.26bp against 7.14bp in the core
    session, and 28.89bp on INTC. Against an edge breaking even near 6bp of
    friction, that is a fifth of the budget given away before the position
    has done anything.
    """
    report = plan([row("AAPL")], open_allowed={"AAPL": False})

    assert report.actionable == ()
    assert any("opening window" in (i.reason or "") for i in report.intents), (
        [i.reason for i in report.intents]
    )


def test_an_existing_position_is_still_managed_inside_the_window():
    """M375: unable to leave.

    The guard is about entering badly. Being stuck in a position because
    the bell rang half an hour ago is a worse failure than any spread.
    """
    held = {"AAPL": {"size": 10.0, "direction": "BUY", "deal_id": "D1"}}

    # Signal gone: the position must still be closeable.
    closing = plan([row("AAPL", size=0.0)], positions=held,
                   open_allowed={"AAPL": False})
    assert any(i.action == "close" for i in closing.intents), (
        f"a close was blocked by the opening window: "
        f"{[(i.action, i.reason) for i in closing.intents]}"
    )

    # And a drifted position must still be adjustable.
    resizing = plan([row("AAPL", size=2.0)], positions=held,
                    open_allowed={"AAPL": False})
    assert any(i.action in ("resize", "close") for i in resizing.intents)
    assert not any("opening window" in (i.reason or "") for i in resizing.intents)


def test_the_window_is_measured_against_the_regular_session():
    """M374: a calendar that counts pre-market as open.

    The delay asks whether the market was *also* open N minutes ago. If the
    calendar counted extended hours, that would answer yes before the bell
    and the guard would never bind -- a false negative that looks exactly
    like a guard working.
    """
    from cfdinvest.config import load_config
    from cfdinvest.schedule.market_calendar import describe

    tickers = list(load_config().market.tickers)
    before = describe(tickers, pd.Timestamp("2026-08-28T13:29:00Z"))
    after = describe(tickers, pd.Timestamp("2026-08-28T13:31:00Z"))

    assert int(before["is_open"].sum()) == 0, (
        "the calendar reports instruments open before the bell, so the "
        "opening delay would be measured from the wrong moment"
    )
    assert int(after["is_open"].sum()) == len(tickers)


def test_the_window_closes_after_the_configured_delay():
    from cfdinvest.config import load_config
    from cfdinvest.schedule.market_calendar import describe

    """The calendar mechanism, tested across delays rather than at whatever
    the shipped one happens to be.

    This asserted ``delay > 0`` and broke on 2026-09-09 when the delay was
    measured to cost 58 percentage points and set to zero. The mechanism was
    fine; the test was pinned to a configured value, which is the
    test-the-constant failure this project's own rules name. What matters is
    that the calendar answers "was this market also open N minutes ago"
    correctly for ANY N, including zero.
    """
    settings = load_config()
    tickers = list(settings.market.tickers)
    delay = 30                       # explicit, not the shipped value

    def allowed(at):
        now = describe(tickers, at)["is_open"]
        settled = describe(tickers, at - pd.Timedelta(minutes=delay))["is_open"]
        return sum(
            1 for t in now.index if bool(now[t]) and bool(settled.get(t, False))
        )

    open_at = pd.Timestamp("2026-08-28T13:30:00Z")
    assert allowed(open_at + pd.Timedelta(minutes=1)) == 0
    assert allowed(open_at + pd.Timedelta(minutes=delay - 1)) == 0
    assert allowed(open_at + pd.Timedelta(minutes=delay + 1)) == len(tickers)

    # And zero, which is what ships from 2026-09-09. A delay of nothing must
    # permit dealing from the first minute -- not be read as falsy and
    # silently disable the whole check, and not accidentally refuse
    # everything. The dealing path spells this `settled = describe(...) if
    # delay else None`, so the branch is real and worth an assertion.
    delay = 0
    assert allowed(open_at + pd.Timedelta(minutes=1)) == len(tickers), (
        "a zero delay must allow dealing from the open"
    )


def test_a_cold_start_does_not_open_the_whole_book_at_once():
    """M376: thirteen positions in one pass.

    Measured on 2026-08-28: a book restarted from flat would have opened
    thirteen at once, at the widest spreads of the day, on a seventeen-hour
    old signal.
    """
    rows = [row(f"T{n}", confidence=0.5 + n / 100) for n in range(13)]
    report = plan(rows, max_opens=3)

    opened = [i for i in report.intents if i.action == "open"]
    deferred = [i for i in report.intents if "deferred" in (i.reason or "")]

    assert len(opened) == 3, f"{len(opened)} opened, expected 3"
    assert len(deferred) == 10
    assert all(i.target_units == i.held_units for i in deferred), (
        "a deferred open still carried a target"
    )


def test_the_kept_opens_are_the_highest_conviction():
    """M377: which three, chosen arbitrarily.

    Row order is whatever the book happened to replay. If only three go
    through, they should be the three the strategy believes in most.
    """
    rows = [row("LOW", confidence=0.36), row("MID", confidence=0.55),
            row("HIGH", confidence=0.90), row("ALSO_LOW", confidence=0.40)]
    rechecked = {
        r["ticker"]: {"direction": "long", "confidence": r["confidence"]}
        for r in rows
    }
    report = plan(rows, max_opens=2, rechecked=rechecked)

    opened = {i.ticker for i in report.intents if i.action == "open"}
    assert opened == {"HIGH", "MID"}, f"opened {opened}"


def test_an_instrument_the_window_says_nothing_about_is_not_opened():
    """M375b: silence read as permission.

    In the live path the map always covers the same instruments as the
    session gate, so a missing entry means a caller supplied a partial one.
    That is "not checked", and this project does not treat not-checked as
    safe -- least of all on the guard whose whole job is to refuse.
    """
    report = plan([row("AAPL")], open_allowed={"MSFT": True})

    assert report.actionable == ()
    assert any("opening window" in (i.reason or "") for i in report.intents), (
        "an instrument absent from the window map was opened anyway"
    )


def test_no_stagger_configured_opens_everything():
    rows = [row(f"T{n}") for n in range(6)]
    report = plan(rows, max_opens=None)
    assert len([i for i in report.intents if i.action == "open"]) == 6


def test_the_guards_are_absent_by_default():
    """Neither may switch itself on for a caller that did not ask."""
    rows = [row(f"T{n}") for n in range(5)]
    report = plan(rows)
    assert len([i for i in report.intents if i.action == "open"]) == 5
    assert not any("opening window" in (i.reason or "") for i in report.intents)
    assert not any("deferred" in (i.reason or "") for i in report.intents)


def test_both_settings_reach_the_bridge():
    """The seam: configured, and actually handed over."""
    import inspect

    from cfdinvest.config import load_config
    from cfdinvest.schedule import app

    settings = load_config()
    # Configured and handed over -- NOT "switched on". Asserting a positive
    # delay tested the shipped value rather than the seam, and zero is a
    # legitimate setting from 2026-09-09.
    assert isinstance(settings.strategy.open_delay_minutes, int)
    assert settings.strategy.open_delay_minutes >= 0
    # `is not None` was the same test-the-constant mistake as the delay
    # above: it asserted the throttle was SWITCHED ON, not that it reaches
    # the bridge. Unset is a legitimate setting from 2026-09-10, and the
    # seam is what this test is for -- the plumbing assertions below.
    budget = settings.strategy.max_opens_per_pass
    assert budget is None or (isinstance(budget, int) and budget > 0), budget

    source = inspect.getsource(app._command_trade_capital)
    assert "open_allowed=open_allowed" in source
    assert "max_opens_per_pass=app_config.strategy.max_opens_per_pass" in source
