"""Deleting yesterday's files must never fail today's run.

Found 2026-09-15, on the box, from a flood of mail. The sequence, from the
journal, three milliseconds apart:

    19:53:36.028  Persisted 10 artefact(s) to .../runs/20260915T195329Z
    19:53:36.031  Scheduled run 20260915T195329Z failed
                  run_pipeline -> _prune_old_runs -> item.unlink()
                  PermissionError: .../runs/20260911T200634Z/trades.parquet

**The run had already done its whole job.** Analysis, regimes, signals,
execution and persistence all completed; the artefacts the dealer reads
were on disk. Then the tidy-up tried to delete a four-day-old directory
that had been written by root during an investigation, raised, and --
because `_prune_old_runs` is called from inside `run_pipeline`'s try block
*after* `_persist` -- skipped `_record_finish`.

The cost of calling that a failed run:

    39   "RUN FAILED" mails in one afternoon
    120  monitoring alerts over the following 14 hours, every 30 minutes
    0    positions at risk
    0    stale decisions -- every run persisted normally throughout

`last_success` never advanced, so `check_scheduler_health` reported "The
last run started and never completed. Everything on the dashboard is from
an earlier run." That sentence was false in every particular, and it is the
kind of false that sends somebody to look at the wrong thing during a
session.

THE SECOND HALF IS WORSE AND WAS NEARLY MISSED. The old loop had no
per-directory handling, so the first undeletable file abandoned the whole
sweep. Retention did not partially work; it stopped. Four days of runs
accumulated, and 143MB was reclaimed the moment it could run again. A
housekeeping job that silently stops is the failure its own docstring warns
about -- "a VM fills its disk after a few months and the failure surfaces
as something unrelated".

AND THE CONTRACT ALREADY EXISTED, ONE MODULE OVER.
`persist.retention.prune_runs`, which the standalone `prune-runs` job calls,
documents itself as "Never fails the caller" and returns its error in the
result. So this is one responsibility implemented twice with different
contracts, and the weaker of the two sat on the critical path. That
duplication is recorded rather than resolved here: collapsing them changes
what retention keeps, and the two disagree about policy (`RunConfig.
keep_runs` against `retention.DEFAULT_KEEP_RUNS`/`DEFAULT_KEEP_FULL`).

What these tests fix in place:

  * pruning never raises at the caller, whatever the filesystem says
  * one refused directory does not stop the others being cleaned
  * directories inside the keep window are never touched
  * the refusals are REPORTED rather than swallowed -- absence is named

Mutations checked:

  M1740 the OSError re-raised instead of collected --
        `test_an_undeletable_directory_does_not_raise` fails
  M1741 `break` instead of `continue`, the old abandon-the-sweep shape --
        `test_one_bad_directory_does_not_stop_the_others` fails
  M1742 the keep window ignored so everything is pruned --
        `test_the_keep_window_is_respected` fails
  M1743 the refusals dropped on the floor (returning `[]` always) --
        `test_refusals_are_reported_not_swallowed` fails
  M1744 the call site reverting to ignoring the result --
        `test_the_caller_says_something_about_a_refusal` fails
"""

from __future__ import annotations

import dataclasses
import logging
from pathlib import Path

import pytest

from cfdinvest.schedule import runner as runner_module
from cfdinvest.schedule.runner import RunConfig, _prune_old_runs

#: Enough directories to have some inside the keep window and some outside.
RUNS = [
    "20260910T100000Z", "20260911T100000Z", "20260912T100000Z",
    "20260913T100000Z", "20260914T100000Z", "20260915T100000Z",
]


class _Repository:
    """The two attributes `_account_output_root` reaches for."""

    def __init__(self, root: Path) -> None:
        self.config = None
        self._root = root


@pytest.fixture
def lake(tmp_path, monkeypatch):
    """A runs directory with six run folders, each holding a file.

    Patched at `_account_output_root` rather than at `account_root`: the
    test is about the pruning loop, and resolving the account tree is a
    different question with its own tests.
    """
    root = tmp_path / "runs"
    for name in RUNS:
        directory = root / name
        directory.mkdir(parents=True)
        (directory / "trades.parquet").write_bytes(b"x")
        (directory / "summary.json").write_text("{}")

    monkeypatch.setattr(
        runner_module, "_account_output_root", lambda repository, config: root
    )
    return root


def config_keeping(keep: int | None) -> RunConfig:
    return dataclasses.replace(RunConfig(), keep_runs=keep)


