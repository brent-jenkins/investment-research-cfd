"""The eight checks that need Trading 212's own numbers.

:mod:`cfdinvest.livehelper.t212_rules` holds the schema and does the loading.
This holds the checks, so that adding one is a change in one file and the
loader does not grow reasons to know what a rule *means*.

Three outcomes, not two
-----------------------
Every check returns one of:

``pass``
    A rule was recorded and the trade satisfies it.
``critical`` / ``warning``
    A rule was recorded and the trade does not.
``unchecked``
    No rule was recorded, so nothing was tested.

The third is the reason this module reports passes at all. Elsewhere in the
helper a silent check is a passing check, which is fine when every check can
always run. Here most of them cannot: the table is one you fill in by hand,
and a report that showed nothing would be identical whether nine rules passed
or none existed. **The coverage is the finding.**

A passing check never moves the verdict. It exists so the absence of one is
visible.

Nothing is assumed
------------------
A field that is not recorded is not defaulted. There is no "typical" minimum
size, no "usual" leverage, no assumed spread. Trading 212 publishes none of
this in machine-readable form and this project makes no broker calls, so an
invented value would be a number that looks measured, is not, and is trusted
because it sits beside numbers that are.

Pure: a trade and a rule set in, findings out.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "CHECKS",
    "TITLES",
    "RuleFinding",
    "check_quantity_precision",
    "check_min_quantity",
    "check_max_quantity",
    "check_leverage",
    "check_financing_rate",
    "check_spread",
    "check_trading_hours",
    "check_close_only",
    "check_all",
    "coverage_of",
]

#: Every check, in the order a person would ask them: can the ticket take this
#: order at all, then what will it cost, then is the market even open.
#:
#: Prefixed ``t212_`` throughout, and that is not decoration. The safety engine
#: already has a ``leverage`` check -- *effective* leverage, exposure over
#: balance -- and this one is about the *instrument's* leverage, which is a
#: different number answering a different question. Sharing a name made the
#: checklist and the coverage count confuse the two.
CHECKS: tuple[tuple[str, str], ...] = (
    ("t212_quantity_precision", "Quantity precision"),
    ("t212_min_quantity", "Minimum size"),
    ("t212_max_quantity", "Maximum size"),
    ("t212_leverage", "Instrument leverage"),
    ("t212_financing_rate", "Overnight financing"),
    ("t212_spread", "Typical spread"),
    ("t212_trading_hours", "Trading hours"),
    ("t212_close_only", "Close-only"),
)

#: Which recorded field each check needs. The names differ now that the
#: checks are prefixed, so the mapping is explicit rather than an identity.
#: The human name for each check, so anything holding only a check name can
#: still say it in words.
TITLES: dict[str, str] = {name: title for name, title in CHECKS}

NEEDS: dict[str, str] = {
    "t212_quantity_precision": "quantity_precision",
    "t212_min_quantity": "min_quantity",
    "t212_max_quantity": "max_quantity",
    "t212_leverage": "leverage",
    "t212_financing_rate": "financing_rate",
    "t212_spread": "spread_typical",
    "t212_trading_hours": "trading_hours",
    "t212_close_only": "close_only",
}


def _finite(value: Any) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


@dataclass(frozen=True, slots=True)
class RuleFinding:
    """One broker rule, and what it said about this trade.

    ``severity`` is ``"pass"``, ``"warning"``, ``"critical"`` or
    ``"unchecked"``. The last means the rule was not recorded, and is the only
    one of the four that is about the *table* rather than the trade — which is
    why it is not ``info``: it should never read as a reason the verdict is
    what it is.
    """

    check: str
    title: str
    severity: str
    message: str
    recommendation: str = ""
    value: float | None = None
    limit: float | None = None

    @property
    def passed(self) -> bool:
        return self.severity == "pass"

    @property
    def checked(self) -> bool:
        """Whether a rule existed to test against."""
        return self.severity != "unchecked"

    @property
    def blocking(self) -> bool:
        return self.severity == "critical"


def _not_checked(check: str, title: str, ticker: str) -> RuleFinding:
    """The answer when the rule was never written down.

    Deliberately not silence. A report that showed nothing here would read
    the same whether the rule passed or was never recorded, and those are
    the two states this whole table exists to distinguish.
    """
    return RuleFinding(
        check=check,
        title=title,
        severity="unchecked",
        message=f"Not checked — no {title.lower()} recorded for {ticker}.",
        recommendation=(
            f"Add it to your rules table from what the ticket shows, and this "
            "becomes a real check."
        ),
    )


def _passed(check: str, title: str, message: str, **extra) -> RuleFinding:
    return RuleFinding(
        check=check, title=title, severity="pass", message=message, **extra
    )


# --------------------------------------------------------------------------- #
# Can the ticket take this order?
# --------------------------------------------------------------------------- #
def check_quantity_precision(intent, rules) -> RuleFinding:
    """Trading 212 rejects an over-precise quantity **silently**.

    The ticket simply does not fill and nothing says why, which makes this
    the most valuable of the eight to have recorded.
    """
    from cfdinvest.livehelper.t212_rules import round_to_precision

    title = "Quantity precision"
    if rules.quantity_precision is None:
        return _not_checked("t212_quantity_precision", title, rules.ticker)

    quantity = getattr(intent, "quantity", None)
    if not _finite(quantity):
        return _not_checked("t212_quantity_precision", title, rules.ticker)

    places = int(rules.quantity_precision)
    rounded = round_to_precision(abs(float(quantity)), places)

    if rounded is not None and rounded == 0:
        return RuleFinding(
            check="t212_quantity_precision",
            title=title,
            severity="critical",
            message=(
                f"{abs(float(quantity))} rounds to zero at {places} decimal "
                f"place(s) for {rules.ticker}."
            ),
            recommendation=(
                "The position you have sized is smaller than the smallest one "
                "this instrument can express."
            ),
            value=abs(float(quantity)),
        )

    if rounded is not None and abs(rounded - abs(float(quantity))) > 1e-12:
        return RuleFinding(
            check="t212_quantity_precision",
            title=title,
            severity="critical",
            message=(
                f"{abs(float(quantity))} has more than {places} decimal "
                f"place(s); {rules.ticker} will not accept it."
            ),
            recommendation=(
                f"Use {rounded:,.{places}f}. Rounded down, so it stays inside "
                "the limit that produced it. Trading 212 rejects this "
                "silently — the ticket simply does not fill."
            ),
            value=abs(float(quantity)),
            limit=float(places),
        )

    return _passed(
        "t212_quantity_precision", title,
        f"{abs(float(quantity))} fits {rules.ticker}'s {places} decimal "
        "place(s).",
    )


def check_min_quantity(intent, rules) -> RuleFinding:
    """Below the minimum the order will not go.

    The recommendation matters more than the finding here. Your risk limit
    says 0.04 and the instrument stops at 0.1: the answer is that **the trade
    cannot be taken at your risk limit**, not that the size should be raised
    to meet it.
    """
    title = "Minimum size"
    if rules.min_quantity is None:
        return _not_checked("t212_min_quantity", title, rules.ticker)

    quantity = getattr(intent, "quantity", None)
    if not _finite(quantity):
        return _not_checked("t212_min_quantity", title, rules.ticker)

    size, minimum = abs(float(quantity)), float(rules.min_quantity)
    if size >= minimum:
        return _passed(
            "t212_min_quantity", title,
            f"{size:,.4f} is at or above {rules.ticker}'s {minimum:,.4f} "
            "minimum.",
            value=size, limit=minimum,
        )

    return RuleFinding(
        check="t212_min_quantity",
        title=title,
        severity="critical",
        message=(
            f"{size:,.4f} is below the {minimum:,.4f} minimum for "
            f"{rules.ticker}."
        ),
        recommendation=(
            "Do not round up to reach it — that is a larger position than "
            "your limits allowed. Either skip this trade, or widen the stop "
            "so the risk budget buys the minimum size."
        ),
        value=size,
        limit=minimum,
    )


def check_max_quantity(intent, rules) -> RuleFinding:
    """Above the maximum the order will not go either."""
    title = "Maximum size"
    if rules.max_quantity is None:
        return _not_checked("t212_max_quantity", title, rules.ticker)

    quantity = getattr(intent, "quantity", None)
    if not _finite(quantity):
        return _not_checked("t212_max_quantity", title, rules.ticker)

    size, maximum = abs(float(quantity)), float(rules.max_quantity)
    if size <= maximum:
        return _passed(
            "t212_max_quantity", title,
            f"{size:,.4f} is within {rules.ticker}'s {maximum:,.4f} maximum.",
            value=size, limit=maximum,
        )

    return RuleFinding(
        check="t212_max_quantity",
        title=title,
        severity="critical",
        message=(
            f"{size:,.4f} is above the {maximum:,.4f} maximum for "
            f"{rules.ticker}."
        ),
        recommendation=f"Reduce the quantity to {maximum:,.4f}, or split it.",
        value=size,
        limit=maximum,
    )


# --------------------------------------------------------------------------- #
# What will it cost?
# --------------------------------------------------------------------------- #
def check_leverage(intent, rules, market=None) -> RuleFinding:
    """Does the leverage you assumed match the one Trading 212 sets?

    This is the quiet one. Instrument leverage is the divisor that turns
    exposure into margin, so assuming 20x where the platform gives 5x
    understates the margin this trade commits by four times — and margin is
    what force-closes without warning.
    """
    title = "Instrument leverage"
    if not _finite(rules.leverage):
        return _not_checked("t212_leverage", title, rules.ticker)

    recorded = float(rules.leverage)
    assumed = getattr(intent, "leverage", None)
    if not _finite(assumed):
        assumed = getattr(market, "leverage", None)

    if not _finite(assumed):
        return _passed(
            "t212_leverage", title,
            f"{rules.ticker} is recorded at {recorded:,.0f}x, which is what "
            "the margin figures used.",
            value=recorded,
        )

    if abs(float(assumed) - recorded) < 1e-9:
        return _passed(
            "t212_leverage", title,
            f"{recorded:,.0f}x matches what you supplied.",
            value=recorded, limit=recorded,
        )

    understated = float(assumed) > recorded
    return RuleFinding(
        check="t212_leverage",
        title=title,
        severity="critical" if understated else "warning",
        message=(
            f"You supplied {float(assumed):,.0f}x but {rules.ticker} is "
            f"recorded at {recorded:,.0f}x."
        ),
        recommendation=(
            (
                "The margin this trade commits is understated by "
                f"{float(assumed) / recorded:,.1f} times. Margin is what "
                "force-closes without warning, and it does not ask first."
            )
            if understated
            else (
                "The margin figures are conservative by "
                f"{recorded / float(assumed):,.1f} times — safe, but the "
                "headroom is real and you are not using it."
            )
        ),
        value=float(assumed),
        limit=recorded,
    )


def check_financing_rate(intent, rules, config=None, *, nights: int = 1) -> RuleFinding:
    """What holding this overnight costs, against what the trade risks.

    Reported as a share of the risk budget rather than in isolation. Six
    pounds a night is meaningless; six pounds a night against a forty-pound
    stop is a fifth of the risk budget every three nights, and a position
    that has to move to stand still.
    """
    from cfdinvest.livehelper.config import LiveHelperConfig

    limits = config or LiveHelperConfig()
    title = "Overnight financing"
    if not _finite(rules.financing_rate):
        return _not_checked("t212_financing_rate", title, rules.ticker)

    rate = float(rules.financing_rate)
    quantity = getattr(intent, "quantity", None)
    price = getattr(intent, "entry_price", None)
    if not (_finite(quantity) and _finite(price)):
        return _passed(
            "t212_financing_rate", title,
            f"{rules.ticker} is recorded at {rate:.2%} a year. No size or "
            "price supplied, so the cost was not put in context.",
            value=rate,
        )

    notional = abs(float(quantity) * float(price))
    nightly = notional * rate / 365.0
    total = nightly * max(1, int(nights))

    stop = getattr(intent, "stop_loss", None)
    if _finite(stop) and _finite(price):
        risk = abs(float(price) - float(stop)) * abs(float(quantity))
    else:
        risk = limits.max_risk_per_trade_gbp

    share = total / risk if risk > 0 else None

    if share is not None and share >= 0.05:
        return RuleFinding(
            check="t212_financing_rate",
            title=title,
            severity="warning",
            message=(
                f"Financing is {nightly:,.2f} a night at {rate:.2%} a year — "
                f"{share:.0%} of what this trade risks, per night."
            ),
            recommendation=(
                "At this rate the position has to move to stand still. Worth "
                "knowing before you hold it over a weekend, which is three "
                "nights and not one."
            ),
            value=nightly,
            limit=risk,
        )

    return _passed(
        "t212_financing_rate", title,
        f"Financing is {nightly:,.2f} a night at {rate:.2%} a year"
        + (f" — {share:.1%} of what the trade risks." if share is not None else "."),
        value=nightly,
    )


def check_spread(intent, rules, config=None) -> RuleFinding:
    """The spread against the stop distance, not against the price.

    The ratio is what matters and the threshold for it already exists in
    :class:`~cfdinvest.livehelper.config.LiveHelperConfig`. This check is the
    same question asked of the *recorded typical* spread rather than the one
    you are looking at now — which is the version that can be answered before
    the platform is even open.
    """
    from cfdinvest.livehelper.config import LiveHelperConfig

    limits = config or LiveHelperConfig()
    title = "Typical spread"
    if not _finite(rules.spread_typical):
        return _not_checked("t212_spread", title, rules.ticker)

    spread = float(rules.spread_typical)
    price = getattr(intent, "entry_price", None)
    stop = getattr(intent, "stop_loss", None)

    if not (_finite(price) and _finite(stop)):
        return _passed(
            "t212_spread", title,
            f"{rules.ticker}'s typical spread is {spread:,.4f}. No stop "
            "supplied, so it was not put in context — a spread only means "
            "something as a share of what you are risking.",
            value=spread,
        )

    distance = abs(float(price) - float(stop))
    if distance <= 0:
        return _not_checked("t212_spread", title, rules.ticker)

    share = spread / distance
    if share > limits.spread_warning_threshold:
        return RuleFinding(
            check="t212_spread",
            title=title,
            severity="warning",
            message=(
                f"The typical spread of {spread:,.4f} is {share:.0%} of your "
                f"{distance:,.4f} stop distance."
            ),
            recommendation=(
                "That much of the risk budget is spent on entry before the "
                "trade has done anything. A wider stop and a smaller size buys "
                "the same risk with more room."
            ),
            value=share,
            limit=limits.spread_warning_threshold,
        )

    return _passed(
        "t212_spread", title,
        f"The typical spread of {spread:,.4f} is {share:.0%} of the stop "
        "distance.",
        value=share, limit=limits.spread_warning_threshold,
    )


# --------------------------------------------------------------------------- #
# Is it even open?
# --------------------------------------------------------------------------- #
def check_trading_hours(intent, rules, now=None) -> RuleFinding:
    """Is the instrument open, by the window you recorded?

    A warning rather than a block: the platform is the authority and the
    recorded window is a note you made. Being told the market may be shut is
    useful; being *stopped* by a stale note is not.
    """
    from cfdinvest.livehelper.t212_rules import validate_trading_hours

    title = "Trading hours"
    if not rules.trading_hours:
        return _not_checked("t212_trading_hours", title, rules.ticker)

    issues = validate_trading_hours(rules, now)
    if not issues:
        return _passed(
            "t212_trading_hours", title,
            f"Inside {rules.ticker}'s recorded window ({rules.trading_hours}).",
        )

    issue = issues[0]
    return RuleFinding(
        check="t212_trading_hours",
        title=title,
        # An unreadable window is a fault in the table, not a fact about the
        # trade -- so it reports as unchecked rather than as a warning.
        severity="unchecked" if issue.severity == "info" else issue.severity,
        message=issue.message,
        recommendation=issue.recommendation,
    )


def check_close_only(intent, rules, *, closing: bool = False) -> RuleFinding:
    """Close-only instruments accept a closing order and drop an opening one.

    The worst shape a rejection can take: you believe you are in a position
    you are not, and find out when it fails to move.
    """
    title = "Close-only"
    if rules.close_only is None:
        return _not_checked("t212_close_only", title, rules.ticker)

    if not rules.close_only:
        return _passed(
            "t212_close_only", title, f"{rules.ticker} is open for new positions."
        )

    if closing:
        return _passed(
            "t212_close_only", title,
            f"{rules.ticker} is close-only, and closing is still allowed.",
        )

    return RuleFinding(
        check="t212_close_only",
        title=title,
        severity="critical",
        message=(
            f"{rules.ticker} is marked close-only; an opening order will not "
            "fill."
        ),
        recommendation=(
            "Trading 212 does this around expiries and corporate actions. "
            "Check the instrument page before assuming the order went in — "
            "the rejection is silent."
        ),
    )


# --------------------------------------------------------------------------- #
# All of them
# --------------------------------------------------------------------------- #
def check_all(
    intent,
    table=None,
    *,
    config=None,
    market=None,
    now=None,
    closing: bool = False,
    nights: int = 1,
) -> tuple[list[RuleFinding], bool]:
    """Run every check. Returns the findings and whether the instrument is known.

    ``verified`` is ``False`` when the instrument is not in the table at all.
    That is a different statement from every field being unrecorded — one
    says you have not written this instrument down, the other that you wrote
    it down without that field — and only the first caps the verdict.
    """
    instrument = str(getattr(intent, "instrument", "") or "")

    if table is None:
        from cfdinvest.livehelper.t212_rules import InstrumentRules

        rules = InstrumentRules(ticker=instrument or "UNKNOWN")
        verified = False
    else:
        rules = table.get(instrument)
        verified = not rules.empty

    findings = [
        check_quantity_precision(intent, rules),
        check_min_quantity(intent, rules),
        check_max_quantity(intent, rules),
        check_leverage(intent, rules, market),
        check_financing_rate(intent, rules, config, nights=nights),
        check_spread(intent, rules, config),
        check_trading_hours(intent, rules, now),
        check_close_only(intent, rules, closing=closing),
    ]
    return findings, verified


def coverage_of(findings: Sequence[RuleFinding]) -> dict[str, Any]:
    """How much of the trade the table could actually speak to.

    The number worth showing. Eight checks with two rules recorded is two
    checks and six blanks, and a report that did not say so would look like
    eight passes.
    """
    # Keyed on severity rather than on a property, so this works on both a
    # RuleFinding and the plain Finding the assessment carries. They share the
    # vocabulary and not the class.
    checked = [f for f in findings if f.severity != "unchecked"]
    return {
        "total": len(findings),
        "checked": len(checked),
        "passed": len([f for f in checked if f.severity == "pass"]),
        "failed": len([f for f in checked if f.severity != "pass"]),
        "not_checked": len(findings) - len(checked),
        "coverage": len(checked) / len(findings) if findings else None,
        "missing": tuple(
            f.check for f in findings if f.severity == "unchecked"
        ),
        # The same list in words. The keys are for code and read badly in a
        # sentence a person is meant to act on. Looked up from CHECKS rather
        # than read off the finding, because a plain Finding carries no title.
        "missing_titles": tuple(
            TITLES.get(f.check, f.check)
            for f in findings
            if f.severity == "unchecked"
        ),
    }
