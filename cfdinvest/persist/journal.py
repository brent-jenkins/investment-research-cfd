"""The journal on disk: append-only, one line per entry.

JSON Lines rather than a file per entry, and for the same reason as the
assessment log: entries are small, frequent, and read in bulk. A directory of
four thousand tiny files is a directory nobody wants.

**Nothing is ever rewritten.** An entry says what you believed at the time,
and an entry that could be edited later is a diary that agrees with whatever
you now think. Corrections are appended; the original stays.

Writes append. Reads never raise.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from cfdinvest.persist.storage import account_root

logger = logging.getLogger(__name__)

__all__ = [
    "JOURNAL_DIR",
    "SCHEMA_VERSION",
    "journal_path",
    "append_entry",
    "append_entries",
    "load_entries",
    "load_journal_frame",
    "last_entry",
]

JOURNAL_DIR = "journal"
SCHEMA_VERSION = 1


def journal_path(config=None, account: str | None = None) -> Path:
    """The log file."""
    return account_root(config, account) / JOURNAL_DIR / "journal.jsonl"


def append_entry(entry, config=None, *, account: str | None = None) -> str | None:
    """Append one entry. Returns the path, or ``None`` if it could not.

    Never raises. A journal that failed to record is a lost note; a journal
    that took the caller down with it would be worse, because the caller is
    usually finishing something more important.
    """
    return append_entries([entry], config, account=account)


def append_entries(
    entries: Iterable[Any], config=None, *, account: str | None = None
) -> str | None:
    """Append several entries in one open. Returns the path, or ``None``."""
    rows = [
        {"schema_version": SCHEMA_VERSION, **entry.to_document()}
        for entry in entries
        if entry is not None
    ]
    if not rows:
        return None

    path = journal_path(config, account)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")
        return str(path)
    except Exception as error:  # noqa: BLE001
        logger.warning("Could not write the journal to %s: %s", path, error)
        return None


def load_entries(config=None, account: str | None = None, *, limit: int | None = None):
    """Every entry, oldest first. Never raises.

    A malformed line is skipped with a warning. An append log's last line is
    what a crash takes, and losing the other four hundred to it would be a
    poor trade.
    """
    from cfdinvest.journal.entry import JournalEntry

    path = journal_path(config, account)
    found = []
    try:
        if not path.is_file():
            return []
        with path.open("r", encoding="utf-8") as handle:
            for number, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    found.append(JournalEntry.from_document(json.loads(line)))
                except (json.JSONDecodeError, TypeError, ValueError) as error:
                    logger.warning("Skipping journal line %d: %s", number, error)
    except OSError as error:
        logger.warning("Journal unreadable at %s: %s", path, error)
        return []

    return found[-limit:] if limit else found


def last_entry(config=None, account: str | None = None):
    """The most recent entry, for "what changed since last time"."""
    found = load_entries(config, account)
    return found[-1] if found else None


def load_journal_frame(
    config=None, account: str | None = None, *, limit: int | None = None
) -> pd.DataFrame:
    """The journal as a table, for the dashboard."""
    entries = load_entries(config, account, limit=limit)
    if not entries:
        return pd.DataFrame(
            columns=[
                "timestamp", "kind", "instrument", "direction", "quantity",
                "price", "verdict", "narrative",
            ]
        )

    frame = pd.DataFrame(
        [
            {
                "timestamp": entry.timestamp,
                "kind": entry.kind,
                "instrument": entry.instrument,
                "direction": entry.direction,
                "quantity": entry.quantity,
                "price": entry.price,
                "verdict": entry.verdict,
                "narrative": entry.narrative,
                "behavioural_note": entry.behavioural_note,
                "execution_note": entry.execution_note,
                "regime_note": entry.regime_note,
                "source": entry.source,
            }
            for entry in entries
        ]
    )
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
    return frame.sort_values("timestamp").reset_index(drop=True)
