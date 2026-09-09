"""Many issues, one verdict.

Nine checks produce a list. An operator glancing at a phone needs a colour,
and an operator opening the page needs the list grouped so the same problem
reported by three checks reads as one problem.

Pure: mappings in, mappings out.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

import pandas as pd

from cfdinvest.monitoring.core import SEVERITIES, TYPES, MonitoringIssue

__all__ = [
    "STATUSES",
    "MonitoringSnapshot",
    "aggregate_monitoring_issues",
    "summarise_monitoring_state",
    "issues_frame",
]

#: Worst first.
STATUSES: tuple[str, ...] = ("red", "yellow", "green")

#: What each type is called when it is shown to a person.
TYPE_LABELS: dict[str, str] = {
    # ``livequotes`` was in TYPES and missing here, so a live-quote issue
    # rendered to a person as the bare token "livequotes". Found by the test
    # that asserts every type has a label, written for a different type
    # being added -- which is the argument for that test existing.
    "livequotes": "Live quotes",
    "lake": "Data lake",
    "scheduler": "Scheduler",
    "disk": "Disk space",
    "run": "The last run",
    "confirm": "Confirmations",
    "email": "Notifications",
    "pipeline": "Pipeline output",
    "system": "The machine",
    "credentials": "Broker credentials",
    # Not "is the daemon running" -- "is the account being looked at". The
    # two came apart on 2026-09-09 for 45 minutes of an open session while
    # every other label here reported fine.
    "dealing": "Dealing liveness",
    # The only entry here that is not a fault. It says the book is worth
    # looking at while everything is working exactly as built, so it reads
    # as a question rather than an alarm.
    "review": "Strategy review",
}


def aggregate_monitoring_issues(
    issues: Iterable[MonitoringIssue] | None,
) -> dict[str, Any]:
    """Counts, grouping and an overall colour.

    ``red`` when anything is critical, ``yellow`` when anything is a warning,
    ``green`` otherwise. Informational issues do not move the colour: a note
    that psutil is not installed is worth reading and is not worth an alert,
    and a status that went yellow for one would stop meaning anything.
    """
    found = sorted(
        list(issues or []), key=lambda issue: (issue.rank, issue.type, issue.message)
    )

    counts = {name: 0 for name in SEVERITIES}
    for issue in found:
        counts[issue.severity] += 1

    grouped: dict[str, list[MonitoringIssue]] = {}
    for issue in found:
        grouped.setdefault(issue.type, []).append(issue)

    if counts["critical"]:
        status = "red"
    elif counts["warning"]:
        status = "yellow"
    else:
        status = "green"

    return {
        "overall_status": status,
        "critical_count": counts["critical"],
        "warning_count": counts["warning"],
        "info_count": counts["info"],
        "total": len(found),
        "issues": found,
        "grouped": grouped,
        "types_affected": tuple(
            name for name in TYPES if name in grouped
        ),
        "clean_types": tuple(name for name in TYPES if name not in grouped),
    }


def summarise_monitoring_state(
    issues: Iterable[MonitoringIssue] | None,
    *,
    now: pd.Timestamp | None = None,
) -> dict[str, Any]:
    """The aggregate, plus a sentence to put in a subject line.

    The headline names the *worst* thing rather than counting everything,
    because a subject line reading "7 issues" tells you nothing about whether
    to get out of bed.
    """
    aggregate = aggregate_monitoring_issues(issues)
    found = aggregate["issues"]

    if not found:
        headline = "Everything checked is clear."
    else:
        worst = found[0]
        others = len(found) - 1
        headline = worst.message
        if others:
            headline += f" (and {others} other{'s' if others > 1 else ''})"

    return {
        **aggregate,
        "headline": headline,
        "timestamp": pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="UTC"),
    }


@dataclass(frozen=True, slots=True)
class MonitoringSnapshot:
    """One pass of the checks, and what it found."""

    timestamp: pd.Timestamp
    overall_status: str = "green"
    issues: tuple[MonitoringIssue, ...] = ()
    system_resources: dict[str, Any] = field(default_factory=dict)
    headline: str = ""
    critical_count: int = 0
    warning_count: int = 0
    info_count: int = 0
    run_id: str | None = None

    @property
    def ok(self) -> bool:
        """No critical issue. Not the same as nothing to look at."""
        return self.overall_status != "red"

    def of_type(self, kind: str) -> tuple[MonitoringIssue, ...]:
        return tuple(issue for issue in self.issues if issue.type == kind)

    def to_document(self) -> dict[str, Any]:
        from cfdinvest.persist.writers import jsonable

        return {
            "timestamp": self.timestamp.isoformat(),
            "overall_status": self.overall_status,
            "headline": self.headline,
            "critical_count": self.critical_count,
            "warning_count": self.warning_count,
            "info_count": self.info_count,
            "run_id": self.run_id,
            "issues": [issue.to_document() for issue in self.issues],
            "system_resources": jsonable(self.system_resources),
        }

    @classmethod
    def from_summary(
        cls,
        summary: dict[str, Any],
        *,
        system_resources: dict[str, Any] | None = None,
        run_id: str | None = None,
    ) -> "MonitoringSnapshot":
        return cls(
            timestamp=summary["timestamp"],
            overall_status=summary["overall_status"],
            issues=tuple(summary["issues"]),
            system_resources=dict(system_resources or {}),
            headline=summary["headline"],
            critical_count=summary["critical_count"],
            warning_count=summary["warning_count"],
            info_count=summary["info_count"],
            run_id=run_id,
        )


def issues_frame(issues: Sequence[MonitoringIssue] | None) -> pd.DataFrame:
    """The issues as a table, for the dashboard."""
    rows = [
        {
            "severity": issue.severity,
            "type": TYPE_LABELS.get(issue.type, issue.type),
            "message": issue.message,
            "remedy": issue.remedy,
            "timestamp": issue.timestamp,
        }
        for issue in (issues or [])
    ]
    if not rows:
        return pd.DataFrame(
            columns=["severity", "type", "message", "remedy", "timestamp"]
        )
    return pd.DataFrame(rows)
