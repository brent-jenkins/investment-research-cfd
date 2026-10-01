"""A paused account takes no new risk and can still shed all of it.

Owner's decision, 2026-10-01: the strategy is measured to lose and the demo
balance cannot be reset, so opening stops. The failure this guards against
runs in both directions -- a pause that still lets risk in is no pause, and
a pause that stops a close is the 2026-09-10 halt that blocked its own
remedy.

M1940 an open placed while paused
M1941 a resize that GROWS a position placed while paused
M1942 a close refused while paused
M1943 a shrinking resize refused while paused
M1944 the flatten refused while paused
M1945 the shipped config not reaching the field
"""

from __future__ import annotations

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


def plan(rows, *, positions=None, paused=True, closing_soon=None):
    markets = {r["ticker"]: Market(r["ticker"]) for r in rows}
    return bridge.plan_orders(
        rows, markets, positions or {},
        balance=5000.0, limits=Limits(),
        exposure_per_unit_size=1.0, rebalance_threshold=0.25,
        session_open={r["ticker"]: True for r in rows},
        recheck=False,
        closing_soon=closing_soon,
        flatten_before_close=closing_soon is not None,
        opens_paused=paused,
    )


def row(ticker, size=0.5, direction="long"):
    return {"ticker": ticker, "direction": direction, "size": size,
            "confidence": 0.8}


def held(units, direction="BUY"):
    return {"size": units, "direction": direction, "deal_id": "D1"}


def actions(report):
    return {i.ticker: i.action for i in report.actionable}


def test_nothing_opens_while_paused_and_the_record_says_why():
    """M1940. The same row opens unpaused -- so the refusal is the pause."""
    assert actions(plan([row("AAA")], paused=False)) == {"AAA": "open"}

    report = plan([row("AAA")])
    assert report.actionable == ()
    assert [i.reason for i in report.intents] == ["opening is paused"]


def test_a_position_is_not_grown_while_paused():
    """M1941. 10 units held against a target of 25 is a growing resize."""
    positions = {"AAA": held(10.0)}
    assert actions(plan([row("AAA")], positions=positions, paused=False)) == {
        "AAA": "resize"
    }
    assert actions(plan([row("AAA")], positions=positions)) == {}


def test_a_close_still_goes_through_while_paused():
    """M1942. A signal gone flat must still empty the position."""
    report = plan([row("AAA", size=0.0)], positions={"AAA": held(10.0)})
    assert actions(report) == {"AAA": "close"}


def test_a_shrinking_resize_still_goes_through_while_paused():
    """M1943. 40 units held against a target of 25 reduces risk."""
    report = plan([row("AAA")], positions={"AAA": held(40.0)})
    assert actions(report) == {"AAA": "resize"}
    (intent,) = report.actionable
    assert abs(intent.target_units) < 40.0


def test_the_flatten_still_runs_while_paused():
    """M1944. The bell must still empty the book, long and short."""
    report = plan(
        [row("AAA"), row("BBB", direction="short")],
        positions={"AAA": held(25.0), "BBB": held(25.0, "SELL")},
        closing_soon={"AAA": True, "BBB": True},
    )
    assert actions(report) == {"AAA": "close", "BBB": "close"}


def test_the_setting_reaches_the_bridge():
    """M1945. A switch the loader drops, or the dealer never hands on, is a
    pause that never happened. The dealer has no dry run, so the hand-off is
    checked on an UNCOMMENTED line -- a comment once satisfied a source
    check here (M1762)."""
    import inspect

    from cfdinvest.config import load_config
    from cfdinvest.schedule import app

    assert load_config(overrides={"strategy": {"opens_paused": True}}).strategy.opens_paused
    assert not load_config(overrides={"strategy": {"opens_paused": False}}).strategy.opens_paused

    live = [
        line.split("#", 1)[0]
        for line in inspect.getsource(app._command_trade_capital).splitlines()
    ]
    assert any(
        "opens_paused=" in line and "app_config.strategy" in line for line in live
    ), "the dealing pass does not hand strategy.opens_paused to plan_orders"
