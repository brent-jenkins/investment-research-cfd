"""Every gate the dealing path applies must exist in the engine too.

Three times now the account has run a rule the backtest did not model, and
"measure through the engine" caught none of them -- because in all three the
measuring was fine and the *modelling* was incomplete:

  * the shadow book re-implemented the time stop, the cooldown, the blackout
    and the confidence gate, and implemented none of the breakout gate, the
    size hysteresis, the decayed trim, the leverage cap or the concentration
    cap. The demo account traded that rule set.
  * ``open_delay_minutes`` lived only in ``schedule/app.py``, so production
    refused to open in the first half hour while the backtest happily took
    228 opening-bar trades. Modelling it moved the figure 19.5pp without
    moving a single trade.
  * ``advisor/livecheck.py`` -- the last look -- refused roughly a fifth of
    candidate opens for a week while the engine took every one of them.
    Modelling it moved the figure 140.6pp, again without changing what the
    account did, and it turned a +100pp "finding" about morning entries into
    an artefact of the gate being invisible.

The common shape is not a bad measurement. It is that a gate living in the
dealing path has no obligation to have an ``ExecutionConfig`` counterpart,
and nothing failed the build when it did not. Some of these rules *cannot*
live in the engine -- the last look needs a live broker quote, which does not
exist at backtest decision time -- so the engine's job is to model them, and
this file is what obliges it to.

The check is deliberately total rather than selective. Every parameter of
``plan_orders`` must be classified: either it is a gate mapped to the engine
field that models it, or it is declared not-a-gate with a reason. A new
parameter of any kind fails the build until somebody has answered "does the
engine model this?" -- which is the question nobody asked three times.

Mutations checked, each reintroduced as a bug and confirmed to fail:
  M325 a new gate parameter added to plan_orders and never registered here
  M326 a mapping pointing at an ExecutionConfig field that does not exist
  M327 a gate silently moved into the not-a-gate list without a reason
  M328 the registry drifting out of step with the real signature
  M329 a mapped field that _execution_config never actually populates
  M330 a field wired into the main construction but not the fallback
  M332 a gate whose setting can be turned on without the engine noticing
       -- SURVIVES when the mutation deletes the comparison itself, which
       is the test being removed rather than a bug being introduced. The
       property is covered from two other directions (M330's forced
       fallback and M333), and the coverage guard below catches the
       likelier accidental form, a filter that quietly excludes everything
  M333 max_margin_usage mapped here and never handed to the engine
  M334 the fallback construction dropping the entry gate
"""

from __future__ import annotations

import inspect

import pytest

from cfdinvest.broker.capital import bridge
from cfdinvest.execution.frame import ExecutionConfig

#: Gate -> the ExecutionConfig attribute that models it, dotted for nested
#: config objects. The value is resolved against a real ExecutionConfig, so a
#: renamed or deleted field fails here rather than silently un-modelling a
#: rule the account still applies.
MODELLED_BY = {
    "open_allowed": "open_delay_minutes",
    "entry_confidence": "thresholds.entry_confidence",
    "rebalance_threshold": "rebalance_threshold",
    "exposure_per_unit_size": "exposure_per_unit_size",
    "limits": "concentration",
    "max_margin_usage": "max_margin_usage",
}

#: Gates whose engine field exists and is DELIBERATELY not populated by
#: ``_execution_config``. Each states why, because "modelled" and "wired" are
#: different claims and this file could not previously tell them apart.
#:
#: That gap cost a day. ``flatten_before_close`` was mapped to
#: ``overnight_profit_take`` here and never set, so every scheduled pipeline
#: run described a book carrying overnight on an engine whose time stop
#: cannot fire before the close. Monitoring then read that run's 99.8%
#: drawdown and reported the ACCOUNT wiped out, while the account was up
#: 0.94% on the day.
NOT_WIRED = {
    "flatten_before_close": (
        "the engine models it as `overnight_profit_take` and research passes "
        "it explicitly, but it may not reach the config that generates live "
        "intents. The engine derives 'session end' from the DATA -- the last "
        "stamp of each calendar day -- which is right in a backtest and "
        "wrong on the final, in-progress day: the newest bar is also today's "
        "last stamp, so the engine flattens the bar it is deciding on and "
        "`persist.intent` reads that flattened bar. Wired on 2026-09-08 and "
        "unwired on 2026-09-09 after it stopped the account dealing for a "
        "session. Measured on one moment's data: wired, 0 positions on the "
        "last bar; unwired, 6."
    ),
    "recheck": (
        "the bridge applies the last look against a live broker quote. "
        "Wiring the engine's version into the live path would filter twice "
        "where production filters once, and the engine's version compares "
        "two COMPLETED bars, which corresponds to nothing the bridge does. "
        "Worth +274pp in a backtest and still not wired, because the "
        "backtest and the intent generator share one config."
    ),
    "max_opens_per_pass": (
        "the bridge throttles opens per pass and the engine bar is an hour "
        "against a fifteen-minute pass, so the live budget of 3 permits 12 "
        "per bar -- at which the engine gate is exactly inert. Wiring it "
        "would throttle opens the bridge is already throttling."
    ),
}

