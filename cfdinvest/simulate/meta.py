"""Applying a posture inside a rehearsal, and recording what it did.

The simulation already replays a run with the helper's advice applied. This
adds the posture to that: each decision is taken under the profile in force,
and every change of posture is written down.

The journal is the output
-------------------------
A single posture applied to a whole run tells you very little — it is one
number multiplied through. What is worth having is the **sequence**: when the
posture moved, what moved it, and what the multipliers were on each side.

So this produces a meta-strategy journal rather than a summary statistic. A
run of steadily more forward postures is a thing to notice, and no aggregate
shows it.

The ceiling still applies
-------------------------
Nothing here bypasses it. Postures are applied through the same
``refine_with_meta``, so a simulated forward posture is capped by the same
safety-approved size a live one would be — which is most of the point of
rehearsing it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Sequence

import pandas as pd

logger = logging.getLogger(__name__)

__all__ = ["PostureChange", "MetaJournal", "simulate_postures"]


@dataclass(frozen=True, slots=True)
class PostureChange:
    """One moment the posture moved, and what moved it."""

    at: Any
    was: str
    now: str
    score: float = 0.0
    risk_budget: float = 0.0
    sizing_multiplier: float = 1.0
    exit_multiplier: float = 1.0
    reason: str = ""

    @property
    def forward(self) -> bool:
        from cfdinvest.metastrategy.profile import POSTURES

        return POSTURES.index(self.now) > POSTURES.index(self.was)


@dataclass(frozen=True, slots=True)
class MetaJournal:
    """Every posture the rehearsal passed through."""

    changes: tuple[PostureChange, ...] = ()
    postures: dict[str, int] = field(default_factory=dict)
    capped: int = 0
    decisions: int = 0
    notes: tuple[str, ...] = ()

    @property
    def moved(self) -> int:
        return len(self.changes)

    @property
    def drifted_forward(self) -> bool:
        """More moves forward than back.

        Worth a name: a posture that only ever ratchets one way is not
        responding to the record, it is following it.
        """
        if not self.changes:
            return False
        forward = len([change for change in self.changes if change.forward])
        return forward > len(self.changes) - forward

    def to_frame(self) -> pd.DataFrame:
        if not self.changes:
            return pd.DataFrame(
                columns=["at", "was", "now", "score", "risk_budget",
                         "sizing_multiplier", "exit_multiplier", "reason"]
            )
        return pd.DataFrame(
            [
                {
                    "at": change.at,
                    "was": change.was,
                    "now": change.now,
                    "score": change.score,
                    "risk_budget": change.risk_budget,
                    "sizing_multiplier": change.sizing_multiplier,
                    "exit_multiplier": change.exit_multiplier,
                    "reason": change.reason,
                }
                for change in self.changes
            ]
        )

    def to_document(self) -> dict[str, Any]:
        from cfdinvest.persist.writers import jsonable

        return jsonable(
            {
                # First key, like every other simulated artefact.
                "simulated": True,
                "decisions": self.decisions,
                "postures": dict(self.postures),
                "capped_by_safety": self.capped,
                "moves": self.moved,
                "drifted_forward": self.drifted_forward,
                "changes": [
                    {
                        "at": (
                            change.at.isoformat()
                            if hasattr(change.at, "isoformat")
                            else str(change.at)
                        ),
                        "was": change.was,
                        "now": change.now,
                        "score": change.score,
                        "sizing_multiplier": change.sizing_multiplier,
                        "reason": change.reason,
                    }
                    for change in self.changes
                ],
                "notes": list(self.notes),
            }
        )


def simulate_postures(
    profiles: Sequence[tuple[Any, Any]],
    *,
    capped_flags: Sequence[bool] = (),
) -> MetaJournal:
    """Turn a sequence of ``(moment, profile)`` into a journal.

    Pure. The rehearsal decides what the profile is at each point; this
    records the shape of the sequence, which is the part worth reading.
    """
    changes: list[PostureChange] = []
    counts: dict[str, int] = {}
    previous = None

    for moment, profile in profiles:
        if profile is None:
            continue
        counts[profile.posture] = counts.get(profile.posture, 0) + 1

        if previous is not None and profile.posture != previous.posture:
            changes.append(
                PostureChange(
                    at=moment,
                    was=previous.posture,
                    now=profile.posture,
                    score=profile.score,
                    risk_budget=profile.risk_budget,
                    sizing_multiplier=profile.sizing_multiplier,
                    exit_multiplier=profile.exit_multiplier,
                    reason=profile.notes[0] if profile.notes else "",
                )
            )
        previous = profile

    notes: list[str] = []
    capped = len([flag for flag in capped_flags if flag])
    if capped:
        notes.append(
            f"{capped} decision(s) asked for more size than the safety engine "
            "allowed and were held at the safe size. The posture was applied "
            "and overruled, which is not the same as it never asking."
        )
    if changes and len([c for c in changes if c.forward]) > len(changes) / 2:
        notes.append(
            "The posture moved forward more often than back. A posture that "
            "ratchets one way is following the record rather than responding "
            "to it."
        )
    if not changes and counts:
        notes.append(
            f"The posture never moved from {next(iter(counts))}. With one "
            "posture throughout, this rehearsal measures a single multiplier."
        )

    return MetaJournal(
        changes=tuple(changes),
        postures=counts,
        capped=capped,
        decisions=len([profile for _, profile in profiles if profile is not None]),
        notes=tuple(notes),
    )
