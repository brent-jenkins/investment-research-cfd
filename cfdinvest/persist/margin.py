"""Margin: what the broker requires, and how much of the account it uses.

Two questions, one store, and the first is the one that prompted it.

**Does the broker change its margin requirement on positions already open?**
Capital publishes a ``marginFactor`` per instrument and reserves the right to
vary it -- tiered by position size, raised in volatile conditions and around
events. That is ordinary risk management by a counterparty, and it is also
the mechanism by which a book can be forced to close at the worst moment, so
whether and when it moves is worth knowing rather than assuming. The app has
been fetching the figure since the account opened and discarding it.

It cannot be answered retrospectively. A margin factor is a *current* value
with no history served by the API, so a change that happened yesterday is
gone. That is why this exists before anything reads it: the series has to be
running before the question can be asked, which is the same reason the
intraday lake takes the union of configured and already-collected names.

**And how close is the account to a close-out?** ``available`` is what is left
after committed margin, so ``balance - available`` is the margin in use.
Recorded every pass, because it is the number that decides whether a
drawdown is survivable or terminal, and the backtest cannot price it -- there
is no margin model in the engine, so every drawdown figure this project
holds is an upper bound on what the account would actually live through.

Written thin. Rows are appended and never edited, they are observations of a
moment, and this layer takes plain mappings rather than broker objects so it
holds no import of anything that can reach a network.

**Size.** Factors are written only when they *change*, plus one heartbeat per
instrument per day so that "unchanged" and "unobserved" stay distinguishable
-- absence named rather than inferred. Ten instruments at a daily heartbeat is
a few kilobytes a year; writing every pass would be 40MB and would say the
same thing.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Iterable, Mapping

logger = logging.getLogger(__name__)

__all__ = [
    "MARGIN_DIR",
    "record_margin",
    "load_margin",
    "latest_factors",
    "factor_changes",
    "usage_summary",
    "changed_factors",
]

#: Where the readings live, under the account root.
MARGIN_DIR = "margin"


def _path(config=None, account: str | None = None) -> Path:
    from cfdinvest.persist.storage import account_root

    return Path(account_root(config, account)) / MARGIN_DIR / "observations.jsonl"


def record_margin(
    rows: Iterable[Mapping[str, Any]],
    config=None,
    account: str | None = None,
) -> int:
    """Append readings. Returns how many were written. Never raises."""
    written = 0
    try:
        path = _path(config, account)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for row in rows or ():
                if not row:
                    continue
                handle.write(json.dumps(dict(row), sort_keys=True) + "\n")
                written += 1
    except Exception as error:  # noqa: BLE001 - a lost reading is not an outage
        logger.warning("Could not record margin readings: %s", error)
        return written
    return written


def load_margin(config=None, account: str | None = None) -> list[dict]:
    """Every reading, oldest first. Never raises; skips a truncated line."""
    rows: list[dict] = []
    try:
        path = _path(config, account)
        if not path.is_file():
            return rows
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("Skipping a malformed margin reading in %s", path)
    except OSError as error:
        logger.warning("Margin readings unreadable at %s: %s", path, error)
    return rows


def latest_factors(config=None, account: str | None = None) -> dict[str, float]:
    """The most recent margin factor seen for each instrument.

    What the next pass compares against to decide whether anything moved.
    An instrument never seen is absent rather than defaulted, because a
    missing factor and a factor of zero would size very differently.
    """
    seen: dict[str, float] = {}
    for row in load_margin(config, account):
        if str(row.get("kind") or "") != "factor":
            continue
        ticker = str(row.get("ticker") or "")
        value = row.get("margin_factor")
        if not ticker or value is None:
            continue
        try:
            seen[ticker] = float(value)
        except (TypeError, ValueError):
            continue
    return seen


def changed_factors(
    observed: Mapping[str, Any],
    known: Mapping[str, float] | None = None,
    *,
    now=None,
    heartbeat_days: float = 1.0,
    last_seen: Mapping[str, str] | None = None,
) -> list[dict]:
    """Which observations are worth writing: the changes, plus a heartbeat.

    Returns rows ready for :func:`record_margin`. A value equal to the last
    one recorded is written only if the heartbeat is due, so the file stays
    small without losing the ability to say "this was observed and had not
    moved" -- which is the distinction between an unchanged factor and an
    unread one, and only one of those is evidence.
    """
    import pandas as pd

    known = dict(known or {})
    last_seen = dict(last_seen or {})
    try:
        stamp = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="UTC")
        if stamp.tz is None:
            stamp = stamp.tz_localize("UTC")
    except Exception:  # noqa: BLE001 - an unusable clock writes everything
        stamp = None

    rows: list[dict] = []
    for ticker, value in (observed or {}).items():
        if value is None:
            continue
        try:
            factor = float(value)
        except (TypeError, ValueError):
            continue
        previous = known.get(str(ticker))
        moved = previous is None or abs(factor - previous) > 1e-12
        due = True
        if not moved and stamp is not None:
            seen_at = last_seen.get(str(ticker))
            if seen_at:
                try:
                    age = (stamp - pd.Timestamp(seen_at)).total_seconds() / 86400.0
                    due = age >= float(heartbeat_days)
                except Exception:  # noqa: BLE001
                    due = True
        if not moved and not due:
            continue
        rows.append({
            "at": str(stamp) if stamp is not None else "",
            "kind": "factor",
            "ticker": str(ticker),
            "margin_factor": factor,
            "previous": previous,
            "changed": bool(moved and previous is not None),
        })
    return rows


def factor_changes(config=None, account: str | None = None) -> list[dict]:
    """Every time a margin factor actually moved, oldest first.

    The direct answer to "does the broker change this on positions already
    open". An empty list after a long run is a real answer; an empty list
    after a short one is not, which is why ``usage_summary`` reports how long
    the series has been running alongside it.
    """
    return [
        row for row in load_margin(config, account)
        if str(row.get("kind") or "") == "factor" and bool(row.get("changed"))
    ]


def usage_summary(config=None, account: str | None = None) -> dict:
    """Margin utilisation now and at its worst, with the sample behind it.

    ``checked`` is False when nothing has been recorded. A system that has
    never looked and an account using no margin are different statements, and
    this project reports the first as unchecked rather than as zero.
    """
    rows = [
        row for row in load_margin(config, account)
        if str(row.get("kind") or "") == "usage"
    ]
    summary = {
        "checked": bool(rows),
        "observations": len(rows),
        "latest": None,
        "peak": None,
        "first_at": "",
        "latest_at": "",
        "factor_changes": len(factor_changes(config, account)),
    }
    if not rows:
        return summary

    def share(row):
        try:
            balance = float(row.get("balance") or 0.0)
            used = float(row.get("used") or 0.0)
            return used / balance if balance > 0 else None
        except (TypeError, ValueError):
            return None

    shares = [(row, share(row)) for row in rows]
    usable = [(row, value) for row, value in shares if value is not None]
    summary["first_at"] = str(rows[0].get("at") or "")
    summary["latest_at"] = str(rows[-1].get("at") or "")
    if usable:
        summary["latest"] = usable[-1][1]
        summary["peak"] = max(value for _, value in usable)
    return summary
