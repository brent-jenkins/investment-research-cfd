"""What Trading 212 will actually accept, per instrument.

A trade can be inside every risk limit you have and still be rejected: the
quantity has more decimal places than the instrument allows, it is under the
minimum, the instrument is close-only that week. Trading 212 rejects some of
these **silently** — the ticket simply does not fill — so a helper that only
checked your own limits would say SAFE about an order that was never going to
be placed.

**These numbers are not shipped.**
-----------------------------------
There is no published, machine-readable table of Trading 212 instrument rules,
and this project makes no broker API calls. Inventing a plausible one would
produce exactly the failure the rest of this codebase is built to avoid: a
number that looks measured, is not, and is trusted because it is printed next
to numbers that are.

So the table starts **empty**, and you fill it in from what the Trading 212
ticket shows you:

    cfdinvest-helper --rules ~/.config/cfdinvest/t212_rules.toml ...

An instrument with no rules is reported as *unverified* — never as compliant.
Every check below returns "not checked" rather than "passed" when the rule it
needs is absent, and :mod:`cfdinvest.livehelper.safety` caps the verdict at
``BORDERLINE`` for anything uncomputable. That is the same rule the rest of
the helper follows, applied to a table nobody can populate for you.

The file format
---------------
::

    [SPX500]
    quantity_precision = 2     # decimal places the ticket accepts
    min_quantity = 0.1
    max_quantity = 100.0
    leverage = 20.0            # T212 sets this per instrument
    financing_rate = 0.0295    # annual, as a fraction
    spread_typical = 0.7       # in instrument currency, not basis points
    currency = "USD"
    trading_hours = "23:00-22:00"   # UTC, as the platform shows them
    close_only = false

Every field is optional. A partial entry is better than none: it makes the
checks it covers real and leaves the rest honestly unverified.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field, replace
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping

logger = logging.getLogger(__name__)

__all__ = [
    "InstrumentRules",
    "RuleTable",
    "DEFAULT_RULES_PATH",
    "load_rules",
    "rules_from_mapping",
    "round_to_precision",
    "validate_quantity",
    "validate_trading_hours",
    "validate_close_only",
]

#: Where a filled-in table is looked for when none is passed.
DEFAULT_RULES_PATH = "~/.config/cfdinvest/t212_rules.toml"


def _finite(value: Any) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


@dataclass(frozen=True, slots=True)
class InstrumentRules:
    """What one instrument will accept. Every field optional, absence honest.

    Attributes
    ----------
    quantity_precision:
        Decimal places the ticket accepts. ``2`` means ``0.68`` is fine and
        ``0.684`` is not — and Trading 212 does not always say which.
    min_quantity, max_quantity:
        The size bounds. A calculated size under the minimum is the common
        case: your risk limit says 0.04 and the instrument will not go below
        0.1, which means **the trade cannot be taken at your risk limit** —
        not that it should be rounded up.
    leverage:
        Set per instrument by Trading 212, and the divisor that turns exposure
        into margin. Not published in machine-readable form, which is why the
        margin figures elsewhere in this package are absent without it.
    financing_rate:
        Annual, as a fraction. Charged on the *notional*, overnight.
    spread_typical:
        In instrument currency, as the ticket shows it. Not basis points.
    trading_hours:
        ``"HH:MM-HH:MM"`` in UTC. A window that wraps midnight is normal for
        an index CFD and is handled.
    close_only:
        Trading 212 puts instruments into close-only around expiries and
        corporate actions. An open order then simply does not fill.
    """

    ticker: str
    quantity_precision: int | None = None
    min_quantity: float | None = None
    max_quantity: float | None = None
    leverage: float | None = None
    financing_rate: float | None = None
    spread_typical: float | None = None
    currency: str | None = None
    trading_hours: str | None = None
    close_only: bool | None = None
    #: When the contract expires, if it is a dated one.
    #:
    #: Trading 212's index CFDs are expiring contracts -- "UK 100 EXPIRING
    #: 17 SEPTEMBER 2026" -- rather than perpetual. That matters twice: a
    #: position cannot simply be held through it, and a dated contract
    #: usually carries its funding in the forward price instead of charging
    #: overnight interest. Recorded so the difference is visible; nothing
    #: here infers one from the other.
    expiry: str | None = None
    #: The instrument's name in the Trading 212 app, as you read it there.
    #:
    #: This **overrides** the suggestion shipped in t212_instruments, and it
    #: is the only name that has actually been checked. The shipped ones are
    #: hand-written and at least one was wrong -- "US 500" against the app's
    #: "USA 500" -- which is precisely the error that sends you to the wrong
    #: instrument or to none at all.
    t212_name: str | None = None
    #: ``False`` when Trading 212 does not offer it at all, so the signal
    #: layer can stop suggesting something you cannot place.
    available: bool | None = None
    source: str = "unverified"
    notes: str | None = None

    def __post_init__(self) -> None:
        if self.quantity_precision is not None:
            places = int(self.quantity_precision)
            if not 0 <= places <= 8:
                raise ValueError(
                    f"quantity_precision must be 0-8, got {self.quantity_precision}"
                )
            object.__setattr__(self, "quantity_precision", places)

        for name in ("min_quantity", "max_quantity", "leverage", "spread_typical"):
            value = getattr(self, name)
            if value is None:
                continue
            if not (_finite(value) and float(value) > 0):
                raise ValueError(f"{name} must be a positive number, got {value!r}")
            object.__setattr__(self, name, float(value))

        if (
            self.min_quantity is not None
            and self.max_quantity is not None
            and self.min_quantity > self.max_quantity
        ):
            raise ValueError(
                f"min_quantity {self.min_quantity} is above max_quantity "
                f"{self.max_quantity} for {self.ticker}"
            )

    @property
    def known(self) -> tuple[str, ...]:
        """Which fields were actually supplied."""
        return tuple(
            name
            for name in (
                "quantity_precision", "min_quantity", "max_quantity", "leverage",
                "financing_rate", "spread_typical", "currency", "trading_hours",
                "close_only",
            )
            if getattr(self, name) is not None
        )

    @property
    def empty(self) -> bool:
        return not self.known

    @property
    def verified(self) -> bool:
        """Whether these came from somewhere you can point at.

        ``source`` is whatever you wrote in the file. It is not checked and
        cannot be — the point is that *you* recorded where the numbers came
        from, so a future you can tell a transcribed ticket from a guess.
        """
        return self.source not in ("unverified", "", None)


@dataclass(frozen=True, slots=True)
class RuleTable:
    """Every instrument's rules, and where they came from."""

    rules: dict[str, InstrumentRules] = field(default_factory=dict)
    path: str | None = None

    def __len__(self) -> int:
        return len(self.rules)

    def __contains__(self, ticker: object) -> bool:
        return str(ticker).upper() in self.rules

    def get(self, ticker: str) -> InstrumentRules:
        """The rules for one instrument, or an empty set that says so.

        Never ``None``: a caller that had to check would eventually forget,
        and the forgetting would read as "no constraint" rather than "not
        known". An empty :class:`InstrumentRules` makes every check return
        "not checked" instead.
        """
        return self.rules.get(
            str(ticker).upper(), InstrumentRules(ticker=str(ticker).upper())
        )

    @property
    def tickers(self) -> tuple[str, ...]:
        return tuple(sorted(self.rules))