#: Parameters that carry data rather than apply a rule. Each states why, so
#: that moving a gate in here to silence the test is a visible act rather
#: than a one-word edit.
NOT_A_GATE = {
    "opens_paused": (
        "an operational stop, not a strategy rule: it says whether the "
        "account may take risk at all, and a backtest of a paused account "
        "is an empty book. Nothing about which trades win depends on it."
    ),
    "signals": "the engine's own intended positions, which is the input",
    "markets": "broker market detail: epics, bid and offer",
    "positions": "what the account currently holds, read from the broker",
    "balance": "the account balance, which sizes rather than gates",
    "placed": "the app's own record, for reconciliation against the account",
    "fx_rate": "the account-currency conversion, modelled in execution.fx",
    "rechecked": "the last look's readings; the gate itself is `recheck`",
    "available": (
        "margin headroom read from the broker. Data, not a rule: the "
        "ceiling that uses it is `max_margin_usage`, and the engine's "
        "equivalent is equity, which it already has."
    ),
    "closing_soon": (
        "which instruments are within the FLATTEN window, read from the "
        "exchange calendar. Data, not a rule: the gate that uses it is "
        "`flatten_before_close`, driven since 2026-09-23 by "
        "`flatten_minutes_before_close` rather than `close_delay_minutes`. "
        "The engine models the flatten as an exit at the open of the "
        "session's last bar, which is coarser than either -- the hourly "
        "grid cannot express 30 minutes before a close."
    ),
    "session_ending": (
        "which instruments are inside the NO-NEW-OPENS window before the "
        "close. Decides nothing: `open_allowed` has already refused the "
        "open, and this only chooses which of two reasons the refusal "
        "carries, so the record stops calling the closing window the "
        "opening one."
    ),
    "session_open": (
        "the exchange calendar. Deliberately not modelled: the engine's panel "
        "is already session-restricted, so every bar it sees is one the "
        "market was open for, and a mask would be the identity."
    ),
}


#: The ``strategy`` setting that drives each mapped gate, where one does.
#:
#: Needed to tell two things apart that look identical from the engine side:
#: a field at its default because the gate is switched OFF everywhere, and a
#: field at its default because nobody wired it. ``max_margin_usage`` is the
#: first -- unset in config and unset in the engine, consistently off.
#: ``flatten_before_close`` was the second, and cost a day.
SETTING_FOR = {
    "max_margin_usage": "max_margin_usage",
    "open_allowed": "open_delay_minutes",
    "entry_confidence": "entry_confidence",
    "rebalance_threshold": "rebalance_threshold",
    "exposure_per_unit_size": "exposure_per_unit_size",
    "limits": None,
}


def _resolve(config, path: str):
    """Follow a dotted attribute path, raising if any hop is missing."""
    current = config
    for part in path.split("."):
        current = getattr(current, part)
    return current


def test_every_dealing_gate_is_classified():
    """No parameter of the dealing path may go unclassified.

    This is the check that would have caught all three failures. It does not
    ask whether the engine models a gate *correctly* -- no static check can
    -- but it does force the question to be asked and answered in writing
    before a gate can reach the account.
    """
    actual = set(inspect.signature(bridge.plan_orders).parameters)
    classified = set(MODELLED_BY) | set(NOT_WIRED) | set(NOT_A_GATE)

    unclassified = actual - classified
    assert not unclassified, (
        f"{sorted(unclassified)} reached the dealing path without anyone "
        "saying whether the engine models it. Add it to MODELLED_BY with the "
        "ExecutionConfig field that reproduces it, or to NOT_A_GATE with a "
        "reason. A gate the engine does not model is a backtest describing a "
        "strategy the account does not run -- which has happened three times."
    )

    stale = classified - actual
    assert not stale, (
        f"{sorted(stale)} is registered here but is no longer a parameter of "
        "plan_orders. A registry that has drifted from the thing it describes "
        "is worse than none, because it reads as coverage."
    )


def test_no_gate_is_classified_twice():
    """A parameter in two lists would be modelled and exempt at once."""
    for left, right, names in (
        (MODELLED_BY, NOT_A_GATE, "modelled and declared not a gate"),
        (MODELLED_BY, NOT_WIRED, "modelled-and-wired and declared unwired"),
        (NOT_WIRED, NOT_A_GATE, "declared unwired and declared not a gate"),
    ):
        both = set(left) & set(right)
        assert not both, f"{sorted(both)} is both {names}"


