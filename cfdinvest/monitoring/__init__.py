"""Operational health: is the machine alright, on a box nobody is watching?

**The failure this exists to catch is a system that looks fine.** A stale lake
still serves prices. A dead scheduler still leaves yesterday's run sitting
there. Every page renders perfectly against data that is quietly wrong.

::

    cfdinvest-schedule monitor
    cfdinvest-schedule monitor --watch --interval-minutes 30

```python
from cfdinvest.monitoring import run_monitoring

snapshot = run_monitoring(config)
print(snapshot.overall_status, snapshot.headline)
```

Also a page in the dashboard: **Operational Monitoring**.

Nothing here drives the pipeline or places a trade. It reads artefacts, reads
the machine, and writes one snapshot.
"""

from __future__ import annotations

from cfdinvest.monitoring.aggregate import (
    STATUSES,
    MonitoringSnapshot,
    aggregate_monitoring_issues,
    issues_frame,
    summarise_monitoring_state,
)
from cfdinvest.monitoring.collect import collect_issues, run_monitoring
from cfdinvest.monitoring.core import (
    SEVERITIES,
    TYPES,
    MonitoringIssue,
    MonitoringThresholds,
    check_confirmation_mismatches,
    check_disk_space,
    check_email_delivery,
    check_lake_freshness,
    check_live_quote_freshness,
    check_missing_frames,
    check_pipeline_anomalies,
    check_run_failures,
    check_scheduler_health,
    check_system_resources,
)
from cfdinvest.monitoring.resources import read_disk_usage, read_system_resources

__all__ = [
    "TYPES",
    "SEVERITIES",
    "STATUSES",
    "MonitoringThresholds",
    "MonitoringIssue",
    "MonitoringSnapshot",
    "check_lake_freshness",
    "check_live_quote_freshness",
    "check_scheduler_health",
    "check_disk_space",
    "check_run_failures",
    "check_missing_frames",
    "check_confirmation_mismatches",
    "check_email_delivery",
    "check_pipeline_anomalies",
    "check_system_resources",
    "aggregate_monitoring_issues",
    "summarise_monitoring_state",
    "issues_frame",
    "collect_issues",
    "run_monitoring",
    "read_disk_usage",
    "read_system_resources",
]
