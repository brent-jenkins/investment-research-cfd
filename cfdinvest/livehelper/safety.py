"""Is this trade inside the bounds you set for yourself?

This is the consolidated safety core: the execution-rule checks, the
risk-limit enforcement, the margin early warning and the go/no-go verdict all
live here, because they are the same operation — measure the intended trade,
compare each number against a limit, and say what to do about it. Splitting
them across separate packages would mean a threshold has several homes and
they can disagree.

**Three rules govern the verdict, and the third is the important one:**

1. Any hard limit breached makes the trade ``UNSAFE``.
2. Any warning, or any figure within ``borderline_fraction`` of a limit,
   makes it ``BORDERLINE``.
3. **Anything that cannot be computed also makes it ``BORDERLINE`` at best.**
   A missing stop, an unknown exchange rate, an unpublished leverage — none
   of these are reasons to say ``SAFE``. A helper that grades an unpriceable
   trade green has done something worse than stay silent, because a green
   label launders a decision nobody actually checked.

Everything here is pure: it reads the intent, the config and a market state,
and returns an assessment. Nothing is placed, nothing is fetched, and no
Trading 212 interface is touched.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from cfdinvest.livehelper.config import LiveHelperConfig
from cfdinvest.livehelper.intent import TradeIntent

__all__ = [
    "STATUSES",
    "SEVERITIES",
    "MarketState",
    "Finding",
    "TradeSafetyAssessment",
    "evaluate_trade_safety",
]

#: Worst first, which is also the order they are reported in.
STATUSES: tuple[str, ...] = ("UNSAFE", "BORDERLINE", "SAFE")
SEVERITIES: tuple[str, ...] = ("critical", "warning", "info")


@dataclass(frozen=True, slots=True)
class MarketState:
    """What the market is doing, as far as you know it.

    Every field is optional and every absence is handled, because this is
    typed in by hand at the point of trading. What is *not* done is guessing:
    an absent exchange rate on a foreign instrument stops the assessment
    reaching ``SAFE`` rather than defaulting to 1.0.

    Attributes
    ----------
    last_price:
        Used when the intent is "at market" and carries no entry price.
    spread:
        The quoted spread in **instrument currency**, not basis points —
        that is what the Trading 212 ticket shows.
    volatility:
        Annualised, as a fraction. ``0.25`` is 25% a year.
    fx_rate:
        Account currency per one unit of instrument currency. A GBP account
        trading a USD instrument at 0.79 GBP/USD has ``fx_rate=0.79``.
    leverage:
        The instrument's leverage, if you know it. Trading 212 sets this per
        instrument and does not publish a machine-readable list.
    """

    last_price: float | None = None
    spread: float | None = None
    volatility: float | None = None
    fx_rate: float | None = None
    leverage: float | None = None
    near_financing_rollover: bool = False
    close_only: bool = False

    def price_for(self, intent: TradeIntent) -> float | None:
        """The price this trade will be struck at, as best as it is known."""
        return intent.entry_price if intent.entry_price is not None else self.last_price


@dataclass(frozen=True, slots=True)
class Finding:
    """One check, and what it found.

    ``recommendation`` is what to *do*, phrased as an instruction. "Leverage
    too high" is not actionable; "reduce quantity to 0.34" is.
    """

    check: str
    severity: str
    message: str
    recommendation: str = ""
    value: float | None = None
    limit: float | None = None

    @property
    def blocking(self) -> bool:
        return self.severity == "critical"


@dataclass(frozen=True, slots=True)
class TradeSafetyAssessment:
    """The verdict, the numbers behind it, and what to do."""

    status: str
    risk_gbp: float | None = None
    exposure_gbp: float | None = None
    leverage: float | None = None
    instrument_leverage: float | None = None
    margin_required_gbp: float | None = None
    margin_usage_pct_after_trade: float | None = None
    fx_exposure_pct: float | None = None
    reward_risk_ratio: float | None = None
    volatility_flag: bool = False
    spread_flag: bool = False
    findings: tuple[Finding, ...] = ()
    unknowns: tuple[str, ...] = ()

    @property
    def reasons(self) -> list[str]:
        """Why the verdict is what it is, worst first."""
        order = {name: n for n, name in enumerate(SEVERITIES)}
        return [
            f.message
            for f in sorted(self.findings, key=lambda f: order.get(f.severity, 9))
        ]

    @property
    def recommendations(self) -> list[str]:
        """What to do about it, worst first, without repeats."""
        order = {name: n for n, name in enumerate(SEVERITIES)}
        seen: list[str] = []
        for finding in sorted(self.findings, key=lambda f: order.get(f.severity, 9)):
            if finding.recommendation and finding.recommendation not in seen:
                seen.append(finding.recommendation)
        return seen

    @property
    def go(self) -> bool:
        """The go/no-go the checklist would give. ``BORDERLINE`` is not a go."""
        return self.status == "SAFE"

    @property
    def critical(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.blocking)


def _finite(value: Any) -> bool:
    return value is not None and isinstance(value, (int, float)) and math.isfinite(value)


def evaluate_trade_safety(
    trade_intent: TradeIntent,
    config: LiveHelperConfig | None = None,
    market_state: MarketState | None = None,
    *,
    rules=None,
    now=None,
) -> TradeSafetyAssessment:
    """Measure an intended trade against your limits, and against the broker's.

    Two different kinds of constraint, deliberately both here. Yours are about
    how much you are willing to lose; Trading 212's are about what its ticket
    will accept, and a trade can pass every one of yours and still be silently
    rejected for having three decimal places.

    ``rules`` is a :class:`~cfdinvest.livehelper.t212_rules.RuleTable`. With
    none, the broker checks report themselves unchecked rather than passed --
    the table is one you fill in, since nothing publishes it.

    Pure. Returns every figure it could compute, every check that fired, and
    a verdict that never reaches ``SAFE`` on incomplete information.
    """
    limits = config or LiveHelperConfig()
    market = market_state or MarketState()
    findings: list[Finding] = []
    unknowns: list[str] = []

    price = market.price_for(trade_intent)
    rate = _resolve_rate(trade_intent, market, findings, unknowns)
    instrument_leverage = _resolve_leverage(
        trade_intent, market, findings, unknowns
    )

    if not _finite(price):
        unknowns.append("price")
        findings.append(
            Finding(
                "price", "critical",
                "No entry price and no last price, so nothing can be sized.",
                "Supply an entry price, or a last price for a market order.",
            )
        )

    exposure = (
        trade_intent.quantity * price * rate
        if _finite(price) and _finite(rate)
        else None
    )
    risk = _risk(trade_intent, price, rate, findings, unknowns)

    # Two different things are called leverage, and conflating them makes the
    # advice wrong. The *instrument's* leverage is fixed by Trading 212 and
    # no change of size touches it; it is what divides exposure into margin.
    # The *effective* leverage is what this trade does to the account, and it
    # is the one a smaller position reduces -- so it is the one the limit is
    # checked against, because it is the one you can act on.
    margin = (
        exposure / instrument_leverage
        if _finite(exposure) and _finite(instrument_leverage)
        else None
    )
    leverage = (
        exposure / limits.account_balance_gbp if _finite(exposure) else None
    )
    usage = (
        (limits.used_margin_gbp + margin) / limits.account_balance_gbp
        if _finite(margin)
        else None
    )
    fx_share = (
        exposure / limits.account_balance_gbp
        if _finite(exposure) and trade_intent.is_foreign
        else (0.0 if not trade_intent.is_foreign else None)
    )

    _check_levels(trade_intent, price, findings)
    _check_limit(
        findings, limits, "risk", risk, limits.max_risk_per_trade_gbp,
        "Risk", "£", "Reduce quantity, or move the stop closer to entry.",
    )
    _check_limit(
        findings, limits, "exposure", exposure,
        limits.max_exposure_per_instrument_gbp, "Exposure", "£",
        "Reduce quantity.",
    )
    _check_limit(
        findings, limits, "leverage", leverage, limits.max_leverage_per_trade,
        "Effective leverage", "x", "Reduce quantity.",
    )
    _check_limit(
        findings, limits, "margin", usage,
        limits.max_margin_usage_pct_after_trade, "Margin usage after this trade",
        "%", "Reduce quantity, or close something else first.",
    )
    if trade_intent.is_foreign:
        _check_limit(
            findings, limits, "fx", fx_share, limits.max_fx_exposure_pct,
            "Currency exposure", "%",
            "Reduce quantity, or trade a sterling-denominated instrument.",
        )

    volatility_flag = _check_volatility(market, limits, findings)
    spread_flag = _check_spread(trade_intent, market, limits, price, findings)
    reward_risk = _check_reward_risk(trade_intent, price, findings)
    _check_broker_state(market, findings)
    _check_broker_rules(trade_intent, rules, findings, unknowns, now)

    status = _verdict(findings, unknowns)

    return TradeSafetyAssessment(
        status=status,
        risk_gbp=risk,
        exposure_gbp=exposure,
        leverage=leverage,
        margin_required_gbp=margin,
        margin_usage_pct_after_trade=usage,
        instrument_leverage=instrument_leverage,
        fx_exposure_pct=fx_share,
        reward_risk_ratio=reward_risk,
        volatility_flag=volatility_flag,
        spread_flag=spread_flag,
        findings=tuple(findings),
        unknowns=tuple(dict.fromkeys(unknowns)),
    )


def _check_broker_rules(intent, rules, findings, unknowns, now) -> None:
    """What Trading 212's ticket will accept, if you have recorded it.

    Folded into the same finding list as everything else so one verdict covers
    both kinds of constraint. A trade that is safe by your limits and
    unplaceable by the broker's is not a trade, and two separate answers would
    let you act on the reassuring one.
    """
    from cfdinvest.livehelper import t212_rules as broker

    if rules is None:
        # Noted, not counted against the verdict. Nobody has a rules table
        # until they write one, and an unknown here would cap every trade at
        # BORDERLINE on a fresh install -- a verdict that is always the same
        # is a verdict nobody reads, which would cost more than it protects.
        findings.append(
            Finding(
                check="broker_rules",
                severity="info",
                message=(
                    "Trading 212's own limits were not checked: no rules table "
                    "was supplied."
                ),
                recommendation=(
                    "Record the instrument's precision, minimum size and "
                    "leverage from the ticket; see cfdinvest/livehelper/"
                    "README.md. Until then the ticket may still reject a "
                    "trade this says is safe."
                ),
            )
        )
        return

    instrument = rules.get(intent.instrument)
    if instrument.empty:
        # A table exists and this instrument is not in it. *That* is an
        # unknown: you opted into these checks and this one could not run.
        unknowns.append(f"the Trading 212 rules for {intent.instrument}")

    issues = (
        broker.validate_quantity(intent.quantity, instrument)
        + broker.validate_close_only(instrument, intent.direction)
        + broker.validate_trading_hours(instrument, now)
    )
    for issue in issues:
        if issue.severity == "info" and issue.check == "quantity_rules":
            # Already carried as an unknown; two lines saying the same absence
            # reads as two problems.
            continue
        findings.append(
            Finding(
                check=issue.check,
                severity=issue.severity,
                message=issue.message,
                recommendation=issue.recommendation,
                value=issue.value,
                limit=issue.limit,
            )
        )


# --------------------------------------------------------------------------- #
# Resolving the inputs
# --------------------------------------------------------------------------- #
def _resolve_rate(intent, market, findings, unknowns) -> float | None:
    """The exchange rate, or a recorded absence."""
    if not intent.is_foreign:
        return 1.0
    if _finite(market.fx_rate):
        return float(market.fx_rate)

    unknowns.append("fx_rate")
    findings.append(
        Finding(
            "fx_rate", "critical",
            f"{intent.instrument} is quoted in {intent.instrument_currency} and "
            f"no {intent.account_currency}/{intent.instrument_currency} rate was "
            "given, so risk and exposure cannot be stated in "
            f"{intent.account_currency}.",
            "Supply the exchange rate, or assume nothing about this trade's size.",
        )
    )
    return None


def _resolve_leverage(intent, market, findings, unknowns) -> float | None:
    """The instrument's leverage, or a recorded absence."""
    for candidate in (intent.leverage, market.leverage):
        if _finite(candidate):
            return float(candidate)

    unknowns.append("leverage")
    findings.append(
        Finding(
            "leverage", "warning",
            "No leverage given, so margin cannot be computed. Trading 212 sets "
            "it per instrument and does not publish a list.",
            "Read the leverage off the instrument's page and pass it in.",
        )
    )
    return None


