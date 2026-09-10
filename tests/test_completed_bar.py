"""The dealer decides on completed bars, never on the bar in progress.

Two faults in two days were the same question asked twice -- WHICH BAR DOES
THE DEALER DECIDE ON -- and both answers were wrong.

``execution_lag = 1`` pointed it at the previous completed bar, which during
a session is right and at the open is the previous session's last: eighteen
hours and one gap stale. That opened TGT on 2026-09-10 on a signal whose
confidence had halved overnight.

Dropping the lag to 0 pointed it at the newest bar, which is the bar IN
PROGRESS. The run at 15:11 read an eleven-minute-old 15:00 bar on which TMUS
scored 0.771 against a 0.75 gate. The account opened it; twenty minutes
later the same bar read 0.415 and the engine exited:

    run                newest bar   age      TMUS confidence
    20260910T144601Z   14:00        complete           0.445
    20260910T151118Z   15:00        11 min             0.771   <- opened
    20260910T153148Z   15:00        31 min             0.415   <- exited
    20260910T155248Z   15:00        52 min             0.770   <- re-opened
    20260910T165458Z   15:00        complete           0.779

Eleven minutes of an hourly bar is noise, and every figure this project
holds was measured on completed bars.

That the hour ENDED at 0.779 -- clearing the gate -- is luck rather than
vindication, and it is why the churn is the fault rather than the answer.
One bar drove three account actions in forty minutes, and at 15:31 the
engine exited a position it would have kept had it waited, paying a spread
each way to find out. A decision that is right by coincidence is the
shadow-book failure with better manners.

Trimming the run to the newest COMPLETED bar answers the question once. The
lag stays at 0, so the dealer still acts on the newest bar it is given, and
the page reads the same run -- so page and dealer still agree, which is what
the lag change was for.

The first hour falls out rather than needing a rule: before 14:00 UTC no bar
of the session has finished, the newest completed bar is the previous
session's last, and the book is FLATTENED there -- so the intent is "hold
nothing" and nothing opens until the 13:00 bar completes at 14:00, thirty
minutes after the open.

Mutations checked, each reintroduced as a bug and confirmed to fail:
  M800 the span not subtracted, so the bar in progress is the end again
  M810 rounding up instead of down, ending past the data
  M820 an explicit --end overridden, so a research window is silently moved
  M830 an unparseable interval guessed at rather than left alone
  M850 the trim wired to the wrong field

M840 -- the trim applied AFTER the pipeline runs -- is asserted by
source order rather than mutated, because moving a block is not a
string substitution and a half-moved one would not compile. Said here
rather than left implied: an unmutated property is a weaker check, and
this file's own rule is that the mutations listed are the ones run.
"""

from __future__ import annotations

import inspect
import warnings

import pandas as pd
import pytest

from cfdinvest.schedule import app
from cfdinvest.schedule.app import _completed_bar_end

HOUR = pd.Timedelta(hours=1)


def moment(text: str) -> pd.Timestamp:
    return pd.Timestamp(text, tz="UTC")


@pytest.mark.parametrize(
    "now",
    ["2026-09-10 13:36", "2026-09-10 14:00", "2026-09-10 14:05",
     "2026-09-10 15:12", "2026-09-10 19:59"],
)
def test_the_bar_returned_has_finished(now):
    """M800/M810: the property, stated as completeness rather than a value.

    A bar labelled T on an hourly grid finishes at T + 1h. So whatever is
    returned must satisfy `end + span <= now` -- that is what "completed"
    means, and it is true at every moment rather than at the one the test
    author happened to pick.
    """
    at = moment(now)
    end = _completed_bar_end("1h", at)

    assert end is not None
    assert end + HOUR <= at, (
        f"the bar labelled {end} has not finished at {at}; the run would "
        "decide on a bar in progress"
    )


@pytest.mark.parametrize(
    "now", ["2026-09-10 13:36", "2026-09-10 14:05", "2026-09-10 15:12"]
)
def test_it_is_the_NEWEST_completed_bar(now):
    """Not merely a completed bar -- the latest one.

    Trimming too far would throw away good data and make the dealer late,
    which is the fault being fixed rather than a safe overshoot.
    """
    at = moment(now)
    end = _completed_bar_end("1h", at)

    assert end + HOUR + HOUR > at, (
        f"{end} is not the newest completed bar at {at}; a later one had "
        "also finished"
    )


