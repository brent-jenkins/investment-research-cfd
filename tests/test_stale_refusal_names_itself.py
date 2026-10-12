"""A refusal because the analysis is behind says so.

`decision_is_stale` clears `open_allowed`, and the bridge wrote every such
refusal "within the opening window" -- on 2026-10-08 that was 114 rows in
the middle of the session. The label decides nothing.

Mutations checked:

  M2050 a stale-decision refusal still labelled as the opening window
  M2051 the label refusing an open on its own
  M2052 the dealer not handing the flag on
"""
from __future__ import annotations

import inspect

from cfdinvest.broker.capital import bridge


class Market:
    def __init__(self, epic, price=100.0):
        self.epic, self.bid, self.offer = epic, price, price * 1.0006
        self.min_deal_size, self.ok = 0.1, True


class Limits:
    max_exposure_per_instrument_gbp = 3500.0
    max_total_exposure_gbp = 17500.0


def _plan(**kw):
    rows = [{"ticker": "AAA", "direction": "long", "size": 0.5, "confidence": 0.8}]
    return bridge.plan_orders(
        rows, {"AAA": Market("AAA")}, {}, balance=5000.0, limits=Limits(),
        exposure_per_unit_size=1.0, rebalance_threshold=0.25,
        session_open={"AAA": True}, recheck=False, **kw,
    )


def test_a_stale_refusal_names_the_analysis():
    """M2050."""
    report = _plan(open_allowed={"AAA": False}, decision_stale=True)
    assert [i.reason for i in report.intents] == [bridge.STALE_DECISION_REASON]


def test_the_label_alone_refuses_nothing():
    """M2051."""
    report = _plan(open_allowed={"AAA": True}, decision_stale=True)
    assert [i.action for i in report.actionable] == ["open"]


def test_the_dealer_hands_it_on():
    """M2052, on an uncommented line."""
    from cfdinvest.schedule import app

    live = [l.split("#", 1)[0] for l in inspect.getsource(app._command_trade_capital).splitlines()]
    assert any("decision_stale=bool(decision_stale)" in l for l in live)


def test_the_replay_reads_the_same_spelling():
    """The replay groups clock refusals by string; a drift would quietly
    move stale refusals out of the clock and change what a replay reports."""
    from cfdinvest.research import replay

    assert bridge.STALE_DECISION_REASON in replay._CLOCK_REASONS
