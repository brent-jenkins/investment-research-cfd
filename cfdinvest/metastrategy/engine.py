"""Deciding the posture, from what the record actually supports.

Deterministic and explainable: the same inputs give the same posture, and
every input's effect on it is reported as a signed contribution you can add up
by hand.

The asymmetry, stated plainly
-----------------------------
**Cutting requires one reason. Raising requires all of them.**

Any single adverse finding — a drawdown, a broken correlation, an asserted
behavioural pattern — is enough to hold back. Moving *forward* needs a
measurable positive expectancy, no asserted behavioural pattern, no elevated
regime, and a shallow drawdown, together. Miss one and the posture stays
neutral, and the note says which.

That is not caution for its own sake. Cutting size wrongly costs money;
raising size wrongly can remove you from the game, and the evidence here —
expectancy over a few dozen trades, a classifier's label — is far better at
telling you when to hold back than when to push.

Real money counts against
-------------------------
A live account reaches a forward posture on strictly stronger evidence than a
simulation does. The inputs are identical; what differs is what a mistake
costs, and that belongs in the decision rather than in a disclaimer.

Nothing here places a trade, overrides a limit, or recomputes a position.
"""

from __future__ import annotations

import logging
import math
from typing import Any

import pandas as pd

from cfdinvest.metastrategy.profile import (
    POSTURE_SETTINGS,
    POSTURES,
    MetaStrategyProfile,
)

logger = logging.getLogger(__name__)

__all__ = [
    "FORWARD_REQUIREMENTS",
    "compute_meta_strategy",
    "posture_for",
]

#: What must *all* be true before a forward posture is available. Each is a
#: reason to be cautious in its own right; the point is that their absence is
#: not a reason to push, so every one of them has to be clear.
FORWARD_REQUIREMENTS: tuple[str, ...] = (
    "an established positive expectancy, not merely a positive one",
    "no asserted behavioural pattern",
    "no elevated market regime",
    "a drawdown under 5%",
)

#: Score at which each posture becomes available, worst first. The gaps are
#: uneven on purpose: neutral is easy to reach and assertive is not.
_BANDS: tuple[tuple[float, str], ...] = (
    (1.75, "opportunistic"),
    (1.00, "assertive"),
    (-0.75, "neutral"),
    (float("-inf"), "conservative"),
)


def _finite(value: Any) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def posture_for(score: float, *, forward_allowed: bool) -> str:
    """The posture a score earns, given whether forward is available at all.

    ``forward_allowed`` is the gate, not a modifier. A score high enough for
    ``assertive`` still gives ``neutral`` when any forward requirement is
    unmet — the score says how good things look and the gate says whether the
    evidence is the kind that supports acting on it.
    """
    for floor, posture in _BANDS:
        if score >= floor:
            if not forward_allowed and posture in ("assertive", "opportunistic"):
                return "neutral"
            return posture
    return "conservative"


def compute_meta_strategy(
    insights=None,
    bias_profile=None,
    regime_profile=None,
    pnl_history=None,
    account_state=None,
    *,
    now=None,
) -> MetaStrategyProfile:
    """Work out the posture. Pure, deterministic, explainable.

    ``account_state`` may be a string (``"live"``), a mapping, or anything
    with an ``account``/``type`` attribute — the router and the registry both
    produce something usable, and requiring one shape would make this
    awkward to call from either.
    """
    moment = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="UTC")
    if moment.tz is None:
        moment = moment.tz_localize("UTC")

    contributions: dict[str, float] = {}
    notes: list[str] = []
    missing: list[str] = []
    unmet: list[str] = []

    account = _account_of(account_state)
    real_money = account == "live"

    expectancy_ok = _from_expectancy(insights, contributions, notes, missing, unmet)
    drawdown_ok = _from_drawdown(pnl_history, contributions, notes, missing, unmet)
    regime_ok = _from_regime(regime_profile, contributions, notes, missing, unmet)
    bias_ok = _from_bias(bias_profile, contributions, notes, missing, unmet)
    _from_account(account, contributions, notes)

    score = sum(contributions.values())
    forward_allowed = expectancy_ok and drawdown_ok and regime_ok and bias_ok

    if not forward_allowed and unmet:
        notes.append(
            "A forward posture needs every one of "
            f"{len(FORWARD_REQUIREMENTS)} things to be clear at once, and "
            f"{len(unmet)} are not: {'; '.join(unmet)}. Any of these alone is "
            "a reason to hold back, and their absence is not a reason to push."
        )

    posture = posture_for(score, forward_allowed=forward_allowed)

    # Real money reaches forward on strictly stronger evidence. The inputs are
    # the same; what differs is what a mistake costs.
    if real_money and posture == "opportunistic":
        posture = "assertive"
        notes.append(
            "Held at assertive rather than opportunistic because this is the "
            "real-money account. The evidence is identical; what a mistake "
            "costs is not."
        )

    # No separate low-confidence guard, deliberately: every path that
    # produces low confidence also closes the gate, because an unestablished
    # expectancy is itself an unmet requirement. A guard here would be code
    # that cannot run, and code that cannot run cannot be trusted to. The
    # coupling is asserted by a test rather than defended twice.
    confidence = _confidence(missing, insights)

    settings = dict(POSTURE_SETTINGS[posture])

    if not notes:
        notes.append(
            "Nothing argues strongly either way, so the posture is neutral. "
            "That is a finding, not a default."
        )

    return MetaStrategyProfile(
        posture=posture,
        score=score,
        confidence=confidence,
        account=account,
        notes=tuple(notes),
        contributions=contributions,
        missing=tuple(missing),
        as_of=moment,
        details={
            "forward_allowed": forward_allowed,
            "unmet_requirements": tuple(unmet),
            "requirements": FORWARD_REQUIREMENTS,
            "bands": {name: floor for floor, name in _BANDS},
            "real_money": real_money,
        },
        **settings,
    )


