"""Margin, as a direction rather than a level.

The safety engine already says whether margin usage is over the limit, and
:mod:`cfdinvest.risk` already says what the book is carrying. Both answer
*now*. Neither can tell you that usage has climbed from 20% to 45% over a
fortnight while every individual check passed — and that is the shape a margin
call actually has.

Trading 212 force-closes on a margin ratio **without warning**, and it picks
the position, not you. By the time a level check fails there is no time to do
anything gentle. So this looks at the slope.

Nothing here re-derives a margin figure. The level comes from the assessment
the safety engine produced, the history from the risk snapshots the monitoring
layer already writes. What is new is the comparison between them.

Four things it watches
----------------------
**Rising usage** — the slope, over at least three readings.
**Leverage creep** — position sizes drifting up while nothing else changed.
**Currency instability** — an exchange rate moving your margin in a currency
you did not think you were exposed to.
**Volatility spikes** — the same position needing more room in a faster market.

Pure and deterministic.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Sequence

from cfdinvest.livehelper.config import LiveHelperConfig
from cfdinvest.livehelper.context import TradingContext

logger = logging.getLogger(__name__)

__all__ = ["MarginWarning", "MarginWarningConfig", "compute_margin_warning"]


def _finite(value: Any) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


@dataclass(frozen=True, slots=True)
class MarginWarningConfig:
    """When a slope is worth mentioning.

    The *levels* are not here: they live in
    :class:`~cfdinvest.livehelper.config.LiveHelperConfig` and
    :class:`cfdinvest.risk.RiskLimits`, and repeating them would give the
    same question two answers. What is new is how much movement counts.

    Attributes
    ----------
    minimum_readings:
        Below this, no trend is reported. Two points make a line through
        anything, and a warning on every second reading is one nobody reads.
    rising_usage:
        Increase in margin usage, in percentage points as a fraction, across
        the recorded history.
    approaching_limit:
        Share of your usage limit at which the level alone is worth saying,
        even flat. Being told at the limit is being told too late.
    leverage_creep:
        Proportional increase in effective leverage over the history.
    """

    minimum_readings: int = 3
    rising_usage: float = 0.10
    approaching_limit: float = 0.80
    leverage_creep: float = 0.25


@dataclass(frozen=True, slots=True)
class MarginWarning:
    """What margin is doing, and how much room is left.

    ``severity`` is ``"ok"``, ``"watch"`` or ``"act"``. Three words rather
    than a number, because the only useful question is what to do, and a
    score of 62 does not answer it.
    """

    severity: str = "ok"
    flags: tuple[str, ...] = ()
    usage_pct: float | None = None
    headroom_pct: float | None = None
    trend: float | None = None
    readings: int = 0
    recommendations: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def act(self) -> bool:
        return self.severity == "act"

    @property
    def quiet(self) -> bool:
        return self.severity == "ok" and not self.flags


def _slope(history: Sequence[float], minimum: int = 3) -> float | None:
    """Change across the history. ``None`` when there is not enough of it.

    ``minimum`` matches :attr:`~cfdinvest.livehelper.context.MarginState.trend`
    deliberately. Early these disagreed -- three readings there, two here --
    so a two-reading history reported a trend in one place and none in the
    other, which is the sort of thing that gets debugged twice.
    """
    values = [float(v) for v in history if _finite(v)]
    if len(values) < minimum:
        return None
    return values[-1] - values[0]


def compute_margin_warning(
    assessment=None,
    context: TradingContext | None = None,
    config: LiveHelperConfig | None = None,
    settings: MarginWarningConfig | None = None,
) -> MarginWarning:
    """Where margin is heading, from the level and the recorded history."""
    limits = config or LiveHelperConfig()
    thresholds = settings or MarginWarningConfig()
    where = context or TradingContext()
    margin = where.margin

    usage = margin.usage_pct
    if not _finite(usage):
        usage = getattr(assessment, "margin_usage_pct_after_trade", None)

    flags: list[str] = []
    actions: list[str] = []
    severity = "ok"

    if not _finite(usage) and not margin.history:
        return MarginWarning(
            severity="watch",
            flags=("Margin could not be measured.",),
            recommendations=(
                "Record the instrument's leverage so margin can be computed. "
                "Unknown margin is not low margin, and Trading 212 closes on "
                "the ratio without asking.",
            ),
            missing=where.missing,
        )

    headroom = None
    if _finite(usage):
        headroom = limits.max_margin_usage_pct_after_trade - float(usage)
        stress = float(usage) / limits.max_margin_usage_pct_after_trade

        if float(usage) > limits.max_margin_usage_pct_after_trade:
            severity = "act"
            flags.append(
                f"Margin usage {float(usage):.0%} is over your "
                f"{limits.max_margin_usage_pct_after_trade:.0%} limit."
            )
            actions.append(
                "Reduce exposure now. A forced close is chosen by the broker, "
                "and it will not pick the position you would."
            )
        elif stress >= thresholds.approaching_limit:
            severity = "watch"
            flags.append(
                f"Margin usage {float(usage):.0%} is at {stress:.0%} of your "
                "limit."
            )
            actions.append(
                "Still inside the limit. Being told at the limit is being told "
                "too late to do anything gentle about it."
            )

    trend = _slope(margin.history, thresholds.minimum_readings)
    if len(margin.history) < thresholds.minimum_readings:
        if margin.history:
            flags.append(
                f"Only {len(margin.history)} margin reading(s) recorded, so no "
                "trend could be measured."
            )
    elif trend is not None and trend >= thresholds.rising_usage:
        severity = "act" if severity == "act" else "watch"
        flags.append(
            f"Margin usage has risen {trend:+.0%} across the last "
            f"{len(margin.history)} readings "
            f"({margin.history[0]:.0%} to {margin.history[-1]:.0%})."
        )
        actions.append(
            "Every individual check passed while this happened. That is the "
            "shape a margin call has — a slope, not a step."
        )

    leverage_trend = _slope(margin.leverage_history, thresholds.minimum_readings)
    if (
        leverage_trend is not None
        and len(margin.leverage_history) >= thresholds.minimum_readings
        and margin.leverage_history[0] > 0
        and leverage_trend / margin.leverage_history[0] >= thresholds.leverage_creep
    ):
        severity = "act" if severity == "act" else "watch"
        growth = leverage_trend / margin.leverage_history[0]
        flags.append(
            f"Effective leverage has crept up {growth:+.0%} "
            f"({margin.leverage_history[0]:,.1f}x to "
            f"{margin.leverage_history[-1]:,.1f}x)."
        )
        actions.append(
            "Leverage creep is usually a run of individually reasonable "
            "sizing decisions. Each one passed; the sequence did not."
        )

    fx_share = getattr(assessment, "fx_exposure_pct", None)
    if _finite(fx_share) and float(fx_share) > limits.max_fx_exposure_pct * 0.8:
        flags.append(
            f"Currency exposure is {float(fx_share):.0%} of the account."
        )
        actions.append(
            "Margin is posted in sterling and the position is not. An "
            "exchange-rate move changes your margin without the instrument "
            "moving at all."
        )
        severity = "watch" if severity == "ok" else severity

    if where.regime.high_volatility and _finite(usage):
        flags.append(
            f"Volatility regime is {where.regime.volatility!r} with "
            f"{float(usage):.0%} of the margin limit used."
        )
        actions.append(
            "The same position needs more room in a fast market, and the "
            "margin does not adjust for that — you do."
        )
        severity = "watch" if severity == "ok" else severity

    return MarginWarning(
        severity=severity,
        flags=tuple(flags),
        usage_pct=float(usage) if _finite(usage) else None,
        headroom_pct=headroom,
        trend=trend,
        readings=len(margin.history),
        recommendations=tuple(dict.fromkeys(actions)),
        missing=where.missing,
        details={
            "limit": limits.max_margin_usage_pct_after_trade,
            "history": list(margin.history),
            "leverage_history": list(margin.leverage_history),
            "leverage_trend": leverage_trend,
            "fx_exposure_pct": fx_share if _finite(fx_share) else None,
        },
    )
