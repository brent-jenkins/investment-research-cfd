"""Where run artefacts live, and how to find them.

**This extends the existing run store rather than creating a second one.**
The scheduler already writes ``<data root>/runs/<run id>/`` containing
``equity.parquet``, ``trades.parquet``, ``positions.parquet``,
``signals.parquet`` and ``summary.json``, and the dashboard already reads
them. Persisting the missing artefacts *into that same directory* keeps one
run store; adding a parallel tree under a different root would give every
consumer two places to look and two answers to "what was the last run".

The specification asked for ``~/.local/share/cfdinvest/``. That is honoured
as the **default** when nothing else is configured, but ``CFD_DATA_ROOT``
still wins, because it is what the ingestion, preprocessing, execution and
dashboard layers already resolve against — and because the development
machine here is Windows, where ``~/.local/share`` is not where anything
belongs.

Resolution order, highest first:

1. an explicit ``AppConfig``
2. ``CFD_DATA_ROOT``
3. ``$XDG_DATA_HOME/cfdinvest`` or ``~/.local/share/cfdinvest`` (POSIX),
   ``%LOCALAPPDATA%\\cfdinvest`` (Windows)

**Account separation is anticipated, not yet switched on.** ``account=None``
resolves to the layout that exists today; passing ``"virtual"`` or ``"live"``
inserts one path segment. Building the resolver that way now means the later
layer is a default change rather than a migration.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

__all__ = [
    "ACCOUNTS",
    "RUNS_DIR",
    "SCHEDULER_DIR",
    "RUN_ID_FORMAT",
    "data_root",
    "account_root",
    "runs_root",
    "generate_run_id",
    "get_run_directory",
    "ensure_run_directory",
    "get_scheduler_state_path",
    "ensure_scheduler_state",
    "list_run_directories",
    "latest_run_directory",
    "atomic_write_bytes",
    "atomic_write_text",
]

#: The two account trees the separation layer will use.
#: ``simulation`` is here so simulated artefacts get the same isolation the
#: virtual and live trees already have. A simulated equity curve filed where a
#: real one goes is the worst outcome this project has, and the cheapest
#: defence is that they cannot share a directory.
ACCOUNTS: tuple[str, ...] = ("virtual", "live", "simulation")

RUNS_DIR = "runs"
SCHEDULER_DIR = "scheduler"

#: Matches what the scheduler already generates, so old and new runs sort
#: together. The specification wrote ``YYYYMMDD-HHMMSS``; the existing format
#: carries a ``Z`` and is unambiguous about the zone, which matters more.
RUN_ID_FORMAT = "%Y%m%dT%H%M%SZ"


def _platform_default() -> Path:
    """Where an unconfigured install should keep its data."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
        return Path(base) / "cfdinvest"
    xdg = os.environ.get("XDG_DATA_HOME")
    return Path(xdg) / "cfdinvest" if xdg else Path.home() / ".local" / "share" / "cfdinvest"


def data_root(config=None) -> Path:
    """The lake root, by the resolution order in this module's docstring."""
    if config is not None:
        return Path(config.paths.data_root)
    env = os.environ.get("CFD_DATA_ROOT")
    if env:
        return Path(env)
    return _platform_default()


def account_root(config=None, account: str | None = None) -> Path:
    """The root for one account, or the shared root when there is no account.

    ``account=None`` means "resolve from the environment", which yields an
    account only when ``CFD_ACCOUNT_MODE`` is set. Separation is opt-in for a
    reason: turning it on by default would strand every artefact written
    before it existed one directory above where the readers now look, and a
    lake that appears to have lost its history is a bad way to find out about
    a new feature. See ``deployment/README.md`` for the one-line migration.

    **The lake itself is not under here.** Raw prices are the same prices
    whichever account you trade them in, and ingesting the S&P twice to keep
    two identical copies would burn the FRED rate limit for nothing. What
    separates is everything downstream: runs, confirmations, monitoring, risk.
    """
    from cfdinvest.env import resolve_account

    root = data_root(config)
    resolved = resolve_account(account)
    if resolved is None:
        return root
    if resolved not in ACCOUNTS:
        raise ValueError(f"unknown account {resolved!r}; expected one of {ACCOUNTS}")
    return root / resolved


def runs_root(config=None, account: str | None = None) -> Path:
    """The directory holding every run."""
    return account_root(config, account) / RUNS_DIR


def generate_run_id(now: pd.Timestamp | None = None) -> str:
    """A UTC run identifier. Injectable so a test can pin it."""
    moment = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="UTC")
    if moment.tz is None:
        moment = moment.tz_localize("UTC")
    return moment.tz_convert("UTC").strftime(RUN_ID_FORMAT)


def get_run_directory(
    timestamp: str | pd.Timestamp, config=None, account: str | None = None
) -> Path:
    """Where one run's artefacts belong. Does not create anything.

    Accepts a run id or a timestamp. A reader asking where something is
    should not bring it into existence, or an empty directory appears for
    every run anybody ever looked at.
    """
    run_id = (
        timestamp
        if isinstance(timestamp, str)
        else generate_run_id(timestamp)
    )
    return runs_root(config, account) / run_id


def ensure_run_directory(
    timestamp: str | pd.Timestamp, config=None, account: str | None = None
) -> Path:
    """The same directory, created if absent.

    **Never clears an existing one.** Persistence is append-only: a rerun
    under the same identifier adds to what is there, and a past run is never
    rewritten. Deleting on collision would make a retry destroy the record of
    the attempt it was retrying.
    """
    directory = get_run_directory(timestamp, config, account)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def get_scheduler_state_path(config=None, account: str | None = None) -> Path:
    """Where the scheduler records what it last did."""
    return account_root(config, account) / SCHEDULER_DIR / "state.json"


def ensure_scheduler_state(config=None, account: str | None = None) -> Path:
    """The scheduler state path, with its directory created.

    The file itself is not created: an absent state file means "nothing has
    run yet", which is true and worth being able to say. Writing an empty one
    would turn that into "a run happened and recorded nothing".
    """
    path = get_scheduler_state_path(config, account)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def list_run_directories(config=None, account: str | None = None) -> list[Path]:
    """Every run directory, newest first.

    Run ids sort lexicographically in time order, which is what makes
    "newest" a sort rather than a stat of every directory.
    """
    root = runs_root(config, account)
    try:
        if not root.is_dir():
            return []
        return sorted(
            (p for p in root.iterdir() if p.is_dir()),
            key=lambda p: p.name,
            reverse=True,
        )
    except OSError as error:
        logger.warning("Run directory unreadable at %s: %s", root, error)
        return []


def latest_run_directory(config=None, account: str | None = None) -> Path | None:
    """The most recent run, or ``None`` when nothing has run."""
    directories = list_run_directories(config, account)
    return directories[0] if directories else None


# --------------------------------------------------------------------------- #
# Atomic writes
# --------------------------------------------------------------------------- #
def atomic_write_bytes(payload: bytes, path: Path) -> Path:
    """Write bytes so a reader never sees a partial file.

    Written to a temporary name in the same directory and renamed into place.
    ``os.replace`` is atomic within a filesystem on every platform this runs
    on; the same-directory part matters, because a rename across filesystems
    is a copy and loses the guarantee.

    An unattended machine writing at midnight will eventually be interrupted
    mid-write, and a half-written parquet that reads as "corrupt" is much
    worse than one that reads as "absent".
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, path)
    return path


def atomic_write_text(payload: str, path: Path, encoding: str = "utf-8") -> Path:
    """The same guarantee, for text."""
    return atomic_write_bytes(payload.encode(encoding), path)
