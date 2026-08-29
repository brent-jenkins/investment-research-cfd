"""Capacity is a limit of its own, not a side effect of the universe.

Nothing in the engine counted positions. Holding the traded list to eight
names was the only thing keeping the book to eight, so one knob answered two
questions -- which instruments are worth watching, and how many may be held
at once -- and answered the second by deciding the first a month in advance.

Separating them measured better: all forty-nine candidates eligible with
eight slots returned 13.6% against 7.5% for a monthly list of eight, ahead
in both halves. The gain is eligibility rather than the cap; the cap is what
makes eligibility affordable on an account that cannot hold thirty-one
positions.

M410 the limit configured and never enforced
M411 an incumbent displaced by a better-ranked newcomer, paying a round trip
     to act on a difference the ranking cannot resolve
M412 a refused newcomer recorded as held, so the position frame claims a
     holding the book never took
M413 the limit applied after the sizing caps, so the book is sized for
     members it is about to lose
M414 a lowered limit flattening a live book instead of letting it drain
M415 absent configuration changing behaviour, so every recorded run moves
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from cfdinvest.execution import ExecutionConfig, run_backtest
from cfdinvest.execution.engine import _apply_position_limit
from cfdinvest.preprocessing.frame import AnalysisFrame
from cfdinvest.signals.frame import SignalFrame


class Config:
    def __init__(self, limit):
        self.max_open_positions = limit


def book(names=12, bars=40, seed=0):
    index = pd.date_range("2026-08-10", periods=bars, freq="h", tz="UTC")
    columns = [f"N{i}" for i in range(names)]
    rng = np.random.default_rng(seed)
    price = pd.DataFrame(
        100 * np.exp(np.cumsum(rng.normal(0, 0.01, (bars, names)), axis=0)),
        index=index, columns=columns,
    )
    signals = SignalFrame(
        direction=pd.DataFrame("long", index=index, columns=columns),
        size=pd.DataFrame(1.0, index=index, columns=columns),
        confidence=pd.DataFrame(0.9, index=index, columns=columns),
        reason=pd.DataFrame("", index=index, columns=columns),
    )
    analysis = AnalysisFrame(
        prices={"open": price, "high": price, "low": price, "close": price}
    )
    return signals, analysis


def test_the_limit_is_enforced_end_to_end():
    """M410: configured, resolved, and never actually applied."""
    signals, analysis = book(names=12)
    for limit in (3, 6, 8):
        result = run_backtest(signals, analysis, ExecutionConfig(
            initial_equity=10_000.0, max_leverage=5.0,
            max_open_positions=limit,
        ))
        held = (result.positions != 0).sum(axis=1)
        assert held.max() <= limit, (
            f"limit {limit} but {int(held.max())} positions were open"
        )
        assert held.max() == limit, (
            f"limit {limit} never actually bound; the fixture is too small"
        )


def test_absent_configuration_changes_nothing():
    """M415: a default that silently moves every run ever recorded."""
    signals, analysis = book(names=12)
    settings = dict(initial_equity=10_000.0, max_leverage=5.0)
    default = run_backtest(signals, analysis, ExecutionConfig(**settings))
    explicit = run_backtest(signals, analysis, ExecutionConfig(
        max_open_positions=None, **settings))

    assert ExecutionConfig().max_open_positions is None
    assert (default.positions != 0).sum(axis=1).max() == 12
    pd.testing.assert_frame_equal(default.positions, explicit.positions)


def test_an_incumbent_is_not_displaced_by_a_better_newcomer():
    """M411: churn dressed up as optimisation.

    The newcomer wants more size, so a rule that simply took the largest
    would swap them. The incumbent is the one already earning, and the
    ranking is not accurate enough to justify paying a round trip for the
    difference.
    """
    target = np.array([1.0, 5.0])        # incumbent small, newcomer large
    held_now = np.array([1, 1], dtype=np.int8)
    units = np.array([1.0, 0.0])         # only the first is currently open

    kept, directions = _apply_position_limit(
        target, held_now, units, Config(1)
    )
    assert kept[0] != 0.0, "the incumbent was displaced by a newcomer"
    assert kept[1] == 0.0, "the newcomer took a slot that was not free"
    assert directions[1] == 0


def test_a_freed_slot_is_filled_by_the_best_newcomer():
    """Capacity is the constraint, so a slot that frees must be reused."""
    target = np.array([0.0, 2.0, 5.0])   # incumbent has closed this bar
    held_now = np.array([0, 1, 1], dtype=np.int8)
    units = np.array([3.0, 0.0, 0.0])

    kept, _ = _apply_position_limit(target, held_now, units, Config(1))
    assert kept[2] != 0.0, "the freed slot went unfilled"
    assert kept[1] == 0.0, "the weaker newcomer took the slot"


def test_a_refused_newcomer_is_flat_not_held():
    """M412: the position frame recording a holding never taken."""
    target = np.array([4.0, 3.0])
    held_now = np.array([1, 1], dtype=np.int8)
    units = np.array([0.0, 0.0])

    kept, directions = _apply_position_limit(target, held_now, units, Config(1))
    refused = kept == 0.0
    assert refused.any()
    assert (directions[refused] == 0).all(), (
        "a refused entry kept its direction, so it reads as a live position"
    )


def test_lowering_the_limit_closes_nothing_at_all():
    """M414, restated: the limit bounds opening and never closing.

    The first version of this fix stopped the book being flattened whole
    and still zeroed the surplus -- three incumbents with room for two lost
    the smallest, and the comment above the code called that "letting them
    close naturally" when zeroing the units *is* the close.

    It matters more now than it did. Capacity is computed from the balance,
    so the limit falls on its own whenever the account does, and the old
    behaviour would liquidate a position for an administrative reason at a
    moment the strategy did not choose. The book is allowed to sit above
    the limit and drains as positions exit on their own terms -- the same
    rule a departing instrument already follows.
    """
    target = np.array([5.0, 3.0, 1.0])
    held_now = np.array([1, 1, 1], dtype=np.int8)
    units = np.array([5.0, 3.0, 1.0])

    kept, directions = _apply_position_limit(target, held_now, units, Config(2))

    assert int((kept != 0).sum()) == 3, (
        "an open position was closed to satisfy a limit that had been "
        "lowered under it"
    )
    assert list(kept) == [5.0, 3.0, 1.0]
    assert list(directions) == [1, 1, 1]


def test_the_limit_still_refuses_newcomers_while_it_is_breached():
    """Draining is not the same as ignoring the limit. While incumbents
    alone fill or exceed it, nothing new may open."""
    # Two incumbents, one newcomer, room for two.
    target = np.array([5.0, 3.0, 4.0])
    held_now = np.array([1, 1, 1], dtype=np.int8)
    units = np.array([5.0, 3.0, 0.0])

    kept, _ = _apply_position_limit(target, held_now, units, Config(2))

    assert kept[0] != 0.0 and kept[1] != 0.0, "an incumbent was displaced"
    assert kept[2] == 0.0, (
        "a newcomer opened into a book that was already at its limit -- and "
        "it ranked above one of the incumbents, which is exactly the swap "
        "that pays a round trip to act on a difference the ranking cannot "
        "resolve"
    )


def test_a_limit_of_zero_opens_nothing_and_closes_nothing():
    """Zero is a real instruction, distinct from absent -- and it means
    "open nothing", not "close everything". A capacity of zero is what a
    balance too small to size a single slot produces, and liquidating the
    book on it would turn a sizing constraint into a stop-out."""
    target = np.array([5.0, 3.0, 2.0])
    held_now = np.array([1, 1, 1], dtype=np.int8)
    units = np.array([5.0, 3.0, 0.0])          # two held, one newcomer

    kept, directions = _apply_position_limit(target, held_now, units, Config(0))

    assert list(kept) == [5.0, 3.0, 0.0], (
        "a limit of zero closed open positions instead of refusing new ones"
    )
    assert list(directions) == [1, 1, 0]


def test_slots_are_applied_before_the_sizing_caps():
    """M413: sizing a book for members it is about to lose.

    Asserted on the engine's own ordering rather than by composing the two
    functions by hand, because the property is *where* the call sits.
    """
    import inspect

    from cfdinvest.execution import engine

    source = inspect.getsource(engine.run_execution)
    slots = source.index("_apply_position_limit(")
    concentration = source.index("_apply_concentration_cap(")
    leverage = source.index("_apply_leverage_cap(")
    assert slots < concentration < leverage, (
        "the position limit runs after the sizing caps, so the book is "
        "sized including positions that are then refused"
    )


def test_the_limit_leaves_room_for_the_concentration_cap():
    """The two limits answer different questions and must both bind.

    With six slots at 20% concentration a book can still reach its gross
    ceiling; a slot limit that quietly capped exposure as well would be a
    second leverage control nobody configured.
    """
    signals, analysis = book(names=12)
    result = run_backtest(signals, analysis, ExecutionConfig(
        initial_equity=10_000.0, max_leverage=5.0,
        max_open_positions=6, concentration=0.40,
    ))
    notional = result.notionals.abs()
    gross = notional.sum(axis=1)
    live = gross > 0
    assert live.any()
    top = (notional.max(axis=1)[live] / gross[live]).max()
    assert top <= 0.45, f"the concentration cap stopped binding, top share {top:.2f}"
    assert (notional[live] != 0).sum(axis=1).max() == 6
