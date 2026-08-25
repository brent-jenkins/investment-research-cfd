"""One journal entry: what happened, and what was known when it did.

The rule this layer is built on
-------------------------------
**An entry is written from what was known at the time, and never rewritten
afterwards.**

Regenerating a narrative later, with the outcome in hand, turns it into a
story about the result rather than the decision. "I was right to take it
because it won" is exactly the lesson a journal must not teach — a good
decision that lost and a bad one that won look identical in hindsight, and
telling them apart is the only reason to keep a journal at all.

So an entry is composed once, at the moment, from the artefacts as they stood.
When the outcome arrives it is **appended as a separate entry**, not merged
into the first. The narrative keeps saying what you believed; the outcome says
what happened; the reader puts them side by side and does the work.

Nothing here recomputes
-----------------------
No P&L is calculated, no size is checked, no verdict is reached. Every figure
is read from an artefact some other layer wrote and the only thing added is
English. A test parses this package and fails the build if it imports anything
that could compute.

Pure: mappings in, an entry out.
"""

from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

logger = logging.getLogger(__name__)

__all__ = [
    "KINDS",
    "JournalEntry",
    "compose_entry",
    "compose_outcome_entry",
    "narrate",
    "behavioural_note",
    "execution_note",
    "regime_note",
    "what_changed",
]

#: ``trade`` — you did something. ``considered`` — you looked and did not.
#: ``outcome`` — a position you had closed, recorded after the fact.
#: ``note`` — anything you wrote yourself.
KINDS: tuple[str, ...] = ("trade", "considered", "outcome", "note")


def _finite(value: Any) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _get(source: Any, name: str, default=None):
    """Read a field from either an object or a mapping."""
    if source is None:
        return default
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


@dataclass(frozen=True, slots=True)
class JournalEntry:
    """One thing that happened, and the context it happened in.

    Attributes
    ----------
    narrative:
        What you did, in a sentence you can read a year later without the
        surrounding artefacts.
    behavioural_note, execution_note, regime_note:
        The three lenses worth separating. What you were like, what the fill
        was like, what the market was like — a single blob mixes evidence
        about you with evidence about the market, and the two lead to
        different corrections.
    changed:
        What is different since the previous entry. The one thing a journal
        can say that no single artefact can.
    """

    timestamp: Any
    kind: str = "trade"
    instrument: str | None = None
    direction: str | None = None
    quantity: float | None = None
    price: float | None = None
    narrative: str = ""
    behavioural_note: str = ""
    execution_note: str = ""
    regime_note: str = ""
    changed: tuple[str, ...] = ()
    verdict: str | None = None
    reasons: tuple[str, ...] = ()
    regimes: dict[str, str] = field(default_factory=dict)
    run_id: str | None = None
    source: str = "auto"
    notes: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}, got {self.kind!r}")

    @property
    def headline(self) -> str:
        """One line, for a list."""
        if self.kind == "note":
            return self.notes or "Note"
        parts = [p for p in (self.direction, self.instrument) if p]
        return f"{self.kind}: {' '.join(parts)}" if parts else self.kind

    def to_document(self) -> dict[str, Any]:
        from cfdinvest.persist.writers import jsonable

        payload = asdict(self)
        payload["timestamp"] = (
            self.timestamp.isoformat()
            if hasattr(self.timestamp, "isoformat")
            else str(self.timestamp)
        )
        return jsonable(payload)

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> "JournalEntry":
        """Rebuild from disk, ignoring fields a later version may add."""
        known = set(cls.__slots__)
        payload = {k: v for k, v in dict(document).items() if k in known}
        payload.setdefault("timestamp", None)
        for name in ("changed", "reasons"):
            if name in payload and payload[name] is not None:
                payload[name] = tuple(payload[name])
        return cls(**payload)


# --------------------------------------------------------------------------- #
# The narrative
# --------------------------------------------------------------------------- #
def narrate(confirmation=None, assessment=None, recommendation=None) -> str:
    """What you did, in a sentence.

    Written in the past tense and without a judgement. A journal that told you
    the trade was good or bad at the moment of writing would be doing the
    reader's job, and doing it with less information than the reader will have.
    """
    action = str(_get(confirmation, "action", "") or "").lower()
    instrument = _get(confirmation, "ticker") or _get(assessment, "instrument")
    direction = _get(confirmation, "direction")
    quantity = _get(confirmation, "quantity")
    price = _get(confirmation, "price")

    if action == "ignore":
        return (
            f"Looked at {instrument} and did not take it."
            + (f" {_get(confirmation, 'notes')}" if _get(confirmation, "notes") else "")
        )

    if action == "close":
        return (
            f"Closed {instrument}"
            + (f" at {float(price):,.4f}" if _finite(price) else "")
            + "."
        )

    if action == "flip":
        close_price = _get(confirmation, "close_price")
        return (
            f"Flipped {instrument} to {direction}"
            + (f", closing at {float(close_price):,.4f}" if _finite(close_price) else "")
            + (f" and opening at {float(price):,.4f}" if _finite(price) else "")
            + "."
        )

    if instrument is None:
        return "Recorded an entry with no instrument."

    sentence = f"Opened {direction or 'a position in'} {instrument}"
    if _finite(quantity):
        sentence += f", {abs(float(quantity)):,.4f} units"
    if _finite(price):
        sentence += f" at {float(price):,.4f}"
    sentence += "."

    stop = _get(recommendation, "stop_level")
    target = _get(recommendation, "take_profit_level")
    if _finite(stop) or _finite(target):
        bits = []
        if _finite(stop):
            bits.append(f"stop {float(stop):,.4f}")
        if _finite(target):
            bits.append(f"target {float(target):,.4f}")
        sentence += f" Plan: {', '.join(bits)}."
    return sentence


