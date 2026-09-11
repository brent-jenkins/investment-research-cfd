"""Every run the DEALER may read carries the live corrections.

``persist.intent`` reads the newest persisted run, whichever command wrote
it. So the corrections that make a run safe to deal on are properties of
*any run whose output reaches the account*, not of one command — and
keeping them inside ``_command_run_once`` was an accident of where they
were first needed.

``_command_run_continuous`` called ``run_pipeline`` directly and applied
none of them. It is manual-only and the daemon does not use it, so nothing
was ever wrong on the box — but it writes to the same run store, and a run
it persisted would have been read by the dealer at ``execution_lag = 1``
over an untrimmed panel. That is the eighteen-hour-old signal that opened
TGT on 2026-09-10 and the eleven-minute partial bar that churned TMUS the
same afternoon, both restored, from a path nobody would think to check.

It was found while checking a definition, not by a test. This file is the
test.

**This is not a second decision path.** The corrections change the INPUTS
to the one engine and never its rules, which is what keeps them inside the
"one engine, and it is the production one" constraint — that rule exists
because the account once traded a rule set no backtest described, and a
start date is not a rule.

Mutations checked, each reintroduced as a bug and confirmed to fail:
  M1200 the lag left at the research value, so the dealer reads a stale bar
  M1210 the panel left untrimmed, so the dealer decides on a partial bar
  M1220 the flatten unmodelled, so the run holds what the account closed
  M1230 an explicit --end silently overridden
  M1240 a persisting caller that does not apply the corrections
"""

from __future__ import annotations

import ast
import dataclasses
import inspect

import pandas as pd

from cfdinvest.config import load_config
from cfdinvest.schedule import app


class Args:
    interval = None
    start = end = universe = account = tickers = None


def research_config():
    return app._backtest_config(Args(), load_config())


def corrected(end=None):
    base = research_config()
    if end is not None:
        base = dataclasses.replace(base, end=end)
    return app.for_live_intent(base, load_config())


def test_the_dealer_decides_on_the_newest_bar():
    """M1200: lag 1 reaches back a bar — eighteen hours across the gap."""
    assert corrected().execution.execution_lag == 0
    assert research_config().execution.execution_lag == 1, (
        "research must keep the lag; dropping it makes every figure "
        "look-ahead, which is a far worse failure than the one being fixed"
    )


def test_the_panel_stops_at_a_completed_bar():
    """M1210: 'newest' without this is the bar IN PROGRESS.

    Asserted as a relation to now rather than a value, so it holds whenever
    the suite runs.
    """
    end = corrected().end

    assert end is not None, "the panel is untrimmed; the dealer sees a partial bar"
    assert pd.Timestamp(end) < pd.Timestamp.now(tz="UTC")


def test_the_close_of_session_flatten_is_modelled():
    """M1220: without it the run holds names the account closed at the bell."""
    settings = load_config()
    take = corrected().execution.overnight_profit_take

    if getattr(settings.strategy, "flatten_before_close", False):
        assert take == app.FLATTEN_AT_SESSION_END
    else:
        assert take is None


def test_an_explicit_window_is_never_moved():
    """M1230: a stated research window must survive.

    Every figure this project quotes was measured over a window somebody
    chose. Silently shifting it by a bar is the class of error the
    retraction rule exists for.
    """
    stated = pd.Timestamp("2026-01-01", tz="UTC")
    assert corrected(end=stated).end == stated


def test_the_corrections_do_not_touch_the_rules():
    """The constraint this has to live inside.

    "One engine, and it is the production one" forbids a second
    implementation of a RULE. These change inputs — a lag, a window, and
    whether the modelled book flattens — so the gates, caps and sizing must
    come through untouched.
    """
    base = research_config()
    live = app.for_live_intent(base, load_config())

    for field in ("max_leverage", "concentration", "entry_confidence",
                  "exposure_per_unit_size", "time_stop_days",
                  "rebalance_threshold"):
        if hasattr(base.execution, field):
            assert getattr(live.execution, field) == getattr(base.execution, field), (
                f"{field} changed; this must not alter the strategy"
            )
    assert live.tickers == base.tickers, "the universe changed"


