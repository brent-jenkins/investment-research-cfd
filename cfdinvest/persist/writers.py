"""Writing run artefacts. Atomic, append-only, and never destructive.

Three rules, and the reason for each:

**Atomic.** Every file is written to a temporary name and renamed into place.
An unattended machine writing at midnight will eventually be interrupted
mid-write, and a half-written Parquet that reads as *corrupt* is much worse
than one that reads as *absent*.

**Append-only.** A run directory is added to, never cleared. A rerun under the
same identifier overwrites individual artefacts but never deletes the others,
so a retry cannot destroy the record of the attempt it was retrying.

**Never fatal.** A writer that fails logs and returns, and the caller carries
on. Persistence exists to make a completed run inspectable later; a failure to
record it must not also destroy it. The one exception is the run metadata
itself, whose absence would leave an unlabelled directory — and even that is
reported rather than raised.

Frames are validated against :mod:`cfdinvest.persist.schema` before writing,
and a mismatch is logged rather than refused: losing a whole run's record over
one renamed column protects nothing.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from cfdinvest import storage as lake_storage
from cfdinvest.persist import schema
from cfdinvest.persist.storage import atomic_write_text

logger = logging.getLogger(__name__)

__all__ = [
    "jsonable",
    "write_frame",
    "write_document",
    "write_analysis_frame",
    "write_regime_frame",
    "write_execution_frame",
    "write_pnl_frame",
    "write_execution_metadata",
    "write_run_metadata",
    "write_cost_breakdown",
    "write_attribution",
    "write_all",
]


def jsonable(value: Any) -> Any:
    """Coerce numpy, pandas and dataclass values into JSON-safe ones.

    Recursive and total: anything it cannot place becomes its ``str``. A
    metadata document that failed to serialise because one value was a
    ``numpy.float64`` would lose the other forty that were fine.

    ``NaN`` and infinities become ``None`` — JSON cannot spell them and every
    parser disagrees about what to do instead.
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
    if isinstance(value, (pd.Timestamp, pd.Timedelta)):
        return value.isoformat()
    if isinstance(value, pd.Series):
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


# --------------------------------------------------------------------------- #
# Primitives
# --------------------------------------------------------------------------- #
def write_frame(frame: pd.DataFrame, run_dir: Path, artefact: str) -> str | None:
    """Write one Parquet artefact. Returns its path, or ``None``.

    An empty frame is skipped rather than written: an absent file says "the
    run produced none of these", which is true and cheap to check, whereas a
    zero-row Parquet says the same thing while costing a read to find out.
    """
    spec = schema.spec_for(artefact)
    if frame is None or getattr(frame, "empty", True):
        logger.debug("Nothing to write for %s", artefact)
        return None

    missing = schema.validate_frame(frame, artefact)
    if missing:
        logger.warning(
            "%s is missing %s; writing anyway so the run is still recorded",
            artefact, ", ".join(missing),
        )

    path = Path(run_dir) / spec.filename
    try:
        # The lake's writer is already atomic (temp file, then os.replace)
        # and is reused so that every Parquet in this project is produced
        # exactly one way.
        lake_storage.write_parquet(frame, path)
        return str(path)
    except Exception as error:  # noqa: BLE001 - recording must not destroy
        logger.warning("Could not write %s to %s: %s", artefact, path, error)
        return None


def write_document(payload: Any, run_dir: Path, artefact: str) -> str | None:
    """Write one JSON artefact, wrapped with its schema version."""
    spec = schema.spec_for(artefact)
    path = Path(run_dir) / spec.filename
    document = {
        "schema_version": spec.version,
        "artefact": spec.name,
        "payload": jsonable(payload),
    }
    try:
        atomic_write_text(json.dumps(document, indent=2), path)
        return str(path)
    except Exception as error:  # noqa: BLE001
        logger.warning("Could not write %s to %s: %s", artefact, path, error)
        return None


# --------------------------------------------------------------------------- #
# Frames
# --------------------------------------------------------------------------- #
#: Above this many distinct labels, build the column by string concatenation
#: instead of as a categorical. The categorical route enumerates the product
#: of the index levels, which is only worth doing while that product is small
#: relative to the rows it labels -- and it always is for the panels here.
MAX_ENUMERATED_LABELS = 100_000


