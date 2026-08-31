"""Run the pipeline once, persist the outputs, and summarise the result.

Orchestration and persistence only. The pipeline itself belongs to
:mod:`cfdinvest.backtest.runner`, which this module calls — duplicating the
stage sequence here would give two places for it to drift, and the
scheduled path is exactly where a drift would go unnoticed.

What this adds on top:

* **Persistence.** Each run writes to its own directory, so a run is a
  durable artefact rather than a process that printed something.
* **A summary object.** What changed on the latest bar, which is what an
  alert reports and what a human wants to read.
* **Error containment.** An unattended run must not die silently. A failure
  is captured into the summary, marked, and returned so the caller can
  notify on it.
"""

from __future__ import annotations

import logging
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import pandas as pd

from cfdinvest import storage
from cfdinvest.backtest import BacktestConfig, evaluate
from cfdinvest.backtest.runner import run_pipeline as run_backtest_pipeline
from cfdinvest.repository import DataRepository
from cfdinvest.schedule.market_calendar import describe

logger = logging.getLogger(__name__)

__all__ = ["RunConfig", "RunSummary", "run_pipeline", "load_summary"]


@dataclass(frozen=True, slots=True)
class RunConfig:
    """Settings for one scheduled run."""

    #: Pipeline settings; the universe lives in ``backtest.tickers``.
    backtest: BacktestConfig = field(default_factory=BacktestConfig)
    #: Where run artefacts are written, relative to the lake's data root.
    output_dir: str = "runs"
    #: Persist the frames, not only the summary.
    save_outputs: bool = True
    #: Refresh the lake from the vendors before running. Off by default so
    #: the documented behaviour is "read the lake"; turn it on for an
    #: unattended VM that has nothing else keeping the data current.
    refresh_data: bool = False
    #: Keep at most this many run directories. ``None`` keeps everything.
    keep_runs: int | None = 30
    #: Path to the Trading 212 instrument rules. ``None`` looks at the
    #: default location; the table is optional and its absence means the
    #: broker checks report themselves not consulted rather than passed.
    rules_path: str | None = None
    #: Which account's tree to write into. ``None`` resolves from
    #: ``CFD_ACCOUNT_MODE``, and falls back to the shared root when that is
    #: unset -- separation is opt-in, so an existing lake keeps working.
    account: str | None = None
    #: Check the machine's health after each run and record what was found.
    monitoring: bool = True
    #: Persist each monitoring pass. The history is how you tell a status that
    #: turned red an hour ago from one that has been red for a week.
    store_monitoring: bool = True
    #: Email on a red status. Off in tests and in any run that should be
    #: silent; the check still runs and is still recorded.
    monitoring_alerts: bool = True


@dataclass(frozen=True, slots=True)
class RunSummary:
    """What one run did, in a form that can be emailed or stored.

    Attributes
    ----------
    status:
        ``"ok"``, ``"empty"`` (nothing to trade) or ``"failed"``.
    latest_signals:
        Active signals on the most recent bar — the actionable part.
    latest_trades:
        Trades the simulator executed on the most recent bar.
    error:
        Exception text when ``status == "failed"``, otherwise ``None``.
    """

    run_id: str
    started_at: pd.Timestamp
    finished_at: pd.Timestamp
    status: str
    universe: tuple[str, ...] = ()
    bars: int = 0
    as_of: pd.Timestamp | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    latest_signals: pd.DataFrame = field(default_factory=pd.DataFrame)
    latest_trades: pd.DataFrame = field(default_factory=pd.DataFrame)
    market_state: pd.DataFrame = field(default_factory=pd.DataFrame)
    output_paths: dict[str, str] = field(default_factory=dict)
    error: str | None = None

    @property
    def ok(self) -> bool:
        """Whether the run completed without an exception."""
        return self.status != "failed"

    @property
    def duration_seconds(self) -> float:
        """Wall-clock time the run took."""
        return float((self.finished_at - self.started_at).total_seconds())

    def to_dict(self) -> dict[str, Any]:
        """A JSON-serialisable view, for persistence and email bodies."""
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": self.duration_seconds,
            "status": self.status,
            "universe": list(self.universe),
            "bars": self.bars,
            "as_of": self.as_of,
            "metrics": self.metrics,
            "latest_signals": self.latest_signals.reset_index().to_dict("records")
            if not self.latest_signals.empty
            else [],
            "latest_trades": self.latest_trades.to_dict("records")
            if not self.latest_trades.empty
            else [],
            "output_paths": self.output_paths,
            "error": self.error,
        }

    def headline(self) -> str:
        """One line describing the run."""
        if self.status == "failed":
            return f"{self.run_id}: FAILED — {self.error}"
        if self.status == "empty":
            return f"{self.run_id}: no data to trade"
        return (
            f"{self.run_id}: {self.bars} bars, "
            f"{len(self.latest_signals)} active signals, "
            f"{len(self.latest_trades)} trades on {self.as_of:%Y-%m-%d}"
        )


