"""The standard evaluation: the same questions, asked the same way.

This exists because every performance figure in this project used to come
from a throwaway script, which is how two tables came to disagree about "the
full nineteen" while both were correct -- one used the shipped entry
threshold and the other replaced it, and nothing recorded which.

These tests are less about arithmetic than about the battery refusing to
flatter. Each one reintroduces a way a report could mislead.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from cfdinvest.analytics.evaluate import (
    SLIPPAGE_STEPS,
    Evaluation,
    _curve_stats,
    _interpolate_breakeven,
    render,
)


def curve(values, start="2024-01-01", freq="D"):
    index = pd.date_range(start, periods=len(values), freq=freq, tz="UTC")
    return pd.Series(values, index=index)


# --------------------------------------------------------------------------- #
# The arithmetic
# --------------------------------------------------------------------------- #
def test_a_flat_curve_has_no_return_and_no_drawdown():
    stats = _curve_stats(curve([100.0] * 400))
    assert stats["cagr"] == pytest.approx(0.0, abs=1e-9)
    assert stats["max_drawdown"] == pytest.approx(0.0, abs=1e-9)


def test_a_doubling_over_a_year_is_reported_as_such():
    stats = _curve_stats(curve(list(np.linspace(100.0, 200.0, 366))))
    assert stats["cagr"] == pytest.approx(100.0, rel=0.02)
    assert stats["total_return"] == pytest.approx(100.0, rel=0.01)
    assert stats["max_drawdown"] == pytest.approx(0.0, abs=1e-9)


def test_a_curve_too_short_to_judge_reports_absence_not_zero():
    """M195: two data points given a CAGR.

    Absence is named here as everywhere else. A zero would read as
    "measured and flat" rather than "not measurable".
    """
    stats = _curve_stats(curve([100.0, 101.0]))
    assert stats["cagr"] is None
    assert stats["max_drawdown"] is None
    assert stats["sharpe"] is None

    empty = _curve_stats(None)
    assert empty["cagr"] is None


def test_the_drawdown_is_peak_to_trough_not_start_to_end():
    """A curve that recovers still had the drawdown."""
    stats = _curve_stats(curve([100.0, 150.0, 75.0] + [150.0] * 400))
    assert stats["max_drawdown"] == pytest.approx(-50.0, rel=0.01)


# --------------------------------------------------------------------------- #
# The break-even, which is the figure that decides reachability
# --------------------------------------------------------------------------- #
def test_the_breakeven_is_where_the_strategy_stops_beating_the_benchmark():
    """M196: a break-even read off the wrong crossing.

    Not where the CAGR hits zero -- where it drops to what buy-and-hold of
    the same instruments returned. A strategy that makes 10% while the
    universe made 25% has no edge left, whatever its sign.
    """
    # 100% at no friction, falling to -20% at 10bp; benchmark 25%.
    steps = [(0.0, 100.0), (2.0, 70.0), (4.0, 40.0), (6.0, 10.0), (10.0, -20.0)]
    breakeven = _interpolate_breakeven(steps, 25.0)

    assert breakeven is not None
    assert 4.0 < breakeven < 6.0, breakeven
    # 40 -> 10 across 4bp to 6bp; 25 is halfway, so 5bp.
    assert breakeven == pytest.approx(5.0, abs=0.1)


def test_a_strategy_that_never_beats_the_benchmark_has_no_breakeven():
    steps = [(0.0, 10.0), (2.0, 5.0), (4.0, 0.0)]
    assert _interpolate_breakeven(steps, 25.0) is None


def test_no_benchmark_means_no_breakeven_rather_than_a_guess():
    steps = [(0.0, 100.0), (4.0, 10.0)]
    assert _interpolate_breakeven(steps, None) is None


def test_the_slippage_curve_spans_the_range_that_matters():
    """It has to reach past the break-even or it cannot show one."""
    assert SLIPPAGE_STEPS[0] == 0.0, "the frictionless case is the reference"
    assert max(SLIPPAGE_STEPS) >= 10.0, (
        "the curve stops before the edge dies, so no break-even can be found"
    )
    assert list(SLIPPAGE_STEPS) == sorted(SLIPPAGE_STEPS)


# --------------------------------------------------------------------------- #
# The report refuses to flatter
# --------------------------------------------------------------------------- #
def test_the_report_always_carries_the_benchmark():
    """M197: a return quoted without what the universe did on its own.

    This universe was assembled with hindsight and rose about 25% a year
    over the window. A headline without that beside it is the single most
    misleading thing this project could print.
    """
    text = render(Evaluation(
        headline={"cagr": 120.0, "max_drawdown": -18.0, "sharpe": 2.4,
                  "trips": 2000, "trips_per_year": 1000.0, "win_rate": 57.0},
        benchmark={"cagr": 24.7, "max_drawdown": -24.5},
    ))
    assert "BUY AND HOLD" in text
    assert "24.7" in text
    assert "difference" in text.lower()
    # And the difference is stated rather than left to be worked out.
    assert "95.3" in text or "95.4" in text


def test_a_failed_shuffle_control_is_called_out_in_the_notes():
    """The control exists to be able to fail.

    If random directions beat the real signal often, the direction carries
    no information -- and a report that printed the numbers without saying
    so would be handing over a conclusion nobody drew.
    """
    from cfdinvest.analytics import evaluate as module

    trips = pd.DataFrame({
        "pct": [1.0, -1.0, 0.5, -0.5] * 25,
        "side": ["long", "short"] * 50,
        "won": [1.0, 0.0] * 50,
        "notional": [1000.0] * 100,
        "pnl": [10.0, -10.0] * 50,
        "asset": ["AAA"] * 100,
        "confidence": [0.5] * 100,
    })
    # A near-zero real sum: random draws will beat it about half the time.
    real = float(trips.pct.sum())
    assert abs(real) < 1e-6

    rng = np.random.default_rng(1)
    values = trips.pct.to_numpy()
    draws = np.array([
        float((values * rng.choice([1.0, -1.0], size=len(values))).sum())
        for _ in range(module.SHUFFLE_DRAWS)
    ])
    beat = int((draws >= real).sum())
    assert beat > module.SHUFFLE_DRAWS * 0.05, (
        "the fixture is meant to be indistinguishable from noise"
    )


def test_absent_figures_render_as_absent_not_as_zero():
    """A report of an evaluation that could not run must not read as flat."""
    text = render(Evaluation(
        headline={"cagr": None, "max_drawdown": None, "sharpe": None, "trips": 0},
        benchmark={"cagr": None},
        notes=("no signals were produced; nothing to evaluate",),
    ))
    assert "—" in text
    assert "0.0%" not in text.split("NOTES")[0], "an absent figure was drawn as zero"
    assert "nothing to evaluate" in text


def test_the_side_split_is_shown_because_long_only_is_beta():
    text = render(Evaluation(sides={
        "long": {"trips": 1456, "share": 69.0, "win_rate": 57.4, "total_pct": 621.7},
        "short": {"trips": 669, "share": 31.0, "win_rate": 55.9, "total_pct": 229.4},
    }))
    assert "BY SIDE" in text
    assert "long" in text and "short" in text
    assert "669" in text


def test_the_evaluate_command_is_registered():
    """A battery nothing can call is a battery nobody runs."""
    from cfdinvest.analytics.cli import build_parser

    parser = build_parser()
    actions = [a for a in parser._actions if hasattr(a, "choices") and a.choices]
    commands = set()
    for action in actions:
        commands.update(action.choices or {})
    assert "evaluate" in commands, "the evaluate command is not registered"
