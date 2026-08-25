"""Applying the posture to the refinements, without loosening anything.

The meta-strategy layer decides how hard to push. This applies that decision
to the sizing and exit refinements the helper already produced — and is the
place where the one dangerous thing it could do is prevented.

The ceiling
-----------
**A posture may ask for more size. It cannot create the room for it.**

``sizing_multiplier`` above 1.0 is applied to the refinement's multiplier, and
the result is then capped at the quantity the safety engine already approved.
So a forward posture can use headroom that was already granted and can never
manufacture any. When a request is overruled that is *reported* — an
overruled reason to size up is a different situation from one never made, and
silently absorbing it would make the posture look like it had been applied.

The tolerances
--------------
``safety_tolerance`` and ``margin_tolerance`` are clamped at 1.0 in the
profile itself, so applying them here can only ever hold you further inside
your own limits. A finding that says the posture "relaxed" a tolerance means
it moved back toward the limit you set, never past it.

Exits are the exception worth naming
------------------------------------
A forward posture *widens* the stop rather than tightening it, because a
posture that pushed size and tightened stops at the same time would be
compounding two risks while reading like one decision. The exit refinement
re-checks the risk limit on a widened stop and reports the size it fits at,
which is the pairing that makes it safe.

Pure: refinements and a profile in, adjusted refinements out. Nothing is
mutated — the originals are returned alongside.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field, replace
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["MetaAdjustment", "refine_with_meta"]


def _finite(value: Any) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


@dataclass(frozen=True, slots=True)
class MetaAdjustment:
    """What the posture changed, and what it was refused."""

    posture: str = "neutral"
    sizing: Any = None
    exit: Any = None
    original_sizing_multiplier: float | None = None
    original_exit_multiplier: float | None = None
    capped_by_safety: bool = False
    findings: tuple[str, ...] = ()
    recommendations: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def changed(self) -> bool:
        return bool(self.findings)

    @property
    def requested_more_than_allowed(self) -> bool:
        """A reason to size up that the safety engine refused.

        Worth its own name. It means the posture was applied and overruled,
        which reads very differently from a posture that never asked.
        """
        return self.capped_by_safety


def refine_with_meta(
    sizing_refinement=None,
    exit_refinement=None,
    safety_assessment=None,
    meta_strategy_profile=None,
    *,
    sizing_recommendation=None,
    config=None,
    precision: int | None = None,
) -> MetaAdjustment:
    """Apply a posture to the refinements the helper already produced.

    ``sizing_recommendation`` is the safety engine's own answer, and it is the
    ceiling. Without it nothing caps the request, and that is said rather
    than assumed safe.
    """
    from cfdinvest.livehelper.sizing_helper import DEFAULT_PRECISION, round_down

    profile = meta_strategy_profile
    if profile is None:
        return MetaAdjustment(
            sizing=sizing_refinement,
            exit=exit_refinement,
            findings=(),
            details={"applied": False, "reason": "no meta-strategy profile"},
        )

    places = DEFAULT_PRECISION if precision is None else precision
    findings: list[str] = []
    recommendations: list[str] = []

    adjusted_sizing = _apply_sizing(
        sizing_refinement, sizing_recommendation, profile, places,
        findings, recommendations,
    )
    adjusted_exit = _apply_exit(exit_refinement, profile, findings)
    _apply_tolerances(safety_assessment, profile, findings, recommendations)

    return MetaAdjustment(
        posture=profile.posture,
        sizing=adjusted_sizing or sizing_refinement,
        exit=adjusted_exit or exit_refinement,
        original_sizing_multiplier=getattr(sizing_refinement, "multiplier", None),
        original_exit_multiplier=getattr(exit_refinement, "exit_multiplier", None),
        capped_by_safety=bool(
            adjusted_sizing is not None
            and getattr(adjusted_sizing, "capped_by_safety", False)
            and profile.raises_size
        ),
        findings=tuple(findings),
        recommendations=tuple(recommendations),
        details={
            "applied": True,
            "posture": profile.posture,
            "sizing_multiplier": profile.sizing_multiplier,
            "exit_multiplier": profile.exit_multiplier,
            "safety_tolerance": profile.safety_tolerance,
            "margin_tolerance": profile.margin_tolerance,
        },
    )


def _apply_sizing(
    sizing, recommendation, profile, places, findings, recommendations
):
    """Scale the sizing refinement, then let the safety engine have the last word."""
    from cfdinvest.livehelper.sizing_helper import round_down

    if sizing is None:
        return None

    factor = float(profile.sizing_multiplier)
    if abs(factor - 1.0) < 1e-9:
        return sizing

    multiplier = float(getattr(sizing, "multiplier", 1.0)) * factor
    intended = getattr(sizing, "intended_quantity", None)

    adjusted = (
        round_down(float(intended) * multiplier, places)
        if _finite(intended) else getattr(sizing, "adjusted_quantity", None)
    )

    # The ceiling. Everything above this line is a proposal.
    safe = getattr(sizing, "safe_quantity", None)
    if not _finite(safe):
        safe = getattr(recommendation, "recommended_quantity", None)

    capped = False
    contributions = dict(getattr(sizing, "contributions", {}) or {})
    contributions[f"meta:{profile.posture}"] = factor
    reasons = list(getattr(sizing, "reasons", ()) or ())

    if factor > 1.0:
        findings.append(
            f"Meta-strategy increased sizing by "
            f"{(factor - 1.0) * 100:.0f}% ({profile.posture})."
        )
    else:
        findings.append(
            f"Meta-strategy reduced sizing to {factor:.0%} ({profile.posture})."
        )

    if adjusted is not None and _finite(safe) and adjusted > float(safe):
        capped = True
        adjusted = round_down(float(safe), places)
        message = (
            f"The {profile.posture} posture asked for more than the safe size "
            f"of {float(safe):,.4f} and was held there. A posture can use "
            "headroom the safety engine granted; it cannot create any."
        )
        findings.append(message)
        recommendations.append(message)
        reasons.append(message)
    elif factor > 1.0 and not _finite(safe):
        message = (
            f"The {profile.posture} posture raised the size and no "
            "safety-approved maximum was available to cap it against. Treat "
            "the result as unbounded until one is."
        )
        findings.append(message)
        recommendations.append(message)
        reasons.append(message)

    try:
        return replace(
            sizing,
            multiplier=multiplier,
            adjusted_quantity=adjusted,
            capped_by_safety=capped or getattr(sizing, "capped_by_safety", False),
            reasons=tuple(reasons),
            contributions=contributions,
        )
    except TypeError as error:  # pragma: no cover - a shape we do not own
        logger.warning("Could not apply the posture to the sizing: %s", error)
        return sizing


def _apply_exit(exit_refinement, profile, findings):
    """Scale the stop distance.

    A forward posture widens rather than tightens. Pushing size and
    tightening stops at once compounds two risks while reading like one
    decision, and the wider stop is re-checked against the risk limit by the
    exit layer itself.
    """
    if exit_refinement is None:
        return None

    factor = float(profile.exit_multiplier)
    if abs(factor - 1.0) < 1e-9:
        return exit_refinement

    multiplier = float(getattr(exit_refinement, "exit_multiplier", 1.0)) * factor
    entry = getattr(exit_refinement, "entry_price", None)
    original_stop = getattr(exit_refinement, "original_stop", None)
    adjusted_stop = getattr(exit_refinement, "adjusted_stop", None)

    if _finite(entry) and _finite(original_stop):
        # Rebuilt from the *original* distance and the combined multiple,
        # rather than scaling an already-scaled level a second time. An
        # earlier version tried to recover the entry from the two levels,
        # which cannot work when nothing had moved them apart yet.
        adjusted_stop = float(entry) + (
            float(original_stop) - float(entry)
        ) * multiplier

    if factor > 1.0:
        findings.append(
            f"Meta-strategy widened the stop to {multiplier:.0%} of the "
            f"distance you set ({profile.posture})."
        )
    else:
        findings.append(
            f"Meta-strategy reduced exit aggressiveness to {factor:.0%} "
            f"({profile.posture})."
        )

    reasons = list(getattr(exit_refinement, "reasons", ()) or ())
    reasons.append(
        f"The {profile.posture} posture applied {factor:.0%} to the stop "
        "distance. A wider stop at the same size is more risk, so check the "
        "size it fits at."
    )

    try:
        return replace(
            exit_refinement,
            exit_multiplier=multiplier,
            adjusted_stop=adjusted_stop,
            reasons=tuple(reasons),
        )
    except TypeError as error:  # pragma: no cover
        logger.warning("Could not apply the posture to the exit: %s", error)
        return exit_refinement


def _apply_tolerances(assessment, profile, findings, recommendations) -> None:
    """Report what the tolerances did, without touching a limit.

    The tolerances are read by whatever consults them; nothing is rewritten
    here. A layer that edited an assessment's limits in place would leave two
    versions of the same verdict in circulation.
    """
    if profile.safety_tolerance < 1.0:
        findings.append(
            f"Meta-strategy tightened safety tolerance to "
            f"{profile.safety_tolerance:.0%} of your limits ({profile.posture})."
        )
        recommendations.append(
            f"Hold {(1 - profile.safety_tolerance) * 100:.0f}% further inside "
            "each limit than usual while this posture stands."
        )
    else:
        findings.append(
            "Meta-strategy relaxed safety tolerance to your configured "
            "limits. It cannot go past them."
        )

    if profile.margin_tolerance < 1.0:
        findings.append(
            f"Meta-strategy tightened margin tolerance to "
            f"{profile.margin_tolerance:.0%} of your limit ({profile.posture})."
        )
    else:
        findings.append(
            "Meta-strategy relaxed margin tolerance to your configured limit. "
            "It cannot go past it."
        )
