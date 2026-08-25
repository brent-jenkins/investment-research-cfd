"""Monitoring snapshots on disk: append-only, one file per pass.

Same shape as :mod:`cfdinvest.persist.risk`, for the same reason — a snapshot
is a *reading*, and the useful question afterwards is almost always "when did
this start", not "what is it now". A red status that has been red for a week
is a different problem from one that turned red an hour ago, and only the
history can tell them apart.

Writes are atomic. Reads never raise.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pandas as pd

from cfdinvest.persist.storage import account_root, atomic_write_text

logger = logging.getLogger(__name__)

__all__ = [
    "MONITORING_DIR",
    "SCHEMA_VERSION",
    "monitoring_root",
    "write_monitoring_snapshot",
    "load_latest_monitoring_snapshot",
    "list_monitoring_snapshots",
    "load_monitoring_history",
    "prune_monitoring_snapshots",
]

MONITORING_DIR = "monitoring"
SCHEMA_VERSION = 1

#: ``YYYYMMDD-HHMMSS-monitor.json`` — sorts in time order, so "latest" is a
#: sort rather than a stat of every file.
FILENAME_FORMAT = "%Y%m%d-%H%M%S"


def monitoring_root(config=None, account: str | None = None) -> Path:
    """Where monitoring snapshots live."""
    return account_root(config, account) / MONITORING_DIR


def write_monitoring_snapshot(
    snapshot, config=None, *, account: str | None = None,
    now: pd.Timestamp | None = None,
) -> str | None:
    """Write one snapshot. Returns its path, or ``None`` if it could not.

    Never raises. The most likely reason a monitoring write fails is a full
    disk — which is a thing this layer exists to *report*, so failing loudly
    here would replace the report with a crash.
    """
    # The snapshot's own timestamp wins over the caller's: the filename and
    # the content must not be able to disagree, or "the latest file" and "the
    # latest timestamp" stop being the same snapshot.
    moment = getattr(snapshot, "timestamp", None)
    if moment is None:
        moment = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="UTC")
    moment = pd.Timestamp(moment)
    if moment.tz is None:
        moment = moment.tz_localize("UTC")

    document = {
        "schema_version": SCHEMA_VERSION,
        **(
            snapshot.to_document()
            if hasattr(snapshot, "to_document")
            else dict(snapshot or {})
        ),
    }
    document.setdefault("timestamp", moment.isoformat())

    root = monitoring_root(config, account)
    path = root / f"{moment.strftime(FILENAME_FORMAT)}-monitor.json"

    # Append-only means a second pass in the same second must not land on the
    # first. Unlikely on a schedule measured in minutes, and the claim in the
    # docstring should be true rather than nearly true.
    if path.exists():
        stamp = moment.strftime(FILENAME_FORMAT)
        for suffix in range(2, 100):
            # The counter goes *before* the "-monitor" marker so the name still
            # matches the glob the lister uses. Appending it produced a file
            # that was written and then invisible.
            candidate = root / f"{stamp}-{suffix}-monitor.json"
            if not candidate.exists():
                path = candidate
                break

    try:
        atomic_write_text(json.dumps(document, indent=2), path)
        return str(path)
    except Exception as error:  # noqa: BLE001
        logger.warning("Could not write the monitoring snapshot to %s: %s", path, error)
        return None


def list_monitoring_snapshots(config=None, account: str | None = None) -> list[Path]:
    """Every snapshot, newest first."""
    root = monitoring_root(config, account)
    try:
        if not root.is_dir():
            return []
        return sorted(root.glob("*-monitor.json"), key=lambda p: p.name, reverse=True)
    except OSError as error:
        logger.warning("Monitoring directory unreadable at %s: %s", root, error)
        return []


def _read(path: Path) -> dict | None:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        logger.warning("Monitoring snapshot unreadable at %s: %s", path, error)
        return None
    return document if isinstance(document, dict) else None


def load_latest_monitoring_snapshot(
    config=None, account: str | None = None
) -> dict | None:
    """The most recent snapshot, or ``None`` when none has been written."""
    for path in list_monitoring_snapshots(config, account):
        document = _read(path)
        if document is not None:
            return document
    return None


def load_monitoring_snapshot(path: Path | str) -> dict | None:
    """One specific snapshot, for looking back at a past pass."""
    return _read(Path(path))


def load_monitoring_history(
    config=None, account: str | None = None, *, limit: int = 200
) -> pd.DataFrame:
    """Headline figures from recent snapshots, oldest first.

    A table rather than the documents: charting how long a status has been red
    needs four scalars per pass, and loading two hundred full snapshots to
    draw one line is a lot of JSON for one number.
    """
    rows = []
    for path in list_monitoring_snapshots(config, account)[:limit]:
        document = _read(path)
        if document is None:
            continue
        rows.append(
            {
                "timestamp": document.get("timestamp"),
                "overall_status": document.get("overall_status"),
                "critical": document.get("critical_count"),
                "warning": document.get("warning_count"),
                "info": document.get("info_count"),
                "headline": document.get("headline"),
                "run_id": document.get("run_id"),
                "path": str(path),
            }
        )

    if not rows:
        return pd.DataFrame(
            columns=["timestamp", "overall_status", "critical", "warning", "info",
                     "headline", "run_id", "path"]
        )

    frame = pd.DataFrame(rows)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
    return frame.sort_values("timestamp").reset_index(drop=True)


def prune_monitoring_snapshots(
    config=None, account: str | None = None, *, keep: int = 500
) -> int:
    """Delete all but the newest ``keep`` snapshots. Returns how many went.

    A pass per run on a daily schedule is small; a pass every fifteen minutes
    for a year is twenty-eight thousand files. Pruning is opt-in and never
    automatic on write, because the layer that reports a full disk should not
    also be the layer that quietly deletes evidence to avoid one.
    """
    paths = list_monitoring_snapshots(config, account)
    removed = 0
    for path in paths[keep:]:
        try:
            path.unlink()
            removed += 1
        except OSError as error:  # noqa: PERF203
            logger.warning("Could not remove %s: %s", path, error)
    return removed
