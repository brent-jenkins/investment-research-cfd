"""Where analytics results are written, and how they are read back.

Analytics artefacts live beside the run they describe, under
``<data_root>/analytics/<run_id>/``, and are written once. Nothing here
modifies a pipeline run: the run directory is read, the analytics directory
is written, and the two never mix. A result that has been written is never
rewritten by a later read.

**Readers never raise.** These feed a dashboard whose job includes reporting
that the system is broken, so a missing file, a truncated JSON document or an
unreadable directory all have to come back as "not available" rather than as
a traceback. Absence and failure are distinguished — an analytics run that has
not happened yet is not the same as one whose output cannot be read — because
only one of them is a fault.

Writes are atomic: a temporary file is renamed into place, so a reader never
sees half a document even if the process dies mid-write.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import pandas as pd

from cfdinvest import storage

logger = logging.getLogger(__name__)

__all__ = [
    "ANALYTICS_DIR",
    "ARTEFACTS",
    "SCHEMA_VERSION",
    "analytics_root",
    "run_directory",
    "list_analytics_runs",
    "save_backtest_result",
    "save_sweep",
    "save_walkforward",
    "save_montecarlo",
    "load_run_metrics",
    "load_run_attribution",
    "load_walkforward_summary",
    "load_montecarlo_summary",
    "load_sweep",
    "has_analytics",
]

#: Sub-directory of the lake that analytics writes into. Never the run
#: directory itself: a pipeline run is an immutable record of what happened,
#: and scoring it later must not touch it.
ANALYTICS_DIR = "analytics"

#: Bumped when a stored document's shape changes. Every artefact carries it,
#: so a reader can tell "written by an older version" from "corrupt".
SCHEMA_VERSION = 1

#: Filenames, by artefact.
ARTEFACTS: dict[str, str] = {
    "metrics": "metrics.json",
    "attribution": "attribution.json",
    "costs": "costs.json",
    "warnings": "warnings.json",
    "sweep": "sweep.parquet",
    "walkforward": "walkforward.parquet",
    "walkforward_summary": "walkforward_summary.json",
    "montecarlo": "montecarlo.json",
    "montecarlo_paths": "montecarlo_paths.parquet",
}


# --------------------------------------------------------------------------- #
# Layout
# --------------------------------------------------------------------------- #
def analytics_root(config) -> Path:
    """The analytics directory for a lake."""
    return Path(config.paths.data_root) / ANALYTICS_DIR


def run_directory(config, run_id: str, *, create: bool = False) -> Path:
    """Where one run's analytics live.

    ``create=False`` by default: a reader asking where something is should
    not bring it into existence, or an empty directory appears for every run
    anybody ever looked at.
    """
    directory = analytics_root(config) / str(run_id)
    if create:
        directory.mkdir(parents=True, exist_ok=True)
    return directory


def list_analytics_runs(config) -> list[str]:
    """Run identifiers that have analytics, newest first.

    Run ids sort lexicographically in time order, which is what makes
    "newest" a sort rather than a stat of every directory.
    """
    root = analytics_root(config)
    try:
        if not root.is_dir():
            return []
        return sorted(
            (p.name for p in root.iterdir() if p.is_dir()), reverse=True
        )
    except OSError as error:  # noqa: BLE001
        logger.warning("Analytics directory unreadable: %s", error)
        return []


def has_analytics(config, run_id: str) -> bool:
    """Whether anything has been written for this run."""
    directory = run_directory(config, run_id)
    return directory.is_dir() and any(directory.iterdir())


# --------------------------------------------------------------------------- #
# Serialisation
# --------------------------------------------------------------------------- #
def jsonable(value: Any) -> Any:
    """Coerce numpy, pandas and dataclass values into JSON-safe ones.

    Recursive and total: anything it cannot place becomes its ``str``. A
    metrics document that fails to serialise because one value was a
    ``numpy.float64`` would lose the other forty that were fine.

    ``NaN`` and infinities become ``None``, because JSON has no way to spell
    them and every parser disagrees about what to do instead.
    """
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        number = float(value)
        return number if np.isfinite(number) else None
    if isinstance(value, np.ndarray):
        return [jsonable(item) for item in value.tolist()]
    if isinstance(value, (pd.Timestamp,)):
        return value.isoformat()
    if isinstance(value, pd.Timedelta):
        return value.isoformat()
    if isinstance(value, (pd.Series,)):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, pd.DataFrame):
        return jsonable(value.reset_index().to_dict("records"))
    if is_dataclass(value) and not isinstance(value, type):
        return jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _document(payload: Any, kind: str) -> dict[str, Any]:
    """Wrap a payload with its schema version and what it is."""
    return {
        "schema_version": SCHEMA_VERSION,
        "artefact": kind,
        "payload": jsonable(payload),
    }


def _write_json(payload: Any, path: Path, kind: str) -> Path:
    """Atomically write one JSON document."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(_document(payload, kind), indent=2, sort_keys=False),
        encoding="utf-8",
    )
    os.replace(temporary, path)
    return path


def _read_json(path: Path) -> dict[str, Any] | None:
    """Read one document back, returning ``None`` for anything unusable."""
    try:
        if not path.is_file():
            return None
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        logger.warning("Analytics artefact unreadable at %s: %s", path, error)
        return None

    if not isinstance(document, dict) or "payload" not in document:
        logger.warning("Analytics artefact at %s has no payload", path)
        return None
    if document.get("schema_version") != SCHEMA_VERSION:
        logger.info(
            "Analytics artefact at %s was written by schema %s, reading as %s",
            path, document.get("schema_version"), SCHEMA_VERSION,
        )
    return document


