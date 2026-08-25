"""A log of what the live helper said, so the dashboard can look back.

The helper answers one trade at a time and forgets. Recording its verdicts
turns that into something the risk dashboard can summarise: how many of the
trades you considered last month were unsafe, and what kept making them so.

**Append-only, one line per assessment.** JSON Lines rather than one file per
verdict — these are small and frequent, and a directory with four thousand
tiny files is a directory nobody wants. Appending is also the only write shape
where a crash mid-write loses the current line and nothing else.

Recording is opt-in: the helper writes here only with ``--record``. Nothing is
written by simply asking a question.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from cfdinvest.persist.storage import account_root

logger = logging.getLogger(__name__)

__all__ = [
    "ASSESSMENTS_DIR",
    "SCHEMA_VERSION",
    "assessments_path",
    "record_assessment",
    "load_assessments",
]

ASSESSMENTS_DIR = "assessments"
SCHEMA_VERSION = 1


def assessments_path(config=None, account: str | None = None) -> Path:
    """The log file."""
    return account_root(config, account) / ASSESSMENTS_DIR / "assessments.jsonl"


def record_assessment(
    intent, assessment, sizing=None, config=None, *,
    account: str | None = None, now: pd.Timestamp | None = None,
) -> str | None:
    """Append one verdict. Returns the log path, or ``None`` if it could not.

    Never raises. The helper has already printed its answer by the time this
    runs, and failing to file a copy must not turn a successful check into an
    error.
    """
    moment = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="UTC")
    if moment.tz is None:
        moment = moment.tz_localize("UTC")

    row = {
        "schema_version": SCHEMA_VERSION,
        "timestamp": moment.isoformat(),
        "instrument": getattr(intent, "instrument", None),
        "direction": getattr(intent, "direction", None),
        "quantity": getattr(intent, "quantity", None),
        "entry_price": getattr(intent, "entry_price", None),
        "stop_loss": getattr(intent, "stop_loss", None),
        "take_profit": getattr(intent, "take_profit", None),
        "instrument_currency": getattr(intent, "instrument_currency", None),
        "status": getattr(assessment, "status", None),
        "risk_gbp": getattr(assessment, "risk_gbp", None),
        "exposure_gbp": getattr(assessment, "exposure_gbp", None),
        "leverage": getattr(assessment, "leverage", None),
        "margin_required_gbp": getattr(assessment, "margin_required_gbp", None),
        "margin_usage_pct_after_trade": getattr(
            assessment, "margin_usage_pct_after_trade", None
        ),
        "fx_exposure_pct": getattr(assessment, "fx_exposure_pct", None),
        "reasons": list(getattr(assessment, "reasons", []) or []),
        # The check names, by severity. Grouping on prose fails twice over:
        # the messages carry amounts, so the same failure never groups, and
        # the reasons list includes info-level findings, which are not why
        # anything was refused.
        "critical_checks": [
            f.check
            for f in getattr(assessment, "findings", ())
            if getattr(f, "severity", "") == "critical"
        ],
        "warning_checks": [
            f.check
            for f in getattr(assessment, "findings", ())
            if getattr(f, "severity", "") == "warning"
        ],
        "recommendations": list(getattr(assessment, "recommendations", []) or []),
        "unknowns": list(getattr(assessment, "unknowns", []) or []),
        "recommended_quantity": getattr(sizing, "recommended_quantity", None),
        "binding_constraint": getattr(sizing, "binding_constraint", None),
    }

    path = assessments_path(config, account)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Appending, not rewriting: a crash loses the line being written and
        # leaves every earlier verdict intact.
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_plain(row)) + "\n")
        return str(path)
    except Exception as error:  # noqa: BLE001
        logger.warning("Could not record the assessment to %s: %s", path, error)
        return None


def _plain(row: Mapping[str, Any]) -> dict[str, Any]:
    """Coerce to JSON-safe values, with NaN becoming null."""
    from cfdinvest.persist.writers import jsonable

    return jsonable(dict(row))


def load_assessments(
    config=None, account: str | None = None, *, limit: int | None = None
) -> pd.DataFrame:
    """Every recorded verdict, oldest first. Empty when none were recorded.

    A malformed line is skipped rather than failing the read: this is an
    append log, and the one line a crash truncated should not cost the other
    four thousand.
    """
    path = assessments_path(config, account)
    if not path.is_file():
        return pd.DataFrame()

    rows: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    logger.debug("Skipping a malformed assessment line")
    except OSError as error:
        logger.warning("Assessment log unreadable at %s: %s", path, error)
        return pd.DataFrame()

    if not rows:
        return pd.DataFrame()

    frame = pd.DataFrame(rows)
    if "timestamp" in frame.columns:
        frame["timestamp"] = pd.to_datetime(
            frame["timestamp"], utc=True, errors="coerce"
        )
        frame = frame.sort_values("timestamp").reset_index(drop=True)
    return frame.tail(limit) if limit else frame
