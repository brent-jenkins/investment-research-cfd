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


#: The last universe announced, so the daemon says it once per change
#: rather than twice a minute. Process-local and deliberately not
#: persisted: a restart re-announcing what it is trading is useful.
_LAST_UNIVERSE: tuple | None = None


def _traded(app_config, account=None) -> list[str]:
    """What to trade, from the screen when permitted and the config always.

    One owner. ``market.tickers`` was read directly in seven places, so
    following the screen would have meant seven edits and the certainty
    that a later one would be missed -- the same defect that had the cost
    gate reading an empty store while a full one sat beside it.
    """
    from cfdinvest.universe.traded import traded_universe

    resolved = traded_universe(app_config, account)
    # Logged on change, not on every resolution. This is called from every
    # job on the fifteen-minute cadence and several times within each, so
    # announcing it unconditionally wrote the same line twice a minute --
    # which is how a log stops being read, and would bury the one message
    # that matters: the day the list actually moves.
    if resolved.source != "config":
        global _LAST_UNIVERSE
        signature = (resolved.source, resolved.as_of, resolved.names)
        if signature != _LAST_UNIVERSE:
            _LAST_UNIVERSE = signature
            logger.info(
                "Traded universe: %s -- %s",
                resolved.describe(), ", ".join(resolved.names),
            )
    return list(resolved.names)


def _ever_selected(config, account=None) -> list[str]:
    """Every instrument any recorded screen has ever selected.

    Data collection outlives membership. A name that leaves the traded list
    is not a name we stop caring about: it may return next month, its
    history is what the screen ranks it on, and a gap in the middle of that
    history is not recoverable later -- the vendor serves what it serves
    today, and a bar not fetched while it existed is gone.

    The selections record is the right source because it is append-only and
    already holds every decision the screen has made. Order is preserved so
    the current list stays at the front of any fetch.

    A genuinely delisted instrument simply stops returning data. Ingestion
    reports that per-symbol without taking the rest of the fetch down, which
    is the correct behaviour here -- marking it as delisted, rather than
    merely absent, is a separate question and deliberately not answered by
    this function.
    """
    try:
        from cfdinvest.universe.screen import load_screens

        rows = load_screens(config, account)
    except Exception as error:  # noqa: BLE001 - collection is best-effort
        logger.debug("Selection history unreadable (%s); not extending", error)
        return []
    names: list[str] = []
    seen: set[str] = set()
    for row in rows or ():
        for ticker in (row or {}).get("selected") or ():
            name = str(ticker)
            if name and name not in seen:
                seen.add(name)
                names.append(name)
    return names


def _with_held(tickers, config, account=None) -> list[str] | None:
    """The traded list plus anything still open that has left it.

    A name dropped from the universe is not a name whose position should be
    abandoned. Without this it gets no signal row, the paper pass reads its
    direction as ``flat``, and ``time_stop_days`` is 0.29 -- one US session
    -- so ``signal gone`` closes it on the next open. Every position in a
    departing instrument would be liquidated within a session of the change
    for no reason the strategy ever gave.

    Keeping it in the signal universe lets it exit on its own terms: a
    reversal, or its own time stop, the same way it would have if the
    universe had never changed. It does not let it be re-entered -- see
    the traded-universe rule, which restricts *opening* to the
    configured list. Running a position out and taking a new one are
    different acts and only the first is wanted here.

    Order preserved and duplicates dropped, so the traded names keep their
    place at the front. ``None`` in, ``None`` out.
    """
    if tickers is None:
        return None
    names = [str(t) for t in tickers if str(t)]
    seen = set(names)

    # Both books, because they hold different things and either can carry a
    # position in a departed instrument.
    #
    # The confirm layer alone is not enough and shipping it that way made
    # this inert: it records what has been confirmed by hand, and in this
    # deployment nothing ever has -- it reads zero rows while the paper book
    # holds fourteen positions. A departing instrument would have gone
    # unsignalled, read as flat, and been closed by the 0.29-day time stop
    # inside a session, which is the exact liquidation this exists to stop.
    for source in (_confirmed_instruments, _book_instruments):
        try:
            found = source(config, account)
        except Exception as error:  # noqa: BLE001 - never block a run on this
            logger.warning("Could not read held instruments (%s): %s",
                           source.__name__, error)
            continue
        for ticker in found or ():
            name = str(ticker)
            if name and name not in seen:
                seen.add(name)
                names.append(name)
    return names


def _screen_candidates(config) -> list[str]:
    """Every instrument the screen may rank, plus everything already ingested.

    **Once a series is being collected it is never dropped.** Owner's
    instruction, and it is stronger than "every current candidate": a name
    removed from ``universe.candidates`` would otherwise stop being fetched
    and its history would acquire a hole exactly where somebody later wants
    to know what happened. Holes cannot be backfilled once the vendor's
    retention window has passed, and the whole point of a lake is that the
    past stays available.

    So the set is the configured candidates *union what is already on disk*,
    which makes it monotonic under configuration changes.

    **The one exception is a series that has genuinely ended** -- delisted,
    suspended, or the vendor simply stopping. That is not the same as a name
    leaving the traded list, and it is the only reason to stop fetching:
    there is nothing left to fetch. Such a name is recorded in
    ``metadata/delisted.json`` and skipped here, so it does not cost a
    failed request on every tick forever.

    Marking is deliberate rather than automatic. "No new bars today" is what
    a market holiday, a data outage and a delisting all look like from here,
    and inferring death from silence would quietly stop collecting a live
    instrument -- the failure this rule exists to prevent, arrived at from
    the other side. Until a detector exists that can tell those apart, a
    dead name costs one logged line per tick, which is the cheaper mistake.

    Order is not preserved against the traded list by design -- the caller
    appends these after the names it already cares about, so a partial
    failure degrades the candidates rather than the book.
    """
    names: list[str] = []
    seen: set[str] = set()
    for ticker in (
        getattr(getattr(config, "universe", None), "candidates", ()) or ()
    ):
        name = str(ticker)
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    for name in _already_ingested(config):
        if name not in seen:
            seen.add(name)
            names.append(name)
    return names