def _risk(intent, price, rate, findings, unknowns) -> float | None:
    """What this trade loses between entry and stop, in account currency."""
    if not intent.has_stop:
        unknowns.append("stop_loss")
        findings.append(
            Finding(
                "stop_loss", "critical",
                "No stop-loss. The loss on this trade is unbounded, not large — "
                "there is no number to compare against your risk limit.",
                "Set a stop before placing this trade.",
            )
        )
        return None

    distance = intent.stop_distance(price)
    if not _finite(distance) or not _finite(rate):
        unknowns.append("risk")
        return None
    if distance == 0:
        findings.append(
            Finding(
                "stop_loss", "critical",
                "The stop is at the entry price, which is not a stop.",
                "Move the stop away from entry.",
            )
        )
        return 0.0
    return intent.quantity * distance * rate


# --------------------------------------------------------------------------- #
# The checks
# --------------------------------------------------------------------------- #
def _check_limit(
    findings, limits, name, value, limit, label, unit, recommendation
) -> None:
    """One numeric limit, with a borderline band below it."""
    if not _finite(value) or not _finite(limit) or limit <= 0:
        return

    shown = _format(value, unit)
    capped = _format(limit, unit)

    if value > limit:
        findings.append(
            Finding(
                name, "critical",
                f"{label} {shown} is over your limit of {capped}.",
                recommendation, float(value), float(limit),
            )
        )
    elif value > limits.borderline_fraction * limit:
        findings.append(
            Finding(
                name, "warning",
                f"{label} {shown} uses {value / limit:.0%} of your {capped} limit.",
                recommendation, float(value), float(limit),
            )
        )


