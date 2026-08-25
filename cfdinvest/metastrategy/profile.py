"""The risk posture: how hard to push, and what that means numerically.

This is the first layer in the project whose stated purpose is to *increase*
risk. Everything else reduces, reports, or refuses. That difference deserves
saying out loud, because it changes what counts as enough evidence.

Why raising and cutting are not symmetric
-----------------------------------------
**Cutting size wrongly costs you money. Raising size wrongly can remove you
from the game.** Those are different kinds of error and they do not deserve
the same evidence bar, so the engine requires more to raise than to cut and
says so in the notes when it declines to.

The evidence available here is genuinely weak for the raising direction:
expectancy over a few dozen trades, a regime label from a classifier, a
drawdown figure. Any of those is strong enough to justify carrying less. None
is strong enough, alone, to justify carrying more.

The two tolerances can only tighten
------------------------------------
``safety_tolerance`` and ``margin_tolerance`` multiply the *configured* limits
and are clamped at ``1.0``. A posture may hold you further inside your own
limits; it can never move one outward. "Relaxed" in a note means *back toward
the limit you set*, never past it — a tolerance that could exceed the
configured limit would be a way around it, and the limits would be advisory.

The ceiling
-----------
``sizing_multiplier`` above ``1.0`` is a **request**. The refinement layer
caps the result at the size the safety engine already approved, so a posture
can only ever use headroom that was already granted — never create it. When a
request is overruled, that is reported rather than silently absorbed.
"""

from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Mapping

logger = logging.getLogger(__name__)

__all__ = [
    "POSTURES",
    "MetaStrategyProfile",
    "neutral_profile",
]

#: Least to most forward. ``neutral`` is the default and the one a thin
#: record earns: with nothing measurable, pushing and holding back are equally
#: unjustified, and neutral is the only honest answer.
POSTURES: tuple[str, ...] = (
    "conservative", "neutral", "assertive", "opportunistic",
)

#: What each posture does, numerically. Written down rather than computed so
#: the mapping can be argued with, and so a posture is a name for a set of
#: numbers rather than a mood.
#:
#: Note that ``sizing_multiplier`` reaches above 1.0 only for the two forward
#: postures, and even then it is a request the safety engine bounds.
POSTURE_SETTINGS: dict[str, dict[str, float]] = {
    "conservative": {
        "risk_budget": 0.005,
        "sizing_multiplier": 0.60,
        "exit_multiplier": 0.85,
        "safety_tolerance": 0.70,
        "margin_tolerance": 0.60,
        "bias_correction_strength": 1.00,
        "regime_alignment_strength": 1.00,
        "performance_alignment_strength": 1.00,
    },
    "neutral": {
        "risk_budget": 0.010,
        "sizing_multiplier": 1.00,
        "exit_multiplier": 1.00,
        "safety_tolerance": 0.90,
        "margin_tolerance": 0.85,
        "bias_correction_strength": 0.80,
        "regime_alignment_strength": 0.80,
        "performance_alignment_strength": 0.80,
    },
    "assertive": {
        "risk_budget": 0.015,
        "sizing_multiplier": 1.20,
        "exit_multiplier": 1.10,
        "safety_tolerance": 1.00,
        "margin_tolerance": 0.95,
        "bias_correction_strength": 0.60,
        "regime_alignment_strength": 0.60,
        "performance_alignment_strength": 0.60,
    },
    "opportunistic": {
        "risk_budget": 0.020,
        "sizing_multiplier": 1.40,
        "exit_multiplier": 1.20,
        "safety_tolerance": 1.00,
        "margin_tolerance": 1.00,
        "bias_correction_strength": 0.50,
        "regime_alignment_strength": 0.50,
        "performance_alignment_strength": 0.50,
    },
}

#: The most any posture may ask for. A request beyond this is not a posture,
#: it is a different strategy.
MAX_SIZING_REQUEST = 1.50


def _finite(value: Any) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