def rules_from_mapping(
    data: Mapping[str, Mapping[str, Any]], path: str | None = None
) -> RuleTable:
    """Build a table from a parsed document. Pure.

    An instrument whose entry is malformed is skipped with a warning rather
    than failing the load: one bad line in a hand-written file must not leave
    you with no rules at all, which would be a silent downgrade to unverified
    for everything.
    """
    rules: dict[str, InstrumentRules] = {}
    for ticker, entry in (data or {}).items():
        if not isinstance(entry, Mapping):
            logger.warning("Skipping %s: expected a table of fields", ticker)
            continue
        name = str(ticker).upper()
        fields = {
            key: value
            for key, value in entry.items()
            if key in InstrumentRules.__slots__ and key != "ticker"
        }
        unknown = set(entry) - set(fields) - {"ticker"}
        if unknown:
            logger.warning(
                "Ignoring unknown field(s) for %s: %s", name, ", ".join(sorted(unknown))
            )
        try:
            rules[name] = InstrumentRules(ticker=name, **fields)
        except (TypeError, ValueError) as error:
            logger.warning("Skipping %s: %s", name, error)
    return RuleTable(rules=rules, path=path)


def load_rules(path: str | Path | None = None) -> RuleTable:
    """Read the rules table. Never raises.

    A missing file is the normal state — nobody has one until they write one —
    and an empty table means every check reports "not checked", which is the
    correct answer rather than a failure.
    """
    target = Path(path).expanduser() if path is not None else Path(
        DEFAULT_RULES_PATH
    ).expanduser()

    try:
        if not target.is_file():
            logger.debug("No Trading 212 rules table at %s", target)
            return RuleTable(path=str(target))
        raw = target.read_bytes()
    except OSError as error:
        logger.warning("Could not read the rules table at %s: %s", target, error)
        return RuleTable(path=str(target))

    try:
        if target.suffix.lower() == ".json":
            import json

            data = json.loads(raw.decode("utf-8"))
        else:
            import tomllib

            data = tomllib.loads(raw.decode("utf-8"))
    except Exception as error:  # noqa: BLE001
        logger.warning("Could not parse the rules table at %s: %s", target, error)
        return RuleTable(path=str(target))

    if not isinstance(data, Mapping):
        logger.warning("The rules table at %s is not a table of instruments", target)
        return RuleTable(path=str(target))

    return rules_from_mapping(data, str(target))