def _already_ingested(config) -> list[str]:
    """Instruments the decision feed already holds bars for.

    Read from the lake rather than from configuration, because the point is
    to survive a configuration change. Never raises: an unreadable directory
    means the configured candidates alone, which is the old behaviour.
    """
    from pathlib import Path

    try:
        root = Path(getattr(getattr(config, "paths", None), "data_root", "data"))
        interval = getattr(getattr(config, "market", None), "interval", None)
        if not interval:
            return []
        folder = root / "raw" / "market" / str(interval)
        if not folder.is_dir():
            return []
        dead = _delisted(config)
        return sorted(
            path.stem for path in folder.glob("*.parquet")
            if path.stem and "=X" not in path.stem and path.stem not in dead
        )
    except Exception as error:  # noqa: BLE001 - collection is best-effort
        logger.warning("Could not list what is already ingested: %s", error)
        return []


def _delisted(config) -> frozenset[str]:
    """Instruments whose series has ended, and which are not worth fetching.

    Read from ``metadata/delisted.json``: either a list of tickers or an
    object keyed by ticker (so a reason and a date can be recorded beside
    it). Absent means none, which is the common case.

    Never raises. An unreadable file means nothing is skipped, which errs
    towards collecting too much rather than too little -- the right
    direction, since a missed bar cannot be recovered later and a wasted
    request can.
    """
    import json
    from pathlib import Path

    try:
        root = Path(getattr(getattr(config, "paths", None), "data_root", "data"))
        path = root / "metadata" / "delisted.json"
        if not path.is_file():
            return frozenset()
        blob = json.loads(path.read_text(encoding="utf-8"))
        names = blob.keys() if isinstance(blob, dict) else blob
        return frozenset(str(n) for n in (names or ()) if str(n))
    except Exception as error:  # noqa: BLE001
        logger.warning("Could not read the delisted list: %s", error)
        return frozenset()


def _confirmed_instruments(config, account=None) -> list[str]:
    """Anything with a confirmed position on the real account."""
    from cfdinvest.livequotes.poller import held_instruments

    return list(held_instruments(config, account) or ())


def _book_instruments(config, account=None) -> list[str]:
    """Anything the engine currently intends to hold.

    Named for the book it used to read; it now reads ``persist.intent``,
    which is the engine's own final bar. The book applied a different rule
    set, so "what is held" taken from it named instruments the strategy had
    not chosen and missed ones it had.
    """
    from cfdinvest.persist import intent

    return list(intent.held_tickers(config, account) or ())

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


