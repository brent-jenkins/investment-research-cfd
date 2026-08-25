"""The risk posture: how hard to push, and why.

::

    from cfdinvest.metastrategy import compute_meta_strategy

    profile = compute_meta_strategy(insights, bias, regime, pnl, "live")
    profile.posture            # "assertive"
    profile.contributions      # every input's signed effect

Also a page in the dashboard: **Meta-Strategy**.

**Cutting requires one reason. Raising requires all of them.** Any single
adverse finding is enough to hold back; moving forward needs a measurable
positive expectancy, no asserted behavioural pattern, no elevated regime and
a shallow drawdown, together.

A posture may *ask* for more size and can never create the room for it — the
refinement layer caps the result at what the safety engine already approved,
and reports the overrule. The two tolerances are clamped at 1.0, so a posture
can hold you further inside your own limits and never move one outward.
"""

from __future__ import annotations

from cfdinvest.metastrategy.engine import (
    FORWARD_REQUIREMENTS,
    compute_meta_strategy,
    posture_for,
)
from cfdinvest.metastrategy.profile import (
    MAX_SIZING_REQUEST,
    POSTURE_SETTINGS,
    POSTURES,
    MetaStrategyProfile,
    neutral_profile,
)

__all__ = [
    "POSTURES",
    "POSTURE_SETTINGS",
    "MAX_SIZING_REQUEST",
    "FORWARD_REQUIREMENTS",
    "MetaStrategyProfile",
    "neutral_profile",
    "compute_meta_strategy",
    "posture_for",
]