# --------------------------------------------------------------------------- #
# What each input contributes
# --------------------------------------------------------------------------- #
def _from_expectancy(insights, contributions, notes, missing, unmet) -> bool:
    """Does the record show an edge, and is it still there?

    Read from the insights layer's own finding rather than recomputed, so
    there is one expectancy in the project and not two.
    """
    if insights is None:
        missing.append("the insights report")
        unmet.append("expectancy could not be read")
        return False

    finding = None
    getter = getattr(insights, "get", None)
    if callable(getter):
        finding = getter("expectancy_trend")

    if finding is None or not getattr(finding, "usable", False):
        missing.append("a measurable expectancy")
        unmet.append("expectancy is not measurable yet")
        notes.append(
            "Expectancy could not be measured, so nothing was added for it. "
            "Too few trades to distinguish an edge from a run."
        )
        return False

    buckets = getattr(finding, "buckets", ()) or ()
    recent = next(
        (b.expectancy for b in reversed(buckets) if b.expectancy is not None), None
    )
    if not _finite(recent):
        missing.append("a measurable expectancy")
        unmet.append("expectancy is not measurable yet")
        return False

    recent = float(recent)
    if recent > 0:
        contributions["expectancy"] = 1.0
        notes.append(
            f"Recent expectancy is {recent:,.2f} a trade over "
            f"{finding.trades} trades."
        )
        # Positive is not the same as established. A positive expectancy over
        # a dozen trades is exactly the weak evidence this layer says must not
        # justify raising -- it counts toward the score, and it does not open
        # the gate. Without this the whole forward path could rest on a run.
        if getattr(finding, "confidence", "") != "confident":
            notes.append(
                f"That expectancy is {finding.confidence} rather than "
                "established, so it counts toward the posture without opening "
                "the way to a forward one."
            )
            unmet.append("the expectancy is not yet established")
            return False
        return True

    contributions["expectancy"] = -1.5
    notes.append(
        f"Recent expectancy is {recent:,.2f} a trade. The strategy has not "
        "been making money, which is a reason to carry less and to look at "
        "it — not a reason to press."
    )
    unmet.append("recent expectancy is not positive")
    return False


def _from_drawdown(pnl_history, contributions, notes, missing, unmet) -> bool:
    """How deep in a hole the account is.

    Accepts the risk layer's P&L metrics, a mapping, or a bare number, since
    all three turn up depending on who is calling.
    """
    depth = _drawdown_of(pnl_history)
    if depth is None:
        missing.append("the drawdown")
        unmet.append("the drawdown could not be read")
        return False

    magnitude = abs(float(depth))
    if magnitude >= 0.10:
        contributions["drawdown"] = -1.5
        notes.append(
            f"{magnitude:.1%} into a drawdown. This is where the edge is least "
            "certain and the urge to make it back is strongest."
        )
        unmet.append(f"the drawdown is {magnitude:.0%}")
        return False
    if magnitude >= 0.05:
        contributions["drawdown"] = -0.75
        notes.append(f"{magnitude:.1%} into a drawdown.")
        unmet.append(f"the drawdown is {magnitude:.0%}")
        return False

    contributions["drawdown"] = 0.5
    return True