def _series_labels(index: pd.MultiIndex, levels: int):
    """The ``series`` column for a stacked panel: ``SPY``, or ``SPY|GLD``.

    Returns a Categorical wherever it can, and that is the whole point.

    A correlation panel is ``(timestamp, ticker) x ticker``, so it stacks to
    bars x n x n rows -- 43 instruments over eight years is 4.17 million.
    This column used to be built with ``flat[keys].astype(str).agg("|".join,
    axis=1)``: a row-wise Python loop allocating a string per row. A
    deployment was OOM-killed by it four times, at three gigabytes and
    climbing, to write a file of 512K.

    The labels are drawn from the index's own levels, so a correlation panel
    has at most n x n = 1,849 distinct values however many bars it spans.
    Encoding 4.17 million rows as codes into 1,849 categories costs a few
    megabytes rather than a gigabyte, and Parquet stores it dictionary-
    encoded, which is what it was going to do with the strings anyway.

    (Note this is a categorical *column*, not a categorical column *index* --
    the latter does not survive a Parquet round trip. See ``storage``.)
    """
    key_levels = list(range(1, levels))
    if not key_levels:
        return ""

    sizes = [len(index.levels[i]) for i in key_levels]
    total = 1
    for size in sizes:
        total *= max(size, 1)

    if total and total <= MAX_ENUMERATED_LABELS:
        # Fold the per-level codes into one code in the product space, then
        # name only the products. Rows never hold a string.
        code = np.zeros(len(index), dtype=np.int64)
        missing = np.zeros(len(index), dtype=bool)
        for position, level in enumerate(key_levels):
            codes = np.asarray(index.codes[level], dtype=np.int64)
            missing |= codes < 0
            code = code * sizes[position] + np.maximum(codes, 0)

        values = [index.levels[level].astype(str) for level in key_levels]
        categories = values[0]
        for nxt in values[1:]:
            categories = [f"{a}|{b}" for a in categories for b in nxt]
        categories = list(categories)

        code = np.where(missing, -1, code)
        return pd.Categorical.from_codes(code, categories=categories)

    # Fallback for a panel with more distinct labels than rows to save on.
    # Vectorised regardless: the row-wise join is never the right answer.
    parts = [index.get_level_values(level).astype(str) for level in key_levels]
    joined = pd.Series(parts[0])
    if len(parts) > 1:
        joined = joined.str.cat([pd.Series(part) for part in parts[1:]], sep="|")
    return joined.to_numpy()


def _to_long(panels: dict[str, pd.DataFrame], value_name: str) -> pd.DataFrame:
    """Stack a dictionary of panels into one long frame.

    Long rather than wide because the number of panels varies with
    configuration — a run with correlations enabled has more than one without
    — and a wide file whose columns depend on settings is one no reader can
    have a fixed schema for.

    **Panels do not all have the same shape.** Prices and returns are
    ``timestamp x ticker`` and stack into two levels; correlations are
    already ``(timestamp, pair) x ticker`` and stack into three. Assuming two
    was the bug that stopped the analysis frame being written at all, so the
    level count is read rather than assumed, and anything beyond the
    timestamp is folded into one ``series`` column — ``SPY`` for a price,
    ``SPY|GLD`` for a correlation pair.
    """
    blocks = []
    for name, panel in (panels or {}).items():
        if panel is None or getattr(panel, "empty", True):
            continue
        try:
            stacked = panel.stack(future_stack=True).rename(value_name).to_frame()
        except Exception as error:  # noqa: BLE001 - one odd panel, not the lot
            logger.warning("Could not stack panel %s: %s", name, error)
            continue

        levels = stacked.index.nlevels
        names = ["timestamp"] + [f"level_{n}" for n in range(1, levels)]
        stacked.index = stacked.index.set_names(names)
        flat = stacked.reset_index()

        keys = names[1:]
        if keys:
            flat["series"] = _series_labels(stacked.index, levels)
            flat = flat.drop(columns=keys)
        else:
            flat["series"] = ""

        flat.insert(1, "panel", name)
        blocks.append(flat[["timestamp", "panel", "series", value_name]])

    if not blocks:
        return pd.DataFrame(columns=["timestamp", "panel", "series", value_name])
    return pd.concat(blocks, ignore_index=True)


