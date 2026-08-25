"""Which account a trade belongs in, and why.

With one account there is no question. With a live account, a demo, a trend
sleeve and one you only trade in calm markets, every trade needs an answer,
and the answer needs to be reconstructible six months later.

What routing is, and is not
---------------------------
**It is a suggestion with reasons attached.** Nothing here places a trade,
moves money between accounts, or changes an account's settings. It reads a
trade and a registry and says where it fits.

**It never routes to live by accident.** Every other failure here is
recoverable — a trade filed against the wrong sleeve is a bookkeeping error
you can correct. A trade routed to the real-money account because nothing
matched is not, so an unmatched trade goes to the *safest* candidate and says
that is what happened.

Why the reasons matter as much as the answer
--------------------------------------------
A router that returned only a name would be impossible to argue with. Six
months later, looking at a trade in the wrong sleeve, you need to know whether
the rule was wrong or the inputs were — and only the reasons distinguish
those.

So a routing decision carries every rule that matched, every rule that
rejected a candidate, and the confidence that follows from how much of the
input was actually available.

Pure: a trade, a registry and some context in; a decision out.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "RoutingDecision",
    "RoutingRule",
    "route_trade",
    "explain_routing",
]

#: Ordered safest first. Where nothing matches, the first of these that exists
#: gets the trade -- a demo account is the only harmless default.
SAFE_ORDER: tuple[str, ...] = ("virtual", "simulation", "live")


@dataclass(frozen=True, slots=True)
class RoutingRule:
    """One reason an account was chosen or rejected."""

    account: str
    rule: str
    matched: bool
    detail: str = ""

    @property
    def mark(self) -> str:
        return "+" if self.matched else "-"


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    """Where a trade goes, and every reason behind it."""

    account: str | None = None
    label: str = ""
    confidence: str = "low"
    reasons: tuple[str, ...] = ()
    rules: tuple[RoutingRule, ...] = ()
    candidates: tuple[str, ...] = ()
    fell_back: bool = False
    is_real_money: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def routed(self) -> bool:
        return self.account is not None

    @property
    def needs_a_decision(self) -> bool:
        """Whether a person should look at this before acting on it.

        A fall-back, a tie, or real money on thin evidence. Not "is it
        wrong" — the router cannot know that — but "is this one where the
        machine should not be the last word".
        """
        return self.fell_back or self.confidence == "low" or (
            self.is_real_money and self.confidence != "high"
        )

    def matched_for(self, account: str) -> tuple[RoutingRule, ...]:
        return tuple(
            rule for rule in self.rules if rule.account == account and rule.matched
        )


def route_trade(
    trade_intent=None,
    registry=None,
    *,
    strategy: str | None = None,
    regime=None,
    assessment=None,
    bias=None,
    plan=None,
    prefer: str | None = None,
) -> RoutingDecision:
    """Decide which account a trade belongs in.

    ``prefer`` is an explicit choice — a person saying where it goes. It wins
    over every rule, because a router that could overrule the person operating
    it is a router that gets switched off.
    """
    from cfdinvest.accounts.registry import default_registry

    book = registry if registry is not None else default_registry()
    enabled = [profile for profile in book if profile.enabled]

    if not enabled:
        return RoutingDecision(
            reasons=(
                "No account is enabled, so there is nowhere to route to.",
            ),
        )

    if prefer is not None:
        chosen = book.get(prefer)
        if chosen is None or not chosen.enabled:
            return RoutingDecision(
                reasons=(
                    f"{prefer!r} was asked for and is not an enabled account.",
                ),
                candidates=tuple(p.name for p in enabled),
            )
        return RoutingDecision(
            account=chosen.name,
            label=chosen.label,
            confidence="high",
            reasons=(f"Routed to {chosen.label} because you said so.",),
            rules=(RoutingRule(chosen.name, "explicit", True),),
            candidates=(chosen.name,),
            is_real_money=chosen.is_real_money,
        )

    instrument = str(getattr(trade_intent, "instrument", "") or "").upper()
    labels = _regime_labels(regime)
    rules: list[RoutingRule] = []
    scores: dict[str, int] = {}
    known: list[str] = []

    for profile in enabled:
        score = 0
        known.append(profile.name)

        if profile.instruments:
            hit = instrument in {i.upper() for i in profile.instruments}
            rules.append(
                RoutingRule(
                    profile.name, "instrument", hit,
                    f"{instrument or 'no instrument'} "
                    f"{'is' if hit else 'is not'} in "
                    f"{', '.join(profile.instruments)}",
                )
            )
            if not hit:
                # A named instrument list is a restriction, not a preference.
                scores[profile.name] = -1
                continue
            score += 2

        if profile.strategy:
            hit = strategy is not None and str(strategy) == profile.strategy
            rules.append(
                RoutingRule(
                    profile.name, "strategy", hit,
                    f"strategy {strategy!r} "
                    f"{'matches' if hit else 'does not match'} "
                    f"{profile.strategy!r}",
                )
            )
            if hit:
                score += 2
            elif strategy is not None:
                scores[profile.name] = -1
                continue

        if profile.regimes:
            hit = bool(labels & {r.lower() for r in profile.regimes})
            rules.append(
                RoutingRule(
                    profile.name, "regime", hit,
                    f"{', '.join(sorted(labels)) or 'no regime'} "
                    f"{'is' if hit else 'is not'} in "
                    f"{', '.join(profile.regimes)}",
                )
            )
            if hit:
                score += 2
            elif labels:
                scores[profile.name] = -1
                continue

        if plan is not None and getattr(plan, "available", False):
            stance = plan.stance_on(instrument) if instrument else None
            if stance == "avoided":
                rules.append(
                    RoutingRule(
                        profile.name, "meta-strategy", False,
                        "the plan lists this instrument as one to avoid",
                    )
                )
                score -= 1

        scores[profile.name] = scores.get(profile.name, 0) + score

    viable = {name: score for name, score in scores.items() if score >= 0}
    reasons: list[str] = []

    if not viable:
        return _fall_back(
            book, enabled, rules, known,
            "Every account ruled this trade out.",
        )

    best = max(viable.values())
    winners = sorted(name for name, score in viable.items() if score == best)

    if best == 0:
        return _fall_back(
            book, enabled, rules, known,
            "Nothing about this trade matched any account's rules.",
        )

    if len(winners) > 1:
        # A tie is not a decision. Picking one alphabetically and presenting
        # it as an answer is worse than saying two accounts fit.
        chosen = _safest(book, winners)
        reasons.append(
            f"{len(winners)} accounts fit equally well "
            f"({', '.join(winners)}); took the safer of them."
        )
        confidence = "low"
    else:
        chosen = book.get(winners[0])
        confidence = "high" if best >= 4 else "medium"

    if chosen is None:
        return _fall_back(book, enabled, rules, known, "No account could be read.")

    for rule in rules:
        if rule.account == chosen.name and rule.matched:
            reasons.append(f"{rule.rule}: {rule.detail}")

    if chosen.is_real_money and confidence != "high":
        reasons.append(
            "This is the real-money account and the match was not clear-cut. "
            "Worth confirming before you place it."
        )

    return RoutingDecision(
        account=chosen.name,
        label=chosen.label,
        confidence=confidence,
        reasons=tuple(reasons),
        rules=tuple(rules),
        candidates=tuple(sorted(known)),
        is_real_money=chosen.is_real_money,
        details={"scores": scores, "winners": winners},
    )


def _fall_back(book, enabled, rules, known, why: str) -> RoutingDecision:
    """Where an unmatched trade goes.

    The safest enabled account, never the live one if anything else exists.
    Every other routing mistake is a bookkeeping error you can correct; a
    trade routed to real money because nothing matched is not.
    """
    chosen = _safest(book, [profile.name for profile in enabled])
    if chosen is None:
        return RoutingDecision(
            reasons=(why, "No account could be chosen."),
            rules=tuple(rules),
            candidates=tuple(sorted(known)),
            fell_back=True,
        )

    reasons = [why]
    if chosen.is_real_money:
        reasons.append(
            f"Fell back to {chosen.label}, which is the only enabled account "
            "and holds real money. Confirm before you place anything."
        )
    else:
        reasons.append(
            f"Fell back to {chosen.label}, which is the safest enabled "
            "account. Nothing was routed to real money by default."
        )

    return RoutingDecision(
        account=chosen.name,
        label=chosen.label,
        confidence="low",
        reasons=tuple(reasons),
        rules=tuple(rules),
        candidates=tuple(sorted(known)),
        fell_back=True,
        is_real_money=chosen.is_real_money,
    )


def _safest(book, names: Sequence[str]):
    """The least dangerous of several accounts.

    Ordered by what a mistake costs: a demo account first, then a simulation
    tree, then anything else, and real money last.
    """
    available = [book.get(name) for name in names]
    available = [profile for profile in available if profile is not None]
    if not available:
        return None

    def rank(profile) -> tuple[int, str]:
        if profile.type in SAFE_ORDER:
            return (SAFE_ORDER.index(profile.type), profile.name)
        # An unlisted type is safer than live and less certain than a demo.
        return (SAFE_ORDER.index("live") - 1, profile.name)

    return sorted(available, key=rank)[0]


def _regime_labels(regime) -> set[str]:
    """Whatever regime labels are in force, lowercased."""
    if regime is None:
        return set()
    if isinstance(regime, str):
        return {regime.lower()}
    if isinstance(regime, dict):
        return {str(value).lower() for value in regime.values() if value}

    labels = getattr(regime, "regimes", None)
    if isinstance(labels, dict):
        return {str(value).lower() for value in labels.values() if value}
    return set()


def explain_routing(decision: RoutingDecision) -> list[str]:
    """The decision as lines, for a terminal or an email."""
    if not decision.routed:
        return ["Not routed."] + [f"  {reason}" for reason in decision.reasons]

    lines = [
        f"{decision.label} ({decision.account})  "
        f"[{decision.confidence} confidence]"
    ]
    if decision.is_real_money:
        lines.append("  REAL MONEY")
    lines += [f"  {reason}" for reason in decision.reasons]

    rejected = [rule for rule in decision.rules if not rule.matched]
    if rejected:
        lines.append("  Ruled out:")
        lines += [
            f"    - {rule.account}: {rule.detail}" for rule in rejected
        ]
    if decision.needs_a_decision:
        lines.append("  Worth confirming before you act on this.")
    return lines