def behavioural_note(assessment=None, bias=None, refinement=None) -> str:
    """What you were like, as against what the trade was like.

    Kept apart from the execution note on purpose: evidence about you and
    evidence about the market lead to different corrections, and one blob
    invites attributing the first to the second.
    """
    parts: list[str] = []

    status = _get(assessment, "status")
    if status in ("BORDERLINE", "UNSAFE"):
        parts.append(f"Took it with a {status} verdict standing.")

    if bias is not None and _get(bias, "available"):
        measured = _get(bias, "measured", ()) or ()
        loud = []
        for name in measured:
            severity = bias.severity(name) if hasattr(bias, "severity") else None
            if severity is not None and float(severity) >= 0.5:
                loud.append(f"{name.replace('_', ' ')} {float(severity):.0%}")
        if loud:
            parts.append(f"Behavioural profile at the time: {', '.join(loud)}.")
    else:
        parts.append("No behavioural profile was available.")

    multiplier = _get(refinement, "multiplier")
    if _finite(multiplier) and abs(float(multiplier) - 1.0) > 0.01:
        parts.append(
            f"The refinement layer suggested {float(multiplier):.0%} of the "
            "intended size."
        )
    return " ".join(parts)


def execution_note(confirmation=None, review=None) -> str:
    """What the fill was like, against what was intended."""
    parts: list[str] = []

    slippage = _get(confirmation, "slippage")
    if slippage is None:
        slippage = _get(review, "slippage")
    share = _get(review, "slippage_share_of_stop")

    if _finite(slippage) and abs(float(slippage)) > 0:
        worse = "worse" if float(slippage) > 0 else "better"
        sentence = f"Filled {abs(float(slippage)):,.4f} {worse} than planned"
        if _finite(share):
            sentence += f" — {abs(float(share)):.0%} of the stop distance"
        parts.append(sentence + ".")

    deviation = _get(confirmation, "quantity_deviation")
    if deviation is None:
        deviation = _get(review, "quantity_deviation")
    if _finite(deviation) and abs(float(deviation) - 1.0) > 0.01:
        parts.append(
            f"Took {float(deviation):.0%} of the recommended size."
        )

    status = _get(review, "status")
    if status and status != "AS PLANNED":
        parts.append(f"Against the plan: {status}.")

    return " ".join(parts) or "Filled as planned."


def regime_note(regimes: Mapping[str, str] | None, elevated=()) -> str:
    """What the market was like, in the classifier's own words."""
    if not regimes:
        return "No regime classification was recorded."

    described = ", ".join(
        f"{name.replace('_regime', '').replace('_', ' ')} {label}"
        for name, label in regimes.items()
    )
    # No "Market:" prefix -- the renderer supplies the label, and having both
    # produced "Market: Market: volatility high".
    note = f"{described.capitalize()}."
    if elevated:
        note += (
            f" Elevated: {', '.join(str(e).replace('_regime', '') for e in elevated)}."
        )
    return note


def what_changed(current: Mapping[str, Any], previous=None) -> tuple[str, ...]:
    """What is different since the previous entry.

    The one thing a journal can say that no single artefact can. A regime that
    turned last Tuesday is invisible in today's snapshot and obvious in the
    difference between two.
    """
    if previous is None:
        return ("First entry — nothing to compare against yet.",)

    changes: list[str] = []

    before = dict(_get(previous, "regimes", {}) or {})
    after = dict(current.get("regimes") or {})
    for name in sorted(set(before) | set(after)):
        was, now = before.get(name), after.get(name)
        if was != now and was is not None and now is not None:
            changes.append(
                f"{name.replace('_regime', '').replace('_', ' ').capitalize()} "
                f"regime moved from {was} to {now}."
            )

    was_verdict = _get(previous, "verdict")
    now_verdict = current.get("verdict")
    if was_verdict and now_verdict and was_verdict != now_verdict:
        changes.append(f"Verdict moved from {was_verdict} to {now_verdict}.")

    was_instrument = _get(previous, "instrument")
    now_instrument = current.get("instrument")
    if was_instrument and now_instrument and was_instrument != now_instrument:
        changes.append(
            f"Different instrument: {was_instrument} last time, "
            f"{now_instrument} this time."
        )

    gap = _elapsed(current.get("timestamp"), _get(previous, "timestamp"))
    if gap:
        changes.append(gap)

    return tuple(changes) or ("Nothing material has changed since the last entry.",)


