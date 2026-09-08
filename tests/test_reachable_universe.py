"""A universe size the screen cannot fill must never reach the config.

On 2026-09-08 `universe.max_target` was raised from 25 to 70 during a live
session. Nine sectors at five apiece can fill 38 of the 100 candidates, so
the screen returned 38, `traded_universe` refused a selection whose size did
not match the intended 70, and the account fell back to `market.tickers` --
**eight names**, worse than the 25 it started from. It was caught in minutes
only because the change was verified afterwards.

The deeper fault was that research and production disagreed about what the
screen selects. Every size-curve figure this project recorded was swept over
the RAW ranking, taking its first N; the screen skips names whose sector is
full and reaches further down. Measured on the day: the harness's top thirty
and the screen's actual thirty shared **eight names**. The harness held
twenty-two technology names where the account held a book across nine
sectors. Two different strategies wearing one number.

So the selection rule now lives in one place, `select_within_sector_cap`,
and both callers use it.

Mutations checked, each reintroduced as a bug and confirmed to fail:
  M460 a configured target above what the sector cap can fill
  M461 the cap ignored, so the ranking is taken top-N as the harness did
  M462 reachable_size ignoring the cap and returning the whole map
  M463 an unmapped instrument counted against a sector
  M464 the cap counting selected names rather than names per sector
"""

from __future__ import annotations

import pytest

from cfdinvest.config import load_config
from cfdinvest.universe.screen import (
    effective_sector_cap,
    reachable_size,
    select_within_sector_cap,
)


def test_the_configured_target_is_reachable_with_the_configured_cap():
    """The check that would have stopped the live session going wrong.

    Reads the shipped configuration rather than a fixture, because the
    failure was a configured value and only the real one can reproduce it.
    """
    settings = load_config()
    candidates = set(settings.universe.candidates or ())
    sectors = {t: s for t, s in (settings.universe.sectors or {}).items()
               if t in candidates}
    # Resolved through the screen's own rule, NOT read literally. A config
    # value of None means "the screen's default of 5", so reading it
    # literally concludes 100 names are reachable and waves through a target
    # of 70 that can only be filled to 38 -- which is the mutation this test
    # first failed to catch.
    cap = effective_sector_cap(settings)
    target = getattr(settings.universe, "max_target", None)

    if not sectors or target is None:
        pytest.skip("no sector map or no cap configured")

    reachable = reachable_size(sectors, cap)
    assert target <= reachable, (
        f"universe.max_target is {target} but the screen can only fill "
        f"{reachable} names from {len(candidates)} candidates at "
        f"max_per_sector={cap}. Asking for more returns fewer, "
        "traded_universe refuses the size mismatch whole, and the account "
        "falls back to market.tickers -- which is how a live session ended "
        "up on eight names."
    )


def test_the_cap_makes_the_screen_reach_further_down_the_ranking():
    """M461: taking the top N is what the research harness did.

    The property is that a capped selection is NOT a prefix of the ranking.
    Without this the shared function could quietly become `ordered[:target]`
    and every figure measured through it would silently describe the wrong
    book again.
    """
    ordered = [("A", "tech"), ("B", "tech"), ("C", "tech"),
               ("D", "energy"), ("E", "tech"), ("F", "health")]

    selected, capped = select_within_sector_cap(ordered, target=4, cap=2)

    assert selected == ["A", "B", "D", "F"], selected
    assert capped == ["C", "E"], capped
    assert selected != [t for t, _ in ordered[:4]], (
        "the capped selection is a prefix of the ranking, so the cap did "
        "nothing -- which is exactly the harness bug"
    )


def test_names_passed_over_for_sector_are_reported_separately():
    """A name held back by concentration is not a name that ranked badly.

    The screen reports the two apart so a reader can tell 'not good enough'
    from 'good enough, but its sector was full'.
    """
    ordered = [("A", "tech"), ("B", "tech"), ("C", "energy")]
    selected, capped = select_within_sector_cap(ordered, target=3, cap=1)

    assert selected == ["A", "C"]
    assert capped == ["B"]


def test_an_unmapped_instrument_is_never_capped():
    """M463: absence named. An empty sector is not a sector.

    Counting unmapped names into one bucket would cap them against each
    other for having nothing in common, which is the opposite of what the
    limit is for.
    """
    ordered = [("A", ""), ("B", ""), ("C", ""), ("D", "tech")]
    selected, capped = select_within_sector_cap(ordered, target=4, cap=1)

    assert selected == ["A", "B", "C", "D"], selected
    assert capped == []


def test_the_cap_counts_per_sector_not_per_selection():
    """M464: a cap that counts total picks is a target, not a sector cap."""
    ordered = [("A", "tech"), ("B", "energy"), ("C", "tech"), ("D", "energy")]
    selected, _ = select_within_sector_cap(ordered, target=4, cap=2)

    assert selected == ["A", "B", "C", "D"], (
        "two per sector across two sectors is four names; a cap counting "
        "the selection would have stopped at two"
    )


def test_reachable_size_answers_what_the_cap_permits():
    """M462: the arithmetic the config check depends on."""
    sectors = {"a": "tech", "b": "tech", "c": "tech", "d": "energy"}

    assert reachable_size(sectors, cap=None) == 4
    assert reachable_size(sectors, cap=5) == 4       # cap above supply
    assert reachable_size(sectors, cap=2) == 3       # 2 tech + 1 energy
    assert reachable_size(sectors, cap=1) == 2


def test_a_target_beyond_supply_returns_what_exists_rather_than_erroring():
    """Asking for more than exists is answered honestly and quietly.

    The screen's own note reports the shortfall; this function's job is to
    return what it could fill, so the CALLER can compare sizes and refuse.
    Raising here would move the decision into the wrong layer.
    """
    ordered = [("A", "tech"), ("B", "energy")]
    selected, _ = select_within_sector_cap(ordered, target=10, cap=5)

    assert selected == ["A", "B"]
