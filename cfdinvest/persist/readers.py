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
    "load_positions",
    "rebuild_analysis_frame",
    "rebuild_signal_frame",
    "load_confirmed_positions",
    "load_confirmed_fills",
    "merge_simulated_and_confirmed_positions",
    "load_merged_positions",
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


def load_positions(
    run_id: str | None = None,
    config=None,
    account: str | None = None,
    *,
    confirmed: bool = True,
) -> tuple[pd.DataFrame, str]:
    """The run's positions, preferring your confirmed fills over the simulated.

    Returns the frame and which of the two it is, because every number
    downstream depends on that choice and a caller that cannot see it cannot
    label its own output honestly.

    The confirmed artefact is written only when something was confirmed, so
    its absence means the simulation stands unchallenged -- which is different
    from a book that was confirmed as flat.
    """
    if confirmed:
        overlaid = load_frame("positions_confirmed", run_id, config, account)
        if not overlaid.empty:
            return overlaid, "confirmed"
    return load_frame("positions", run_id, config, account), "simulated"


def load_execution_frame(
    run_id: str | None = None,
    config=None,
    account: str | None = None,
    *,
    confirmed: bool = True,
) -> dict[str, pd.DataFrame]:
    """Trades, positions, equity and stop events, as a dictionary.

    Not an :class:`~cfdinvest.execution.frame.ExecutionFrame`: that object
    carries configuration and derived views that were never serialised, and
    returning a half-populated one would invite callers to trust fields that
    are empty for storage reasons rather than for run reasons.
    """
    parts = {
        name: load_frame(name, run_id, config, account)
        for name in ("trades", "equity", "stop_events")
    }
    parts["positions"], parts["positions_source"] = load_positions(
        run_id, config, account, confirmed=confirmed
    )
    equity = parts.get("equity")
    if equity is not None and not equity.empty and "equity" in equity.columns:
        parts["equity_curve"] = pd.to_numeric(
            equity["equity"], errors="coerce"
        ).rename("equity")
    return parts


# --------------------------------------------------------------------------- #
# Confirmations: what actually happened
# --------------------------------------------------------------------------- #
# Thin entry points. The model, the replay and the merge live in
# cfdinvest.persist.confirm, so there is one home for what a confirmation
# means and these are only the doors into it from the reading side.
def load_confirmed_positions(config=None, account: str | None = None) -> dict:
    """The book as the confirmations describe it, per instrument.

    Empty when nothing has been confirmed, which is not the same as a flat
    book: it means the simulated positions stand unchallenged.
    """
    from cfdinvest.persist import confirm

    return confirm.confirmed_positions(confirm.load_confirmations(config, account))


def load_confirmed_fills(
    run_id: str | None = None, config=None, account: str | None = None
) -> pd.DataFrame:
    """Confirmed fills, as a table. Filtered to one run when asked.

    Filtering by ``run_id`` returns only confirmations recorded *against* that
    run. A confirmation carries the run it answers, so a trade you took on
    your own initiative is not attributed to a recommendation that never
    existed.
    """
    from cfdinvest.persist import confirm

    found = confirm.load_confirmations(config, account)
    if run_id is not None:
        found = [c for c in found if c.run_id == run_id]
    return confirm.to_frame(found)


def merge_simulated_and_confirmed_positions(simulated, confirmed=None, **kwargs):
    """Overlay confirmed positions onto a simulated panel.

    Re-exported from :mod:`cfdinvest.persist.confirm`; see that module for
    what the merge does and does not do to history.
    """
    from cfdinvest.persist import confirm

    return confirm.merge_simulated_and_confirmed_positions(
        simulated, confirmed, **kwargs
    )


def load_merged_positions(
    run_id: str | None = None, config=None, account: str | None = None
) -> tuple[pd.DataFrame, dict]:
    """A run's positions with confirmations overlaid, and what changed.

    The report is returned rather than logged because a silent override is
    indistinguishable from a bug -- the position changes and nothing says why.
    """
    from cfdinvest.persist import confirm

    return confirm.merge_simulated_and_confirmed_positions(
        load_frame("positions", run_id, config, account),
        confirm.load_confirmations(config, account),
    )


