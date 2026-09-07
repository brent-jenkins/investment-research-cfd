"""The balance has to matter, and until now it did not.

Every cost the engine charges is a fraction of notional, so nothing in the
model responded to the size of the account. A 1,000 book and a 100,000 book
produced the same CAGR to the decimal -- and only one of them can place the
orders, because a broker has a minimum deal size and the engine had no notion
of one.

That is why "how many names should the book hold" could never be answered
from the backtest. The size curve is one table for every balance, and the
owner's requirement is the opposite: the app should choose a few large
positions or many small ones depending on what the balance can actually
fund. It cannot choose on evidence that does not distinguish the two.

The rule modelled is ``bridge._round_down``: down to the increment, never up,
because a size rounded up can breach the limit it was computed from. Capital
treats ``min_deal_size`` as floor and step at once -- 0.1 units on NVDA.

The ticket cost varies by a factor of 700 across the 63 instruments recorded
on the account -- 0.19 on BKNG against 137.36 on MRNA -- so this is per
instrument. A constant could not say which names a small account may hold,
which is the only question it is here to answer.

Mutations checked, each reintroduced as a bug and confirmed to fail:
  M440 rounding up instead of down, so a size breaches the cap that made it
  M441 the rounding manufacturing a close on a held position
  M442 a deliberate close rounded into a hold
  M443 a missing increment read as "refuse" rather than "unconstrained"
  M444 shorts rounded away from zero instead of toward it
  M445 the increment read but never changing the answer
"""

from __future__ import annotations

import numpy as np
import pytest

from cfdinvest.execution.engine import _quantise_to_deal_size


class Config:
    def __init__(self, sizes=None):
        self.min_deal_sizes = sizes


COLUMNS = ["NVDA", "MRNA", "BKNG"]
STEP = {"NVDA": 0.1, "MRNA": 1.0}     # BKNG deliberately unrecorded


def quantise(target, held):
    return _quantise_to_deal_size(
        np.array(target, dtype="float64"), np.array(held, dtype="float64"),
        COLUMNS, Config(STEP),
    )


def test_an_open_the_account_cannot_fund_is_refused():
    """M445: the case the whole field exists for.

    A target of 0.4 units of MRNA against a 1.0 increment is not a small
    position, it is not a position. On a small balance this is what removes
    an expensive name from the universe, and nothing modelled it before.
    """
    out = quantise([0.35, 0.4, 5.0], [0.0, 0.0, 0.0])

    assert out[1] == 0.0, "an unfundable open survived"
    assert out[0] == pytest.approx(0.3), "a fundable open was not quantised"


def test_rounding_is_down_never_up():
    """M440: a size rounded up breaches the cap that produced it.

    The caps run before this, so rounding up would let the book exceed the
    ceiling it was just shrunk to -- making this function the cause of the
    problem it exists downstream of.
    """
    out = quantise([0.29, 2.9, 1.0], [0.0, 0.0, 0.0])

    assert out[0] == pytest.approx(0.2), "rounded up past the computed size"
    assert out[1] == pytest.approx(2.0), "rounded up past the computed size"


def test_a_held_position_is_never_closed_by_the_rounding():
    """M441: the model inventing an exit the account never makes.

    A position already open is fundable by construction -- it was opened. If
    a decayed trim or a cap takes its target under one increment, the honest
    outcome is that no trade happens, not that the book sells out.
    """
    out = quantise([0.05, 0.4, 1.0], [0.0, 2.0, 0.0])

    assert out[1] == pytest.approx(2.0), (
        "the rounding closed a held position, which is an exit the account "
        "would never have placed"
    )
    # 0.05 is half of NVDA's 0.1 increment and the book is flat there, so it
    # is refused -- the same arithmetic, and the held flag is what separates
    # the two outcomes.
    assert out[0] == 0.0, "a flat name was rescued as though it were held"


def test_a_deliberate_close_still_closes():
    """M442: refusing an exit is never the safe direction.

    The guard above must tell a rounded-away open from an intended close.
    One is the model being unable to act; the other is the strategy acting.
    """
    out = quantise([0.0, 0.0, 0.0], [5.0, 3.0, 1.0])

    assert list(out) == [0.0, 0.0, 0.0], "a close was rounded into a hold"


def test_an_unrecorded_increment_does_not_refuse_the_name():
    """M443: absence named, not absence treated as a veto.

    Refusing what cannot be measured reads as caution and would empty the
    book on the first run against a fresh spread store -- the opposite
    failure from the usual one, and just as wrong.
    """
    out = quantise([0.0, 0.0, 0.0001], [0.0, 0.0, 0.0])

    assert out[2] == pytest.approx(0.0001), (
        "an instrument with no recorded increment was refused"
    )


def test_shorts_round_toward_zero():
    """M444: away from zero is rounding up, wearing a minus sign."""
    out = quantise([-0.29, -2.9, 0.0], [0.0, 0.0, 0.0])

    assert out[0] == pytest.approx(-0.2)
    assert out[1] == pytest.approx(-2.0)


def test_no_increments_configured_is_the_shipped_behaviour():
    """The default must reproduce every recorded figure exactly."""
    target = np.array([0.35, 0.4, 5.0])
    out = _quantise_to_deal_size(
        target, np.zeros(3), COLUMNS, Config(None)
    )
    assert np.allclose(out, target)


def test_the_balance_now_changes_the_book():
    """The property the whole change exists to create.

    Same strategy, same increments, two balances. Before this the engine
    produced identical books because every cost is a fraction of notional;
    a model that cannot tell 1,000 from 100,000 cannot be asked which one
    should hold more names.
    """
    small = quantise([0.04, 0.4, 0.0], [0.0, 0.0, 0.0])     # a 1,000 book
    large = quantise([4.0, 40.0, 0.0], [0.0, 0.0, 0.0])     # a 100,000 book

    assert int((small != 0).sum()) == 0, "the small book funded nothing"
    assert int((large != 0).sum()) == 2, "the large book was constrained"
