"""Signal engine: target positions from preprocessing and regime outputs.

Reads only from :class:`~cfdinvest.preprocessing.AnalysisFrame` and
:class:`~cfdinvest.regimes.RegimeFrame`, and writes to neither.

    trend      -> crossovers, breakouts, blended strength per asset
    sizing     -> volatility-targeted and ATR-risk position sizes
    filters    -> regime, correlation and macro gates
    combined   -> direction / size / confidence / reason
    frame      -> SignalFrame, the single output

A signal is an *intention*: what to hold and how much. Turning intentions
into fills, costs and P&L belongs to the execution layer, and nothing here
assumes how that happens.

Direction resolution is a precedence list over an enumerable domain -- 32
combinations of the discrete facts, each mapping to exactly one direction,
verified by :func:`cfdinvest.signals.combined.verify_direction_totality`.
"""

from cfdinvest.signals.combined import (
    DIRECTIONS,
    direction_decision_table,
    verify_direction_totality,
)
from cfdinvest.signals.filters import (
    ASSET_CLASSES,
    FilterOutcome,
    combine_outcomes,
    correlation_filter,
    macro_filter,
    regime_filter,
)
from cfdinvest.signals.frame import SignalConfig, SignalFrame, generate_signals
from cfdinvest.signals.sizing import (
    atr_risk_size,
    blend_sizes,
    volatility_target_size,
)
from cfdinvest.signals.trend import trend_components, trend_strength

__all__ = [
    "SignalConfig",
    "SignalFrame",
    "generate_signals",
    "trend_components",
    "trend_strength",
    "volatility_target_size",
    "atr_risk_size",
    "blend_sizes",
    "regime_filter",
    "correlation_filter",
    "macro_filter",
    "combine_outcomes",
    "FilterOutcome",
    "direction_decision_table",
    "verify_direction_totality",
    "DIRECTIONS",
    "ASSET_CLASSES",
]