def rebuild_analysis_frame(long_frame=None, run_id=None, config=None, account=None):
    """Rebuild an :class:`AnalysisFrame` from the persisted long form.

    The inverse of ``writers._to_long``. The engine wants panels keyed by
    field -- ``prices["close"]``, ``volatility["atr"]`` -- and what is on disk
    is one long table of ``timestamp, panel, series, value``. Pivoting it back
    is mechanical, and having it here means anything that needs to re-run the
    engine over a stored run does not have to know the storage shape.

    **Not everything survives the round trip.** The correlation panels were
    MultiIndexed matrices before they were flattened, and are returned as they
    are stored rather than reassembled -- nothing that reads this needs them,
    and guessing at the original shape would be worse than saying so. The
    metadata is empty for the same reason: it was written to its own artefact.
    """
    from cfdinvest.preprocessing.frame import AnalysisFrame

    frame = (
        long_frame
        if long_frame is not None
        else load_analysis_frame(run_id, config, account)
    )
    if frame is None or getattr(frame, "empty", True):
        return AnalysisFrame()
    if not {"timestamp", "panel", "series", "value"} <= set(frame.columns):
        logger.warning("The analysis frame is not in the long form; not rebuilt")
        return AnalysisFrame()

    groups: dict[str, dict[str, pd.DataFrame]] = {}
    for name, part in frame.groupby("panel"):
        family, _, field = str(name).partition(".")
        if not field:
            continue
        wide = part.pivot_table(
            index="timestamp", columns="series", values="value", aggfunc="last"
        )
        wide.index = pd.to_datetime(wide.index, utc=True, errors="coerce")
        groups.setdefault(family, {})[field] = wide.sort_index()

    return AnalysisFrame(
        prices=groups.get("prices", {}),
        returns=groups.get("returns", {}),
        volatility=groups.get("volatility", {}),
        correlations=groups.get("correlations", {}),
    )


def rebuild_signal_frame(long_frame=None, run_id=None, config=None, account=None):
    """Rebuild a :class:`SignalFrame` from the persisted long form.

    ``signals.parquet`` is stored MultiIndexed on ``(timestamp, ticker)`` with
    one column per field, and flat cells are not stored at all. The engine
    wants the opposite shape -- one wide ``timestamp x ticker`` panel per
    field -- so this unstacks and fills the gaps back in.

    **The gaps have to be filled correctly or the run is wrong.** A cell that
    was not stored means *flat*, not *unknown*: leaving it NaN would let a
    position persist through a bar the strategy had exited, which is a
    different strategy with better-looking results.
    """
    from cfdinvest.signals.frame import SignalFrame

    frame = (
        long_frame
        if long_frame is not None
        else load_frame("signals", run_id, config, account)
    )
    if frame is None or getattr(frame, "empty", True):
        return SignalFrame()
    if not isinstance(frame.index, pd.MultiIndex) or frame.index.nlevels < 2:
        logger.warning("The signal frame is not MultiIndexed; not rebuilt")
        return SignalFrame()

    panels: dict[str, pd.DataFrame] = {}
    for column in frame.columns:
        wide = frame[column].unstack(level=-1)
        wide.index = pd.to_datetime(wide.index, utc=True, errors="coerce")
        panels[str(column)] = wide.sort_index()

    direction = panels.get("direction", pd.DataFrame())
    if not direction.empty:
        # An unstored cell is flat. See the docstring: NaN here would carry a
        # position through a bar the strategy was out of.
        direction = direction.fillna("flat")
    size = panels.get("size", pd.DataFrame())
    if not size.empty:
        size = size.fillna(0.0)
    confidence = panels.get("confidence", pd.DataFrame())
    if not confidence.empty:
        confidence = confidence.fillna(0.0)
    reason = panels.get("reason", pd.DataFrame())
    if not reason.empty:
        reason = reason.fillna("")

    return SignalFrame(
        direction=direction,
        size=size,
        confidence=confidence,
        reason=reason,
    )
