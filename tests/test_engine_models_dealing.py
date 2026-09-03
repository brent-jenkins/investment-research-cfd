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
    "recheck": "entry_recheck",
    "entry_confidence": "thresholds.entry_confidence",
    "rebalance_threshold": "rebalance_threshold",
    "exposure_per_unit_size": "exposure_per_unit_size",
    "limits": "concentration",
    "max_opens_per_pass": "max_opens_per_pass",
    "max_margin_usage": "max_margin_usage",
}

#: Parameters that carry data rather than apply a rule. Each states why, so
#: that moving a gate in here to silence the test is a visible act rather
#: than a one-word edit.
NOT_A_GATE = {
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
    "session_open": (
        "the exchange calendar. Deliberately not modelled: the engine's panel "
        "is already session-restricted, so every bar it sees is one the "
        "market was open for, and a mask would be the identity."
    ),
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
    classified = set(MODELLED_BY) | set(NOT_A_GATE)

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
    """A parameter in both lists would be modelled and exempt at once."""
    both = set(MODELLED_BY) & set(NOT_A_GATE)
    assert not both, f"{sorted(both)} is both modelled and declared not a gate"


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