#: The panel groups written to the long-form analysis artefact.
#:
#: **`correlations` is deliberately absent, and it is the reason this
#: constant exists rather than a literal in the function below.**
#:
#: A rolling correlation panel is every asset against every other, so it is
#: QUADRATIC in the universe: 109 instruments is 11,881 series per bar, and
#: over 6,400 bars that is roughly 76 million values. `_to_long` then stacks
#: it and gives every row a string label, and `pd.concat` materialises those
#: categoricals into Python strings -- gigabytes of them.
#:
#: It stalled the live daemon on 2026-09-11. The pipeline began at 13:35 and
#: was still inside this call at 14:24, in uninterruptible sleep, thrashing
#: against its memory cgroup:
#:
#:     14:19   RSS 3.55 GB   memory.events high 111,331
#:     14:23   RSS 5.59 GB   memory.events high 146,823
#:
#: `trade-capital` runs after `run-intraday` in the same tick, so the account
#: did not deal at all that session and nothing alerted -- the daemon was
#: alive and busy, just not doing anything.
#:
#: **And the memory limit had been hiding it.** This file's sibling unit
#: records the pipeline "peaking at 1801MB against MemoryHigh=1800M" on
#: 2026-09-09. That was not a peak, it was the ceiling: the run had been
#: memory-starved and throttled for days, and raising the cap to 3G simply
#: let it grow to 3G. Raising it again to 5G on 2026-09-11 let it reach
#: 5.6GB within four minutes. A limit is not a diagnosis.
#:
#: What is lost is a sweep-time convenience, not information: correlations
#: are derived from `returns`, which IS persisted, so any consumer can
#: recompute them. `monitoring` already lists `analysis` as an optional
#: artefact, and the dealing path reads `positions`, `equity` and `signals`
#: rather than this.
ANALYSIS_PANEL_GROUPS: tuple[str, ...] = ("prices", "returns", "volatility")


def write_analysis_frame(run_dir: Path, analysis_frame) -> str | None:
    """Preprocessed panels — prices, returns, volatility, ATR — in long form.

    Stored so that analytics need not re-run preprocessing for every point of
    a parameter sweep. This is the single change that makes a fifty-point
    sweep affordable.

    Correlations are excluded because they are quadratic in the universe and
    stalled the live daemon -- see `ANALYSIS_PANEL_GROUPS`.
    """
    if analysis_frame is None:
        return None
    panels: dict[str, pd.DataFrame] = {}
    for group in ANALYSIS_PANEL_GROUPS:
        panels.update(
            {
                f"{group}.{name}": panel
                for name, panel in (getattr(analysis_frame, group, {}) or {}).items()
                if isinstance(panel, pd.DataFrame)
            }
        )
    return write_frame(_to_long(panels, "value"), run_dir, "analysis")


#: The five classifications a RegimeFrame carries. Each is a **market-level
#: Series**, one label per bar — not a per-asset panel. Writing them as
#: columns of one frame is both simpler and truer than stacking, which was
#: the second reason the regime artefact never appeared.
REGIME_SERIES: tuple[str, ...] = (
    "volatility_regime",
    "trend_regime",
    "correlation_regime",
    "macro_regime",
    "combined_regime",
)


def write_regime_frame(run_dir: Path, regime_frame) -> str | None:
    """Regime classifications, one column per classifier.

    Computed in memory today and discarded, which is why the dashboard can
    only show the reason string the signal layer embedded.
    """
    if regime_frame is None:
        return None

    columns = {}
    for name in REGIME_SERIES:
        series = getattr(regime_frame, name, None)
        if isinstance(series, pd.Series) and not series.empty:
            columns[name] = series

    if not columns:
        return None

    frame = pd.DataFrame(columns)
    frame.index.name = "timestamp"
    return write_frame(frame.reset_index(), run_dir, "regimes")


def write_execution_frame(run_dir: Path, execution_frame) -> dict[str, str]:
    """The trade log, positions, equity and stop events.

    The first three already exist; this writes them through the same path so
    a run persisted by this layer is complete on its own rather than half
    written by two different callers.
    """
    written: dict[str, str] = {}
    if execution_frame is None:
        return written

    for artefact, frame in (
        ("trades", getattr(execution_frame, "trades", None)),
        ("positions", getattr(execution_frame, "positions", None)),
        ("stop_events", getattr(execution_frame, "stop_events", None)),
    ):
        path = write_frame(
            frame.reset_index(drop=True)
            if artefact in ("trades", "stop_events") and frame is not None
            and not frame.empty
            else frame,
            run_dir,
            artefact,
        )
        if path:
            written[artefact] = path

    equity = getattr(execution_frame, "equity_curve", None)
    if equity is not None and len(equity):
        path = write_frame(equity.to_frame("equity"), run_dir, "equity")
        if path:
            written["equity"] = path

    return written


