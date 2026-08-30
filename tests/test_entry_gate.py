"""The breakout entry gate: it refuses openings and closes nothing.

Validated across four lookbacks -- trending-only beats always-on at every
one, by 1.1 to 1.5 points -- which is what separates it from the several
ideas that won at a single parameter and were fitted.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from cfdinvest.execution.engine import _apply_entry_gate
from cfdinvest.signals.breakout import DEFAULT_LOOKBACK, entry_mask


def test_a_shut_gate_refuses_new_positions():
    target = np.array([5.0, 3.0])
    held = np.array([1, 1], dtype=np.int8)
    units = np.array([0.0, 0.0])          # both would be opening

    kept, directions = _apply_entry_gate(
        target, held, units, np.array([True, False])
    )
    assert kept[0] == 5.0, "an allowed entry was refused"
    assert kept[1] == 0.0, "a refused entry opened anyway"
    assert directions[1] == 0, (
        "a refused newcomer kept its direction, so the position frame "
        "records a holding the book never took"
    )


def test_a_shut_gate_never_closes_an_incumbent():
    """The distinction that has been got wrong twice in the engine: a
    condition on *entering* must not be applied to something already in."""
    target = np.array([5.0, 3.0])
    held = np.array([1, 1], dtype=np.int8)
    units = np.array([5.0, 3.0])          # both already open

    kept, directions = _apply_entry_gate(
        target, held, units, np.array([False, False])
    )
    assert list(kept) == [5.0, 3.0], (
        "the gate closed open positions -- it governs opening only"
    )
    assert list(directions) == [1, 1]


def test_an_absent_gate_permits_everything():
    target = np.array([5.0, 3.0])
    kept, _ = _apply_entry_gate(
        target, np.array([1, 1], dtype=np.int8), np.array([0.0, 0.0]), None
    )
    assert list(kept) == [5.0, 3.0]


def test_the_range_excludes_the_bar_being_traded():
    """Without the shift every new high trivially clears a window that
    contains it -- lookahead by arithmetic, and it would make the filter
    look spectacular."""
    index = pd.date_range("2026-01-01", periods=40, freq="h", tz="UTC")
    close = pd.DataFrame({"A": np.arange(100.0, 140.0)}, index=index)
    direction = pd.DataFrame("long", index=index, columns=["A"])

    mask = entry_mask(close, direction, lookback=5)
    # On a monotone rise every bar is a new high against the PRIOR window,
    # so all are allowed -- but the value being compared must be the prior
    # window's max, not this bar's.
    highs = close.rolling(5).max().shift(1)
    assert bool((close["A"] > highs["A"]).iloc[10]), "the shift is missing"
    assert bool(mask["A"].iloc[10])


def test_the_filter_is_off_outside_the_named_regimes():
    """The requirement is added where it helps rather than removed where it
    hurts -- measured: applied in sideways markets it returns 73.0% against
    74.4% for no filter at all."""
    index = pd.date_range("2026-01-01", periods=60, freq="h", tz="UTC")
    close = pd.DataFrame({"A": 100 + np.sin(np.arange(60))}, index=index)
    direction = pd.DataFrame("long", index=index, columns=["A"])

    trending = entry_mask(close, direction,
                          pd.Series(["uptrend"] * 60, index=index), lookback=10)
    sideways = entry_mask(close, direction,
                          pd.Series(["sideways"] * 60, index=index), lookback=10)

    assert int(sideways["A"].sum()) == 60, "the filter bound in a sideways market"
    assert int(trending["A"].sum()) < 60, "the filter did not bind in a trend"


def test_an_unknown_regime_leaves_the_filter_off():
    """Absence is not evidence a trend is running. Refusing entries because
    the label is missing would be inferring from silence."""
    index = pd.date_range("2026-01-01", periods=60, freq="h", tz="UTC")
    close = pd.DataFrame({"A": 100 + np.sin(np.arange(60))}, index=index)
    direction = pd.DataFrame("long", index=index, columns=["A"])

    mask = entry_mask(close, direction,
                      pd.Series(["unknown"] * 60, index=index), lookback=10)
    assert int(mask["A"].sum()) == 60


def test_too_little_history_is_permitted_not_refused():
    """"Not enough history to test" is not "failed the test"."""
    index = pd.date_range("2026-01-01", periods=30, freq="h", tz="UTC")
    close = pd.DataFrame({"A": np.arange(100.0, 130.0)}, index=index)
    direction = pd.DataFrame("long", index=index, columns=["A"])

    mask = entry_mask(close, direction, lookback=DEFAULT_LOOKBACK)
    assert bool(mask["A"].iloc[0]), "the leading window refused entries"
