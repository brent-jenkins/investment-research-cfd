"""Running every check against a real lake.

The impure half: this reads the filesystem and hands plain values to the pure
functions in :mod:`cfdinvest.monitoring.core`.

**Nothing here can drive the pipeline.** It reads artefacts, reads the
machine, and writes one snapshot. There is no import of the backtest engine
and none of the scheduler's runner, and a test asserts that stays true — a
monitoring pass that could start a run would be reporting on a system it had
just disturbed.

The repository *is* imported, for one read: the coverage manifest, which is
where the newest bar in the lake is recorded. That is a read of a written
manifest, not a fetch — it cannot reach a vendor and cannot ingest.

**A check that raises does not stop the pass.** Nine checks run; one of them
failing must not silence the other eight, which is exactly the case where you
most need them. A crashed check is reported as its own issue, because a check
that silently stops protecting you is worse than one that fails loudly.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Mapping

import pandas as pd

from cfdinvest.monitoring import core
from cfdinvest.monitoring.aggregate import MonitoringSnapshot, summarise_monitoring_state
from cfdinvest.monitoring.core import MonitoringIssue, MonitoringThresholds
from cfdinvest.monitoring.resources import read_system_resources

logger = logging.getLogger(__name__)

__all__ = ["collect_issues", "run_monitoring"]


def _guarded(kind: str, name: str, call: Callable[[], list[MonitoringIssue]]):
    """Run one check, turning a crash into an issue rather than an exception."""
    try:
        return list(call() or [])
    except Exception as error:  # noqa: BLE001 - one check, not the pass
        logger.exception("Monitoring check %s failed", name)
        return [
            MonitoringIssue(
                type=kind,
                severity="warning",
                message=f"The {name} check itself failed: {error}",
                details={"error": f"{type(error).__name__}: {error}"},
                remedy=(
                    "This is a bug in the monitoring, not necessarily in what "
                    "it was checking — and it means that thing is now "
                    "unchecked."
                ),
            )
        ]


def _lake_metadata(config) -> dict[str, Any] | None:
    """The newest bar in the lake, without loading the lake.

    Read from the repository's coverage manifest rather than through
    :mod:`cfdinvest.ui.sources`. The dashboard is an optional install --
    monitoring has to work on a headless box where it was never put -- and a
    monitoring layer that imported the UI to find out whether the data was
    fresh would fail on exactly the machine it exists to watch.

    The newest bar, not a file mtime: a lake rewritten this morning with data
    ending last month is stale, and the mtime would hide that.
    """
    try:
        from cfdinvest.repository import DataRepository

        coverage = DataRepository(config).coverage()
    except Exception as error:  # noqa: BLE001
        logger.warning("Could not read lake coverage: %s", error)
        return None

    if coverage is None or getattr(coverage, "empty", True):
        return None

    column = next(
        (name for name in ("end", "last", "newest", "end_date")
         if name in coverage.columns),
        None,
    )
    if column is None:
        return None

    newest = pd.to_datetime(coverage[column], utc=True, errors="coerce").max()
    return {
        "newest": newest,
        "tickers": sorted(str(t) for t in coverage.index),
        "series_count": int(len(coverage)),
    }


def collect_issues(
    config=None,
    *,
    account: str | None = None,
    run_id: str | None = None,
    thresholds: MonitoringThresholds | None = None,
    notify_state: Mapping[str, Any] | None = None,
    resources: Mapping[str, Any] | None = None,
    now: pd.Timestamp | None = None,
) -> tuple[list[MonitoringIssue], dict[str, Any]]:
    """Run all nine checks. Returns the issues and the resource reading."""
    from cfdinvest.persist import readers, scheduler_state
    from cfdinvest.persist.storage import data_root

    limits = thresholds or core.DEFAULTS
    moment = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="UTC")
    if moment.tz is None:
        moment = moment.tz_localize("UTC")

    machine = dict(resources) if resources is not None else read_system_resources(
        data_root(config)
    )

    def read(name: str, *args):
        try:
            return getattr(readers, name)(*args)
        except Exception as error:  # noqa: BLE001
            logger.warning("Could not read %s: %s", name, error)
            return None

    chosen = run_id or read("load_latest_run", config)
    run_metadata = read("load_run_metadata", chosen, config, account) or {}
    execution_metadata = read("load_execution_metadata", chosen, config, account) or {}

    try:
        state = scheduler_state.load_scheduler_state(config, account)
    except Exception as error:  # noqa: BLE001
        logger.warning("Could not read the scheduler state: %s", error)
        state = None

    present: set[str] | None = None
    if chosen:
        try:
            status = readers.artefact_status(chosen, config, account)
            present = set(status.index[status["present"]].astype(str))
        except Exception as error:  # noqa: BLE001
            logger.warning("Could not list the run's artefacts: %s", error)

    try:
        simulated, _ = readers.load_positions(chosen, config, account, confirmed=False)
    except Exception as error:  # noqa: BLE001
        logger.warning("Could not read positions: %s", error)
        simulated = None
    confirmed = read("load_confirmed_positions", config, account) or {}

    issues: list[MonitoringIssue] = []
    issues += _guarded("lake", "lake freshness", lambda: core.check_lake_freshness(
        _lake_metadata(config), limits, now=moment))
    issues += _guarded("scheduler", "scheduler health", lambda: core.check_scheduler_health(
        state, limits, now=moment))
    issues += _guarded("disk", "disk space", lambda: core.check_disk_space(
        machine, limits, now=moment))
    issues += _guarded("run", "run failure", lambda: core.check_run_failures(
        run_metadata, now=moment))
    issues += _guarded("pipeline", "missing artefact", lambda: core.check_missing_frames(
        present, now=moment))
    issues += _guarded("confirm", "confirmation drift",
                       lambda: core.check_confirmation_mismatches(
                           simulated, confirmed, limits, now=moment))
    issues += _guarded("email", "email delivery", lambda: core.check_email_delivery(
        notify_state, now=moment))
    issues += _guarded("pipeline", "pipeline anomaly", lambda: core.check_pipeline_anomalies(
        run_metadata, execution_metadata, limits, now=moment))
    issues += _guarded("system", "system resource", lambda: core.check_system_resources(
        machine.get("cpu_percent"), machine.get("memory_percent"), limits, now=moment))

    return issues, machine


def run_monitoring(
    config=None,
    *,
    account: str | None = None,
    run_id: str | None = None,
    thresholds: MonitoringThresholds | None = None,
    notify_state: Mapping[str, Any] | None = None,
    store: bool = True,
    now: pd.Timestamp | None = None,
) -> MonitoringSnapshot:
    """One monitoring pass: check, aggregate, and record.

    Returns a snapshot whether or not anything was wrong, and whether or not
    it could be stored. A monitoring layer that returns nothing when it cannot
    write is a monitoring layer that goes quiet exactly when the disk is full.
    """
    from cfdinvest.persist import readers

    issues, machine = collect_issues(
        config,
        account=account,
        run_id=run_id,
        thresholds=thresholds,
        notify_state=notify_state,
        resources=None,
        now=now,
    )
    summary = summarise_monitoring_state(issues, now=now)

    try:
        chosen = run_id or readers.load_latest_run(config)
    except Exception:  # noqa: BLE001
        chosen = run_id

    snapshot = MonitoringSnapshot.from_summary(
        summary, system_resources=machine, run_id=chosen
    )

    if store:
        from cfdinvest.persist import monitoring as monitoring_store

        monitoring_store.write_monitoring_snapshot(
            snapshot, config, account=account, now=snapshot.timestamp
        )

    log = logger.error if snapshot.overall_status == "red" else logger.info
    log("Monitoring: %s — %s", snapshot.overall_status.upper(), snapshot.headline)
    return snapshot
