"""Meta-strategy profiles on disk: append-only, one file per pass.

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
    "METASTRATEGY_DIR",
    "SCHEMA_VERSION",
    "metastrategy_root",
    "write_meta_strategy",
    "load_latest_meta_strategy",
    "list_meta_strategies",
    "load_meta_strategy_history",
]

METASTRATEGY_DIR = "metastrategy"
SCHEMA_VERSION = 1

FILENAME_FORMAT = "%Y%m%d-%H%M%S"


def metastrategy_root(config=None, account: str | None = None) -> Path:
    """Where meta-strategy profiles live."""
    return account_root(config, account) / METASTRATEGY_DIR


def write_meta_strategy(
    report, config=None, *, account: str | None = None, now=None, inputs=None
) -> str | None:
    """Write one report. Returns its path, or ``None`` if it could not.

    Never raises: failing to file a report must not take down whatever asked
    for it.
    """
    moment = getattr(report, "as_of", None)
    if moment is None:
        moment = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="UTC")
    moment = pd.Timestamp(moment)
    if moment.tz is None:
        moment = moment.tz_localize("UTC")

    document = {
        "schema_version": SCHEMA_VERSION,
        "timestamp": moment.isoformat(),
        "profile": (
            report.to_document()
            if hasattr(report, "to_document")
            else dict(report or {})
        ),
        "inputs": dict(inputs or {}),
        "notes": list(getattr(report, "notes", ()) or ()),
    }

    root = metastrategy_root(config, account)
    path = root / f"{moment.strftime(FILENAME_FORMAT)}-metastrategy.json"
    if path.exists():
        stamp = moment.strftime(FILENAME_FORMAT)
        for suffix in range(2, 100):
            candidate = root / f"{stamp}-{suffix}-metastrategy.json"
            if not candidate.exists():
                path = candidate
                break

    try:
        atomic_write_text(json.dumps(document, indent=2), path)
        return str(path)
    except Exception as error:  # noqa: BLE001
        logger.warning("Could not write the meta-strategy profile to %s: %s", path, error)
        return None


def list_meta_strategies(config=None, account: str | None = None) -> list[Path]:
    """Every report, newest first."""
    root = metastrategy_root(config, account)
    try:
        if not root.is_dir():
            return []
        return sorted(
            root.glob("*-metastrategy.json"), key=lambda p: p.name, reverse=True
        )
    except OSError as error:
        logger.warning("Meta-strategy directory unreadable at %s: %s", root, error)
        return []


def _read(path: Path) -> dict | None:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        logger.warning("Meta-strategy profile unreadable at %s: %s", path, error)
        return None
    return document if isinstance(document, dict) else None


def load_latest_meta_strategy(config=None, account: str | None = None) -> dict | None:
    """The most recent report, or ``None`` when none has been written."""
    for path in list_meta_strategies(config, account):
        document = _read(path)
        if document is not None:
            return document
    return None


def load_meta_strategy_history(
    config=None, account: str | None = None, *, limit: int = 200,
) -> pd.DataFrame:
    """How the posture has moved, oldest first.

    A run of steadily more forward postures is a thing to notice, and no
    single profile shows it.
    """
    rows = []
    for path in list_meta_strategies(config, account)[:limit]:
        document = _read(path)
        if document is None:
            continue
        profile = document.get("profile") or {}
        rows.append(
            {
                "timestamp": document.get("timestamp"),
                "posture": profile.get("posture"),
                "score": profile.get("score"),
                "risk_budget": profile.get("risk_budget"),
                "sizing_multiplier": profile.get("sizing_multiplier"),
                "exit_multiplier": profile.get("exit_multiplier"),
                "confidence": profile.get("confidence"),
                "account": profile.get("account"),
            }
        )

    if not rows:
        return pd.DataFrame(
            columns=["timestamp", "posture", "score", "risk_budget",
                     "sizing_multiplier", "exit_multiplier", "confidence",
                     "account"]
        )

    frame = pd.DataFrame(rows)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
    return frame.sort_values("timestamp").reset_index(drop=True)
