"""One instrument can be several positions, everywhere it is counted.

Capital merges only OPPOSING trades. Two SELLs on one epic are two
positions with two deal ids, and on 2026-09-10 that single fact produced
three separate failures in six hours, because both the account view and the
record modelled ONE POSITION PER INSTRUMENT:

    17:36:52  SPGI SELL 1.99 @ 414.93
    18:18:36  SPGI SELL 8.64 @ 413.14
    18:40:14  SPGI SELL 8.67 @ 412.13     19.30 units in three legs

  * ``{p.epic: ... for p in held}`` kept the last leg, so every limit
    measured 8.67 units of 19.30. The concentration cap passed a book that
    was **100% of gross in one name** against a 20% cap, and each pass
    topped the position up again -- the three legs are the bug's own
    footprints.
  * a close shut one leg, so the flatten before the bell would have carried
    10.63 units overnight on a record that said flat.
  * ``open_orders`` popped the whole epic on that one close row, so the
    record said flat while two legs were open, ``reconcile`` reported them
    as UNEXPLAINED, and the bridge halted -- before planning, so the close
    that would have cleared it could never be produced.

The fixes are in four places and this suite covers them together, because
they are one fault and fixing three of four leaves the account wrong.

Mutations checked, each reintroduced as a bug and confirmed to fail:
  M950 the account view collapsing legs again
  M960 legs summed unsigned, so a hedged pair doubles instead of netting
  M970 the record popping the whole epic on a close of one leg
  M980 the record keeping one leg's size rather than the total
  M990 an arrival recorded without a fill level, which the reader ignores
  M995 an open taking its side from the caller's sign, inverting a short
  M996 a resize taking its side from the fill, inverting a trimmed long
"""

from __future__ import annotations

import json

import pytest

from cfdinvest.broker.capital.bridge import reconcile
from cfdinvest.persist import orders
from cfdinvest.schedule.app import _aggregate_positions


class Leg:
    """What ``dealing.list_positions`` hands back, as far as this cares."""

    def __init__(self, epic, direction, size, deal_id, level=None):
        self.epic = epic
        self.direction = direction
        self.size = size
        self.deal_id = deal_id
        self.level = level


# ---------------------------------------------------------- the account view

def test_the_account_view_sums_every_leg():
    """M950: the collapse that defeated the concentration cap."""
    got = _aggregate_positions([
        Leg("SPGI", "SELL", 1.99, "a"),
        Leg("SPGI", "SELL", 8.64, "b"),
        Leg("SPGI", "SELL", 8.67, "c"),
    ])

    assert got["SPGI"]["size"] == pytest.approx(19.30), (
        "the book is under-stated, so every limit downstream measures a "
        "fraction of the real exposure"
    )
    assert got["SPGI"]["direction"] == "SELL"
    assert got["SPGI"]["deal_ids"] == ["a", "b", "c"], (
        "the legs are not all listed, so a close cannot shut them"
    )


def test_opposing_legs_net_rather_than_double():
    """M960: summing magnitudes would read a flat book as twice its size.

    Netting mode means this should not arise -- an opposing order closes
    rather than opens. "Should not arise" is exactly how the original
    collapse was reasoned about, so it is asserted instead.
    """
    got = _aggregate_positions([
        Leg("X", "BUY", 5.0, "1"), Leg("X", "SELL", 3.0, "2"),
    ])
    assert got["X"]["size"] == pytest.approx(2.0)
    assert got["X"]["direction"] == "BUY"

    flat = _aggregate_positions([
        Leg("X", "BUY", 5.0, "1"), Leg("X", "SELL", 5.0, "2"),
    ])
    assert flat["X"]["size"] == pytest.approx(0.0)


def test_instruments_stay_separate():
    """Aggregation must not merge across instruments."""
    got = _aggregate_positions([
        Leg("SPGI", "SELL", 8.0, "a"), Leg("LULU", "SELL", 46.3, "b"),
    ])
    assert set(got) == {"SPGI", "LULU"}
    assert got["LULU"]["size"] == pytest.approx(46.3)


def test_an_unreadable_leg_does_not_break_the_pass():
    """A pass that dies opens nothing AND closes nothing."""
    assert _aggregate_positions(None) == {}
    assert _aggregate_positions([object()]) == {}
    got = _aggregate_positions([object(), Leg("SPGI", "SELL", 8.0, "a")])
    assert got["SPGI"]["size"] == pytest.approx(8.0)


# --------------------------------------------------------------- the record

@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "orders" / "orders.jsonl"
    monkeypatch.setattr(orders, "_path", lambda *a, **k: path)
    return path


def write(store, **fields):
    store.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "at": "2026-09-10", "kind": "result", "ticker": "SPGI",
        "epic": "SPGI", "status": "OPEN", "ok": True, "level": 413.0,
        "direction": "SELL",
    }
    row.update(fields)
    with store.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


def three_legs(store):
    write(store, action="open", deal_id="a", units=1.99, position_units=1.99)
    write(store, action="open", deal_id="b", units=8.64, position_units=8.64)
    write(store, action="open", deal_id="c", units=8.67, position_units=8.67)


def test_the_record_sums_its_legs(store):
    """M980: one leg's size is not the position."""
    three_legs(store)
    assert orders.open_orders()["SPGI"]["size"] == pytest.approx(19.30)