# --------------------------------------------------------------- the guard

FUNCTION = (ast.FunctionDef, ast.AsyncFunctionDef)


def _calls(node, callee: str) -> bool:
    """Does this function call ``callee`` anywhere in its own body?"""
    return any(
        isinstance(inner, ast.Call)
        and isinstance(inner.func, ast.Name)
        and inner.func.id == callee
        for inner in ast.walk(node)
    )


def _functions_calling(module_source: str, callee: str) -> set[str]:
    """Every function whose body calls ``callee``, nested ones included."""
    tree = ast.parse(module_source)
    return {
        node.name for node in ast.walk(tree)
        if isinstance(node, FUNCTION) and _calls(node, callee)
    }


def _unprotected_runners(module_source: str) -> list[str]:
    """Functions that run the pipeline with no correction in lexical scope.

    **Scope matters, and the first version of this guard ignored it.** In
    ``_command_run_continuous`` the pipeline is run by a nested ``job``
    closure while the corrections are applied in the enclosing function, so
    the config reaches it by closure. A per-function check reported ``job``
    as unprotected — a false alarm, and one that would have been "fixed" by
    weakening the guard rather than by reading the code.

    So a runner counts as protected when IT or any ENCLOSING function
    applies the corrections, which is how the config actually travels.
    """
    tree = ast.parse(module_source)
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    unprotected = []
    for node in ast.walk(tree):
        if not isinstance(node, FUNCTION) or not _calls(node, "run_pipeline"):
            continue
        # Only the innermost function owning the call; an enclosing one
        # "calls" the pipeline only through this one.
        if any(
            isinstance(inner, FUNCTION) and inner is not node
            and _calls(inner, "run_pipeline")
            for inner in ast.walk(node)
        ):
            continue

        scope, protected = node, False
        while scope is not None:
            if isinstance(scope, FUNCTION) and _calls(scope, "for_live_intent"):
                protected = True
                break
            scope = parents.get(scope)
        if not protected:
            unprotected.append(node.name)
    return sorted(unprotected)


def test_every_caller_that_persists_a_run_applies_the_corrections():
    """M1240: the guard, and the reason this file exists.

    Parsed rather than grepped, so a call inside a nested helper still
    counts and a mention in a comment or docstring does not.

    Total by construction: any function that runs the pipeline must have
    the corrections in scope, so a NEW entry point fails here until
    somebody has answered "will the dealer read this run?" — the question
    nobody asked about ``run-continuous``.
    """
    source = inspect.getsource(app)

    assert _functions_calling(source, "run_pipeline"), (
        "no caller of run_pipeline found; this guard has gone blind"
    )

    missing = _unprotected_runners(source)
    assert not missing, (
        "these persist a run the dealer may read without applying the live "
        f"corrections: {missing}. Either call for_live_intent, or do not "
        "persist into the account's run store."
    )


def test_the_guard_would_notice_a_new_entry_point():
    """The guard must be able to fail, not merely pass today.

    A parser that matched nothing would pass this suite forever while the
    next ``run-continuous`` shipped unprotected — this project's inert-gate
    failure wearing a test's clothes.
    """
    bare = "def persists_a_run():\n    return run_pipeline(one, two)\n"
    assert _unprotected_runners(bare) == ["persists_a_run"], (
        "the guard cannot see an unprotected runner, so it would pass "
        "forever while the next entry point shipped unprotected"
    )

    applied = (
        "def persists_a_run():\n"
        "    cfg = for_live_intent(cfg, app_config)\n"
        "    return run_pipeline(cfg)\n"
    )
    assert _unprotected_runners(applied) == []

    # The closure case that produced the first version's false alarm.
    nested = (
        "def outer():\n"
        "    cfg = for_live_intent(cfg, app_config)\n"
        "    def job(event):\n"
        "        return run_pipeline(cfg)\n"
        "    return job\n"
    )
    assert _unprotected_runners(nested) == [], (
        "a nested runner protected by its enclosing scope is reported as "
        "unprotected; the guard is not reading how the config travels"
    )
