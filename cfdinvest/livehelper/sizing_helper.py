"""The largest quantity that stays inside every limit.

Sizing is a minimum, not a formula. Each limit implies a ceiling on quantity,
and the safe size is the smallest of them — so the useful output is not only
the number but **which limit produced it**, because that is the one to change
if the size is too small to bother with.

**Rounding is always down.** A size rounded up to the nearest tick can breach
the very limit it was computed from, which would make this module the cause
of the problem it exists to prevent.

Pure: it reads the intent, the config and the market state, and returns a
recommendation. It changes nothing and places nothing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from cfdinvest.livehelper.config import LiveHelperConfig
from cfdinvest.livehelper.intent import TradeIntent
from cfdinvest.livehelper.safety import MarketState, _finite

__all__ = [
    "DEFAULT_PRECISION",
    "SizingRecommendation",
    "compute_safe_sizing",
    "round_down",
]

#: Decimal places a Trading 212 CFD quantity is rounded to.
#:
#: The platform sets this per instrument and does not publish a list, so this
#: is a conservative default rather than a fact. Pass ``precision`` when the
#: instrument's real precision is known; rounding down means a too-fine
#: default is safe, only slightly wasteful.
DEFAULT_PRECISION: int = 4


def round_down(value: float, precision: int = DEFAULT_PRECISION) -> float:
    """Round towards zero at ``precision`` decimals.

    Down, never nearest: a quantity rounded *up* can breach the limit it was
    derived from, and a sizing helper that does that is worse than none.
    """
    if not _finite(value) or value <= 0:
        return 0.0
    factor = 10**precision
    return math.floor(value * factor) / factor


@dataclass(frozen=True, slots=True)
class SizingRecommendation:
    """The safe size, and what decided it.

    Attributes
    ----------
    binding_constraint:
        Which limit produced the number. The one to relax if the size is too
        small to be worth trading.
    ceilings:
        Every limit's implied **hard** maximum, so the ordering is visible
        rather than having to be inferred from one figure. The recommended
        quantity sits below the smallest of these, at the borderline
        threshold, so that taking the recommendation reads as SAFE rather
        than as sitting exactly on a limit.
    scale:
        Recommended over intended. Below 1.0 the intended trade is too big.
    """

    recommended_quantity: float = 0.0
    recommended_exposure_gbp: float | None = None
    recommended_leverage: float | None = None
    margin_required_gbp: float | None = None
    margin_usage_pct_after_trade: float | None = None
    recommended_risk_gbp: float | None = None
    binding_constraint: str | None = None
    ceilings: dict[str, float] = field(default_factory=dict)
    scale: float | None = None
    notes: tuple[str, ...] = ()

    @property
    def reduces(self) -> bool:
        """Whether the intended size needs cutting."""
        return self.scale is not None and self.scale < 0.999

    @property
    def tradeable(self) -> bool:
        """Whether any size at all clears every limit."""
        return self.recommended_quantity > 0


#: What each constraint is called in the recommendation, in the order they
#: are reported when several bind at once.
_LABELS: dict[str, str] = {
    "risk": "risk per trade",
    "exposure": "exposure per instrument",
    "leverage": "effective leverage",
    "margin": "margin usage",
    "fx": "currency exposure",
}


def compute_safe_sizing(
    trade_intent: TradeIntent,
    config: LiveHelperConfig | None = None,
    market_state: MarketState | None = None,
    *,
    precision: int = DEFAULT_PRECISION,
) -> SizingRecommendation:
    """The largest quantity that satisfies every limit at once.

    Returns a recommendation of zero — with a note saying why — when no size
    clears the limits, which happens when the stop is so wide that even a
    minimum position risks more than allowed. That is a real answer, and
    quietly returning a tiny number instead would hide it.
    """
    limits = config or LiveHelperConfig()
    market = market_state or MarketState()
    notes: list[str] = []

    price = market.price_for(trade_intent)
    rate = 1.0 if not trade_intent.is_foreign else market.fx_rate
    leverage = trade_intent.leverage or market.leverage

    if not _finite(price) or price <= 0:
        return SizingRecommendation(
            notes=("No price, so no size can be computed.",)
        )
    if not _finite(rate) or rate <= 0:
        return SizingRecommendation(
            notes=(
                f"{trade_intent.instrument} is quoted in "
                f"{trade_intent.instrument_currency} and no exchange rate was "
                "given, so no size can be stated in "
                f"{trade_intent.account_currency}.",
            )
        )

    value_per_unit = price * rate          # account currency per unit held
    ceilings: dict[str, float] = {}

    distance = trade_intent.stop_distance(price)
    if _finite(distance) and distance > 0:
        ceilings["risk"] = limits.max_risk_per_trade_gbp / (distance * rate)
    else:
        notes.append(
            "No usable stop, so the risk limit cannot bound the size. The "
            "number below respects exposure and margin only."
        )

    ceilings["exposure"] = limits.max_exposure_per_instrument_gbp / value_per_unit
    ceilings["leverage"] = (
        limits.max_leverage_per_trade * limits.account_balance_gbp / value_per_unit
    )
    if trade_intent.is_foreign:
        ceilings["fx"] = (
            limits.max_fx_exposure_pct * limits.account_balance_gbp / value_per_unit
        )

    if _finite(leverage) and leverage > 0:
        ceilings["margin"] = limits.free_margin_gbp * leverage / value_per_unit
    else:
        notes.append(
            "No instrument leverage given, so the margin limit cannot bound "
            "the size. Read it off the instrument's page for a complete answer."
        )

    binding = min(ceilings, key=ceilings.get) if ceilings else None
    ceiling = ceilings[binding] if binding else 0.0

    # Sized to the *borderline* threshold, not the hard limit. A quantity
    # sitting exactly on a limit is flagged BORDERLINE by the safety check,
    # which would mean this module's own recommendation never came back
    # clean -- advice that fails the adviser's test is not advice.
    target = ceiling * limits.borderline_fraction
    quantity = round_down(min(target, trade_intent.quantity), precision)

    if quantity <= 0:
        notes.append(
            f"No size clears every limit — the {_LABELS.get(binding, binding)} "
            "limit is reached before the smallest tradeable quantity. Widen "
            "that limit, or pick an instrument with a tighter stop."
        )
        return SizingRecommendation(
            binding_constraint=binding,
            ceilings={k: round_down(v, precision) for k, v in ceilings.items()},
            scale=0.0,
            notes=tuple(notes),
        )

    exposure = quantity * value_per_unit
    margin = exposure / leverage if _finite(leverage) and leverage > 0 else None
    usage = (
        (limits.used_margin_gbp + margin) / limits.account_balance_gbp
        if _finite(margin)
        else None
    )
    risk = (
        quantity * distance * rate
        if _finite(distance) and distance > 0
        else None
    )
    scale = quantity / trade_intent.quantity if trade_intent.quantity else None

    if scale is not None and scale < 0.999:
        notes.append(
            f"Cut from {trade_intent.quantity:,.4f} to {quantity:,.4f} — "
            f"{_LABELS.get(binding, binding)} is the binding limit."
        )
    elif scale is not None and scale >= 0.999:
        notes.append(
            f"The intended size already clears every limit; the ceiling is "
            f"{round_down(ceiling, precision):,.4f} on "
            f"{_LABELS.get(binding, binding)}."
        )

    return SizingRecommendation(
        recommended_quantity=quantity,
        recommended_exposure_gbp=exposure,
        recommended_leverage=exposure / limits.account_balance_gbp,
        margin_required_gbp=margin,
        margin_usage_pct_after_trade=usage,
        recommended_risk_gbp=risk,
        binding_constraint=binding,
        ceilings={k: round_down(v, precision) for k, v in ceilings.items()},
        scale=scale,
        notes=tuple(notes),
    )