def _run_directory(repository: DataRepository, config: RunConfig, run_id: str) -> Path:
    """Resolve (and create) the directory for one run's artefacts.

    Under the account tree when separation is on, so a virtual run cannot land
    where a live one is read from.
    """
    root = _account_output_root(repository, config) / run_id
    root.mkdir(parents=True, exist_ok=True)
    return root


def _account_output_root(repository: DataRepository, config: RunConfig) -> Path:
    """Where this account's runs live."""
    from cfdinvest.persist.storage import account_root

    return account_root(
        getattr(repository, "config", None), config.account
    ) / config.output_dir


def _prune_old_runs(repository: DataRepository, config: RunConfig) -> None:
    """Delete the oldest run directories beyond ``keep_runs``.

    Unattended runs accumulate. Without a cap, a VM fills its disk after a
    few months and the failure surfaces as something unrelated.
    """
    if config.keep_runs is None:
        return

    root = _account_output_root(repository, config)
    if not root.is_dir():
        return

    directories = sorted((p for p in root.iterdir() if p.is_dir()), reverse=True)
    for stale in directories[config.keep_runs :]:
        for item in sorted(stale.rglob("*"), reverse=True):
            item.unlink() if item.is_file() else item.rmdir()
        stale.rmdir()
        logger.debug("Pruned old run directory %s", stale)


def _persist(result, summary_fields: dict, directory: Path) -> dict[str, str]:
    """Write the run's frames to disk and return where they went."""
    paths: dict[str, str] = {}
    execution = result.execution
    signals = result.signals

    if execution is not None and not execution.equity_curve.empty:
        equity_path = directory / "equity.parquet"
        storage.write_parquet(execution.equity_curve.to_frame(), equity_path)
        paths["equity"] = str(equity_path)

        if not execution.trades.empty:
            trades_path = directory / "trades.parquet"
            storage.write_parquet(
                execution.trades.reset_index(drop=True), trades_path
            )
            paths["trades"] = str(trades_path)

        positions_path = directory / "positions.parquet"
        storage.write_parquet(execution.positions, positions_path)
        paths["positions"] = str(positions_path)

    if signals is not None and not signals.direction.empty:
        active = signals.to_long()
        if not active.empty:
            signals_path = directory / "signals.parquet"
            storage.write_parquet(active, signals_path)
            paths["signals"] = str(signals_path)

    summary_path = directory / "summary.json"
    storage.write_json(summary_fields, summary_path)
    paths["summary"] = str(summary_path)
    return paths


def _latest_slice(result) -> tuple[pd.Timestamp | None, pd.DataFrame, pd.DataFrame]:
    """The most recent bar's actionable signals and executed trades."""
    signals, execution = result.signals, result.execution
    if signals is None or signals.direction.empty:
        return None, pd.DataFrame(), pd.DataFrame()

    as_of = signals.direction.index[-1]

    active = signals.to_long()
    latest_signals = (
        active.loc[[as_of]] if as_of in active.index.get_level_values(0) else active.iloc[:0]
    )

    latest_trades = pd.DataFrame()
    if execution is not None and not execution.trades.empty:
        latest_trades = execution.trades[execution.trades["timestamp"] == as_of]

    return as_of, latest_signals, latest_trades


