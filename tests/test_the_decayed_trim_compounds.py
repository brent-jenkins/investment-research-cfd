"""The decayed trim halves a position on EVERY bar, not once.

Found 2026-09-15 while chasing the turnover residual. The rule's own commit
(f947329, "Halve a position you would no longer open") says what it is for:
"Trimming to half leaves a position that continues to run, and the exit band
still decides when it goes." A position that is halved again on the next
bar, and again on the one after, does not continue to run -- it evaporates
while paying a spread crossing on each step down.

THE MECHANISM IS THE ORDER OF FOUR LINES IN THE ENGINE LOOP:

    resize       = needs_resize(units, target_units, threshold=3.0)
    keep         = (new_held == held) & (new_held != 0) & ~resize
    target_units = np.where(keep, units, target_units)      <- target := HELD
    target_units = _trim_decayed(target_units, units, confidence, config)

`rebalance_threshold` of 3.0 means a held position almost never counts as
drifted, so `keep` is True and the third line replaces the freshly computed
target with WHAT IS ALREADY HELD. `_trim_decayed` then multiplies that by
0.5. Next bar the held figure is the trimmed one, and it is halved again.

`rebalance_threshold` cannot stop it, and makes it worse: `needs_resize`
measures drift relative to CURRENT units, so as the position shrinks its
distance from the full conviction target grows. Once that ratio passes 3.0
the position is restored to full size and the decay starts over. The
measured shape over 11,058 held-bar pairs is that sawtooth:

    shrank past half   29.2%     <- 0.375x, being conviction 0.75 x trim 0.5
    halved             2.2%
    roughly unchanged  25.8%
    grew               34.3%     <- the jump back

    units at exit / at entry     median 0.1844
    trips exiting under 10% of entry size   29.2%

**The median trip exits holding 18% of the size it entered with.**

WHY THE EXISTING TESTS CANNOT SEE IT. `tests/test_entry_gate.py` has four
tests for this rule and all four call `_trim_decayed` ONCE with
``target == units``. The function is correct in isolation; the defect is in
the loop that calls it, and no test ran the loop across consecutive bars.
That is this project's recurring shape -- a check on what went in, where
the failure was in what came out.

IT WAS LATENT UNTIL THE GATE MOVED. The trim's threshold is
`entry_confidence` by deliberate design -- "one owner, and raising the entry
bar raises this with it". It was swept at 0.35, where a held position's
confidence often recovers above the bar and the trim stops firing. At the
0.75 shipped since 2026-09-04, against a median confidence of 0.34 among
names with a direction, it essentially never recovers. Same mechanism as
the breakout gate and as the last look's unreachable pass mark: the gate
moved and nothing re-checked what moved with it.

**THE ACCOUNT DOES NOT DO THIS**, which is the part that matters most.
`bridge.plan_orders` scores the same step with the same `needs_resize` at
the same threshold: a 0.375x trim is a drift of 0.625 against 3.0, so the
intent is `action="none", reason="inside the rebalance threshold"` and no
order is placed. The engine's book decays toward nothing while the account
holds full size -- so the backtest models a fraction of the exposure the
account carries, which is the direction that understates gross.

NOTHING IS CHANGED HERE. Correcting the order moves every recorded figure
in the project, including the sweep that chose `decayed_multiplier = 0.5`
itself -- that table measured the compounding version, so it says what it
says about a rule nobody described. Whether to correct it is the owner's
decision, and these tests exist so it cannot change quietly in either
direction, the way `entered_bars_ago` is reported and not enforced.

Mutations checked:

  M1730 the trim applied to the freshly computed target instead of to the
        held units -- the position sits at half and stays there, and three
        tests fail including `test_it_does_not_settle_at_half`.

        **The first attempt at this mutation did not mutate**, and it is
        recorded rather than quietly redone: it INSERTED a trim before
        `keep` while leaving the original call in place, so the original
        went on compounding and all five tests passed. A surviving mutation
        reported an inert test that is not inert. The mutation has to MOVE
        the call, not add one -- which is the same mistake made on
        2026-09-12 in `_completed_bar_end`, so it is now twice.

        It is also the fix, demonstrated: under that order the position
        settles at half, which is what the rule's commit describes.
  M1731 `_trim_decayed` made a no-op -- `test_a_held_position_shrinks_on_
        every_bar` fails, proving that test is about this rule and not
        about the sizing caps or the volatility target
  M1732 the trim's confidence condition dropped, so it fires on every held
        position -- the control in `test_the_decay_is_the_trim_and_nothing_
        else` fails, proving the entry bar is what gates it
  M1733 the multiplier hardcoded at 0.5 rather than read from the config --
        `test_the_multiplier_still_governs_the_size_of_each_step` fails,
        proving the parameter is not inert configuration
"""

