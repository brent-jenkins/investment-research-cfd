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


def trading_age_days(newest, now) -> float | None:
    """Staleness in trading days, or ``None`` when it cannot be computed.

    One owner for the *measurement*, beside the threshold it is compared
    against. ``lake_stale_days`` was changed from calendar days to trading
    days, and a threshold whose units move while its consumers keep their
    own arithmetic is two definitions of one number -- which is how the
    dashboard came to compare a calendar age against a trading limit and
    call a healthy lake critical.

    Falls back to calendar days if the market calendar cannot be read, and
    says so, because over-reporting staleness is the safe direction.
    """
    if newest is None or now is None:
        return None
    try:
        from cfdinvest.schedule.market_calendar import trading_days_between

        return float(trading_days_between(newest, now))
    except Exception as error:  # noqa: BLE001 - a calendar fault is not an outage
        logger.warning(
            "Could not measure trading-day staleness (%s); falling back to "
            "calendar days, which over-reports across a closed market", error,
        )
        try:
            return float((now - newest).total_seconds() / 86_400.0)
        except Exception:  # noqa: BLE001
            return None

__all__ = [
    "win_rate_check",
    "TYPES",
    "SEVERITIES",
    "MonitoringThresholds",
    "MonitoringIssue",
    "check_lake_freshness",
    "check_price_repairs",
    "check_credential_expiry",
    "check_live_quote_freshness",
    "check_scheduler_health",
    "check_disk_space",
    "check_deploy_is_current",
    "check_run_failures",
    "check_missing_frames",
    "check_confirmation_mismatches",
    "review_triggers",
    "check_email_delivery",
    "check_pipeline_anomalies",
    "check_system_resources",
]

