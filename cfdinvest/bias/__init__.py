"""Patterns in how you have traded — described, not diagnosed.

::

    from cfdinvest.bias import run_bias_detection

    report = run_bias_detection(config)
    print(report.headline)
    profile = report.to_profile()      # what the refinements read

Also a page in the dashboard: **Behaviour**.

**This does not tell you what kind of person you are.** It reports observable
patterns in a trade record and leaves the interpretation where it belongs.
Every finding names the pattern rather than the person, offers the innocent
reading alongside the worrying one, and proposes a change to a process rather
than to a temperament.

A severity produced here shrinks a real position — the refinement layer cuts
size when one passes 0.5 — which is why the sample floor is higher than
anywhere else in this project, and why an unmeasured pattern stays absent
rather than becoming a zero.
"""

from __future__ import annotations

from cfdinvest.bias.controller import (
    BiasReport,
    build_bias_report,
    run_bias_detection,
)
from cfdinvest.bias.model import (
    ASSERT_THRESHOLD,
    BIASES,
    CONFIDENT_OBSERVATIONS,
    MINIMUM_OBSERVATIONS,
    BiasFinding,
    detect_fear_driven_underexposure,
    detect_late_exits,
    detect_leverage_creep,
    detect_loss_aversion,
    detect_margin_blindness,
    detect_overconfidence,
    detect_premature_exits,
    detect_revenge_trading,
    detect_risk_avoidance,
    model_detectors,
)

__all__ = [
    "BIASES",
    "MINIMUM_OBSERVATIONS",
    "CONFIDENT_OBSERVATIONS",
    "ASSERT_THRESHOLD",
    "BiasFinding",
    "BiasReport",
    "build_bias_report",
    "run_bias_detection",
    "model_detectors",
    "detect_overconfidence",
    "detect_revenge_trading",
    "detect_fear_driven_underexposure",
    "detect_risk_avoidance",
    "detect_loss_aversion",
    "detect_premature_exits",
    "detect_late_exits",
    "detect_leverage_creep",
    "detect_margin_blindness",
]