def run_pipeline(
    universe: Sequence[str] | None,
    config: RunConfig | None = None,
    *,
    repository: DataRepository | None = None,
    app_config=None,
    now: pd.Timestamp | None = None,
) -> RunSummary:
    """Run the whole pipeline once and persist the result.

    Parameters
    ----------
    universe:
        Tickers to run over. ``None`` uses whatever
        ``config.backtest.tickers`` holds, and failing that the whole lake.
    config:
        Run settings.
    repository:
        An open :class:`~cfdinvest.repository.DataRepository`. Supplied in
        tests and reused by a long-running scheduler; built from
        ``app_config`` when omitted.
    app_config:
        An :class:`~cfdinvest.config.AppConfig`, used only to build a
        repository when one is not given.
    now:
        Clock override. Injected so a run is reproducible and testable
        rather than dependent on when it happened to execute.

    Returns
    -------
    RunSummary
        Never raises for a pipeline failure: the error is captured, the
        status is ``"failed"``, and the caller decides whether to notify.
    """
    settings = config or RunConfig()
    started = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="UTC")
    if started.tzinfo is None:
        started = started.tz_localize("UTC")
    run_id = started.strftime("%Y%m%dT%H%M%SZ")

    if repository is None:
        from cfdinvest.config import load_config

        repository = DataRepository(app_config or load_config())

    tickers = tuple(universe) if universe else tuple(settings.backtest.tickers or ())
    backtest_config = settings.backtest
    if tickers:
        from dataclasses import replace as _replace

        backtest_config = _replace(backtest_config, tickers=list(tickers))

    # Recorded at the *start*, so a run that dies mid-pipeline still leaves
    # a trace. A last_run newer than last_success is exactly the signal that
    # something began and did not finish -- which deriving "last run" from
    # directory timestamps could never say.
    _record_start(repository, run_id, started, settings.account)

    try:
        if settings.refresh_data:
            _refresh(repository, tickers, backtest_config)

        result = run_backtest_pipeline(repository, backtest_config)
        evaluated = evaluate(result, backtest_config)

        as_of, latest_signals, latest_trades = _latest_slice(result)
        resolved_universe = (
            tuple(result.analysis.close.columns)
            if result.analysis is not None and not result.analysis.close.empty
            else tickers
        )
        status = "ok" if as_of is not None else "empty"

        summary = RunSummary(
            run_id=run_id,
            started_at=started,
            finished_at=pd.Timestamp.now(tz="UTC") if now is None else started,
            status=status,
            universe=resolved_universe,
            bars=int(evaluated.metadata.get("bars", 0) or 0),
            as_of=as_of,
            metrics=evaluated.metrics,
            latest_signals=latest_signals,
            latest_trades=latest_trades,
            market_state=describe(resolved_universe, started)
            if resolved_universe
            else pd.DataFrame(),
        )

        if settings.save_outputs and status != "empty":
            directory = _run_directory(repository, settings, run_id)
            paths = _persist(result, summary.to_dict(), directory)
            paths.update(
                _persist_extras(
                    repository, result, summary, backtest_config, directory, run_id
                )
            )
            paths.update(
                _persist_confirmed(
                    repository, directory, run_id, settings.account
                )
            )
            summary = _with_paths(summary, paths)
            _prune_old_runs(repository, settings)

        _record_finish(repository, summary, settings.account)
        _run_monitoring(repository, settings, summary)
        logger.info(summary.headline())
        return summary

    except Exception as error:  # noqa: BLE001 - an unattended run must not die silently
        logger.exception("Scheduled run %s failed", run_id)
        _record_failure(repository, run_id, error, settings.account)
        return RunSummary(
            run_id=run_id,
            started_at=started,
            finished_at=pd.Timestamp.now(tz="UTC") if now is None else started,
            status="failed",
            universe=tickers,
            error=f"{type(error).__name__}: {error}",
            market_state=pd.DataFrame(),
            metrics={"traceback": traceback.format_exc()},
        )


def _persist_extras(
    repository, result, summary, backtest_config, directory, run_id: str
) -> dict[str, str]:
    """Write the artefacts the run used to discard.

    The per-bar P&L decomposition, the execution settings, the analysis and
    regime panels, the cost split and the attribution. None of them changes
    what the pipeline did; all of them are things a later reader could
    otherwise only reconstruct approximately, or not at all.

    Never raises. Failing to *record* a completed run must not also destroy
    it, so a write problem is logged and the run still returns.
    """
    try:
        from cfdinvest.persist import build_run_metadata, writers

        metadata = build_run_metadata(
            run_id,
            config=backtest_config,
            started_at=summary.started_at,
            finished_at=summary.finished_at,
            status=summary.status,
            tickers=summary.universe,
            interval=getattr(backtest_config, "interval", None),
            bars=summary.bars,
            as_of=summary.as_of,
            warnings=(),
            error=summary.error,
        )
        return writers.write_all(
            directory,
            analysis_frame=getattr(result, "analysis", None),
            regime_frame=getattr(result, "regimes", None),
            execution_frame=getattr(result, "execution", None),
            run_metadata=metadata,
        )
    except Exception as error:  # noqa: BLE001
        logger.warning("Could not persist the extended artefacts: %s", error)
        return {}


