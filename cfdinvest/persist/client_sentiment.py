"""The forward record of Capital's client positioning.

Append-only, like every observation store here: a reading describes a
moment that does not come back, and the broker keeps no history of it, so a
row lost or rewritten is gone for good. Never pruned by any scheduled job.

**An experiment, not a permanent series** (owner, 2026-09-28). Futility
look 2026-12-28, decision 2027-03-29, against the test pre-registered in
CLAUDE.md; on a null the job, this module and the data are removed.

Each row is one market at one moment::

    {"at": ISO-8601 UTC, "ticker": ..., "long_pct": ..., "short_pct": ...}

``at`` is the time THIS store took the reading. A market the broker did not
answer for is written with ``long_pct`` and ``short_pct`` as ``null``, so a
gap in the record says "asked, not answered" rather than looking identical
to "not asked" -- absence is named, never inferred.

Monthly files, so no single file grows without bound: about 110 markets
every thirty minutes is roughly 200MB a year across twelve files.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)

__all__ = ["SENTIMENT_DIR", "load_client_sentiment", "record_client_sentiment"]

#: Where the record lives, under the account root.
SENTIMENT_DIR = "client_sentiment"


def _dir(config=None, account: str | None = None) -> Path:
    from cfdinvest.persist.storage import account_root

    return Path(account_root(config, account)) / SENTIMENT_DIR


def _now_utc() -> str:
    """The recording time, as ISO-8601 UTC. A function so a test can fix it."""
    import datetime as dt

    return dt.datetime.now(dt.timezone.utc).isoformat()


def record_client_sentiment(
    readings: Mapping[str, Mapping[str, Any] | None],
    config=None,
    account: str | None = None,
) -> int:
    """Append one row per market. Returns how many rows were written.

    Every market asked about gets a row, answered or not. Never raises: a
    lost reading is not an outage, and the next one is thirty minutes away.
    """
    if not readings:
        return 0
    at = _now_utc()
    try:
        folder = _dir(config, account)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{at[:7]}.jsonl"
        written = 0
        with path.open("a", encoding="utf-8") as handle:
            for ticker, reading in readings.items():
                reading = reading or {}
                row = {
                    "at": at,
                    "ticker": str(ticker),
                    "long_pct": reading.get("long"),
                    "short_pct": reading.get("short"),
                }
                handle.write(json.dumps(row, sort_keys=True) + "\n")
                written += 1
        return written
    except Exception as error:  # noqa: BLE001 - collection is best-effort
        logger.warning("Could not record client sentiment: %s", error)
        return 0


def load_client_sentiment(config=None, account: str | None = None) -> list[dict]:
    """Every recorded row, oldest file first. Never raises."""
    try:
        folder = _dir(config, account)
        if not folder.is_dir():
            return []
        rows = []
        for path in sorted(folder.glob("*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        logger.warning("Skipping a malformed row in %s", path)
        return rows
    except Exception as error:  # noqa: BLE001
        logger.warning("Client sentiment unreadable: %s", error)
        return []
