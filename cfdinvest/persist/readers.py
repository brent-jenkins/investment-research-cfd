"""Reading run artefacts back. Pure, read-only, and never raising.

These feed a dashboard whose job includes reporting that the system is
broken, so a missing file, a truncated document or an unreadable directory
all have to come back as "not available" rather than as a traceback.

**Absence and failure are different.** A run that never wrote a P&L frame is
not the same as one whose P&L frame cannot be read; only the second is a
fault. Frame readers return an empty frame either way, and
:func:`artefact_status` distinguishes them when a caller needs to know.

**Nothing is recomputed.** A reader returns what was written or nothing at
all. Silently recalculating a missing artefact would make the dashboard show
numbers that no run ever produced.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pandas as pd

from cfdinvest import storage as lake_storage
from cfdinvest.persist import schema
from cfdinvest.persist.storage import (
    get_run_directory,
    latest_run_directory,
    list_run_directories,
)

logger = logging.getLogger(__name__)

__all__ = [
    "list_runs",
    "load_latest_run",
    "run_directory_for",
    "artefact_status",
    "load_frame",
    "load_document",
    "load_analysis_frame",
    "load_regime_frame",
    "load_execution_frame",
    "load_pnl_frame",
    "load_execution_metadata",
    "load_run_metadata",
    "load_cost_breakdown",
    "load_attribution",
    "load_stop_events",
    "load_run_warnings",
]


def list_runs(config=None, account: str | None = None) -> list[str]:
    """Run identifiers, newest first."""
    return [p.name for p in list_run_directories(config, account)]


def load_latest_run(config=None, account: str | None = None) -> str | None:
    """The most recent run identifier, or ``None`` when nothing has run."""
    directory = latest_run_directory(config, account)
    return directory.name if directory else None


def run_directory_for(
    run_id: str | None, config=None, account: str | None = None
) -> Path | None:
    """Resolve a run identifier, defaulting to the newest run."""
    if run_id is None:
        return latest_run_directory(config, account)
    directory = get_run_directory(run_id, config, account)
    return directory if directory.is_dir() else None


def artefact_status(
    run_id: str | None = None, config=None, account: str | None = None
) -> pd.DataFrame:
    """Which artefacts a run has, and how large they are.

    The table the dashboard needs to say "regimes were not persisted for this
    run" rather than drawing an empty panel.
    """
    directory = run_directory_for(run_id, config, account)
    rows = []
    for spec in schema.ARTEFACTS.values():
        path = None if directory is None else directory / spec.filename
        present = bool(path and path.is_file())
        rows.append(
            {
                "artefact": spec.name,
                "filename": spec.filename,
                "present": present,
                "bytes": path.stat().st_size if present else 0,
                "description": spec.description,
            }
        )
    return pd.DataFrame(rows).set_index("artefact")


# --------------------------------------------------------------------------- #
# Primitives
# --------------------------------------------------------------------------- #
def load_frame(
    artefact: str, run_id: str | None = None, config=None,
    account: str | None = None,
) -> pd.DataFrame:
    """One Parquet artefact, or an empty frame."""
    spec = schema.spec_for(artefact)
    directory = run_directory_for(run_id, config, account)
    if directory is None:
        return pd.DataFrame()

    path = directory / spec.filename
    try:
        if not path.is_file():
            return pd.DataFrame()
        return lake_storage.read_parquet(path)
    except Exception as error:  # noqa: BLE001 - a dashboard must not crash
        logger.warning("Artefact %s unreadable at %s: %s", artefact, path, error)
        return pd.DataFrame()


def load_document(
    artefact: str, run_id: str | None = None, config=None,
    account: str | None = None,
) -> dict[str, Any] | None:
    """One JSON artefact's payload, or ``None``."""
    spec = schema.spec_for(artefact)
    directory = run_directory_for(run_id, config, account)
    if directory is None:
        return None

    path = directory / spec.filename
    try:
        if not path.is_file():
            return None
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        logger.warning("Artefact %s unreadable at %s: %s", artefact, path, error)
        return None

    if not isinstance(document, dict):
        return None
    if "payload" not in document:
        # The scheduler's own summary.json is a bare document rather than a
        # wrapped one. Reading it as its own payload keeps one reader for
        # both rather than making every caller know which is which.
        return document
    if document.get("schema_version") != spec.version:
        logger.info(
            "Artefact %s at %s was written by schema %s, reading as %s",
            artefact, path, document.get("schema_version"), spec.version,
        )
    return document["payload"]


# --------------------------------------------------------------------------- #
# Named readers
# --------------------------------------------------------------------------- #
def load_analysis_frame(run_id: str | None = None, config=None, account=None):
    """Preprocessed panels in long form. Empty when the run did not store them."""
    return load_frame("analysis", run_id, config, account)


def load_regime_frame(run_id: str | None = None, config=None, account=None):
    """Regime classifications in long form."""
    return load_frame("regimes", run_id, config, account)


def load_pnl_frame(run_id: str | None = None, config=None, account=None):
    """The engine's per-bar decomposition, whose components sum to the total."""
    return load_frame("pnl", run_id, config, account)


def load_cost_breakdown(run_id: str | None = None, config=None, account=None):
    """Cost totals per instrument."""
    return load_frame("costs", run_id, config, account)


def load_attribution(run_id: str | None = None, config=None, account=None):
    """The P&L attribution table."""
    return load_frame("attribution", run_id, config, account)


def load_stop_events(run_id: str | None = None, config=None, account=None):
    """One row per stop-loss or take-profit breach."""
    return load_frame("stop_events", run_id, config, account)


def load_execution_metadata(run_id: str | None = None, config=None, account=None):
    """Every setting the execution engine ran under."""
    return load_document("execution_metadata", run_id, config, account)


def load_run_metadata(run_id: str | None = None, config=None, account=None):
    """Run identity, timing, universe, warnings and the config snapshot."""
    return load_document("metadata", run_id, config, account)


def load_run_warnings(run_id: str | None = None, config=None, account=None) -> list[str]:
    """A run's recorded warnings. Empty when there are none *or* none stored.

    The two are distinguishable through :func:`artefact_status`; conflating
    them here would let "not recorded" render as "nothing wrong".
    """
    metadata = load_run_metadata(run_id, config, account) or {}
    warnings = metadata.get("warnings")
    return list(warnings) if isinstance(warnings, (list, tuple)) else []


def load_execution_frame(
    run_id: str | None = None, config=None, account: str | None = None
) -> dict[str, pd.DataFrame]:
    """Trades, positions, equity and stop events, as a dictionary.

    Not an :class:`~cfdinvest.execution.frame.ExecutionFrame`: that object
    carries configuration and derived views that were never serialised, and
    returning a half-populated one would invite callers to trust fields that
    are empty for storage reasons rather than for run reasons.
    """
    parts = {
        name: load_frame(name, run_id, config, account)
        for name in ("trades", "positions", "equity", "stop_events")
    }
    equity = parts.get("equity")
    if equity is not None and not equity.empty and "equity" in equity.columns:
        parts["equity_curve"] = pd.to_numeric(
            equity["equity"], errors="coerce"
        ).rename("equity")
    return parts
