"""How good a trade this is, as distinct from whether it is allowed.

The safety engine answers "may I place this?" — a yes or no against limits.
That leaves a gap: a trade can clear every limit and still be a poor one,
because it is a fifth-best setup taken in a regime the strategy has never made
money in, at a moment when your last four exits were early.

This scores that. **It cannot change the verdict.** A high score on an UNSAFE
trade is still an UNSAFE trade — the score describes quality within what is
permitted, and letting it override a limit would make the limits advisory.

Where the score comes from
--------------------------
Every component reads something that already exists: the safety engine's own
findings, the analytics layer's expectancy, the regime classifier's labels,
the confirmation history's execution record. **No threshold is defined here**
that lives anywhere else — a component that needs a limit reads it from the
config that owns it.

The one thing that is new is the weighting, which is a judgement about what
matters and is written down as such.

A score with an absent component is scored on what is left, and says so. It
is *not* given the benefit of the doubt: an unmeasured component scores at the
midpoint rather than at full marks, because a trade nobody could assess is not
a good trade.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any

from cfdinvest.livehelper.config import LiveHelperConfig
from cfdinvest.livehelper.context import TradingContext

logger = logging.getLogger(__name__)

__all__ = ["QualityScore", "QualityComponent", "refine_quality", "GRADES"]

#: What a score means, worst first. Words rather than a bare number: "62" is
#: not a decision and "thin" is.
GRADES: tuple[tuple[float, str, str], ...] = (
    (0.80, "strong", "A good setup by every measure that could be taken."),
    (0.65, "sound", "A reasonable trade. Nothing argues strongly against it."),
    (0.50, "thin", "Permitted, but little in its favour. Worth asking whether "
                   "you would take it if you had not already decided to."),
    (0.00, "poor", "Inside your limits and hard to justify on its merits."),
)

#: What each component is worth. A judgement, written down so it can be
#: argued with rather than inferred from the arithmetic.
WEIGHTS: dict[str, float] = {
    "safety_margin": 0.30,
    "reward_risk": 0.20,
    "expectancy_alignment": 0.20,
    "regime_alignment": 0.15,
    "psychological_stability": 0.10,
    "execution_consistency": 0.05,
}

#: What an unmeasured component scores. Deliberately the midpoint: full marks
#: would let missing data flatter a trade, and zero would punish a fresh
#: install for being fresh.
UNMEASURED = 0.50


def _finite(value: Any) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


@dataclass(frozen=True, slots=True)
class QualityComponent:
    """One dimension of the score, and whether it could be measured."""

    name: str
    score: float
    weight: float
    measured: bool
    note: str = ""

    @property
    def contribution(self) -> float:
        return self.score * self.weight


@dataclass(frozen=True, slots=True)
class QualityScore:
    """The score, the grade, and every component behind it."""

    score: float = 0.0
    grade: str = "poor"
    verdict: str | None = None
    components: tuple[QualityComponent, ...] = ()
    reasons: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def percent(self) -> int:
        return int(round(self.score * 100))

    @property
    def measured_weight(self) -> float:
        """How much of the score rests on something actually measured."""
        return sum(c.weight for c in self.components if c.measured)

    @property
    def confident(self) -> bool:
        """Whether enough was measurable for the score to mean much."""
        return self.measured_weight >= 0.60

    @property
    def description(self) -> str:
        for floor, grade, text in GRADES:
            if self.score >= floor:
                return text
        return GRADES[-1][2]


def refine_quality(
    trade_intent,
    assessment=None,
    context: TradingContext | None = None,
    config: LiveHelperConfig | None = None,
    *,
    execution_history=None,
) -> QualityScore:
    """Score a trade's quality within what the limits already permit."""
    limits = config or LiveHelperConfig()
    where = context or TradingContext()
    components: list[QualityComponent] = []
    reasons: list[str] = []

    components.append(_safety_margin(assessment, limits, reasons))
    components.append(_reward_risk(assessment, reasons))
    components.append(_expectancy_alignment(where, reasons))
    components.append(_regime_alignment(where, reasons))
    components.append(_psychological_stability(where, reasons))
    components.append(_execution_consistency(execution_history, reasons))

    total = sum(component.contribution for component in components)
    weight = sum(component.weight for component in components)
    score = total / weight if weight else 0.0

    grade = next(name for floor, name, _ in GRADES if score >= floor)

    unmeasured = [c.name for c in components if not c.measured]
    if unmeasured:
        reasons.append(
            f"Scored on {sum(c.weight for c in components if c.measured):.0%} "
            f"of the available evidence; {', '.join(unmeasured)} could not be "
            "measured and scored at the midpoint rather than being assumed "
            "good."
        )

    status = getattr(assessment, "status", None)
    if status in ("UNSAFE", "BORDERLINE") and score >= 0.65:
        reasons.append(
            f"This scores well and the verdict is still {status}. The score "
            "describes quality within what is permitted; it cannot permit "
            "anything."
        )

    return QualityScore(
        score=score,
        grade=grade,
        verdict=status,
        components=tuple(components),
        reasons=tuple(reasons),
        missing=where.missing,
        details={
            "weights": dict(WEIGHTS),
            "measured_weight": sum(c.weight for c in components if c.measured),
        },
    )