def test_the_bar_in_progress_is_excluded():
    """The concrete TMUS case, named.

    `slice_range` is inclusive, so an end of 14:00 keeps the 14:00 bar and
    drops the 15:00 one that carried eleven minutes of data.
    """
    assert _completed_bar_end("1h", moment("2026-09-10 15:12")) == moment(
        "2026-09-10 14:00"
    )


def test_it_reads_the_grid_rather_than_assuming_an_hour():
    """M810: the rule is about the interval, not about hours.

    The lake holds 5- and 15-minute stores, and a rule hard-coded to an
    hourly grid would silently mistrim any run over them.
    """
    assert _completed_bar_end("5m", moment("2026-09-10 15:12")) == moment(
        "2026-09-10 15:05"
    )
    assert _completed_bar_end("15min", moment("2026-09-10 15:12")) == moment(
        "2026-09-10 14:45"
    )


@pytest.mark.parametrize(
    "interval,expected",
    [("1h", "2026-09-10 14:00"), ("h", "2026-09-10 14:00"),
     ("1d", "2026-09-09 00:00"), ("d", "2026-09-09 00:00"),
     ("1D", "2026-09-09 00:00"), ("15min", "2026-09-10 14:45")],
)
def test_both_spellings_of_an_interval_are_read(interval, expected):
    """The lake's spelling and pandas' spelling have drifted apart.

    A bare unit is a ValueError to `Timedelta` and "1d" is deprecated in
    favour of "1D". Refusing either would leave the run untrimmed on a
    dealing path -- silently, and exactly where the trim matters.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        got = _completed_bar_end(interval, moment("2026-09-10 15:12"))

    assert got == moment(expected)


@pytest.mark.parametrize("interval", [None, "", "nonsense", 0, -1])
def test_an_unreadable_interval_leaves_the_run_alone(interval):
    """M830: absence is named, never guessed.

    A grid that cannot be parsed means the trim cannot be computed, and the
    honest answer is to leave the run as it shipped rather than invent a
    boundary. Weaker, and the safe direction.
    """
    assert _completed_bar_end(interval, moment("2026-09-10 15:12")) is None


def test_run_once_trims_before_it_runs():
    """M840/M850: a trim applied late, or to the wrong field, is inert.

    Asserted on the source because the effect -- a persisted run built from
    a different window -- needs a lake to observe, and an inert trim looks
    identical to a working one from outside.
    """
    source = inspect.getsource(app._command_run_once)

    assert "_completed_bar_end" in source, (
        "the scheduled run does not trim the bar in progress, so the dealer "
        "can decide on a partial bar"
    )
    assert "end=trimmed" in source, (
        "the trim is not applied to `end`; it is being wired to something "
        "else and is inert"
    )
    assert source.index("_completed_bar_end") < source.index("run_pipeline("), (
        "the trim is computed after the pipeline runs, which is too late"
    )


def test_an_explicit_window_wins():
    """M820: a research run over a stated window must not be moved.

    This project's figures are quoted against windows somebody chose. A
    trim that silently overrode `--end` would shift every one of them by a
    bar, which is the class of error this file's retraction rule exists for.
    """
    source = inspect.getsource(app._command_run_once)
    trim = source.index("trimmed = (")

    assert "backtest.end" in source[trim:trim + 400], (
        "the trim does not consult the configured end, so an explicit "
        "--end would be overridden"
    )
    assert "if backtest.end is not None" in source[trim:trim + 400], (
        "the configured end is not preferred over the computed trim"
    )


def test_the_lag_is_unchanged_and_still_zero():
    """The trim replaces the lag rather than joining it.

    With the run stopping at the newest completed bar, lag 0 means "decide
    on that bar". Putting the lag back would reach a further bar into the
    past on top of the trim, which is the eighteen-hour fault again.
    """
    assert app.INTENT_EXECUTION_LAG == 0


# --------------------------------------------------------------------------
# The trim shipped INERT once, and these are the tests that would have caught
# it. `--interval` defaults to None, which means "whatever market.interval
# says" rather than "no bars", so reading the raw argument got None on every
# scheduled run and `_completed_bar_end` returned None. Deployed 16:04 on
# 2026-09-10, caught at 16:18 by reading the run on the box -- every test
# above passed throughout.
#
# Mutations checked:
#   M860 the configured default ignored, so a scheduled run goes untrimmed
#   M870 the explicit interval ignored in favour of the default
# --------------------------------------------------------------------------

class Market:
    def __init__(self, interval):
        self.interval = interval


class Config:
    def __init__(self, interval):
        self.market = Market(interval)


class Backtest:
    def __init__(self, interval):
        self.interval = interval


def test_a_missing_interval_falls_back_to_the_configured_one():
    """M860: the bug that made the whole trim inert on the live path."""
    from cfdinvest.schedule.app import _intent_interval

    assert _intent_interval(Backtest(None), Config("1h")) == "1h"
    assert _intent_interval(Backtest(""), Config("1h")) == "1h"


def test_an_explicit_interval_wins():
    """M870: a stated bar size must not be overridden by the default."""
    from cfdinvest.schedule.app import _intent_interval

    assert _intent_interval(Backtest("5m"), Config("1h")) == "5m"


def test_the_resolved_interval_actually_produces_a_trim():
    """The property end to end: a scheduled run must come out trimmed.

    Asserting the resolver alone would not have caught the original bug
    either -- what failed was the pair. So this asks the question the live
    path asks: given a run with no interval and a config that has one, is
    the bar in progress excluded?
    """
    from cfdinvest.schedule.app import _completed_bar_end, _intent_interval

    resolved = _intent_interval(Backtest(None), Config("1h"))
    end = _completed_bar_end(resolved, moment("2026-09-10 16:13"))

    assert end == moment("2026-09-10 15:00"), (
        "a scheduled run with no --interval is not trimmed, so the dealer "
        "still decides on the bar in progress"
    )


def test_run_once_resolves_rather_than_reading_the_raw_argument():
    """The call site is the half that was wrong, so assert on it."""
    source = inspect.getsource(app._command_run_once)

    assert "_intent_interval(" in source, (
        "the trim reads the raw --interval, which is None on every "
        "scheduled run, so the run goes untrimmed"
    )


# --------------------------------------------------------------------------
# The trim shipped inert TWICE, and both times because it was tested with
# interval strings the live path never passes it.
#
#   1st  the raw --interval, which is None on every scheduled run
#   2nd  `market.interval`, which is "capital_1h" -- a LAKE STORE NAME, not
#        a pandas frequency. `pd.Timedelta("capital_1h")` is a ValueError,
#        so the deliberate "leave it alone rather than guess" path fired on
#        every run.
#
# Every test above passed through both. They all handed the helper a
# frequency string. So the test that matters is the one that asks the
# SHIPPED CONFIGURATION what it declares, rather than one that picks its own
# input -- which is this project's "test the property, not the constant"
# rule applied to a fixture instead of a threshold.
#
# Mutations checked:
#   M890 the store-name branch removed, so the shipped interval goes untrimmed
#   M900 an unknown store guessed at rather than left alone
# --------------------------------------------------------------------------

def test_the_shipped_interval_actually_produces_a_trim():
    """M890: the test that would have caught both inert deploys.

    Reads what the configuration really says rather than naming a value
    here. If `market.interval` moves to `capital_5m`, or to anything else
    the lake can serve, this still asserts the thing that matters: the
    dealer must not decide on the bar in progress.
    """
    from cfdinvest.config import load_config
    from cfdinvest.schedule.app import _completed_bar_end, _intent_interval

    settings = load_config()
    resolved = _intent_interval(Backtest(None), settings)

    assert resolved, "the shipped config declares no market interval"

    at = moment("2026-09-10 16:34")
    end = _completed_bar_end(resolved, at)

    assert end is not None, (
        f"the shipped interval {resolved!r} cannot be turned into a bar "
        "span, so every scheduled run goes untrimmed and the dealer "
        "decides on the bar in progress"
    )
    assert end < at, "the trim did not move the end back at all"


@pytest.mark.parametrize(
    "store,expected",
    [("capital_1h", "2026-09-10 15:00"), ("capital_5m", "2026-09-10 16:25"),
     ("capital_15m", "2026-09-10 16:15"), ("capital_1m", "2026-09-10 16:33"),
     ("capital_1d", "2026-09-09 00:00")],
)
def test_every_capital_store_resolves_to_its_bar(store, expected):
    """A store name is a directory, and the lake serves several of them.

    Resolved through `capital_prices.resolution_for`, which is the existing
    single owner of that mapping -- a second parser here is how the two
    would drift.
    """
    assert _completed_bar_end(store, moment("2026-09-10 16:34")) == moment(expected)


@pytest.mark.parametrize("store", ["capital_9z", "capital_", "capital_2y"])
def test_an_unknown_store_is_left_alone(store):
    """M900: absence named rather than guessed.

    A store this project does not serve is not a licence to invent a bar
    size -- that would trim a run to a window nobody chose.
    """
    assert _completed_bar_end(store, moment("2026-09-10 16:34")) is None