def _execution_config(tickers, strategy=None, config=None, account=None):
    """The cost model, built from what the venue being traded charges.

    The engine has always been able to take per-asset financing rates and a
    currency map; nothing populated them, so every run financed the whole
    universe at a flat 8% long and *earned* 2% short. Charging both sides on
    everything recorded was worth £24,666 of understated cost over the
    2005-2026 run, most of it from shorts that were being paid to exist.

    **The venue is now Capital**, because that is where the orders go.
    Financing at 7.86%/yr long against the 25.33% previously charged is a
    factor of 3.2 on the dominant cost line, and an engine costed on a
    broker the account does not use produces signals sized for the wrong
    world. The rates come from ``config/capital_rules.toml``, read from the
    broker's API, and no network is touched here -- this runs long before
    market open and must not depend on a broker being awake.

    The financing figure is corroborated rather than confirmed; see the
    terms file. Anything with no recorded pair keeps the house rate, logged
    by name rather than absorbed, because a run financed partly at venue
    rates and partly at a default has a total that is neither and looks like
    neither.
    """
    from cfdinvest.config import StrategyConfig
    from cfdinvest.execution import ExecutionConfig
    from cfdinvest.execution.fx import FXConfig
    from cfdinvest.execution.state import HysteresisThresholds
    from cfdinvest.advisor.capital_venue import static_rules
    from cfdinvest.ingestion.earnings import load_earnings_dates
    from cfdinvest.livehelper import broker_costs

    strategy = strategy or StrategyConfig()
    universe = list(tickers or ())
    try:
        # `broker_costs` was already venue-agnostic -- every reader takes an
        # optional table and only the default was tied to one broker. So the
        # switch is which table is handed in, not new machinery.
        venue_table = static_rules(universe)
        rates = broker_costs.financing_overrides(universe, venue_table)
        currencies = broker_costs.instrument_currencies(universe, venue_table)
        fee_flags = broker_costs.fx_fee_flags(universe, venue_table)
        missing = broker_costs.unpriced(universe, venue_table)
    except Exception as error:  # noqa: BLE001 - an unreadable table is not an outage
        logger.warning(
            "Could not read the broker rules; financing falls back to the "
            "house rate for the whole universe: %s", error
        )
        return ExecutionConfig(
            max_leverage=strategy.max_leverage,
            concentration=strategy.max_instrument_concentration,
            slippage_fraction=strategy.slippage_bps / 10_000.0,
            # Carried onto the fallback deliberately: a failure to read
            # financing rates is no reason to also stop charging the spread
            # the broker quoted, and a cost model that changes silently when
            # an unrelated read fails is the harder bug to find.
            use_measured_spreads=getattr(strategy, "use_measured_spreads", False),
        confidence_sizing_strength=getattr(
            strategy, "confidence_sizing_strength", None
        ),
        confidence_sizing_floor=getattr(
            strategy, "confidence_sizing_floor", 0.25
        ),
        )

    if missing:
        logger.warning(
            "No recorded financing for %d of %d instruments, charged at the "
            "house rate instead: %s",
            len(missing), len(universe), ", ".join(missing),
        )
    logger.info(
        "Financing %d instruments at Capital's recorded rates (read %s, "
        "confirmed against a charge: %s); %d of %d pay a conversion fee.",
        len(rates), getattr(venue_table, "read_on", "") or "unknown",
        getattr(venue_table, "financing_verified", "unknown"),
        sum(fee_flags.values()), len(fee_flags),
    )

    # Passed the config deliberately. Without it `_path` resolves to the OS
    # application-data directory rather than the configured lake, so the
    # loader looked in a place nothing writes, found nothing, and returned
    # an empty mapping -- which the engine reads as "no instrument is
    # blacked out". The blackout was inert from the day it was written,
    # whatever `earnings_blackout_days` said, and it failed open in silence
    # because the absence is logged at debug.
    #
    # This is the failure the house rule about naming absence exists for: a
    # rule that cannot run should say so, not report the same thing as a
    # rule that ran and found nothing to do.
    try:
        earnings = load_earnings_dates(config, account)
    except Exception as error:  # noqa: BLE001 - no dates is not an outage
        logger.warning("Could not read earnings dates: %s", error)
        earnings = {}
    if strategy is not None and getattr(strategy, "earnings_blackout_days", 0):
        if not earnings:
            logger.warning(
                "Earnings blackout is configured at %s days but no reporting "
                "dates could be read: the rule will not bite.",
                strategy.earnings_blackout_days,
            )
        else:
            missing = [t for t in (tickers or ()) if str(t) not in earnings]
            if missing:
                logger.warning(
                    "Earnings blackout has no dates for %d of %d instruments: %s",
                    len(missing), len(tickers or ()), ", ".join(sorted(missing)),
                )

    return ExecutionConfig(
        financing_rates=rates or None,
        max_leverage=strategy.max_leverage,
        concentration=strategy.max_instrument_concentration,
        slippage_fraction=strategy.slippage_bps / 10_000.0,
        rebalance_threshold=strategy.rebalance_threshold,
        exposure_per_unit_size=strategy.exposure_per_unit_size,
        reentry_cooldown_days=strategy.reentry_cooldown_days,
        time_stop_days=strategy.time_stop_days,
        earnings_dates=earnings or None,
        earnings_blackout_days=strategy.earnings_blackout_days,
        decayed_position_multiplier=getattr(
            strategy, "decayed_position_multiplier", None
        ),
        breakout_lookback=getattr(strategy, "breakout_lookback", None),
        use_measured_spreads=getattr(strategy, "use_measured_spreads", False),
        confidence_sizing_strength=getattr(
            strategy, "confidence_sizing_strength", None
        ),
        confidence_sizing_floor=getattr(
            strategy, "confidence_sizing_floor", 0.25
        ),
        thresholds=HysteresisThresholds(
            # The size pair is passed explicitly for the same reason the
            # confidence pair is: left to the dataclass default it was the
            # strategy's real entry bar with no owner, and no sweep could
            # reach it.
            entry_size=getattr(strategy, "entry_size", 0.10),
            exit_size=getattr(strategy, "exit_size", 0.05),
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
    universe = _with_held(
        _universe(args) or _traded(app_config, getattr(args, "account", None)) or None,
        app_config,
        getattr(args, "account", None),
    )
    return BacktestConfig(
        tickers=universe,
        series_ids=list(app_config.macro.series_ids) or None,
        interval=args.interval,
        start=args.start,
        end=args.end,
        execution=_execution_config(
            universe, app_config.strategy, app_config,
            getattr(args, "account", None),
        ),
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


def _engine_rows(app_config, account=None) -> list[dict]:
    """What the engine says to hold. See ``persist.intent`` for the why.

    Kept as a thin name here because the dealing pass reads better for it;
    the logic has one owner and it is not this module.
    """
    from cfdinvest.persist import intent

    return intent.intended_positions(app_config, account)


def _size_engine_rows(rows, prices) -> list[dict]:
    """Fill each row's share of the balance, once marks are known."""
    from cfdinvest.persist import intent

    return intent.apply_prices(rows, prices)


def _with_exits(rows, held_positions) -> list[dict]:
    """The engine's positions, plus a flat row for anything it has exited.

    ``intended_positions`` reports what is *held*, so an instrument the
    engine has closed is simply absent from it -- and ``plan_orders``
    iterates the rows it is given, so an absent instrument gets no intent,
    no close order, and stays open on the account forever while financing
    accrues.

    Measured rather than assumed. With the broker holding AAPL and the
    engine's rows empty the bridge planned *nothing at all*; the same case
    with an explicit flat row planned ``close``. The bridge was never
    wrong -- it was never told.

    **This is not a force-close, and the distinction is the whole point.**
    The engine keeps signalling anything it holds (``_with_held``), so a
    name that has left the traded list still exits on its own reversal or
    its own time stop, exactly as it would have had the universe never
    changed. What crosses to the broker here is that decision, after the
    engine has taken it. Closing a position the engine still wants is a
    different act entirely, and it is what ``max_open_positions`` was
    stopped from doing on 2026-08-29.

    ``rows`` must be a read, never ``None``. An unreadable run would
    otherwise synthesise a flat row for every open position and liquidate
    the book on a missing artefact, so the caller checks first and this
    refuses to guess.
    """
    if rows is None:
        raise ValueError("cannot derive exits from an unreadable run")

    intended = {str((row or {}).get("ticker") or "") for row in rows}
    exited = []
    for position in held_positions or ():
        epic = str(getattr(position, "epic", "") or "")
        if epic and epic not in intended:
            exited.append(epic)
            # Flat rather than sized: the bridge reads direction and share
            # and plans the close itself, so no rule is re-stated here.
            rows.append({
                "ticker": epic, "direction": "flat", "size": 0.0,
                "units": 0.0, "confidence": None,
            })
    if exited:
        logger.info(
            "Capital: %d position(s) the engine has exited and the account "
            "still holds; closing on the engine's own decision: %s",
            len(exited), ", ".join(sorted(exited)),
        )
    return rows


def _dealing_tickers(tickers, book_rows, held_positions) -> list[str]:
    """The traded list, plus everything actually held.

    The traded list says what may be *opened*. What may be *closed* is
    whatever the broker says is open, and that is the broker's fact rather
    than a configuration value -- so the two sets are fetched together and
    only the opening side is filtered later.

    Nine positions were stranded on a funded demo account for want of this.
    Narrowing the universe to eight names left AAPL, GOOGL, META, MSFT,
    ORCL, ADBE, XOM, NKE and COST held with no market detail; every pass
    refused them for "no price", so nothing could ever close them. They
    were not liquidated on departure -- correct, that is the run-out rule --
    but they could not exit on their own merits either, and financing
    accrued on all nine regardless of what their signals said.

    The same principle as ``_with_held`` one layer up: leaving the list
    stops re-entry and nothing else.
    """
    names = {str(t) for t in (tickers or ()) if t}
    for row in book_rows or ():
        ticker = str((row or {}).get("ticker") or "")
        if ticker:
            names.add(ticker)
    for position in held_positions or ():
        epic = str(getattr(position, "epic", "") or "")
        if epic:
            names.add(epic)
    return sorted(names)


def _command_trade_capital(args, app_config, smtp=None, transport=None) -> int:
    """One pass of the demo account: decide, then place what survives.

    The decision is :func:`cfdinvest.broker.capital.bridge.plan_orders`,
    which is pure and takes no network. This reads what it needs, hands it
    over, and acts on what comes back -- so the thing that decides and the
    thing that can lose money stay separate.

    Never raises. A failed pass leaves the account exactly as it was, which
    is the whole reason the planner refuses rather than guessing.
    """
    from cfdinvest.advisor import livecheck
    from cfdinvest.broker.capital import bridge
    from cfdinvest.broker.capital import client as capital
    from cfdinvest.broker.capital import dealing as dealing_module
    from cfdinvest.persist import orders as order_store
    from cfdinvest.persist import readers
    from cfdinvest.risk import RiskLimits
    from cfdinvest.schedule.market_calendar import describe

    account_name = getattr(args, "account", None)
    tickers = _traded(app_config, account_name)
    if not tickers:
        return EXIT_OK

    settings = dealing_module.DealingConfig.from_env()
    if not settings.broker.complete:
        logger.debug("No Capital.com credentials; not trading")
        return EXIT_OK

    refusal = settings.refusal()
    if refusal:
        # Not a warning. Dealing being off is the normal, safe state, and a
        # message every fifteen minutes about a feature nobody switched on
        # is how a log stops being read.
        logger.debug("Not trading: %s", refusal)
        return EXIT_OK

    session = capital.open_session(settings.broker)
    if not session.ok:
        logger.warning("Capital: could not authenticate: %s", session.error)
        return EXIT_OK

    account = capital.fetch_account(session, settings.broker)
    if not account.ok:
        logger.warning("Capital: no balance (%s); nothing can be sized", account.error)
        return EXIT_OK

    held, held_error = dealing_module.list_positions(session, settings)
    if held_error:
        # An unreadable book is not an empty one, and only one of those
        # means it is safe to open more.
        logger.warning("Capital: positions unreadable (%s); not trading", held_error)
        return EXIT_OK

    positions = {
        p.epic: {"size": p.size, "direction": p.direction, "deal_id": p.deal_id}
        for p in held
    }
    placed = order_store.open_orders(app_config, account_name)

    # What the account should hold comes from the ENGINE, read back from the
    # run the intraday job already persisted.
    #
    # It used to come from the shadow book, on the reasoning that the book
    # had already applied the entry bar, the cooldown, the blackout and the
    # time stop, so re-deriving them here would be a second decision path.
    # The reasoning was right and the premise was wrong: the book applies
    # those four and *none* of the breakout gate, the size hysteresis, the
    # decayed trim, the leverage cap or the concentration cap. So the second
    # decision path existed anyway, and the account has been trading it --
    # 163.7% at -52.8% where the engine returns 130.5% at -40.3%, measured
    # 2026-08-30.
    #
    # Read rather than recomputed: run-intraday persists the pipeline and
    # this reads its final bar, so there is still exactly one place where a
    # position is decided.
    latest = _engine_rows(app_config, account_name)

    # None and [] mean different things here and collapsing them liquidates
    # the account. ``intended_positions`` returns None when no run could be
    # read and [] when a run was read and holds nothing -- "unchecked"
    # against "flat", the project's oldest distinction, at the one call site
    # where getting it wrong places orders. An unreadable run must leave the
    # book exactly as it is.
    if latest is None:
        logger.warning(
            "Capital: no readable run to mirror; leaving the book untouched"
        )
        return EXIT_OK
    if not latest:
        logger.info("Capital: the engine holds nothing; nothing to mirror")

    # An exit the engine has already taken has to reach the broker.
    latest = _with_exits(latest, held)

    # Price the traded list *and* everything actually held. These are not
    # the same set, and treating them as one stranded nine positions on a
    # funded demo account: narrowing the universe left AAPL, GOOGL, META,
    # MSFT, ORCL, ADBE, XOM, NKE and COST held with no market detail, so
    # every pass refused them for "no price" and nothing could ever close
    # them. They were not closed on departure -- correctly, that is the
    # run-out rule -- but they could not be closed on their own merits
    # either, which is the half nobody asked for.
    #
    # The traded list says what may be *opened*. What may be *closed* is
    # whatever the broker says is open, and that is the broker's fact, not
    # a configuration value. Same principle as ``_with_held`` one layer up.
    dealing_tickers = _dealing_tickers(tickers, latest, held)
    stranded = sorted(set(dealing_tickers) - set(tickers))
    if stranded:
        logger.info(
            "Capital: pricing %d instrument(s) held outside the traded list "
            "so they can still be closed on their own merits: %s",
            len(stranded), ", ".join(stranded),
        )

    markets = {
        m.ticker: m
        for m in capital.fetch_market_details(
            dealing_tickers, session, settings.broker
        )
    }

    # Now that the instruments are priced, turn the engine's units into the
    # share of balance the bridge sizes from. Done here rather than earlier
    # because the list to price is not known until the rows are read.
    # CapitalMarket carries bid and offer, not a mid -- asking it for one
    # returns None for every instrument and sizes nothing, silently.
    _size_engine_rows(latest, {
        name: (
            (float(detail.bid) + float(detail.offer)) / 2.0
            if getattr(detail, "bid", None) and getattr(detail, "offer", None)
            else None
        )
        for name, detail in markets.items()
    })

    now = pd.Timestamp.now(tz="UTC")
    try:
        calendar = describe(dealing_tickers, now)
        session_open = {
            str(i): bool(v) for i, v in calendar["is_open"].items()
        }
        # Whether each market was *also* open this many minutes ago. If it
        # was not, we are inside the opening window and no new position is
        # taken. Asked of the calendar rather than computed from a session
        # start, so daylight saving, holidays and per-instrument sessions
        # are handled by the thing that already knows about them.
        delay = int(getattr(app_config.strategy, "open_delay_minutes", 0) or 0)
        settled = (describe(dealing_tickers, now - pd.Timedelta(minutes=delay))
                   if delay else None)
        open_allowed = {
            ticker: (settled is None or bool(settled["is_open"].get(ticker, False)))
            for ticker in session_open
        }
    except Exception as error:  # noqa: BLE001 - an unknown calendar is closed
        logger.warning("Capital: no session calendar (%s); trading nothing", error)
        session_open = {}
        open_allowed = {}

    # The last look, over the instruments that might open. Priced from the
    # broker's own mid, which is the price an order would actually meet.
    candidates = [
        str(row.get("ticker") or "") for row in latest
        if str(row.get("ticker") or "") not in positions
        and session_open.get(str(row.get("ticker") or ""), False)
    ]
    prices = {
        t: (markets[t].bid + markets[t].offer) / 2
        for t in candidates
        if t in markets and markets[t].ok
    }
    rechecked = (
        livecheck.reevaluate(candidates, prices, config=app_config, at=now)
        if candidates else {}
    )

    fx = _capital_fx_rate(markets, account.currency)

    report = bridge.plan_orders(
        latest, markets, positions,
        balance=account.balance,
        limits=RiskLimits.for_balance(
            account.balance,
            max_leverage=app_config.strategy.max_leverage,
            concentration=app_config.strategy.max_instrument_concentration,
        ),
        exposure_per_unit_size=app_config.strategy.exposure_per_unit_size,
        rebalance_threshold=_execution_config(tickers, app_config.strategy)
        .rebalance_threshold,
        session_open=session_open,
        placed=placed,
        fx_rate=fx,
        recheck=True,
        rechecked=rechecked,
        open_allowed=open_allowed,
        max_opens_per_pass=app_config.strategy.max_opens_per_pass,
        entry_confidence=app_config.strategy.entry_confidence,
    )

    order_store.record_plan(report, app_config, account_name, now=now)

    if report.halted:
        logger.warning("Capital: NOT TRADING -- %s", report.halt_reason)
        if not getattr(args, "no_email", False):
            _send_bridge_halt(report, smtp, transport)
        return EXIT_OK

    opened = closed = resized = 0
    outcomes: list[tuple] = []
    for intent in report.actionable:
        if intent.action == "close":
            result = dealing_module.close_position(intent.deal_id, session, settings)
            closed += 1 if result.ok else 0
        else:
            result = dealing_module.place_position(
                intent.epic,
                "BUY" if intent.target_units > 0 else "SELL",
                abs(intent.target_units - intent.held_units)
                if intent.action == "resize" else abs(intent.target_units),
                session, settings,
            )
            if result.ok:
                opened += 1 if intent.action == "open" else 0
                resized += 1 if intent.action == "resize" else 0
        order_store.record_result(intent, result, app_config, account_name, now=now)
        outcomes.append((intent, result))
        if result.error:
            logger.warning(
                "Capital: %s %s failed: %s", intent.action, intent.ticker, result.error
            )

    refused_intents = [i for i in report.intents if i.action == "refused"]
    if outcomes or refused_intents:
        if not getattr(args, "no_email", False):
            _send_capital_trades(
                outcomes, refused_intents, account, settings.broker.environment,
                smtp, transport,
            )

    refused = len(refused_intents)
    logger.info(
        "Capital: opened %d, resized %d, closed %d, refused %d, balance %.2f",
        opened, resized, closed, refused, account.balance or 0.0,
    )
    return EXIT_OK


def _capital_fx_rate(markets, account_currency: str) -> float:
    """Account currency per unit of the instrument currency.

    Read from the instruments themselves rather than assumed. Everything in
    this universe is USD against a GBP account, and getting it upside down
    misstates every position by the square of the rate.
    """
    from cfdinvest.livequotes.poller import poll_live_quotes

    currencies = {
        getattr(m, "currency", "") for m in markets.values() if getattr(m, "ok", False)
    }
    if not currencies or currencies <= {account_currency, ""}:
        return 1.0
    quotes = poll_live_quotes(["GBPUSD=X"])
    rate = quotes.get("GBPUSD=X")
    return 1.0 / float(rate) if rate else 1.0


def _plain_message(subject: str, lines, smtp=None):
    """A plain-text email in the shape ``notify.send`` takes.

    Every helper in ``notify`` composes one specific report, so there was no
    way to send an arbitrary body. Two call sites here invented
    ``notify.Message``, which does not exist, and both caught the resulting
    AttributeError and logged it -- so the halt alert, whose entire job is
    to be noticed, has never once sent.
    """
    from email.message import EmailMessage

    from cfdinvest.schedule.notify import SmtpConfig

    settings = smtp or SmtpConfig.from_env()
    message = EmailMessage()
    message["Subject"] = f"[CFDInvest] {subject}"
    message["From"] = getattr(settings, "sender", None) or "cfdinvest@localhost"
    message["To"] = ", ".join(getattr(settings, "recipients", ()) or [])
    message.set_content("\n".join(str(line) for line in lines))
    return message


def _send_capital_trades(
    outcomes, refused, account, environment, smtp, transport
) -> None:
    """What the account did this pass. Never raises.

    Distinct from the shadow book's mail, which reports what the simulation
    decided. The two are usually the same and the interesting moments are
    exactly when they are not: an order the broker refused, or accepted and
    never confirmed, appears here and nowhere else.

    A failure is reported as loudly as a fill. A mail listing only what
    worked would read as a complete account of the pass and would not be
    one.
    """
    from cfdinvest.schedule import notify

    try:
        where = str(environment or "unknown").lower()
        lines = [
            f"Capital.com {where} account -- this pass.",
            "",
        ]
        done = [(i, r) for i, r in outcomes if getattr(r, "ok", False)]
        failed = [(i, r) for i, r in outcomes if not getattr(r, "ok", False)]

        if done:
            lines.append("Placed:")
            for intent, result in done:
                size = getattr(result, "size", None) or intent.target_units
                level = getattr(result, "level", None)
                lines.append(
                    f"  {intent.action.upper():6s} {intent.ticker:6s} "
                    f"{intent.direction or '':5s} {size:g} units"
                    + (f" at {level:g}" if level else "")
                )
        if failed:
            lines += ["", "FAILED -- the account may not match the record:"]
            for intent, result in failed:
                why = getattr(result, "error", "") or getattr(result, "reason", "")
                lines.append(
                    f"  {intent.action.upper():6s} {intent.ticker:6s} {why}"
                )
        if refused:
            # Counted, not listed. A refusal is the normal outcome of a
            # limit doing its job and there can be a dozen a pass; spelling
            # each one out is how a mail stops being read, and they are all
            # in the order store with their reasons intact. A *failure* is
            # different and stays in full above.
            reasons: dict[str, int] = {}
            for intent in refused:
                key = str(intent.reason or "refused").split(":")[0]
                reasons[key] = reasons.get(key, 0) + 1
            lines += ["", "Not opened: " + ", ".join(
                f"{count} {reason}" for reason, count in sorted(reasons.items())
            )]

        balance = getattr(account, "balance", None)
        if balance is not None:
            lines += ["", f"Balance: {balance:,.2f} "
                          f"{getattr(account, 'currency', '') or ''}".rstrip()]

        if not done and not failed:
            logger.info(
                "Capital: %d refused and nothing placed; no mail sent", len(refused)
            )
            return
        subject = (
            f"{len(done)} placed, {len(failed)} failed "
            f"on the Capital {where} account"
        )
        notify.send(
            _plain_message(subject, lines, smtp),
            config=smtp, transport=transport,
        )
    except Exception as error:  # noqa: BLE001 - a lost mail is not an outage
        logger.warning("Could not send the Capital trade mail: %s", error)


def _send_bridge_halt(report, smtp, transport) -> None:
    """Say loudly that the account and the record disagree. Never raises."""
    from cfdinvest.schedule import notify

    try:
        body = [
            "The Capital.com bridge stopped opening positions.",
            "",
            report.halt_reason,
            "",
            "Existing positions were left alone. Nothing was closed and "
            "nothing was opened. This needs a person to look before it will "
            "trade again.",
        ]
        if report.unexplained:
            body += ["", f"In the account, not in the record: "
                         f"{', '.join(report.unexplained)}"]
        if report.missing:
            body += ["", f"In the record, not in the account: "
                         f"{', '.join(report.missing)}"]
        notify.send(
            _plain_message("the account and the record disagree", body, smtp),
            config=smtp, transport=transport,
        )
    except Exception as error:  # noqa: BLE001
        logger.warning("Could not send the halt alert: %s", error)


def _command_record_candidate_spreads(args, app_config) -> int:
    """The same reading, over the screen's candidates, once a day.

    Gives the screen a cost term with real provenance. Until this existed it
    ranked on liquidity, volatility and continuity alone -- none of which
    can see that an instrument costs four times the median to trade, which
    is how INTC stayed in the universe for months.

    The traded names are excluded: they already have a fifteen-minute
    series and reading them twice would put two cadences in one file under
    one scope.
    """
    traded = set(_traded(app_config, getattr(args, "account", None)))
    pool = [
        t for t in (getattr(app_config.universe, "candidates", ()) or ())
        if t not in traded
    ]
    if not pool:
        logger.debug("No candidates outside the traded list; nothing to read")
        return EXIT_OK

    return _command_record_spreads(
        _spread_args(args, pool, "candidate"), app_config
    )


def _spread_args(args, tickers, scope):
    """A copy of ``args`` naming which instruments to read and why."""
    import copy

    clone = copy.copy(args)
    clone.spread_tickers = list(tickers)
    clone.spread_scope = scope
    return clone


def _command_record_spreads(args, app_config) -> int:
    """Read what a trade costs right now, and append it. Never fails a run.

    One call for the whole universe. The reading is taken whether or not the
    session is open and *tagged* with which it was, because an out-of-session
    spread is nearly twice an in-session one -- so pooling them would produce
    a median describing trading nobody does, and discarding them would throw
    away the evidence that says so.

    Absent credentials are not an error. Most installations have none, and a
    scheduled job that logs a failure every fifteen minutes for a feature
    nobody configured is how a log stops being read.
    """
    from cfdinvest.broker.capital import client as capital
    from cfdinvest.persist import spreads as store
    from cfdinvest.schedule.market_calendar import describe

    account = getattr(args, "account", None)
    tickers = list(
        getattr(args, "spread_tickers", None)
        or _traded(app_config, getattr(args, "account", None))
        or ()
    )
    if not tickers:
        return EXIT_OK
    # Which list this reading covers. Written onto every row so a later
    # reader can tell a traded instrument's fifteen-minute series from a
    # candidate's daily one, and never pool the two into one median.
    scope = str(getattr(args, "spread_scope", "") or "traded")

    settings = capital.CapitalConfig.from_env()
    if not settings.complete:
        logger.debug("No Capital.com credentials; not recording spreads")
        return EXIT_OK

    session = capital.open_session(settings)
    if not session.ok:
        logger.warning("Could not read spreads: %s", session.error)
        return EXIT_OK

    now = pd.Timestamp.now(tz="UTC")
    rows = capital.fetch_market_details(tickers, session, settings, now=str(now))

    try:
        calendar = describe(tickers, now)
        open_now = {
            str(index): bool(value)
            for index, value in calendar["is_open"].items()
        }
    except Exception as error:  # noqa: BLE001 - an unknown calendar is not fatal
        logger.warning("Could not read the session calendar: %s", error)
        open_now = {}

    observations = []
    for row in rows:
        observations.append({
            "at": str(now),
            "ticker": row.ticker,
            "epic": row.epic,
            "bid": row.bid,
            "offer": row.offer,
            "spread_bp": None if row.spread_pct is None else row.spread_pct * 100,
            "min_deal_size": row.min_deal_size,
            "margin_factor": row.margin_factor,
            # Raw and unconverted; the unit is not documented. See the note
            # in persist.spreads.
            "funding_long_raw": row.funding_long,
            "funding_short_raw": row.funding_short,
            "market_status": row.tradable,
            "session_open": open_now.get(row.ticker),
            "scope": scope,
            "error": row.error,
        })

    written = store.record_spreads(observations, app_config, account)
    priced = sum(1 for row in rows if row.ok)
    logger.info(
        "Spreads (%s): recorded %d reading(s), %d priced of %d",
        scope, written, priced, len(rows),
    )
    return EXIT_OK


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


def _capital_marks(tickers) -> dict:
    """Live bid/offer/mid per instrument from Capital, or an empty dict.

    One batch call for the whole universe. Never raises: the book must keep
    recording when a broker is unreachable, and an empty result is read by
    the caller as "price it the old way and say so", which is a statement
    rather than a silence.
    """
    wanted = [str(t) for t in tickers if str(t)]
    if not wanted:
        return {}
    try:
        from cfdinvest.broker.capital import client as capital

        settings = capital.CapitalConfig.from_env()
        if not settings.complete:
            logger.debug("No Capital credentials; the book prices on the table")
            return {}
        session = capital.open_session(settings)
        if not session.ok:
            logger.warning("Capital: could not authenticate for marks: %s", session.error)
            return {}
        marks = {}
        for market in capital.fetch_market_details(wanted, session, settings):
            if not getattr(market, "ok", False):
                continue
            bid, offer = getattr(market, "bid", None), getattr(market, "offer", None)
            try:
                bid, offer = float(bid), float(offer)
            except (TypeError, ValueError):
                continue
            if not (bid > 0 and offer >= bid):
                continue
            marks[str(market.ticker).upper()] = {
                "bid": bid, "offer": offer, "mid": (bid + offer) / 2.0,
                "currency": getattr(market, "currency", "") or "",
            }
        return marks
    except Exception as error:  # noqa: BLE001
        logger.warning("Capital marks unavailable: %s", error)
        return {}


def _capital_environment() -> str:
    """Which Capital account the book is being priced against.

    Returned as the broker reports it rather than assumed. Demo and live are
    different evidence and the venue stamp carries the difference, so this
    must never guess: an unreadable configuration produces an empty string,
    which `venue_name` turns into an explicit unknown.
    """
    try:
        from cfdinvest.broker.capital import client as capital

        return str(capital.CapitalConfig.from_env().environment or "")
    except Exception as error:  # noqa: BLE001
        logger.debug("Could not read the Capital environment: %s", error)
        return ""


def _live_armed() -> bool | None:
    """Whether the live order path was armed when this pass was decided.

    Recorded on the book row, and deliberately not "did the broker place
    it" -- that is only knowable after the broker replies, and writing it
    would mean editing a row after the outcome. What the broker actually did
    is in the append-only order store.

    ``None`` rather than ``False`` when it cannot be determined: not knowing
    and knowing it was off are different statements, and only one of them
    should read as reassurance later.
    """
    try:
        from cfdinvest.broker.capital import dealing as gate

        return not gate.DealingConfig.from_env().refusal()
    except Exception as error:  # noqa: BLE001
        logger.debug("Could not read the live gate: %s", error)
        return None


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
        # Held names are topped up too, not just the traded list.
        #
        # A departing instrument keeps its signals so it can exit on its own
        # reversal or time stop (``_with_held``), and its position can now be
        # priced at the broker (``_dealing_tickers``) -- but both of those
        # read a lake that had stopped being updated for it, because this job
        # fetched ``market.tickers`` alone. The exit decision would have been
        # taken on bars that stopped arriving the day the name left the list,
        # which is worse than either failure it was meant to fix: a stale
        # signal is confidently wrong where a missing one is merely absent.
        #
        # Data is kept current for anything the book still holds, whether or
        # not it is currently selectable. A name that has genuinely stopped
        # trading is a different case -- ingestion reports it per-symbol and
        # the rest of the fetch is unaffected.
        account = getattr(args, "account", None)
        wanted = _with_held(backtest.tickers, app_config, account) or list(
            backtest.tickers
        )
        # Everything ever selected, not merely everything held. A departed
        # name keeps accruing history so it can be ranked on return, and so
        # a position reopened in it later is not decided on a series with a
        # hole in the middle.
        for name in _ever_selected(app_config, account):
            if name not in wanted:
                wanted.append(name)
        # And every candidate the screen may rank, not only what is traded.
        #
        # Two reasons, and the second is the one that bites. A candidate the
        # screen picks tomorrow needs history today, or its first intraday
        # tick fetches years of bars inline on a fifteen-minute job. And
        # `universe.screen._spread_series` prefers the capital_1h archive
        # over the live observation store *if the archive exists at all* --
        # all or nothing, not per instrument. Seeding history for the
        # candidates therefore switched the screen's cost term from a store
        # refreshed daily to an archive that would never update again, which
        # is the mirror image of the failure that function's own docstring
        # warns about: a deployment ran a month reading an archive it did
        # not have while the store it did have filled up beside it.
        #
        # Keeping the archive current for every candidate makes preferring
        # it correct. Incremental after the first fetch -- backfill resumes
        # from what is on disk -- so an ordinary tick costs a few bars each.
        for name in _screen_candidates(app_config):
            if name not in wanted:
                wanted.append(name)
        runner_module._refresh(
            DataRepository(app_config), _with_fx_support(wanted), backtest
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
    elif name == "prune-runs":
        _command_prune_runs(_job_args(args, "prune-runs"), app_config)
    elif name == "record-candidate-spreads":
        _command_record_candidate_spreads(
            _job_args(args, "poll-live-quotes"), app_config
        )
    elif name == "record-spreads":
        _command_record_spreads(_job_args(args, "poll-live-quotes"), app_config)
    elif name == "trade-capital":
        _command_trade_capital(
            _job_args(args, "poll-live-quotes"), app_config, smtp, transport
        )
    elif name == "ingest-minute":
        _command_ingest_minute(args, app_config)
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


def _command_ingest_minute(args, app_config) -> int:
    """Collect minute bars for everything the book cares about.

    The set is the traded list, plus what is held, plus everything any
    screen has ever selected -- the same reasoning as the intraday top-up.
    A name that left the universe still accrues history, because minute
    retention is the shortest the vendor offers and a day not fetched is
    gone rather than merely late.

    Incremental by construction: ``backfill_ticker`` resumes from what is
    already on disk, so a daily run fetches a day and an interrupted run
    picks up where it stopped. Nothing here is atomic across instruments
    and does not need to be.
    """
    from cfdinvest.broker.capital import client as capital
    from cfdinvest.ingestion import capital_prices

    account = getattr(args, "account", None)
    backtest = _backtest_config(args, app_config)
    wanted = _with_held(backtest.tickers, app_config, account) or list(
        backtest.tickers
    )
    for extra in _ever_selected(app_config, account):
        if extra not in wanted:
            wanted.append(extra)
    if not wanted:
        return EXIT_OK

    settings = capital.CapitalConfig.from_env()
    if not getattr(settings, "complete", True):
        logger.debug("No Capital credentials; no minute bars collected")
        return EXIT_OK

    try:
        reports = capital_prices.backfill(
            wanted, settings, app_config,
            interval="capital_1m", resolution="1m",
        )
    except Exception as error:  # noqa: BLE001 - collection is best-effort
        logger.warning("Minute ingestion failed: %s", error)
        return EXIT_OK

    written = sum(int(r.get("rows") or 0) for r in reports or ())
    failed = [str(r.get("ticker")) for r in reports or () if r.get("error")]
    logger.info(
        "Minute bars: %d instrument(s), %d row(s) written%s",
        len(reports or ()), written,
        f"; no data for {', '.join(failed)}" if failed else "",
    )
    return EXIT_OK


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
                    scheduler_module.any_open(_traded(app_config, account), now)
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
    universe = _universe(args) or _traded(app_config, getattr(args, "account", None))
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
    universe = _universe(args) or _traded(app_config, getattr(args, "account", None))
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
        if args.command == "daemon":
            return _command_daemon(args, app_config, smtp, transport)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return EXIT_FAILED

    print(f"Unknown command {args.command!r}", file=sys.stderr)
    return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
