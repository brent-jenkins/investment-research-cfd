"""What the scheduler last did, recorded rather than inferred.

Until now "last run" was derived from the newest directory under ``runs/``,
which answers a different question: it says when a run last *completed*, and
says nothing at all about a run that started and died, or about a scheduler
process that is no longer alive. The dashboard reported that honestly and
could do no better.

This file closes it. The scheduler writes here when a run starts, succeeds or
fails, so the difference between "nothing has run" and "a run failed" is
recorded rather than guessed at.

**The file is state, not history.** It holds the last of each kind of event;
the run directories remain the durable record. That keeps it small enough to
write on every run without the write itself becoming a cost, and small enough
that a corrupt one loses almost nothing.

Reads never raise. Writes are atomic.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pandas as pd

from cfdinvest.persist.storage import (
    atomic_write_text,
    ensure_scheduler_state,
    get_scheduler_state_path,
)

logger = logging.getLogger(__name__)

__all__ = [
    "SCHEMA_VERSION",
    "EMPTY_STATE",
    "load_scheduler_state",
    "write_scheduler_state",
    "update_last_run",
    "update_last_success",
    "update_last_error",
    "clear_last_error",
]

SCHEMA_VERSION = 1

#: The shape a caller can always rely on, even before anything has run.
EMPTY_STATE: dict[str, Any] = {
    "schema_version": SCHEMA_VERSION,
    "last_run": None,
    "last_run_id": None,
    "last_success": None,
    "last_success_run_id": None,
    "last_error": None,
    "last_error_at": None,
    "last_duration": None,
    "runs": 0,
    "failures": 0,
}


def _timestamp(value: Any) -> str | None:
    """Any timestamp-like value as an ISO-8601 UTC string."""
    if value is None:
        return None
    try:
        stamp = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if stamp.tz is None:
        stamp = stamp.tz_localize("UTC")
    return stamp.tz_convert("UTC").isoformat()


def load_scheduler_state(config=None, account: str | None = None) -> dict[str, Any]:
    """The recorded state, or a fully-formed empty one.

    Never raises and never returns a partial dictionary: a caller reading
    ``state["last_success"]`` should not have to guard the key, because a
    missing key and a null value would then mean the same thing to them and
    different things to us.
    """
    path = get_scheduler_state_path(config, account)
    try:
        if not path.is_file():
            return dict(EMPTY_STATE)
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        logger.warning("Scheduler state unreadable at %s: %s", path, error)
        return dict(EMPTY_STATE)

    if not isinstance(document, dict):
        logger.warning("Scheduler state at %s is not an object", path)
        return dict(EMPTY_STATE)
    if document.get("schema_version") != SCHEMA_VERSION:
        logger.info(
            "Scheduler state at %s was written by schema %s, reading as %s",
            path, document.get("schema_version"), SCHEMA_VERSION,
        )
    return {**EMPTY_STATE, **document}


def write_scheduler_state(
    state: dict[str, Any], config=None, account: str | None = None
) -> str | None:
    """Replace the state file atomically. Returns its path, or ``None``.

    Never raises: a scheduler that crashed because it could not record that
    it had run would be worse than one that quietly did not record it.
    """
    path = ensure_scheduler_state(config, account)
    payload = {**EMPTY_STATE, **(state or {}), "schema_version": SCHEMA_VERSION}
    try:
        atomic_write_text(json.dumps(payload, indent=2), path)
        return str(path)
    except Exception as error:  # noqa: BLE001
        logger.warning("Could not write scheduler state to %s: %s", path, error)
        return None


def _update(changes: dict[str, Any], config, account) -> dict[str, Any]:
    """Read, merge, write — the only way this file is modified."""
    state = load_scheduler_state(config, account)
    state.update(changes)
    write_scheduler_state(state, config, account)
    return state


def update_last_run(
    timestamp=None, *, run_id: str | None = None, config=None,
    account: str | None = None,
) -> dict[str, Any]:
    """Record that a run started.

    Written at the *start*, so a run that dies mid-pipeline still leaves a
    trace. A ``last_run`` newer than ``last_success`` is exactly the signal
    that something began and did not finish.
    """
    return _update(
        {
            "last_run": _timestamp(timestamp or pd.Timestamp.now(tz="UTC")),
            "last_run_id": run_id,
            "runs": load_scheduler_state(config, account).get("runs", 0) + 1,
        },
        config,
        account,
    )


def update_last_success(
    timestamp=None, *, run_id: str | None = None, duration: float | None = None,
    config=None, account: str | None = None,
) -> dict[str, Any]:
    """Record that a run finished cleanly, and clear the last error.

    Clearing matters: a stale error from a fortnight ago sitting beside a
    successful run this morning would have the dashboard shouting about a
    problem that has already been fixed.
    """
    return _update(
        {
            "last_success": _timestamp(timestamp or pd.Timestamp.now(tz="UTC")),
            "last_success_run_id": run_id,
            "last_duration": None if duration is None else float(duration),
            "last_error": None,
            "last_error_at": None,
        },
        config,
        account,
    )


def update_last_error(
    error: Any, timestamp=None, *, run_id: str | None = None, config=None,
    account: str | None = None,
) -> dict[str, Any]:
    """Record that a run failed, and why."""
    state = load_scheduler_state(config, account)
    return _update(
        {
            "last_error": str(error)[:2000],
            "last_error_at": _timestamp(timestamp or pd.Timestamp.now(tz="UTC")),
            "last_run_id": run_id or state.get("last_run_id"),
            "failures": state.get("failures", 0) + 1,
        },
        config,
        account,
    )


def clear_last_error(config=None, account: str | None = None) -> dict[str, Any]:
    """Forget the last error without recording a success.

    For an operator who has fixed something by hand and does not want to wait
    for the next scheduled run to silence the banner.
    """
    return _update({"last_error": None, "last_error_at": None}, config, account)
