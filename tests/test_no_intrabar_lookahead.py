"""No rule may decide using a bar's close and act at that bar's open.

The engine fills at a bar's OPEN under the default ``fill_mode``. Any rule
that tests a price and then trades on the same bar must therefore test
against the FILL price, never the close -- otherwise it chooses knowing how
the bar turned out and acts before it happened.

**The time stop did exactly that until 2026-09-04**, and it is 88% of all
exits. It was found by accident, while investigating an unrelated
profit-taking idea, and confirmed by direct measurement rather than by
comparing two runs:

    population                          n        mean      median
    bars a position was simply held  24,282   +0.1010%   +0.0624%
    bars the TIME STOP exited on      2,278   -0.9199%   -0.7948%

A difference of 1.02% per exit. On an ordinary held bar a position gained a
tenth of a percent; on a time-stop exit bar it lost nine-tenths. The median
says it was the typical exit rather than a few outliers. Correcting it moved
the measured strategy from +467.7% to -47.5%, so essentially the entire
backtested edge was this bug.

The account never had it: production runs the engine over completed bars and
deals in the following one, so filling at the open of a bar whose close is
known cannot happen live. The backtest was measuring something the daemon
could not do.

These tests are built so the correct answer is known by construction rather
than inferred from a difference between two large runs -- which is what let
the bug survive. A handful of bars, one instrument, one decision.

M356 the time stop judging on the bar's close rather than the fill
M357 the overnight profit take doing the same
M358 a rule acting on a bar it has not been given the fill price for
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from cfdinvest.execution import run_backtest
from cfdinvest.execution.frame import ExecutionConfig
from cfdinvest.preprocessing.frame import AnalysisFrame
from cfdinvest.signals.frame import SignalFrame


def _frames(opens, closes):
    """One instrument, hourly bars, a persistent long signal.

    ``open`` and ``close`` are supplied separately so a bar can rise or fall
    within itself -- which is the whole point, and which every other fixture
    in this suite collapses by setting them equal.
    """
    index = pd.DatetimeIndex([
        pd.Timestamp("2026-01-05 13:00", tz="UTC") + pd.Timedelta(hours=n)
        for n in range(len(opens))
    ])
    frame = lambda values: pd.DataFrame({"AAA": list(values)}, index=index)
    signals = SignalFrame(
        direction=frame(["long"] * len(index)),
        size=frame([0.1] * len(index)),
        confidence=frame([0.9] * len(index)),
        reason=frame([""] * len(index)),
    )
    close = frame(closes)
    analysis = AnalysisFrame(
        prices={
            "close": close, "open": frame(opens),
            "high": frame([max(o, c) for o, c in zip(opens, closes)]),
            "low": frame([min(o, c) for o, c in zip(opens, closes)]),
        },
        returns=close.pct_change().fillna(0.0),
    )
    return index, signals, analysis


#: A time stop that comes due on bar 2 and NOT on bar 1.
#:
#: Ages are 0.0417 days at bar 1 and 0.0833 at bar 2, so 0.06 sits between
#: them. At 0.04 the stop fired on bar 1, closed, and re-entered on bar 2 --
#: leaving a position in place that the assertion read as a survivor. A
#: fixture that lets the rule fire twice cannot test which bar it judged on.
STOP = dict(time_stop_days=0.06, execution_lag=0)


def test_a_position_up_at_the_fill_is_not_time_stopped_by_a_falling_close():
    """The decisive case, and the one the bug got wrong.

    Entered at 100. On the bar the time stop comes due the position is UP at
    the open -- which is the price an exit would get -- and DOWN by the
    close. A rule judging at the fill sees a working position and holds. A
    rule judging at the close sees a failing one and exits, at the good
    price it has just seen it will not keep.
    """
    #        bar0    bar1    bar2 <- stop due here: opens up, closes down
    opens = [100.0, 104.0, 106.0]
    closes = [100.0, 105.0,  95.0]
    index, signals, analysis = _frames(opens, closes)

    held = run_backtest(signals, analysis, ExecutionConfig(**STOP)).positions
    position = float(held["AAA"].fillna(0.0).loc[index[2]])

    assert position != 0.0, (
        "the position was time-stopped on a bar where it was UP 6% at the "
        "fill price and only down at the close. The rule is judging on the "
        "close and acting at the open -- it cannot know the close yet."
    )


def test_a_position_down_at_the_fill_is_time_stopped_despite_a_rising_close():
    """The mirror, which a naive 'use an earlier price' fix would fail.

    Down at the open, up by the close. The exit must happen: at the moment of
    dealing the position had not worked, and the recovery is not yet known.
    """
    opens = [100.0, 99.0, 94.0]
    closes = [100.0, 99.0, 108.0]
    index, signals, analysis = _frames(opens, closes)

    held = run_backtest(signals, analysis, ExecutionConfig(**STOP)).positions
    position = float(held["AAA"].fillna(0.0).loc[index[2]])

    assert position == 0.0, (
        "the position survived a bar where it was DOWN 6% at the fill price, "
        "because the bar later recovered. The rule is reading the close."
    )


def test_the_two_cases_differ_only_in_the_bar_they_disagree_on():
    """Guard against a fixture that would pass either way.

    Both cases must actually reach the time stop and both must produce a
    trade at some point -- otherwise the assertions above are satisfied by a
    book that never traded, which is how an inert arm reads as a pass.
    """
    for opens, closes in (([100.0, 104.0, 106.0], [100.0, 105.0, 95.0]),
                          ([100.0, 99.0, 94.0], [100.0, 99.0, 108.0])):
        _, signals, analysis = _frames(opens, closes)
        result = run_backtest(signals, analysis, ExecutionConfig(**STOP))
        assert (result.positions["AAA"].fillna(0.0) != 0).any(), (
            "this fixture never opened a position, so it tests nothing"
        )


def test_the_overnight_profit_take_judges_at_the_fill_too():
    """The same principle, on the rule where the flaw was first noticed.

    Two four-bar sessions. On the last bar of the first session the position
    is up 12% at the fill and up only 1% by the close. A 10% threshold must
    fire: at the price it would transact at, the gain is there.
    """
    opens = [100.0, 100.0, 100.0, 112.0,  101.0, 101.0, 101.0, 101.0]
    closes = [100.0, 100.0, 100.0, 101.0,  101.0, 101.0, 101.0, 101.0]
    index = pd.DatetimeIndex(
        [pd.Timestamp("2026-01-05 13:00", tz="UTC") + pd.Timedelta(hours=n)
         for n in range(4)]
        + [pd.Timestamp("2026-01-06 13:00", tz="UTC") + pd.Timedelta(hours=n)
           for n in range(4)]
    )
    frame = lambda v: pd.DataFrame({"AAA": list(v)}, index=index)
    close = frame(closes)
    signals = SignalFrame(
        direction=frame(["long"] * 8), size=frame([0.1] * 8),
        confidence=frame([0.9] * 8), reason=frame([""] * 8),
    )
    analysis = AnalysisFrame(
        prices={"close": close, "open": frame(opens),
                "high": frame([max(o, c) for o, c in zip(opens, closes)]),
                "low": frame([min(o, c) for o, c in zip(opens, closes)])},
        returns=close.pct_change().fillna(0.0),
    )

    result = run_backtest(signals, analysis, ExecutionConfig(
        execution_lag=0, overnight_profit_take=10.0))
    assert float(result.positions["AAA"].fillna(0.0).loc[index[3]]) == 0.0, (
        "a position up 12% at the fill price was carried overnight against a "
        "10% take, because the rule read the bar's close instead"
    )


def test_no_decision_rule_reads_the_bars_close():
    """Structural, and deliberately blunt.

    Every rule that decides and trades on the same bar must compare against
    `reference`. This greps the loop rather than testing behaviour, because
    the next such rule will be added by somebody who has not read this file,
    and the behavioural tests above only cover the two rules that exist.
    """
    import inspect

    from cfdinvest.execution import engine

    source = inspect.getsource(engine.run_execution)
    body = "\n".join(
        line for line in source.splitlines()
        if not line.strip().startswith("#")
    )
    # `price_now` is legitimate for marking to market and for the trade log.
    permitted = ("bar_pnl(", "notionals[bar]", "previous_close = np.where",
                 '"price": float(price_now', "nansum(units *",
                 "price_now = close[bar]")
    lines = body.splitlines()
    for number, line in enumerate(lines, 1):
        if "price_now" not in line:
            continue
        # A wrapped call puts the argument on a continuation line with none
        # of the identifying tokens on it, so judge the statement rather than
        # the line.
        window = chr(10).join(lines[max(number - 4, 0):number])
        assert any(token in window for token in permitted), (
            f"line {number} of run_execution uses price_now -- this bar's "
            f"close -- outside the permitted accounting uses:\n    {line}\n"
            "A rule that decides on this bar and fills on this bar must "
            "compare against `reference`, the fill price. See the time stop, "
            "which did this until 2026-09-04 and cost the project every "
            "performance figure it had."
        )
