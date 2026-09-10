"""Yesterday's closing state is not today's decision.

``intended_positions`` reads ``positions.iloc[-1]``. On the last bar of a
session that book is **flat by construction**: the account flattens before
the bell and the engine models it. So the intent read from a session-end
bar is "hold nothing".

That is true of 19:59 and false of 13:35 the next morning. Between the open
and the completion of the session's first bar, the newest COMPLETED bar IS
the previous session's last -- a direct consequence of the completed-bar
rule shipped the same day -- and reading it says "hold nothing" when what it
means is "yesterday ended flat".

A position carried into the morning would be closed on that, before any bar
of the new day exists. The owner's objection on 2026-09-10:

    Shouldn't the system evaluate the open position before closing it?
    What if SPGI is one of the candidates to open a trade on tomorrow?
    Closing the open trade only to immediately open a new one doesn't
    make sense.

It does not. SPGI was carried overnight reading **0.901** on its last
completed bar, so it was likely to be wanted again within the hour, and the
round trip would have paid the spread twice to arrive back where it
started.

Nothing here decides whether to keep a position. It declines to decide on
evidence that is not about today, and waits at most one bar for evidence
that is. ``plan_orders`` then does what it already does: target against
held, ``none`` when they agree, a close when they do not.

**The test is the harm, not the calendar**, and the first version got that
wrong. It asked whether the bar was the last of its session -- which cannot
be told apart from "the last bar we happen to have, because ingestion
stopped at 15:00". It would have suppressed dealing whenever the lake went
stale, and the account would quietly never have closed anything again. The
test below for that case is what caught it, before it shipped.

What matters is whether acting would EMPTY the book on evidence from
another day. So the rule is: an earlier day, AND a flat intent. A stale bar
that still holds positions keeps the old behaviour, because acting on it
closes some things rather than everything.

Mutations checked, each reintroduced as a bug and confirmed to fail:
  M1000 the check inert, so yesterday's flat book closes this morning's book
  M1010 today's own session-end bar treated as stale, so the bell never flattens
  M1020 a previous day that still HOLDS suppressed too, so a stale lake
        silently stops the account ever closing anything
  M1030 the date comparison dropped, suppressing every pass
  M1040 the dealing pass computing it but not returning
"""

from __future__ import annotations

import inspect

import pandas as pd
import pytest

from cfdinvest.persist import intent as intent_store
from cfdinvest.schedule import app


def session(day, hours=(13, 14, 15, 16, 17, 18, 19)):
    return [pd.Timestamp(f"{day} {h:02d}:00", tz="UTC") for h in hours]


def frame(stamps, flat_at_end=True):
    """A book that holds SPGI, flattened on the final bar if asked.

    `flat_at_end` models what the engine really produces: the account
    flattens before the bell and the engine models it, so a session's last
    bar is flat. A frame that held a position on every bar -- as an earlier
    version of this fixture did -- is not a book this engine can produce,
    and testing against it proved nothing about the real case.
    """
    units = [1.0] * len(stamps)
    if flat_at_end and units:
        units[-1] = 0.0
    return pd.DataFrame({"SPGI": units}, index=pd.DatetimeIndex(stamps))


@pytest.fixture
def positions(monkeypatch):
    """Stand in for the persisted run, which needs a lake to build."""
    holder = {"frame": None}

    def load(_unused, config=None, account=None):
        return {"positions": holder["frame"]}

    monkeypatch.setattr(
        "cfdinvest.persist.readers.load_execution_frame", load, raising=False
    )
    return holder


def ask(positions, stamps, now, flat_at_end=True):
    positions["frame"] = frame(stamps, flat_at_end=flat_at_end)
    return intent_store.decision_is_a_previous_session_close(
        None, None, now=pd.Timestamp(now, tz="UTC")
    )


def test_the_previous_sessions_close_is_not_acted_on(positions):
    """M1000: the whole point.

    At 13:35 the newest completed bar is yesterday's 19:00, where the book
    is flat because of the flatten rather than because of a decision.
    """
    assert ask(positions, session("2026-09-10"), "2026-09-11 13:35") is True


def test_todays_own_session_end_is_acted_on(positions):
    """M1010: the bell must still flatten.

    At 20:05, yesterday's argument does not apply -- that IS today's
    closing decision, and suppressing it would leave the book open
    overnight, which is the fault this whole configuration exists to
    avoid.
    """
    assert ask(positions, session("2026-09-10"), "2026-09-10 20:05") is False


def test_a_mid_session_bar_from_yesterday_is_still_acted_on(positions):
    """M1020: ordinary staleness is a different fault.

    Ingestion stopping at 15:00 yesterday leaves a bar the flatten never
    touched, so its book is a real view rather than an artefact. Treating
    it as suppressible would mean a broken lake silently stops the account
    ever closing anything.
    """
    stale = session("2026-09-10", hours=(13, 14, 15))
    assert ask(positions, stale, "2026-09-11 13:35", flat_at_end=False) is False


def test_a_bar_from_today_is_always_acted_on(positions):
    """M1030: suppressing every pass would stop the account dealing."""
    assert ask(positions, session("2026-09-11"), "2026-09-11 16:35") is False
    assert ask(positions, session("2026-09-11", hours=(13,)),
               "2026-09-11 14:05") is False


def test_a_previous_day_that_still_holds_is_acted_on(positions):
    """M1020, restated: the rule is about emptying the book, not the date.

    A stale bar that still holds positions is ordinary staleness. Acting on
    it closes some things rather than everything, so suppressing it would
    mean a broken lake silently stops the account ever closing anything --
    which the first version of this rule would have done, and its own test
    caught.
    """
    assert ask(positions, session("2026-09-10"), "2026-09-11 13:35",
               flat_at_end=False) is False


def test_it_cannot_be_told_without_a_run(positions):
    """Absence keeps the old behaviour rather than stopping the account."""
    positions["frame"] = None
    assert intent_store.decision_is_a_previous_session_close(
        None, None, now=pd.Timestamp("2026-09-11 13:35", tz="UTC")
    ) is False


def test_an_unreadable_frame_never_blocks_a_pass(positions, monkeypatch):
    """A pass that raises opens nothing AND closes nothing."""
    def explode(*_args, **_kwargs):
        raise RuntimeError("the run is unreadable")

    monkeypatch.setattr(
        "cfdinvest.persist.readers.load_execution_frame", explode, raising=False
    )
    assert intent_store.decision_is_a_previous_session_close(
        None, None, now=pd.Timestamp("2026-09-11 13:35", tz="UTC")
    ) is False


def test_the_dealing_pass_holds_rather_than_acting():
    """M1040: computing the answer and ignoring it is the inert version.

    Asserted on the source because the effect needs a broker and a lake to
    observe, and this project has shipped three gates this week that
    reported success while doing nothing.
    """
    source = inspect.getsource(app._command_trade_capital)

    assert "decision_is_a_previous_session_close" in source, (
        "the dealing pass acts on the previous session's closing state, so "
        "a carried position is closed before the day has a view"
    )
    check = source.index("decision_is_a_previous_session_close")
    after = source[check:check + 1200]

    assert "return EXIT_OK" in after, (
        "the check is computed and not acted on, so it is inert"
    )
    assert "record_plan(" in after, (
        "the pass returns without recording what it would have done, so "
        "the hold leaves no evidence"
    )
    assert check < source.index("for intent in report.actionable"), (
        "the check runs after the orders are placed, which is too late"
    )
