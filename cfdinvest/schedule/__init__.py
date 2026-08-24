"""Scheduling and orchestration: run the pipeline unattended, market-aware.

    market_calendar  static session rules; holidays derived, not listed
    runner           one run, persisted, with a summary object
    notify           SMTP alerts, with the transport injectable
    scheduler        cron lines, APScheduler triggers, or a sleep loop
    app              the CLI entry point

Nothing here models markets. It decides *when* the pipeline runs and *who
hears about it*; the pipeline itself belongs to :mod:`cfdinvest.backtest`.

Recommended deployment is cron plus ``run-once``: the process does its work
and exits, which survives reboots and cannot leak over a month.
"""

from cfdinvest.schedule.market_calendar import (
    SESSIONS,
    describe,
    is_open,
    next_close,
    next_open,
    session_for,
    us_market_holidays,
)
from cfdinvest.schedule.notify import (
    ConsoleTransport,
    RecordingTransport,
    SmtpConfig,
    build_daily_summary,
    build_error_alert,
    build_trade_alert,
    send_daily_summary,
    send_error_alert,
    send_test_email,
    send_trade_alert,
)
from cfdinvest.schedule.runner import RunConfig, RunSummary, run_pipeline
from cfdinvest.schedule.scheduler import (
    ScheduleEvent,
    Scheduler,
    SchedulerConfig,
    any_open,
    build_apscheduler,
    crontab_lines,
    next_event,
    open_assets,
)

__all__ = [
    "SESSIONS",
    "is_open",
    "next_open",
    "next_close",
    "session_for",
    "us_market_holidays",
    "describe",
    "RunConfig",
    "RunSummary",
    "run_pipeline",
    "SmtpConfig",
    "RecordingTransport",
    "ConsoleTransport",
    "build_trade_alert",
    "build_daily_summary",
    "build_error_alert",
    "send_trade_alert",
    "send_daily_summary",
    "send_error_alert",
    "send_test_email",
    "Scheduler",
    "SchedulerConfig",
    "ScheduleEvent",
    "next_event",
    "any_open",
    "open_assets",
    "crontab_lines",
    "build_apscheduler",
]