def load_rules_once(settings) -> object | None:
    """Read the instrument rules table, once per run.

    Loaded here rather than per trade so every decision in a run is judged
    against the same table -- a file edited mid-run would otherwise give two
    trades two different answers, and nothing would say which.

    Never raises. A missing table is the normal state.
    """
    try:
        from cfdinvest.livehelper.t212_rules import load_rules

        table = load_rules(getattr(settings, "rules_path", None))
        if len(table):
            logger.info(
                "Trading 212 rules loaded for %d instrument(s) from %s",
                len(table), table.path,
            )
        else:
            logger.info(
                "No Trading 212 instrument rules at %s; the broker checks "
                "will report themselves not consulted", table.path,
            )
        return table
    except Exception as error:  # noqa: BLE001 - an absent table is not a fault
        logger.warning("Could not load the Trading 212 rules: %s", error)
        return None


def _run_monitoring(repository, settings, summary: RunSummary) -> None:
    """Check the machine, record what was found, and shout if it is red.

    Runs *after* the state is recorded, so the monitoring pass sees the run it
    is reporting on rather than the one before it.

    Never raises. A monitoring failure must not turn a completed run into a
    failed one -- the layer that reports problems must not become one.

    Only a red status sends mail. An alert that arrives every day for a yellow
    becomes an alert nobody opens on the day it is red.
    """
    if not settings.monitoring:
        return
    try:
        from cfdinvest.monitoring import run_monitoring
        from cfdinvest.schedule import notify

        snapshot = run_monitoring(
            getattr(repository, "config", None),
            account=settings.account,
            run_id=summary.run_id,
            notify_state=notify.delivery_state(),
            store=settings.store_monitoring,
        )
        if snapshot.overall_status == "red" and settings.monitoring_alerts:
            notify.send_monitoring_alert(snapshot)
    except Exception as error:  # noqa: BLE001
        logger.warning("Monitoring pass failed: %s", error)


def _persist_confirmed(
    repository, directory, run_id: str, account=None
) -> dict[str, str]:
    """Write the run's positions with your confirmations overlaid.

    **As a separate artefact, never over the top of ``positions.parquet``.**
    That file records what the engine did, and rewriting it with fills the
    engine never made would leave the run unable to answer the one question
    worth asking of it -- how far reality drifted from the simulation. Two
    files, both true, and a reader that prefers the confirmed one.

    Written only when something has been confirmed. An empty overlay file
    would be indistinguishable from "everything was confirmed as simulated",
    and those are not the same claim.

    Never raises: a confirmation problem must not fail a completed run.
    """
    try:
        from cfdinvest.persist import confirm, writers

        confirmations = confirm.load_confirmations(
            getattr(repository, "config", None), account
        )
        if not confirmations:
            return {}

        from cfdinvest import storage as lake_storage

        simulated = lake_storage.read_parquet(directory / "positions.parquet")
        merged, report = confirm.merge_simulated_and_confirmed_positions(
            simulated, confirmations
        )
        if merged.empty:
            return {}

        path = directory / "positions_confirmed.parquet"
        lake_storage.write_parquet(merged, path)

        if report["overridden"]:
            logger.info(
                "Confirmations overrode %d position(s) in run %s: %s",
                len(report["overridden"]),
                run_id,
                ", ".join(sorted(report["overridden"])),
            )
        return {"positions_confirmed": str(path)}
    except Exception as error:  # noqa: BLE001
        logger.warning("Could not overlay confirmations: %s", error)
        return {}


def _record_start(repository, run_id: str, started, account=None) -> None:
    """Note in the scheduler state that a run began."""
    try:
        from cfdinvest.persist import scheduler_state

        scheduler_state.update_last_run(
            started, run_id=run_id, config=repository.config, account=account
        )
    except Exception as error:  # noqa: BLE001 - never fail a run over bookkeeping
        logger.debug("Could not record the run start: %s", error)


