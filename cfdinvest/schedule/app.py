"""Command-line entry point for scheduled runs.

    python -m cfdinvest.schedule.app run-once
    python -m cfdinvest.schedule.app run-continuous
    python -m cfdinvest.schedule.app send-test-email --dry-run
    python -m cfdinvest.schedule.app market-status
    python -m cfdinvest.schedule.app crontab

``run-once`` is the cron entry point: it does the work and exits, which is
the shape that survives reboots and does not leak. ``run-continuous`` is for
hosts without cron.

Exit codes matter for unattended use — cron mails on a non-zero exit, and a
monitor can alert on it:

===== ==========================================
``0`` the run completed
``1`` the run failed
``2`` bad arguments or configuration
===== ==========================================
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import replace

import pandas as pd

from cfdinvest.backtest import BacktestConfig
from cfdinvest.config import load_config
from cfdinvest.logging_utils import configure_logging
from cfdinvest.repository import DataRepository
from cfdinvest.schedule import notify as notify_module
from cfdinvest.schedule import scheduler as scheduler_module
from cfdinvest.schedule.market_calendar import describe
from cfdinvest.schedule.notify import (
    ConsoleTransport,
    RecordingTransport,
    SmtpConfig,
    send_daily_summary,
    send_error_alert,
    send_test_email,
    send_trade_alert,
)
from cfdinvest.schedule.runner import RunConfig, run_pipeline
from cfdinvest.schedule.scheduler import Scheduler, SchedulerConfig

logger = logging.getLogger(__name__)

__all__ = ["build_parser", "main"]

EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser. Separated so tests can parse without running."""
    parser = argparse.ArgumentParser(
        prog="cfdinvest-schedule",
        description="Run the cfdinvest pipeline on a market-aware schedule.",
    )
    parser.add_argument("--config", default=None, help="path to a TOML config file")
    parser.add_argument("--data-root", default=None, help="override the lake location")
    # Comma-separated, not nargs="*": a greedy list option placed before a
    # subcommand swallows the subcommand itself, so `--tickers SPY TLT
    # market-status` fails with "command required". This also matches the
    # CFD_TICKERS environment convention used elsewhere in the project.
    parser.add_argument(
        "--tickers",
        default=None,
        metavar="SPY,TLT,GLD",
        help="comma-separated universe; defaults to config",
    )
    parser.add_argument("--interval", default=None, help="bar size, e.g. 1d")
    parser.add_argument("--start", default=None, help="history start, YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="history end, YYYY-MM-DD")
    parser.add_argument("--log-level", default=None, help="DEBUG, INFO, WARNING")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="capture emails instead of sending them",
    )
    parser.add_argument(
        "--no-email", action="store_true", help="skip notifications entirely"
    )

    commands = parser.add_subparsers(dest="command", required=True)

    once = commands.add_parser("run-once", help="run the pipeline once and exit")
    once.add_argument(
        "--refresh", action="store_true", help="top up the lake from the vendors first"
    )
    once.add_argument(
        "--force",
        action="store_true",
        help="run even when every market in the universe is closed",
    )
    once.add_argument(
        "--trade-alerts",
        action="store_true",
        help="email one alert per trade on the latest bar",
    )
    once.add_argument("--no-save", action="store_true", help="do not persist outputs")

    continuous = commands.add_parser(
        "run-continuous", help="wait for market events and run at each"
    )
    continuous.add_argument("--max-runs", type=int, default=None)
    continuous.add_argument("--delay-minutes", type=int, default=15)
    continuous.add_argument("--on-open", action="store_true")
    continuous.add_argument("--no-on-close", action="store_true")
    continuous.add_argument("--refresh", action="store_true")
    continuous.add_argument(
        "--apscheduler",
        action="store_true",
        help="use APScheduler's cron triggers instead of the sleep loop",
    )

    commands.add_parser("send-test-email", help="prove the mail path works")
    commands.add_parser("market-status", help="show session state for the universe")

    crontab = commands.add_parser("crontab", help="print crontab lines for the universe")
    # dest is explicit: the subparsers already use dest="command", and a
    # plain --command here silently overwrites which subcommand was chosen.
    crontab.add_argument(
        "--command",
        dest="cron_command",
        default="cfdinvest-schedule run-once",
        help="the command each crontab line should invoke",
    )
    crontab.add_argument("--delay-minutes", type=int, default=15)

    return parser


def _universe(args) -> list[str] | None:
    """Split the comma-separated ``--tickers`` value, or return ``None``."""
    if not args.tickers:
        return None
    return [part.strip() for part in args.tickers.split(",") if part.strip()]


def _app_config(args):
    """Resolve the AppConfig, applying CLI overrides."""
    overrides: dict[str, dict[str, object]] = {}
    if args.data_root:
        overrides.setdefault("paths", {})["data_root"] = args.data_root
    if args.log_level:
        overrides.setdefault("logging", {})["level"] = args.log_level
    return load_config(args.config, overrides=overrides)


def _backtest_config(args, app_config) -> BacktestConfig:
    """Build the pipeline config from CLI arguments and the app config."""
    tickers = _universe(args) or list(app_config.market.tickers) or None
    return BacktestConfig(
        tickers=tickers,
        series_ids=list(app_config.macro.series_ids) or None,
        interval=args.interval,
        start=args.start,
        end=args.end,
    )


def _transport(args):
    """Choose an email transport: recording for a dry run, else the default."""
    if args.no_email:
        return RecordingTransport()          # built, never sent
    if args.dry_run:
        return ConsoleTransport()
    return None                              # notify falls back to SMTP


