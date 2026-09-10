"""A position the record holds and the account does not must reconcile.

On 2026-09-10 TGT was closed outside the dealing path -- an opposing BUY
placed through the API on the owner's instruction to close it. (This
docstring first said the OWNER closed it by hand; they did not, and the
broker's activity log says `source: USER` at 13:44:55 with no matching plan
or result row in this app's record.) The owner's stated mode does permit a
hand closure in an emergency, so two causes share the signature.

Either way no result row was written, so `open_orders` went on reporting
TGT held, `reconcile` reported it missing, and `plan_orders` halted **every
dealing pass for the rest of the session**:

    Capital: NOT TRADING -- the account and the record disagree
    — missing: TGT. Opening nothing until a person has looked.

The guard was doing exactly what it says. The fault is that a permitted
action could jam the dealer with nothing in the app able to clear it, and
that the jam was silent about being a jam -- it read as a routine warning
while the account sat flat through a whole session.

**The two directions are not symmetrical, and the asymmetry is the whole
design.** An UNEXPLAINED position is money at risk nobody chose, and the
bridge still refuses to act at all on one. A MISSING position is the record
being behind the account, and the account is the truth about what is held.

What makes healing safe here is specific and was measured rather than
assumed: `placed` is read by `reconcile` and by nothing else in
`plan_orders`. Every position figure -- sizing, exposure, the leverage and
concentration caps -- comes from `positions`, the broker's own answer. So a
record behind the account cannot produce a wrong order. It can only stop
the account trading.

Mutations checked, each reintroduced as a bug and confirmed to fail:
  M700 the departure never recorded, so the record stays ahead forever
  M710 healing applied to unexplained positions too, adopting one nobody chose
  M720 the close row written with a fabricated level of zero
  M730 `reconciled` not marked, so a hand closure reads as one the app placed
  M740 a close row stamped OPEN, which `_confirmed` rejects silently
  M741 the row carrying neither a status nor an ok flag
  M742 an error left on the row, which disqualifies it at read time
  M750 the departure recorded but `placed` not re-read, so the pass still halts

One mutation SURVIVED and is recorded because it says something about the
code rather than about the suite: writing the row with an empty `status`
and `ok: True` still reconciles, because `_confirmed` deliberately honours
`ok` for a row carrying no status at all -- the one case where the
observation is genuinely absent rather than disagreeing. It was a bad
mutation rather than a gap. M740 and M741 are the real forms.
"""

from __future__ import annotations

import json

import pytest

from cfdinvest.broker.capital.bridge import reconcile
from cfdinvest.persist import orders


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "orders" / "orders.jsonl"
    monkeypatch.setattr(orders, "_path", lambda *a, **k: path)
    return path


def an_open(store, epic="TGT", units=28.7, level=156.1):
    store.parent.mkdir(parents=True, exist_ok=True)
    with store.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "at": "2026-09-10 13:36:10+00:00", "kind": "result",
            "ticker": epic, "epic": epic, "action": "open", "status": "OPEN",
            "ok": True, "level": level, "units": units, "direction": "SELL",
            "position_units": units, "position_direction": "SELL",
            "deal_id": "deal-1",
        }, sort_keys=True) + "\n")


def test_the_record_learns_that_the_position_is_gone(store):
    """M700/M740: the jam, and the silent way of not fixing it.

    A row the store writes but `_confirmed` rejects would leave the record
    unchanged while every log line said it had been healed -- which is this
    project's own inert-gate failure in a new place.
    """
    an_open(store)
    assert "TGT" in orders.open_orders()

    orders.record_departure("TGT", orders.open_orders()["TGT"], now="2026-09-10")

    assert "TGT" not in orders.open_orders(), (
        "the record still believes it holds TGT, so the bridge will halt on "
        "the next pass exactly as before"
    )


def test_the_bridge_then_agrees_with_the_account(store):
    """The property that matters, stated end to end.

    Asserted through `reconcile` itself rather than by inspecting the row,
    so it is the real comparison that has to come out clean.
    """
    an_open(store)
    account: dict = {}

    unexplained, missing = reconcile(account, orders.open_orders())
    assert missing == ("TGT",)

    orders.record_departure("TGT", orders.open_orders()["TGT"], now="2026-09-10")

    unexplained, missing = reconcile(account, orders.open_orders())
    assert missing == () and unexplained == ()


def test_the_exit_price_is_absent_rather_than_invented(store):
    """M720: a fabricated zero would be a measured-looking wrong number.

    This app did not place the close, so it has no fill price. Recording
    0.0 would pair into `completed_trips` and produce a P&L computed
    against nothing -- a figure indistinguishable from a real one, which is
    the failure this project's retraction rule exists to prevent.
    """
    an_open(store)
    orders.record_departure("TGT", orders.open_orders()["TGT"], now="2026-09-10")

    row = [json.loads(line) for line in store.read_text().splitlines()][-1]
    assert row["level"] is None, "an exit price was invented"

    trips = orders.completed_trips()
    assert not [t for t in trips if t["ticker"] == "TGT"], (
        "a trip was recorded with no known exit price"
    )


def test_a_reconciled_close_is_distinguishable(store):
    """M730: a hand closure and a placed close are different facts.

    A rising count of these is an order path recording fills it is not
    getting, which is a worse problem than an emergency closure and looks
    identical without the mark.
    """
    an_open(store)
    orders.record_departure("TGT", orders.open_orders()["TGT"], now="2026-09-10")

    row = [json.loads(line) for line in store.read_text().splitlines()][-1]
    assert row.get("reconciled") is True
    assert row.get("action") == "close"


def test_an_unexplained_position_is_never_adopted(store):
    """M710: the direction that must keep halting.

    A position on the account the app never opened is money at risk nobody
    chose. Healing it would be adopting a position rather than reconciling
    a record, and `reconcile` must go on reporting it so the bridge halts.
    """
    account = {"NFLX": {"size": 3.0, "direction": "BUY"}}

    unexplained, missing = reconcile(account, orders.open_orders())

    assert unexplained == ("NFLX",), (
        "an unexplained position stopped being reported, so the bridge "
        "would now trade alongside a position nobody chose"
    )


def test_the_dealing_pass_heals_before_it_plans(store):
    """M750: recording the departure and then not re-reading the record.

    The pass would write the row, pass the stale `placed` to `plan_orders`,
    and halt anyway -- fixed on disk and still jammed in memory, which is
    the hardest version of this to notice because the record looks right
    afterwards.
    """
    import inspect

    from cfdinvest.schedule import app

    source = inspect.getsource(app._command_trade_capital)

    assert "record_departure" in source, (
        "the dealing pass never reconciles a departed position"
    )
    heal = source.index("record_departure")
    plan = source.index("bridge.plan_orders(")
    assert heal < plan, "the record is healed after the plan is made"
    assert "placed = order_store.open_orders(" in source[heal:plan], (
        "the record is not re-read after healing, so the stale belief is "
        "what reaches plan_orders and the pass halts anyway"
    )
