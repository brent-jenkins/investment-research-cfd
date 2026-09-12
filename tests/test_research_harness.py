"""The sweep checks itself, and cannot look forward.

The 2026-09-08 battery chose the universe the account trades and was run
from a script that was never kept, so on 2026-09-12 nothing could regenerate
its 191.0%. The harness exists to stop that recurring, and it is only worth
having if two things hold.

**It refuses to report an arm whose baseline does not reproduce.** CLAUDE.md
already requires that of a sweep. The requirement kept being met by hand, in
scripts that were then thrown away, which is the same as not having it.

**The screen cannot see the future.** A membership mask built from
selections dated after a bar would let the backtest trade names chosen with
hindsight, and a universe sweep is exactly where that would flatter the
result without looking wrong.

Mutations checked:

  M1680 assert_reproduces warns instead of raising
  M1681 the tolerance widened until anything passes
  M1682 an arm with no recorded figure read anyway
  M1683 absent metrics treated as a pass
  M1684 the mask taking the NEXT selection rather than the most recent
  M1685 bars before the first selection permitted rather than refused
  M1686 the research config borrowing for_live_intent's lag of 0
"""
from __future__ import annotations

import dataclasses

import pandas as pd
import pytest

from cfdinvest.research import harness


def test_an_arm_that_misses_its_recorded_figure_raises() -> None:
    """M1680, M1681: a sweep with a wrong baseline produces arms that are
    differences between two wrong numbers."""
    with pytest.raises(harness.ReproductionError) as caught:
        harness.assert_reproduces((8, 50), {"cagr": -0.842, "maxdd": -0.999,
                                            "trades": 38877})

    message = str(caught.value)
    assert "-84.2%" in message and "191.0%" in message, (
        "the error must name both figures, or nobody can act on it"
    )


def test_an_arm_that_lands_on_its_recorded_figure_passes() -> None:
    recorded = harness.RECORDED[(8, 50)]["cagr"]
    harness.assert_reproduces((8, 50), {"cagr": recorded, "maxdd": -0.066,
                                        "trades": 3219})


def test_the_tolerance_is_the_projects_own_noise_floor() -> None:
    """M1681: a tolerance nobody chose is a tolerance that grows.

    Asserted against the OWNER rather than against a number. This test first
    asserted 9.3 as a literal, which made the test a third copy of a
    threshold `risk.capacity` exists to own -- so a change to the floor would
    have failed here and been "fixed" by editing the literal, which is how
    two owners become three.
    """
    from cfdinvest.risk.capacity import NOISE_FLOOR_PP

    assert harness.REPRODUCTION_TOLERANCE_PP == pytest.approx(float(NOISE_FLOOR_PP))

    recorded = harness.RECORDED[(8, 50)]["cagr"]
    floor = harness.REPRODUCTION_TOLERANCE_PP / 100.0
    # Just inside, and just outside, expressed against the floor itself so
    # this keeps testing the property when the floor is re-derived.
    harness.assert_reproduces(
        (8, 50), {"cagr": recorded - floor * 0.9, "maxdd": 0.0}
    )
    with pytest.raises(harness.ReproductionError):
        harness.assert_reproduces(
            (8, 50), {"cagr": recorded - floor * 1.1, "maxdd": 0.0}
        )


def test_an_arm_with_no_recorded_figure_cannot_be_read() -> None:
    """M1682: sweeping something unrecorded and believing it is how a
    configuration gets adopted on a number nobody can check."""
    with pytest.raises(harness.ReproductionError):
        harness.assert_reproduces((3, 12), {"cagr": 5.0, "maxdd": -0.01})


def test_absent_metrics_are_not_a_pass() -> None:
    """M1683: "no equity curve" is not "reproduced"."""
    with pytest.raises(harness.ReproductionError):
        harness.assert_reproduces((8, 50), None)
    with pytest.raises(harness.ReproductionError):
        harness.assert_reproduces((8, 50), {})


def _index(days: int, start: str = "2026-09-01"):
    return pd.DatetimeIndex(
        pd.date_range(start, periods=days, freq="D", tz="UTC")
    )


def test_a_bar_takes_the_most_recent_selection_not_a_later_one() -> None:
    """M1684: the screen must not be read from the future.

    A bar on the 3rd is governed by the selection made on the 2nd, never by
    the one made on the 5th -- that is hindsight, and a universe sweep is
    where it would flatter the result most quietly.
    """
    index = _index(6)
    selections = {
        pd.Timestamp("2026-09-02", tz="UTC"): ["AAA"],
        pd.Timestamp("2026-09-05", tz="UTC"): ["BBB"],
    }

    mask = harness.membership_mask(selections, index, ["AAA", "BBB"])

    # The 3rd and 4th are governed by the 2nd's selection.
    assert bool(mask.loc[pd.Timestamp("2026-09-03", tz="UTC"), "AAA"])
    assert not bool(mask.loc[pd.Timestamp("2026-09-03", tz="UTC"), "BBB"])
    # From the 5th, the newer selection applies.
    assert bool(mask.loc[pd.Timestamp("2026-09-05", tz="UTC"), "BBB"])
    assert not bool(mask.loc[pd.Timestamp("2026-09-05", tz="UTC"), "AAA"])


def test_bars_before_the_first_selection_are_refused() -> None:
    """M1685: "no screen had run yet" is not "everything was permitted".

    Permitting them opens the whole candidate pool across the warm-up, which
    is the pool-much-larger-than-slots configuration this project measured
    as destructive.
    """
    index = _index(4)
    selections = {pd.Timestamp("2026-09-03", tz="UTC"): ["AAA"]}

    mask = harness.membership_mask(selections, index, ["AAA"])

    assert not bool(mask.loc[pd.Timestamp("2026-09-01", tz="UTC"), "AAA"])
    assert not bool(mask.loc[pd.Timestamp("2026-09-02", tz="UTC"), "AAA"])
    assert bool(mask.loc[pd.Timestamp("2026-09-03", tz="UTC"), "AAA"])


def test_an_empty_selection_yields_a_mask_that_permits_nothing() -> None:
    mask = harness.membership_mask({}, _index(3), ["AAA"])
    assert not mask.to_numpy().any()


def test_a_name_outside_the_panel_is_ignored() -> None:
    """A selection may name an instrument the price panel does not carry."""
    index = _index(2)
    selections = {pd.Timestamp("2026-09-01", tz="UTC"): ["AAA", "MISSING"]}

    mask = harness.membership_mask(selections, index, ["AAA"])
    assert list(mask.columns) == ["AAA"]
    assert bool(mask.iloc[0]["AAA"])


def test_the_research_config_does_not_borrow_the_live_lag() -> None:
    """M1686: execution_lag 0 fills on the bar the signal came from.

    `for_live_intent` sets it deliberately, because live you know the price
    you are dealing at. A backtest that borrows it is reading the future,
    and the engine says so itself.
    """
    import inspect

    source = inspect.getsource(harness.research_backtest)
    assert "INTENT_EXECUTION_LAG" not in source
    assert "execution_lag=0" not in source.replace(" ", "")


def test_the_research_config_models_the_session_flatten() -> None:
    """This configuration day trades, and the flatten is not a field.

    Without it a sweep measures the carry-overnight book the account stopped
    running on 2026-09-06.
    """
    import inspect

    source = inspect.getsource(harness.research_backtest)
    assert "FLATTEN_AT_SESSION_END" in source, (
        "a research run that does not flatten measures a different book"
    )