# --------------------------------------------------------------------------- #
# The checks that need the table
# --------------------------------------------------------------------------- #
def round_to_precision(quantity: float, places: int | None) -> float | None:
    """Round *down* to the instrument's precision.

    Down, always. Rounding a size up to fit the ticket increases the position
    past whatever limit produced the number — a sizing helper that quietly
    added 4% to stay tidy would be undoing the work of the limit it just
    respected.

    ``None`` when the precision is not known, which is not the same as zero.
    """
    if places is None or not _finite(quantity):
        return None
    try:
        step = Decimal(1).scaleb(-int(places))
        return float(Decimal(str(float(quantity))).quantize(step, rounding=ROUND_DOWN))
    except (InvalidOperation, ValueError):  # pragma: no cover - defensive
        return None


@dataclass(frozen=True, slots=True)
class RuleIssue:
    """One broker constraint, and what it means for this order."""

    check: str
    severity: str
    message: str
    recommendation: str = ""
    value: float | None = None
    limit: float | None = None

    @property
    def blocking(self) -> bool:
        return self.severity == "critical"


def validate_quantity(
    quantity: float | None, rules: InstrumentRules
) -> list[RuleIssue]:
    """Will the ticket take this size?

    Three separate questions — too small, too large, too precise — because the
    answers are different actions. Too small usually means *do not take the
    trade*; too precise means round it down.
    """
    if not _finite(quantity) or float(quantity) <= 0:
        return [
            RuleIssue(
                check="quantity",
                severity="critical",
                message="No quantity to check.",
                recommendation="Enter the size you intend to trade.",
            )
        ]

    size = float(quantity)
    issues: list[RuleIssue] = []

    if rules.min_quantity is not None and size < rules.min_quantity:
        issues.append(
            RuleIssue(
                check="min_quantity",
                severity="critical",
                message=(
                    f"{size:,.4f} is below the {rules.min_quantity:,.4f} minimum "
                    f"for {rules.ticker}."
                ),
                recommendation=(
                    "Do not round up to reach the minimum — that is a larger "
                    "position than your limits allowed. Either skip this trade "
                    "or widen the stop so the risk budget buys the minimum size."
                ),
                value=size,
                limit=rules.min_quantity,
            )
        )

    if rules.max_quantity is not None and size > rules.max_quantity:
        issues.append(
            RuleIssue(
                check="max_quantity",
                severity="critical",
                message=(
                    f"{size:,.4f} is above the {rules.max_quantity:,.4f} maximum "
                    f"for {rules.ticker}."
                ),
                recommendation=f"Reduce the quantity to {rules.max_quantity:,.4f}.",
                value=size,
                limit=rules.max_quantity,
            )
        )

    if rules.quantity_precision is not None:
        rounded = round_to_precision(size, rules.quantity_precision)
        if rounded is not None and abs(rounded - size) > 1e-12:
            issues.append(
                RuleIssue(
                    check="quantity_precision",
                    severity="critical",
                    message=(
                        f"{size} has more than {rules.quantity_precision} decimal "
                        f"place(s); {rules.ticker} will not accept it."
                    ),
                    recommendation=(
                        f"Use {rounded:,.{rules.quantity_precision}f}. Rounded "
                        "down, so it stays inside the limit that produced it."
                    ),
                    value=size,
                    limit=float(rules.quantity_precision),
                )
            )
        if rounded == 0:
            issues.append(
                RuleIssue(
                    check="quantity_precision",
                    severity="critical",
                    message=(
                        f"{size} rounds to zero at {rules.quantity_precision} "
                        "decimal place(s)."
                    ),
                    recommendation=(
                        "The position you have sized is smaller than the "
                        "smallest one this instrument can express."
                    ),
                    value=size,
                )
            )

    if rules.quantity_precision is None and rules.min_quantity is None:
        issues.append(
            RuleIssue(
                check="quantity_rules",
                severity="info",
                message=(
                    f"No size rules recorded for {rules.ticker}, so the ticket "
                    "may still reject this."
                ),
                recommendation=(
                    "Add its precision and minimum to your rules table; see "
                    "cfdinvest/livehelper/README.md."
                ),
            )
        )
    return issues


