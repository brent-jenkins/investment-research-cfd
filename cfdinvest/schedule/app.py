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
import dataclasses
import logging
import sys
from dataclasses import replace

import pandas as pd

from cfdinvest.backtest import BacktestConfig
from cfdinvest.signals.frame import SignalConfig
from cfdinvest.config import load_config
from cfdinvest.logging_utils import configure_logging
from cfdinvest.repository import DataRepository
from cfdinvest.schedule import notify as notify_module
from cfdinvest.schedule import runner as runner_module
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

    risk = commands.add_parser(
        "risk-snapshot",
        help="read the persisted artefacts and report the book's risk",
        description=(
            "Compute a risk snapshot from what the pipeline already wrote. "
            "Reads artefacts and writes only the snapshot; it never runs the "
            "pipeline, refreshes the lake or touches a position."
        ),
    )
    risk.add_argument(
        "--watch",
        action="store_true",
        help="keep taking snapshots on an interval instead of exiting",
    )
    risk.add_argument(
        "--interval-minutes",
        type=float,
        default=15.0,
        help="gap between snapshots when watching (default: 15)",
    )
    risk.add_argument(
        "--max-observations",
        type=int,
        default=None,
        help="stop after this many snapshots; unbounded by default",
    )
    risk.add_argument(
        "--no-store", action="store_true", help="report without recording"
    )
    risk.add_argument(
        "--balance",
        type=float,
        default=None,
        help="account balance in GBP; the denominator of leverage and margin",
    )
    risk.add_argument(
        "--account", default=None, help="which account's artefacts to read"
    )

    monitor = commands.add_parser(
        "monitor",
        help="check the machine's operational health",
        description=(
            "Run every operational check against what is on disk. Reads "
            "artefacts and the machine, and writes one snapshot; it never "
            "runs the pipeline or touches a position."
        ),
    )
    monitor.add_argument(
        "--watch", action="store_true", help="keep checking on an interval"
    )
    monitor.add_argument("--interval-minutes", type=float, default=30.0)
    monitor.add_argument("--max-passes", type=int, default=None)
    monitor.add_argument(
        "--no-store", action="store_true", help="report without recording"
    )
    monitor.add_argument(
        "--alert",
        action="store_true",
        help="email on a red status; off by default so a manual check is quiet",
    )
    monitor.add_argument("--account", default=None)

    quotes = commands.add_parser(
        "poll-live-quotes",
        help="fetch the current price for every confirmed position",
    )
    quotes.add_argument("--account", default=None)

    paper = commands.add_parser(
        "paper-book",
        help="one pass of the simulated forward-test book",
    )
    paper.add_argument(
        "--account",
        help="which account's book to write; the shared root when unset",
    )

    daemon_parser = commands.add_parser(
        "daemon",
        help="run every scheduled job in one loop until stopped",
    )
    daemon_parser.add_argument("--account", default=None)
    daemon_parser.add_argument(
        "--tick-seconds", type=int, default=30,
        help="how often to ask what is due; the jobs have their own cadences",
    )
    daemon_parser.add_argument(
        "--max-ticks", type=int, default=None,
        help="stop after this many ticks; for tests and dry runs",
    )

    advise = commands.add_parser(
        "advisor-alert",
        help="email anything newly worth acting on",
        description=(
            "Compares the open book and the latest signals against what has "
            "already been said, and emails only what is new. Places nothing."
        ),
    )
    advise.add_argument("--account", default=None)
    advise.add_argument(
        "--balance", type=float, default=None,
        help="account balance, for sizing the suggestions",
    )
    advise.add_argument(
        "--min-confidence", type=float, default=None,
        help="the bar a suggestion must clear (default 0.60)",
    )

    prune = commands.add_parser(
        "prune-runs",
        help="remove old run artefacts so the disk does not fill",
        description=(
            "A complete run is ~31MB and 98% of that is the analysis frame, "
            "which is only read for the most recent run. Recent runs keep "
            "everything; older ones keep the light artefacts; oldest go."
        ),
    )
    prune.add_argument("--keep-full", type=int, default=None)
    prune.add_argument("--keep-runs", type=int, default=None)
    prune.add_argument(
        "--dry-run", dest="prune_dry_run", action="store_true",
        help="report what would go without removing anything",
    )
    prune.add_argument("--account", default=None)

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