@pytest.mark.parametrize("gate,path", sorted(MODELLED_BY.items()))
def test_the_engine_field_that_models_a_gate_exists(gate, path):
    """The named field must be real.

    Asserted against a live ExecutionConfig rather than by parsing, so a
    field that is renamed, moved onto another config object or deleted fails
    here. Without this the registry could name anything and still pass.
    """
    config = ExecutionConfig()
    try:
        _resolve(config, path)
    except AttributeError as error:
        pytest.fail(
            f"the dealing path applies '{gate}' and this file claims "
            f"ExecutionConfig.{path} models it, but that field does not "
            f"exist: {error}. Either the engine never modelled the gate, or "
            "the field was renamed and the backtest quietly stopped "
            "reproducing what the account does."
        )


def test_the_not_a_gate_exemptions_carry_a_reason():
    """An exemption without a reason is a gate somebody wanted to stop failing."""
    for name, reason in NOT_A_GATE.items():
        assert isinstance(reason, str) and len(reason.split()) >= 4, (
            f"'{name}' is exempted with no real reason given. The exemption "
            "is the escape hatch, so it has to cost a sentence."
        )


def test_a_modelled_gate_is_actually_wired_into_the_production_config():
    """M329: a field that exists, is mapped here, and is never set.

    The old check asked whether ``ExecutionConfig`` HAS the field. It does
    not follow that ``_execution_config`` populates it, and for
    ``flatten_before_close`` it did not: the field existed, this file said it
    modelled the gate, and every scheduled run carried positions overnight
    while the account flattened at the bell. Measured on 2026-09-08 that
    difference was 274 percentage points, and monitoring read the resulting
    99.8% drawdown as the account being wiped out on a day the account made
    money.

    So "modelled" now means "wired", and anything deliberately left unwired
    has to say so in ``NOT_WIRED`` with its reason. The point is not that
    unwired is forbidden -- two gates are unwired on purpose -- it is that
    nobody can leave one unwired by accident.
    """
    from cfdinvest.config import load_config
    from cfdinvest.schedule.app import _execution_config

    settings = load_config()
    tickers = tuple(settings.market.tickers or ())
    live = _execution_config(tickers, settings.strategy, settings)
    default = ExecutionConfig()

    unwired = []
    for gate, path in sorted(MODELLED_BY.items()):
        got = _resolve(live, path)
        if got != _resolve(default, path):
            continue                      # the engine carries a real value
        setting = SETTING_FOR.get(gate)
        if setting is None:
            continue                      # no single setting drives it
        live_value = getattr(settings.strategy, setting, None)
        if live_value in (None, False, 0):
            continue                      # the gate is OFF on BOTH sides
        unwired.append((gate, path, setting, live_value))

    assert not unwired, (
        "these gates are ON in the shipped configuration and their engine "
        "field still carries its default, so the account applies a rule the "
        "backtest does not: "
        + "; ".join(f"strategy.{setting}={value!r} but "
                    f"ExecutionConfig.{path} is at its default"
                    for _, path, setting, value in unwired)
        + ". Wire it, or move it to NOT_WIRED with the reason."
    )


def test_the_unwired_exemptions_carry_a_reason():
    """An exemption without a reason is a gate somebody wanted to stop
    failing. The escape hatch has to cost a sentence, same as NOT_A_GATE."""
    for name, reason in NOT_WIRED.items():
        assert isinstance(reason, str) and len(reason.split()) >= 8, (
            f"'{name}' is declared unwired with no real reason. Leaving a "
            "gate unmodelled is a decision about what the account does, and "
            "it has to read as one."
        )


#: A value that switches each gate ON, for the positive wiring test. Chosen to
#: differ from the ExecutionConfig default so "the field moved" is unambiguous.
SWITCHED_ON = {
    "max_margin_usage": 0.5,
    "open_delay_minutes": 45,
    "entry_confidence": 0.9,
    "rebalance_threshold": 1.0,
    "exposure_per_unit_size": 6.0,
}


def test_turning_a_gate_on_moves_its_engine_field():
    """M332: the positive form, which no escape clause can swallow.

    Asking "is the field at its default?" needs an exemption for a gate that
    is legitimately switched off, and an exemption is a hole. This asks the
    question that has no legitimate negative answer: turn the setting ON and
    the engine field MUST move. A gate wired for one config value and not
    another does not exist.
    """
    import dataclasses

    from cfdinvest.config import load_config
    from cfdinvest.schedule.app import _execution_config

    settings = load_config()
    tickers = tuple(settings.market.tickers or ())
    default = ExecutionConfig()

    dead = []
    for gate, path in sorted(MODELLED_BY.items()):
        setting = SETTING_FOR.get(gate)
        if setting is None or setting not in SWITCHED_ON:
            continue
        strategy = dataclasses.replace(
            settings.strategy, **{setting: SWITCHED_ON[setting]})
        engine = _execution_config(tickers, strategy, settings)
        if _resolve(engine, path) == _resolve(default, path):
            dead.append((setting, path))

    assert not dead, (
        "switching these settings on changed nothing in the engine, so the "
        "backtest cannot express the rule the account applies: "
        + "; ".join(f"strategy.{a} -> ExecutionConfig.{b}" for a, b in dead)
    )