def _drawdown_of(pnl_history) -> float | None:
    if pnl_history is None:
        return None
    if isinstance(pnl_history, (int, float)):
        return float(pnl_history) if _finite(pnl_history) else None
    if isinstance(pnl_history, dict):
        for key in ("current_drawdown", "current_drawdown_fraction",
                    "max_drawdown"):
            if _finite(pnl_history.get(key)):
                return float(pnl_history[key])
        return None
    for name in ("current_drawdown", "max_drawdown"):
        value = getattr(pnl_history, name, None)
        if _finite(value):
            return float(value)
    return None


def _from_regime(regime_profile, contributions, notes, missing, unmet) -> bool:
    """Whether the market is the one the sizing assumed.

    What counts as elevated is asked of the regime profile rather than
    decided again here.
    """
    if regime_profile is None or not getattr(regime_profile, "available", False):
        missing.append("the market regime")
        unmet.append("the regime could not be read")
        return False

    elevated = tuple(getattr(regime_profile, "elevated", ()) or ())
    if elevated:
        contributions["regime"] = -0.5 * len(elevated)
        notes.append(
            f"Elevated regime: {', '.join(elevated)}. Not a reason to stand "
            "aside — a crisis is also where the opportunity is — but not a "
            "market to be pushing into either."
        )
        unmet.append(f"the regime is elevated ({', '.join(elevated)})")
        return False

    trend = str(getattr(regime_profile, "trend", "") or "").lower()
    if trend in ("up", "strong", "trending", "trend_up", "bull"):
        contributions["regime"] = 0.5
        notes.append(f"Trend regime is {trend!r}, which is what this is for.")
    else:
        contributions["regime"] = 0.25
    return True


def _from_bias(bias_profile, contributions, notes, missing, unmet) -> bool:
    """What the behavioural layer found.

    Behavioural findings only ever count *against*. There is no pattern whose
    presence argues for a larger position, and a system that found one would
    be telling someone their mood was a reason to trade bigger.
    """
    if bias_profile is None or not getattr(bias_profile, "available", False):
        missing.append("a behavioural profile")
        unmet.append("no behavioural profile has been built")
        return False

    measured = tuple(getattr(bias_profile, "measured", ()) or ())
    if not measured:
        missing.append("a behavioural profile")
        unmet.append("nothing behavioural could be measured")
        return False

    worst_name, worst = None, 0.0
    for name in measured:
        severity = bias_profile.severity(name)
        if severity is not None and float(severity) > worst:
            worst_name, worst = name, float(severity)

    if worst >= 0.5:
        contributions["behaviour"] = -1.0 * worst
        notes.append(
            f"The behavioural profile shows {worst_name.replace('_', ' ')} at "
            f"{worst:.0%}. The setup may be fine; the moment may not be."
        )
        unmet.append(f"{worst_name.replace('_', ' ')} is asserted at {worst:.0%}")
        return False

    contributions["behaviour"] = 0.25
    return True


def _from_account(account, contributions, notes) -> None:
    """Real money counts against a forward posture."""
    if account == "live":
        contributions["account"] = -0.25
        return
    if account == "simulation":
        contributions["account"] = 0.25
        notes.append(
            "This is the simulation account, so the posture is free to be "
            "wrong in a way it is not on the others."
        )


def _account_of(account_state) -> str | None:
    """Whatever was passed, as an account name."""
    if account_state is None:
        return None
    if isinstance(account_state, str):
        return account_state.lower()
    if isinstance(account_state, dict):
        for key in ("account", "type", "name"):
            if account_state.get(key):
                return str(account_state[key]).lower()
        return None
    for name in ("type", "account", "name"):
        value = getattr(account_state, name, None)
        if value:
            return str(value).lower()
    return None


def _confidence(missing: list[str], insights) -> str:
    """How much of the record the posture rests on.

    Three words rather than a number, and the only one that matters is
    ``low`` — which forbids a forward posture outright.
    """
    if len(missing) >= 3:
        return "low"

    finding = None
    getter = getattr(insights, "get", None)
    if callable(getter):
        finding = getter("expectancy_trend")
    if finding is not None and getattr(finding, "confidence", "") == "insufficient":
        return "low"

    if missing:
        return "medium"

    if getattr(finding, "confidence", "") == "confident":
        return "high"
    return "medium"
