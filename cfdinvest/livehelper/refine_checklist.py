"""The go/no-go, item by item, in the order a person actually checks them.

The verdict already exists — :attr:`TradeSafetyAssessment.go` is the answer.
What it does not give you is the *list*: which specific things were checked,
which passed, and which one is the reason if it did not.

That matters at the moment of trading. "UNSAFE" tells you to stop; it does not
tell you what to change, and a person under time pressure with a ticket open
needs the second thing.

**No item invents a check.** Every one reads a finding the safety engine
already produced or a figure the context already holds. This module decides
the ordering and the wording; the answers come from elsewhere. Four items are
new to this layer — regime, behavioural, meta-strategy and margin stability —
and they read the refinement context rather than defining thresholds of their
own.

Ordered by what stops you soonest. A checklist that puts "is the reward to
risk sensible" above "is there a stop" is one you can complete on a trade with
no stop.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from cfdinvest.livehelper.config import LiveHelperConfig
from cfdinvest.livehelper.context import TradingContext

logger = logging.getLogger(__name__)

__all__ = ["ChecklistItem", "Checklist", "refine_checklist"]

#: ``pass`` — checked and fine. ``fail`` — checked and not.
#: ``unchecked`` — could not be answered, which is **not** a pass.
STATES: tuple[str, ...] = ("pass", "fail", "unchecked")


@dataclass(frozen=True, slots=True)
class ChecklistItem:
    """One question, its answer, and what to do when the answer is no."""

    key: str
    question: str
    state: str
    detail: str = ""
    action: str = ""
    blocking: bool = True

    @property
    def failed(self) -> bool:
        return self.state == "fail"

    @property
    def stops_you(self) -> bool:
        """A failure that should stop the trade, as against inform it."""
        return self.failed and self.blocking

    @property
    def mark(self) -> str:
        return {"pass": "[x]", "fail": "[!]", "unchecked": "[ ]"}[self.state]


@dataclass(frozen=True, slots=True)
class Checklist:
    """Every item, and whether the trade survives them."""

    items: tuple[ChecklistItem, ...] = ()
    verdict: str | None = None
    missing: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def go(self) -> bool:
        """Nothing blocking failed, and nothing blocking went unchecked.

        An unchecked blocking item is not a pass. The whole reason a
        checklist exists is that "I did not look" and "it was fine" feel the
        same afterwards.
        """
        return not any(
            item.stops_you or (item.blocking and item.state == "unchecked")
            for item in self.items
        )

    @property
    def blockers(self) -> tuple[ChecklistItem, ...]:
        return tuple(item for item in self.items if item.stops_you)

    @property
    def unchecked(self) -> tuple[ChecklistItem, ...]:
        return tuple(item for item in self.items if item.state == "unchecked")

    @property
    def first_blocker(self) -> ChecklistItem | None:
        """The one to fix. Ordered, so this is the earliest thing wrong."""
        return self.blockers[0] if self.blockers else None

    def render(self) -> list[str]:
        """The list as lines, for a terminal or an email."""
        lines = []
        for item in self.items:
            lines.append(f"  {item.mark} {item.question}")
            if item.detail:
                lines.append(f"        {item.detail}")
            if item.failed and item.action:
                lines.append(f"        -> {item.action}")
        return lines


def refine_checklist(
    trade_intent,
    assessment=None,
    context: TradingContext | None = None,
    config: LiveHelperConfig | None = None,
    *,
    margin_warning=None,
) -> Checklist:
    """Build the list, in the order that stops you soonest."""
    limits = config or LiveHelperConfig()
    where = context or TradingContext()
    findings = {
        finding.check: finding for finding in getattr(assessment, "findings", ())
    }

    def from_finding(
        key: str, question: str, checks: tuple[str, ...], *, blocking: bool = True
    ) -> ChecklistItem:
        """An item whose answer is a finding the safety engine already made."""
        hit = next((findings[c] for c in checks if c in findings), None)
        if hit is None:
            return ChecklistItem(key, question, "pass", blocking=blocking)
        return ChecklistItem(
            key,
            question,
            "fail" if hit.severity in ("critical", "warning") else "pass",
            detail=hit.message,
            action=hit.recommendation,
            blocking=blocking and hit.severity == "critical",
        )

    items: list[ChecklistItem] = []

    if assessment is None:
        items.append(
            ChecklistItem(
                "assessed", "Has the trade been checked at all?", "unchecked",
                detail="No assessment was supplied.",
                action="Run the helper on this trade before placing it.",
            )
        )
        return Checklist(items=tuple(items), missing=where.missing)

    # -- the things that stop you first ------------------------------------ #
    items.append(from_finding("price", "Is there a price to size from?",
                              ("price",)))
    items.append(from_finding("stop", "Is there a stop-loss?",
                              ("stop_loss", "stop_side")))
    items.append(from_finding("risk", "Is the risk within your per-trade limit?",
                              ("risk",)))
    items.append(from_finding("exposure", "Is the exposure within its limit?",
                              ("exposure",)))
    items.append(from_finding("leverage", "Is the leverage within its limit?",
                              ("leverage",)))
    items.append(from_finding("margin", "Is there margin headroom afterwards?",
                              ("margin",)))
    items.append(from_finding("fx", "Is the currency exposure acceptable?",
                              ("fx", "fx_rate")))
    items.append(
        from_finding(
            "broker", "Will the Trading 212 ticket accept this order?",
            ("quantity_precision", "min_quantity", "max_quantity",
             "close_only", "quantity"),
        )
    )
    items.append(
        from_finding(
            "conditions", "Are the spread and volatility reasonable?",
            ("spread", "volatility"), blocking=False,
        )
    )

    # -- what this layer adds ---------------------------------------------- #
    items.append(_regime_item(where))
    items.append(_bias_item(where))
    items.append(_plan_item(trade_intent, where))
    items.append(_margin_stability_item(where, margin_warning))

    return Checklist(
        items=tuple(items),
        verdict=getattr(assessment, "status", None),
        missing=where.missing,
        details={"limits": limits.__class__.__name__},
    )


# --------------------------------------------------------------------------- #
# The four this layer adds
# --------------------------------------------------------------------------- #
def _regime_item(where) -> ChecklistItem:
    """Non-blocking on purpose: a crisis is also where the opportunity is."""
    regime = where.regime
    if not regime.available:
        return ChecklistItem(
            "regime", "Does the market regime suit this trade?", "unchecked",
            detail="No regime classification was available.",
            blocking=False,
        )
    if not regime.elevated:
        return ChecklistItem(
            "regime", "Does the market regime suit this trade?", "pass",
            detail=f"{', '.join(f'{k}: {v}' for k, v in regime.regimes.items())}",
            blocking=False,
        )
    return ChecklistItem(
        "regime", "Does the market regime suit this trade?", "fail",
        detail=f"Elevated: {', '.join(regime.elevated)}.",
        action=(
            "Not a reason to stand aside, and a reason to be smaller. Check "
            "the sizing refinement has been applied."
        ),
        blocking=False,
    )


def _bias_item(where) -> ChecklistItem:
    """The one item that is about you rather than the trade."""
    bias = where.bias
    if not bias.available:
        return ChecklistItem(
            "bias", "Are you in a state to be trading this?", "unchecked",
            detail="No behavioural profile has been built yet.",
            blocking=False,
        )

    severities = {
        name: bias.severity(name) for name in bias.measured
    }
    worst = max(
        ((name, value) for name, value in severities.items() if value is not None),
        key=lambda pair: pair[1],
        default=None,
    )
    if worst is None or worst[1] < 0.5:
        return ChecklistItem(
            "bias", "Are you in a state to be trading this?", "pass",
            blocking=False,
        )
    return ChecklistItem(
        "bias", "Are you in a state to be trading this?", "fail",
        detail=f"{worst[0].replace('_', ' ')} at {worst[1]:.0%}.",
        action=(
            "The setup may be fine and the moment may not be. Worth a pause "
            "rather than a refusal."
        ),
        blocking=False,
    )


def _plan_item(trade_intent, where) -> ChecklistItem:
    plan = where.plan
    if not plan.available:
        return ChecklistItem(
            "plan", "Does this fit the meta-strategy plan?", "unchecked",
            detail="No meta-strategy plan has been produced yet.",
            blocking=False,
        )
    stance = plan.stance_on(getattr(trade_intent, "instrument", ""))
    if stance == "avoided":
        return ChecklistItem(
            "plan", "Does this fit the meta-strategy plan?", "fail",
            detail=f"The plan lists this instrument as one to avoid."
                   f"{f' ({plan.rationale})' if plan.rationale else ''}",
            action="Take it smaller, or take a preferred instrument instead.",
            blocking=False,
        )
    return ChecklistItem(
        "plan", "Does this fit the meta-strategy plan?", "pass",
        detail="Preferred by the plan." if stance == "preferred" else "",
        blocking=False,
    )


def _margin_stability_item(where, margin_warning) -> ChecklistItem:
    """Not "is margin fine now" — the safety engine answers that.

    This asks whether it has been *moving*, which is the shape a margin call
    actually has and which no point-in-time check can see.
    """
    if margin_warning is None:
        if not where.margin.available:
            return ChecklistItem(
                "margin_stability", "Is margin usage stable?", "unchecked",
                detail="No margin history was available.",
                blocking=False,
            )
        return ChecklistItem(
            "margin_stability", "Is margin usage stable?", "unchecked",
            detail="No margin warning was computed.",
            blocking=False,
        )

    if margin_warning.severity == "ok":
        return ChecklistItem(
            "margin_stability", "Is margin usage stable?", "pass",
            detail=(
                f"{margin_warning.readings} reading(s) recorded."
                if margin_warning.readings else ""
            ),
            blocking=False,
        )
    return ChecklistItem(
        "margin_stability", "Is margin usage stable?", "fail",
        detail="; ".join(margin_warning.flags[:2]),
        action=(
            margin_warning.recommendations[0]
            if margin_warning.recommendations else ""
        ),
        blocking=margin_warning.act,
    )
