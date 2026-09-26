"""A held position keeps its size, as it does on the account.

This file replaces `test_the_decayed_trim_compounds.py`, which pinned the
opposite on purpose.

THE DEFECT, recorded 2026-09-15 and left in place then. The engine decided
whether to keep a held position BEFORE the decayed trim and the confidence
sizing ran, so it set the target to the units held and then multiplied
THAT:

    keep         = (new_held == held) & (new_held != 0) & ~resize
    target_units = np.where(keep, units, target_units)      <- target := HELD
    target_units = _trim_decayed(target_units, units, ...)  <- x 0.5, again
    target_units = _size_by_confidence(target_units, ...)   <- x this hour's reading

The trim halved a held position on every bar -- the median trip exited at
18% of its entry size -- and confidence sizing rescaled it on every bar with
each hour's reading. The 2026-09-15 entry found the trim and left it: "these
tests exist so it cannot change quietly in either direction".

WHY IT IS CHANGED NOW, on 2026-09-26. Replaying the live period against the
account (`research.replay`) found the harness holding an eighth of the
account's exposure from the second hour of every trip -- AMAT opened at
18.87 units and was 2.36 an hour later -- and 277 of its 438 trades in nine
days were resizes, where the account made 7 in three weeks. The account
does none of it: the dealer compares the engine's target with what is held
under this same 3.0 threshold, finds the drift inside it, and keeps full
size. A backtest that sizes differently from the account cannot describe
it, and making the backtest describe the account is the job.

So the keep is judged AFTER the trim and the sizing, which now shape the
fresh target rather than the position held.

WHAT THIS MEANS FOR THE TRIM. At the shipped `rebalance_threshold` of 3.0 a
half-size target is a drift of 0.5 and never trips it, so the trim no longer
moves a held position in the engine -- exactly as it never has on the
account. The 2026-09-01 sweep that chose `decayed_position_multiplier = 0.5`
measured the compounding version, a rule nobody described; it is not
evidence for the trim as the account runs it, which is not at all.

Mutations checked:

  M1840 the keep moved back before the trim and the sizing (the old order)
  M1841 the keep made unconditional, ignoring the rebalance threshold
  M1842 confidence sizing removed, so entry size no longer reflects conviction
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
OPENS_AT = 12


@pytest.fixture
def analysis():
    """One instrument on a quiet, gently rising path.

    Low volatility, so the volatility-scaled target cannot move far enough to
    trip the rebalance threshold on its own.
    """
    calendar = pd.bdate_range("2021-01-04", periods=BARS, tz="UTC", name="timestamp")
    close = 100 * np.exp(np.cumsum(np.full(BARS, 0.0004)))
    tidy = pd.DataFrame(
        {"open": close, "high": close * 1.001, "low": close * 0.999,
         "close": close, "volume": 1e6, "ticker": "SPX"},
        index=calendar,
    ).set_index("ticker", append=True).sort_index()
    return build_analysis_frame(
        tidy, interval="1d", config=PreprocessConfig(correlation_window=5)
    )


def signals_for(analysis, *, confidences, opening=0.90):
    """Flat, then long: ``opening`` on the entry bar, then ``confidences`` cycled."""
    calendar, columns = analysis.calendar, analysis.close.columns
    direction = pd.DataFrame("flat", index=calendar, columns=columns)
    size = pd.DataFrame(0.0, index=calendar, columns=columns)
    confidence = pd.DataFrame(0.0, index=calendar, columns=columns)

    direction.iloc[OPENS_AT:] = "long"
    size.iloc[OPENS_AT:] = 0.50
    confidence.iloc[OPENS_AT] = opening
    after = len(calendar) - OPENS_AT - 1
    confidence.iloc[OPENS_AT + 1:] = np.resize(np.asarray(confidences, float), after)[:, None]

    return SignalFrame(
        direction=direction, size=size, confidence=confidence,
        reason=pd.DataFrame("test", index=calendar, columns=columns),
    )


def config(**overrides) -> ExecutionConfig:
    """The shipped trim, sizing and threshold; everything else that moves units off."""
    base = ExecutionConfig(
        thresholds=HysteresisThresholds(entry_confidence=0.75, exit_confidence=0.20),
        decayed_position_multiplier=0.5,
        confidence_sizing_strength=0.75,
        rebalance_threshold=3.0,
        time_stop_days=None,
        default_spread_bps=0.0,
        slippage_fraction=0.0,
    )
    return dataclasses.replace(base, **overrides)


def held_units(analysis, signals, settings) -> np.ndarray:
    series = run_backtest(signals, analysis, settings).positions["SPX"].fillna(0.0)
    return series[series != 0.0].to_numpy(dtype=float)


def test_a_decayed_position_is_not_trimmed_again_and_again(analysis):
    """M1840: the finding of 2026-09-15, now the behaviour that must NOT run."""
    units = held_units(analysis, signals_for(analysis, confidences=[0.40]), config())

    assert len(units) >= 4, "the position closed before the trim could be observed"
    assert np.allclose(units[1:6], units[0]), (
        "a held position below the entry bar changed size bar after bar: "
        f"{units[:6]}. The account keeps it at full size, so a backtest that "
        "shrinks it models an eighth of the account's exposure"
    )


def test_a_held_position_is_not_rescaled_by_each_hours_confidence(analysis):
    """M1840: the second half of the same fault.

    Confidence swinging above and below the entry bar rescaled a held
    position on every bar -- 4.38, 9.58, 4.79 units -- and paid for each
    step. The account never places those orders.
    """
    units = held_units(
        analysis, signals_for(analysis, confidences=[0.90, 0.40, 0.80, 0.50]), config()
    )
    assert np.allclose(units[1:8], units[0]), (
        f"a held position was resized with each bar's confidence: {units[:8]}"
    )


def test_the_threshold_still_decides_when_a_position_moves(analysis):
    """M1841: keeping must stay conditional on drift.

    With drift rebalancing switched ON (a threshold of 0.25), a half-size
    target is a drift of 0.5 and the position IS cut -- once. It then sits
    at half, because the trim now shapes the fresh target rather than the
    held one: the rule its commit describes, "halve a position you would no
    longer open", and not the compounding version.
    """
    units = held_units(
        analysis, signals_for(analysis, confidences=[0.40]),
        config(rebalance_threshold=0.25, confidence_sizing_strength=None),
    )
    assert abs(units[1]) < 0.75 * abs(units[0]), (
        f"a drift past the threshold did not resize the position: {units[:4]}"
    )
    assert np.allclose(units[2:6], units[1]), (
        f"after one cut the position went on shrinking: {units[:6]}"
    )


def test_conviction_still_sizes_the_entry(analysis):
    """M1842: moving the keep must not remove confidence sizing from entries."""
    strong = held_units(analysis, signals_for(analysis, confidences=[0.9], opening=0.95), config())
    weaker = held_units(analysis, signals_for(analysis, confidences=[0.9], opening=0.80), config())

    assert abs(strong[0]) > abs(weaker[0]), (
        "an entry at 0.95 confidence opened no larger than one at 0.80, so "
        "confidence sizing no longer reaches the entry"
    )
