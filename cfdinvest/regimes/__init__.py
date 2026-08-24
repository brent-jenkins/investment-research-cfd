"""Regime classification: what kind of market and macro backdrop is this?

Reads only from :class:`~cfdinvest.preprocessing.AnalysisFrame` and writes
nothing back. Every transform is a pure function; the only object holding
state is the :class:`RegimeFrame` collecting the labels.

    volatility   -> low / medium / high            (realised vol + ATR)
    trend        -> uptrend / sideways / downtrend (moving averages + momentum)
    correlation  -> diversified / normal / breakdown
    macro        -> expansion / overheating / slowdown / stagflation / contraction
    combined     -> crisis / stagflation / risk_off / neutral / risk_on / expansion

The combined mapping is a precedence list of predicates, enumerable in full
by :func:`cfdinvest.regimes.combined.decision_table` -- 384 combinations,
each mapping to exactly one label, verified rather than asserted.

A regime is a *description* of conditions. No signal logic lives here.
"""

from cfdinvest.regimes.combined import (
    COMBINED_LABELS,
    COMBINED_REGIMES,
    classify_combined,
    decision_table,
    verify_totality,
)
from cfdinvest.regimes.correlation import classify_correlation, classify_hedge
from cfdinvest.regimes.frame import (
    RegimeConfig,
    RegimeFrame,
    attach_regimes,
    classify_regimes,
)
from cfdinvest.regimes.labels import UNKNOWN
from cfdinvest.regimes.macro import classify_macro
from cfdinvest.regimes.trend import classify_trend
from cfdinvest.regimes.volatility import classify_volatility

__all__ = [
    "RegimeConfig",
    "RegimeFrame",
    "classify_regimes",
    "attach_regimes",
    "classify_volatility",
    "classify_trend",
    "classify_correlation",
    "classify_hedge",
    "classify_macro",
    "classify_combined",
    "decision_table",
    "verify_totality",
    "COMBINED_REGIMES",
    "COMBINED_LABELS",
    "UNKNOWN",
]
