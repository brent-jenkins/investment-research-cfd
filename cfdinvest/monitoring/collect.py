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
from cfdinvest.monitoring.resources import (
    read_deployed_revisions,
    read_system_resources,
)

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

        repository = DataRepository(config)
        coverage = repository.coverage()
        span = repository.coverage_span(include_macro=True)
    except Exception as error:  # noqa: BLE001
        logger.warning("Could not read lake coverage: %s", error)
        return None

    if coverage is None or getattr(coverage, "empty", True):
        return None

    # Ask the repository rather than naming manifest columns here. The list
    # this used to carry -- "end", "last", "newest", "end_date" -- matched
    # nothing the ingestors actually write, so this returned None on every
    # real lake and the freshness check silently never ran.
    newest = span[1]
    if newest is None:
        return None

    return {
        "newest": newest,
        "tickers": sorted(str(t) for t in coverage.index),
        "series_count": int(len(coverage)),
    }


def _live_quotes(config) -> dict | None:
    """The quote file, or ``None`` when it cannot be read.

    Reads through ``livequotes.storage``, which opens files and nothing else.
    The monitoring layer must stay free of anything that can open a socket --
    that is what its own import test asserts.
    """
    try:
        from cfdinvest.livequotes.storage import load_quotes

        return load_quotes(config)
    except Exception as error:  # noqa: BLE001
        logger.warning("Could not read live quotes: %s", error)
        return None


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
    # Read from the environment rather than from config: the expiry belongs
    # to the key, the key belongs to the machine, and the two move together.
    # An unset date reports "unchecked" rather than nothing, so a credential
    # whose lifetime nobody recorded is visible as exactly that.
    import os as _os

    issues += _guarded(
        "credentials", "credential expiry",
        lambda: core.check_credential_expiry(
            _os.environ.get("CFD_CAPITAL_KEY_EXPIRES"), now=moment
        ),
    )
    issues += _guarded("lake", "lake freshness", lambda: core.check_lake_freshness(
        _lake_metadata(config), limits, now=moment))
    issues += _guarded("lake", "price repair", lambda: core.check_price_repairs(
        _price_repairs(config, moment), now=moment))
    issues += _guarded("livequotes", "live quote freshness",
                       lambda: core.check_live_quote_freshness(
                           _live_quotes(config), now=moment))
    issues += _guarded("scheduler", "scheduler health", lambda: core.check_scheduler_health(
        state, limits, now=moment))
    issues += _guarded("disk", "disk space", lambda: core.check_disk_space(
        machine, limits, now=moment))
    issues += _guarded("deploy", "deployed revision",
                       lambda: core.check_deploy_is_current(
                           read_deployed_revisions(), now=moment))
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
    # Last, and the only check that asks whether the thing WORKS rather than
    # whether it exists. Everything above can report clean while the account
    # goes unexamined for an entire session -- which is precisely what
    # happened on 2026-09-09.
    issues += _guarded("dealing", "dealing liveness",
                       lambda: core.check_dealing_liveness(
                           _last_dealing_at(config, account),
                           _market_open(config, account, moment),
                           limits, now=moment,
                           last_blocked=_last_blocked(config, account)))

    return issues, machine


def _last_dealing_at(config, account=None):
    """When the dealing path last examined the account, or ``None``.

    Read from ``persist.margin``, which writes a usage row on every dealing
    pass -- including a pass that looks at the book and correctly does
    nothing. That is what makes it a liveness signal: it records that the
    account was EXAMINED, not that an order was placed.

    ``None`` on any failure, which the check reports as "never recorded"
    rather than treating as fresh.
    """
    try:
        from cfdinvest.persist import margin as margin_store

        rows = [
            row for row in margin_store.load_margin(config, account)
            if str(row.get("kind") or "") == "usage" and row.get("at")
        ]
        return rows[-1]["at"] if rows else None
    except Exception:  # noqa: BLE001 - absence is not an outage
        return None


def _price_repairs(config, now):
    """What repairing the last month of Capital bars found; None if unreadable."""
    try:
        from cfdinvest.repository import DataRepository

        repository = DataRepository(config)
        repository.load_market(
            interval=config.market.interval,
            start=now - pd.Timedelta(days=30), columns=["open", "close"],
        )
        return list(repository.last_price_repairs)
    except Exception as error:  # noqa: BLE001
        logger.warning("Could not check Capital's prices for repairs: %s", error)
        return None


def _last_blocked(config, account=None):
    """The newest pass that ran and could not read the account, or ``None``.

    ``(at, reason)`` from the ``blocked`` rows the dealer writes when the
    balance or positions are unreadable. ``None`` on any failure, which the
    check reads as "no such pass" -- the older, less specific alert.
    """
    try:
        from cfdinvest.persist import margin as margin_store

        rows = [
            row for row in margin_store.load_margin(config, account)
            if str(row.get("kind") or "") == "blocked" and row.get("at")
        ]
        return (rows[-1]["at"], str(rows[-1].get("reason") or "")) if rows else None
    except Exception:  # noqa: BLE001
        return None


def _market_open(config, account=None, now=None):
    """Whether any traded instrument is dealing, or ``None`` if unknown.

    Deliberately three-valued. The daemon treats an unreadable calendar as
    CLOSED, because trading on a guess is worse than pausing; monitoring must
    treat it as UNKNOWN, because "the market was shut" and "we could not tell"
    are different statements and only one of them excuses a silent daemon.
    """
    try:
        from cfdinvest.schedule import scheduler as scheduler_module
        from cfdinvest.universe.traded import traded_universe

        names = list(traded_universe(config, account).names)
        return bool(scheduler_module.any_open(names, now))
    except Exception:  # noqa: BLE001
        return None


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
