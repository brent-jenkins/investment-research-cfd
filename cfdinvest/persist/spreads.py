"""What a trade actually costs, read from the broker and appended.

The failure this prevents
------------------------
Spread is the largest line in the cost model — 46.6% of everything charged —
and it was one constant of 10bp for every instrument in every condition.
Measuring it once showed the constant was roughly double the median. But
replacing one guess with one snapshot is still a guess, and a single
afternoon's reading would characterise nothing:

* across instruments it ranges 2.8bp to 28.6bp, a factor of ten
* outside the session it widens by a median of 1.85×, and JNJ by 14.9×
* between two readings minutes apart in the same session AVGO moved
  5.1 → 3.2 and TSLA 4.5 → 5.9

The last of those is why this exists. A spread is not a property of an
instrument, it is a property of an instrument *at a moment*, and the moment
that matters is the one the engine trades on. So readings are appended on
the same cadence as the bars the engine decides from, and the cost model
reads a distribution rather than a number somebody wrote down.

**Append-only, like every other observation here.** A spread reading
describes a moment that does not come back, and one that can be edited
afterwards is not evidence of anything.

**Every reading carries whether the exchange session was open.** Not for
filtering later out of tidiness: an out-of-session spread is nearly twice an
in-session one, and pooling them would produce a median that describes
trading nobody does. The summary uses in-session readings by default and
says how many it had.

**The funding rates are recorded raw and unconverted.** The broker returns
``-0.0215402`` with no documented unit. Read as a percentage per day that is
7.9% a year, which is plausible for a CFD; read as a fraction it is 786%,
which is not. Recording the raw figure means the day that ambiguity is
settled against a real charge, the history does not have to be recomputed
from an assumption that was wrong.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "SPREAD_DIR",
    "MIN_OBSERVATIONS",
    "record_spreads",
    "load_spreads",
    "spread_summary",
]

#: Where the readings live, under the account root.
SPREAD_DIR = "spreads"

#: Readings needed before a measured spread is used in place of the class
#: default.
#:
#: One session at the intraday cadence is about twenty-six, so this is
#: roughly "most of a day". Below it the figures are still available and
#: still shown; what is withheld is the *claim* that they describe the
#: instrument, which is the same rule the win rate follows at twenty closed
#: trades and the bias layer at its own floor.
MIN_OBSERVATIONS = 20


def _path(config=None, account: str | None = None) -> Path:
    from cfdinvest.persist.storage import account_root

    return Path(account_root(config, account)) / SPREAD_DIR / "observations.jsonl"


def record_spreads(
    rows: Iterable[Mapping[str, Any]],
    config=None,
    account: str | None = None,
) -> int:
    """Append one reading per instrument. Returns how many. Never raises.

    Takes plain mappings rather than broker objects, so this layer holds no
    import of anything that can reach a network — the caller reads, this
    writes, and the two cannot be confused for each other.
    """
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
        logger.warning("Could not record spread readings: %s", error)
        return written
    return written


def load_spreads(config=None, account: str | None = None) -> list[dict]:
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
                logger.warning("Skipping a malformed spread reading in %s", path)
    except OSError as error:
        logger.warning("Spread readings unreadable at %s: %s", path, error)
    return rows


def spread_summary(
    rows: Sequence[Mapping[str, Any]] | None = None,
    *,
    tickers: Sequence[str] | None = None,
    in_session_only: bool = True,
    minimum: int = MIN_OBSERVATIONS,
    scope: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Per instrument: the median spread, its sample, and whether to use it.

    Returns ``{ticker: {...}}`` carrying ``median_bp``, ``observations``,
    ``usable`` and, where there are enough, ``p90_bp``.

    ``usable`` is the whole point. A median of three readings is arithmetic,
    not a measurement, and the cost model needs to know which it is holding —
    so the figure is always reported and the *claim* is what gets withheld.

    ``scope`` selects one series. Traded instruments are read every fifteen
    minutes and candidates once a day, so a ticker that has been both would
    otherwise have the two cadences pooled into one median — dominated by
    whichever was sampled more often, and describing neither. Rows written
    before the scope existed carry none and count as ``traded``, which is
    what they were.
    """
    import statistics

    readings = list(rows or ())
    wanted = set(tickers) if tickers else None

    grouped: dict[str, list[float]] = {}
    for row in readings:
        ticker = str(row.get("ticker") or "")
        if not ticker or (wanted is not None and ticker not in wanted):
            continue
        if scope is not None and str(row.get("scope") or "traded") != scope:
            continue
        if in_session_only and not row.get("session_open"):
            continue
        value = row.get("spread_bp")
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number <= 0 or number != number:
            continue
        grouped.setdefault(ticker, []).append(number)

    summary: dict[str, dict[str, Any]] = {}
    for ticker in sorted(wanted or grouped):
        values = sorted(grouped.get(ticker, ()))
        if not values:
            # Named rather than omitted. A ticker missing from this mapping
            # and one with no readings look identical to a caller iterating
            # it, and only one of them is a gap worth chasing.
            summary[ticker] = {
                "median_bp": None, "p90_bp": None,
                "observations": 0, "usable": False,
            }
            continue
        summary[ticker] = {
            "median_bp": statistics.median(values),
            "p90_bp": (
                values[int(len(values) * 0.9)] if len(values) >= minimum else None
            ),
            "observations": len(values),
            "usable": len(values) >= minimum,
        }
    return summary