def test_both_execution_configs_carry_the_same_fields():
    """M330: the fallback modelling a different strategy from the main path.

    ``_execution_config`` builds an ExecutionConfig twice -- once normally and
    once when the broker rules table cannot be read. A field wired into one
    and not the other means the backtest silently changes strategy exactly
    when something else has already gone wrong, which is the worst possible
    moment for it. Read from the source, because the property is which
    keywords are present rather than what they evaluate to.
    """
    import ast
    import inspect
    import textwrap

    from cfdinvest.schedule import app

    tree = ast.parse(textwrap.dedent(inspect.getsource(app._execution_config)))
    builds = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", "") == "ExecutionConfig"
    ]
    assert len(builds) >= 2, (
        f"expected the main construction and the fallback, found {len(builds)}"
    )

    # Only the GATES. The fallback legitimately omits the financing rates,
    # the currency map and the earnings dates -- those are exactly what it
    # could not read. A gate is different: the account applies it whatever
    # happened here, so a construction that drops one models a strategy
    # nobody runs, at the moment something has already gone wrong.
    gates = {path.split(".")[0] for path in MODELLED_BY.values()}
    field_sets = [{kw.arg for kw in build.keywords if kw.arg} for build in builds]
    for index, fields in enumerate(field_sets):
        dropped = gates - fields
        assert not dropped, (
            f"ExecutionConfig construction {index} in _execution_config "
            f"drops the gate field(s) {sorted(dropped)}. Every construction "
            "must carry every gate, or one of them models a different "
            "strategy from the account -- and the fallback is taken "
            "precisely when the broker table could not be read."
        )


def test_the_fallback_config_carries_the_same_gate_VALUES(monkeypatch):
    """M330: the fallback present in name and inert in value.

    The structural check above sees that the keyword is written. It cannot
    see ``overnight_profit_take=None`` sitting where the live value belongs.
    So this forces the fallback -- the broker rules table unreadable, which
    is the only way in -- and compares the gates it produces against the
    main path's.

    The fallback is taken exactly when something has already gone wrong, so
    a divergence here is a strategy change nobody chose, arriving on the
    worst day.
    """
    from cfdinvest.advisor import capital_venue
    from cfdinvest.config import load_config
    from cfdinvest.schedule import app

    settings = load_config()
    tickers = tuple(settings.market.tickers or ())
    main = app._execution_config(tickers, settings.strategy, settings)

    def unreadable(*_args, **_kwargs):
        raise RuntimeError("broker rules unreadable, for the test")

    monkeypatch.setattr(capital_venue, "static_rules", unreadable)
    fallback = app._execution_config(tickers, settings.strategy, settings)

    differing = []
    for gate, path in sorted(MODELLED_BY.items()):
        want, got = _resolve(main, path), _resolve(fallback, path)
        if want != got:
            differing.append((gate, path, want, got))

    assert not differing, (
        "the fallback ExecutionConfig disagrees with the main one about a "
        "gate the account applies either way: "
        + "; ".join(f"{g}: ExecutionConfig.{p} is {b!r} on the fallback and "
                    f"{a!r} normally" for g, p, a, b in differing)
    )


def test_the_wiring_check_actually_examined_every_gate():
    """A filter that excludes everything reports a clean pass.

    The positive wiring test skips gates with no single driving setting.
    Without this, widening that skip -- or letting SETTING_FOR fall out of
    step with MODELLED_BY -- would make it inert while still passing, which
    is this project's most repeated failure and the reason every list here
    is checked for completeness rather than trusted.
    """
    driven = {g for g in MODELLED_BY if SETTING_FOR.get(g) is not None}
    testable = {g for g in driven if SETTING_FOR[g] in SWITCHED_ON}

    assert testable, "the positive wiring test examines no gate at all"
    missing = driven - testable
    assert not missing, (
        f"{sorted(missing)} name a driving setting but have no value in "
        "SWITCHED_ON, so the positive test silently skips them"
    )
    unknown = set(SETTING_FOR) - set(MODELLED_BY)
    assert not unknown, (
        f"{sorted(unknown)} is in SETTING_FOR but not MODELLED_BY; the two "
        "have drifted and the wiring check is asking about a gate that is "
        "no longer mapped"
    )