#: How each foreign currency's GBP rate is built from series the lake holds.
#:
#: Only EURGBP is quoted the way a sterling account needs it. GBPUSD is USD
#: per GBP, so a dollar instrument takes its reciprocal; and nothing here
#: publishes GBP/JPY, GBP/CAD or GBP/CHF at all, so those cross through the
#: dollar. An exponent of -1 inverts the leg.
#:
#: Without this every foreign rate fell back to 1.0 -- a US share priced at
#: $150 was marked at £150 -- and the engine said so in a warning per
#: instrument per run that nothing was reading. Sterling went from about
#: 1.90 to 1.27 against the dollar over the backtest, so "1.0" was not a
#: small approximation.
LAKE_FX_PAIRS: dict[str, object] = {
    "USD": ("GBPUSD=X", -1),
    "EUR": "EURGBP=X",
    "JPY": [("USDJPY=X", -1), ("GBPUSD=X", -1)],
    "CAD": [("USDCAD=X", -1), ("GBPUSD=X", -1)],
    "CHF": [("USDCHF=X", -1), ("GBPUSD=X", -1)],
}


def fx_support_tickers() -> tuple[str, ...]:
    """Every currency pair :data:`LAKE_FX_PAIRS` names, deduplicated.

    Ingested but never traded. The universe is twenty US shares; the pairs
    exist so the engine can convert a dollar price into sterling, and an
    instrument that is only there to be read must not also be offered as a
    position.

    Derived from the map rather than listed again. The map is the one owner
    of which pairs matter, and a second list would be right until the day it
    was not -- which is exactly how this failed the second time.

    That second failure is why this exists. The map was correct and the
    warning was explicit -- "no USD series for NVDA in the lake ... using the
    configured rate 1" -- but the pairs had stopped being *fetched* when the
    universe narrowed to US shares and dropped them from the ticker list. So
    every intraday run on the server marked a $150 share at £150, twenty
    warnings a run, into a log nobody reads.
    """
    found: list[str] = []
    for value in LAKE_FX_PAIRS.values():
        legs = value if isinstance(value, list) else [value]
        for leg in legs:
            ticker = leg[0] if isinstance(leg, tuple) else leg
            if isinstance(ticker, str) and ticker and ticker not in found:
                found.append(ticker)
    return tuple(found)


def _with_fx_support(tickers) -> list[str]:
    """The universe, plus the pairs needed to price it in sterling."""
    wanted = list(tickers or [])
    for pair in fx_support_tickers():
        if pair not in wanted:
            wanted.append(pair)
    return wanted


def _with_screen_pool(tickers, app_config) -> list[str]:
    """The list, plus every candidate the universe screen ranks.

    Daily bars only. The screen measures liquidity and volatility on daily
    data -- they belong to an instrument rather than to the bar size it is
    traded on -- and the candidates are not traded, so the intraday job has
    no reason to fetch them.

    Without this the screen ranks whatever happens to be in the lake, which
    is the traded universe, and re-ranking the held names against each other
    reports "it would add: nothing". That is the reassuring answer arrived at
    by not being able to see the alternatives -- the third time today that a
    thing named in the configuration was never fetched.
    """
    wanted = list(tickers or [])
    for candidate in getattr(app_config.universe, "candidates", ()) or ():
        if candidate not in wanted:
            wanted.append(str(candidate))
    return wanted


