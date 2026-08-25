"""Keeping the run directory from filling the disk.

A complete run is about 31MB, and 98% of that is one artefact: the analysis
frame, which is the preprocessed panels in long form and three quarters
correlation rows. Everything else together -- trades, positions, equity,
P&L, signals, the regime labels, the metadata -- is under half a megabyte.

That difference is the whole design. The heavy artefact is only ever read
for the **most recent** run, because that is what the simulation layer
rebuilds from; the light ones are the record of what the system did, and a
year of them is a few hundred megabytes. So they are kept on different
terms:

* the last few runs keep everything,
* every run keeps its light artefacts for a long time,
* beyond that the directory goes.

Roughly 11GB a year becomes roughly 280MB.

Two refusals worth stating. The newest run is never touched, whatever the
settings say -- it is what every reader defaults to, and a retention policy
that can delete the thing the dashboard is about to open is a bug with a
schedule. And a run is never *partly* pruned into an unreadable state: the
heavy artefacts are removed as a set, and the metadata that says a run
existed stays.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "HEAVY_ARTEFACTS",
    "DEFAULT_KEEP_FULL",
    "DEFAULT_KEEP_RUNS",
    "PrunePlan",
    "plan_pruning",
    "prune_runs",
]

#: The artefacts worth removing. Both are rebuildable: the analysis frame
#: from the lake, attribution from the trades. Nothing here removes something
#: that cannot be produced again from what remains.
HEAVY_ARTEFACTS: tuple[str, ...] = ("analysis.parquet", "attribution.parquet")

#: How many recent runs keep every artefact. Five rather than one because
#: the simulation reads the latest, and you want a couple to compare against
#: when the latest looks wrong.
DEFAULT_KEEP_FULL = 5

#: How many runs are kept at all. A year of daily runs, at under half a
#: megabyte each once slimmed.
DEFAULT_KEEP_RUNS = 365


@dataclass(frozen=True, slots=True)
class PrunePlan:
    """What pruning would do. Pure: deciding is separate from deleting."""

    slim: tuple[str, ...] = ()
    delete: tuple[str, ...] = ()
    keep_whole: tuple[str, ...] = ()

    @property
    def touches_anything(self) -> bool:
        return bool(self.slim or self.delete)


def plan_pruning(
    run_ids: Sequence[str],
    *,
    keep_full: int = DEFAULT_KEEP_FULL,
    keep_runs: int = DEFAULT_KEEP_RUNS,
) -> PrunePlan:
    """Decide what to slim and what to remove. Pure, and never destructive.

    ``run_ids`` in any order; they sort lexicographically, which for
    ``20260825T150644Z`` is chronological.

    The newest run is always kept whole, even if ``keep_full`` is zero. Every
    reader defaults to it, and a policy that can delete what the dashboard is
    about to open is a bug with a schedule attached.
    """
    ordered = sorted({str(run) for run in run_ids or ()}, reverse=True)
    if not ordered:
        return PrunePlan()

    full = max(int(keep_full), 1)
    total = max(int(keep_runs), full)

    keep_whole = tuple(ordered[:full])
    slim = tuple(ordered[full:total])
    delete = tuple(ordered[total:])
    return PrunePlan(slim=slim, delete=delete, keep_whole=keep_whole)


def prune_runs(
    config=None,
    *,
    account: str | None = None,
    keep_full: int = DEFAULT_KEEP_FULL,
    keep_runs: int = DEFAULT_KEEP_RUNS,
    dry_run: bool = False,
) -> dict:
    """Apply :func:`plan_pruning`. Returns what it did. Never raises.

    A failure to prune must not fail whatever asked for it -- this runs after
    a pipeline run, and a full disk is a worse outcome than a failed delete
    but neither is a reason to lose the run that just completed.
    """
    from cfdinvest.persist import readers
    from cfdinvest.persist.storage import account_root

    result = {
        "slimmed": [], "deleted": [], "freed_bytes": 0,
        "dry_run": bool(dry_run), "error": None,
    }

    try:
        runs = readers.list_runs(config, account=account)
        root = account_root(config, account) / "runs"
    except Exception as error:  # noqa: BLE001
        result["error"] = f"could not list runs: {error}"
        logger.warning("Pruning skipped: %s", result["error"])
        return result

    plan = plan_pruning(runs, keep_full=keep_full, keep_runs=keep_runs)
    if not plan.touches_anything:
        return result

    for run_id in plan.slim:
        directory = root / run_id
        for name in HEAVY_ARTEFACTS:
            path = directory / name
            try:
                if not path.is_file():
                    continue
                size = path.stat().st_size
                if not dry_run:
                    path.unlink()
                result["freed_bytes"] += size
                result["slimmed"].append(f"{run_id}/{name}")
            except OSError as error:
                logger.warning("Could not remove %s: %s", path, error)

    for run_id in plan.delete:
        directory = root / run_id
        try:
            if not directory.is_dir():
                continue
            size = sum(f.stat().st_size for f in directory.rglob("*") if f.is_file())
            if not dry_run:
                import shutil

                shutil.rmtree(directory)
            result["freed_bytes"] += size
            result["deleted"].append(run_id)
        except OSError as error:
            logger.warning("Could not remove %s: %s", directory, error)

    if result["slimmed"] or result["deleted"]:
        logger.info(
            "Pruned runs: slimmed %d, removed %d, freed %.1f MB%s",
            len(result["slimmed"]), len(result["deleted"]),
            result["freed_bytes"] / 1e6, " (dry run)" if dry_run else "",
        )
    return result
