"""What the record says, sliced — and how much of it is worth believing.

::

    from cfdinvest.insights import run_insights

    report = run_insights(config)
    for insight in report.usable:
        print(insight.title, "-", insight.finding)

Also a page in the dashboard: **Insights**.

**Every insight carries its sample size, and one below the threshold reports
no finding at all.** Slicing a small record makes it smaller: two hundred
trades across four regimes and eight instruments is thirty-two buckets of six,
and six trades say nothing. The figures stay available; what is withheld is
the claim.

Nothing here recomputes what the analytics layer owns. This groups trades and
asks the existing functions about each group.
"""

from __future__ import annotations

from cfdinvest.insights.controller import InsightReport, build_report, run_insights
from cfdinvest.insights.model import (
    CONFIDENT_SAMPLE,
    MINIMUM_SAMPLE,
    Bucket,
    Insight,
    execution_quality_trend,
    expectancy_trend,
    fx_sensitivity,
    instrument_performance,
    margin_behaviour_trend,
    model_insights,
    regime_performance,
    spread_sensitivity,
    volatility_sensitivity,
    win_loss_distribution,
)

__all__ = [
    "MINIMUM_SAMPLE",
    "CONFIDENT_SAMPLE",
    "Insight",
    "Bucket",
    "InsightReport",
    "build_report",
    "run_insights",
    "model_insights",
    "expectancy_trend",
    "win_loss_distribution",
    "regime_performance",
    "instrument_performance",
    "volatility_sensitivity",
    "spread_sensitivity",
    "fx_sensitivity",
    "execution_quality_trend",
    "margin_behaviour_trend",
]