def _execution_config(tickers, strategy=None):
    """The cost model, built from what the broker actually charges.

    The engine has always been able to take per-asset financing rates and a
    currency map; nothing populated them, so every run financed the whole
    universe at a flat 8% long and *earned* 2% short. Trading 212 charges
    both sides on everything recorded -- 25.33% to hold a US share long,
    9.16% to hold one short -- and the difference over the 2005-2026 run was
    £24,666 of understated cost, most of it from shorts that were being paid
    to exist.

    Anything with no recorded pair keeps the house rate. That is a real
    approximation and it is logged by name rather than absorbed, because a
    run financed partly at broker rates and partly at a default has a total
    that is neither and looks like neither.
    """
    from cfdinvest.config import StrategyConfig
    from cfdinvest.execution import ExecutionConfig
    from cfdinvest.execution.fx import FXConfig
    from cfdinvest.execution.state import HysteresisThresholds
    from cfdinvest.ingestion.earnings import load_earnings_dates
    from cfdinvest.livehelper import broker_costs

    strategy = strategy or StrategyConfig()
    universe = list(tickers or ())
    try:
        rates = broker_costs.financing_overrides(universe)
        currencies = broker_costs.instrument_currencies(universe)
        fee_flags = broker_costs.fx_fee_flags(universe)
        missing = broker_costs.unpriced(universe)
    except Exception as error:  # noqa: BLE001 - an unreadable table is not an outage
        logger.warning(
            "Could not read the broker rules; financing falls back to the "
            "house rate for the whole universe: %s", error
        )
        return ExecutionConfig(
            max_leverage=strategy.max_leverage,
            slippage_fraction=strategy.slippage_bps / 10_000.0,
        )

    if missing:
        logger.warning(
            "No recorded financing for %d of %d instruments, charged at the "
            "house rate instead: %s",
            len(missing), len(universe), ", ".join(missing),
        )
    logger.info(
        "Financing %d instruments at recorded broker rates; %d of %d pay the "
        "conversion fee.",
        len(rates), sum(fee_flags.values()), len(fee_flags),
    )

    try:
        earnings = load_earnings_dates()
    except Exception as error:  # noqa: BLE001 - no dates is not an outage
        logger.warning("Could not read earnings dates: %s", error)
        earnings = {}

    return ExecutionConfig(
        financing_rates=rates or None,
        max_leverage=strategy.max_leverage,
        slippage_fraction=strategy.slippage_bps / 10_000.0,
        rebalance_threshold=strategy.rebalance_threshold,
        exposure_per_unit_size=strategy.exposure_per_unit_size,
        reentry_cooldown_days=strategy.reentry_cooldown_days,
        time_stop_days=strategy.time_stop_days,
        earnings_dates=earnings or None,
        earnings_blackout_days=strategy.earnings_blackout_days,
        thresholds=HysteresisThresholds(
            entry_confidence=strategy.entry_confidence,
            exit_confidence=strategy.exit_confidence,
            exit_on_flat=strategy.exit_on_flat,
            exit_on_opposing_signal=strategy.exit_on_opposing_signal,
        ),
        fx=FXConfig(
            enabled=strategy.fx_enabled,
            account_currency=strategy.account_currency,
            instrument_currencies=currencies or None,
            fx_pair_tickers=dict(LAKE_FX_PAIRS),
            fx_fee_applies=fee_flags or None,
            fx_conversion_fee_pct=strategy.fx_conversion_fee_pct,
            fx_rate_source="lake",
        ),
    )


