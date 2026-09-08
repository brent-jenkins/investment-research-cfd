"""The number shown and the reasoning shown must be the same computation.

Spotted by the owner on the Universe page, 2026-09-08. It read:

    Instruments held      25
    Chosen by             the curve (largest)
    ...peak 70 at 215.7%; 2 size(s) within 8pp: 70, 100

The metric came from ``resolve_target``, which applies the viability ceiling
and ``universe.max_target``. The caption came from ``operating_size(curve)``
with **no ceiling**, so it explained choosing 70 or 100 beside a metric
reading 25. Both halves were individually correct and the page as a whole
was false: a reader would conclude the curve chose 25, when the cap did.

It was about to get worse rather than better. On the re-measured curve the
unbounded answer is 100 at 490.4%, so the caption would have explained a
hundred-name book next to a metric reading 25 -- a fourfold disagreement, on
the page a person reads to understand what the account does, while the open
question is precisely whether that cap should be removed.

``explain_target`` computes it once and returns the reasoning with the
answer. The page reads both from that.

Mutations checked, each reintroduced as a bug and confirmed to fail:
  M450 the caption's answer computed without the ceiling again
  M451 bound_by always naming the curve, whatever actually bound
  M452 curve_slots computed WITH the ceiling, so the gap can never show
  M453 resolve_target computing its own answer instead of delegating
  M454 the page recomputing the size instead of reading the decision
"""

from __future__ import annotations

import pathlib

import pytest

from cfdinvest.universe.traded import explain_target, resolve_target


class Universe:
    def __init__(self, curve, max_target=None, target=None, preference="largest"):
        self.size_curve = curve
        self.max_target = max_target
        self.target = target
        self.size_preference = preference
        self.candidates = ()


class Settings:
    """No margin or spread store, so the viability ceiling cannot compute and
    falls back to what is held -- which is the deployment's own behaviour on
    a machine with no recorded balance, and the reason this is testable."""

    def __init__(self, universe):
        self.universe = universe
        self.strategy = type("S", (), {
            "max_leverage": 3.5, "max_instrument_concentration": 0.20})()
        self.paths = type("P", (), {"data_root": "/nonexistent"})()


# The re-measured curve: monotone, no peak, so the unbounded answer is 100.
CURVE = {8: 57.0, 12: 82.5, 16: 104.9, 25: 159.2, 40: 240.9, 70: 401.3,
         100: 490.4}


def test_the_answer_and_the_reasoning_come_from_one_computation():
    """M453: two owners is how the page came to contradict itself."""
    # The fallback is deliberately NOT the answer. An earlier version of this
    # test passed 25 with the answer also 25, so a resolve_target returning
    # the fallback verbatim satisfied it -- a vacuous assertion of exactly the
    # kind this project keeps finding.
    settings = Settings(Universe(CURVE, max_target=25))
    decision = explain_target(settings, 40)

    assert decision.slots != 40, "the fixture cannot distinguish the two"
    assert decision.slots == resolve_target(settings, 40), (
        "resolve_target and explain_target disagree, which is the two-owner "
        "split that produced a page explaining a decision it did not make"
    )


def test_the_curve_alone_is_reported_and_can_exceed_the_answer():
    """M452: the gap is the whole point.

    ``curve_slots`` must ignore every ceiling. If it is computed with them it
    always equals the answer, the page can never show that the measurement
    wanted something else, and the defect returns silently.
    """
    settings = Settings(Universe(CURVE, max_target=25))
    decision = explain_target(settings, 25)

    assert decision.curve_slots == 100, (
        "the unbounded curve answer is not being reported; on this curve the "
        "measurement wants the whole pool"
    )
    assert decision.slots < decision.curve_slots


