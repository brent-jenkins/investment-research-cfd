"""At its ceiling, the book chooses rather than dilutes.

The owner's question on 2026-09-07 was about the other side of this: a book
with free cash should not open lower-quality trades just because it can. It
does not -- ``_target_units`` sizes each position independently from its own
signal and conviction, nothing normalises across the book, and idle cash
stays idle. Free cash is never a trigger to open anything.

The mirror case was the one that was wrong. Once gross notional breached
``max_leverage`` the engine scaled the WHOLE book proportionally, holdings
included, so a trade that just scraped the entry bar was never refused: it
opened, and every existing position was trimmed pro rata to fund it. The
trimming was conviction-blind -- a 0.76 entry diluted a 0.95 position by the
same percentage -- and spread was paid to shrink the good ones.

Two divergences from the dealing path came out of it, in one pair of rules:

  * **Order was inverted.** ``bridge.plan_orders`` defers surplus opens by
    conviction and THEN applies the gross ceiling, so the survivors are sized
    against the room the refusals freed. The engine capped and then deferred.
  * **Scope differed.** The bridge shrinks only ``open`` and ``resize``
    intents, so a held position sitting at ``none`` passes through untouched.
    The engine scaled everything.

Both comments cited the other as their authority and the order between them
was inverted, which is why ``test_engine_models_dealing`` could not catch it:
the parameter exists on both sides, and only the sequence differed.

Mutations checked, each reintroduced as a bug and confirmed to fail:
  M430 the deferral moved back after the gross cap
  M431 selection scaling the held book as well as the new opens
  M432 the ranking reversed, so the strongest open is dropped first
  M433 an absent confidence sorting last, so an unranked name keeps its place
  M434 selection falling back to scaling when the held book is not supplied
  M435 the flag read but never changing the answer
"""

from __future__ import annotations

import inspect

import numpy as np
import pytest

from cfdinvest.execution import engine
from cfdinvest.execution.engine import _apply_leverage_cap


class Config:
    """Only the fields the cap reads. A real ExecutionConfig would drag the
    whole engine in for a function that takes four numbers."""

    def __init__(self, max_leverage=2.5, selects=True):
        self.max_leverage = max_leverage
        self.leverage_cap_selects = selects


PRICE = np.array([100.0, 100.0, 100.0])
EQUITY = 1_000.0  # at 2.5x, a ceiling of 2,500


def test_the_weakest_new_open_is_dropped_not_the_strongest():
    """M432: rank the wrong way and the book keeps its worst trade.

    Three opens of 1,000 each against a 2,500 ceiling: one must go, and it
    must be the one with least conviction behind it.
    """
    target = np.array([10.0, 10.0, 10.0])
    held = np.zeros(3)
    confidence = np.array([0.95, 0.40, 0.80])

    out = _apply_leverage_cap(
        target, PRICE, EQUITY, Config(), held=held, confidence=confidence
    )

    assert out[1] == 0.0, "the least-convinced open survived"
    assert out[0] == pytest.approx(10.0), "the strongest open was shrunk"
    assert out[2] == pytest.approx(10.0), "a survivor was shrunk anyway"
    assert float(np.abs(out * PRICE).sum()) <= 2_500.0


def test_a_held_position_is_not_trimmed_to_fund_a_new_one():
    """M431: the dilution this whole change exists to remove.

    A 2,000 position already held, and a 1,000 open arriving on top of a
    2,500 ceiling. The open is refused; the holding does not move.
    """
    target = np.array([20.0, 10.0, 0.0])
    held = np.array([20.0, 0.0, 0.0])
    confidence = np.array([0.90, 0.76, 0.0])

    out = _apply_leverage_cap(
        target, PRICE, EQUITY, Config(), held=held, confidence=confidence
    )

    assert out[0] == pytest.approx(20.0), (
        "the held position was trimmed to make room for a new open, which is "
        "the conviction-blind dilution this replaces"
    )
    assert out[1] == 0.0, "the new open was funded by shrinking the book"