def test_closing_one_leg_leaves_the_others(store):
    """M970: the bug that halted the dealer with the market closing."""
    three_legs(store)
    write(store, action="close", status="CLOSED", deal_id="a", level=None)

    live = orders.open_orders()

    assert "SPGI" in live, (
        "one close removed the whole instrument, so the record says flat "
        "while two legs are open and reconcile will report them unexplained"
    )
    assert live["SPGI"]["size"] == pytest.approx(17.31)
    assert sorted(live["SPGI"]["deal_ids"]) == ["b", "c"]


def test_closing_every_leg_clears_the_instrument(store):
    """The other end: all legs gone means the record must say flat."""
    three_legs(store)
    for deal in ("a", "b", "c"):
        write(store, action="close", status="CLOSED", deal_id=deal, level=None)

    assert orders.open_orders() == {}


def test_a_close_naming_no_known_leg_clears_the_instrument(store):
    """Deliberate, and the safer direction.

    After a resize the record's deal id is the ORDER's rather than the
    surviving position's, so an unrecognised id means "something closed and
    the record cannot say which leg". Believing a leg is still open when it
    is not is what halts dealing.
    """
    three_legs(store)
    write(store, action="close", status="CLOSED", deal_id="unknown", level=None)

    assert orders.open_orders() == {}


def test_the_record_and_the_account_agree_across_a_partial_close(store):
    """The property, end to end, through ``reconcile`` itself."""
    three_legs(store)
    write(store, action="close", status="CLOSED", deal_id="a", level=None)

    account = _aggregate_positions([
        Leg("SPGI", "SELL", 8.64, "b"), Leg("SPGI", "SELL", 8.67, "c"),
    ])
    unexplained, missing = reconcile(account, orders.open_orders())

    assert unexplained == () and missing == (), (
        "the record and the account still disagree after a partial close, "
        "which is what halted the bridge on 2026-09-10"
    )


# ------------------------------------------------------------------ arrivals

def test_an_arrival_is_recorded_from_the_broker(store):
    """The mirror of a departure, which had to be improvised at a console."""
    written = orders.record_arrival(
        "SPGI", Leg("SPGI", "SELL", 8.64, "b", level=413.14), now="2026-09-10"
    )

    assert written == 1
    live = orders.open_orders()
    assert live["SPGI"]["size"] == pytest.approx(8.64)
    assert live["SPGI"]["direction"] == "SELL"

    row = [json.loads(line) for line in store.read_text().splitlines()][-1]
    assert row["level"] == pytest.approx(413.14), "the level was not recorded"
    assert row["reconciled"] is True, (
        "an arrival must be distinguishable from an order this app placed"
    )


def test_an_arrival_without_a_level_is_refused(store):
    """M990: a row the reader ignores is worse than no row.

    ``_confirmed`` requires a level on an open. Writing one without would
    report success, change nothing, and leave the disagreement in place --
    this project's inert-gate failure in a new spot.
    """
    written = orders.record_arrival(
        "SPGI", Leg("SPGI", "SELL", 8.64, "b", level=None), now="2026-09-10"
    )

    assert written == 0
    assert orders.open_orders() == {}


# ---------------------------------------------------------------- the side

class Order:
    """What the broker said it did."""

    def __init__(self, direction, size):
        self.direction = direction
        self.size = size


class Ask:
    """What the app asked for."""

    def __init__(self, action, target, held=0.0):
        self.action = action
        self.target_units = target
        self.held_units = held


def test_an_open_records_the_side_the_broker_filled():
    """The trap that inverted a live position on 2026-09-10.

    The side used to come from the SIGN of the caller's target. A console
    script recording two short SPGI legs passed an unsigned 8.64 -- because
    a size read off the broker is a magnitude -- and the record said BUY for
    a position the account held SELL. `reconcile` compares epics, so it
    reported clean.
    """
    units, side = orders._resulting_position(
        Ask("open", 8.64), Order("SELL", 8.64)
    )

    assert side == "SELL", (
        "the record takes the side from the caller's sign convention, so an "
        "unsigned target on a short inverts a live position"
    )
    assert units == pytest.approx(8.64)


def test_a_signed_target_still_works():
    """The old convention must keep working where callers use it."""
    units, side = orders._resulting_position(
        Ask("open", -8.64), Order("SELL", 8.64)
    )
    assert (units, side) == (pytest.approx(8.64), "SELL")

    units, side = orders._resulting_position(
        Ask("open", 8.64), Order("BUY", 8.64)
    )
    assert (units, side) == (pytest.approx(8.64), "BUY")


def test_the_sign_is_the_fallback_when_the_broker_says_nothing():
    """Absence named: no fill direction means fall back, not guess."""
    units, side = orders._resulting_position(
        Ask("open", -8.64), Order("", 8.64)
    )
    assert side == "SELL"


def test_a_resize_still_takes_its_side_from_the_delta():
    """A resize goes as an OPPOSING order, so the fill side is not the position.

    Trimming a long from 100 to 60 sends SELL 40 and the position stays
    LONG. Taking the side from the fill here would record it as short --
    which is the bug fixed on 2026-09-04, and it must not come back through
    this door.
    """
    units, side = orders._resulting_position(
        Ask("resize", 60.0, held=100.0), Order("SELL", 40.0)
    )

    assert (units, side) == (pytest.approx(60.0), "BUY"), (
        "a trimmed long is being recorded as a short"
    )