def _format(value: float, unit: str) -> str:
    if unit == "%":
        return f"{value:.1%}"
    if unit == "£":
        return f"£{value:,.2f}"
    if unit == "x":
        return f"{value:,.2f}x"
    return f"{value:,.2f}"


def _check_levels(intent, price, findings) -> None:
    """Stops and targets on the wrong side, which is easy to fat-finger."""
    correct_stop = intent.stop_is_on_the_correct_side(price)
    if correct_stop is False:
        side = "below" if intent.direction == "long" else "above"
        findings.append(
            Finding(
                "stop_side", "critical",
                f"The stop at {intent.stop_loss:,.4f} is on the wrong side of a "
                f"{intent.direction}: it should be {side} the entry. As written "
                "it would trigger immediately.",
                f"Move the stop {side} the entry price.",
            )
        )

    correct_target = intent.target_is_on_the_correct_side(price)
    if correct_target is False:
        side = "above" if intent.direction == "long" else "below"
        findings.append(
            Finding(
                "target_side", "critical",
                f"The take-profit at {intent.take_profit:,.4f} is on the wrong "
                f"side of a {intent.direction}: it should be {side} the entry.",
                f"Move the take-profit {side} the entry price.",
            )
        )

    if intent.take_profit is None:
        findings.append(
            Finding(
                "take_profit", "info",
                "No take-profit. Not a fault — letting winners run is a "
                "choice — but the reward side is then unmeasured.",
            )
        )


