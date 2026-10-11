"""What the last look saw, kept when it disagrees badly with the engine.

The failure this prevents
-------------------------
On 2026-10-08 the live re-check read nine names the engine had just
entered long at about 0.84 as exactly flat, on every pass, all day. The
order store said THAT it disagreed and could not say WHY, and the day could
not be replayed: the quotes it priced against live in ``livequotes`` as a
single ``latest.json`` that the next poll overwrites. It took a hand-built
reconstruction from the plan rows' prices to find the cause.

So when a FRESH decision -- the engine's entry on the newest completed bar,
``entered_bars_ago == 0`` -- is refused by the last look with the live
reading at least ``DISAGREEMENT`` below the engine's, the pass writes one
row: the moment, every price the check used, every reading with its reason
and regime, and the names that disagreed. With the lake's own bars that is
everything ``livecheck.reevaluate`` needs to run the moment again.

Chases are excluded on purpose: a name entered hours ago has had time to
decay, and a check refusing it is the gate working, not disagreeing.

Append-only, like every observation here. Diagnostic: nothing reads it to
decide anything.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Iterable, Mapping

logger = logging.getLogger(__name__)

__all__ = ["DISAGREEMENT", "disagreements", "record_snapshot", "load_snapshots"]

#: How far below the engine's reading the live one must sit, on a fresh
#: entry, to be kept. A third of a component is 0.33; the 2026-09-10 and
#: 2026-10-08 failures read 0.32 and 0.84 low. Marginal names noise pushes
#: under a 0.75 bar sit within about 0.05 and are not worth a row each pass.
DISAGREEMENT = 0.25


def _path(config=None, account: str | None = None) -> Path:
    from cfdinvest.persist.storage import account_root

    return Path(account_root(config, account)) / "last_look" / "disagreements.jsonl"


def disagreements(intents: Iterable[Any]) -> list[dict[str, Any]]:
    """The fresh last-look refusals that sit far below the engine. Pure.

    An absent live reading counts as zero only when the check refused on
    "no longer a signal" -- the reading IS flat then. "not re-checked"
    carries no reading and is a different failure, reported elsewhere.
    """
    from cfdinvest.persist.orders import LAST_LOOK_REASONS

    found = []
    for intent in intents or ():
        if getattr(intent, "action", "") != "refused":
            continue
        reason = str(getattr(intent, "reason", "") or "")
        if reason not in LAST_LOOK_REASONS or reason == "not re-checked":
            continue
        if getattr(intent, "entered_bars_ago", None) != 0:
            continue
        engine = getattr(intent, "engine_confidence", None)
        live = getattr(intent, "live_confidence", None)
        if engine is None:
            continue
        if live is None:
            if reason != "no longer a signal":
                continue
            live = 0.0
        if float(engine) - float(live) >= DISAGREEMENT:
            found.append({"ticker": intent.ticker, "direction": intent.direction,
                          "engine_confidence": float(engine),
                          "live_confidence": float(live), "reason": reason})
    return found


def record_snapshot(
    *, at, candidates, prices: Mapping[str, float], readings: Mapping[str, Mapping],
    disagreeing: list[dict], config=None, account: str | None = None,
) -> bool:
    """Append one row. Never raises: losing a diagnostic must not stop a pass."""
    try:
        row = {
            "at": str(at),
            "candidates": sorted(str(c) for c in candidates or ()),
            "prices": {str(k): float(v) for k, v in (prices or {}).items() if v is not None},
            "readings": {
                str(k): {key: (str(val) if key == "as_of" else val) for key, val in dict(v).items()}
                for k, v in (readings or {}).items()
            },
            "disagreeing": list(disagreeing),
        }
        path = _path(config, account)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")
        return True
    except Exception as error:  # noqa: BLE001
        logger.warning("Could not record the last-look snapshot: %s", error)
        return False


def load_snapshots(config=None, account: str | None = None) -> list[dict]:
    """Every snapshot, oldest first. Never raises; skips a truncated line."""
    rows: list[dict] = []
    try:
        path = _path(config, account)
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError as error:
        logger.warning("Last-look snapshots unreadable: %s", error)
    return rows