def _elapsed(now, before) -> str | None:
    """How long since the last entry, when both timestamps parse."""
    try:
        import pandas as pd

        difference = pd.Timestamp(now) - pd.Timestamp(before)
    except Exception:  # noqa: BLE001
        return None

    seconds = difference.total_seconds()
    if seconds < 0:
        return None
    # Below an hour there is nothing to say. "0 hour(s) since the last entry"
    # is noise, and noise in the one field that reports change makes the real
    # changes harder to see.
    if seconds < 3_600:
        return None
    if seconds < 86_400:
        return f"{seconds / 3600.0:.0f} hour(s) since the last entry."
    return f"{seconds / 86_400.0:.0f} day(s) since the last entry."


# --------------------------------------------------------------------------- #
# Composing
# --------------------------------------------------------------------------- #
def compose_entry(
    confirmation=None,
    *,
    assessment=None,
    recommendation=None,
    review=None,
    regimes: Mapping[str, str] | None = None,
    elevated=(),
    bias=None,
    refinement=None,
    previous=None,
    run_id: str | None = None,
    timestamp=None,
    source: str = "auto",
) -> JournalEntry:
    """Build one entry from the artefacts as they stand now.

    Composed once and stored. Calling this again later with a different
    ``regimes`` would produce a different entry, which is why the writer
    stores the result rather than the inputs.
    """
    moment = timestamp or _get(confirmation, "timestamp")
    action = str(_get(confirmation, "action", "") or "").lower()
    kind = "considered" if action == "ignore" else "trade"

    current = {
        "timestamp": moment,
        "regimes": dict(regimes or {}),
        "verdict": _get(assessment, "status"),
        "instrument": _get(confirmation, "ticker"),
    }

    return JournalEntry(
        timestamp=moment,
        kind=kind,
        instrument=_get(confirmation, "ticker"),
        direction=_get(confirmation, "direction"),
        quantity=_get(confirmation, "quantity"),
        price=_get(confirmation, "price"),
        narrative=narrate(confirmation, assessment, recommendation),
        behavioural_note=behavioural_note(assessment, bias, refinement),
        execution_note=execution_note(confirmation, review),
        regime_note=regime_note(regimes, elevated),
        changed=what_changed(current, previous),
        verdict=_get(assessment, "status"),
        reasons=tuple(_get(assessment, "reasons", ()) or ()),
        regimes=dict(regimes or {}),
        run_id=run_id,
        source=source,
        notes=_get(confirmation, "notes"),
        details={
            "action": action or None,
            "exposure": _get(confirmation, "exposure"),
            "recommended_price": _get(confirmation, "recommended_price"),
            "recommended_quantity": _get(confirmation, "recommended_quantity"),
        },
    )


def compose_outcome_entry(
    instrument: str,
    *,
    realised_pnl: float | None = None,
    opened_at=None,
    closed_at=None,
    entry=None,
    exit_price=None,
    timestamp=None,
    run_id: str | None = None,
    notes: str | None = None,
) -> JournalEntry:
    """Record how a position ended, as a **new** entry.

    Never merged into the entry that opened it. A good decision that lost and
    a bad one that won look identical once the result is written over the
    reasoning, and telling those apart is the whole point of the exercise.

    The P&L figure is read from the decomposition the execution layer wrote.
    Nothing here computes it.
    """
    parts = [f"Closed {instrument}"]
    if _finite(exit_price):
        parts.append(f"at {float(exit_price):,.4f}")
    sentence = " ".join(parts) + "."

    if _finite(realised_pnl):
        sentence += (
            f" Realised {float(realised_pnl):,.2f} — as recorded by the "
            "execution layer, not recomputed here."
        )

    held = _elapsed(closed_at, opened_at)
    if held:
        sentence += f" Held {held.replace(' since the last entry.', '.')}"

    return JournalEntry(
        timestamp=timestamp or closed_at,
        kind="outcome",
        instrument=instrument,
        price=float(exit_price) if _finite(exit_price) else None,
        narrative=sentence,
        behavioural_note=(
            "The outcome is filed separately from the decision on purpose. "
            "Read it beside the original entry rather than instead of it."
        ),
        execution_note="",
        regime_note="",
        changed=(),
        run_id=run_id,
        source="auto",
        notes=notes,
        details={
            "realised_pnl": float(realised_pnl) if _finite(realised_pnl) else None,
            "opened_at": opened_at,
            "closed_at": closed_at,
            "entry_price": float(entry) if _finite(entry) else None,
        },
    )
