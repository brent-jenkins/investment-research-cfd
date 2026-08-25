"""Did the last run finish, or did it die?

The scheduler writes ``last_run`` **before** the pipeline starts and
``last_success`` only when it completes. That ordering is the whole point:
a start with no matching success is a run that began and never came back,
which no amount of looking at run directories could tell you — a directory
exists whether or not the run that made it survived.

Three states, and the middle one is the reason this exists:

``OK``
    The last start has a matching success.
``FAILED``
    A start with no success after it, or a recorded error. The pipeline died
    part-way, and whatever the dashboard is showing is from an earlier run.
``UNKNOWN``
    Nothing recorded yet, or a state file that could not be read. Not
    reassuring, and deliberately not reported as ``OK``.

Pure: it reads a mapping and returns a mapping.
"""

from __future__ import annotations

from typing import Any, Mapping

import pandas as pd

__all__ = ["STATUSES", "compute_scheduler_status"]

STATUSES: tuple[str, ...] = ("OK", "FAILED", "UNKNOWN")


def _stamp(value: Any) -> pd.Timestamp | None:
    """Parse a recorded timestamp, tolerating anything."""
    if value in (None, ""):
        return None
    try:
        parsed = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if parsed.tz is None:
        parsed = parsed.tz_localize("UTC")
    return parsed.tz_convert("UTC")


def compute_scheduler_status(
    scheduler_state: Mapping[str, Any] | None,
    *,
    stale_after_hours: float = 48.0,
    now: pd.Timestamp | None = None,
) -> dict[str, Any]:
    """Classify what the scheduler last did.

    Parameters
    ----------
    stale_after_hours:
        How long a *successful* run may be the most recent one before it is
        worth saying so. Staleness is reported alongside the status rather
        than folded into it: a run that succeeded three days ago succeeded,
        and conflating that with failure would hide which of the two happened.

    Returns
    -------
    dict
        ``run_status``, the two timestamps, a ``message`` written to be read
        aloud, and the supporting figures.
    """
    state = dict(scheduler_state or {})
    started = _stamp(state.get("last_run"))
    succeeded = _stamp(state.get("last_success"))
    errored_at = _stamp(state.get("last_error_at"))
    error = state.get("last_error")
    moment = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="UTC")
    if moment.tz is None:
        moment = moment.tz_localize("UTC")

    if started is None and succeeded is None:
        return {
            "run_status": "UNKNOWN",
            "message": "No runs recorded yet.",
            "last_run_started_at": None,
            "last_run_completed_at": None,
            "last_error": error,
            "last_error_at": errored_at,
            "age_hours": None,
            "stale": False,
            "incomplete": False,
            "runs": int(state.get("runs", 0) or 0),
            "failures": int(state.get("failures", 0) or 0),
        }

    # A start with no success after it is the case this file exists for.
    incomplete = started is not None and (succeeded is None or succeeded < started)
    status = "FAILED" if (incomplete or error) else "OK"

    reference = succeeded or started
    age = (
        (moment - reference).total_seconds() / 3600.0
        if reference is not None
        else None
    )
    stale = bool(status == "OK" and age is not None and age > stale_after_hours)

    if incomplete:
        message = (
            "Last run started but did not complete. What is shown below is "
            "from an earlier run."
        )
    elif error:
        message = f"Last run recorded an error: {error}"
    elif stale:
        message = (
            f"Last run completed successfully, but that was {age:.0f} hours "
            "ago. Check that the schedule is still firing."
        )
    else:
        message = "Last run completed successfully."

    return {
        "run_status": status,
        "message": message,
        "last_run_started_at": started,
        "last_run_completed_at": succeeded,
        "last_error": error,
        "last_error_at": errored_at,
        "age_hours": age,
        "stale": stale,
        "incomplete": incomplete,
        "runs": int(state.get("runs", 0) or 0),
        "failures": int(state.get("failures", 0) or 0),
        "last_duration": state.get("last_duration"),
        "run_id": state.get("last_run_id"),
    }
