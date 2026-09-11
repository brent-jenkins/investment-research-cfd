"""A stale run may not OPEN, and must never stop a CLOSE.

While dealing sat behind the pipeline in one sequential loop, a stale run
was impossible by construction: the run had just been written by the job
before it. Separating the dealer into its own process is what stops a
wedged pipeline preventing a close — on 2026-09-11 the pipeline hung at
13:35 and the account could not close for the rest of the session — but it
introduces the opposite risk. The analysis can now fail, hang or fall
behind while the dealer carries on happily, acting on whatever was last
written.

So the guard is asymmetric, and it is the same asymmetry the margin guard
and the reconciliation halt already make: **a book the app cannot form a
current view about is exactly the book that most needs to be reducible.**
Opens are refused; closes flow.

``STALE_DECISION_SPACINGS = 2`` rather than 1 because the newest completed
bar is, by construction, up to one spacing old the moment it is written —
a one-spacing limit would refuse on a perfectly fresh run. Two gives the
analysis a full cycle to miss before the dealer notices.

Mutations checked, each reintroduced as a bug and confirmed to fail:
  M1500 the guard inert, so a stale run still opens
  M1510 a fresh run judged stale, so the account never opens at all
  M1520 the limit cut to one spacing, refusing on a run that is fine
  M1530 an unreadable run treated as stale rather than left to plan_orders
  M1540 the guard applied AFTER planning, where it cannot affect anything
  M1545 the guard short-circuited by a constant, leaving the name in place
  M1550 the guard closing the book rather than only withholding opens
"""

from __future__ import annotations

import ast
import inspect
import textwrap

import pandas as pd
import pytest

from cfdinvest.persist import intent as intent_store
from cfdinvest.schedule import app

HOUR = pd.Timedelta(hours=1)
BAR = pd.Timestamp("2026-09-11 15:00", tz="UTC")


class Config:
    class market:
        interval = "capital_1h"


@pytest.fixture
def run(monkeypatch):
    """Stand in for the persisted run, which needs a lake to build."""
    holder = {"frame": None}

    def load(_unused, config=None, account=None):
        return {"positions": holder["frame"]}

    monkeypatch.setattr(
        "cfdinvest.persist.readers.load_execution_frame", load, raising=False
    )
    return holder


def with_bar(run, bar):
    run["frame"] = pd.DataFrame(
        {"SPGI": [1.0]}, index=pd.DatetimeIndex([bar])
    )


def stale_at(run, minutes):
    with_bar(run, BAR)
    return intent_store.decision_is_stale(
        Config(), None, now=BAR + pd.Timedelta(minutes=minutes)
    )


def test_a_fresh_run_is_not_stale(run):
    """M1510: the newest completed bar is already up to one spacing old.

    Refusing on that would mean the account never opens anything, which is
    a worse failure than the one being guarded against and would look
    exactly like the gate misfiring.
    """
    assert stale_at(run, 5) is False
    assert stale_at(run, 59) is False
    assert stale_at(run, 90) is False, (
        "a run barely over one spacing is normal, not stale"
    )


def test_a_run_two_spacings_old_is_stale(run):
    """M1500/M1520: the limit itself."""
    assert stale_at(run, 121) is True
    assert stale_at(run, 240) is True


def test_the_boundary_is_two_spacings(run):
    """Stated as the property rather than a magic number.

    Asserted against the constant so moving it moves the test with it, and
    against the bar span so a change of grid does not silently change the
    meaning.
    """
    from cfdinvest.ingestion.capital_prices import bar_span

    limit = bar_span("capital_1h") * intent_store.STALE_DECISION_SPACINGS

    assert stale_at(run, limit.total_seconds() / 60 - 1) is False
    assert stale_at(run, limit.total_seconds() / 60 + 1) is True


def test_an_unreadable_run_is_not_called_stale(run):
    """M1530: absence and staleness are different statements.

    `plan_orders` already declines an instrument it has no intent for, so a
    missing run needs no second punishment here — and treating "cannot
    tell" as "too old" would stop the account dealing whenever a read
    hiccuped.
    """
    run["frame"] = None
    assert intent_store.decision_is_stale(Config(), None, now=BAR) is False

    run["frame"] = pd.DataFrame()
    assert intent_store.decision_is_stale(Config(), None, now=BAR) is False


def test_an_unknown_bar_size_is_not_called_stale(run):
    """A grid that cannot be read must not be guessed at."""
    class Unknown:
        class market:
            interval = "nonsense"

    with_bar(run, BAR)
    assert intent_store.decision_is_stale(
        Unknown(), None, now=BAR + pd.Timedelta(days=5)
    ) is False


def test_the_age_is_reported_not_just_the_verdict(run):
    """The warning has to say how old, or nobody can act on it."""
    with_bar(run, BAR)
    age = intent_store.decision_age(Config(), None, now=BAR + 3 * HOUR)
    assert age == 3 * HOUR


def test_the_guard_runs_before_the_orders_are_planned():
    """M1540: the mistake I actually made writing this.

    The first draft placed the guard after `plan_orders`, where
    `open_allowed` has already been passed and clearing it changes nothing.
    An inert guard reporting success is this project's oldest failure, so
    the ordering is asserted rather than assumed.
    """
    source = inspect.getsource(app._command_trade_capital)

    assert "decision_is_stale" in source, (
        "the dealer does not check staleness, so it will open on whatever "
        "the analysis last managed to write"
    )
    assert source.index("decision_is_stale") < source.index("bridge.plan_orders("), (
        "the staleness guard runs after the orders are planned, where it "
        "cannot affect them"
    )

    # **Position is not the same as effect, and a mutation proved it.**
    # `if False and decision_is_stale(...)` leaves the name sitting in the
    # right place while the guard does nothing, and the ordering assertion
    # above passes happily. So the SHAPE of the condition is asserted too:
    # the `if` test must be the call itself, not the call buried inside
    # something that can short-circuit it away.
    tree = ast.parse(textwrap.dedent(source))
    guards = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and any(
            isinstance(inner, ast.Call)
            and getattr(inner.func, "attr", None) == "decision_is_stale"
            for inner in ast.walk(node.test)
        )
    ]

    assert len(guards) == 1, f"expected one staleness guard, found {len(guards)}"
    assert isinstance(guards[0].test, ast.Call), (
        "the staleness check is wrapped in a wider expression, so it can be "
        "short-circuited away while still reading as present"
    )


def test_the_guard_withholds_opens_and_not_closes():
    """M1550: the whole asymmetry.

    Clearing `open_allowed` withholds entries while leaving the pass to run,
    so closes are still planned and placed. Skipping the pass entirely
    would strand the book — which is the failure this separation exists to
    prevent, reintroduced by its own guard.
    """
    source = inspect.getsource(app._command_trade_capital)
    guard = source.index("decision_is_stale")
    after = source[guard:guard + 900]

    assert "open_allowed" in after, (
        "the guard does not clear open_allowed, so it is not withholding "
        "entries"
    )
    assert "return" not in after.split("open_allowed")[0], (
        "the guard returns early, which would stop closes as well as opens"
    )
