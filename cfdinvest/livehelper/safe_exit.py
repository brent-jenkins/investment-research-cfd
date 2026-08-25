"""Getting out: how much, and whether it has to be now.

Every other module here is about a trade you have not placed. This one is
about one you are already in and would like to be less in.

The thing it exists to avoid
----------------------------
**A forced closure.** Trading 212 closes positions on a margin ratio without
warning, and it chooses which — typically the largest, which is rarely the one
you would have picked. Anything you do yourself, however clumsy, beats that,
because at least you choose the position and the moment.

So the question is not "should I close" but "what is the smallest exit that
puts the account out of reach of a decision it does not get to make". A full
exit when a third would do is a cost paid for nothing; a third when a full
exit is needed is a decision deferred to the broker.

What it will not do
-------------------
**It does not place anything.** It computes what to close and says so.

**It does not tell you to close a winner to fund a loser**, or the reverse.
Which position to reduce is a judgement about the trades themselves, and this
knows only the arithmetic of margin. Where it can, it says how much needs to
go and leaves the choice of which where it belongs.

**It will not recommend a partial exit below the instrument's minimum size.**
An exit you cannot place is not an exit, and Trading 212 will simply not fill
it — see :mod:`~cfdinvest.livehelper.t212_rules`.

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

__all__ = ["ExitPlan", "SafeExitConfig", "compute_safe_exit", "URGENCIES"]

#: How soon, worst first.
URGENCIES: tuple[str, ...] = ("now", "soon", "when convenient", "not needed")


def _finite(value: Any) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


@dataclass(frozen=True, slots=True)
class SafeExitConfig:
    """How much headroom an exit should buy.

    The margin *limit* is not here — it belongs to
    :class:`~cfdinvest.livehelper.config.LiveHelperConfig`. What is new is
    where to aim once you have decided to reduce.

    Attributes
    ----------
    target_usage_fraction:
        Where to land, as a share of your usage limit. Exiting to sit exactly
        on the limit means the next tick puts you back over, so the exit has
        to overshoot to be worth making.
    forced_closure_margin:
        Usage above which this is urgent rather than advisable. Trading 212's
        own close-out level is higher than your limit; the gap between them is
        the room you have to act in, and it is smaller than it looks.
    minimum_partial:
        The smallest fraction worth exiting. Below this the spread and the
        conversion fee cost more than the headroom is worth.
    """

    target_usage_fraction: float = 0.70
    forced_closure_margin: float = 0.90
    minimum_partial: float = 0.10

    def __post_init__(self) -> None:
        for name in (
            "target_usage_fraction", "forced_closure_margin", "minimum_partial"
        ):
            value = getattr(self, name)
            if not 0 < value <= 1:
                raise ValueError(f"{name} is a fraction in (0, 1], got {value}")
        if self.target_usage_fraction >= self.forced_closure_margin:
            raise ValueError(
                "target_usage_fraction must be below forced_closure_margin, "
                f"got {self.target_usage_fraction} and "
                f"{self.forced_closure_margin}"
            )


@dataclass(frozen=True, slots=True)
class ExitPlan:
    """What to close, how much of it, and how soon.

    Attributes
    ----------
    kind:
        ``"none"``, ``"partial"``, ``"full"`` or ``"margin_relief"``. The
        last is a partial exit taken for the account rather than for the
        trade, and is worth naming separately: it is the one you would not
        otherwise have made.
    fraction:
        How much of the position to close, in ``[0, 1]``.
    quantity:
        The units, rounded to something the ticket will take when the
        instrument's rules are known.
    """

    kind: str = "none"
    fraction: float = 0.0
    quantity: float | None = None
    remaining_quantity: float | None = None
    urgency: str = "not needed"
    frees_margin_gbp: float | None = None
    usage_after: float | None = None
    reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def needed(self) -> bool:
        return self.kind != "none"

    @property
    def urgent(self) -> bool:
        return self.urgency == "now"


def compute_safe_exit(
    trade_intent=None,
    assessment=None,
    context: TradingContext | None = None,
    config: LiveHelperConfig | None = None,
    settings: SafeExitConfig | None = None,
    *,
    position_quantity: float | None = None,
    rules=None,
) -> ExitPlan:
    """The smallest exit that puts the account out of reach of a forced close.

    ``position_quantity`` is what you are holding; without it the intent's
    quantity is used. ``rules`` is a
    :class:`~cfdinvest.livehelper.t212_rules.RuleTable`, used to keep a
    partial exit above the instrument's minimum size.
    """
    limits = config or LiveHelperConfig()
    thresholds = settings or SafeExitConfig()
    where = context or TradingContext()

    quantity = position_quantity
    if not _finite(quantity):
        quantity = getattr(trade_intent, "quantity", None)

    usage = where.margin.usage_pct
    if not _finite(usage):
        usage = getattr(assessment, "margin_usage_pct_after_trade", None)

    reasons: list[str] = []
    warnings: list[str] = []

    if not _finite(usage):
        return ExitPlan(
            urgency="not needed",
            warnings=(
                "Margin usage could not be measured, so no exit was computed. "
                "Unknown margin is not low margin — record the instrument's "
                "leverage.",
            ),
            missing=where.missing,
        )

    usage = float(usage)
    limit = limits.max_margin_usage_pct_after_trade
    stress = usage / limit if limit > 0 else None

    if stress is None or stress < thresholds.target_usage_fraction:
        return ExitPlan(
            urgency="not needed",
            usage_after=usage,
            reasons=(
                f"Margin usage is {usage:.0%}, comfortably inside the "
                f"{limit:.0%} limit. Nothing needs closing for the account's "
                "sake.",
            ),
            missing=where.missing,
            details={"stress": stress, "limit": limit},
        )

    target_usage = limit * thresholds.target_usage_fraction
    # Exiting to sit exactly on the limit means the next tick puts you back
    # over; the exit has to overshoot to be worth the spread it costs.
    fraction = max(0.0, min(1.0, (usage - target_usage) / usage))

    if stress >= thresholds.forced_closure_margin:
        urgency = "now"
        kind = "full" if fraction >= 0.85 else "margin_relief"
        reasons.append(
            f"Margin usage is {usage:.0%}, at {stress:.0%} of your {limit:.0%} "
            "limit. Trading 212 closes on its own ratio without warning and "
            "picks the position itself — usually the largest, rarely the one "
            "you would choose."
        )
    elif usage > limit:
        urgency = "soon"
        kind = "margin_relief"
        reasons.append(
            f"Margin usage {usage:.0%} is over your own {limit:.0%} limit, "
            "though still short of the broker's."
        )
    else:
        urgency = "when convenient"
        kind = "partial"
        reasons.append(
            f"Margin usage is {usage:.0%}, at {stress:.0%} of your limit. "
            "Reducing now is cheap; reducing later may not be your decision."
        )

    if where.regime.high_volatility:
        reasons.append(
            f"Volatility regime is {where.regime.volatility!r}, so the margin "
            "requirement is more likely to move against you before you next "
            "look."
        )
        if urgency == "when convenient":
            urgency = "soon"

    exit_quantity = None
    remaining = None
    if _finite(quantity) and float(quantity) > 0:
        exit_quantity = float(quantity) * fraction
        remaining = float(quantity) - exit_quantity

        instrument = None
        if rules is not None:
            instrument = rules.get(getattr(trade_intent, "instrument", ""))

        if instrument is not None and _finite(instrument.min_quantity):
            minimum = float(instrument.min_quantity)
            if 0 < exit_quantity < minimum:
                warnings.append(
                    f"The partial exit of {exit_quantity:,.4f} is below the "
                    f"{minimum:,.4f} minimum for this instrument. An exit you "
                    "cannot place is not an exit — close the whole position or "
                    "reduce a different one."
                )
                kind, fraction = "full", 1.0
                exit_quantity, remaining = float(quantity), 0.0
            elif remaining is not None and 0 < remaining < minimum:
                warnings.append(
                    f"The remainder of {remaining:,.4f} would be below the "
                    f"{minimum:,.4f} minimum, leaving a position you could not "
                    "later reduce. Closing in full instead."
                )
                kind, fraction = "full", 1.0
                exit_quantity, remaining = float(quantity), 0.0

        if instrument is not None and instrument.quantity_precision is not None:
            from cfdinvest.livehelper.t212_rules import round_to_precision

            rounded = round_to_precision(
                exit_quantity, instrument.quantity_precision
            )
            if rounded is not None and rounded > 0:
                exit_quantity = rounded
                remaining = float(quantity) - rounded

    if fraction < thresholds.minimum_partial and kind != "full":
        return ExitPlan(
            urgency="not needed",
            usage_after=usage,
            reasons=(
                f"Only {fraction:.0%} would need to go, which the spread and "
                "the conversion fee would cost more than the headroom is "
                "worth.",
            ),
            missing=where.missing,
            details={"stress": stress, "limit": limit},
        )

    required = where.margin.required_gbp
    freed = float(required) * fraction if _finite(required) else None

    reasons.append(
        "Which position to reduce is a judgement about the trades themselves. "
        "This knows only the arithmetic of margin, so it says how much has to "
        "go and leaves the choice of which to you."
    )

    return ExitPlan(
        kind=kind,
        fraction=fraction,
        quantity=exit_quantity,
        remaining_quantity=remaining,
        urgency=urgency,
        frees_margin_gbp=freed,
        usage_after=usage * (1 - fraction),
        reasons=tuple(reasons),
        warnings=tuple(warnings),
        missing=where.missing,
        details={
            "stress": stress,
            "limit": limit,
            "target_usage": target_usage,
            "usage_before": usage,
        },
    )