def _record_finish(repository, summary: RunSummary, account=None) -> None:
    """Note the outcome, clearing any stale error on success."""
    try:
        from cfdinvest.persist import scheduler_state

        if summary.status == "failed":
            scheduler_state.update_last_error(
                summary.error or "run failed",
                summary.finished_at,
                run_id=summary.run_id,
                config=repository.config,
                account=account,
            )
        else:
            scheduler_state.update_last_success(
                summary.finished_at,
                run_id=summary.run_id,
                duration=summary.duration_seconds,
                config=repository.config,
                account=account,
            )
    except Exception as error:  # noqa: BLE001
        logger.debug("Could not record the run outcome: %s", error)


def _record_failure(repository, run_id: str, error: Exception, account=None) -> None:
    """Note that a run raised."""
    try:
        from cfdinvest.persist import scheduler_state

        scheduler_state.update_last_error(
            f"{type(error).__name__}: {error}",
            run_id=run_id,
            config=repository.config,
            account=account,
        )
    except Exception as recording_error:  # noqa: BLE001
        logger.debug("Could not record the failure: %s", recording_error)


def _with_paths(summary: RunSummary, paths: dict[str, str]) -> RunSummary:
    """Return a copy of the summary carrying the artefact paths."""
    from dataclasses import replace

    return replace(summary, output_paths=paths)


def _reference_pairs(config) -> tuple[str, ...]:
    """The currency pairs that exist to convert, not to be traded.

    Read from the one place that names them rather than pattern-matched on
    "=X", so a pair added to the conversion map is covered here without a
    second list drifting behind it.
    """
    try:
        from cfdinvest.schedule.app import fx_support_tickers

        return tuple(str(t) for t in fx_support_tickers() or ())
    except Exception as error:  # noqa: BLE001 - a missing map is not an outage
        logger.warning("Could not read the reference pairs: %s", error)
        return ()


def _refresh(repository: DataRepository, tickers, backtest_config) -> None:
    """Top the lake up from the vendors before running.

    Opt-in. Reuses the ingestion layer unchanged; a failure here is raised
    so the run is marked failed rather than proceeding on stale data
    without saying so.
    """
    from cfdinvest.ingestion import MacroDataIngestor, MarketDataIngestor
    from cfdinvest.ingestion import capital_prices

    config = repository.config

    # RESOLVED, never compared raw. ``backtest_config.interval`` is None
    # whenever nothing overrode it on the command line -- which is always,
    # in the daemon -- and None means "whatever the config says", not
    # "daily".
    #
    # Comparing it raw sent every intraday top-up down the yfinance branch,
    # where ``MarketDataIngestor`` resolved the same None against the config,
    # got "capital_1h", and asked yfinance for an interval it has never
    # heard of. The vendor rejected all hundred names on every pass and the
    # decision feed silently stopped advancing: on 2026-08-31 the book
    # opened positions at 15:06 UTC on bars that ended the previous Friday
    # at 19:00.
    #
    # The routing decision and the fetch were reading the same field at
    # different levels of resolution, which is why the logs said
    # "Downloading capital_1h" from the module that cannot download it.
    # ``_command_ingest_daily`` already resolves before comparing, and says
    # so in a comment; this, twelve lines away, did not.
    interval = backtest_config.interval or getattr(
        getattr(config, "market", None), "interval", None
    )
    logger.info(
        "Refreshing market data for %d ticker(s) at %s", len(tickers) or 0, interval
    )

    # Intraday bars come from the venue that fills the orders. yfinance is
    # still the source for daily bars, volume and the screening series --
    # but a decision taken on one feed and executed on another is two
    # sources of truth, and the intraday one has to be the broker's.
    #
    # Routed here rather than inside MarketDataIngestor because that class
    # is yfinance by definition: handing it "capital_1h" would send a label
    # it cannot parse to a vendor that never had the data.
    if capital_prices.is_capital_interval(interval):
        from cfdinvest.broker.capital import client as capital

        settings = capital.CapitalConfig.from_env()
        if not getattr(settings, "complete", True):
            # Raised rather than warned: this is the trading path's own
            # data. Continuing would run the strategy on whatever bars
            # happened to be on disk without saying the refresh never ran.
            raise RuntimeError(
                f"{interval} bars are the decision feed but no Capital "
                "credentials are configured, so the lake cannot be topped up"
            )
        # The broker does not quote the currency pairs, and never will.
        #
        # They ride along in the ticker list because the engine needs them to
        # price a dollar instrument in sterling, and ``backtest.runner``
        # already falls back to the vendor interval for them. But they were
        # still being *asked* of Capital on every pass, which did two things:
        # recorded five permanent "failed" entries in the lake manifest, so
        # the dashboard showed a critical banner that could never clear; and
        # left nobody fetching them at all once the decision feed moved off
        # yfinance. Their 1h bars were two days stale and getting staler,
        # which is how every USD instrument ends up converting at 1.0 -- a
        # regression this project has already paid 11 points of drawdown for.
        #
        # So the list is split by what each vendor can actually serve. This
        # is not a fallback firing after a failure; it is not asking the
        # wrong vendor in the first place.
        reference = set(_reference_pairs(config))
        served = [t for t in (tickers or ()) if str(t) not in reference]
        pairs = [t for t in (tickers or ()) if str(t) in reference]

        capital_prices.backfill(
            served or None, settings, config,
            interval=interval,
            resolution=capital_prices.resolution_for(interval),
        )
        if pairs:
            from cfdinvest.backtest.runner import REFERENCE_FALLBACK_INTERVAL

            logger.info(
                "Refreshing %d reference pair(s) at %s -- the broker does not "
                "quote them", len(pairs), REFERENCE_FALLBACK_INTERVAL,
            )
            MarketDataIngestor(config).ingest(
                pairs, interval=REFERENCE_FALLBACK_INTERVAL
            )
    else:
        MarketDataIngestor(config).ingest(list(tickers) or None, interval=interval)

    if config.macro.api_key:
        with MacroDataIngestor(config) as ingestor:
            ingestor.ingest(list(backtest_config.series_ids or ()) or None)
    else:
        logger.info("No FRED API key configured; skipping the macro refresh")

    # The screen's candidates too, not only what is traded. EDGAR covered
    # nineteen names of a forty-nine name pool, which is the same blind spot
    # the spread readings had: a selection rule cannot weigh a filing rate
    # for an instrument whose filings were never fetched.
    #
    # One fetch of the union rather than two of the halves, because
    # `store_filings` and `store_earnings_dates` *rewrite* -- two writers
    # would take turns deleting each other's half. Forty-nine requests at
    # the module's own 0.15s pause is about seven seconds against SEC's ten
    # a second.
    _refresh_events(config, _with_candidates(tickers, config))