#: What an issue is about. Matches the spec's categories, plus ``livequotes``
#: for the intraday price poller, which post-dates them.
TYPES: tuple[str, ...] = (
    "livequotes",
    "lake", "scheduler", "disk", "run", "confirm", "email", "pipeline", "system",
    # Whether the machine is running what the repository says. Added with the
    # deploy session guard on 2026-09-19: that guard defers a deploy while a
    # market is open, and a deferred deploy nobody can see is a box quietly
    # running last week's code.
    "deploy",
    "credentials",
    # The daemon being ALIVE and the account being LOOKED AT are different
    # facts, and until 2026-09-09 only the first was checked. A cgroup
    # memory throttle stalled the daemon for 45 minutes of an open session
    # while the unit stayed `active` and every existing type reported fine.
    "dealing",
    # Not an operational fault. Every other type here says something is
    # broken; this one says the book is worth a human look while everything
    # is working exactly as built. Kept separate so it can never be read as
    # an outage, and so nothing wires it to execution.
    "review",
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

    #: In TRADING days, not calendar days -- see ``_lake_issues``. The old
    #: value of 3.0 was in calendar days and breached every Monday.
    #:
    #: 2.5 rather than 1.0, and the reason is an artefact worth naming. The
    #: lake's newest datum is usually a *daily* bar, and daily bars are
    #: stamped at midnight -- so the bar for Friday carries 2026-08-28
    #: 00:00 while representing a session that ended at 20:00. Its whole
    #: session therefore counts as elapsed the moment it is written, and the
    #: lake reads exactly 1.00 trading days old in normal operation, rising
    #: to about 2.0 just before the next daily ingest. A threshold of 1.0
    #: would have fired every afternoon, which is the same weekly-alarm-clock
    #: failure it replaced, only louder.
    #:
    #: The sharper measure would be the staleness of the *decision feed*
    #: alone -- capital_1h, whose newest bar reads 0.15 trading days old at
    #: the same moment -- rather than the newest datum across market and
    #: macro series together. That is a change to what load_lake_metadata
    #: reports and is not made here; recorded so the next reader knows this
    #: value accommodates an artefact rather than measuring the thing
    #: directly.
    lake_stale_days: float = 2.5
    #: In trading hours. The intraday jobs run every fifteen minutes while a
    #: market is open, so 48 hours was simultaneously far too loose to catch
    #: a weekday failure and too tight to survive a weekend -- the worst of
    #: both. Four trading hours is roughly sixteen missed ticks.
    scheduler_stale_hours: float = 4.0
    missed_run_grace_minutes: float = 90.0
    #: How long the account may go unexamined DURING AN OPEN SESSION before
    #: it is a fault. Dealing passes run every 15 minutes, so 45 is three
    #: missed passes -- long enough that a slow pipeline or a restart does
    #: not cry wolf, short enough to catch a stall inside one session.
    #:
    #: Sized by the failure it exists to catch: on 2026-09-09 the daemon sat
    #: in cgroup memory throttling for 45 minutes of an open session. It was
    #: `active`, its process was alive, `Restart=always` never fired and the
    #: watchdog timer saw a healthy unit -- so every existing check reported
    #: fine while the account was not being looked at.
    dealing_stale_minutes: float = 45.0
    disk_free_gb: float = 1.0
    disk_free_fraction: float = 0.05
    disk_warning_multiple: float = 5.0
    publication_calendar_stale_days: float = 45.0
    cpu_percent: float = 90.0
    memory_percent: float = 90.0
    max_confirmation_drift: float = 0.10
    stale_confirmation_days: float = 30.0

    #: Peak-to-trough fall in the book that is worth a look. **A review
    #: trigger, never a circuit breaker**, and the distinction is load
    #: bearing rather than pedantic: four automatic pauses were tested
    #: causally on this strategy and every one lost money, because it earns
    #: most in exactly the conditions a fear-based rule would sit out. So
    #: this raises a warning for a person and stops nothing.
    #:
    #: What it is for is the drawdown that is worse than the model plausibly
    #: produces -- a statement about the model rather than about the market.
    #:
    #: **Was 0.45, against a stale -39.5%, and it was wrong for a reason
    #: worth keeping.** The threshold was set from the single backtest's
    #: worst drawdown plus a margin, and one backtest is one path. Block-
    #: bootstrapping the book's own bar returns (4,000 paths, the engine's
    #: configuration, 2026-08-31) shows the realised 41.4% was a MILD one:
    #:
    #:     block    p50     p75     p90     p95     p99   worse than 41.4%
    #:         1   49.0%   56.4%   63.9%   68.7%   77.8%        80.7%
    #:         5   47.3%   54.5%   61.4%   65.8%   75.1%        74.7%
    #:        20   45.4%   52.0%   59.3%   63.3%   72.9%        68.3%
    #:        50   43.2%   49.0%   55.4%   59.3%   66.9%        59.0%
    #:       100   41.9%   47.8%   54.1%   58.2%   66.6%        52.9%
    #:
    #: Between 53% and 81% of modelled paths drew down worse than the one
    #: the backtest happened to produce, so 0.45 sat between the median and
    #: the 75th percentile of the model's own range. It did not mean "worse
    #: than anything modelled"; it meant "about average", and it would have
    #: fired on an ordinary bad patch and taught the reader to ignore it.
    #:
    #: **Two questions, two thresholds.** Trying to answer both with one
    #: number is what made 0.45 incoherent. "The book has fallen a long way"
    #: is a fact about the account and halving qualifies whatever the model
    #: says; "this is worse than the model can produce" is a fact about the
    #: model. They call for different responses -- the first is a reason to
    #: look, the second a reason to doubt the strategy -- so they are
    #: separate readings of the same drawdown. See
    #: ``unmodelled_drawdown_fraction`` for the second.
    #:
    #: This one stays at 0.50: a book that has halved is worth a person's
    #: attention, and that is the owner's stated policy of running for
    #: growth and reviewing when things turn bad. It is NOT evidence the
    #: model is wrong -- roughly a quarter to two-fifths of modelled paths
    #: reach it -- and the message says so, because a warning that overstates
    #: itself is one nobody believes twice.
    #:
    #: The old 0.60 reasoning, kept because it belongs to the other
    #: threshold: 0.60 sits near the 90th to 95th percentile at blocks 20 to 50 --
    #: a week to a fortnight of hourly bars, which is the horizon this
    #: book's losing runs cluster over given a four-hour median hold. Roughly
    #: one modelled path in ten to twenty reaches it. That is deliberately
    #: not the 99th: this trigger stops nothing and only asks a person to
    #: look, so being seen occasionally in a genuinely bad stretch is the
    #: point, and a threshold nobody ever reaches is not a safeguard.
    #:
    #: Read as a sensitivity check on sequencing, not a forecast. Resampling
    #: assumes returns are exchangeable draws from a stable process and they
    #: are not -- ``backtest.montecarlo`` says so in its own docstring.
    #: Note also that here the i.i.d. shuffle produces WIDER drawdowns than
    #: the block bootstrap, the opposite of that module's stated intuition,
    #: because this book's realised sequence was favourable and scrambling
    #: it destroys the recovery ordering.
    review_drawdown_fraction: float = 0.50

    #: The drawdown that is worse than the model itself plausibly produces.
    #:
    #: Distinct from ``review_drawdown_fraction`` above in what it claims
    #: rather than merely in size. That one says the account has fallen a
    #: long way; this one says the fall is outside the range the strategy's
    #: own resampled paths reach, which is evidence about the model rather
    #: than about the market -- and therefore a reason to re-measure the
    #: strategy rather than to watch it more closely.
    #:
    #: 0.60 is near the 90th to 95th percentile of the modelled distribution
    #: at block sizes 20 to 50 (see the table above), so roughly one path in
    #: ten to twenty reaches it. Deliberately not the 99th: this stops
    #: nothing and only asks a person to look, and a threshold nobody ever
    #: reaches is not a safeguard.
    unmodelled_drawdown_fraction: float = 0.60

    #: The win rate the model says to expect, and the terms on which a
    #: forward reading is allowed to contradict it.
    #:
    #: **Pre-registered on 2026-09-03, before the data existed to test it.**
    #: The live account then had 7 completed trips of which 1 won, which
    #: looks alarming and is not: at the model's 43.1% that is a 12% outcome,
    #: and the standard error on a win rate at n=7 is 18.7pp. Setting the
    #: rule now, rather than the first time a red number appears, is the
    #: whole point -- a threshold chosen while looking at the result is not a
    #: test of it.
    #:
    #: The band widens with the sample rather than being a fixed number,
    #: because "below 30%" means something different at 10 trips and at 500.
    #: Two standard errors below the model rate, and silent until there are
    #: enough trips for the question to have an answer:
    #:
    #:     50 trips   speaks below 29%
    #:    100 trips   speaks below 33%
    #:    200 trips   speaks below 36%
    #:
    #: A warning, never a pause. Four automatic pauses were tested causally
    #: on this strategy and all four lost money.
    model_win_rate: float = 0.431
    #: Below this many completed trips the check says nothing at all --
    #: reporting "unmeasured" rather than a reassuring pass.
    win_rate_min_trips: int = 50
    #: How many standard errors below the model rate is worth a person's
    #: attention.
    win_rate_sigmas: float = 2.0

    #: Months of trailing return that may be negative before it is worth
    #: asking whether the strategy still fits the market. A trend follower
    #: in a sideways market is the known failure mode and it does not show
    #: up as a drawdown spike; it shows up as months of going nowhere.
    review_flat_months: float = 3.0


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

    # Measured in TRADING time, not calendar time. A Friday close is three
    # calendar days old by Monday breakfast every single week, so a
    # calendar threshold of three is not a warning about anything -- it is a
    # weekly alarm clock, and an alert that cries wolf on schedule is one
    # nobody reads. The vitals page has always known this; the email did
    # not, and the two disagreed about the same fact.
    calendar_days = (moment - newest).total_seconds() / 86_400.0
    age_days = trading_age_days(newest, moment)
    if age_days is None:
        age_days = calendar_days
    if age_days <= limits.lake_stale_days:
        return []

    return [
        MonitoringIssue(
            type="lake",
            severity="critical",
            message=(
                f"The newest data in the lake is {age_days:.1f} trading "
                f"days old (threshold {limits.lake_stale_days:.1f}); "
                f"{calendar_days:.1f} calendar days."
            ),
            timestamp=moment,
            details={
                "newest": newest,
                "age_days": age_days,
                "calendar_days": calendar_days,
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
                    f"hours ago, {status.get('trading_age_hours') or 0:.1f} "
                    f"of them with a market open (threshold "
                    f"{limits.scheduler_stale_hours:.0f} trading hours)."
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
def check_deploy_is_current(
    revisions: Mapping[str, Any] | None,
    thresholds: MonitoringThresholds | None = None,
    *,
    now: pd.Timestamp | None = None,
) -> list[MonitoringIssue]:
    """Is the box running what the repository says it should?

    ``revisions`` is ``{"head", "origin", "behind"}`` -- the deployed
    revision, what origin/main holds on disk, and how many commits separate
    them. Taking the numbers rather than shelling out to git keeps this pure
    and lets a test describe a stale box without having one.

    **This exists because of the guard that needs it.** The session guard in
    `deploy.sh` refuses to deploy while a market is open, and refuses AFTER
    fetching -- so a refused deploy leaves origin/main ahead of HEAD. Without
    something reading that, the guard would trade a deploy landing mid-session
    for a box silently running last week's code, which is the worse failure
    and the exact shape of the 2026-09-12 MemoryHigh finding: "the repository,
    the tests and the deploy all described one configuration and the machine
    ran another."

    It is a WARNING rather than critical. Being a commit behind is normal for
    the minutes between a push and the deploy, and on a weekday afternoon it
    is the guard working as designed. What it must never be is invisible.

    Absence is reported as unchecked rather than as current: a box whose
    revisions cannot be read is not thereby up to date.
    """
    moment = _now(now)

    if not revisions:
        return [_unreadable("deploy", "Deployed revision", "no reading")]

    head = str(revisions.get("head") or "")
    origin = str(revisions.get("origin") or "")
    if not head or not origin:
        return [_unreadable("deploy", "Deployed revision", "revision unreadable")]

    if head == origin:
        return []

    behind = revisions.get("behind")
    count = f"{int(behind)} commit(s)" if _finite(behind) else "an unknown number"
    return [
        MonitoringIssue(
            type="deploy",
            severity="warning",
            message=(
                f"The box is {count} behind the repository "
                f"(running {head[:7]}, origin/main is {origin[:7]})."
            ),
            timestamp=moment,
            details={"head": head, "origin": origin, "behind": behind},
            remedy=(
                "Expected for the minutes after a push, and expected all "
                "session if the deploy guard refused. Run "
                "deployment/deploy.sh after the close, or override the guard "
                "if what is running is placing wrong trades."
            ),
        )
    ]


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
                    f"The SIMULATED run drew down "
                    f"{abs(float(drawdown)):.0%}. This is the backtest, not "
                    "the account — check the broker balance before acting."
                ),
                timestamp=moment,
                details={"max_drawdown_fraction": drawdown},
                remedy=(
                    "At this magnitude it is usually a bug rather than a "
                    "market. Check leverage and the stop distance -- and "
                    "check what the run MODELS. The scheduled pipeline "
                    "does NOT apply the last look, which the account applies "
                    "on every open and which was worth 274 percentage "
                    "points when measured on 2026-09-08 -- so a large "
                    "simulated drawdown is the expected reading rather than "
                    "a signal. Nor does it apply the close-of-session "
                    "flatten -- briefly wired on 2026-09-08 and unwired the "
                    "next day, because the engine reads 'session end' from "
                    "the data and so flattened the live bar it was deciding "
                    "on. A run that does not model the dealing "
                    "path cannot tell you the account is in trouble; the "
                    "broker balance can."
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
def check_dealing_liveness(
    last_dealing_at: Any,
    market_open: bool | None,
    thresholds: MonitoringThresholds | None = None,
    *,
    now: pd.Timestamp | None = None,
    last_blocked: tuple[Any, str] | None = None,
) -> list[MonitoringIssue]:
    """Is the account actually being looked at, or merely supervised?

    ``last_blocked`` is the newest ``(at, reason)`` of a pass that RAN and
    could not read the account. When one is newer than the last complete
    pass and recent, the dealer is running and the broker is the problem,
    and the alert says so -- added 2026-10-02, after a zero balance from
    Capital was reported as a stalled daemon and blamed on memory.

    Every other check here asks whether a component is *present*. This one
    asks whether it is *working*, and the distinction is the whole reason it
    exists.

    On 2026-09-09 the daemon exceeded its cgroup ``MemoryHigh`` by one
    megabyte. The kernel's response to ``memory.high`` is to sleep the task
    on every allocation, proportional to the overage, so it was throttled
    39,932 times and stopped finishing anything. It never reached
    ``MemoryMax``, so it was never killed: the process stayed alive, the unit
    stayed ``active``, ``Restart=always`` never fired, ``cfdinvest.timer``
    saw a healthy service, and the monitoring snapshot reported fine. The
    account went 45 minutes of an open session without a single dealing pass
    and nothing anywhere said so.

    ``last_dealing_at`` is the newest margin-usage reading, which the dealing
    path writes on every pass -- so it records that the account was examined,
    not merely that a process was running. A pass that examines the book and
    correctly does nothing still writes one, which is what makes it a
    liveness signal rather than an activity signal.

    Silent when the market is closed, because not dealing is then correct.
    **An unknown calendar is reported rather than assumed either way**: "we
    could not tell whether the market was open" is a different statement from
    "the market was shut", and only one of them is reassuring.
    """
    limits = thresholds or DEFAULTS
    moment = _now(now)

    if market_open is None:
        return [_unreadable(
            "dealing", "Dealing liveness",
            "the market calendar could not be read, so a quiet daemon cannot "
            "be told from a shut exchange",
        )]
    if not market_open:
        return []

    stamp = _utc(last_dealing_at)
    if stamp is None:
        return [
            MonitoringIssue(
                type="dealing",
                # A WARNING, not critical, and the distinction is the same
                # one check_scheduler_health draws about a first run: never
                # having dealt is what a fresh install looks like, and a
                # check that cries critical on every new deployment is one
                # people learn to skip. Having dealt and then STOPPED is the
                # fault, and that is the branch below.
                severity="warning",
                message=(
                    "The market is open and no dealing pass has ever been "
                    "recorded."
                ),
                timestamp=moment,
                details={"last_dealing_at": None},
                remedy=(
                    "Expected on a fresh install, before the first dealing "
                    "pass. If the deployment is not fresh, the daemon is not "
                    "reaching the account -- check that cfdinvest.service is "
                    "running and that the broker credentials resolve."
                ),
            )
        ]

    age = (moment - stamp).total_seconds() / 60.0
    if age <= limits.dealing_stale_minutes:
        return []

    details = {
        "last_dealing_at": str(stamp),
        "age_minutes": round(age, 1),
        "threshold_minutes": limits.dealing_stale_minutes,
    }
    blocked_at = _utc(last_blocked[0]) if last_blocked else None
    if (
        blocked_at is not None and blocked_at > stamp
        and (moment - blocked_at).total_seconds() / 60.0 <= limits.dealing_stale_minutes
    ):
        reason = last_blocked[1]
        return [
            MonitoringIssue(
                type="dealing",
                severity="critical",
                message=(
                    f"The market is open and the dealer is running, but it has "
                    f"not been able to read the account for {age:.0f} minutes: "
                    f"{reason}."
                ),
                timestamp=moment,
                details={**details, "last_blocked_at": str(blocked_at), "reason": reason},
                remedy=(
                    "The box is working; the broker is not answering usefully. "
                    "A zero balance is Capital's usual reply on a fresh "
                    "session while the book is flat, and each pass already "
                    "retries it three times. If it persists, check Capital's "
                    "API status and that the credentials in "
                    "/etc/cfdinvest/env still resolve. Nothing can be opened "
                    "or closed until it clears."
                ),
            )
        ]

    return [
        MonitoringIssue(
            type="dealing",
            severity="critical",
            message=(
                f"The market is open and the account has not been examined "
                f"for {age:.0f} minutes."
            ),
            timestamp=moment,
            details=details,
            remedy=(
                "No dealing pass has run, or none has finished, and a unit "
                "reading `active` does not settle it -- that is how the "
                "2026-09-09 stall hid. The dealer is its own oneshot "
                "service on a five-minute timer: check "
                "`systemctl status cfdinvest-dealer.timer` and "
                "`journalctl -u cfdinvest-dealer -n 50` for the last pass "
                "and how it ended."
            ),
        )
    ]


def check_price_repairs(
    repairs: Sequence[Any] | None,
    *,
    now: pd.Timestamp | None = None,
    recent_days: float = 14.0,
) -> list[MonitoringIssue]:
    """Has Capital's price history needed repairing lately?

    ``repairs`` is ``DataRepository.last_price_repairs`` after loading the
    recent lake: what ``ingestion.price_repair`` rescaled, and what it could
    not verify. ``None`` means the load did not happen, which is reported as
    unchecked rather than clean.

    An ``unverified`` jump is a WARNING: a move of a quarter or more on a
    day the daily store does not cover yet -- the shape of a split on the day
    it happens, which the engine reads as a crash until the nightly ingest
    lets it be repaired. A recent repair is INFO: it worked, and a person
    holding that name may want to know its history just moved.
    """
    moment = _now(now)
    if repairs is None:
        return [_unreadable("lake", "Price repair",
                            "the recent lake could not be loaded")]
    cutoff = moment - pd.Timedelta(days=recent_days)
    recent = [r for r in repairs
              if _utc(getattr(r, "end", None)) is not None
              and _utc(getattr(r, "end")) >= cutoff]
    issues: list[MonitoringIssue] = []
    unverified = [r for r in recent if r.kind == "unverified"]
    if unverified:
        issues.append(MonitoringIssue(
            type="lake", severity="warning",
            message=(
                "Unverified price jump(s) in Capital's bars: "
                + ", ".join(f"{r.ticker} at {r.start}" for r in unverified)
                + ". Possibly a split the daily store has not caught up with."
            ),
            timestamp=moment,
            details={"jumps": [r.to_document() for r in unverified]},
            remedy=(
                "Compare the name's price with its listing. A split shows as a "
                "move by a round factor (2x, 3x, 10x); the nightly daily "
                "ingest lets the repair correct it. Until then the engine "
                "reads it as a crash, so treat any signal on that name today "
                "as suspect."
            ),
        ))
    repaired = [r for r in recent if r.kind != "unverified"]
    if repaired:
        issues.append(MonitoringIssue(
            type="lake", severity="info",
            message=(
                "Capital's history was repaired: "
                + ", ".join(f"{r.ticker} {r.kind} x{r.factor:.3f}" for r in repaired)
            ),
            timestamp=moment,
            details={"repairs": [r.to_document() for r in repaired]},
            remedy="Nothing to do; recorded so a moved history is not a surprise.",
        ))
    return issues


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


def win_rate_check(
    trips=None, thresholds: "MonitoringThresholds | None" = None, now=None,
) -> list[MonitoringIssue]:
    """Is the forward win rate below what the model says to expect?

    **This stops nothing**, like every review trigger here. It asks a person
    to look, on terms fixed before the data existed -- see
    ``model_win_rate``.

    Silent below ``win_rate_min_trips``, and silent by SAYING SO rather than
    by passing: at seven trips a 14% win rate and a 60% win rate are both
    unremarkable, and a green tick on that evidence is a claim nobody has
    earned. "Not enough trades to say" is the honest reading and it is the
    one reported.

    Absent or unreadable trips return ``unchecked`` for the same reason.
    """
    import math

    settings = thresholds or DEFAULTS
    moment = _now(now)
    if trips is None:
        return [_unreadable("review", "Forward win rate", "no trades readable")]

    completed = [t for t in trips if t and t.get("pnl") is not None]
    minimum = int(settings.win_rate_min_trips)
    if len(completed) < minimum:
        return [
            MonitoringIssue(
                type="review",
                severity="info",
                message=(
                    f"{len(completed)} completed trip(s); the win rate is not "
                    f"measurable below {minimum}. At this sample almost any "
                    "rate is unremarkable."
                ),
                timestamp=moment,
                details={"trips": len(completed), "minimum": minimum},
                remedy="Nothing to do. It becomes answerable with more trades.",
            )
        ]

    wins = sum(1 for t in completed if float(t["pnl"]) > 0)
    rate = wins / len(completed)
    expected = float(settings.model_win_rate)
    error = math.sqrt(max(expected * (1.0 - expected), 1e-12) / len(completed))
    floor = expected - float(settings.win_rate_sigmas) * error
    if rate >= floor:
        return []
    return [
        MonitoringIssue(
            type="review",
            severity="warning",
            message=(
                f"The forward win rate is {rate:.0%} over {len(completed)} "
                f"trips, below the {floor:.0%} that {settings.win_rate_sigmas:g} "
                f"standard errors under the model's {expected:.0%} allows."
            ),
            timestamp=moment,
            details={
                "trips": len(completed), "wins": wins, "win_rate": rate,
                "expected": expected, "floor": floor,
            },
            remedy=(
                "Worth looking at rather than acting on. The strategy is "
                "designed to lose more trips than it wins -- the profit comes "
                "from winners running -- so check the HOLD durations and the "
                "exit reasons before the rate: winners cut short is a "
                "different fault from entries being wrong. Do not pause "
                "automatically; every automatic pause tested here lost money."
            ),
        )
    ]


def review_triggers(
    equity_curve=None,
    thresholds: MonitoringThresholds | None = None,
    *,
    now: pd.Timestamp | None = None,
) -> list[MonitoringIssue]:
    """Say when the book is worth a human look. Never act on it.

    The owner's policy is to run for maximum growth and review the strategy
    when things turn bad. That only works if something says they have, and
    nothing did: monitoring watched staleness and wipeouts, so a book could
    halve while every check reported green.

    **This stops nothing.** It cannot be wired to execution and should not
    be: four automatic pauses were tested causally here and all four lost
    money, because the strategy earns most in the volatility a fear-based
    rule would sit out. What it produces is a warning addressed to a person.

    Two triggers, deliberately different in kind:

    * A drawdown deeper than the model plausibly produces. Not "a
      drawdown" -- the book draws down 41.4% in backtest and that is the
      price of the growth rate, and resampling puts the MEDIAN modelled
      drawdown between 42% and 49%, so even that realised figure was a mild
      path. The threshold is set from the distribution rather than from the
      one path, because "worse than the single backtest" is a statement
      about luck and "worse than the model's own range" is a statement
      about the model. See ``review_drawdown_fraction``.
    * Months of going nowhere. A trend follower in a sideways market is the
      known failure mode, and it does not announce itself as a crash. It
      announces itself as a flat line, which no drawdown threshold catches.

    Absent or unreadable equity returns ``unchecked`` rather than silence,
    because "not measured" and "measured and fine" are different statements.
    """
    moment = _now(now)
    if equity_curve is None or len(equity_curve) < 2:
        return [_unreadable("review", "Review triggers", "no equity curve")]

    settings = thresholds or DEFAULTS
    issues: list[MonitoringIssue] = []
    curve = equity_curve.dropna()
    if curve.empty:
        return [_unreadable("review", "Review triggers", "equity curve is empty")]

    drawdown = float((curve / curve.cummax() - 1.0).min())

    # Two readings of the same number, saying different things. The first is
    # about the account and the second about the model; both fire on a deep
    # enough fall, and that is intended -- the second escalates the first
    # rather than replacing it.
    limit = float(settings.review_drawdown_fraction)
    if abs(drawdown) >= limit:
        issues.append(
            MonitoringIssue(
                type="review",
                severity="warning",
                message=(
                    f"The book is {abs(drawdown):.1%} below its peak, past "
                    f"the {limit:.0%} worth a look."
                ),
                timestamp=moment,
                details={"drawdown_fraction": drawdown, "limit": limit},
                remedy=(
                    "Worth a look, and not by itself evidence the strategy "
                    "has stopped working: a drawdown this deep is inside the "
                    "range the model's own resampled paths reach. Do not "
                    "pause automatically -- every automatic pause tested "
                    "here lost money."
                ),
            )
        )

    unmodelled = float(
        getattr(settings, "unmodelled_drawdown_fraction", limit) or limit
    )
    if abs(drawdown) >= unmodelled:
        issues.append(
            MonitoringIssue(
                type="review",
                severity="warning",
                message=(
                    f"The book is {abs(drawdown):.1%} below its peak, beyond "
                    f"the {unmodelled:.0%} that only about one modelled path "
                    f"in ten reaches."
                ),
                timestamp=moment,
                details={"drawdown_fraction": drawdown, "limit": unmodelled},
                remedy=(
                    "Worth reviewing the strategy rather than the plumbing: "
                    "a drawdown outside the model's own range says the model "
                    "is missing something, which is a reason to re-measure "
                    "rather than to watch. Do not pause automatically -- "
                    "every automatic pause tested here lost money."
                ),
            )
        )

    months = float(settings.review_flat_months)
    window = curve.index.max() - pd.Timedelta(days=months * 30.44)
    trailing = curve.loc[curve.index >= window]
    if len(trailing) > 1 and float(trailing.index.min().value) > float(curve.index.min().value):
        change = float(trailing.iloc[-1] / trailing.iloc[0] - 1.0)
        if change < 0:
            issues.append(
                MonitoringIssue(
                    type="review",
                    severity="warning",
                    message=(
                        f"The book has returned {change:.1%} over the last "
                        f"{months:.0f} months."
                    ),
                    timestamp=moment,
                    details={"trailing_return": change, "months": months},
                    remedy=(
                        "Going sideways is the known failure mode for a trend "
                        "follower and does not show up as a drawdown spike. "
                        "Worth asking whether the market still suits the "
                        "strategy -- a question for a person, not a trigger."
                    ),
                )
            )
    return issues
