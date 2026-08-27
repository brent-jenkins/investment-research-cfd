"""Is the machine healthy? Nine questions, asked of what is on disk.

**The failure this exists to catch is a system that looks fine.** A stale lake
still serves prices. A dead scheduler still leaves yesterday's run sitting
there. A full disk makes every other failure confusing, because the lake stops
updating and runs fail to write and nothing says why. None of these announce
themselves; all of them are silent until someone looks.

This is the headless half of that. :mod:`cfdinvest.ui.health` asks "is what
this page showing you trustworthy?" of data the dashboard has already loaded;
this module asks "is the box alright?" of what is on disk, after every run, on
a machine nobody is watching. The thresholds are shared — :class:`~cfdinvest.
ui.health.HealthThresholds` takes its defaults from
:class:`MonitoringThresholds` — so the banner and the email cannot disagree
about when "old" becomes "wrong".

Every function here is pure. The clock is injected, the data arrives as plain
mappings and frames, and a test can construct any combination of broken states
without a filesystem. :mod:`cfdinvest.monitoring.collect` does the reading.

Two rules shape every check
---------------------------
**A check that cannot run is not a passing check.** An unanswerable question
about safety is not a reassuring answer, so "could not read the scheduler
state" is an issue in its own right rather than an absence of issues.

**Every issue carries what to do about it.** "Disk space low" is not
actionable; "3.1 GB free of 40 GB (threshold 5.0) — prune old runs with
keep_runs, or resize the volume" is. The email alert has nowhere else to get
that from, and an alert that names a problem without a next step is one that
gets archived.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

__all__ = [
    "TYPES",
    "SEVERITIES",
    "MonitoringThresholds",
    "MonitoringIssue",
    "check_lake_freshness",
    "check_credential_expiry",
    "check_live_quote_freshness",
    "check_scheduler_health",
    "check_disk_space",
    "check_run_failures",
    "check_missing_frames",
    "check_confirmation_mismatches",
    "check_email_delivery",
    "check_pipeline_anomalies",
    "check_system_resources",
]

#: What an issue is about. Matches the spec's categories, plus ``livequotes``
#: for the intraday price poller, which post-dates them.
TYPES: tuple[str, ...] = (
    "livequotes",
    "lake", "scheduler", "disk", "run", "confirm", "email", "pipeline", "system",
    "credentials",
)

#: Ordered worst-first, which is also the display order.
SEVERITIES: tuple[str, ...] = ("critical", "warning", "info")

_RANK = {name: index for index, name in enumerate(SEVERITIES)}


@dataclass(frozen=True, slots=True)
class MonitoringThresholds:
    """When "old" becomes "wrong", and "low" becomes "about to break".

    One home for every number, because the dashboard banner and the unattended
    email alert both read them. Two copies would drift, and the day they drift
    is the day the banner says fine while the email says critical.

    Attributes
    ----------
    lake_stale_days:
        How far the newest data may fall behind now. Three days covers a
        normal weekend; four means a Friday run was missed.
    scheduler_stale_hours:
        How long since the last *completed* run before that is a failure.
        Forty-eight hours is two missed daily runs — one could be a holiday,
        two is a broken scheduler.
    missed_run_grace_minutes:
        How long a scheduled event may sit in the past before it counts as
        missed, allowing for a run that is simply still going.
    disk_free_gb, disk_free_fraction:
        Either bound tripping is enough. A percentage alone is useless on a
        small volume and an absolute alone is useless on a large one.
    disk_warning_multiple:
        How much headroom above the critical bound still warrants a warning.
        Being told at 1 GB free is being told too late to do anything gentle.
    publication_calendar_stale_days:
        Generous, because monthly series legitimately go weeks between prints.
    cpu_percent, memory_percent:
        Sustained load worth mentioning. Not critical on their own: a box at
        95% memory is doing its job right up until it is not.
    max_confirmation_drift:
        How far a confirmed position may sit from the simulated one before it
        is worth saying so, as a fraction. Rounding to what the ticket takes
        is normal; a tenth of the size is not.
    stale_confirmation_days:
        How long an open confirmed position may go without any newer
        confirmation before it is worth asking whether it is still open.
    """

    lake_stale_days: float = 3.0
    scheduler_stale_hours: float = 48.0
    missed_run_grace_minutes: float = 90.0
    disk_free_gb: float = 1.0
    disk_free_fraction: float = 0.05
    disk_warning_multiple: float = 5.0
    publication_calendar_stale_days: float = 45.0
    cpu_percent: float = 90.0
    memory_percent: float = 90.0
    max_confirmation_drift: float = 0.10
    stale_confirmation_days: float = 30.0


DEFAULTS = MonitoringThresholds()


@dataclass(frozen=True, slots=True)
class MonitoringIssue:
    """One thing that is wrong, what was found, and what to do.

    Attributes
    ----------
    details:
        The numbers, so a later reader can tell a near miss from a disaster
        without re-running the check.
    remedy:
        The next thing to do. Carried on the issue rather than composed by
        whatever displays it, so the email, the page and the log all say the
        same thing.
    """

    type: str
    severity: str
    message: str
    timestamp: pd.Timestamp | None = None
    details: dict[str, Any] = field(default_factory=dict)
    remedy: str = ""

    def __post_init__(self) -> None:
        if self.type not in TYPES:
            raise ValueError(f"type must be one of {TYPES}, got {self.type!r}")
        if self.severity not in SEVERITIES:
            raise ValueError(
                f"severity must be one of {SEVERITIES}, got {self.severity!r}"
            )

    @property
    def rank(self) -> int:
        """Worst first."""
        return _RANK[self.severity]

    def to_document(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "severity": self.severity,
            "message": self.message,
            "timestamp": (
                self.timestamp.isoformat() if self.timestamp is not None else None
            ),
            "details": _jsonable(self.details),
            "remedy": self.remedy,
        }


def _jsonable(value: Any) -> Any:
    from cfdinvest.persist.writers import jsonable

    return jsonable(value)


def _utc(value: Any) -> pd.Timestamp | None:
    if value in (None, ""):
        return None
    try:
        moment = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if moment is pd.NaT:
        return None
    return moment.tz_localize("UTC") if moment.tz is None else moment.tz_convert("UTC")


def _now(now: pd.Timestamp | None) -> pd.Timestamp:
    return _utc(now) or pd.Timestamp.now(tz="UTC")


def _finite(value: Any) -> bool:
    try:
        return value is not None and np.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _unreadable(kind: str, what: str, why: str = "") -> MonitoringIssue:
    """The issue every check raises when it could not run.

    An unanswerable question about safety is not a reassuring answer.
    """
    return MonitoringIssue(
        type=kind,
        severity="warning",
        message=f"{what} could not be checked{f': {why}' if why else '.'}",
        details={"reason": why},
        remedy=(
            "This is not a clean bill of health — it is the absence of one. "
            "Check the file exists and is readable."
        ),
    )


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #
#: How long before a broker credential expires the system starts saying so.
#:
#: Seven days, because a key is replaced by a person logging into a web
#: interface with two-factor authentication, and that is not a thing that
#: happens at 03:00 on the morning it stops working.
KEY_EXPIRY_WARNING_DAYS = 7


def check_credential_expiry(
    expires_on: str | None,
    *,
    label: str = "Capital.com API key",
    warn_days: int = KEY_EXPIRY_WARNING_DAYS,
    now: pd.Timestamp | None = None,
) -> list[MonitoringIssue]:
    """Is a broker credential close to expiring, or already gone?

    A key that expires is not a failure until it is, and then it is a total
    one: every order refused, every reading absent, and the symptom is an
    authentication error indistinguishable from a wrong password. The
    difference between a week's notice and none is the difference between a
    calendar entry and an outage.

    **An unset date is reported as unchecked, not as fine.** Nobody has to
    record an expiry, but a system silent about a credential whose lifetime
    it does not know would be making the strongest claim from the weakest
    evidence, which is the rule everything here follows.

    Critical once expired, a warning inside the window, and silent otherwise
    -- a check that speaks every day for eleven months is one nobody reads in
    the twelfth.
    """
    moment = _now(now)

    if not str(expires_on or "").strip():
        return [
            _unreadable(
                "credentials",
                f"{label} expiry",
                "no expiry date is recorded, so its remaining life is unknown",
            )
        ]

    try:
        expiry = pd.Timestamp(str(expires_on).strip())
    except (TypeError, ValueError):
        return [
            _unreadable(
                "credentials", f"{label} expiry", f"{expires_on!r} is not a date"
            )
        ]
    if expiry.tz is None:
        expiry = expiry.tz_localize("UTC")

    days = (expiry - moment).total_seconds() / 86_400.0

    if days <= 0:
        return [
            MonitoringIssue(
                type="credentials",
                severity="critical",
                message=(
                    f"{label} expired on {expiry:%Y-%m-%d}. Nothing can "
                    "authenticate until it is replaced."
                ),
                details={"expires_on": str(expiry), "days_remaining": days},
                remedy=(
                    "Generate a new key in the broker's settings and update the "
                    "environment file. Dealing and market data both stop until "
                    "it is done."
                ),
            )
        ]
    if days <= warn_days:
        return [
            MonitoringIssue(
                type="credentials",
                severity="warning",
                message=(
                    f"{label} expires in {days:.0f} day(s), on {expiry:%Y-%m-%d}."
                ),
                details={"expires_on": str(expiry), "days_remaining": days},
                remedy=(
                    "Generate a replacement before it lapses; it needs a person, "
                    "two-factor authentication and a web interface, none of which "
                    "are available at 03:00."
                ),
            )
        ]
    return []


# --------------------------------------------------------------------------- #
# Lake
# --------------------------------------------------------------------------- #
def check_live_quote_freshness(
    quote_document: Mapping[str, Any] | None,
    *,
    now: pd.Timestamp | None = None,
) -> list[MonitoringIssue]:
    """Are the live quotes recent enough for the helper to prefer them?

    A **warning**, never critical. Stale quotes are not a broken system: the
    live helper and the risk snapshot both fall back to the daily bar on
    their own, which is what they did before quotes existed. What is worth
    saying is that the fallback is happening, because from the outside a
    quiet poller and a quiet market look identical.

    Absence of the file is not reported here. A deployment that has never
    polled has not failed at anything, and warning about a feature nobody
    switched on trains you to ignore the banner.
    """
    from cfdinvest.livequotes.storage import DEFAULT_MAX_AGE_MINUTES, age_minutes

    moment = _now(now)
    quotes = (quote_document or {}).get("quotes") or {}
    if not quotes:
        return []

    age = age_minutes(quote_document, moment)
    if age is None:
        return [
            _unreadable(
                "livequotes",
                "Live quote freshness",
                "the quote file does not say when it was taken",
            )
        ]
    if age <= DEFAULT_MAX_AGE_MINUTES:
        return []

    return [
        MonitoringIssue(
            type="livequotes",
            severity="warning",
            message=(
                f"Live quotes are {age:.0f} minutes old "
                f"(threshold {DEFAULT_MAX_AGE_MINUTES:.0f}); the daily bar "
                "is being used instead."
            ),
            timestamp=moment,
            details={
                "age_minutes": age,
                "threshold_minutes": DEFAULT_MAX_AGE_MINUTES,
                "instruments": sorted(str(k) for k in quotes),
            },
            remedy=(
                "Check the scheduler daemon is running "
                "(`systemctl status cfdinvest`). Prices in the helper are "
                "yesterday's close until it is."
            ),
        )
    ]


def check_lake_freshness(
    lake_metadata: Mapping[str, Any] | None,
    thresholds: MonitoringThresholds | None = None,
    *,
    now: pd.Timestamp | None = None,
) -> list[MonitoringIssue]:
    """Is the newest data in the lake recent enough to trade on?

    ``lake_metadata`` needs a ``newest`` timestamp; ``rows`` and ``tickers``
    are used in the detail when present.
    """
    limits = thresholds or DEFAULTS
    moment = _now(now)

    if not lake_metadata:
        return [_unreadable("lake", "Lake freshness", "no lake metadata")]

    newest = _utc(lake_metadata.get("newest") or lake_metadata.get("newest_bar"))
    if newest is None:
        return [_unreadable("lake", "Lake freshness", "no newest bar recorded")]

    age_days = (moment - newest).total_seconds() / 86_400.0
    if age_days <= limits.lake_stale_days:
        return []

    return [
        MonitoringIssue(
            type="lake",
            severity="critical",
            message=(
                f"The newest data in the lake is {age_days:.1f} days old "
                f"(threshold {limits.lake_stale_days:.0f})."
            ),
            timestamp=moment,
            details={
                "newest": newest,
                "age_days": age_days,
                "threshold_days": limits.lake_stale_days,
                "tickers": lake_metadata.get("tickers"),
            },
            remedy=(
                "Every signal is being computed on stale prices. Refresh the "
                "lake (`cfdinvest-schedule run-once --refresh`) and check the "
                "vendor credentials."
            ),
        )
    ]


# --------------------------------------------------------------------------- #
# Scheduler
# --------------------------------------------------------------------------- #
def check_scheduler_health(
    scheduler_state: Mapping[str, Any] | None,
    thresholds: MonitoringThresholds | None = None,
    *,
    now: pd.Timestamp | None = None,
) -> list[MonitoringIssue]:
    """Did the last run finish, and was it recent?

    Delegates the classification to
    :func:`cfdinvest.risk.scheduler_status.compute_scheduler_status` rather
    than re-deriving it, so the risk dashboard and the monitoring alert cannot
    disagree about whether a run died.
    """
    limits = thresholds or DEFAULTS
    moment = _now(now)

    if scheduler_state is None:
        return [_unreadable("scheduler", "Scheduler health", "no state file")]

    from cfdinvest.risk.scheduler_status import compute_scheduler_status

    status = compute_scheduler_status(
        scheduler_state, stale_after_hours=limits.scheduler_stale_hours, now=moment
    )
    issues: list[MonitoringIssue] = []

    if status["run_status"] == "UNKNOWN":
        issues.append(
            MonitoringIssue(
                type="scheduler",
                severity="warning",
                message="No pipeline run has ever been recorded.",
                timestamp=moment,
                details={"state": dict(scheduler_state)},
                remedy=(
                    "Expected on a fresh install. If the deployment is not "
                    "fresh, the scheduler is not reaching this machine."
                ),
            )
        )
    elif status["incomplete"]:
        issues.append(
            MonitoringIssue(
                type="scheduler",
                severity="critical",
                message=(
                    "The last run started and never completed. Everything on "
                    "the dashboard is from an earlier run."
                ),
                timestamp=moment,
                details={
                    "last_run_started_at": status["last_run_started_at"],
                    "last_run_completed_at": status["last_run_completed_at"],
                },
                remedy=(
                    "Check the scheduler log for a crash or an out-of-memory "
                    "kill. Nothing on the dashboard is as recent as it looks."
                ),
            )
        )
    elif status["last_error"]:
        issues.append(
            MonitoringIssue(
                type="scheduler",
                severity="critical",
                message=f"The last run recorded an error: {status['last_error']}",
                timestamp=moment,
                details={"last_error_at": status["last_error_at"]},
                remedy="Check the scheduler log for the traceback.",
            )
        )
    elif status["stale"]:
        issues.append(
            MonitoringIssue(
                type="scheduler",
                severity="critical",
                message=(
                    f"The last successful run was {status['age_hours']:.0f} "
                    f"hours ago (threshold "
                    f"{limits.scheduler_stale_hours:.0f})."
                ),
                timestamp=moment,
                details={"age_hours": status["age_hours"]},
                remedy=(
                    "The schedule has stopped firing. Check the cron entry or "
                    "the service unit on the host."
                ),
            )
        )

    if status.get("failures") and status["run_status"] == "OK":
        issues.append(
            MonitoringIssue(
                type="scheduler",
                severity="info",
                message=(
                    f"{status['failures']} run(s) have failed since this state "
                    "file was created."
                ),
                timestamp=moment,
                details={"failures": status["failures"], "runs": status["runs"]},
                remedy="The most recent run succeeded. Worth a look if it grows.",
            )
        )
    return issues


# --------------------------------------------------------------------------- #
# Disk
# --------------------------------------------------------------------------- #
def check_disk_space(
    usage: Mapping[str, Any] | None,
    thresholds: MonitoringThresholds | None = None,
    *,
    now: pd.Timestamp | None = None,
) -> list[MonitoringIssue]:
    """Is there room to write the next run?

    ``usage`` is ``{"path", "free_gb", "total_gb"}`` — see
    :func:`cfdinvest.monitoring.resources.read_disk_usage`. Taking the numbers
    rather than a path keeps this pure and lets a test describe a full disk
    without having one.
    """
    limits = thresholds or DEFAULTS
    moment = _now(now)

    if not usage:
        return [_unreadable("disk", "Disk space", "no reading")]

    free = usage.get("free_gb")
    total = usage.get("total_gb")
    if not _finite(free):
        return [_unreadable("disk", "Disk space", "free space unreadable")]

    free = float(free)
    fraction = float(free) / float(total) if _finite(total) and float(total) > 0 else None
    details = {
        "path": usage.get("path"),
        "free_gb": free,
        "total_gb": total,
        "free_fraction": fraction,
        "threshold_gb": limits.disk_free_gb,
        "threshold_fraction": limits.disk_free_fraction,
    }

    critical = free < limits.disk_free_gb or (
        fraction is not None and fraction < limits.disk_free_fraction
    )
    if critical:
        return [
            MonitoringIssue(
                type="disk",
                severity="critical",
                message=(
                    f"{free:,.1f} GB free"
                    + (f" ({fraction:.1%})" if fraction is not None else "")
                    + f" — below the {limits.disk_free_gb:,.1f} GB / "
                    f"{limits.disk_free_fraction:.0%} floor."
                ),
                timestamp=moment,
                details=details,
                remedy=(
                    "A full disk makes every other failure confusing: the lake "
                    "stops updating and runs fail to write, with nothing "
                    "saying why. Lower `keep_runs` to prune old run "
                    "directories, or resize the volume."
                ),
            )
        ]

    warning_at = limits.disk_free_gb * limits.disk_warning_multiple
    if free < warning_at:
        return [
            MonitoringIssue(
                type="disk",
                severity="warning",
                message=(
                    f"{free:,.1f} GB free — heading for the "
                    f"{limits.disk_free_gb:,.1f} GB floor."
                ),
                timestamp=moment,
                details=details,
                remedy=(
                    "Still fine. Being told at the floor is being told too "
                    "late to do anything gentle about it."
                ),
            )
        ]
    return []


# --------------------------------------------------------------------------- #
# Runs
# --------------------------------------------------------------------------- #
def check_run_failures(
    run_metadata: Mapping[str, Any] | None,
    *,
    now: pd.Timestamp | None = None,
) -> list[MonitoringIssue]:
    """Did the run that just finished actually work?"""
    moment = _now(now)
    if not run_metadata:
        return [_unreadable("run", "The last run", "no run metadata")]

    status = str(run_metadata.get("status", "")).lower()
    run_id = run_metadata.get("run_id")

    if status == "failed":
        return [
            MonitoringIssue(
                type="run",
                severity="critical",
                message=f"Run {run_id} failed: {run_metadata.get('error')}",
                timestamp=moment,
                details=dict(run_metadata),
                remedy="Check the scheduler log for the traceback.",
            )
        ]
    if status == "empty":
        return [
            MonitoringIssue(
                type="run",
                severity="warning",
                message=f"Run {run_id} produced nothing to trade.",
                timestamp=moment,
                details={"run_id": run_id, "bars": run_metadata.get("bars")},
                remedy=(
                    "Normal when no signal fired. Repeated across many runs, "
                    "it usually means the universe or the filters are wrong."
                ),
            )
        ]

    warnings = list(run_metadata.get("warnings") or ())
    if warnings:
        return [
            MonitoringIssue(
                type="run",
                severity="info",
                message=f"Run {run_id} recorded {len(warnings)} warning(s).",
                timestamp=moment,
                details={"warnings": warnings},
                remedy="; ".join(str(w) for w in warnings[:3]),
            )
        ]
    return []


def check_missing_frames(
    present: Mapping[str, Any] | Iterable[str] | None,
    *,
    required: Sequence[str] = (
        "trades", "positions", "equity", "summary", "metadata",
    ),
    optional: Sequence[str] = ("pnl", "analysis", "regimes", "attribution"),
    now: pd.Timestamp | None = None,
) -> list[MonitoringIssue]:
    """Did the run write everything it was supposed to?

    A run directory that exists is not a run that finished. Half a run looks
    exactly like a whole one from the outside, and the dashboard will render
    it without complaint.
    """
    moment = _now(now)
    if present is None:
        return [_unreadable("pipeline", "Run artefacts", "no run directory")]

    names = set(present.keys() if isinstance(present, Mapping) else present)
    issues: list[MonitoringIssue] = []

    absent = [name for name in required if name not in names]
    if absent:
        issues.append(
            MonitoringIssue(
                type="pipeline",
                severity="critical",
                message=f"The run is missing {', '.join(absent)}.",
                timestamp=moment,
                details={"missing": absent, "present": sorted(names)},
                remedy=(
                    "Half a run looks like a whole one from the outside. The "
                    "run probably died between writing artefacts — check the "
                    "log and disk space."
                ),
            )
        )

    missing_optional = [name for name in optional if name not in names]
    if missing_optional:
        issues.append(
            MonitoringIssue(
                type="pipeline",
                severity="info",
                message=(
                    f"The run did not write {', '.join(missing_optional)}."
                ),
                timestamp=moment,
                details={"missing": missing_optional},
                remedy=(
                    "Analytics that need these will report them absent rather "
                    "than reconstruct them."
                ),
            )
        )
    return issues


# --------------------------------------------------------------------------- #
# Confirmations
# --------------------------------------------------------------------------- #
def check_confirmation_mismatches(
    simulated_positions: pd.DataFrame | None,
    confirmed_positions: Mapping[str, Mapping[str, Any]] | None,
    thresholds: MonitoringThresholds | None = None,
    *,
    now: pd.Timestamp | None = None,
) -> list[MonitoringIssue]:
    """Where does what you hold differ from what the system thinks?

    **Nothing confirmed is not a mismatch.** Silence leaves the simulated
    positions standing, which is correct; reporting it as a problem would make
    every unattended week look broken.
    """
    limits = thresholds or DEFAULTS
    moment = _now(now)

    if not confirmed_positions:
        return []
    if simulated_positions is None or getattr(simulated_positions, "empty", True):
        return [
            _unreadable(
                "confirm", "Confirmation drift", "no simulated positions to compare"
            )
        ]

    frame = simulated_positions.copy()
    if "timestamp" in frame.columns:
        frame = frame.set_index("timestamp")
    latest = frame.iloc[-1]

    drifted: dict[str, dict[str, float]] = {}
    unknown: list[str] = []
    stale: list[str] = []

    for name, entry in confirmed_positions.items():
        confirmed = float(entry.get("quantity", 0.0))
        at = _utc(entry.get("at"))
        if at is not None and confirmed != 0:
            age = (moment - at).total_seconds() / 86_400.0
            if age > limits.stale_confirmation_days:
                stale.append(name)

        if name not in latest.index:
            if confirmed != 0:
                unknown.append(name)
            continue

        raw = latest[name]
        simulated = float(raw) if _finite(raw) else 0.0
        reference = max(abs(simulated), abs(confirmed))
        if reference == 0:
            continue
        drift = abs(simulated - confirmed) / reference
        if drift > limits.max_confirmation_drift:
            drifted[name] = {
                "simulated": simulated,
                "confirmed": confirmed,
                "drift": drift,
            }

    issues: list[MonitoringIssue] = []
    if drifted:
        issues.append(
            MonitoringIssue(
                type="confirm",
                severity="warning",
                message=(
                    f"{len(drifted)} position(s) differ from the simulation by "
                    f"more than {limits.max_confirmation_drift:.0%}: "
                    f"{', '.join(sorted(drifted))}."
                ),
                timestamp=moment,
                details={"drifted": drifted},
                remedy=(
                    "Expected if you sized differently on purpose. If not, "
                    "one of the two is wrong — and the analytics are being "
                    "computed on the confirmed side."
                ),
            )
        )
    if unknown:
        issues.append(
            MonitoringIssue(
                type="confirm",
                severity="info",
                message=(
                    f"You hold {', '.join(sorted(unknown))}, which the system "
                    "does not trade."
                ),
                timestamp=moment,
                details={"instruments": sorted(unknown)},
                remedy=(
                    "Perfectly allowed. These carry risk the engine is not "
                    "modelling, so nothing here will warn you about them."
                ),
            )
        )
    if stale:
        issues.append(
            MonitoringIssue(
                type="confirm",
                severity="warning",
                message=(
                    f"{', '.join(sorted(stale))} has been open since a "
                    f"confirmation more than "
                    f"{limits.stale_confirmation_days:.0f} days old."
                ),
                timestamp=moment,
                details={"instruments": sorted(stale)},
                remedy=(
                    "Either it is still open and the risk figures are right, "
                    "or you closed it and never said so — in which case every "
                    "page is showing a position you do not have."
                ),
            )
        )
    return issues


# --------------------------------------------------------------------------- #
# Email
# --------------------------------------------------------------------------- #
def check_email_delivery(
    notify_state: Mapping[str, Any] | None,
    *,
    now: pd.Timestamp | None = None,
) -> list[MonitoringIssue]:
    """Did the alerts actually go?

    Mail failures are swallowed by design — an unattended run that did its
    work must not be reported as broken because a mail server was unreachable.
    That is right, and it is exactly why the failure has to surface somewhere
    else. This is somewhere else.
    """
    moment = _now(now)
    if not notify_state:
        return []

    failed = int(notify_state.get("failed", 0) or 0)
    sent = int(notify_state.get("sent", 0) or 0)

    if not failed:
        # Reported only when nothing failed. A delivery failure is the more
        # important fact of the two, and returning early on "switched off"
        # would hide it.
        if not notify_state.get("enabled", True):
            return [
                MonitoringIssue(
                    type="email",
                    severity="info",
                    message="Email notifications are switched off.",
                    timestamp=moment,
                    details={},
                    remedy="Nothing will reach you when a run fails.",
                )
            ]
        return []

    return [
        MonitoringIssue(
            type="email",
            severity="critical" if not sent else "warning",
            message=(
                f"{failed} notification(s) failed to send"
                + (f", {sent} succeeded" if sent else " and none succeeded")
                + "."
            ),
            timestamp=moment,
            details={
                "failed": failed,
                "sent": sent,
                "last_error": notify_state.get("last_error"),
                "last_failed_subject": notify_state.get("last_failed_subject"),
            },
            remedy=(
                "Check SMTP_PASSWORD and the relay host. While this is "
                "failing, a failed run will not reach you — which is the "
                "state where you most need it to."
            ),
        )
    ]


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #
def check_pipeline_anomalies(
    run_metadata: Mapping[str, Any] | None,
    execution_metadata: Mapping[str, Any] | None,
    thresholds: MonitoringThresholds | None = None,
    *,
    now: pd.Timestamp | None = None,
) -> list[MonitoringIssue]:
    """Did the run produce something impossible, or merely surprising?

    Two different things, kept apart. An accounting identity that does not
    hold is a bug; a big drawdown is a market. Only the first is critical.
    """
    moment = _now(now)
    if not execution_metadata:
        return [_unreadable("pipeline", "Pipeline output", "no execution metadata")]

    issues: list[MonitoringIssue] = []
    initial = execution_metadata.get("initial_equity")
    final = execution_metadata.get("final_equity")

    if _finite(initial) and _finite(final):
        if float(final) <= 0:
            issues.append(
                MonitoringIssue(
                    type="pipeline",
                    severity="critical",
                    message=f"The run ended with equity of {float(final):,.2f}.",
                    timestamp=moment,
                    details={"initial_equity": initial, "final_equity": final},
                    remedy=(
                        "A simulated account cannot go to zero on these "
                        "position sizes. Suspect the sizing or the FX rate "
                        "before believing the result."
                    ),
                )
            )
    elif not _finite(final):
        issues.append(
            MonitoringIssue(
                type="pipeline",
                severity="critical",
                message="The run's final equity is not a number.",
                timestamp=moment,
                details={"final_equity": final},
                remedy=(
                    "A NaN in the equity curve makes every metric NaN. Check "
                    "for a missing price or a zero exchange rate."
                ),
            )
        )

    # Explicitly not `a or b`: a run over zero bars is the case this check
    # exists for, and zero is falsy, so the short-circuit would skip straight
    # past it to the other source and find nothing.
    bars = (run_metadata or {}).get("bars")
    if bars is None:
        bars = execution_metadata.get("bars")
    if _finite(bars) and int(bars) == 0:
        issues.append(
            MonitoringIssue(
                type="pipeline",
                severity="critical",
                message="The run processed no bars.",
                timestamp=moment,
                details={"bars": bars},
                remedy=(
                    "The universe resolved to nothing, or the lake had no "
                    "data in the requested window."
                ),
            )
        )

    drawdown = execution_metadata.get("max_drawdown_fraction")
    if _finite(drawdown) and abs(float(drawdown)) >= 0.99:
        issues.append(
            MonitoringIssue(
                type="pipeline",
                severity="critical",
                message=(
                    f"The run drew down {abs(float(drawdown)):.0%} — the "
                    "account was effectively wiped out."
                ),
                timestamp=moment,
                details={"max_drawdown_fraction": drawdown},
                remedy=(
                    "At this magnitude it is a bug rather than a market. "
                    "Check leverage and the stop distance."
                ),
            )
        )

    if execution_metadata.get("fx_enabled") and not execution_metadata.get(
        "fx_rate_source"
    ):
        issues.append(
            MonitoringIssue(
                type="pipeline",
                severity="warning",
                message="FX conversion ran without recording a rate source.",
                timestamp=moment,
                details={"fx_enabled": True},
                remedy=(
                    "The converted figures cannot be reproduced without "
                    "knowing which rates were used."
                ),
            )
        )
    return issues


# --------------------------------------------------------------------------- #
# The machine
# --------------------------------------------------------------------------- #
def check_system_resources(
    cpu: float | None = None,
    memory: float | None = None,
    thresholds: MonitoringThresholds | None = None,
    *,
    now: pd.Timestamp | None = None,
) -> list[MonitoringIssue]:
    """Is the box under strain?

    Both readings are optional and an absent one is reported absent, never
    assumed healthy. Reading them needs ``psutil``, which is not a dependency
    of this project — an unattended box without it should say "not measured"
    rather than "fine".
    """
    limits = thresholds or DEFAULTS
    moment = _now(now)
    issues: list[MonitoringIssue] = []

    if not _finite(cpu) and not _finite(memory):
        return [
            MonitoringIssue(
                type="system",
                severity="info",
                message="CPU and memory were not measured.",
                timestamp=moment,
                details={},
                remedy=(
                    "Install psutil to have these checked. Absent is not the "
                    "same as healthy."
                ),
            )
        ]

    for name, value, limit in (
        ("CPU", cpu, limits.cpu_percent),
        ("Memory", memory, limits.memory_percent),
    ):
        if not _finite(value):
            continue
        if float(value) >= limit:
            issues.append(
                MonitoringIssue(
                    type="system",
                    severity="warning",
                    message=f"{name} is at {float(value):.0f}% (threshold {limit:.0f}%).",
                    timestamp=moment,
                    details={name.lower(): float(value), "threshold": limit},
                    remedy=(
                        "Not a fault on its own — a box at 95% memory is doing "
                        "its job right up until it is not. Worth watching if "
                        "it persists across runs."
                    ),
                )
            )
    return issues