def _with_candidates(tickers, config) -> list[str]:
    """The traded list plus every instrument the screen may rank.

    Order preserved and duplicates dropped, so the traded names keep their
    place at the front and a candidate that is also traded is fetched once.
    """
    names = [str(t) for t in (tickers or ()) if str(t)]
    seen = set(names)
    universe = getattr(getattr(config, "universe", None), "candidates", ()) or ()
    for candidate in universe:
        name = str(candidate)
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def _refresh_events(config, tickers) -> None:
    """Top up the reporting dates and the filings. Never raises.

    Both were being stored by hand, which meant the earnings blackout and
    the filing rate were reading whatever happened to have been fetched
    last -- the same staleness that let a daemon run eleven hours on code
    it had imported before the change.

    Unlike the market refresh above, a failure here is logged rather than
    raised: these feed a warning and a size, not a price, and a run that
    dies because the SEC was briefly unreachable would be a worse outcome
    than one that proceeds with last week's filing count.
    """
    from cfdinvest.ingestion import earnings as earnings_module
    from cfdinvest.ingestion import filings as filings_module

    universe = list(tickers or ())
    if not universe:
        return

    try:
        dates = earnings_module.fetch_earnings_dates(universe)
        if dates:
            earnings_module.store_earnings_dates(dates, config)
            logger.info("Refreshed reporting dates for %d instrument(s)", len(dates))
    except Exception as error:  # noqa: BLE001
        logger.warning("Could not refresh the reporting dates: %s", error)

    try:
        found = filings_module.fetch_filings(universe)
        if found:
            filings_module.store_filings(found, config)
            total = sum(len(rows) for rows in found.values())
            logger.info(
                "Refreshed filings for %d instrument(s), %d filings",
                len(found), total,
            )
    except Exception as error:  # noqa: BLE001
        logger.warning("Could not refresh the filings: %s", error)


def load_summary(path: str | Path) -> dict[str, Any]:
    """Read a persisted run summary back from disk."""
    return storage.read_json(Path(path), default={}) or {}