def _command_run_once(args, app_config, smtp, transport) -> int:
    """Run the pipeline once, notify, and return an exit code."""
    backtest = _backtest_config(args, app_config)
    universe = backtest.tickers or []

    now = pd.Timestamp.now(tz="UTC")
    if universe and not args.force:
        if not scheduler_module.any_open(universe, now):
            logger.info(
                "Every market in the universe is closed; skipping. Use --force to run anyway."
            )
            print(describe(universe, now).to_string())
            return EXIT_OK

    summary = run_pipeline(
        universe,
        RunConfig(
            backtest=backtest,
            refresh_data=args.refresh,
            save_outputs=not args.no_save,
        ),
        repository=DataRepository(app_config),
        app_config=app_config,
        now=now,
    )

    print(summary.headline())
    if not summary.latest_signals.empty:
        print("\nActive signals on the latest bar:")
        print(summary.latest_signals.to_string())

    if not args.no_email:
        if summary.ok:
            send_daily_summary(summary, smtp, transport)
            if args.trade_alerts and not summary.latest_trades.empty:
                for _, trade in summary.latest_trades.iterrows():
                    send_trade_alert(trade, smtp, transport)
        else:
            send_error_alert(
                summary.error or "unknown failure", smtp, transport,
                context=f"run {summary.run_id}",
            )

    return EXIT_OK if summary.ok else EXIT_FAILED


def _command_run_continuous(args, app_config, smtp, transport) -> int:
    """Wait for market events and run at each."""
    backtest = _backtest_config(args, app_config)
    universe = backtest.tickers or []
    if not universe:
        print("No universe configured; set --tickers or config.market.tickers", file=sys.stderr)
        return EXIT_USAGE

    config = SchedulerConfig(
        on_open=args.on_open,
        on_close=not args.no_on_close,
        delay_minutes=args.delay_minutes,
        max_runs=args.max_runs,
    )
    repository = DataRepository(app_config)
    run_config = RunConfig(backtest=backtest, refresh_data=args.refresh)

    def job(event):
        logger.info("Scheduled trigger: %s", event)
        summary = run_pipeline(
            universe, run_config, repository=repository, app_config=app_config
        )
        if not args.no_email:
            if summary.ok:
                send_daily_summary(summary, smtp, transport)
            else:
                send_error_alert(
                    summary.error or "unknown failure", smtp, transport,
                    context=f"run {summary.run_id}",
                )
        return summary

    if args.apscheduler:
        engine = scheduler_module.build_apscheduler(universe, job, config)
        print("Starting APScheduler. Ctrl-C to stop.")
        try:
            engine.start()
        except (KeyboardInterrupt, SystemExit):
            print("\nStopped.")
        return EXIT_OK

    def on_error(error, event):
        if not args.no_email:
            send_error_alert(error, smtp, transport, context=str(event))

    engine = Scheduler(universe, job, config, on_error=on_error)
    print(f"Watching {len(universe)} instrument(s). Ctrl-C to stop.")
    try:
        fired = engine.run_forever()
        print(f"Completed {len(fired)} scheduled run(s).")
    except KeyboardInterrupt:
        print("\nStopped.")
    return EXIT_OK


def _command_send_test_email(args, smtp, transport) -> int:
    """Prove the mail path works, reporting why if it does not."""
    problems = smtp.validate()
    if problems and transport is None:
        print("Email is not configured:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print(
            "\nSet CFD_SMTP_HOST, CFD_SMTP_USER, CFD_SMTP_PASSWORD, "
            "CFD_SMTP_SENDER and CFD_SMTP_RECIPIENTS, or pass --dry-run.",
            file=sys.stderr,
        )
        return EXIT_USAGE

    sent = send_test_email(smtp, transport)
    print("Test email sent." if sent else "Test email was not sent (see the log).")
    return EXIT_OK if sent else EXIT_FAILED


def _command_market_status(args, app_config) -> int:
    """Print the session state of every instrument in the universe."""
    universe = _universe(args) or list(app_config.market.tickers)
    if not universe:
        print("No universe configured.", file=sys.stderr)
        return EXIT_USAGE

    now = pd.Timestamp.now(tz="UTC")
    print(f"As of {now:%Y-%m-%d %H:%M} UTC\n")
    pd.set_option("display.width", 160)
    print(describe(universe, now).to_string())
    return EXIT_OK


def _command_crontab(args, app_config) -> int:
    """Print crontab lines covering the universe's sessions."""
    universe = _universe(args) or list(app_config.market.tickers)
    if not universe:
        print("No universe configured.", file=sys.stderr)
        return EXIT_USAGE

    config = SchedulerConfig(delay_minutes=args.delay_minutes)
    for line in scheduler_module.crontab_lines(universe, args.cron_command, config):
        print(line)
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns a process exit code rather than raising."""
    args = build_parser().parse_args(argv)

    app_config = _app_config(args)
    configure_logging(app_config.logging)

    smtp = SmtpConfig.from_env()
    transport = _transport(args)

    try:
        if args.command == "run-once":
            return _command_run_once(args, app_config, smtp, transport)
        if args.command == "run-continuous":
            return _command_run_continuous(args, app_config, smtp, transport)
        if args.command == "send-test-email":
            return _command_send_test_email(args, smtp, transport)
        if args.command == "market-status":
            return _command_market_status(args, app_config)
        if args.command == "crontab":
            return _command_crontab(args, app_config)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return EXIT_FAILED

    print(f"Unknown command {args.command!r}", file=sys.stderr)
    return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