def _minutes(text: str) -> int | None:
    try:
        hours, minutes = text.strip().split(":")
        total = int(hours) * 60 + int(minutes)
    except (ValueError, AttributeError):
        return None
    return total if 0 <= total <= 24 * 60 else None


def validate_trading_hours(
    rules: InstrumentRules, now=None
) -> list[RuleIssue]:
    """Is the instrument open? Windows that wrap midnight are normal.

    A warning rather than a block: the platform is the authority, the recorded
    window is a note you made, and being told "the market may be shut" is
    useful where being *stopped* on a stale note is not.
    """
    if not rules.trading_hours:
        return []

    import pandas as pd

    moment = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="UTC")
    if moment.tz is None:
        moment = moment.tz_localize("UTC")
    moment = moment.tz_convert("UTC")

    parts = str(rules.trading_hours).split("-")
    if len(parts) != 2:
        return [
            RuleIssue(
                check="trading_hours",
                severity="info",
                message=f"Could not read the trading hours {rules.trading_hours!r}.",
                recommendation='Use "HH:MM-HH:MM" in UTC.',
            )
        ]

    opens, closes = _minutes(parts[0]), _minutes(parts[1])
    if opens is None or closes is None:
        return [
            RuleIssue(
                check="trading_hours",
                severity="info",
                message=f"Could not read the trading hours {rules.trading_hours!r}.",
                recommendation='Use "HH:MM-HH:MM" in UTC.',
            )
        ]

    current = moment.hour * 60 + moment.minute
    # A window like 23:00-22:00 wraps midnight, which is how an index CFD
    # trades: almost all day, with a short break.
    inside = opens <= current < closes if opens < closes else (
        current >= opens or current < closes
    )
    if inside:
        return []

    return [
        RuleIssue(
            check="trading_hours",
            severity="warning",
            message=(
                f"{moment:%H:%M} UTC is outside the recorded window "
                f"{rules.trading_hours} for {rules.ticker}."
            ),
            recommendation=(
                "The order may sit unfilled until the session opens. Trading "
                "212 is the authority here; this is the window you recorded."
            ),
        )
    ]


def validate_close_only(rules: InstrumentRules, direction: str | None = None,
                        closing: bool = False) -> list[RuleIssue]:
    """Close-only instruments accept a closing order and silently drop an open.

    Which is the worst shape a rejection can take: you believe you are in a
    position you are not, and find out when it fails to move.
    """
    if not rules.close_only:
        return []
    if closing:
        return [
            RuleIssue(
                check="close_only",
                severity="info",
                message=f"{rules.ticker} is close-only. Closing is still allowed.",
            )
        ]
    return [
        RuleIssue(
            check="close_only",
            severity="critical",
            message=f"{rules.ticker} is marked close-only; an opening order "
            "will not fill.",
            recommendation=(
                "Trading 212 does this around expiries and corporate actions. "
                "Check the instrument page before assuming the order went in."
            ),
        )
    ]


def describe_coverage(table: RuleTable, tickers: Iterable[str]) -> dict[str, Any]:
    """What is known and what is not, for the dashboard.

    Reported as a coverage figure rather than a pass, because a table with one
    instrument in it protects one instrument.
    """
    wanted = [str(t).upper() for t in tickers]
    known = [t for t in wanted if t in table]
    return {
        "path": table.path,
        "instruments_recorded": len(table),
        "covered": tuple(known),
        "uncovered": tuple(t for t in wanted if t not in table),
        "coverage": len(known) / len(wanted) if wanted else None,
        "verified": tuple(
            t for t in known if table.get(t).verified
        ),
    }
