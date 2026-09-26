"""The engine's closing window measures to the session's CLOSE, not its last bar.

`close_delay_minutes` refuses an entry within that many minutes of the
close. The engine measured "minutes remaining" from each bar's stamp to the
day's LAST BAR STAMP -- and a bar is stamped at its START, so the last
hourly bar of a 20:00 close is stamped 19:00. Every bar was therefore
credited with one bar less time than it had.

At the shipped 90 minutes, live stops opening at 18:30. A 17:00 decision is
dealt at about 18:05 and permitted; the engine fills it on the 18:00 bar,
read 60 minutes remaining where there were 120, and refused it.

Found on 2026-09-26 by replaying the live period through the research
harness against the account's own intents. Every harness entry from
2026-09-19 on was a position the live engine intended on that exact bar;
what the harness missed were the live entries decided at 17:00 -- 6 live
against 0 in the harness over three weeks.

On daily bars the old measure was worse: one bar per day made every bar the
day's last, remaining was always zero, and any closing window refused every
entry the engine could make.

Mutations checked:

  M1820 the bar span removed from `remaining` (the defect restored)
  M1821 the span taken as zero on a one-bar-per-day index
  M1822 the frame's-final-bar exemption removed
  M1823 the final day's close taken as its newest bar, so a live run erases
        the entry it made an hour earlier
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from cfdinvest.execution import run_backtest
from cfdinvest.execution.frame import ExecutionConfig
from cfdinvest.preprocessing.frame import AnalysisFrame
from cfdinvest.signals.frame import SignalFrame


def _run(index, long_at, close_delay, lag=0):
    """A rising price, long on the given bars and flat elsewhere."""
    close = pd.DataFrame({"AAA": np.linspace(100.0, 140.0, len(index))}, index=index)
    want = ["long" if stamp in long_at else "flat" for stamp in index]
    size = [0.1 if stamp in long_at else 0.0 for stamp in index]
    frame = lambda values: pd.DataFrame({"AAA": values}, index=index)
    signals = SignalFrame(
        direction=frame(want), size=frame(size),
        confidence=frame([0.9] * len(index)), reason=frame([""] * len(index)),
    )
    analysis = AnalysisFrame(
        prices={"close": close, "open": close, "high": close, "low": close},
        returns=close.pct_change().fillna(0.0),
    )
    result = run_backtest(
        signals, analysis,
        ExecutionConfig(close_delay_minutes=close_delay, execution_lag=lag),
    )
    return result.positions["AAA"].fillna(0.0)


def _hourly(days=3):
    """Seven hourly bars a day, 13:00 to 19:00 -- a 20:00 close."""
    stamps = []
    for day in range(days):
        base = pd.Timestamp("2026-01-05", tz="UTC") + pd.Timedelta(days=day)
        stamps += [base + pd.Timedelta(hours=13 + n) for n in range(7)]
    return pd.DatetimeIndex(stamps)


def test_an_entry_two_hours_before_the_close_is_permitted() -> None:
    """M1820: the 18:00 bar has 120 minutes left, not 60.

    Day two, so the bar is neither the frame's first nor its last.
    """
    index = _hourly()
    day = pd.Timestamp("2026-01-06", tz="UTC")
    long_at = {day + pd.Timedelta(hours=18), day + pd.Timedelta(hours=19)}

    held = _run(index, long_at, close_delay=90)

    assert held.loc[day + pd.Timedelta(hours=18)] > 0, (
        "the engine refused an entry at 18:00 on a 20:00 close with a 90-minute "
        "window -- it is measuring to the LAST BAR'S STAMP, 19:00, and so "
        "refuses entries the live account takes"
    )


def test_an_entry_inside_the_window_is_still_refused() -> None:
    """The fix must not open the window: the 19:00 bar has 60 minutes left.

    Without this the first test passes against a window that refuses
    nothing, which is the inert-gate failure this project keeps meeting.
    """
    index = _hourly()
    day = pd.Timestamp("2026-01-06", tz="UTC")
    late = day + pd.Timedelta(hours=19)

    refused = _run(index, {late}, close_delay=90)
    control = _run(index, {late}, close_delay=0)

    assert control.loc[late] > 0, "the control never opened, so the test proves nothing"
    assert refused.loc[late] == 0, (
        "an entry an hour before the close opened through a 90-minute window"
    )


def test_a_closing_window_on_daily_bars_does_not_refuse_everything() -> None:
    """M1821: one bar per day is every day's last bar.

    Measured from the last bar's stamp, remaining is zero on every bar and a
    closing window forbids trading outright. A daily bar spans the day, so
    it has the day in front of it.
    """
    index = pd.date_range("2026-01-05", periods=10, freq="B", tz="UTC")
    long_at = set(index[3:7])

    held = _run(index, long_at, close_delay=90)

    assert (held > 0).any(), (
        "a 90-minute closing window refused every entry on daily bars"
    )


def test_a_live_run_does_not_erase_the_entry_it_made_an_hour_ago() -> None:
    """M1823: the day in progress ends at the session close, not at its newest bar.

    A live run stops at the newest COMPLETED bar, so on the day in progress
    the newest bar is also that day's last stamp. Measured to it, an entry
    made on the bar before is "an hour from the close" and refused -- after
    the fact. The engine's book then no longer holds a position the account
    opened, the next intent is flat, and the account closes it "on signal".

    On 2026-09-24 that closed seven positions an hour after they opened --
    AXP, PGR, GS, SCHW, DHR, WFC, TMO -- the live runs' trade logs showing no
    position at all. A position survived only if the next bar re-qualified
    for ENTRY, which is a far stricter exit than anybody chose.

    Here the run ends at 15:00 on its final day, as a live run would. The
    entry at 14:00 must still be there.
    """
    index = _hourly(days=3)
    final_day = pd.Timestamp("2026-01-07", tz="UTC")
    index = index[index <= final_day + pd.Timedelta(hours=15)]
    entry = final_day + pd.Timedelta(hours=14)

    held = _run(index, {entry, final_day + pd.Timedelta(hours=15)}, close_delay=150)

    assert held.loc[entry] > 0, (
        "a run ending at 15:00 refused its own 14:00 entry as if 15:00 were "
        "the session close -- the entry the account opened an hour ago "
        "vanishes from the engine's book and the next pass closes it"
    )


def test_the_frames_final_bar_is_not_treated_as_a_close() -> None:
    """M1822: the live intent run decides on its newest bar.

    That bar is the last thing that has happened, not a session's end, and
    refusing it emptied the intent on 2026-09-10. The exemption is kept.
    """
    index = _hourly(days=1)
    last = index[-1]

    held = _run(index, {last}, close_delay=90)

    assert held.loc[last] > 0, (
        "the newest bar of a run was refused as if the session were closing"
    )
