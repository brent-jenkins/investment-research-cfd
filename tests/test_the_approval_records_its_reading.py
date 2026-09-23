"""An approved open records what the last look read, not only a refusal.

The refusal fields shipped on 2026-09-19 and the first three days of data
produced this, over 669 "no longer clears the bar" refusals:

    engine band     n    live    gap
    <0.40          69   0.393  +0.006
    0.40-0.60     397   0.455  -0.000
    0.60-0.75      23   0.667   0.000
    >=0.75        180   0.448  -0.348

Agreement to a thousandth in every band except the top, which collapses by a
third. That is the 2026-09-10 provisional-bar signature, which read -0.324 on
n=2 at the time and +0.001 after the fix.

**AND IT CANNOT BE READ, BECAUSE THE SAMPLE IS THE REFUSALS.** A name the
gate approved recorded nothing, so the >=0.75 band above is that band's left
tail by construction -- names selected FOR having read low. Two pictures
survive it and they mean opposite things:

    continuous   the check agrees with the engine and noise pushes a few
                 marginal names under the bar. The gate working.
    bimodal      a name either agrees exactly or loses about a third. The
                 2026-09-10 failure, where the reading collapsed only at the
                 top and no name in the book could reach 0.75.

Twenty-four names opened in those three days, so something clears the bar --
which already rules out the total blockage of 2026-09-10 and rules out
nothing else.

THE TEST THAT NAMED THIS AND DID NOT COVER IT is in the sibling file.
`test_an_approval_also_carries_its_reading` asserts that `holds` RETURNS a
reading on approval, and its docstring says reading only refusals "would
describe the left tail of the live/engine gap and call it the whole
distribution". Then only the refusal path was wired. The check was on what
went in where the failure was in what came out -- the same shape as the five
recorded on 2026-08-29, and as the deploy re-exec.

NONE IS NOT ZERO, here too. The last look is consulted on OPENS ONLY, so a
close, a resize and a hold have nothing to report, and with ``recheck`` off
nothing reads anything. All of those record ``None``. A zero would say the
check ran and found a dead signal.

Mutations checked:

  M1780 the approval built without the reading again -- the censoring back.
        Exercised through the real `plan_orders`, never by reading the
        source: M1771 SURVIVED a source check on this very field, and M1762
        passed against a call replaced by ``true # market-open``.
  M1781 an unchecked approval recording 0.0 rather than None
  M1782 `cleared_median` measured against today's configuration instead of
        the bar recorded on each row -- the M1774 trap on the other half
  M1783 approvals folded into `shortfall_median`, mixing the two halves so
        neither can be read
  M1784 no approvals reported as a confident 0.0 rather than None
"""

from __future__ import annotations

import pandas as pd
import pytest

from cfdinvest.broker.capital.bridge import Intent
from cfdinvest.persist import orders as orders_store

from tests.test_broker_capital_bridge import _recheck, only, plan, signal


class _Report:
    halted = False
    halt_reason = ""
    balance = 5000.0

    def __init__(self, intents):
        self.intents = tuple(intents)


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "orders" / "orders.jsonl"
    path.parent.mkdir(parents=True)
    monkeypatch.setattr(orders_store, "_path", lambda config=None, account=None: path)
    return path


def approval(ticker, live, bar=0.75, engine=0.80):
    return Intent(
        ticker=ticker, epic=ticker, direction="long",
        held_units=0.0, target_units=10.0, action="open",
        reason="", price=10.0, entered_bars_ago=0,
        engine_confidence=engine, live_confidence=live, confidence_bar=bar,
    )


def refusal(ticker, live, bar=0.75, engine=0.80):
    return Intent(
        ticker=ticker, epic=ticker, direction="long",
        held_units=0.0, target_units=0.0, action="refused",
        reason="no longer clears the bar", price=10.0, entered_bars_ago=0,
        engine_confidence=engine, live_confidence=live, confidence_bar=bar,
    )


# --------------------------------------------------------------------- #
# The bridge, exercised for real.
# --------------------------------------------------------------------- #
def test_an_approved_open_carries_the_reading_that_approved_it() -> None:
    """M1780: the censoring restored.

    Through `plan_orders`, because a field that is never populated is the
    same as no field, and this project has shipped that twice.
    """
    # The engine's own reading has to come from the signal row, which is
    # where `plan_orders` reads it. `signal()` does not carry one, and
    # asserting against its absence would test the fixture.
    row = dict(signal("AAPL"), confidence=0.81)
    report = plan(
        [row], recheck=True, rechecked=_recheck(confidence=0.92),
        entry_confidence=0.75,
    )
    intent = only(report, "AAPL")

    assert intent.action == "open"
    assert intent.live_confidence == pytest.approx(0.92), (
        "the gate approved on a live reading and did not record it, so the "
        "approved half of the distribution is still invisible and the "
        "refused half cannot be interpreted"
    )
    assert intent.confidence_bar == pytest.approx(0.75)
    assert intent.engine_confidence == pytest.approx(0.81), (
        "without the engine's own reading the gap cannot be computed, and "
        "the gap is the whole diagnosis: the 2026-09-10 signature is "
        "live sitting a third below engine at the top of the range"
    )


