"""Insight reports on disk: append-only, one file per pass.

Same shape as the risk and monitoring snapshots, for the same reason: what a
report said last month is the only way to know whether a finding has survived.
An insight that looked strong in June and has quietly reversed is exactly the
thing a single current report cannot show you, and the thing most worth
seeing.

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
    "INSIGHTS_DIR",
    "SCHEMA_VERSION",
    "insights_root",
    "write_insight_report",
    "load_latest_insight_report",
    "list_insight_reports",
    "load_insight_history",
]

INSIGHTS_DIR = "insights"
SCHEMA_VERSION = 1

FILENAME_FORMAT = "%Y%m%d-%H%M%S"


def insights_root(config=None, account: str | None = None) -> Path:
    """Where insight reports live."""
    return account_root(config, account) / INSIGHTS_DIR


def write_insight_report(
    report, config=None, *, account: str | None = None, now=None
) -> str | None:
    """Write one report. Returns its path, or ``None`` if it could not.

    Never raises: failing to file a report must not take down whatever asked
    for it.
    """
    moment = getattr(report, "timestamp", None)
    if moment is None:
        moment = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="UTC")
    moment = pd.Timestamp(moment)
    if moment.tz is None:
        moment = moment.tz_localize("UTC")

    document = {
        "schema_version": SCHEMA_VERSION,
        **(
            report.to_document()
            if hasattr(report, "to_document")
            else dict(report or {})
        ),
    }

    root = insights_root(config, account)
    path = root / f"{moment.strftime(FILENAME_FORMAT)}-insights.json"
    if path.exists():
        stamp = moment.strftime(FILENAME_FORMAT)
        for suffix in range(2, 100):
            candidate = root / f"{stamp}-{suffix}-insights.json"
            if not candidate.exists():
                path = candidate
                break

    try:
        atomic_write_text(json.dumps(document, indent=2), path)
        return str(path)
    except Exception as error:  # noqa: BLE001
        logger.warning("Could not write the insight report to %s: %s", path, error)
        return None


def list_insight_reports(config=None, account: str | None = None) -> list[Path]:
    """Every report, newest first."""
    root = insights_root(config, account)
    try:
        if not root.is_dir():
            return []
        return sorted(
            root.glob("*-insights.json"), key=lambda p: p.name, reverse=True
        )
    except OSError as error:
        logger.warning("Insights directory unreadable at %s: %s", root, error)
        return []


def _read(path: Path) -> dict | None:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        logger.warning("Insight report unreadable at %s: %s", path, error)
        return None
    return document if isinstance(document, dict) else None


def load_latest_insight_report(config=None, account: str | None = None) -> dict | None:
    """The most recent report, or ``None`` when none has been written."""
    for path in list_insight_reports(config, account):
        document = _read(path)
        if document is not None:
            return document
    return None


def load_insight_history(
    config=None, account: str | None = None, *, key: str | None = None,
    limit: int = 200,
) -> pd.DataFrame:
    """How one insight has moved across reports, oldest first.

    The point of keeping the history: a finding that looked strong in June and
    has since reversed is invisible in the current report and obvious in the
    sequence.
    """
    rows = []
    for path in list_insight_reports(config, account)[:limit]:
        document = _read(path)
        if document is None:
            continue
        for item in document.get("insights") or []:
            if key is not None and item.get("key") != key:
                continue
            rows.append(
                {
                    "timestamp": document.get("timestamp"),
                    "key": item.get("key"),
                    "title": item.get("title"),
                    "confidence": item.get("confidence"),
                    "trades": item.get("trades"),
                    "finding": item.get("finding"),
                }
            )

    if not rows:
        return pd.DataFrame(
            columns=["timestamp", "key", "title", "confidence", "trades", "finding"]
        )

    frame = pd.DataFrame(rows)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
    return frame.sort_values("timestamp").reset_index(drop=True)