def write_pnl_frame(run_dir: Path, pnl_frame) -> str | None:
    """The engine's own per-bar decomposition.

    **The artefact this layer exists for.** Its six components sum to the
    change in equity, and without it the analytics and dashboard layers have
    to reconstruct the split from the trade log — exact for costs, but unable
    to separate the overnight gap from the intraday move.
    """
    frame = pnl_frame
    if hasattr(pnl_frame, "pnl"):
        frame = pnl_frame.pnl
    return write_frame(frame, run_dir, "pnl")


def write_cost_breakdown(run_dir: Path, execution_frame) -> str | None:
    """Cost totals per instrument, from the trade log."""
    try:
        from cfdinvest.analytics.attribution import compute_instrument_attribution

        return write_frame(
            compute_instrument_attribution(execution_frame).reset_index(),
            run_dir,
            "costs",
        )
    except Exception as error:  # noqa: BLE001
        logger.warning("Could not compute the cost breakdown: %s", error)
        return None


def write_attribution(run_dir: Path, execution_frame, analysis_frame=None) -> str | None:
    """The P&L attribution, as a table whose terms sum to the total."""
    try:
        from cfdinvest.analytics.attribution import attribution_frame

        return write_frame(
            attribution_frame(
                execution_frame, analysis_frame=analysis_frame
            ).reset_index(),
            run_dir,
            "attribution",
        )
    except Exception as error:  # noqa: BLE001
        logger.warning("Could not compute the attribution: %s", error)
        return None


# --------------------------------------------------------------------------- #
# Documents
# --------------------------------------------------------------------------- #
def write_execution_metadata(run_dir: Path, metadata: Any) -> str | None:
    """Every setting the execution engine ran under, and its headline figures.

    Without this the Config page can only show built-in defaults, and has to
    say so — it cannot know whether the run used them.
    """
    payload = metadata
    if hasattr(metadata, "metadata"):
        payload = metadata.metadata
    return write_document(payload or {}, run_dir, "execution_metadata")


def write_run_metadata(run_dir: Path, metadata: Any) -> str | None:
    """Run identity, timing, universe, warnings and the config snapshot."""
    return write_document(metadata, run_dir, "metadata")


# --------------------------------------------------------------------------- #
# Everything at once
# --------------------------------------------------------------------------- #
def write_all(
    run_dir: Path,
    *,
    analysis_frame=None,
    regime_frame=None,
    execution_frame=None,
    run_metadata=None,
) -> dict[str, str]:
    """Persist a completed run. Returns what was written, by artefact.

    Ordered so the cheap, always-present artefacts land first: if the process
    dies part-way, what survives is the trade log and the metadata rather
    than a stack of panels with nothing to identify them.
    """
    directory = Path(run_dir)
    directory.mkdir(parents=True, exist_ok=True)
    written: dict[str, str] = {}

    if run_metadata is not None:
        path = write_run_metadata(directory, run_metadata)
        if path:
            written["metadata"] = path

    if execution_frame is not None:
        written.update(write_execution_frame(directory, execution_frame))
        for artefact, function in (
            ("pnl", lambda: write_pnl_frame(directory, execution_frame)),
            ("execution_metadata",
             lambda: write_execution_metadata(directory, execution_frame)),
            ("costs", lambda: write_cost_breakdown(directory, execution_frame)),
            ("attribution",
             lambda: write_attribution(directory, execution_frame, analysis_frame)),
        ):
            path = function()
            if path:
                written[artefact] = path

    if regime_frame is not None:
        path = write_regime_frame(directory, regime_frame)
        if path:
            written["regimes"] = path

    if analysis_frame is not None:
        path = write_analysis_frame(directory, analysis_frame)
        if path:
            written["analysis"] = path

    logger.info("Persisted %d artefact(s) to %s", len(written), directory)
    return written