from __future__ import annotations

import dataclasses

import numpy as np
import pandas as pd
import pytest

from cfdinvest.execution import ExecutionConfig, HysteresisThresholds, run_backtest
from cfdinvest.preprocessing import PreprocessConfig
from cfdinvest.preprocessing.frame import build_analysis_frame
from cfdinvest.signals.frame import SignalFrame

BARS = 40
#: The bar the position opens on. Before it the signal is flat, so the
#: opening is unambiguous and the decay afterwards has a known starting size.
OPENS_AT = 12


@pytest.fixture
def analysis():
    """One instrument on a quiet, gently rising path.

    Deliberately low volatility: the question is what the TRIM does, and a
    price that moves sharply would let the volatility-scaled target move
    enough to trip the rebalance threshold on its own.
    """
    calendar = pd.bdate_range("2021-01-04", periods=BARS, tz="UTC", name="timestamp")
    close = 100 * np.exp(np.cumsum(np.full(BARS, 0.0004)))
    tidy = pd.DataFrame(
        {
            "open": close, "high": close * 1.001, "low": close * 0.999,
            "close": close, "volume": 1e6, "ticker": "SPX",
        },
        index=calendar,
    ).set_index("ticker", append=True).sort_index()
    return build_analysis_frame(
        tidy, interval="1d", config=PreprocessConfig(correlation_window=5)
    )


def signals_for(analysis, *, confidence_after: float):
    """Flat, then long at 0.90, then long at ``confidence_after`` forever.

    0.90 clears the 0.75 entry bar so the position opens at full size.
    What follows is the interesting part: above `exit_confidence` so the
    band does not close it, and below `entry_confidence` so the trim fires.
    """
    calendar, columns = analysis.calendar, analysis.close.columns
    direction = pd.DataFrame("flat", index=calendar, columns=columns)
    size = pd.DataFrame(0.0, index=calendar, columns=columns)
    confidence = pd.DataFrame(0.0, index=calendar, columns=columns)

    direction.iloc[OPENS_AT:] = "long"
    size.iloc[OPENS_AT:] = 0.50
    confidence.iloc[OPENS_AT] = 0.90
    confidence.iloc[OPENS_AT + 1:] = confidence_after

    return SignalFrame(
        direction=direction, size=size, confidence=confidence,
        reason=pd.DataFrame("test", index=calendar, columns=columns),
    )


def config(**overrides) -> ExecutionConfig:
    """The shipped shape, with everything that could confound switched off.

    No time stop, no stops, no conviction sizing and no costs: any of them
    would also move a held position's units, and the claim is about the
    trim specifically.
    """
    base = ExecutionConfig(
        thresholds=HysteresisThresholds(
            entry_confidence=0.75, exit_confidence=0.20,
        ),
        decayed_position_multiplier=0.5,
        rebalance_threshold=3.0,          # shipped: drift rebalancing OFF
        confidence_sizing_strength=None,
        time_stop_days=None,
        default_spread_bps=0.0,
        slippage_fraction=0.0,
    )
    return dataclasses.replace(base, **overrides)


def held_units(analysis, signals, settings) -> np.ndarray:
    """The units actually held, on every bar the position is open."""
    result = run_backtest(signals, analysis, settings)
    series = result.positions["SPX"].fillna(0.0)
    return series[series != 0.0].to_numpy(dtype=float)


