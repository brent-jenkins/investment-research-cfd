"""What a language model read in each stored headline.

**An experiment with a decision date** (owner, 2026-09-30): futility look
2026-12-28, decision 2027-03-29, against the test pre-registered in
CLAUDE.md. On a null the job, this module and the labels are removed.

Kept beside the headline archive rather than inside it. The stories are the
observation and stay untouched; a label is an interpretation made later by
a named model under a named prompt, and a new prompt must be able to label
the same story again without editing what was already recorded. So every
row carries the model and the prompt version, and a reader picks the labels
it wants rather than having the newest silently replace the old.

Append-only, and never pruned by a scheduled job.

Each row::

    {"key": ..., "ticker": ..., "first_seen": ..., "title": ...,
     "model": ..., "prompt_version": ..., "labelled_at": ...,
     "about": ..., "event": ..., "impact": ...}

``key`` identifies the STORY -- ticker, title and publication stamp, the
same triple the archive de-duplicates on -- so a label joins back to the
row it describes.
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Iterable, Mapping

logger = logging.getLogger(__name__)

__all__ = ["LABEL_FILE", "story_key", "record_labels", "load_labels", "labelled_keys"]

#: Where the labels live, under the headline archive's directory.
LABEL_FILE = "labels.jsonl"


def _path(config=None, account: str | None = None) -> Path:
    from cfdinvest.persist.headlines import HEADLINE_DIR
    from cfdinvest.persist.storage import account_root

    return Path(account_root(config, account)) / HEADLINE_DIR / LABEL_FILE


def story_key(story: Mapping[str, Any]) -> str:
    """The archive's own identity for a story, hashed to a short string."""
    triple = "\x1f".join(
        str(story.get(field) or "") for field in ("ticker", "title", "published")
    )
    return hashlib.sha1(triple.encode("utf-8")).hexdigest()[:20]


def record_labels(
    rows: Iterable[Mapping[str, Any]],
    config=None,
    account: str | None = None,
) -> int:
    """Append labels. Returns how many were written. Never raises."""
    written = 0
    try:
        path = _path(config, account)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for row in rows or ():
                if row:
                    handle.write(json.dumps(dict(row), sort_keys=True) + "\n")
                    written += 1
    except Exception as error:  # noqa: BLE001 - a lost label is re-made next hour
        logger.warning("Could not record headline labels: %s", error)
    return written


def load_labels(config=None, account: str | None = None) -> list[dict]:
    """Every label ever written, oldest first. Never raises."""
    rows: list[dict] = []
    try:
        path = _path(config, account)
        if not path.is_file():
            return rows
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    logger.warning("Skipping a malformed label in %s", path)
    except OSError as error:
        logger.warning("Headline labels unreadable: %s", error)
    return rows


def labelled_keys(
    prompt_version: str, config=None, account: str | None = None
) -> set[str]:
    """Stories already labelled under ``prompt_version``."""
    return {
        str(row.get("key"))
        for row in load_labels(config, account)
        if str(row.get("prompt_version")) == str(prompt_version)
    }