def test_the_cap_is_named_when_the_cap_is_what_bound():
    """M451: 'chosen by the curve' when the curve was overruled.

    This is the exact string the owner saw. It is the difference between a
    page saying a measurement chose 25 and one saying a deliberate stop did.
    """
    settings = Settings(Universe(CURVE, max_target=25))
    decision = explain_target(settings, 25)

    assert "max_target" in decision.bound_by, decision.bound_by
    assert "curve" not in decision.bound_by, (
        "the cap bound the answer and the page would credit the curve"
    )

    # M450: the caption's own words. This is the string the owner read --
    # "peak 70 at 215.7%; 2 size(s) within 8pp: 70, 100" beside a metric
    # saying 25. The note must describe the BOUNDED decision, so it has to
    # name the size actually held and must not name the unbounded one.
    assert str(decision.slots) in decision.curve_note, decision.curve_note
    assert str(decision.curve_slots) not in decision.curve_note, (
        f"the note describes the unbounded answer ({decision.curve_slots}) "
        f"while the page shows {decision.slots}: {decision.curve_note}"
    )


def test_the_curve_is_named_when_nothing_overruled_it():
    """The mirror. Without it the test above passes on a constant string."""
    settings = Settings(Universe(CURVE, max_target=None))
    decision = explain_target(settings, 100)

    assert decision.slots == 100
    assert "curve" in decision.bound_by, decision.bound_by
    assert "max_target" not in decision.bound_by


def test_a_pinned_target_says_a_person_chose_it():
    """A number somebody typed must never read as a measurement."""
    settings = Settings(Universe(CURVE, max_target=25, target=12))
    decision = explain_target(settings, 25)

    assert decision.slots == 12
    assert decision.bound_by == "a person"


def test_no_curve_is_reported_as_no_measurement():
    """Absence named. A count held for want of evidence is not a derived one."""
    settings = Settings(Universe({}, max_target=25))
    decision = explain_target(settings, 9)

    assert decision.slots == 9
    assert "measurement" in decision.bound_by, decision.bound_by
    assert decision.curve_slots is None


def test_the_page_reads_the_decision_rather_than_recomputing_it():
    """M450/M454: the regression, asserted structurally.

    The page must not build its own answer from ``operating_size``. That is
    how the caption came to describe a different decision from the metric,
    and nothing about the two numbers being wrong was visible in either.
    """
    page = (pathlib.Path(__file__).resolve().parents[1]
            / "cfdinvest" / "ui" / "views" / "04_Universe.py").read_text(
                encoding="utf-8")

    assert "explain_target" in page, (
        "the Universe page no longer reads the shared decision"
    )
    assert "operating_size(" not in page, (
        "the Universe page computes its own universe size again. That is the "
        "two-owner split: whatever it computes is not what the daemon does, "
        "and the page will explain a decision nobody made."
    )


def test_the_cap_binds_even_when_the_balance_cannot_be_read():
    """The hole the mutation testing found.

    ``max_target`` used to be applied only inside ``_viability_ceiling``, on
    its success path. A run that could not read a balance fell back to the
    traded count and the cap was not applied at all -- so the documented
    "most names to hold, whatever the return curve says" could be exceeded
    by the one path most likely to be taken after an outage.

    It never bit because the fallback is smaller than the cap in every
    shipped configuration, which is what correct-by-coincidence looks like.
    """
    settings = Settings(Universe(CURVE, max_target=25))
    # A fallback ABOVE the cap. There is no margin store here, so the ceiling
    # takes its fallback path and hands back 40.
    decision = explain_target(settings, 40)

    assert decision.slots <= 25, (
        f"max_target is 25 and the book resolved to {decision.slots}; the cap "
        "was bypassed by the ceiling's fallback path"
    )


def test_an_unmeasured_ceiling_does_not_claim_the_balance_decided():
    """Absence named, in the label a person reads.

    The viability ceiling falls back to what is held when it cannot read a
    balance. Labelling that "the viability ceiling, from the balance and deal
    sizes" is a confident sentence about a measurement that did not happen --
    and it is the label that would appear on the Universe page after any
    outage that emptied the margin store.
    """
    settings = Settings(Universe(CURVE, max_target=25))
    decision = explain_target(settings, 12)

    assert decision.slots == 12
    assert "for want of a balance" in decision.bound_by, decision.bound_by
    assert "from the balance" not in decision.bound_by, (
        "no balance was read and the page would say one decided"
    )
