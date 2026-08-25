"""Trading 212 live-execution helper: check a trade before you place it.

**This package cannot trade.** There is no broker client, no HTTP call, no
credential, and no automation of the Trading 212 interface anywhere in it. It
reads an intended trade, measures it against limits you set, and prints a
summary you read while placing the order yourself.

It is also the consolidated Trading 212 safety core. The execution-rule
checks, the risk-limit enforcement, the margin warning, the sizing assistant
and the go/no-go verdict all live here rather than in six packages, because
they are one operation: measure the trade, compare each figure with a limit,
say what to do. Splitting them would give every threshold several homes and
let them disagree.

::

    from cfdinvest.livehelper import check

    print(check("long SPX500 0.68 entry=4434.8 stop=4390 ccy=USD lev=20",
                fx_rate=0.79))
"""

from __future__ import annotations

from dataclasses import dataclass

from cfdinvest.livehelper.config import LiveHelperConfig, load_live_helper_config
from cfdinvest.livehelper.intent import TradeIntent, parse_trade_intent
from cfdinvest.livehelper.safety import (
    Finding,
    MarketState,
    TradeSafetyAssessment,
    evaluate_trade_safety,
)
from cfdinvest.livehelper.sizing_helper import (
    SizingRecommendation,
    compute_safe_sizing,
)
from cfdinvest.livehelper.context import (
    BiasProfile,
    MarginState,
    MetaStrategyPlan,
    PerformanceTrends,
    RegimeProfile,
    TradingContext,
    load_context,
)
from cfdinvest.livehelper.margin_warning import (
    MarginWarning,
    compute_margin_warning,
)
from cfdinvest.livehelper.refine_checklist import Checklist, refine_checklist
from cfdinvest.livehelper.refine_exit import ExitRefinement, refine_exit
from cfdinvest.livehelper.refine_quality import QualityScore, refine_quality
from cfdinvest.livehelper.refine_sizing import (
    RefinementConfig,
    SizingRefinement,
    refine_sizing,
)
from cfdinvest.livehelper.safe_exit import ExitPlan, compute_safe_exit
from cfdinvest.livehelper.confirm_checks import (
    ConfirmationReview,
    review_confirmation,
    validate_confirmation_deviation,
)
from cfdinvest.livehelper.summary import build_execution_helper_summary
from cfdinvest.livehelper.t212_rules import (
    InstrumentRules,
    RuleTable,
    load_rules,
    validate_quantity,
)

__all__ = [
    "LiveHelperConfig",
    "load_live_helper_config",
    "TradeIntent",
    "parse_trade_intent",
    "MarketState",
    "Finding",
    "TradeSafetyAssessment",
    "evaluate_trade_safety",
    "SizingRecommendation",
    "compute_safe_sizing",
    "build_execution_helper_summary",
    "InstrumentRules",
    "RuleTable",
    "load_rules",
    "validate_quantity",
    "ConfirmationReview",
    "review_confirmation",
    "validate_confirmation_deviation",
    "TradingContext",
    "PerformanceTrends",
    "RegimeProfile",
    "BiasProfile",
    "MetaStrategyPlan",
    "MarginState",
    "load_context",
    "RefinementConfig",
    "SizingRefinement",
    "refine_sizing",
    "ExitRefinement",
    "refine_exit",
    "MarginWarning",
    "compute_margin_warning",
    "QualityScore",
    "refine_quality",
    "Checklist",
    "refine_checklist",
    "ExitPlan",
    "compute_safe_exit",
    "Refinement",
    "refine",
    "assess",
    "check",
]


def assess(raw_input, config=None, **market):
    """Parse, evaluate and size in one call.

    Returns ``(intent, assessment, sizing)``. The three pieces are kept
    separate rather than merged, because a caller rendering a web page needs
    the structured values and a caller writing an email needs the text.
    """
    intent = parse_trade_intent(raw_input)
    settings = config or load_live_helper_config()
    state = market.pop("market_state", None) or MarketState(**market)
    assessment = evaluate_trade_safety(intent, settings, state)
    sizing = compute_safe_sizing(intent, settings, state)
    return intent, assessment, sizing


def check(raw_input, config=None, **market) -> str:
    """The whole helper, as the block of text you read before trading."""
    intent, assessment, sizing = assess(raw_input, config, **market)
    return build_execution_helper_summary(
        intent, assessment, sizing, config=config or load_live_helper_config()
    )


@dataclass(frozen=True, slots=True)
class Refinement:
    """Everything the refinement layer produced for one trade.

    The six refinements are kept separate rather than merged into the
    assessment, because they answer different questions and one of them --
    the size -- is a *proposal* the safety engine still bounds. Folding them
    into the verdict would hide which is which.
    """

    sizing: "SizingRefinement | None" = None
    exit: "ExitRefinement | None" = None
    margin: "MarginWarning | None" = None
    quality: "QualityScore | None" = None
    checklist: "Checklist | None" = None
    exit_plan: "ExitPlan | None" = None
    context: "TradingContext | None" = None

    @property
    def missing(self) -> tuple[str, ...]:
        return self.context.missing if self.context else ()

    @property
    def go(self) -> bool:
        """The checklist's answer. Not a second verdict.

        The safety engine owns whether the trade is permitted; this says
        whether every item was checked and passed. A trade can be SAFE with
        an unchecked behavioural item, and that is worth seeing.
        """
        return bool(self.checklist and self.checklist.go)


def refine(
    trade_intent,
    assessment=None,
    sizing_recommendation=None,
    context=None,
    config=None,
    *,
    execution_history=None,
    rules=None,
    position_quantity=None,
) -> Refinement:
    """Run every refinement over one trade, in the order they depend on.

    The margin warning is computed first because the checklist reads it, and
    the exit plan last because it is about a position rather than an intent.
    """
    settings = config or load_live_helper_config()
    where = context or TradingContext()

    margin = compute_margin_warning(assessment, where, settings)
    return Refinement(
        sizing=refine_sizing(
            trade_intent, assessment, sizing_recommendation, where, settings
        ),
        exit=refine_exit(trade_intent, assessment, where, settings),
        margin=margin,
        quality=refine_quality(
            trade_intent, assessment, where, settings,
            execution_history=execution_history,
        ),
        checklist=refine_checklist(
            trade_intent, assessment, where, settings, margin_warning=margin
        ),
        exit_plan=compute_safe_exit(
            trade_intent, assessment, where, settings,
            position_quantity=position_quantity, rules=rules,
        ),
        context=where,
    )
