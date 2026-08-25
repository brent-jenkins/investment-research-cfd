"""Reading the artefacts and assembling one snapshot.

The only part of this package that touches a filesystem, and it only reads.
:mod:`cfdinvest.risk.model` stays pure so a test can construct any
combination of missing artefacts without a disk; this function does the
loading and hands them over.

**No artefact is required.** A lake with a run but no analysis panels gives
position units and no exposure; one with no runs at all gives a snapshot that
says so. Every absence is named in ``unavailable`` rather than filled in.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

import pandas as pd

from cfdinvest.risk.model import RiskLimits, RiskSnapshot, build_risk_snapshot
from cfdinvest.risk.scheduler_status import compute_scheduler_status

logger = logging.getLogger(__name__)

__all__ = ["snapshot_from_disk", "limits_from_execution_metadata"]


def limits_from_execution_metadata(
    metadata: Mapping[str, Any] | None, limits: RiskLimits | None = None
) -> RiskLimits:
    """Fill the account balance from what the run recorded, if it recorded it.

    The balance is the denominator of leverage and margin usage, so a stale
    one makes both wrong in the direction that flatters the book. A run's own
    ``initial_equity`` is at least a figure the run stood behind; an explicit
    :class:`RiskLimits` always wins over it.
    """
    if limits is not None:
        return limits

    from dataclasses import replace

    base = RiskLimits()
    try:
        equity = float((metadata or {}).get("initial_equity"))
    except (TypeError, ValueError):
        return base
    if equity <= 0:
        return base
    return replace(base, account_balance_gbp=equity)


def _currencies(metadata: Mapping[str, Any] | None) -> dict[str, str]:
    """The instrument-to-currency map, if the run recorded one.

    Usually it did not: the execution metadata carries the FX *policy* but
    not the map, so currency exposure is reported against whatever is known
    and the unmapped instruments are listed rather than assumed domestic in
    silence.
    """
    mapping = (metadata or {}).get("instrument_currencies")
    return dict(mapping) if isinstance(mapping, Mapping) else {}


def snapshot_from_disk(
    config,
    *,
    run_id: str | None = None,
    account: str | None = None,
    limits: RiskLimits | None = None,
    instrument_leverage: Mapping[str, float] | None = None,
    now: pd.Timestamp | None = None,
) -> RiskSnapshot:
    """Build a snapshot from persisted artefacts. Reads only.

    Returns a snapshot even when nothing has been recorded — with the
    absences named — because a risk dashboard that renders nothing when the
    pipeline has not run is a dashboard that cannot tell you the pipeline has
    not run.
    """
    from cfdinvest.persist import readers, scheduler_state

    try:
        chosen = run_id or readers.load_latest_run(config)
    except Exception as error:  # noqa: BLE001
        logger.warning("Could not list runs: %s", error)
        chosen = None

    status = compute_scheduler_status(
        scheduler_state.load_scheduler_state(config, account), now=now
    )

    if chosen is None:
        return build_risk_snapshot(
            scheduler_status=status,
            limits=limits or RiskLimits(),
            now=now,
        )

    def read(name: str):
        try:
            return getattr(readers, f"load_{name}")(chosen, config, account)
        except Exception as error:  # noqa: BLE001 - one absent artefact, not all
            logger.warning("Could not read %s for run %s: %s", name, chosen, error)
            return None

    execution_metadata = read("execution_metadata") or {}
    parts = readers.load_execution_frame(chosen, config, account)

    return build_risk_snapshot(
        positions_frame=parts.get("positions"),
        regime_frame=read("regime_frame"),
        pnl_frame=read("pnl_frame"),
        analysis_frame=read("analysis_frame"),
        assessments_frame=_assessments(config, account),
        scheduler_status=status,
        limits=limits_from_execution_metadata(execution_metadata, limits),
        run_id=chosen,
        run_initial_equity=_initial_equity(execution_metadata),
        instrument_currencies=_currencies(execution_metadata),
        instrument_leverage=instrument_leverage,
        now=now,
    )


def _initial_equity(metadata: Mapping[str, Any] | None) -> float | None:
    """What the run itself started with, for the drawdown denominator."""
    try:
        equity = float((metadata or {}).get("initial_equity"))
    except (TypeError, ValueError):
        return None
    return equity if equity > 0 else None


def _assessments(config, account: str | None):
    """Live-helper assessments, if any have been recorded."""
    try:
        from cfdinvest.persist import assessments

        return assessments.load_assessments(config, account)
    except Exception as error:  # noqa: BLE001
        logger.debug("No recorded assessments: %s", error)
        return None