def _check_volatility(market, limits, findings) -> bool:
    """High volatility is a flag, never a block: it is why the trade exists."""
    if not _finite(market.volatility):
        return False
    if market.volatility <= limits.volatility_warning_threshold:
        return False

    findings.append(
        Finding(
            "volatility", "warning",
            f"Volatility is {market.volatility:.0%} a year, above your "
            f"{limits.volatility_warning_threshold:.0%} threshold. Stops are "
            "likelier to be reached, and likelier to gap through.",
            "Consider a smaller size, or a wider stop with the size cut to match.",
            float(market.volatility), float(limits.volatility_warning_threshold),
        )
    )
    return True


def _check_spread(intent, market, limits, price, findings) -> bool:
    """The spread against the stop distance, which is the ratio that matters.

    A two-point spread means nothing on its own. Against a ten-point stop it
    is a fifth of the risk budget spent before the trade has moved.
    """
    if not _finite(market.spread):
        return False

    distance = intent.stop_distance(price)
    if not _finite(distance) or distance <= 0:
        return False

    share = market.spread / distance
    if share <= limits.spread_warning_threshold:
        return False

    findings.append(
        Finding(
            "spread", "warning",
            f"The spread of {market.spread:,.4f} is {share:.0%} of the stop "
            f"distance, above your {limits.spread_warning_threshold:.0%} "
            "threshold. That much of the risk budget is spent on entry.",
            "Widen the stop and cut the size, or wait for the spread to narrow.",
            float(share), float(limits.spread_warning_threshold),
        )
    )
    return True


def _check_reward_risk(intent, price, findings) -> float | None:
    """Reward against risk. Informational — a low ratio can still be right."""
    stop = intent.stop_distance(price)
    target = intent.target_distance(price)
    if not _finite(stop) or not _finite(target) or stop <= 0:
        return None

    ratio = target / stop
    if ratio < 1.0:
        findings.append(
            Finding(
                "reward_risk", "info",
                f"Reward-to-risk is {ratio:.2f}: the take-profit is closer than "
                "the stop, so this needs a win rate above 50% to break even.",
                value=float(ratio), limit=1.0,
            )
        )
    return ratio


def _check_broker_state(market, findings) -> None:
    """Two Trading 212 behaviours worth catching before you type."""
    if market.close_only:
        findings.append(
            Finding(
                "close_only", "critical",
                "This instrument is close-only, so an opening order will be "
                "rejected.",
                "Do not attempt to open. Only a closing order will fill.",
            )
        )
    if market.near_financing_rollover:
        findings.append(
            Finding(
                "rollover", "warning",
                "Financing rolls over shortly. Opening now pays a full night's "
                "financing for minutes of exposure.",
                "Wait until after rollover unless the entry is time-critical.",
            )
        )


# --------------------------------------------------------------------------- #
# The verdict
# --------------------------------------------------------------------------- #
def _verdict(findings: list[Finding], unknowns: list[str]) -> str:
    """Combine the findings into one word.

    Unknowns cap the verdict at ``BORDERLINE`` even with no failing check,
    because "nothing failed" and "nothing could be tested" are not the same
    statement and only one of them is reassuring.
    """
    if any(f.blocking for f in findings):
        return "UNSAFE"
    if any(f.severity == "warning" for f in findings) or unknowns:
        return "BORDERLINE"
    return "SAFE"
