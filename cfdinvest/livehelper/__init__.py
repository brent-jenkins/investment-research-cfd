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
from cfdinvest.livehelper.summary import build_execution_helper_summary

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
