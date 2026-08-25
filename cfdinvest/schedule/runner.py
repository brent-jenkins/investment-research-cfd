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
    """Resolve (and create) the directory for one run's artefacts."""
    root = Path(repository.paths.data_root) / config.output_dir / run_id
    root.mkdir(parents=True, exist_ok=True)
    return root


def _prune_old_runs(repository: DataRepository, config: RunConfig) -> None:
    """Delete the oldest run directories beyond ``keep_runs``.

    Unattended runs accumulate. Without a cap, a VM fills its disk after a
    few months and the failure surfaces as something unrelated.
    """
    if config.keep_runs is None:
        return

    root = Path(repository.paths.data_root) / config.output_dir
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
    _record_start(repository, run_id, started)

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
            summary = _with_paths(summary, paths)
            _prune_old_runs(repository, settings)

        _record_finish(repository, summary)
        logger.info(summary.headline())
        return summary

    except Exception as error:  # noqa: BLE001 - an unattended run must not die silently
        logger.exception("Scheduled run %s failed", run_id)
        _record_failure(repository, run_id, error)
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


def _record_start(repository, run_id: str, started) -> None:
    """Note in the scheduler state that a run began."""
    try:
        from cfdinvest.persist import scheduler_state

        scheduler_state.update_last_run(
            started, run_id=run_id, config=repository.config
        )
    except Exception as error:  # noqa: BLE001 - never fail a run over bookkeeping
        logger.debug("Could not record the run start: %s", error)


def _record_finish(repository, summary: RunSummary) -> None:
    """Note the outcome, clearing any stale error on success."""
    try:
        from cfdinvest.persist import scheduler_state

        if summary.status == "failed":
            scheduler_state.update_last_error(
                summary.error or "run failed",
                summary.finished_at,
                run_id=summary.run_id,
                config=repository.config,
            )
        else:
            scheduler_state.update_last_success(
                summary.finished_at,
                run_id=summary.run_id,
                duration=summary.duration_seconds,
                config=repository.config,
            )
    except Exception as error:  # noqa: BLE001
        logger.debug("Could not record the run outcome: %s", error)


def _record_failure(repository, run_id: str, error: Exception) -> None:
    """Note that a run raised."""
    try:
        from cfdinvest.persist import scheduler_state

        scheduler_state.update_last_error(
            f"{type(error).__name__}: {error}",
            run_id=run_id,
            config=repository.config,
        )
    except Exception as recording_error:  # noqa: BLE001
        logger.debug("Could not record the failure: %s", recording_error)


def _with_paths(summary: RunSummary, paths: dict[str, str]) -> RunSummary:
    """Return a copy of the summary carrying the artefact paths."""
    from dataclasses import replace

    return replace(summary, output_paths=paths)


def _refresh(repository: DataRepository, tickers, backtest_config) -> None:
    """Top the lake up from the vendors before running.

    Opt-in. Reuses the ingestion layer unchanged; a failure here is raised
    so the run is marked failed rather than proceeding on stale data
    without saying so.
    """
    from cfdinvest.ingestion import MacroDataIngestor, MarketDataIngestor

    config = repository.config
    logger.info("Refreshing market data for %d ticker(s)", len(tickers) or 0)
    MarketDataIngestor(config).ingest(
        list(tickers) or None, interval=backtest_config.interval
    )

    if config.macro.api_key:
        with MacroDataIngestor(config) as ingestor:
            ingestor.ingest(list(backtest_config.series_ids or ()) or None)
    else:
        logger.info("No FRED API key configured; skipping the macro refresh")


def load_summary(path: str | Path) -> dict[str, Any]:
    """Read a persisted run summary back from disk."""
    return storage.read_json(Path(path), default={}) or {}