def test_an_unchecked_approval_reports_none_and_never_zero() -> None:
    """M1781: absence recorded as a confident zero.

    With the last look off, nothing read anything. Zero would say the check
    ran and found a dead signal -- manufacturing the collapse the field
    exists to detect, on the side of the distribution that is meant to be
    the control.
    """
    report = plan([signal("AAPL")], recheck=False)
    intent = only(report, "AAPL")

    assert intent.action == "open"
    assert intent.live_confidence is None, (
        "an open nothing re-checked reported a reading anyway"
    )


def test_a_close_reports_no_reading_because_none_was_taken() -> None:
    """The last look is consulted on opens only; the rest must say so."""
    # The broker's own shape for a held position, as the other bridge tests
    # express it: size and side, not a signed unit count.
    held = {"AAPL": {"size": 10.0, "direction": "BUY", "deal_id": "d1"}}
    report = plan(
        [signal("AAPL", direction="short")], positions=held,
        recheck=True, rechecked=_recheck(),
    )
    intent = only(report, "AAPL")

    assert intent.action == "close"
    assert intent.live_confidence is None, (
        "a close recorded a live reading, but the gate never ran on it -- "
        "which would put a number into the distribution describing nothing"
    )


# --------------------------------------------------------------------- #
# The summary, which is where the two halves meet.
# --------------------------------------------------------------------- #
def test_the_summary_reports_both_halves(store) -> None:
    """M1783: approvals folded into the shortfall.

    They measure opposite sides of one bar, and averaging them together
    reports the distance from the bar as roughly nothing -- which is
    exactly the reassuring reading.
    """
    orders_store.record_plan(
        _Report([approval("AAA", 0.90), refusal("BBB", 0.45)]),
        now=pd.Timestamp("2026-09-23 14:00", tz="UTC"),
    )

    summary = orders_store.refusal_summary(
        now=pd.Timestamp("2026-09-23 18:00", tz="UTC")
    )

    assert summary["shortfall_sample"] == 1, (
        "the approval was counted as a refusal, so the two halves are mixed"
    )
    assert summary["shortfall_median"] == pytest.approx(0.30)
    assert summary["cleared_sample"] == 1, "the approval was not counted"
    assert summary["cleared_median"] == pytest.approx(0.15), (
        "the approved reading sat 0.15 ABOVE its bar and was not reported"
    )


def test_the_cleared_median_uses_each_rows_own_bar(store) -> None:
    """M1782: measured against today's configuration instead.

    The M1774 trap on the other half. Two approvals taken at different bars
    -- what a gate change produces in one store -- read against a single
    current bar average to nonsense.
    """
    orders_store.record_plan(
        _Report([approval("AAA", 0.80, bar=0.75)]),
        now=pd.Timestamp("2026-09-23 14:00", tz="UTC"),
    )
    orders_store.record_plan(
        _Report([approval("BBB", 0.40, bar=0.35)]),
        now=pd.Timestamp("2026-09-23 15:00", tz="UTC"),
    )

    summary = orders_store.refusal_summary(
        now=pd.Timestamp("2026-09-23 18:00", tz="UTC")
    )

    assert summary["cleared_sample"] == 2
    # 0.05 and 0.05 against their own bars. Against a single 0.75 bar the
    # second would read -0.35 and the median would be -0.15.
    assert summary["cleared_median"] == pytest.approx(0.05), (
        "the headroom was not measured against the bar recorded on each "
        "row, so a gate change silently corrupts every earlier approval"
    )


def test_no_approvals_reports_none(store) -> None:
    """M1784: a store that cannot say, reporting a number anyway."""
    orders_store.record_plan(
        _Report([refusal("AAA", 0.45)]),
        now=pd.Timestamp("2026-09-23 14:00", tz="UTC"),
    )

    summary = orders_store.refusal_summary(
        now=pd.Timestamp("2026-09-23 18:00", tz="UTC")
    )

    assert summary["cleared_median"] is None
    assert summary["cleared_sample"] == 0
    assert summary["shortfall_sample"] == 1, "the existing half must still work"


def test_the_two_halves_separate_marginality_from_collapse(store) -> None:
    """The point, stated as the reading it has to make possible.

    A continuous check cuts its own distribution at the bar: approvals sit
    just above it, refusals just below. A collapsing one splits: approvals
    agree with the engine, refusals land a third low, and nothing is in
    between. Only the second is a defect, and both raise the refusal rate.
    """
    orders_store.record_plan(
        _Report([
            approval("AAA", 0.78), approval("BBB", 0.77),
            refusal("CCC", 0.45), refusal("DDD", 0.44),
        ]),
        now=pd.Timestamp("2026-09-23 14:00", tz="UTC"),
    )

    summary = orders_store.refusal_summary(
        now=pd.Timestamp("2026-09-23 18:00", tz="UTC")
    )

    hole = summary["cleared_median"] + summary["shortfall_median"]
    assert hole > 0.25, (
        f"approvals sat {summary['cleared_median']:.3f} above the bar and "
        f"refusals {summary['shortfall_median']:.3f} below it, a hole of "
        f"{hole:.3f} through the bar -- the bimodal signature. A summary "
        f"that cannot show this hole cannot tell the 2026-09-10 failure "
        f"from a gate doing its job"
    )
