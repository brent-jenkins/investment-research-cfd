"""Adjusting the stop and the target for the market you are actually in.

The insight this module is built on
-----------------------------------
**A volatile market needs a *wider* stop, not a tighter one.**

The intuition runs the other way — things are dangerous, so get out quickly —
and following it guarantees being stopped out by noise on the way to being
right. A stop is a statement about how far the price may wander before the
idea is wrong, and in a fast market it wanders further for the same idea.

The correct response to volatility is a wider stop **and a smaller position**,
which is why this module and :mod:`~cfdinvest.livehelper.refine_sizing` are
separate and why the sizing one cuts in exactly the regimes this one widens.
Doing one without the other is worse than doing neither: a wider stop at the
same size is simply more risk.

Three rules
-----------
**Widening a stop increases risk per trade, and that is re-checked.** The
whole point of ``max_risk_per_trade_gbp`` is that it binds; a refinement that
quietly moved the stop past it would be a way around it. When a proposed stop
breaches the limit, the refinement reports the size that would make it fit
rather than the stop it wanted.

**A stop never crosses the entry.** A "refinement" that put the stop on the
wrong side would be an instruction to close at once, phrased as advice.

**Targets move more freely than stops.** Moving a target changes what you
might make; moving a stop changes what you can lose. They do not deserve the
same caution.

Pure and deterministic.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any

from cfdinvest.livehelper.config import LiveHelperConfig
from cfdinvest.livehelper.context import TradingContext

logger = logging.getLogger(__name__)

__all__ = ["ExitRefinementConfig", "ExitRefinement", "refine_exit"]


def _finite(value: Any) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


@dataclass(frozen=True, slots=True)
class ExitRefinementConfig:
    """How far the stop and target may move, and when.

    Attributes
    ----------
    high_volatility_stop, correlation_breakdown_stop:
        Multipliers on the stop *distance*. Above 1.0: wider. See the module
        docstring for why that direction is the right one.
    drawdown_target:
        Multiplier on the target distance while in a drawdown. Below 1.0:
        take profit sooner. A drawdown is a poor time to be holding out for
        the last third of a move.
    premature_exit_target, late_exit_target:
        Behavioural corrections, and the only two that pull in opposite
        directions. Applied to the target, not the stop, because the evidence
        for a behavioural pattern is softer than the evidence for a limit.
    margin_stress_target:
        When margin is tight, a nearer target is a way to free some. Applied
        only above ``margin_stress_threshold`` of the usage limit.
    max_stop_multiple, min_stop_multiple:
        The clamp. A stop that has trebled is not a refinement of the
        original idea; it is a different trade.
    """

    high_volatility_stop: float = 1.35
    correlation_breakdown_stop: float = 1.15
    calm_volatility_stop: float = 0.90

    drawdown_target: float = 0.80
    premature_exit_target: float = 1.20
    late_exit_target: float = 0.85
    margin_stress_target: float = 0.85
    margin_stress_threshold: float = 0.80

    max_stop_multiple: float = 2.00
    min_stop_multiple: float = 0.75
    max_target_multiple: float = 2.00
    min_target_multiple: float = 0.50

    def __post_init__(self) -> None:
        if not self.min_stop_multiple < 1 < self.max_stop_multiple:
            raise ValueError(
                "the stop clamp must straddle 1.0, got "
                f"{self.min_stop_multiple}-{self.max_stop_multiple}"
            )
        if not 0 < self.margin_stress_threshold <= 1:
            raise ValueError(
                "margin_stress_threshold is a fraction in (0, 1], got "
                f"{self.margin_stress_threshold}"
            )


@dataclass(frozen=True, slots=True)
class ExitRefinement:
    """Where the stop and target should sit, and why.

    Attributes
    ----------
    exit_multiplier:
        The stop-distance multiplier, clamped. Above 1.0 means a wider stop.
    risk_after_gbp:
        What the adjusted stop risks at the **intended** size. The figure
        that decides whether a wider stop is affordable.
    size_for_widened_stop:
        The quantity at which the wider stop fits inside the risk limit.
        Reported instead of refusing, because "wider stop, smaller size" is
        the answer and stating only half of it is not useful.
    """

    exit_multiplier: float = 1.0
    target_multiplier: float = 1.0
    adjusted_stop: float | None = None
    adjusted_tp: float | None = None
    original_stop: float | None = None
    original_tp: float | None = None
    risk_after_gbp: float | None = None
    size_for_widened_stop: float | None = None
    breaches_risk_limit: bool = False
    reasons: tuple[str, ...] = ()
    contributions: dict[str, float] = field(default_factory=dict)
    missing: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def widens(self) -> bool:
        return self.exit_multiplier > 1.001

    @property
    def unchanged(self) -> bool:
        return (
            abs(self.exit_multiplier - 1.0) < 1e-9
            and abs(self.target_multiplier - 1.0) < 1e-9
        )


def refine_exit(
    trade_intent,
    assessment=None,
    context: TradingContext | None = None,
    config: LiveHelperConfig | None = None,
    refinement: ExitRefinementConfig | None = None,
) -> ExitRefinement:
    """Move the stop and target for the regime, the drawdown and the profile.

    Returns the levels rather than applying them. Nothing here places or
    modifies an order.
    """
    limits = config or LiveHelperConfig()
    settings = refinement or ExitRefinementConfig()
    where = context or TradingContext()

    entry = getattr(trade_intent, "entry_price", None)
    stop = getattr(trade_intent, "stop_loss", None)
    target = getattr(trade_intent, "take_profit", None)
    direction = str(getattr(trade_intent, "direction", "long") or "long").lower()

    stop_factors: dict[str, float] = {}
    target_factors: dict[str, float] = {}
    reasons: list[str] = []

    _stop_from_regime(where, settings, stop_factors, reasons)
    _target_from_context(where, settings, limits, target_factors, reasons)
    _from_bias(where, settings, target_factors, reasons)

    stop_multiplier = _clamp(
        _product(stop_factors), settings.min_stop_multiple,
        settings.max_stop_multiple, "stop", reasons,
    )
    target_multiplier = _clamp(
        _product(target_factors), settings.min_target_multiple,
        settings.max_target_multiple, "target", reasons,
    )

    adjusted_stop = _move(entry, stop, stop_multiplier, direction, "stop")
    adjusted_target = _move(entry, target, target_multiplier, direction, "target")

    if adjusted_stop is not None and entry is not None:
        # A stop on the wrong side of the entry is an instruction to close at
        # once, dressed as advice. Refuse the move rather than emit it.
        wrong_side = (
            adjusted_stop >= float(entry) if direction == "long"
            else adjusted_stop <= float(entry)
        )
        if wrong_side:
            reasons.append(
                "The adjusted stop would sit on the wrong side of the entry, "
                "so it was left where it was."
            )
            adjusted_stop, stop_multiplier = (
                float(stop) if _finite(stop) else None, 1.0
            )

    risk, affordable, breaches = _risk_after(
        trade_intent, entry, adjusted_stop, limits, assessment, reasons
    )

    return ExitRefinement(
        exit_multiplier=stop_multiplier,
        target_multiplier=target_multiplier,
        adjusted_stop=adjusted_stop,
        adjusted_tp=adjusted_target,
        original_stop=float(stop) if _finite(stop) else None,
        original_tp=float(target) if _finite(target) else None,
        risk_after_gbp=risk,
        size_for_widened_stop=affordable,
        breaches_risk_limit=breaches,
        reasons=tuple(reasons),
        contributions={**stop_factors, **{f"target:{k}": v for k, v in
                                          target_factors.items()}},
        missing=where.missing,
        details={
            "direction": direction,
            "regimes": dict(where.regime.regimes),
            "stop_factors": dict(stop_factors),
            "target_factors": dict(target_factors),
        },
    )


# --------------------------------------------------------------------------- #
# The contributions
# --------------------------------------------------------------------------- #
def _product(factors: dict[str, float]) -> float:
    total = 1.0
    for value in factors.values():
        total *= value
    return total


def _clamp(value: float, low: float, high: float, what: str, reasons) -> float:
    clamped = min(max(value, low), high)
    if abs(clamped - value) > 1e-9:
        reasons.append(
            f"The combined {what} adjustment of {value:.2f}x was clamped to "
            f"{clamped:.2f}x — beyond that it is a different trade, not a "
            "refinement of this one."
        )
    return clamped


def _stop_from_regime(where, settings, factors, reasons) -> None:
    """Wider in a fast market. See the module docstring for why."""
    regime = where.regime
    if not regime.available:
        return

    if regime.high_volatility:
        factors["volatility_regime"] = settings.high_volatility_stop
        reasons.append(
            f"Volatility regime is {regime.volatility!r} — stop widened to "
            f"{settings.high_volatility_stop:.0%} of the distance you set. A "
            "tighter stop in a fast market is a stop that gets hit by noise "
            "on the way to being right. Pair it with the smaller size."
        )
    elif str(regime.volatility or "").lower() in {"low", "calm"}:
        factors["volatility_regime"] = settings.calm_volatility_stop
        reasons.append(
            f"Volatility regime is {regime.volatility!r} — the stop can sit "
            f"{settings.calm_volatility_stop:.0%} closer for the same idea."
        )

    if regime.correlations_broken:
        factors["correlation_regime"] = settings.correlation_breakdown_stop
        reasons.append(
            f"Correlation regime is {regime.correlation!r} — stop widened "
            "slightly: instruments move together in a breakdown and the price "
            "path is rougher than the instrument alone suggests."
        )


def _target_from_context(where, settings, limits, factors, reasons) -> None:
    """Take profit sooner in a drawdown, and when margin is tight."""
    depth = where.performance.current_drawdown
    if _finite(depth) and abs(float(depth)) >= 0.05:
        factors["drawdown"] = settings.drawdown_target
        reasons.append(
            f"{abs(float(depth)):.1%} into a drawdown — target brought in to "
            f"{settings.drawdown_target:.0%}. A drawdown is a poor time to be "
            "holding out for the last third of a move."
        )

    usage = where.margin.usage_pct
    if _finite(usage):
        stress = float(usage) / limits.max_margin_usage_pct_after_trade
        if stress >= settings.margin_stress_threshold:
            factors["margin_stress"] = settings.margin_stress_target
            reasons.append(
                f"Margin usage is {float(usage):.0%}, at "
                f"{stress:.0%} of your limit — target brought in to "
                f"{settings.margin_stress_target:.0%}. Closing sooner frees "
                "margin, and margin is what forces a close you did not choose."
            )


def _from_bias(where, settings, factors, reasons) -> None:
    """The two behavioural patterns that pull in opposite directions."""
    bias = where.bias
    if not bias.available:
        return

    premature = bias.severity("premature_exits")
    late = bias.severity("late_exits")

    if premature is not None and premature >= 0.5:
        factors["bias:premature_exits"] = settings.premature_exit_target
        reasons.append(
            f"The behavioural profile shows premature exits at "
            f"{float(premature):.0%} — target moved out to "
            f"{settings.premature_exit_target:.0%}, so the plan does the "
            "holding rather than your nerve."
        )
    if late is not None and late >= 0.5:
        factors["bias:late_exits"] = settings.late_exit_target
        reasons.append(
            f"The behavioural profile shows late exits at {float(late):.0%} — "
            f"target brought in to {settings.late_exit_target:.0%}, so the "
            "decision is made now rather than in the moment."
        )
    if (
        premature is not None and late is not None
        and premature >= 0.5 and late >= 0.5
    ):
        reasons.append(
            "The profile shows both premature and late exits, which mostly "
            "means neither is established. The two adjustments largely cancel."
        )


def _move(entry, level, multiplier, direction, kind) -> float | None:
    """Move a level by a multiple of its distance from the entry."""
    if not (_finite(entry) and _finite(level)):
        return None
    distance = abs(float(entry) - float(level)) * multiplier
    below = (direction == "long") == (kind == "stop")
    return float(entry) - distance if below else float(entry) + distance


def _risk_after(trade_intent, entry, stop, limits, assessment, reasons):
    """What the adjusted stop risks, and what size would make it fit.

    A wider stop is more risk at the same size. Reporting the affordable size
    rather than refusing the stop is the useful half of the answer: "wider
    stop, smaller position" is the response to volatility, and stating only
    the first part leaves the trade worse than not adjusting at all.
    """
    quantity = getattr(trade_intent, "quantity", None)
    if not (_finite(entry) and _finite(stop) and _finite(quantity)):
        return None, None, False

    per_unit = abs(float(entry) - float(stop))
    risk = per_unit * float(quantity)

    # The intent's own currency conversion is the safety engine's job; where
    # it computed a risk, use its ratio so one exchange rate is applied once.
    assessed = getattr(assessment, "risk_gbp", None)
    original_stop = getattr(trade_intent, "stop_loss", None)
    if _finite(assessed) and _finite(original_stop):
        original = abs(float(entry) - float(original_stop)) * float(quantity)
        if original > 0:
            risk = float(assessed) * (risk / original)

    if risk <= limits.max_risk_per_trade_gbp:
        return risk, None, False

    affordable = (
        float(quantity) * limits.max_risk_per_trade_gbp / risk if risk > 0 else 0.0
    )
    reasons.append(
        f"The wider stop risks £{risk:,.2f} at {float(quantity):,.4f} units, "
        f"over your £{limits.max_risk_per_trade_gbp:,.2f} limit. It fits at "
        f"{affordable:,.4f} units — that pairing, wider stop and smaller size, "
        "is the adjustment. A wider stop at the same size is just more risk."
    )
    return risk, affordable, True