@dataclass(frozen=True, slots=True)
class MetaStrategyProfile:
    """How hard to push right now, and every reason for it.

    Attributes
    ----------
    risk_budget:
        Fraction of the account balance to risk per trade. A *target*, not a
        permission: the per-trade limit in
        :class:`~cfdinvest.livehelper.config.LiveHelperConfig` still binds,
        and this is only ever read down from it.
    sizing_multiplier:
        Applied to the refinement layer's multiplier. Above 1.0 is a request
        the safety engine bounds; it can use headroom already granted and
        never create any.
    exit_multiplier:
        Applied to the exit refinement's stop distance. Above 1.0 is a wider
        stop, which needs a smaller size to stay inside the risk limit — the
        exit layer re-checks that and reports the size it fits at.
    safety_tolerance, margin_tolerance:
        Multipliers on your configured limits, **clamped at 1.0**. They can
        hold you further inside a limit and can never move one outward.
    bias_correction_strength:
        How much of the behavioural findings to apply. Lower under a forward
        posture, because a forward posture is only reached when nothing
        behavioural was asserted in the first place.
    contributions:
        Each input's signed effect on the posture score, so a posture can be
        taken apart rather than trusted.
    """

    posture: str = "neutral"
    risk_budget: float = 0.010
    sizing_multiplier: float = 1.0
    exit_multiplier: float = 1.0
    safety_tolerance: float = 0.90
    margin_tolerance: float = 0.85
    bias_correction_strength: float = 0.80
    regime_alignment_strength: float = 0.80
    performance_alignment_strength: float = 0.80

    score: float = 0.0
    confidence: str = "low"
    account: str | None = None
    notes: tuple[str, ...] = ()
    contributions: dict[str, float] = field(default_factory=dict)
    missing: tuple[str, ...] = ()
    as_of: Any = None
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.posture not in POSTURES:
            raise ValueError(
                f"posture must be one of {', '.join(POSTURES)}, got "
                f"{self.posture!r}"
            )
        if not 0 < self.risk_budget <= 0.25:
            raise ValueError(
                f"risk_budget is a fraction of the account in (0, 0.25], got "
                f"{self.risk_budget}. Above a quarter is not a posture."
            )

        # The invariant this class exists to hold. Clamped rather than
        # rejected: a tolerance is a dial, and a dial turned past its stop
        # should rest at the stop rather than refuse to move.
        for name in ("safety_tolerance", "margin_tolerance"):
            value = float(getattr(self, name))
            if not 0 < value:
                raise ValueError(f"{name} must be positive, got {value}")
            if value > 1.0:
                logger.warning(
                    "%s of %.2f would move a configured limit outward; "
                    "clamped to 1.0", name, value,
                )
                object.__setattr__(self, name, 1.0)

        if self.sizing_multiplier > MAX_SIZING_REQUEST:
            object.__setattr__(self, "sizing_multiplier", MAX_SIZING_REQUEST)
        for name in (
            "bias_correction_strength", "regime_alignment_strength",
            "performance_alignment_strength",
        ):
            value = float(getattr(self, name))
            object.__setattr__(self, name, min(max(value, 0.0), 1.0))

    @property
    def index(self) -> int:
        """How far forward, as a position in :data:`POSTURES`."""
        return POSTURES.index(self.posture)

    @property
    def raises_size(self) -> bool:
        return self.sizing_multiplier > 1.0

    @property
    def is_forward(self) -> bool:
        return self.posture in ("assertive", "opportunistic")

    def limit_for(self, configured: float, *, margin: bool = False) -> float:
        """One of your limits, held in by the relevant tolerance.

        Only ever downward. This is the method that makes the invariant true
        in practice rather than only in the docstring.
        """
        if not _finite(configured):
            return configured
        tolerance = self.margin_tolerance if margin else self.safety_tolerance
        return float(configured) * min(float(tolerance), 1.0)

    def to_plan(self):
        """As the :class:`~cfdinvest.livehelper.context.MetaStrategyPlan` the
        refinement layer already reads.

        One shape, filled here and read there, rather than a second plan
        object that has to be kept in step. The contract was written before
        this layer existed and this fills it.
        """
        from cfdinvest.livehelper.context import MetaStrategyPlan

        return MetaStrategyPlan(
            target_scale=self.sizing_multiplier,
            rationale=self.notes[0] if self.notes else self.posture,
            as_of=self.as_of,
            available=True,
        )

    def to_document(self) -> dict[str, Any]:
        from cfdinvest.persist.writers import jsonable

        payload = asdict(self)
        payload["notes"] = list(self.notes)
        payload["missing"] = list(self.missing)
        payload["as_of"] = (
            self.as_of.isoformat()
            if hasattr(self.as_of, "isoformat")
            else (None if self.as_of is None else str(self.as_of))
        )
        return jsonable(payload)

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> "MetaStrategyProfile":
        known = set(cls.__slots__)
        payload = {k: v for k, v in dict(document).items() if k in known}
        for name in ("notes", "missing"):
            if payload.get(name) is not None:
                payload[name] = tuple(payload[name])
        return cls(**payload)

    def describe(self) -> dict[str, str]:
        """The profile as a table, for a page or an email."""
        return {
            "Posture": self.posture,
            "Risk budget": f"{self.risk_budget:.2%} of the account per trade",
            "Sizing": f"{self.sizing_multiplier:.0%}"
            + (" (a request the safety engine bounds)" if self.raises_size else ""),
            "Exit": f"{self.exit_multiplier:.0%} of the stop distance",
            "Safety tolerance": f"{self.safety_tolerance:.0%} of your limits",
            "Margin tolerance": f"{self.margin_tolerance:.0%} of your limit",
            "Bias correction": f"{self.bias_correction_strength:.0%}",
            "Regime alignment": f"{self.regime_alignment_strength:.0%}",
            "Performance alignment": f"{self.performance_alignment_strength:.0%}",
            "Confidence": self.confidence,
        }


def neutral_profile(**changes) -> MetaStrategyProfile:
    """The posture a thin record earns.

    With nothing measurable, pushing and holding back are equally
    unjustified. Neutral is not a hedge — it is the only claim the evidence
    supports.
    """
    settings = dict(POSTURE_SETTINGS["neutral"])
    settings.update(changes)
    return MetaStrategyProfile(posture="neutral", **settings)