# --------------------------------------------------------------------------- #
# The components
# --------------------------------------------------------------------------- #
def _safety_margin(assessment, limits, reasons) -> QualityComponent:
    """How much room is left under the limits, not merely that there is some.

    A trade at 99% of every limit passes and is fragile: one tick of slippage
    puts it over. Read from the assessment's own figures so the limits keep
    one owner.
    """
    weight = WEIGHTS["safety_margin"]
    if assessment is None:
        return QualityComponent("safety headroom", UNMEASURED, weight, False)

    ratios = []
    for value, limit in (
        (getattr(assessment, "risk_gbp", None), limits.max_risk_per_trade_gbp),
        (getattr(assessment, "exposure_gbp", None),
         limits.max_exposure_per_instrument_gbp),
        (getattr(assessment, "leverage", None), limits.max_leverage_per_trade),
        (getattr(assessment, "margin_usage_pct_after_trade", None),
         limits.max_margin_usage_pct_after_trade),
    ):
        if _finite(value) and _finite(limit) and float(limit) > 0:
            ratios.append(min(float(value) / float(limit), 2.0))

    if not ratios:
        return QualityComponent("safety headroom", UNMEASURED, weight, False)

    tightest = max(ratios)
    score = max(0.0, min(1.0, 1.0 - tightest))
    if tightest > 0.85:
        reasons.append(
            f"The trade sits at {tightest:.0%} of its tightest limit. It "
            "passes, and one tick of slippage puts it over."
        )
    return QualityComponent("safety headroom", score, weight, True)


def _reward_risk(assessment, reasons) -> QualityComponent:
    """Two-to-one is the conventional line, and it is a reasonable one."""
    weight = WEIGHTS["reward_risk"]
    ratio = getattr(assessment, "reward_risk_ratio", None)
    if not _finite(ratio):
        return QualityComponent("reward to risk", UNMEASURED, weight, False)

    value = float(ratio)
    score = max(0.0, min(1.0, value / 3.0))
    if value < 1.0:
        reasons.append(
            f"Reward to risk is {value:,.2f} — the trade risks more than it "
            "stands to make, so it needs to be right more often than not."
        )
    return QualityComponent("reward to risk", score, weight, True)


def _expectancy_alignment(where, reasons) -> QualityComponent:
    """Does the strategy actually make money, and is it still doing so?"""
    weight = WEIGHTS["expectancy_alignment"]
    performance = where.performance

    if not performance.edge_is_measurable:
        return QualityComponent(
            "expectancy", UNMEASURED, weight, False,
            note=f"only {performance.trades} trades",
        )

    expectancy = float(performance.expectancy)
    if expectancy <= 0:
        reasons.append(
            f"Expectancy over {performance.trades} trades is {expectancy:,.2f}. "
            "The strategy has not made money on this record."
        )
        return QualityComponent("expectancy", 0.0, weight, True)

    score = 1.0
    if performance.deteriorating:
        score = 0.4
        reasons.append(
            "Recent expectancy is well below the long-run figure — the edge "
            "may be fading, or this may be a normal run. Both look like this."
        )
    return QualityComponent("expectancy", score, weight, True)


def _regime_alignment(where, reasons) -> QualityComponent:
    """Is this the market the strategy was built for?"""
    weight = WEIGHTS["regime_alignment"]
    regime = where.regime
    if not regime.available:
        return QualityComponent("regime", UNMEASURED, weight, False)

    score = 1.0
    if regime.high_volatility:
        score -= 0.3
    if regime.correlations_broken:
        score -= 0.3
    if regime.elevated:
        score -= 0.1 * len(regime.elevated)

    score = max(0.0, min(1.0, score))
    if regime.elevated:
        reasons.append(
            f"Elevated regime: {', '.join(regime.elevated)}. Not a reason to "
            "stand aside — a crisis is also where the opportunity is — but the "
            "setup is being taken in a harder market than the average one."
        )
    return QualityComponent("regime", score, weight, True)


def _psychological_stability(where, reasons) -> QualityComponent:
    """What the behavioural layer found, when it exists."""
    weight = WEIGHTS["psychological_stability"]
    bias = where.bias
    if not bias.available or not bias.measured:
        return QualityComponent(
            "behavioural state", UNMEASURED, weight, False,
            note="no behavioural profile yet",
        )

    severities = [
        bias.severity(name) for name in bias.measured
    ]
    worst = max((s for s in severities if s is not None), default=0.0)
    score = max(0.0, 1.0 - float(worst))
    if worst >= 0.5:
        reasons.append(
            f"The behavioural profile shows a pattern at {float(worst):.0%}. "
            "The setup may be fine; the moment may not be."
        )
    return QualityComponent("behavioural state", score, weight, True)


def _execution_consistency(execution_history, reasons) -> QualityComponent:
    """How closely your fills have matched the plan lately.

    Read from the confirmation record: it is the only place that knows what
    you actually did, as against what was suggested.
    """
    weight = WEIGHTS["execution_consistency"]
    if execution_history is None or getattr(execution_history, "empty", True):
        return QualityComponent(
            "execution record", UNMEASURED, weight, False,
            note="nothing confirmed yet",
        )

    try:
        deviations = execution_history["quantity_deviation"].dropna()
    except (KeyError, TypeError):
        return QualityComponent("execution record", UNMEASURED, weight, False)

    if deviations.empty:
        return QualityComponent("execution record", UNMEASURED, weight, False)

    drift = float((deviations - 1.0).abs().mean())
    score = max(0.0, min(1.0, 1.0 - drift * 2))
    if drift > 0.2:
        reasons.append(
            f"Your recent fills have averaged {drift:.0%} away from the sizes "
            "recommended. The plan and the execution are drifting apart."
        )
    return QualityComponent("execution record", score, weight, True)