def refuse(monkeypatch, blocked: str) -> None:
    """Make one directory's files undeletable, as root ownership did.

    Monkeypatched rather than chmod'ed so the test means the same thing on
    Windows, where a read-only bit does not reproduce a POSIX permission
    denial and the suite would pass for the wrong reason.
    """
    real = Path.unlink

    def guarded(self, *args, **kwargs):
        if blocked in str(self):
            raise PermissionError(13, "Permission denied", str(self))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", guarded)


def test_an_undeletable_directory_does_not_raise(lake, monkeypatch) -> None:
    """M1740: the bug itself.

    The whole incident is this call raising into `run_pipeline`. Nothing
    about a file nobody can delete says the run went wrong.
    """
    refuse(monkeypatch, "20260910T100000Z")

    refused = _prune_old_runs(_Repository(lake), config_keeping(3))

    assert [path.name for path, _ in refused] == ["20260910T100000Z"], (
        "the refused directory was not reported back to the caller"
    )


def test_one_bad_directory_does_not_stop_the_others(lake, monkeypatch) -> None:
    """M1741: the abandoned sweep, which is why the disk kept growing.

    Blocking the NEWEST of the three prunable directories is deliberate.
    The sweep walks oldest-last, so a `break` here still removes nothing
    else -- while blocking the oldest would let a broken implementation
    look correct by having already done the work.
    """
    refuse(monkeypatch, "20260912T100000Z")

    _prune_old_runs(_Repository(lake), config_keeping(3))

    survivors = sorted(p.name for p in lake.iterdir() if p.is_dir())
    assert "20260912T100000Z" in survivors, "the fixture did not block anything"
    assert "20260910T100000Z" not in survivors, (
        "one undeletable directory stopped the whole sweep, so retention "
        "silently stops and the disk goes on growing"
    )
    assert "20260911T100000Z" not in survivors


def test_the_keep_window_is_respected(lake) -> None:
    """M1742: pruning what it was told to keep.

    The guard that matters most -- the newest run is the one the dealer
    reads, and a retention bug that reached it would empty the book's view
    rather than merely fill a disk.
    """
    _prune_old_runs(_Repository(lake), config_keeping(3))

    survivors = sorted(p.name for p in lake.iterdir() if p.is_dir())
    assert survivors == RUNS[-3:], (
        f"expected the newest three kept, got {survivors}"
    )


def test_refusals_are_reported_not_swallowed(lake, monkeypatch, caplog) -> None:
    """M1743: a silent catch, which is the other way to get this wrong.

    Swallowing the error would stop the mail and leave retention quietly
    dead -- trading a loud wrong alarm for a silent real one. The refusal
    has to come back carrying WHY, so the caller can name it.
    """
    refuse(monkeypatch, "20260910T100000Z")

    with caplog.at_level(logging.WARNING):
        refused = _prune_old_runs(_Repository(lake), config_keeping(3))

    assert refused, "the refusal was swallowed"
    path, error = refused[0]
    assert isinstance(error, OSError)
    assert "Permission denied" in str(error), (
        "the reason was lost, so the caller can only say that something "
        "went wrong and not what"
    )
    assert path.name == "20260910T100000Z"


def test_nothing_is_pruned_when_retention_is_off(lake) -> None:
    """``keep_runs = None`` keeps everything, and must not be read as zero.

    `x or fallback` treating a legitimate value as missing is a trap this
    project has already paid for once, and here the cost would be deleting
    every run directory on the machine.
    """
    refused = _prune_old_runs(_Repository(lake), config_keeping(None))

    assert refused == []
    assert sorted(p.name for p in lake.iterdir() if p.is_dir()) == RUNS


def test_the_caller_says_something_about_a_refusal() -> None:
    """M1744: the result ignored again at the call site.

    A return value nothing reads is how this becomes a silent failure a
    second time. The check is deliberately about the call site rather than
    the helper, because the helper can be perfect and still be called as
    `_prune_old_runs(...)` on a line by itself.
    """
    source = Path(runner_module.__file__).read_text(encoding="utf-8")

    assert "refused = _prune_old_runs(repository, settings)" in source, (
        "run_pipeline calls _prune_old_runs without keeping its result, so "
        "a directory nobody can delete is invisible again"
    )
    assert "if refused:" in source, (
        "the result is kept and never acted on, which is the same silence "
        "with an extra variable"
    )
