"""The stale-intent loop is counted, not inferred.

When the account never gets a position the engine entered, the engine goes
on intending it and every later pass attempts an open the last look
correctly refuses. Safe, noisy, and recorded on 2026-09-10 as the reason the
refusal rate looked alarming -- 47.1% blocked outright against 20.7%
measured in the account's first week.

It was diagnosed by reading order rows and reasoning about them, which
cannot separate "the engine is chasing an entry the account never got" from
"the signal decayed and the gate did its job". Both look like a refusal.

`entered_bars_ago` separates them. Zero means the engine opened the position
on the newest completed bar, which is a fresh opportunity. Anything larger
means the engine has been holding while the account has not, and an open
planned from that row is a chase.

**Reported, not enforced.** Refusing on a non-zero age would suppress
entries in a book where 20% of trips produce all the profit, and nothing yet
measures which refusals are the loop. This makes the measurement possible;
it does not pre-empt it.

Mutations checked:

  M1670 the age always reported as zero, so no chase is ever visible
  M1671 the age counted from the start of the run rather than from entry
  M1672 a sign flip counted as the same position continuing
  M1673 a name flat on the final bar given an age anyway
  M1674 the field dropped from the intent row
"""
from __future__ import annotations

import pandas as pd
import pytest

from cfdinvest.persist import intent


def frame(rows: dict[str, list[float]], bars: int) -> pd.DataFrame:
    index = pd.date_range("2026-09-11 13:00", periods=bars, freq="h", tz="UTC")
    return pd.DataFrame(rows, index=index)


def test_a_position_opened_on_the_newest_bar_is_age_zero() -> None:
    """M1670: an age that is always zero hides every chase."""
    positions = frame({"AAA": [0.0, 0.0, 5.0]}, 3)

    assert intent._entry_ages(positions) == {"AAA": 0}


def test_a_position_the_engine_has_held_carries_its_age() -> None:
    """M1671: the age is bars since ENTRY, not bars since the run began."""
    positions = frame({"AAA": [0.0, 5.0, 5.0, 5.0]}, 4)

    # Entered on the second bar of four, so two completed bars have passed.
    assert intent._entry_ages(positions) == {"AAA": 2}


def test_a_position_held_for_the_whole_run_does_not_overrun_the_frame() -> None:
    """The walk stops at the first bar rather than indexing past it."""
    positions = frame({"AAA": [5.0, 5.0, 5.0]}, 3)

    assert intent._entry_ages(positions) == {"AAA": 2}


def test_a_sign_flip_is_a_new_position() -> None:
    """M1672: long becoming short is an entry, not a continuation.

    Counting it as continuing would report a fresh short as old and hide
    exactly the case this exists to show.
    """
    positions = frame({"AAA": [-5.0, -5.0, 7.0]}, 3)

    assert intent._entry_ages(positions) == {"AAA": 0}


def test_a_name_that_is_flat_has_no_age() -> None:
    """M1673: a flat name is not intended, so it cannot be stale."""
    positions = frame({"AAA": [5.0, 5.0, 0.0], "BBB": [0.0, 0.0, 2.0]}, 3)

    ages = intent._entry_ages(positions)
    assert "AAA" not in ages
    assert ages == {"BBB": 0}


def test_an_empty_frame_yields_no_ages() -> None:
    assert intent._entry_ages(pd.DataFrame()) == {}
    assert intent._entry_ages(None) == {}


def test_the_intent_row_carries_the_age() -> None:
    """M1674: the field must reach the caller, or nothing can count it."""
    import inspect

    source = inspect.getsource(intent.intended_positions)
    # Anchored with the colon. `"entered_bars_ago"` alone is a SUBSTRING of
    # `"entered_bars_ago_renamed"`, so the loose form passes against a
    # renamed field -- which is how this test first failed its own mutation,
    # and the same substring trap that once passed a unit-file assertion
    # against a commented-out line.
    assert '"entered_bars_ago":' in source, (
        "intended_positions must carry entered_bars_ago, or the stale-intent "
        "loop stays a matter of opinion"
    )
    assert "_entry_ages" in source, (
        "the age must be computed from the positions frame rather than "
        "assumed"
    )


def test_the_age_is_reported_and_not_enforced() -> None:
    """It must not silently start refusing opens.

    Suppressing entries in a book where 20% of trips produce all the profit
    is a strategy change, and this is a measurement. If a future change wires
    it into the dealing path, that is a deliberate act with its own evidence
    -- and this test should be the thing that makes somebody say so.
    """
    import inspect

    source = inspect.getsource(intent)
    lowered = source.lower()
    for forbidden in ("refuse", "skip", "reject"):
        for line in lowered.splitlines():
            if "entered_bars_ago" in line and forbidden in line:
                pytest.fail(
                    f"intent.py appears to act on entered_bars_ago ({line!r}); "
                    "it is reported, not enforced"
                )