def test_scaling_is_still_the_default_and_still_dilutes():
    """M435: a flag that reads but does not change the answer.

    The contrast case. Without the flag the same book dilutes the holding,
    which is what every recorded figure was measured on -- so this asserts
    the OLD behaviour deliberately, and the two tests together prove the
    flag is what separates them.
    """
    target = np.array([20.0, 10.0, 0.0])
    held = np.array([20.0, 0.0, 0.0])
    confidence = np.array([0.90, 0.76, 0.0])

    out = _apply_leverage_cap(
        target, PRICE, EQUITY, Config(selects=False),
        held=held, confidence=confidence,
    )

    assert out[0] < 20.0, "the default stopped scaling the held book"
    assert out[1] > 0.0, "the default stopped funding the marginal open"
    assert float(np.abs(out * PRICE).sum()) == pytest.approx(2_500.0)


def test_an_unranked_open_is_dropped_before_a_ranked_one():
    """M433: 'no reading' treated as conviction.

    A nan score is not a low score and it is not a high one -- it is the
    name nobody can rank, which is the one to drop when room is short.
    """
    target = np.array([10.0, 10.0, 10.0])
    held = np.zeros(3)
    confidence = np.array([0.40, np.nan, 0.80])

    out = _apply_leverage_cap(
        target, PRICE, EQUITY, Config(), held=held, confidence=confidence
    )

    assert out[1] == 0.0, "the unranked open kept its place in the book"
    assert out[0] == pytest.approx(10.0)


def test_selection_refuses_to_run_without_what_it_ranks_on():
    """M434: falling back to scaling when the inputs are missing.

    An inert gate reporting a clean pass is this project's most-repeated
    failure. Selection without a held book or a confidence row can only
    scale, which is the behaviour it exists to replace, so it raises rather
    than quietly becoming the thing it replaced.
    """
    target = np.array([10.0, 10.0, 10.0])

    with pytest.raises(ValueError, match="held book"):
        _apply_leverage_cap(target, PRICE, EQUITY, Config())

    with pytest.raises(ValueError, match="held book"):
        _apply_leverage_cap(
            target, PRICE, EQUITY, Config(), held=np.zeros(3), confidence=None
        )


def test_a_book_already_over_its_ceiling_is_left_to_drain():
    """Equity falls under a book that is already held, and nothing is cut.

    Same deliberate choice as ``max_open_positions``: never liquidate into a
    drawdown. The account behaves this way -- CLAUDE.md records that the
    gross ceiling has never fired live, because the bridge shrinks only
    ``open`` and ``resize`` intents.
    """
    target = np.array([20.0, 15.0, 0.0])     # 3,500 held, unchanged
    held = np.array([20.0, 15.0, 0.0])
    confidence = np.array([0.90, 0.85, 0.0])

    out = _apply_leverage_cap(
        target, PRICE, 1_000.0, Config(), held=held, confidence=confidence
    )

    assert out[0] == pytest.approx(20.0)
    assert out[1] == pytest.approx(15.0)
    assert float(np.abs(out * PRICE).sum()) == pytest.approx(3_500.0), (
        "a book that drifted above its ceiling was liquidated back into it"
    )


def test_the_deferral_runs_before_the_gross_cap_as_the_dealing_path_does():
    """M430: the order that was inverted, asserted on both sides.

    Read from the source rather than by composing the calls, because the
    property is *where* they sit. Asserting only the engine would let the
    bridge move instead and the divergence reopen from the other end, which
    is exactly how this one survived -- each comment cited the other.
    """
    from cfdinvest.broker.capital import bridge

    engine_source = inspect.getsource(engine.run_execution)
    assert (
        engine_source.index("_defer_surplus_opens(")
        < engine_source.index("_apply_leverage_cap(")
    ), (
        "the engine caps before it defers, so the whole book is shrunk to fit "
        "positions that are then dropped anyway -- and the dealing path does "
        "the opposite, so the backtest stops describing the account"
    )

    bridge_source = inspect.getsource(bridge.plan_orders)
    assert (
        bridge_source.index("max_opens_per_pass is not None")
        < bridge_source.index("max_total_exposure_gbp")
    ), (
        "the dealing path now caps before it defers, which is the order the "
        "engine was just moved off"
    )
