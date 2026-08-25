"""Deciding what is worth interrupting you for, and only saying it once.

An advisor that emails the same thing every five minutes is one you stop
reading, and then it is worse than nothing -- the alert you would have acted
on arrives in a thread you have learned to ignore. So the decision here is
not "is this worth knowing" but **"is this new"**.

What counts as new
------------------
*A suggestion* is new for the day it was produced. Signals come from daily
bars, so the same instrument clearing the bar tomorrow is a fresh statement;
the same one still clearing it this afternoon is not.

*A position warning* is new when it **crosses into** trouble. A position
sitting at 15% from its stop for an hour is one alert, not twelve. It can
alert again if it recovers and comes back, because that genuinely happened
twice.

State lives in one small file and is rewritten, like the account registry and
the quote file: it says what has been said *now*, and last week's copy tells
nobody anything.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "ALERT_DIR",
    "STATE_FILE",
    "Alert",
    "decide_alerts",
    "load_seen",
    "save_seen",
]

ALERT_DIR = "advisor"
STATE_FILE = "alerted.json"
SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class Alert:
    """One thing worth saying, and how loudly."""

    key: str
    kind: str          # "open" or "position"
    severity: str      # "critical" | "warning" | "info"
    headline: str
    detail: str = ""

    @property
    def urgent(self) -> bool:
        return self.severity == "critical"


def _day(value: Any) -> str:
    """The date a signal belongs to, for keying a once-a-day alert."""
    import pandas as pd

    try:
        stamp = pd.Timestamp(value)
    except (TypeError, ValueError):
        return "unknown"
    if pd.isna(stamp):
        return "unknown"
    return stamp.strftime("%Y-%m-%d")


def _shown(ticker: str) -> str:
    """The Trading 212 name with the lake symbol after it, or just the symbol."""
    try:
        from cfdinvest.livehelper.t212_instruments import display_name

        return display_name(ticker)
    except Exception:  # noqa: BLE001 - a name is not worth a failed alert
        return str(ticker)


def decide_alerts(
    suggestions: Sequence[Any] = (),
    watches: Sequence[Any] = (),
    seen: Iterable[str] = (),
    *,
    as_of: Any = None,
) -> tuple[list[Alert], set[str]]:
    """What to send, and the new set of things already said. Pure.

    Returns ``(alerts, seen)``. ``seen`` is the *complete* new state rather
    than an addition, so a position that has recovered drops out of it and
    can alert again if it comes back -- which is a real event, not a repeat.

    Only the tradable suggestions raise an alert. Waking someone for an
    instrument they cannot open a position in is exactly the sort of noise
    that gets a channel muted.
    """
    already = {str(key) for key in (seen or ())}
    fresh: list[Alert] = []
    now_seen: set[str] = set()
    day = _day(as_of)

    for watch in watches or ():
        severity = getattr(watch, "severity", "pass")
        if severity not in ("warning", "critical"):
            # Not in trouble: it drops out of the state, so a later slide
            # back towards the stop alerts again.
            continue
        ticker = getattr(watch, "ticker", "?")
        key = f"position|{ticker}|{severity}"
        now_seen.add(key)
        if key in already:
            continue
        fresh.append(Alert(
            key=key,
            kind="position",
            severity=severity,
            headline=(
                f"{_shown(ticker)} is past its stop"
                if severity == "critical"
                else f"{_shown(ticker)} is close to its stop"
            ),
            detail=getattr(watch, "note", "") or "",
        ))

    for suggestion in suggestions or ():
        if not getattr(suggestion, "tradable", True):
            continue
        ticker = getattr(suggestion, "ticker", "?")
        direction = getattr(suggestion, "direction", "?")
        key = f"open|{ticker}|{direction}|{day}"
        now_seen.add(key)
        if key in already:
            continue

        quantity = getattr(suggestion, "quantity", None)
        cash = getattr(suggestion, "cash", None)
        amount = (
            f"{quantity:,.4f} units" if quantity is not None
            else (f"{cash:,.2f} of your balance" if cash is not None else "size unknown")
        )
        # The Trading 212 name in the headline, because the email is read
        # with the app open and the lake's symbol is not searchable there.
        try:
            from cfdinvest.livehelper.t212_instruments import display_name

            shown = display_name(ticker)
        except Exception:  # noqa: BLE001 - a name is not worth a failed alert
            shown = ticker

        fresh.append(Alert(
            key=key,
            kind="open",
            severity="info",
            headline=(
                f"{direction.capitalize()} {shown} "
                f"({getattr(suggestion, 'confidence', 0.0):.0%} confidence)"
            ),
            detail=(
                f"About {amount}. Check it in the Live-Execution Helper before "
                "placing it."
            ),
        ))

    # Urgent first, then new positions, then suggestions.
    order = {"critical": 0, "warning": 1, "info": 2}
    fresh.sort(key=lambda alert: (order.get(alert.severity, 9), alert.key))
    return fresh, now_seen


def _state_path(config=None, account: str | None = None) -> Path:
    from cfdinvest.persist.storage import account_root

    return account_root(config, account) / ALERT_DIR / STATE_FILE


def load_seen(config=None, account: str | None = None) -> set[str]:
    """What has already been said. Never raises.

    An unreadable state file means everything looks new, which sends one
    duplicate round rather than silently swallowing an alert. Of the two
    failure modes that is the one to choose.
    """
    path = _state_path(config, account)
    try:
        if not path.is_file():
            return set()
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        logger.warning("Alert state unreadable at %s: %s", path, error)
        return set()
    keys = document.get("seen") if isinstance(document, dict) else None
    return {str(key) for key in (keys or [])}


def save_seen(seen: Iterable[str], config=None, account: str | None = None) -> bool:
    """Rewrite the state. Never raises."""
    from cfdinvest.persist.storage import atomic_write_text

    path = _state_path(config, account)
    document = {"schema_version": SCHEMA_VERSION, "seen": sorted(set(seen or ()))}
    try:
        atomic_write_text(json.dumps(document, indent=2), path)
        return True
    except Exception as error:  # noqa: BLE001 - a failed write is not an outage
        logger.warning("Could not record what was alerted: %s", error)
        return False