def _read_frame(path: Path) -> pd.DataFrame:
    """Read one parquet artefact, empty on anything unusable."""
    try:
        if not path.is_file():
            return pd.DataFrame()
        return storage.read_parquet(path)
    except Exception as error:  # noqa: BLE001 - a dashboard must not crash
        logger.warning("Analytics frame unreadable at %s: %s", path, error)
        return pd.DataFrame()


# --------------------------------------------------------------------------- #
# Writers
# --------------------------------------------------------------------------- #
def save_backtest_result(config, run_id: str, result) -> dict[str, str]:
    """Write one scored run's metrics, attribution, costs and warnings."""
    directory = run_directory(config, run_id, create=True)
    written: dict[str, str] = {}

    written["metrics"] = str(
        _write_json(
            {
                "label": result.label,
                "trade_count": result.trade_count,
                "bars": result.bars,
                "period": [jsonable(p) for p in result.period],
                "universe": list(result.universe),
                "config_snapshot": result.config_snapshot,
                "metrics": result.metrics,
            },
            directory / ARTEFACTS["metrics"],
            "metrics",
        )
    )
    written["attribution"] = str(
        _write_json(
            result.attribution, directory / ARTEFACTS["attribution"], "attribution"
        )
    )
    written["costs"] = str(
        _write_json(result.cost_breakdown, directory / ARTEFACTS["costs"], "costs")
    )
    written["warnings"] = str(
        _write_json(list(result.warnings), directory / ARTEFACTS["warnings"], "warnings")
    )
    logger.info("Analytics written for run %s to %s", run_id, directory)
    return written


def save_sweep(config, run_id: str, results: Sequence) -> str:
    """Write a parameter sweep's table."""
    from cfdinvest.analytics.backtest import sweep_frame

    directory = run_directory(config, run_id, create=True)
    path = directory / ARTEFACTS["sweep"]
    storage.write_parquet(sweep_frame(results).reset_index(), path)
    return str(path)


def save_walkforward(config, run_id: str, results: Sequence) -> dict[str, str]:
    """Write every window, and the stability summary over them."""
    from cfdinvest.analytics.walkforward import stability_summary, windows_frame

    directory = run_directory(config, run_id, create=True)
    windows = windows_frame(results)
    path = directory / ARTEFACTS["walkforward"]
    storage.write_parquet(windows.reset_index(), path)

    summary = stability_summary(results)
    summary_path = _write_json(
        {
            "windows": len(results),
            "failed": sum(1 for r in results if not r.ok),
            "stability": summary.reset_index().to_dict("records"),
        },
        directory / ARTEFACTS["walkforward_summary"],
        "walkforward_summary",
    )
    return {"windows": str(path), "summary": str(summary_path)}


def save_montecarlo(
    config, run_id: str, summary: dict, paths: Sequence | None = None
) -> dict[str, str]:
    """Write the resampled distribution, and optionally every path's metrics.

    The paths themselves are not stored — a thousand equity curves is a lot
    of parquet for something reproducible from the seed — but their metrics
    are, because that is what a histogram needs.
    """
    from cfdinvest.analytics.montecarlo import paths_frame

    directory = run_directory(config, run_id, create=True)
    written = {
        "summary": str(
            _write_json(summary, directory / ARTEFACTS["montecarlo"], "montecarlo")
        )
    }
    if paths:
        path = directory / ARTEFACTS["montecarlo_paths"]
        storage.write_parquet(paths_frame(paths).reset_index(), path)
        written["paths"] = str(path)
    return written


# --------------------------------------------------------------------------- #
# Readers — the UI's entry points
# --------------------------------------------------------------------------- #
def _payload(config, run_id: str, artefact: str) -> Any:
    document = _read_json(run_directory(config, run_id) / ARTEFACTS[artefact])
    return None if document is None else document["payload"]


def load_run_metrics(config, run_id: str) -> dict[str, Any] | None:
    """A run's computed metrics, or ``None`` if analytics has not run."""
    return _payload(config, run_id, "metrics")


def load_run_attribution(config, run_id: str) -> dict[str, Any] | None:
    """A run's P&L attribution, or ``None``."""
    return _payload(config, run_id, "attribution")


def load_cost_breakdown(config, run_id: str) -> dict[str, Any] | None:
    """A run's cost breakdown, or ``None``."""
    return _payload(config, run_id, "costs")


def load_run_warnings(config, run_id: str) -> list[str]:
    """A run's analytics warnings. Empty when there are none *or* none ran.

    The two are distinguishable through :func:`has_analytics`; conflating
    them here would let "not scored yet" render as "nothing wrong".
    """
    payload = _payload(config, run_id, "warnings")
    return list(payload) if isinstance(payload, list) else []


def load_walkforward_summary(config, run_id: str) -> dict[str, Any] | None:
    """The stability summary across walk-forward windows, or ``None``."""
    return _payload(config, run_id, "walkforward_summary")


def load_walkforward_windows(config, run_id: str) -> pd.DataFrame:
    """Every walk-forward window as a row. Empty when none were run."""
    return _read_frame(run_directory(config, run_id) / ARTEFACTS["walkforward"])


def load_montecarlo_summary(config, run_id: str) -> dict[str, Any] | None:
    """The resampled outcome distribution, or ``None``."""
    return _payload(config, run_id, "montecarlo")


def load_montecarlo_paths(config, run_id: str) -> pd.DataFrame:
    """Per-path metrics from a Monte Carlo run. Empty when none were run."""
    return _read_frame(run_directory(config, run_id) / ARTEFACTS["montecarlo_paths"])


def load_sweep(config, run_id: str) -> pd.DataFrame:
    """A parameter sweep's table. Empty when none was run."""
    return _read_frame(run_directory(config, run_id) / ARTEFACTS["sweep"])
