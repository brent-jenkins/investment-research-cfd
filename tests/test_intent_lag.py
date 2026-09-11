"""The page and the dealer must be reading the same bar.

On 2026-09-10 they were not:

    UI showed (13:00 bar)   COP CVX BLK GM ABT HD HON
    engine intended         AXP BKNG BLK CMCSA TGT     <- 2026-09-09 19:00

One name in common, by coincidence. TGT's confidence had halved overnight,
0.798 to 0.463, and TGT was the one that opened.

Both halves were reading correctly. The UI reads ``signals`` on the newest
bar -- what the engine thinks now. ``persist.intent`` reads
``positions.iloc[-1]``, and positions carry ``execution_lag``, so the dealer
acted on the PREVIOUS bar's decision. Across the overnight gap that is
eighteen hours and one gap old.

``execution_lag = 1`` is right in a backtest: deciding on bar N's close and
filling at bar N's open reads the future. It is wrong live, where at 13:36
you know the 13:36 price and deal at 13:36. It is a simulation device that
had leaked into the dealing path, and it was invisible while the book
carried positions overnight -- yesterday's view and today's holding were
then the same thing. Day trading flattens nightly, so every morning now
rebuilds from flat.

So the run that produces intents drops the lag and research keeps it. They
cannot be one config, which is the third time that has been the answer this
week: the last look, the flatten, and now this.

Mutations checked, each reintroduced as a bug and confirmed to fail:
  M540 the intent run left at the research lag, so the dealer reads stale
  M550 the lag dropped from _backtest_config too, making every figure look-ahead
  M560 the override applied to the tickers rather than the execution config
  M570 a negative lag, which would deal on the future rather than merely late
"""

from __future__ import annotations

import dataclasses
import inspect

import pytest

from cfdinvest.schedule import app
from cfdinvest.schedule.app import INTENT_EXECUTION_LAG


class Args:
    interval = None
    start = end = universe = account = tickers = None


def research_config():
    from cfdinvest.config import load_config

    return app._backtest_config(Args(), load_config())


def test_research_still_decides_on_a_completed_bar():
    """M550: the half that must NOT change.

    Every recorded figure in this project was measured at lag 1. Dropping it
    here would make the whole backtest look-ahead and every sweep worthless,
    which is a far worse failure than the one being fixed.
    """
    assert research_config().execution.execution_lag == 1, (
        "_backtest_config no longer lags; every backtest figure is now "
        "look-ahead"
    )


def test_the_intent_run_decides_on_the_newest_bar():
    """M540: the half that must change."""
    assert INTENT_EXECUTION_LAG == 0, (
        "the intent run still lags, so the dealer acts on the previous bar "
        "-- eighteen hours old across the overnight gap"
    )


def test_the_two_differ_which_is_the_whole_point():
    """Stated as a relation rather than two constants.

    If a later edit moved both to the same value this passes only when they
    genuinely disagree, which is the property that keeps the page and the
    dealer aligned without making research look-ahead.
    """
    assert INTENT_EXECUTION_LAG != research_config().execution.execution_lag


def test_run_once_applies_the_override_to_the_execution_config():
    """M560: an override applied to the wrong field is an inert one.

    Asserted on the source because the effect -- a persisted run built from
    a different config -- needs a lake to observe, and an inert override
    would look identical to a working one from outside.
    """
    # Repointed on 2026-09-11: the corrections moved from
    # `_command_run_once` into `for_live_intent`, so they could be
    # applied by EVERY caller that persists a run the dealer reads --
    # `_command_run_continuous` was one and applied none of them. This
    # assertion follows the code rather than being relaxed; the source
    # it reads is the function that now owns the behaviour.
    source = inspect.getsource(app.for_live_intent)

    assert "INTENT_EXECUTION_LAG" in source, (
        "the live corrections do not apply the intent lag, so a persisted "
        "run still produces lagged positions"
    )
    assert "for_live_intent(" in inspect.getsource(app._command_run_once), (
        "the scheduled run does not apply the corrections at all"
    )
    assert "execution_lag=INTENT_EXECUTION_LAG" in source, (
        "the override does not set execution_lag; it is being applied to "
        "something else and is inert"
    )
    caller = inspect.getsource(app._command_run_once)
    assert caller.index("for_live_intent(") < caller.index("run_pipeline("), (
        "the corrections are applied after the pipeline runs, too late"
    )


def test_the_override_survives_being_applied():
    """The value must actually land on a real ExecutionConfig.

    A dataclasses.replace onto the wrong object raises; onto the right one
    with a misspelled field also raises. Doing it here proves the expression
    in _command_run_once is well formed against the real type.
    """
    base = research_config()
    intent = dataclasses.replace(
        base,
        execution=dataclasses.replace(
            base.execution, execution_lag=INTENT_EXECUTION_LAG
        ),
    )

    assert intent.execution.execution_lag == INTENT_EXECUTION_LAG
    assert base.execution.execution_lag == 1, "replace mutated the original"
    assert intent.tickers == base.tickers, "the override changed the universe"


def test_a_negative_lag_is_refused_by_the_engine():
    """M570: late is a bug, early is fraud.

    Zero means "decide on what you know now". Negative would mean deciding
    on a bar that has not happened, and the engine refuses it outright --
    asserted here so the intent path can never be pointed past zero by a
    later edit that reads 0 as "less is better".
    """
    from cfdinvest.execution.fills import shift_for_execution

    assert INTENT_EXECUTION_LAG >= 0
    with pytest.raises(ValueError, match="future"):
        shift_for_execution(None, -1)
