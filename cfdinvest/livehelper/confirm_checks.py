"""Checking a fill you already got, against the trade you meant to make.

The rest of this package answers "may I place this?". This answers "what did
I actually place, and is it still the trade I checked?" — which is a different
question, asked after the money is on, and the only one of the two that can be
answered with facts rather than intentions.

It cannot undo anything. Every finding here is something to *know*: a position
larger than the one that passed the limits, an entry so far from the plan that
the stop no longer means what it meant, a fill that used more of the risk
budget getting in than the plan allowed for.

The measure that matters
------------------------
**Slippage against the stop distance, not against the price.** Two points off
a 4000-level index is nothing; two points against a ten-point stop is a fifth
of the risk budget spent on entry, and a reward-to-risk ratio that no longer
holds. Absolute slippage is a number that cannot be acted on.

The same logic runs through the rest: a quantity is measured against the one
that was sized, exposure against the per-instrument cap, margin against the
balance. Everything is a ratio to the thing it endangers.

Pure and offline. Nothing here reads a file or places an order.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Iterable

from cfdinvest.livehelper.config import LiveHelperConfig
from cfdinvest.livehelper.safety import Finding

logger = logging.getLogger(__name__)

__all__ = [
    "ConfirmationReview",
    "validate_confirmed_quantity",
    "validate_confirmed_price",
    "validate_confirmed_exposure",
    "validate_confirmed_margin",
    "validate_confirmed_slippage",
    "validate_confirmation_deviation",
    "review_confirmation",
]

#: How far a fill may drift before it is worth saying so, as a fraction of the
#: quantity that was recommended. Rounding to what the ticket accepts is
#: normal; a tenth of the size is a different position.
QUANTITY_TOLERANCE = 0.10

#: How much of the stop distance may be spent on entry before the trade is a
#: worse one than the plan. A fifth is a lot; a half is a different trade.
SLIPPAGE_WARNING = 0.20
SLIPPAGE_CRITICAL = 0.50


def _finite(value: Any) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


@dataclass(frozen=True, slots=True)
class ConfirmationReview:
    """What the fill turned out to be, and what that changed."""

    findings: tuple[Finding, ...] = ()
    unknowns: tuple[str, ...] = ()
    slippage: float | None = None
    slippage_share_of_stop: float | None = None
    quantity_deviation: float | None = None
    exposure_gbp: float | None = None
    margin_required_gbp: float | None = None
    margin_usage_pct: float | None = None
    reward_risk_after_fill: float | None = None

    @property
    def status(self) -> str:
        """``"AS PLANNED"``, ``"CHANGED"`` or ``"WORSE"``.

        Deliberately not SAFE/BORDERLINE/UNSAFE. Those are a verdict on a
        trade you may still decline; this is a description of one you have
        already taken, and borrowing the vocabulary would suggest the two are
        the same decision.
        """
        if any(finding.blocking for finding in self.findings):
            return "WORSE"
        if self.findings or self.unknowns:
            return "CHANGED"
        return "AS PLANNED"

    @property
    def reasons(self) -> list[str]:
        order = {"critical": 0, "warning": 1, "info": 2}
        return [
            finding.message
            for finding in sorted(
                self.findings, key=lambda f: order.get(f.severity, 9)
            )
        ]

    @property
    def recommendations(self) -> list[str]:
        seen: list[str] = []
        order = {"critical": 0, "warning": 1, "info": 2}
        for finding in sorted(self.findings, key=lambda f: order.get(f.severity, 9)):
            if finding.recommendation and finding.recommendation not in seen:
                seen.append(finding.recommendation)
        return seen


def validate_confirmed_quantity(
    actual: float | None,
    recommended: float | None,
    *,
    tolerance: float = QUANTITY_TOLERANCE,
) -> list[Finding]:
    """Is this the size that passed the checks?

    A partial fill is reported, not scolded — it is the broker's doing, and a
    smaller position than planned is inside every limit the larger one was.
    A *larger* one is not, and is the case worth being loud about.
    """
    if not _finite(actual):
        return [
            Finding(
                check="confirmed_quantity",
                severity="critical",
                message="No confirmed quantity to check.",
                recommendation="Record the size you actually took.",
            )
        ]
    if not _finite(recommended) or float(recommended) == 0:
        return [
            Finding(
                check="confirmed_quantity",
                severity="info",
                message="Nothing was recommended for this instrument, so the "
                "size is not measured against anything.",
                recommendation="A trade of your own is fine; it is simply "
                "outside what the system checked.",
                value=abs(float(actual)),
            )
        ]

    took, planned = abs(float(actual)), abs(float(recommended))
    deviation = took / planned

    if abs(deviation - 1.0) <= tolerance:
        return []

    if took > planned:
        return [
            Finding(
                check="confirmed_quantity",
                severity="critical",
                message=(
                    f"You took {took:,.4f}, which is {deviation:.0%} of the "
                    f"{planned:,.4f} that was checked."
                ),
                recommendation=(
                    "The limits were cleared for the smaller size. Every risk "
                    "figure on the dashboard now understates this position "
                    "until the difference is closed."
                ),
                value=took,
                limit=planned,
            )
        ]

    return [
        Finding(
            check="confirmed_quantity",
            severity="warning",
            message=(
                f"Partial fill: {took:,.4f} of the {planned:,.4f} recommended "
                f"({deviation:.0%})."
            ),
            recommendation=(
                "Inside every limit the full size was, so nothing is unsafe — "
                "but the position is smaller than the plan and the profit "
                "target scales with it."
            ),
            value=took,
            limit=planned,
        )
    ]


def validate_confirmed_price(
    actual: float | None, recommended: float | None
) -> list[Finding]:
    """Did the fill land where the plan assumed?

    Only reports whether a price is *missing* or absurd. Whether the gap
    matters is :func:`validate_confirmed_slippage`, because the answer depends
    on the stop and not on the price.
    """
    if not _finite(actual) or float(actual) <= 0:
        return [
            Finding(
                check="confirmed_price",
                severity="critical",
                message="No confirmed fill price to check.",
                recommendation="Record the price you actually got.",
            )
        ]
    if not _finite(recommended) or float(recommended) <= 0:
        return [
            Finding(
                check="confirmed_price",
                severity="info",
                message="No recommended price was recorded, so the fill is not "
                "measured against anything.",
            )
        ]

    drift = abs(float(actual) - float(recommended)) / float(recommended)
    if drift > 0.25:
        return [
            Finding(
                check="confirmed_price",
                severity="warning",
                message=(
                    f"The fill at {float(actual):,.4f} is {drift:.0%} away from "
                    f"the recommended {float(recommended):,.4f}."
                ),
                recommendation=(
                    "A gap this size is usually a typo or the wrong "
                    "instrument rather than a bad fill. Check before trusting "
                    "the risk figures built on it."
                ),
                value=float(actual),
                limit=float(recommended),
            )
        ]
    return []


def validate_confirmed_slippage(
    actual: float | None,
    recommended: float | None,
    stop_loss: float | None,
    *,
    direction: str | None = None,
) -> tuple[list[Finding], float | None, float | None]:
    """What getting in cost, as a share of what you were risking.

    Returns the findings, the signed slippage, and its share of the stop
    distance. **Positive slippage is worse than planned**, for a long and a
    short alike, so one convention reads the same way for both.
    """
    if not (_finite(actual) and _finite(recommended)):
        return [], None, None

    difference = float(actual) - float(recommended)
    short = str(direction or "").lower() == "short"
    slippage = -difference if short else difference

    if not _finite(stop_loss) or float(stop_loss) <= 0:
        return (
            [
                Finding(
                    check="slippage",
                    severity="info",
                    message=(
                        f"The fill was {abs(slippage):,.4f} "
                        f"{'worse' if slippage > 0 else 'better'} than planned."
                    ),
                    recommendation=(
                        "Without a stop there is nothing to measure that "
                        "against: slippage only means something as a share of "
                        "what you were risking."
                    ),
                    value=slippage,
                )
            ],
            slippage,
            None,
        )

    distance = abs(float(recommended) - float(stop_loss))
    if distance <= 0:
        return [], slippage, None

    share = slippage / distance
    if share <= 0:
        return (
            [
                Finding(
                    check="slippage",
                    severity="info",
                    message=(
                        f"The fill was {abs(slippage):,.4f} better than planned "
                        f"({abs(share):.0%} of the stop distance in your favour)."
                    ),
                    value=slippage,
                )
            ]
            if abs(share) > 0.05
            else [],
            slippage,
            share,
        )

    if share >= SLIPPAGE_CRITICAL:
        severity, note = "critical", (
            "Over half the risk budget was spent getting in. The "
            "reward-to-risk you checked no longer holds — consider whether "
            "this is still the trade you wanted."
        )
    elif share >= SLIPPAGE_WARNING:
        severity, note = "warning", (
            "A meaningful part of the risk budget went on entry. The stop is "
            "now closer than planned in risk terms."
        )
    else:
        return [], slippage, share

    return (
        [
            Finding(
                check="slippage",
                severity=severity,
                message=(
                    f"The fill was {slippage:,.4f} worse than planned — "
                    f"{share:.0%} of the {distance:,.4f} stop distance."
                ),
                recommendation=note,
                value=share,
                limit=SLIPPAGE_WARNING,
            )
        ],
        slippage,
        share,
    )


def validate_confirmed_exposure(
    price: float | None,
    quantity: float | None,
    config: LiveHelperConfig | None = None,
    *,
    fx_rate: float | None = None,
) -> tuple[list[Finding], float | None]:
    """What the position is actually worth, against the per-instrument cap."""
    settings = config or LiveHelperConfig()
    if not (_finite(price) and _finite(quantity)):
        return [], None

    exposure = abs(float(price) * float(quantity))
    if _finite(fx_rate):
        exposure *= float(fx_rate)

    if exposure <= settings.max_exposure_per_instrument_gbp:
        return [], exposure

    return (
        [
            Finding(
                check="confirmed_exposure",
                severity="critical",
                message=(
                    f"The filled position carries £{exposure:,.2f}, over the "
                    f"£{settings.max_exposure_per_instrument_gbp:,.2f} "
                    "per-instrument limit."
                ),
                recommendation=(
                    "This is what you are holding, not what you may open. "
                    "Reducing it is a decision to make now rather than a "
                    "check to fail later."
                ),
                value=exposure,
                limit=settings.max_exposure_per_instrument_gbp,
            )
        ],
        exposure,
    )


def validate_confirmed_margin(
    exposure: float | None,
    instrument_leverage: float | None,
    config: LiveHelperConfig | None = None,
) -> tuple[list[Finding], float | None, float | None]:
    """What the fill committed, and whether the account can carry it.

    Absent without the instrument's leverage, which Trading 212 does not
    publish — an assumed leverage produces a margin figure that is
    confidently wrong in whichever direction the assumption leaned, and
    margin is the number that force-closes without warning.
    """
    settings = config or LiveHelperConfig()
    if not (_finite(exposure) and _finite(instrument_leverage)) or float(
        instrument_leverage
    ) <= 0:
        return (
            [
                Finding(
                    check="confirmed_margin",
                    severity="warning",
                    message="Margin could not be computed for this fill.",
                    recommendation=(
                        "Record the instrument's leverage in your rules table. "
                        "Until then this position's margin is unknown, which "
                        "is not the same as small."
                    ),
                )
            ],
            None,
            None,
        )

    required = float(exposure) / float(instrument_leverage)
    usage = (settings.used_margin_gbp + required) / settings.account_balance_gbp

    if usage <= settings.max_margin_usage_pct_after_trade:
        return [], required, usage

    return (
        [
            Finding(
                check="confirmed_margin",
                severity="critical",
                message=(
                    f"Margin usage is {usage:.0%} with this fill on, over your "
                    f"{settings.max_margin_usage_pct_after_trade:.0%} limit."
                ),
                recommendation=(
                    "Trading 212 force-closes on a margin ratio without "
                    "warning, and it picks the position, not you."
                ),
                value=usage,
                limit=settings.max_margin_usage_pct_after_trade,
            )
        ],
        required,
        usage,
    )


def validate_confirmation_deviation(
    confirmation, recommendation: dict | None = None,
    config: LiveHelperConfig | None = None,
) -> ConfirmationReview:
    """Everything above, for one recorded confirmation.

    ``confirmation`` is a :class:`~cfdinvest.persist.confirm.Confirmation`;
    ``recommendation`` the dict from
    :func:`cfdinvest.confirm.recommendation.recommendation_for`, or ``None``
    to use whatever the confirmation recorded at the time.
    """
    plan = dict(recommendation or {})
    recommended_price = plan.get("entry_price")
    if recommended_price is None:
        recommended_price = getattr(confirmation, "recommended_price", None)
    recommended_quantity = plan.get("scaled_quantity") or plan.get("quantity")
    if recommended_quantity is None:
        recommended_quantity = getattr(confirmation, "recommended_quantity", None)

    return review_confirmation(
        price=getattr(confirmation, "price", None),
        quantity=getattr(confirmation, "quantity", None),
        recommended_price=recommended_price,
        recommended_quantity=recommended_quantity,
        stop_loss=plan.get("stop_level"),
        take_profit=plan.get("take_profit_level"),
        direction=getattr(confirmation, "direction", None),
        instrument_leverage=plan.get("leverage"),
        fx_rate=plan.get("fx_rate"),
        config=config,
    )


def _reward_risk(
    entry: float | None, stop: float | None, target: float | None
) -> float | None:
    """Distance to the target over distance to the stop, from one entry."""
    if not (_finite(entry) and _finite(stop) and _finite(target)):
        return None
    risk = abs(float(entry) - float(stop))
    if risk <= 0:
        return None
    return abs(float(target) - float(entry)) / risk


def review_confirmation(
    *,
    price: float | None,
    quantity: float | None,
    recommended_price: float | None = None,
    recommended_quantity: float | None = None,
    stop_loss: float | None = None,
    take_profit: float | None = None,
    direction: str | None = None,
    instrument_leverage: float | None = None,
    fx_rate: float | None = None,
    config: LiveHelperConfig | None = None,
) -> ConfirmationReview:
    """The whole review, from loose values. Pure."""
    settings = config or LiveHelperConfig()
    findings: list[Finding] = []
    unknowns: list[str] = []

    findings += validate_confirmed_quantity(quantity, recommended_quantity)
    findings += validate_confirmed_price(price, recommended_price)

    slip_findings, slippage, share = validate_confirmed_slippage(
        price, recommended_price, stop_loss, direction=direction
    )
    findings += slip_findings

    exposure_findings, exposure = validate_confirmed_exposure(
        price, quantity, settings, fx_rate=fx_rate
    )
    findings += exposure_findings

    margin_findings, margin, usage = validate_confirmed_margin(
        exposure, instrument_leverage, settings
    )
    findings += margin_findings

    if not _finite(recommended_price):
        unknowns.append("the recommended entry")
    if not _finite(stop_loss):
        unknowns.append("the stop, so slippage cannot be put in context")
    if not _finite(instrument_leverage):
        unknowns.append("the instrument's leverage, so margin is unknown")

    reward_risk = _reward_risk(price, stop_loss, take_profit)
    if reward_risk is not None and _finite(planned_reward_risk := _reward_risk(
        recommended_price, stop_loss, take_profit
    )):
        # The point of measuring slippage against the stop: a worse entry moves
        # the target further away and the stop nearer, and the ratio you
        # checked is not the ratio you now hold.
        if planned_reward_risk > 0 and reward_risk < planned_reward_risk * 0.75:
            findings.append(
                Finding(
                    check="reward_risk_after_fill",
                    severity="warning",
                    message=(
                        f"After this fill the reward-to-risk is "
                        f"{reward_risk:,.2f}, down from the "
                        f"{planned_reward_risk:,.2f} you checked."
                    ),
                    recommendation=(
                        "The trade you are in is not the trade you approved. "
                        "Moving the target rather than the stop is usually the "
                        "cheaper way to restore it."
                    ),
                    value=reward_risk,
                    limit=planned_reward_risk,
                )
            )

    return ConfirmationReview(
        findings=tuple(findings),
        unknowns=tuple(dict.fromkeys(unknowns)),
        slippage=slippage,
        slippage_share_of_stop=share,
        quantity_deviation=(
            abs(float(quantity)) / abs(float(recommended_quantity))
            if _finite(quantity) and _finite(recommended_quantity)
            and float(recommended_quantity) != 0
            else None
        ),
        exposure_gbp=exposure,
        margin_required_gbp=margin,
        margin_usage_pct=usage,
        reward_risk_after_fill=reward_risk,
    )