def _backtest_config(args, app_config) -> BacktestConfig:
    """Build the pipeline config from CLI arguments and the app config.

    The conversion pairs are named in ``signal.reference_tickers`` rather
    than added to ``tickers``. Putting them in the universe was tried and
    is wrong: currency trades nearly around the clock and US shares do not,
    so joining them into one hourly panel makes the index the union of two
    calendars, the shares go NaN outside their session, and every signal in
    the book dies. That is the mixed-session failure the universe was
    narrowed to a single session to escape, walked straight back into.

    They are attached to the price panel afterwards instead, on the traded
    grid -- see ``backtest.runner.attach_reference_prices``.
    """
    universe = _universe(args) or list(app_config.market.tickers) or None
    return BacktestConfig(
        tickers=universe,
        series_ids=list(app_config.macro.series_ids) or None,
        interval=args.interval,
        start=args.start,
        end=args.end,
        execution=_execution_config(universe, app_config.strategy),
        signal=dataclasses.replace(
            SignalConfig(),
            regime_mode=app_config.strategy.regime_mode,
            use_volume=app_config.strategy.use_volume,
            volume_heavy_ratio=app_config.strategy.volume_heavy_ratio,
            volume_heavy_multiplier=app_config.strategy.volume_heavy_multiplier,
            environment_in_confidence=app_config.strategy.environment_in_confidence,
            hold_across_session_open=(
                app_config.strategy.hold_across_session_open
            ),
            reference_tickers=frozenset(fx_support_tickers()),
        ),
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
            if getattr(smtp, "send_run_summary", False):
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
            if summary.ok and getattr(smtp, "send_run_summary", False):
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


def _command_advisor_alert(args, app_config, smtp, transport) -> int:
    """Email anything newly worth acting on. Places nothing, ever."""
    from cfdinvest.advisor import actions, alerts

    account = getattr(args, "account", None)
    threshold = getattr(args, "min_confidence", None)

    balance = getattr(args, "balance", None)
    if balance is None:
        # From the live helper's configured balance -- the same number the
        # sizing engine uses. Without it the alert said "size unknown",
        # which is the one thing it exists to tell you.
        try:
            from cfdinvest.livehelper.config import load_live_helper_config

            balance = load_live_helper_config().account_balance_gbp
        except Exception as error:  # noqa: BLE001
            logger.warning("Advisor: no balance configured: %s", error)
            balance = 0.0

    book, prices, latest, as_of = _advisor_inputs(app_config, account)
    watches = actions.positions_to_watch(book, prices=prices)
    suggestions, _ = actions.suggested_trades(
        latest,
        balance=float(balance or 0.0),
        threshold=(
            actions.DEFAULT_CONFIDENCE if threshold is None else float(threshold)
        ),
        prices=prices,
        mapping=_instrument_mapping(),
    )

    seen = alerts.load_seen(app_config, account)
    fresh, now_seen = alerts.decide_alerts(
        suggestions, watches, seen, as_of=as_of
    )
    alerts.save_seen(now_seen, app_config, account)

    if not fresh:
        logger.info("Advisor: nothing new to say")
        return EXIT_OK

    for alert in fresh:
        logger.info("Advisor: %s", alert.headline)
    if not args.no_email:
        if getattr(smtp, "send_advisor_suggestions", False):
            notify_module.send_advisor_alert(fresh, smtp, transport)
    print(f"{len(fresh)} new alert(s).")
    return EXIT_OK


def _instrument_mapping():
    from cfdinvest.livehelper.t212_instruments import mapping_for

    return mapping_for


def _advisor_inputs(app_config, account):
    """The book, the prices, the latest signals and their timestamp.

    Reads only. Every lookup is guarded: an advisor that cannot read the
    signals should still be able to warn about an open position.
    """
    book, prices, latest, as_of = {}, {}, [], None

    try:
        from cfdinvest.persist.confirm import confirmed_positions, load_confirmations

        replayed = confirmed_positions(
            load_confirmations(app_config, account=account)
        )
        book = {
            name: entry for name, entry in (replayed or {}).items()
            if (entry or {}).get("quantity")
        }
    except Exception as error:  # noqa: BLE001
        logger.warning("Advisor: could not read the confirmed book: %s", error)

    try:
        from cfdinvest.persist import readers

        run_id = readers.load_latest_run(app_config, account=account)
        # The generic artefact reader: there is no load_signal_frame, and
        # rebuild_signal_frame returns a SignalFrame rather than the long
        # form this wants.
        frame = (
            None if run_id is None
            else readers.load_frame("signals", run_id, app_config, account)
        )
        if frame is not None and not frame.empty:
            working = frame.reset_index() if hasattr(frame, "reset_index") else frame
            if "timestamp" in working.columns:
                working["timestamp"] = pd.to_datetime(
                    working["timestamp"], utc=True, errors="coerce"
                )
                as_of = working["timestamp"].max()
                working = working[working["timestamp"] == as_of]
            latest = working.rename(columns={"ticker": "ticker"}).to_dict("records")
    except Exception as error:  # noqa: BLE001
        logger.warning("Advisor: could not read the signals: %s", error)

    try:
        from cfdinvest.livehelper.price_source import resolve_price

        wanted = list(book) + [str(row.get("ticker")) for row in latest]
        for ticker in dict.fromkeys(t for t in wanted if t):
            found = resolve_price(ticker, app_config=app_config, account=account)
            prices[ticker] = (found.price, found.note)
    except Exception as error:  # noqa: BLE001
        logger.warning("Advisor: could not price the instruments: %s", error)

    return book, prices, latest, as_of


def _command_prune_runs(args, app_config) -> int:
    """Remove old run artefacts. Never fails the caller."""
    from cfdinvest.persist import retention

    result = retention.prune_runs(
        app_config,
        account=getattr(args, "account", None),
        keep_full=(
            retention.DEFAULT_KEEP_FULL if getattr(args, "keep_full", None) is None
            else args.keep_full
        ),
        keep_runs=(
            retention.DEFAULT_KEEP_RUNS if getattr(args, "keep_runs", None) is None
            else args.keep_runs
        ),
        dry_run=bool(getattr(args, "prune_dry_run", False)),
    )
    if result.get("error"):
        print(f"Pruning skipped: {result['error']}", file=sys.stderr)
        return EXIT_OK
    print(
        f"Slimmed {len(result['slimmed'])}, removed {len(result['deleted'])}, "
        f"freed {result['freed_bytes'] / 1e6:.1f} MB"
        + (" (dry run)" if result["dry_run"] else "")
    )
    return EXIT_OK


def _command_poll_live_quotes(args, app_config) -> int:
    """Fetch the current price for everything confirmed open, and store it.

    Exit code is ``EXIT_OK`` even when no price could be read. A poll that
    finds nothing is not a failed command -- the readers fall back to the
    daily bar, which is what they do between polls anyway. A non-zero exit
    here would have systemd restarting the loop over a quiet market.
    """
    from cfdinvest.livequotes import poll_live_quotes, write_quotes
    from cfdinvest.livequotes.poller import instruments_to_price

    account = getattr(args, "account", None)
    # Open positions and anything clearing the confidence bar. A suggestion
    # carries a stake and a quantity, and priced from yesterday's close those
    # are yesterday's numbers.
    held = instruments_to_price(app_config, account)
    if not held:
        print("Nothing is open and nothing clears the bar, so there is "
              "nothing to price.")
        return EXIT_OK

    prices = poll_live_quotes(held)
    path = write_quotes(prices, app_config, account=account)
    priced = sum(1 for value in prices.values() if value is not None)
    print(
        f"Priced {priced} of {len(held)} held instrument(s)"
        + (f" -> {path}" if path else " (could not write the quote file)")
    )
    return EXIT_OK


#: The interval the universe screen and the long-history analyses read.
#:
#: Daily, and independent of what the engine trades. Liquidity and
#: volatility are properties of an instrument rather than of the bar size
#: somebody happens to be trading it on, and daily bars carry years of
#: history where hourly carries two.
_SCREENING_INTERVAL = "1d"


def _command_paper_book(args, app_config, smtp=None, transport=None) -> int:
    """One pass of the simulated book, and an email if it did anything.

    The email exists so a selection can be placed by hand on the virtual
    account and compared against the simulated fills. It is built from the
    book's own events rather than from the advisor's separate suggestion
    logic: an email describing a different decision from the one the book
    recorded would compare two things that were never the same.

    Silence when nothing happened, which is most passes. Every cost is the
    real one, from the recorded Trading 212 table.
    """
    from cfdinvest.advisor.papertrade_run import cohorts_for, run_all_cohorts
    from cfdinvest.livequotes.poller import poll_live_quotes
    from cfdinvest.persist import readers

    account = getattr(args, "account", None)
    strategy = app_config.strategy

    try:
        run_id = readers.load_latest_run(app_config, account=account)
        frame = (
            None if run_id is None
            else readers.load_frame("signals", run_id, app_config, account)
        )
    except Exception as error:  # noqa: BLE001
        logger.warning("Paper book: no signals to read (%s)", error)
        return EXIT_OK

    latest = []
    if frame is not None and not getattr(frame, "empty", True):
        working = frame.reset_index() if hasattr(frame, "reset_index") else frame
        if "timestamp" in working.columns:
            stamps = pd.to_datetime(working["timestamp"], utc=True, errors="coerce")
            working = working[stamps == stamps.max()]
        latest = working.to_dict("records")

    wanted = [str(row.get("ticker") or "") for row in latest if row.get("ticker")]
    # Anything already open needs a mark even if it no longer signals.
    from cfdinvest.advisor import papertrade

    wanted += list(papertrade.open_positions(papertrade.load_book(app_config, account)))
    # And the currency pairs, always. The £1,000 stake is exposure in
    # sterling, so a dollar-priced instrument needs the rate to size at all
    # -- without it the position is wrong by the rate itself.
    wanted += ["GBPUSD=X", "EURGBP=X", "USDJPY=X", "USDCAD=X", "USDCHF=X"]
    prices = {
        ticker: value
        for ticker, value in poll_live_quotes(sorted(set(wanted))).items()
        if value is not None
    }

    before = len(papertrade.load_book(app_config, account))
    summaries = run_all_cohorts(
        latest, prices, config=app_config, account=account,
    )

    # The events this pass appended, read back from the book rather than
    # returned by the pass: the book is what was actually recorded, and an
    # alert about something that failed to persist would be a lie about the
    # experiment.
    if not getattr(args, "no_email", False):
        fresh = papertrade.load_book(app_config, account)[before:]

        # One arm only. The book runs four thresholds side by side, and the
        # mail was carrying every event from all of them tagged by arm -- so
        # a single signal on ADBE arrived as four lines and read as the same
        # stock over and over. Sixteen mails in an afternoon, describing a
        # book nobody could trade: you can only hold one of four parallel
        # simulations.
        #
        # The baseline arm, read from the strategy's own entry bar rather
        # than named here. Hardcoding it is exactly how the arms drifted
        # from the engine once already.
        arm = cohorts_for(app_config.strategy)[0][0]
        fresh = [row for row in fresh if str(row.get("cohort") or "") == arm]

        if fresh:
            from cfdinvest.schedule.paper_alert import send_paper_alert

            if getattr(smtp, "send_paper_signals", True):
                send_paper_alert(fresh, config=smtp, transport=transport)

    for summary in summaries:
        print(
            f"Paper book [{summary['cohort']}]: opened {summary['opened']}, "
            f"closed {summary['closed']}, {summary['still_open']} open."
        )
    return EXIT_OK


def run_daemon_job(name, args, app_config, smtp=None, transport=None) -> None:
    """Run one daemon job by name.

    Module level, not a closure inside the loop. It was a closure, which
    made the dispatch untestable -- and the one bug it hid was a lake that
    silently stopped refreshing when the trading interval changed. A branch
    nothing can call is a branch nothing can check.
    """
    if name == "poll-live-quotes":
        _command_poll_live_quotes(
            _job_args(args, "poll-live-quotes"), app_config
        )
    elif name == "ingest-daily":
        # Ingestion alone, not a pipeline run with --refresh. The two are
        # separate jobs here precisely so the pipeline reads a lake that
        # ingestion has finished writing, and folding them back together
        # would put that ordering back inside one command.
        backtest = _backtest_config(args, app_config)
        repository = DataRepository(app_config)
        runner_module._refresh(
            repository, _with_fx_support(backtest.tickers), backtest
        )

        # And the daily bars, whatever interval the engine trades on.
        #
        # Every scheduled ingest follows config.market.interval, which is
        # now 1h -- so the moment the engine moved to hourly bars the
        # daily lake stopped being refreshed by anything and began
        # quietly going stale. Nothing broke, which is the problem: the
        # universe screen reads daily bars, and so does any analysis
        # wanting more than the two years the vendor serves intraday.
        #
        # Here rather than in the intraday job because a daily bar
        # changes once a day, and re-fetching it every fifteen minutes
        # would be asking the vendor for the same row ninety times a
        # session.
        # Resolved, not compared raw. `backtest.interval` is None when
        # nothing overrode it, meaning "whatever the config says" -- so
        # comparing it directly would top up even on a deployment already
        # trading daily bars, fetching the same rows twice.
        trading = backtest.interval or getattr(app_config.market, "interval", None)
        if trading != _SCREENING_INTERVAL:
            try:
                import dataclasses as _dc

                runner_module._refresh(
                    repository,
                    _with_screen_pool(
                        _with_fx_support(backtest.tickers), app_config
                    ),
                    _dc.replace(backtest, interval=_SCREENING_INTERVAL),
                )
            except Exception as error:  # noqa: BLE001
                logger.warning(
                    "Could not top up %s bars: %s", _SCREENING_INTERVAL, error
                )
    elif name == "ingest-intraday":
        # The same top-up, on the trading cadence. Separate from the
        # daily job because they do different work: this one keeps the
        # recent hourly bars current, the daily one refreshes the full
        # history and the macro series.
        backtest = _backtest_config(args, app_config)
        runner_module._refresh(
            DataRepository(app_config), _with_fx_support(backtest.tickers), backtest
        )
    elif name == "run-intraday":
        _command_run_once(
            _job_args(args, "run-once", refresh=False),
            app_config, smtp, transport,
        )
    elif name == "monitoring-snapshot":
        _command_monitor(
            _job_args(args, "monitor", alert=not args.no_email),
            app_config, smtp, transport,
        )
    elif name == "risk-snapshot":
        _command_risk_snapshot(_job_args(args, "risk-snapshot"), app_config)
    elif name == "advisor-alert":
        _command_advisor_alert(
            _job_args(args, "advisor-alert"), app_config, smtp, transport
        )
    elif name == "paper-book":
        _command_paper_book(
            _job_args(args, "poll-live-quotes"), app_config, smtp, transport
        )
    elif name == "prune-runs":
        _command_prune_runs(_job_args(args, "prune-runs"), app_config)
    elif name == "screen-universe":
        # Records what the rule says and nothing else. It cannot change the
        # traded universe -- the CLI it calls has no path that writes the
        # config, and a scheduled job that could rebalance on a ranking
        # never shown to predict return would be acting on more confidence
        # than the evidence carries.
        from cfdinvest.universe import cli as universe_cli

        universe_cli.main(
            ["screen"] + (["--account", args.account] if getattr(args, "account", None) else [])
        )


def _command_daemon(args, app_config, smtp, transport) -> int:
    """Every scheduled job, in one loop, until stopped.

    The decision about what is owed lives in
    :func:`cfdinvest.schedule.daemon.jobs_due` and is pure; this is the part
    that sleeps. A job that raises is logged and does not stop the loop --
    the whole point of a daemon is that tomorrow's run still happens after
    today's failed.
    """
    import time

    from cfdinvest.schedule import daemon as daemon_module

    from cfdinvest.persist import scheduler_state

    account = getattr(args, "account", None)

    # Seeded from disk, not empty. An empty map makes every job due at once,
    # so a restart re-ran the daily pipeline immediately -- and with a deploy
    # restarting the daemon, that happened on every push.
    try:
        last_run: dict[str, object] = scheduler_state.last_job_runs(
            app_config, account
        )
    except Exception as error:  # noqa: BLE001 - a daemon must still start
        logger.warning("Could not read what ran last: %s", error)
        last_run = {}
    ticks = 0

    run_job = lambda name: run_daemon_job(  # noqa: E731
        name, args, app_config, smtp, transport
    )

    print(
        "cfdinvest scheduler daemon. Quotes every "
        f"{daemon_module.QUOTE_INTERVAL_MINUTES} minutes; signals every "
        f"{daemon_module.INTRADAY_MINUTES} minutes while a market is open; "
        "daily top-up "
        f"{daemon_module.INGEST_AT_UTC[0]:02d}:{daemon_module.INGEST_AT_UTC[1]:02d} "
        "UTC; snapshots "
        f"{daemon_module.SNAPSHOT_AT_UTC[0]:02d}:"
        f"{daemon_module.SNAPSHOT_AT_UTC[1]:02d} UTC."
    )
    try:
        while args.max_ticks is None or ticks < args.max_ticks:
            now = pd.Timestamp.now(tz="UTC")
            # Asked once per tick and passed in, so jobs_due stays pure.
            # A calendar that cannot answer is treated as closed: trading on
            # a guess about whether the market is open is worse than pausing.
            try:
                from cfdinvest.schedule import scheduler as scheduler_module

                market_open = bool(
                    scheduler_module.any_open(list(app_config.market.tickers), now)
                )
            except Exception as error:  # noqa: BLE001
                logger.warning("Could not read the market calendar: %s", error)
                market_open = False

            for name in daemon_module.jobs_due(now, last_run, market_open=market_open):
                logger.info("Daemon: running %s", name)
                try:
                    run_job(name)
                except Exception as error:  # noqa: BLE001 - one job, not the loop
                    logger.exception("Daemon: %s failed: %s", name, error)
                # Recorded whether or not it succeeded. A job that fails every
                # time must not be retried every tick -- that is a hot loop
                # against someone else's server, and the failure is already
                # in the log and the monitoring snapshot.
                stamp = pd.Timestamp.now(tz="UTC")
                last_run[name] = stamp
                try:
                    scheduler_state.record_job(
                        name, stamp, config=app_config, account=account
                    )
                except Exception as error:  # noqa: BLE001 - bookkeeping only
                    logger.warning("Could not record %s: %s", name, error)
            ticks += 1
            if args.max_ticks is not None and ticks >= args.max_ticks:
                break
            time.sleep(max(1, int(args.tick_seconds)))
    except KeyboardInterrupt:
        print("\nStopped.")
    return EXIT_OK


#: Options defined on the top-level parser, so they mean the same thing to
#: every subcommand. Everything else belongs to one subcommand and must come
#: from that subcommand's own defaults.
GLOBAL_OPTIONS: tuple[str, ...] = (
    "config", "data_root", "tickers", "interval", "start", "end",
    "log_level", "dry_run", "no_email",
)


def _job_args(args, command: str, **changes):
    """An argument namespace for ``command``, built from its own defaults.

    Each subcommand defines its own flags -- run-once has ``--force``,
    monitor has ``--no-store``, risk-snapshot has ``--balance`` -- and the
    daemon parser defines none of them. Handing a job the daemon's own
    namespace raised ``AttributeError`` on the first run of all three.

    The loop caught each one, as it is meant to, so the service stayed up,
    logged a failure a day and did no work. Error containment turning a hard
    failure into a silent one is the thing to watch for here: the daemon
    looked healthy the whole time.

    Defaults come from the parser rather than a list kept here, so a flag
    added to any subcommand is picked up without this function changing.
    """
    namespace = build_parser().parse_args([command])
    for name in GLOBAL_OPTIONS:
        if hasattr(args, name):
            setattr(namespace, name, getattr(args, name))
    # The daemon's own --account applies to whichever job reads one.
    account = getattr(args, "account", None)
    if account is not None and hasattr(namespace, "account"):
        namespace.account = account
    for name, value in changes.items():
        setattr(namespace, name, value)
    return namespace


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


_MONITOR_MARK = {"green": "OK", "yellow": "CHECK", "red": "PROBLEM"}


def _print_monitoring(snapshot) -> None:
    """One pass, as the operator reads it."""
    print(
        f"{snapshot.timestamp:%Y-%m-%d %H:%M} UTC  "
        f"[{_MONITOR_MARK.get(snapshot.overall_status, '?')}]  {snapshot.headline}"
    )
    for issue in snapshot.issues:
        print(f"  [{issue.severity:8}] {issue.type:9} {issue.message}")
        if issue.remedy:
            print(f"               -> {issue.remedy}")


def _command_monitor(args, app_config, smtp, transport) -> int:
    """Report the machine's health. Runs no pipeline and moves no position."""
    import time

    from cfdinvest.monitoring import run_monitoring

    passes = 0
    snapshot = None
    while True:
        snapshot = run_monitoring(
            app_config,
            account=args.account,
            notify_state=notify_module.delivery_state(smtp),
            store=not args.no_store,
        )
        _print_monitoring(snapshot)
        if args.alert and snapshot.overall_status == "red":
            notify_module.send_monitoring_alert(snapshot, smtp, transport)

        passes += 1
        if not args.watch:
            break
        if args.max_passes is not None and passes >= args.max_passes:
            break
        time.sleep(max(1.0, args.interval_minutes * 60.0))

    return EXIT_OK if snapshot.overall_status != "red" else EXIT_FAILED


def _print_risk(observation) -> None:
    """One observation, as the operator reads it."""
    snapshot = observation.snapshot
    marks = {"ok": "OK", "warning": "CHECK", "critical": "BREACH"}
    # Which book, always. A reading of the paper simulation and a reading of
    # the account you actually traded are different statements, and a line
    # that does not say which invites the reader to assume the worrying one.
    book = snapshot.book or "no book recorded"
    print(
        f"{observation.at:%Y-%m-%d %H:%M} UTC  "
        f"[{marks.get(snapshot.severity, snapshot.severity)}]  {snapshot.message}"
        f"  ({book})"
    )
    for flag in snapshot.flags:
        print(f"  ! {flag}")
    # Marked differently from a breach on purpose: this is what the
    # simulated book would be doing, not what the account is doing.
    for note in getattr(snapshot, "advisory", ()):
        print(f"  ~ simulated: {note}")
    for item in snapshot.unavailable:
        print(f"  ? could not check: {item}")
    if observation.stored_path:
        print(f"  recorded: {observation.stored_path}")


def _command_risk_snapshot(args, app_config) -> int:
    """Report the book's risk. Runs no pipeline and moves no position."""
    from cfdinvest.risk import RiskLimits, RiskMonitor, RiskMonitorConfig

    limits = (
        RiskLimits(account_balance_gbp=args.balance)
        if args.balance is not None
        else None
    )
    settings = RiskMonitorConfig(
        interval_minutes=args.interval_minutes,
        store=not args.no_store,
        max_observations=args.max_observations if args.watch else 1,
    )
    monitor = RiskMonitor(
        app_config,
        settings,
        account=args.account,
        limits=limits,
        on_observation=_print_risk,
    )

    if not args.watch:
        observation = monitor.observe()
        if observation is None:
            print("Could not read the artefacts.", file=sys.stderr)
            return EXIT_FAILED
        return EXIT_OK if observation.severity != "critical" else EXIT_FAILED

    observations = monitor.run_forever()
    if not observations:
        return EXIT_FAILED
    return EXIT_OK if observations[-1].severity != "critical" else EXIT_FAILED


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
        if args.command == "monitor":
            return _command_monitor(args, app_config, smtp, transport)
        if args.command == "risk-snapshot":
            return _command_risk_snapshot(args, app_config)
        if args.command == "send-test-email":
            return _command_send_test_email(args, smtp, transport)
        if args.command == "market-status":
            return _command_market_status(args, app_config)
        if args.command == "crontab":
            return _command_crontab(args, app_config)
        if args.command == "advisor-alert":
            return _command_advisor_alert(args, app_config, smtp, transport)
        if args.command == "prune-runs":
            return _command_prune_runs(args, app_config)
        if args.command == "poll-live-quotes":
            return _command_poll_live_quotes(args, app_config)
        if args.command == "paper-book":
            return _command_paper_book(args, app_config, smtp, transport)
        if args.command == "daemon":
            return _command_daemon(args, app_config, smtp, transport)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return EXIT_FAILED

    print(f"Unknown command {args.command!r}", file=sys.stderr)
    return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
