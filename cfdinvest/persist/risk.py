"""Risk snapshots on disk: append-only, one file per observation.

A snapshot is a *reading*, not a derived artefact — it says what the book
looked like at a moment, and that moment does not come back. So each one gets
its own timestamped file and nothing is ever rewritten; the history is the
point, since the useful question is usually "when did this start" rather than
"what is it now".

Writes are atomic. Reads never raise.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from cfdinvest.persist.storage import account_root, atomic_write_text

logger = logging.getLogger(__name__)

__all__ = [
    "RISK_DIR",
    "SCHEMA_VERSION",
    "risk_root",
    "write_risk_snapshot",
    "load_latest_risk_snapshot",
    "list_risk_snapshots",
    "load_risk_history",
]

RISK_DIR = "risk"
SCHEMA_VERSION = 1

#: ``YYYYMMDD-HHMMSS-risk.json`` — sorts in time order, so "latest" is a sort
#: rather than a stat of every file.
FILENAME_FORMAT = "%Y%m%d-%H%M%S"


def risk_root(config=None, account: str | None = None) -> Path:
    """Where risk snapshots live."""
    return account_root(config, account) / RISK_DIR


def _jsonable(value: Any) -> Any:
    """Reuse the writers' coercion so one rule covers every artefact."""
    from cfdinvest.persist.writers import jsonable

    return jsonable(value)


def write_risk_snapshot(
    snapshot, config=None, *, account: str | None = None,
    scheduler_status: dict | None = None, now: pd.Timestamp | None = None,
) -> str | None:
    """Write one snapshot. Returns its path, or ``None`` if it could not.

    Never raises: a dashboard that failed to record a reading should still
    have shown it.
    """
    moment = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="UTC")
    if moment.tz is None:
        moment = moment.tz_localize("UTC")

    payload = asdict(snapshot) if is_dataclass(snapshot) else dict(snapshot or {})
    document = {
        "schema_version": SCHEMA_VERSION,
        "timestamp": moment.isoformat(),
        "risk_snapshot": _jsonable(payload),
        "scheduler_status": _jsonable(
            scheduler_status if scheduler_status is not None
            else payload.get("scheduler_status", {})
        ),
        "details": {
            "flags": _jsonable(payload.get("flags", ())),
            "severity": getattr(snapshot, "severity", None),
        },
    }

    path = risk_root(config, account) / f"{moment.strftime(FILENAME_FORMAT)}-risk.json"
    try:
        atomic_write_text(json.dumps(document, indent=2), path)
        return str(path)
    except Exception as error:  # noqa: BLE001
        logger.warning("Could not write the risk snapshot to %s: %s", path, error)
        return None


def list_risk_snapshots(config=None, account: str | None = None) -> list[Path]:
    """Every snapshot, newest first."""
    root = risk_root(config, account)
    try:
        if not root.is_dir():
            return []
        return sorted(root.glob("*-risk.json"), key=lambda p: p.name, reverse=True)
    except OSError as error:
        logger.warning("Risk directory unreadable at %s: %s", root, error)
        return []


def load_latest_risk_snapshot(config=None, account: str | None = None) -> dict | None:
    """The most recent snapshot, or ``None`` when none has been written."""
    for path in list_risk_snapshots(config, account):
        document = _read(path)
        if document is not None:
            return document
    return None


def _read(path: Path) -> dict | None:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        logger.warning("Risk snapshot unreadable at %s: %s", path, error)
        return None
    return document if isinstance(document, dict) else None


def load_risk_history(
    config=None, account: str | None = None, *, limit: int = 200
) -> pd.DataFrame:
    """Headline figures from recent snapshots, oldest first.

    A table rather than the documents themselves: what a chart of risk over
    time needs is a handful of scalars per observation, and loading two
    hundred full snapshots to plot one line is a lot of JSON for one number.
    """
    rows = []
    for path in list_risk_snapshots(config, account)[:limit]:
        document = _read(path)
        if document is None:
            continue
        snapshot = document.get("risk_snapshot") or {}
        exposure = snapshot.get("exposure_metrics") or {}
        leverage = snapshot.get("leverage_metrics") or {}
        margin = snapshot.get("margin_metrics") or {}
        pnl = snapshot.get("pnl_risk_metrics") or {}
        rows.append(
            {
                "timestamp": document.get("timestamp"),
                "gross_exposure_gbp": exposure.get("gross_exposure_gbp"),
                "open_positions": exposure.get("open_positions"),
                "effective_leverage": leverage.get("effective_leverage"),
                "margin_usage_pct": margin.get("margin_usage_pct"),
                "current_drawdown": pnl.get("current_drawdown"),
                "flags": len(snapshot.get("flags") or ()),
                "run_status": (document.get("scheduler_status") or {}).get(
                    "run_status"
                ),
            }
        )

    if not rows:
        return pd.DataFrame(
            columns=["timestamp", "gross_exposure_gbp", "effective_leverage", "flags"]
        )

    frame = pd.DataFrame(rows)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
    return frame.sort_values("timestamp").reset_index(drop=True)
