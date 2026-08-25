"""Simulation artefacts on disk, kept where a real one cannot be.

Everything here is written under the ``simulation`` account tree — the same
isolation the virtual and live separation already provides and already tests.
That is the cheapest possible defence against the one failure this layer can
cause, which is someone reading a rehearsal as their actual record.

The account is not configurable. A caller that could choose where simulated
results went would eventually choose wrong.

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
    "SIMULATION_DIR",
    "SIMULATION_ACCOUNT",
    "SCHEMA_VERSION",
    "simulation_root",
    "write_simulation",
    "load_latest_simulation",
    "list_simulations",
    "load_simulation_history",
]

SIMULATION_DIR = "simulations"
SIMULATION_ACCOUNT = "simulation"
SCHEMA_VERSION = 1

FILENAME_FORMAT = "%Y%m%d-%H%M%S"


def simulation_root(config=None) -> Path:
    """Where simulations live. Always the simulation tree.

    No ``account`` parameter, deliberately: the whole point is that this
    cannot be pointed at the tree holding real results.
    """
    return account_root(config, SIMULATION_ACCOUNT) / SIMULATION_DIR


def write_simulation(run, config=None, *, now=None) -> str | None:
    """Write one simulation. Returns its path, or ``None`` if it could not.

    Never raises: a rehearsal that failed to file is a lost rehearsal, and
    taking the caller down with it would be a worse trade.
    """
    moment = getattr(run, "timestamp", None)
    if moment is None:
        moment = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="UTC")
    moment = pd.Timestamp(moment)
    if moment.tz is None:
        moment = moment.tz_localize("UTC")

    document = {
        "schema_version": SCHEMA_VERSION,
        **(run.to_document() if hasattr(run, "to_document") else dict(run or {})),
    }
    # Belt and braces. The controller sets this; if a caller ever hands us a
    # plain mapping, it still comes out marked.
    document["simulated"] = True

    root = simulation_root(config)
    path = root / f"{moment.strftime(FILENAME_FORMAT)}-simulation.json"
    if path.exists():
        stamp = moment.strftime(FILENAME_FORMAT)
        for suffix in range(2, 100):
            candidate = root / f"{stamp}-{suffix}-simulation.json"
            if not candidate.exists():
                path = candidate
                break

    try:
        atomic_write_text(json.dumps(document, indent=2), path)
        return str(path)
    except Exception as error:  # noqa: BLE001
        logger.warning("Could not write the simulation to %s: %s", path, error)
        return None


def list_simulations(config=None) -> list[Path]:
    """Every simulation, newest first."""
    root = simulation_root(config)
    try:
        if not root.is_dir():
            return []
        return sorted(
            root.glob("*-simulation.json"), key=lambda p: p.name, reverse=True
        )
    except OSError as error:
        logger.warning("Simulation directory unreadable at %s: %s", root, error)
        return []


def _read(path: Path) -> dict | None:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        logger.warning("Simulation unreadable at %s: %s", path, error)
        return None
    return document if isinstance(document, dict) else None


def load_latest_simulation(config=None) -> dict | None:
    """The most recent simulation, or ``None`` when none has been written."""
    for path in list_simulations(config):
        document = _read(path)
        if document is not None:
            return document
    return None


def load_simulation_history(config=None, *, limit: int = 100) -> pd.DataFrame:
    """Headline figures from recent simulations, oldest first."""
    rows = []
    for path in list_simulations(config)[:limit]:
        document = _read(path)
        if document is None:
            continue
        differences = document.get("differences") or {}
        rows.append(
            {
                "timestamp": document.get("timestamp"),
                "run_id": document.get("run_id"),
                "label": document.get("label"),
                "headline": document.get("headline"),
                "equity_difference": differences.get("final_equity"),
                "adjustments": document.get("adjustments"),
                "forced_closures": document.get("forced_closures"),
                "simulated": document.get("simulated", True),
                "path": str(path),
            }
        )

    if not rows:
        return pd.DataFrame(
            columns=["timestamp", "run_id", "label", "headline",
                     "equity_difference", "adjustments", "forced_closures",
                     "simulated", "path"]
        )

    frame = pd.DataFrame(rows)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
    return frame.sort_values("timestamp").reset_index(drop=True)