def test_a_held_position_shrinks_on_every_bar(analysis):
    """The finding itself, stated as the behaviour that runs today.

    Not "it is halved" -- it is halved AGAIN AND AGAIN. Three consecutive
    steps down is enough to distinguish compounding from a one-off trim and
    short enough not to depend on the sawtooth's restore.
    """
    units = held_units(analysis, signals_for(analysis, confidence_after=0.40), config())

    assert len(units) >= 4, (
        "the position did not stay open long enough to observe the trim; "
        "something other than the trim closed it"
    )
    steps = np.abs(units[1:4]) / np.abs(units[0:3])
    assert np.all(steps < 0.75), (
        "a held position below the entry bar did not shrink on consecutive "
        f"bars: successive ratios were {steps}. If this test has started "
        "failing, the engine's sizing order has changed and every figure "
        "measured before today describes a different book"
    )


def test_it_does_not_settle_at_half(analysis):
    """M1730: the trim applied to the fresh target rather than to the held.

    That is what the rule's commit message describes -- halve what you would
    open, and let it run -- and it would leave the position sitting at half
    its full size indefinitely. The measured behaviour goes far below that,
    and this is the assertion that tells the two apart.
    """
    units = held_units(analysis, signals_for(analysis, confidence_after=0.40), config())
    opening = abs(units[0])

    assert abs(units[3]) < 0.30 * opening, (
        "by the fourth bar the position is still above 30% of its opening "
        "size, which is the one-off trim the rule documents rather than the "
        f"compounding one measured: {units[:4]}"
    )


def test_the_decay_is_the_trim_and_nothing_else(analysis):
    """M1731, M1732: prove the cause rather than the symptom.

    Two controls, because a shrinking position could equally be the sizing
    caps rescaling the book or the volatility target moving. With the trim
    switched off, and with confidence held above the entry bar, the same
    fixture must hold its size -- otherwise this file is measuring
    something else and says so.
    """
    without = held_units(
        analysis, signals_for(analysis, confidence_after=0.40),
        config(decayed_position_multiplier=None),
    )
    assert abs(without[3]) > 0.90 * abs(without[0]), (
        "with the trim disabled the position still shrank, so the decay is "
        f"not the trim: {without[:4]}"
    )

    above = held_units(
        analysis, signals_for(analysis, confidence_after=0.90), config()
    )
    assert abs(above[3]) > 0.90 * abs(above[0]), (
        "a position whose confidence stayed above the entry bar was trimmed "
        f"anyway: {above[:4]}"
    )


def test_the_multiplier_still_governs_the_size_of_each_step(analysis):
    """M1733: the multiplier ignored.

    A gentler multiplier must decay more slowly. This is what says the
    parameter is still connected to the behaviour -- an arm that decays
    identically at 0.5 and 0.9 would be inert configuration, which this
    project has shipped before and has a rule against.
    """
    steep = held_units(
        analysis, signals_for(analysis, confidence_after=0.40), config()
    )
    gentle = held_units(
        analysis, signals_for(analysis, confidence_after=0.40),
        config(decayed_position_multiplier=0.9),
    )

    assert abs(gentle[3]) / abs(gentle[0]) > abs(steep[3]) / abs(steep[0]), (
        "a multiplier of 0.9 decayed no more slowly than 0.5, so the "
        "parameter is not governing the step"
    )


def test_the_existing_unit_tests_cannot_see_this(analysis):
    """Why four passing tests missed it, asserted rather than narrated.

    `_trim_decayed` is correct: called once with ``target == units`` it
    returns exactly half. Every existing test calls it that way, so the
    function passes while the loop compounds. The defect is the order of
    the calls, and a test of the function can never reach it.
    """
    from cfdinvest.execution.engine import _trim_decayed

    settings = config()
    once = _trim_decayed(
        np.array([10.0]), np.array([10.0]), np.array([0.40]), settings
    )
    assert once[0] == 5.0, (
        "the function itself has changed behaviour; this file's reasoning "
        "about where the defect lives no longer applies"
    )

    # Feeding its own output back in is what the loop does.
    twice = _trim_decayed(once, once, np.array([0.40]), settings)
    assert twice[0] == 2.5, (
        "the function does not compound when fed its own output, so the "
        "loop cannot be doing what this file claims"
    )
