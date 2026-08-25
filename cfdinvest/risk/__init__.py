"""Portfolio risk, read from what the pipeline already wrote.

**Nothing here drives the pipeline.** Positions, prices, the P&L
decomposition and the regime classifications are all read from persisted
artefacts; no position is recomputed and no simulation is run. What cannot be
read is reported absent rather than inferred.

Two scopes, deliberately separate: :class:`RiskLimits` bounds the book,
:class:`~cfdinvest.livehelper.config.LiveHelperConfig` bounds one trade before
you place it.
"""

from __future__ import annotations

from cfdinvest.risk.model import (
    RiskLimits,
    RiskSnapshot,
    build_risk_snapshot,
    compute_current_risk,
    compute_fx_risk,
    compute_leverage_and_margin,
    compute_pnl_risk,
    compute_recent_trade_risk,
    compute_regime_risk,
)
from cfdinvest.risk.monitor import (
    RiskMonitor,
    RiskMonitorConfig,
    RiskObservation,
    take_risk_snapshot,
)
from cfdinvest.risk.scheduler_status import compute_scheduler_status
from cfdinvest.risk.snapshot import snapshot_from_disk

__all__ = [
    "RiskLimits",
    "RiskSnapshot",
    "build_risk_snapshot",
    "compute_current_risk",
    "compute_leverage_and_margin",
    "compute_fx_risk",
    "compute_regime_risk",
    "compute_recent_trade_risk",
    "compute_pnl_risk",
    "compute_scheduler_status",
    "snapshot_from_disk",
    "RiskMonitorConfig",
    "RiskObservation",
    "take_risk_snapshot",
    "RiskMonitor",
]
