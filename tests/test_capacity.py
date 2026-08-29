"""How many positions a balance can hold, and refusing to guess.

The count was fixed at eight, which suited one balance and no other. What
sets the ceiling is granularity rather than the strategy: a slot worth
barely one minimum deal size cannot be sized by conviction or trimmed, so
it quantises every decision to nothing.
"""

from __future__ import annotations

import pytest

from cfdinvest.risk.capacity import (
    GRANULARITY_MULTIPLE,
    Capacity,
    minimum_tickets,
    position_capacity,
)

TICKETS = {
    "A": 21.8, "B": 137.4, "C": 10.0, "D": 45.0, "E": 60.0,
    "F": 5.0, "G": 93.2, "H": 80.0, "I": 2.0, "J": 30.0,
}


def test_no_readings_is_not_computed_rather_than_defaulted():
    """The house rule. A capacity computed from nothing is the kind of
    number that looks measured and is not."""
    result = position_capacity(5000, {}, max_leverage=3.5)
    assert result.slots is None
    assert result.binding == "unmeasured"
    assert "minimum deal sizes" in result.note


def test_no_balance_is_not_computed_either():
    assert position_capacity(None, TICKETS).slots is None
    assert position_capacity(0, TICKETS).slots is None


def test_capacity_rises_with_the_balance():
    """The whole point: an account of five thousand and one of fifty
    thousand do not have the same capacity."""
    small = position_capacity(5_000, TICKETS, max_leverage=3.5, universe_size=5_000)
    large = position_capacity(50_000, TICKETS, max_leverage=3.5, universe_size=5_000)
    assert large.slots > small.slots
    # And roughly in proportion, since nothing else binds here.
    assert large.slots == pytest.approx(small.slots * 10, rel=0.05)


def test_every_slot_can_afford_the_instruments(monkeypatch):
    """The property the ceiling exists for, asserted rather than assumed."""
    result = position_capacity(
        5_000, TICKETS, max_leverage=3.5, universe_size=500
    )
    assert result.notional_per_slot >= GRANULARITY_MULTIPLE * result.ticket, (
        "a slot cannot afford several minimum tickets, so the position "
        "could not be sized at all"
    )


def test_one_expensive_name_does_not_collapse_the_book():
    """A percentile, not the maximum. One dear instrument the screen may
    never select must not set capacity for everything else."""
    with_outlier = dict(TICKETS, ZZZ=50_000.0)
    base = position_capacity(5_000, TICKETS, max_leverage=3.5, universe_size=500)
    outlier = position_capacity(5_000, with_outlier, max_leverage=3.5,
                                universe_size=500)
    assert outlier.slots >= base.slots * 0.5, (
        "a single expensive instrument collapsed capacity for the whole book"
    )


def test_capacity_never_exceeds_the_universe():
    result = position_capacity(
        500_000, TICKETS, max_leverage=3.5, universe_size=12
    )
    assert result.slots == 12
    assert result.binding == "universe"


def test_a_balance_too_small_for_one_slot_is_refused_not_rounded_up():
    """Refusing is the honest answer. Rounding to one slot would assert a
    position the account cannot actually size."""
    dear = {"X": 40_000.0, "Y": 50_000.0}
    result = position_capacity(1_000, dear, max_leverage=1.0)
    assert result.slots is None
    assert "cannot fund one slot" in result.note


def test_the_concentration_cap_sets_a_floor_on_useful_capacity():
    """With a 20% cap, fewer than five positions cannot deploy the book --
    but only where granularity can actually fund them."""
    cheap = {"A": 1.0, "B": 1.0}
    result = position_capacity(
        10_000, cheap, max_leverage=1.0,
        max_instrument_concentration=0.20, universe_size=3,
    )
    # The universe binds first here, and that is reported honestly rather
    # than being overridden by a floor the universe cannot supply.
    assert result.slots == 3
    assert result.binding == "universe"


def test_minimum_tickets_are_measured_not_defaulted():
    rows = [
        {"ticker": "AAA", "min_deal_size": 0.1, "offer": 200.0},
        {"ticker": "BBB", "min_deal_size": 1.0, "offer": 50.0},
        {"ticker": "CCC", "min_deal_size": None, "offer": 100.0},   # no size
        {"ticker": "DDD", "min_deal_size": 1.0},                     # no price
        {"ticker": "", "min_deal_size": 1.0, "offer": 10.0},         # no name
    ]
    tickets = minimum_tickets(rows)
    assert tickets == {"AAA": pytest.approx(20.0), "BBB": pytest.approx(50.0)}, (
        "a ticket was computed from a missing half, which is a default "
        "wearing a measurement's clothes"
    )


def test_the_latest_reading_wins():
    """Prices move; the newest reading describes what a trade costs now."""
    rows = [
        {"ticker": "AAA", "min_deal_size": 0.1, "offer": 100.0},
        {"ticker": "AAA", "min_deal_size": 0.1, "offer": 300.0},
    ]
    assert minimum_tickets(rows)["AAA"] == pytest.approx(30.0)


CURVE = {4: 52.5, 6: 61.7, 8: 74.4, 10: 86.4, 12: 83.7, 16: 62.5, 20: 40.5, 24: 26.0}


def test_the_peak_is_not_taken_on_its_own():
    """8, 10 and 12 all measure within the noise floor of each other. Taking
    the maximum makes the choice a coin-toss dressed as an optimisation."""
    from cfdinvest.risk.capacity import operating_size

    result = operating_size(CURVE)
    assert result.slots == 8, "the raw peak was taken without regard to noise"
    assert "10" in result.note and "86.4" in result.note, (
        "the peak it declined to take is not reported, so the choice cannot "
        "be argued with"
    )


def test_returns_first_takes_the_largest_the_evidence_permits():
    """The owner's stated preference, still bounded: it may not chase a
    peak that is not distinguishable from its neighbours."""
    from cfdinvest.risk.capacity import operating_size

    result = operating_size(CURVE, prefer="largest")
    assert result.slots == 12
    assert result.slots != 24, "returns-first ran past the evidence"


def test_sizes_the_account_cannot_fund_are_not_options():
    from cfdinvest.risk.capacity import operating_size

    result = operating_size(CURVE, ceiling=6, prefer="largest")
    assert result.slots == 6
    assert result.binding == "balance"


def test_a_curve_that_is_flat_within_noise_picks_by_preference_not_by_luck():
    """The failure mode this exists for: re-measuring a flat curve would
    otherwise move the book every time, paying turnover for sampling error."""
    from cfdinvest.risk.capacity import operating_size

    flat = {4: 70.0, 8: 72.0, 12: 71.0, 16: 69.5}
    assert operating_size(flat).slots == 4
    assert operating_size(flat, prefer="largest").slots == 16
    # And a small re-measurement must not move the answer.
    jittered = {4: 72.0, 8: 70.0, 12: 71.5, 16: 70.5}
    assert operating_size(jittered).slots == 4
    assert operating_size(jittered, prefer="largest").slots == 16


def test_an_empty_or_unfundable_curve_is_not_computed():
    from cfdinvest.risk.capacity import operating_size

    assert operating_size({}).slots is None
    assert operating_size(CURVE, ceiling=2).slots is None, (
        "a size below every measured option was rounded into one"
    )
