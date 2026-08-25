"""Reading the journal back, and rendering it.

Filtering and formatting only. Nothing here computes a statistic — that is the
insights layer's job, and a journal that quietly started scoring itself would
be the same layer twice with two answers.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Sequence

import pandas as pd

logger = logging.getLogger(__name__)

__all__ = ["filter_entries", "render_entry", "render_journal", "group_by_day"]


def _utc(value: Any):
    try:
        moment = pd.Timestamp(value)
    except Exception:  # noqa: BLE001
        return None
    if moment is pd.NaT:
        return None
    return moment.tz_localize("UTC") if moment.tz is None else moment.tz_convert("UTC")


def filter_entries(
    entries: Iterable[Any],
    *,
    instrument: str | None = None,
    kind: str | None = None,
    since=None,
    until=None,
) -> list[Any]:
    """Narrow the journal. Pure."""
    found = list(entries or [])

    if instrument is not None:
        wanted = str(instrument).upper()
        found = [
            e for e in found if str(getattr(e, "instrument", "") or "").upper() == wanted
        ]
    if kind is not None:
        found = [e for e in found if getattr(e, "kind", None) == kind]

    start, end = _utc(since), _utc(until)
    if start is not None:
        found = [e for e in found if (_utc(e.timestamp) or start) >= start]
    if end is not None:
        found = [e for e in found if (_utc(e.timestamp) or end) <= end]

    return sorted(found, key=lambda e: _utc(e.timestamp) or pd.Timestamp.min.tz_localize("UTC"))


def render_entry(entry, *, width: int = 72) -> list[str]:
    """One entry as lines, for a terminal or an email.

    The three notes stay on separate lines. Running them together would mix
    evidence about you with evidence about the market, and those lead to
    different corrections.
    """
    import textwrap

    moment = _utc(entry.timestamp)
    header = f"{moment:%Y-%m-%d %H:%M} UTC" if moment is not None else "undated"
    if entry.verdict:
        header += f"  [{entry.verdict}]"

    lines = [header, ""]

    def wrap(text: str, indent: str = "  ") -> None:
        if not text:
            return
        lines.extend(
            textwrap.wrap(
                text, width=width, initial_indent=indent, subsequent_indent=indent
            )
        )

    wrap(entry.narrative)
    for label, text in (
        ("Execution", entry.execution_note),
        ("Market", entry.regime_note),
        ("You", entry.behavioural_note),
    ):
        if text:
            lines.append("")
            wrap(f"{label}: {text}")

    if entry.changed:
        lines.append("")
        lines.append("  Since the last entry:")
        for change in entry.changed:
            wrap(f"- {change}", indent="    ")

    if entry.notes:
        lines.append("")
        wrap(f"Your note: {entry.notes}")

    return lines


def render_journal(entries: Sequence[Any], *, width: int = 72) -> str:
    """Several entries, newest first, separated."""
    if not entries:
        return "The journal is empty."

    blocks = []
    for entry in sorted(
        entries, key=lambda e: _utc(e.timestamp) or pd.Timestamp.min.tz_localize("UTC"),
        reverse=True,
    ):
        blocks.append("\n".join(render_entry(entry, width=width)))
    return ("\n" + "-" * width + "\n").join(blocks)


def group_by_day(entries: Iterable[Any]) -> dict[Any, list[Any]]:
    """Entries by calendar day, for a dashboard that shows a diary."""
    grouped: dict[Any, list[Any]] = {}
    for entry in entries or []:
        moment = _utc(entry.timestamp)
        day = moment.date() if moment is not None else None
        grouped.setdefault(day, []).append(entry)
    return grouped
