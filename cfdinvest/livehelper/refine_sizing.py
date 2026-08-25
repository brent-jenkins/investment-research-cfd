"""Adjusting size for what the per-trade checks cannot see.

:func:`~cfdinvest.livehelper.sizing_helper.compute_safe_sizing` answers "how
large may this trade be?" from the trade itself. It cannot know that you are
three weeks into a drawdown, that correlations have broken down, or that your
last six trades were revenge trades. This adjusts for those.

The rule that keeps this honest
-------------------------------
**A refinement may cut freely. It may never raise size above what the safety
engine already approved.**

Every reason to trade smaller — a drawdown, a broken correlation, a fading
edge — is a reason this layer can act on alone, because a smaller position is
inside every limit the larger one was. Reasons to trade *larger* are
different: they are proposals, and the ceiling stays owned by
``compute_safe_sizing``. Without that rule a meta-strategy layer that wanted
more size would become a way around the limits, and the limits would be
advisory.

So the multiplier is applied to the **intended** quantity, and the result is
then clamped to the recommended safe quantity. Raising the ceiling is not
something this module can do.

What it composes
----------------
Six adjustments, multiplied together and itemised so the total is
explainable. Compounding is the risk here — six independent 0.8× factors is
0.26×, which is not a position — so the product is clamped, and every
contributor is reported with its own factor rather than folded into one
number nobody can take apart.

Thresholds are read from the existing configuration wherever one exists.
:class:`RefinementConfig` holds only what is genuinely new.

Pure and deterministic.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any

from cfdinvest.livehelper.config import LiveHelperConfig
from cfdinvest.livehelper.context import TradingContext
from cfdinvest.livehelper.sizing_helper import DEFAULT_PRECISION, round_down

logger = logging.getLogger(__name__)

__all__ = [
    "RefinementConfig",
    "SizingRefinement",
    "refine_sizing",
]


def _finite(value: Any) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


@dataclass(frozen=True, slots=True)
class RefinementConfig:
    """Only the thresholds that do not already live somewhere else.

    Deliberately small. Drawdown *limits* belong to
    :class:`cfdinvest.risk.RiskLimits`, per-trade limits to
    :class:`~cfdinvest.livehelper.config.LiveHelperConfig`, staleness to
    :class:`cfdinvest.monitoring.MonitoringThresholds`. What is new here is
    how far into a drawdown you start trading smaller, and by how much — a
    question none of those answer.

    Attributes
    ----------
    drawdown_soft, drawdown_hard:
        Where size starts being cut, and where it is cut hard. Fractions,
        positive. A drawdown is when the edge is least certain and the
        temptation to trade bigger is greatest.
    drawdown_soft_factor, drawdown_hard_factor:
        What the size becomes at each. Not a continuous curve: two steps you
        can predict beat a formula you have to evaluate to know what your
        size will be.
    fading_edge_factor:
        Applied when recent expectancy is materially below the long-run
        figure. Half, because the honest response to "the edge may have gone"
        is smaller and still trading, not a stop.
    high_volatility_factor, correlation_breakdown_factor:
        Regime adjustments. Correlation breakdown is the quieter of the two:
        every position size assumes some diversification, and when
        correlations go to one the book is one position.
    bias_factor_floor:
        The most any behavioural finding may cut. Bias severities are the
        softest evidence here and should never dominate the arithmetic.
    minimum_multiplier, maximum_multiplier:
        The clamp on the product. Six 0.8x factors compound to 0.26x, which
        is not a position; the floor stops that becoming a trade nobody meant
        to take. The ceiling exists so an increase is always a proposal.
    """

    drawdown_soft: float = 0.05
    drawdown_hard: float = 0.10
    drawdown_soft_factor: float = 0.75
    drawdown_hard_factor: float = 0.50

    fading_edge_factor: float = 0.50
    no_edge_factor: float = 1.0

    high_volatility_factor: float = 0.70
    correlation_breakdown_factor: float = 0.75
    elevated_regime_factor: float = 0.85

    bias_factor_floor: float = 0.60

    minimum_multiplier: float = 0.25
    maximum_multiplier: float = 1.50

    def __post_init__(self) -> None:
        if not 0 < self.drawdown_soft < self.drawdown_hard:
            raise ValueError(
                "drawdown_soft must be positive and below drawdown_hard, got "
                f"{self.drawdown_soft} and {self.drawdown_hard}"
            )
        for name in (
            "drawdown_soft_factor", "drawdown_hard_factor", "fading_edge_factor",
            "high_volatility_factor", "correlation_breakdown_factor",
            "elevated_regime_factor", "bias_factor_floor", "minimum_multiplier",
        ):
            value = getattr(self, name)
            if not 0 < value <= 1:
                raise ValueError(
                    f"{name} is a fraction in (0, 1], got {value}"
                )
        if self.maximum_multiplier < 1:
            raise ValueError(
                f"maximum_multiplier must be at least 1, got "
                f"{self.maximum_multiplier}"
            )


@dataclass(frozen=True, slots=True)
class SizingRefinement:
    """The adjusted size, and every reason it is what it is.

    Attributes
    ----------
    multiplier:
        The product of the contributions, clamped. Applied to the *intended*
        quantity.
    adjusted_quantity:
        The result, capped at the safe quantity the sizing engine approved
        and rounded down to a tradeable precision.
    capped_by_safety:
        ``True`` when the refinement wanted more than the safety engine
        allows. Worth surfacing: it means a reason to size up was overruled,
        which is a different situation from one that was never proposed.
    contributions:
        Each factor with its own name, so a 0.4x can be taken apart.
    """

    multiplier: float = 1.0
    adjusted_quantity: float | None = None
    intended_quantity: float | None = None
    safe_quantity: float | None = None
    capped_by_safety: bool = False
    reasons: tuple[str, ...] = ()
    contributions: dict[str, float] = field(default_factory=dict)
    missing: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def reduces(self) -> bool:
        return self.multiplier < 0.999

    @property
    def tradeable(self) -> bool:
        return bool(self.adjusted_quantity and self.adjusted_quantity > 0)

    @property
    def unchanged(self) -> bool:
        return abs(self.multiplier - 1.0) < 1e-9


def refine_sizing(
    trade_intent,
    assessment=None,
    sizing=None,
    context: TradingContext | None = None,
    config: LiveHelperConfig | None = None,
    refinement: RefinementConfig | None = None,
    *,
    precision: int = DEFAULT_PRECISION,
) -> SizingRefinement:
    """Adjust the intended quantity for context the trade cannot carry.

    ``sizing`` is the :class:`~cfdinvest.livehelper.sizing_helper.
    SizingRecommendation` the safety engine already produced; its recommended
    quantity is the ceiling. Without it the refinement still computes a
    multiplier, and says that nothing capped it.
    """
    settings = refinement or RefinementConfig()
    where = context or TradingContext()

    contributions: dict[str, float] = {}
    reasons: list[str] = []

    _from_drawdown(where, settings, contributions, reasons)
    _from_edge(where, settings, contributions, reasons)
    _from_regime(where, settings, contributions, reasons)
    _from_bias(where, settings, contributions, reasons)
    _from_plan(trade_intent, where, settings, contributions, reasons)

    raw = 1.0
    for factor in contributions.values():
        raw *= factor

    multiplier = min(
        max(raw, settings.minimum_multiplier), settings.maximum_multiplier
    )
    if abs(multiplier - raw) > 1e-9:
        reasons.append(
            f"The combined adjustment of {raw:.2f}x was clamped to "
            f"{multiplier:.2f}x. Several small cuts compound faster than they "
            "read."
        )

    intended = getattr(trade_intent, "quantity", None)
    adjusted = None
    if _finite(intended):
        adjusted = round_down(float(intended) * multiplier, precision)

    safe = getattr(sizing, "recommended_quantity", None)
    capped = False
    if adjusted is not None and _finite(safe) and adjusted > float(safe):
        # The line this layer does not cross. A refinement may cut freely
        # because a smaller position is inside every limit the larger one
        # was; raising the ceiling is the safety engine's job and stays there.
        capped = True
        reasons.append(
            f"Refinement suggested {adjusted:,.4f} but the safe size is "
            f"{float(safe):,.4f}. Held at the safe size — this layer can cut, "
            "and cannot raise a limit."
        )
        adjusted = round_down(float(safe), precision)

    return SizingRefinement(
        multiplier=multiplier,
        adjusted_quantity=adjusted,
        intended_quantity=float(intended) if _finite(intended) else None,
        safe_quantity=float(safe) if _finite(safe) else None,
        capped_by_safety=capped,
        reasons=tuple(reasons),
        contributions=contributions,
        missing=where.missing,
        details={
            "raw_multiplier": raw,
            "clamped": abs(multiplier - raw) > 1e-9,
            "regimes": dict(where.regime.regimes),
            "drawdown": where.performance.current_drawdown,
            "expectancy": where.performance.expectancy,
            "recent_expectancy": where.performance.recent_expectancy,
        },
    )


# --------------------------------------------------------------------------- #
# The contributions
# --------------------------------------------------------------------------- #
def _from_drawdown(where, settings, contributions, reasons) -> None:
    """Trade smaller when the edge is least certain.

    A drawdown is exactly when the temptation to trade bigger is strongest
    and the evidence for doing so is weakest.
    """
    depth = where.performance.current_drawdown
    if not _finite(depth):
        return

    magnitude = abs(float(depth))
    if magnitude >= settings.drawdown_hard:
        contributions["drawdown"] = settings.drawdown_hard_factor
        reasons.append(
            f"{magnitude:.1%} into a drawdown — size cut to "
            f"{settings.drawdown_hard_factor:.0%}. This is where the edge is "
            "least certain and the urge to make it back is strongest."
        )
    elif magnitude >= settings.drawdown_soft:
        contributions["drawdown"] = settings.drawdown_soft_factor
        reasons.append(
            f"{magnitude:.1%} into a drawdown — size cut to "
            f"{settings.drawdown_soft_factor:.0%}."
        )


def _from_edge(where, settings, contributions, reasons) -> None:
    """Trade smaller when the recent record disagrees with the long one."""
    performance = where.performance
    if not performance.available:
        return

    if not performance.edge_is_measurable:
        # Explicitly no adjustment: an expectancy from a handful of trades
        # moves more than the thing it measures, and sizing off it would be
        # acting on noise while looking like acting on evidence.
        reasons.append(
            f"Only {performance.trades} trade(s) recorded, so no expectancy "
            "adjustment was made. Too few to distinguish an edge from a run."
        )
        return

    if performance.deteriorating:
        contributions["fading_edge"] = settings.fading_edge_factor
        reasons.append(
            f"Recent expectancy ({performance.recent_expectancy:,.2f}) is well "
            f"below the long-run figure ({performance.expectancy:,.2f}) — size "
            f"cut to {settings.fading_edge_factor:.0%} while it is unclear "
            "whether the edge has gone or this is a normal run."
        )
    elif _finite(performance.expectancy) and float(performance.expectancy) <= 0:
        contributions["negative_expectancy"] = settings.fading_edge_factor
        reasons.append(
            f"Expectancy over {performance.trades} trades is "
            f"{performance.expectancy:,.2f} — the strategy has not made money. "
            "Size cut; this is a reason to review it, not to press it."
        )


def _from_regime(where, settings, contributions, reasons) -> None:
    """Trade smaller when the market is not the one the sizing assumed."""
    regime = where.regime
    if not regime.available:
        return

    if regime.high_volatility:
        contributions["volatility_regime"] = settings.high_volatility_factor
        reasons.append(
            f"Volatility regime is {regime.volatility!r} — size cut to "
            f"{settings.high_volatility_factor:.0%}. The same stop distance is "
            "a shorter wait in a fast market."
        )

    if regime.correlations_broken:
        contributions["correlation_regime"] = settings.correlation_breakdown_factor
        reasons.append(
            f"Correlation regime is {regime.correlation!r} — size cut to "
            f"{settings.correlation_breakdown_factor:.0%}. Every position size "
            "assumes some diversification, and when correlations go to one the "
            "book is a single position."
        )

    # Only when nothing more specific already fired: a crisis that has already
    # cut size through volatility should not be charged twice for being a
    # crisis.
    if regime.elevated and not contributions.get("volatility_regime") and not (
        contributions.get("correlation_regime")
    ):
        contributions["elevated_regime"] = settings.elevated_regime_factor
        reasons.append(
            f"Elevated regime ({', '.join(regime.elevated)}) — size cut to "
            f"{settings.elevated_regime_factor:.0%}."
        )


def _from_bias(where, settings, contributions, reasons) -> None:
    """Adjust for what the behavioural layer found, if anything has.

    Only the biases that argue for a *smaller* position act on size.
    Fear-driven under-exposure is real and is not corrected here: a system
    that talked a frightened trader into a bigger position would be the worst
    thing in this repository.
    """
    bias = where.bias
    if not bias.available:
        return

    # Each of these means "you are likely to be trading too big right now".
    for name, label in (
        ("overconfidence", "overconfidence"),
        ("revenge_trading", "revenge trading"),
        ("leverage_creep", "leverage creep"),
    ):
        severity = bias.severity(name)
        if severity is None or severity < 0.5:
            continue
        factor = max(1.0 - float(severity) * 0.4, settings.bias_factor_floor)
        contributions[f"bias:{name}"] = factor
        reasons.append(
            f"Behavioural profile shows {label} at {float(severity):.0%} — "
            f"size cut to {factor:.0%}."
        )

    under = bias.severity("fear_driven_underexposure")
    if under is not None and under >= 0.5:
        reasons.append(
            f"The behavioural profile shows fear-driven under-exposure at "
            f"{float(under):.0%}. **No size increase has been made for it.** "
            "Trading larger because a model called you frightened is not a "
            "decision this should make for you."
        )


def _from_plan(trade_intent, where, settings, contributions, reasons) -> None:
    """What the meta-strategy asked for, treated as a request.

    A cut is applied. A raise is applied too, and then clamped by the safety
    engine downstream — which is the only reason it is safe to apply at all.
    """
    plan = where.plan
    if not plan.available:
        return

    stance = plan.stance_on(getattr(trade_intent, "instrument", ""))
    if stance == "avoided":
        contributions["meta_strategy"] = settings.bias_factor_floor
        reasons.append(
            f"The meta-strategy plan lists "
            f"{getattr(trade_intent, 'instrument', 'this instrument')} as one "
            f"to avoid — size cut to {settings.bias_factor_floor:.0%}."
        )
        return

    scale = plan.target_scale
    if not _finite(scale) or float(scale) <= 0:
        return

    contributions["meta_strategy"] = float(scale)
    if float(scale) < 1:
        reasons.append(
            f"The meta-strategy plan asks for {float(scale):.0%} of normal "
            f"size{f' ({plan.rationale})' if plan.rationale else ''}."
        )
    elif float(scale) > 1:
        reasons.append(
            f"The meta-strategy plan asks for {float(scale):.0%} of normal "
            "size. Treated as a request: it cannot lift the safe size, only "
            "use more of it."
        )
