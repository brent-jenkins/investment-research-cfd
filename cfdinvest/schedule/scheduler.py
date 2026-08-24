"""Market-aware scheduling.

Two ways to run unattended, because they suit different deployments:

**Cron.** :func:`crontab_lines` emits schedule entries derived from the
universe's session closes. The process starts, runs once, exits. Nothing to
supervise, nothing that leaks memory over a month, and the OS restarts it
after a reboot without help. This is the recommended shape.

**A long-lived loop.** :class:`Scheduler` waits for the next market event
and runs then. Useful when cron is unavailable, or when a run must fire
promptly after an open rather than at a fixed wall-clock time. APScheduler
is used when installed; otherwise a plain sleep loop does the same job.

Both paths share :func:`next_event`, so a cron line and a running loop fire
at the same instants rather than drifting apart.

The clock and the sleeper are injectable throughout. That is what makes the
loop testable: a test can simulate a week of market opens in milliseconds
and assert exactly which bars triggered a run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

import pandas as pd

from cfdinvest.schedule import market_calendar as calendar_module
from cfdinvest.schedule.market_calendar import is_open, next_close, next_open

logger = logging.getLogger(__name__)

__all__ = [
    "SchedulerConfig",
    "ScheduleEvent",
    "any_open",
    "open_assets",
    "next_event",
    "crontab_lines",
    "Scheduler",
    "build_apscheduler",
]


@dataclass(frozen=True, slots=True)
class SchedulerConfig:
    """When to run, and how long to wait between checks."""

    #: Run when a market opens.
    on_open: bool = False
    #: Run when a market closes. The default, because a bar is only complete
    #: once the session has ended — running at the open acts on yesterday's
    #: data with today's timestamp.
    on_close: bool = True
    #: Minutes after the event before running, so the vendor has published
    #: the closing bar. Zero means the instant of the event.
    delay_minutes: int = 15
    #: Longest a single sleep may last, so a running loop stays responsive
    #: to a stop signal rather than blocking for a whole weekend.
    max_sleep_seconds: float = 900.0
    #: Stop after this many runs. ``None`` runs until interrupted; tests
    #: pass a small number.
    max_runs: int | None = None
    asset_classes: dict[str, str] | None = None


@dataclass(frozen=True, slots=True)
class ScheduleEvent:
    """One moment the scheduler intends to act on."""

    at: pd.Timestamp
    kind: str          # "open" or "close"
    asset: str

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"{self.kind} of {self.asset} at {self.at:%Y-%m-%d %H:%M %Z}"


def _to_utc(timestamp) -> pd.Timestamp:
    stamp = pd.Timestamp(timestamp)
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def open_assets(
    universe: Iterable[str], timestamp, mapping: dict[str, str] | None = None
) -> tuple[str, ...]:
    """Which assets in the universe are trading right now. Pure."""
    moment = _to_utc(timestamp)
    return tuple(asset for asset in universe if is_open(asset, moment, mapping))


def any_open(
    universe: Iterable[str], timestamp, mapping: dict[str, str] | None = None
) -> bool:
    """Whether any asset in the universe is trading. Pure."""
    return bool(open_assets(universe, timestamp, mapping))


def next_event(
    universe: Sequence[str],
    timestamp,
    config: SchedulerConfig | None = None,
) -> ScheduleEvent | None:
    """The next moment the scheduler should act, across the whole universe.

    Pure. Returns the *earliest* qualifying event strictly after
    ``timestamp``, with the configured delay already applied — so a caller
    can sleep until ``event.at`` without further arithmetic.

    Returns ``None`` when nothing is scheduled, which for a normal universe
    means every session rule is 24/7.
    """
    settings = config or SchedulerConfig()
    moment = _to_utc(timestamp)
    delay = pd.Timedelta(minutes=settings.delay_minutes)

    candidates: list[ScheduleEvent] = []
    for asset in universe:
        if settings.on_close:
            closes = next_close(asset, moment - delay, settings.asset_classes)
            if closes is not None and closes + delay > moment:
                candidates.append(ScheduleEvent(closes + delay, "close", asset))
        if settings.on_open:
            opens = next_open(asset, moment - delay, settings.asset_classes)
            if opens is not None and opens + delay > moment:
                candidates.append(ScheduleEvent(opens + delay, "open", asset))

    if not candidates:
        return None
    return min(candidates, key=lambda event: (event.at, event.kind, event.asset))


def crontab_lines(
    universe: Sequence[str],
    command: str = "cfdinvest-schedule run-once",
    config: SchedulerConfig | None = None,
) -> list[str]:
    """Emit crontab entries covering the universe's session events.

    One line per distinct local time, with a comment naming the assets it
    covers. Times are expressed **in each session's own timezone**, and the
    line says which — a crontab runs in the host's zone, so a US equity
    close pinned to a fixed UTC hour drifts by an hour twice a year.

    Pure.

    Returns
    -------
    list of str
        Crontab lines, ready to paste.
    """
    settings = config or SchedulerConfig()
    grouped: dict[tuple, list[str]] = {}

    for asset in universe:
        rule = calendar_module.session_for(asset, settings.asset_classes)
        if rule.kind == "always":
            continue
        events = []
        if settings.on_close:
            events.append(("close", rule.close_time))
        if settings.on_open:
            events.append(("open", rule.open_time))

        for kind, moment in events:
            total = moment.hour * 60 + moment.minute + settings.delay_minutes
            hour, minute = divmod(total % (24 * 60), 60)
            grouped.setdefault((hour, minute, rule.timezone, kind), []).append(asset)

    lines = [
        "# cfdinvest scheduled runs.",
        "# Times are LOCAL TO EACH SESSION's timezone -- set CRON_TZ per block,",
        "# or the schedule drifts by an hour at every daylight-saving change.",
    ]
    for (hour, minute, zone, kind), assets in sorted(grouped.items()):
        lines.append("")
        lines.append(f"# {kind} + {settings.delay_minutes}min for: {', '.join(sorted(assets))}")
        lines.append(f"CRON_TZ={zone}")
        lines.append(f"{minute} {hour} * * 1-5 {command}")
    return lines


class Scheduler:
    """A market-aware loop that runs a job at each session event.

    The clock and the sleeper are injected, so the whole loop runs offline
    and deterministically in a test: a fake clock can advance a week in a
    few iterations and the assertions are about which events fired, not
    about elapsed wall-clock time.

    Parameters
    ----------
    universe:
        Tickers whose sessions drive the schedule.
    job:
        Called with the :class:`ScheduleEvent` that triggered it. Exceptions
        are caught and passed to ``on_error`` — one bad run must not end the
        loop, or a transient vendor outage silently stops the deployment.
    on_error:
        Optional handler for a failed job, typically an email alert.
    """

    def __init__(
        self,
        universe: Sequence[str],
        job: Callable[[ScheduleEvent], object],
        config: SchedulerConfig | None = None,
        *,
        clock: Callable[[], pd.Timestamp] | None = None,
        sleeper: Callable[[float], None] | None = None,
        on_error: Callable[[BaseException, ScheduleEvent], None] | None = None,
    ) -> None:
        self.universe = tuple(universe)
        self.job = job
        self.config = config or SchedulerConfig()
        self.clock = clock or (lambda: pd.Timestamp.now(tz="UTC"))
        self.sleeper = sleeper
        self.on_error = on_error
        self.history: list[ScheduleEvent] = []

    def _sleep(self, seconds: float) -> None:
        """Wait, in bounded slices so the loop stays interruptible."""
        if seconds <= 0:
            return
        if self.sleeper is not None:
            self.sleeper(seconds)
            return
        import time

        time.sleep(min(seconds, self.config.max_sleep_seconds))

    def run_once(self, event: ScheduleEvent | None = None):
        """Invoke the job for one event, containing any failure."""
        moment = event or ScheduleEvent(self.clock(), "manual", "manual")
        try:
            result = self.job(moment)
            self.history.append(moment)
            return result
        except Exception as error:  # noqa: BLE001 - a bad run must not end the loop
            logger.exception("Scheduled job failed for %s", moment)
            if self.on_error is not None:
                self.on_error(error, moment)
            self.history.append(moment)
            return None

    def run_forever(self) -> list[ScheduleEvent]:
        """Wait for each event in turn and run the job.

        Returns
        -------
        list of ScheduleEvent
            Every event that fired, in order. Bounded by
            ``config.max_runs``; unbounded runs return only on interrupt.
        """
        fired: list[ScheduleEvent] = []
        runs = 0

        while self.config.max_runs is None or runs < self.config.max_runs:
            now = _to_utc(self.clock())
            event = next_event(self.universe, now, self.config)

            if event is None:
                logger.info("Nothing scheduled; the universe trades continuously")
                self.run_once()
                return fired

            wait = (event.at - now).total_seconds()
            if wait > 0:
                logger.info(
                    "Sleeping %.0fs until %s", min(wait, self.config.max_sleep_seconds), event
                )
                self._sleep(wait)
                # A bounded sleep may return early; loop round and re-check
                # rather than assuming the event has arrived.
                if _to_utc(self.clock()) < event.at:
                    continue

            logger.info("Triggering run for %s", event)
            self.run_once(event)
            fired.append(event)
            runs += 1

        return fired


def build_apscheduler(
    universe: Sequence[str],
    job: Callable[[ScheduleEvent], object],
    config: SchedulerConfig | None = None,
):
    """Build an APScheduler with a cron trigger per session event.

    Returns a ``BlockingScheduler`` with jobs registered but **not started**,
    so the caller decides when to block.

    Preferred over :class:`Scheduler` when APScheduler is installed: it
    handles daylight saving per timezone properly, and misfire grace
    periods mean a run skipped during a restart is not silently lost.

    Raises
    ------
    ImportError
        If APScheduler is not installed, naming the alternative rather than
        failing obscurely.
    """
    try:
        from apscheduler.schedulers.blocking import BlockingScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError as error:  # pragma: no cover - depends on environment
        raise ImportError(
            "APScheduler is not installed. Either `pip install apscheduler`, "
            "or use Scheduler(...).run_forever(), or run `run-once` from cron."
        ) from error

    settings = config or SchedulerConfig()
    scheduler = BlockingScheduler()

    registered: set[tuple] = set()
    for asset in universe:
        rule = calendar_module.session_for(asset, settings.asset_classes)
        if rule.kind == "always":
            continue

        events = []
        if settings.on_close:
            events.append(("close", rule.close_time))
        if settings.on_open:
            events.append(("open", rule.open_time))

        for kind, moment in events:
            total = moment.hour * 60 + moment.minute + settings.delay_minutes
            hour, minute = divmod(total % (24 * 60), 60)
            key = (hour, minute, rule.timezone, kind)
            if key in registered:
                continue
            registered.add(key)

            scheduler.add_job(
                job,
                CronTrigger(
                    day_of_week="mon-fri",
                    hour=hour,
                    minute=minute,
                    timezone=rule.timezone,
                ),
                args=[ScheduleEvent(pd.Timestamp.now(tz="UTC"), kind, asset)],
                id=f"{kind}-{rule.timezone}-{hour:02d}{minute:02d}",
                # A run missed during a restart still fires if the process
                # comes back within the hour, rather than vanishing.
                misfire_grace_time=3600,
                coalesce=True,
            )

    return scheduler
