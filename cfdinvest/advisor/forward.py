"""What the system expected, recorded before the answer was known.

Everything measured in this project so far has been measured backwards.
That is the right way to find a defect -- the financing sign error, the
resize ratchet, the eleven hours of stale code -- and a poor way to judge a
strategy that trades a horizon of days. The universe was chosen in 2026
from instruments that are large now, so a long-biased backtest on it is
measuring survivorship; and several of the inputs worth testing, options
positioning above all, have no history to test against at all.

So this records the prediction and the state it was made in, before the
outcome exists, and scores it later. A forward record cannot be
overfitted, cannot look ahead, and does not care that the universe was
picked with hindsight -- it only asks whether what the system said
yesterday was any good.

It is not a substitute for the backtest. It is the half the backtest
cannot supply, and after a few weeks it is the half that matters for a
strategy holding positions for days.

What is recorded
----------------
For every instrument on every run: the direction and confidence the engine
produced, the price it was produced at, and each risk input that has been
measured -- volume against its own normal, the filing rate, the regime, and
the news reading. The last two are recorded despite the news reading having
failed seven tests, because the point of a forward record is to be able to
answer the question later rather than to assume it now.

Append-only, like every other observation store here. A prediction that
can be edited after the fact is not a prediction.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

logger = logging.getLogger(__name__)

__all__ = [
    "FORWARD_DIR",
    "Observation",
    "record_observations",
    "load_observations",
    "score_observations",
]

FORWARD_DIR = "forward"
SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class Observation:
    """One instrument, one run, before the outcome is known."""

    recorded_at: str
    ticker: str
    #: The engine's own view.
    direction: str = "flat"
    confidence: float | None = None
    size: float | None = None
    price: float | None = None
    #: Whether it cleared the bar to be offered as a trade.
    suggested: bool = False

    #: The state it was decided in. Each of these was measured; the
    #: t-statistic is in the module that produces it.
    volume_ratio: float | None = None
    filings_90d: int | None = None
    regime: str = ""
    sentiment: float | None = None
    sentiment_n: int | None = None

    def as_row(self) -> dict[str, Any]:
        return asdict(self)


def record_observations(
    observations: Iterable[Observation], config=None, account: str | None = None
) -> int:
    """Append today's predictions. Never raises; returns the number written.

    Appended rather than rewritten, and that is the whole point. The value
    of this file is that nothing in it can be revised once the outcome is
    known, which is the one property a backtest can never have.
    """
    rows = list(observations or ())
    if not rows:
        return 0

    path = _path(config, account)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row.as_row(), sort_keys=True) + "\n")
        return len(rows)
    except OSError as error:
        logger.warning("Could not record observations to %s: %s", path, error)
        return 0


def load_observations(config=None, account: str | None = None) -> list[dict]:
    """Everything recorded so far. Never raises.

    A malformed line is skipped rather than failing the read: the file is
    appended to by a scheduled job, and one truncated write should not cost
    every observation before it.
    """
    path = _path(config, account)
    rows: list[dict] = []
    try:
        if not path.is_file():
            return rows
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("Skipping a malformed observation in %s", path)
    except OSError as error:
        logger.warning("Observations unreadable at %s: %s", path, error)
    return rows


def score_observations(
    observations: Iterable[Mapping[str, Any]],
    prices: Mapping[str, Any],
    *,
    horizon_days: int = 5,
) -> dict[str, Any]:
    """Join outcomes to predictions and report what they were worth.

    ``prices`` is ``{ticker: Series indexed by date}``.

    Reports the count alongside every figure, and refuses to summarise
    fewer than twenty scored predictions. A hit rate on nine observations
    is a number that will be quoted and should not exist -- the -5.53%
    that looked like a finding on fifteen ticker-months inverted to +2.37%
    on four hundred and fifty-six.
    """
    import numpy as np
    import pandas as pd

    scored = []
    for row in observations or ():
        ticker = str(row.get("ticker") or "")
        series = prices.get(ticker)
        if series is None or not len(series):
            continue
        try:
            when = pd.Timestamp(row.get("recorded_at")).normalize()
        except (TypeError, ValueError):
            continue

        index = pd.DatetimeIndex(series.index)
        if index.tz is not None:
            index = index.tz_localize(None)
        later = index[index > when.tz_localize(None) if when.tzinfo else index > when]
        if len(later) <= horizon_days:
            continue

        start = index.get_loc(later[0])
        end = min(start + horizon_days, len(series) - 1)
        move = float(series.iloc[end] / series.iloc[start] - 1) * 100

        direction = str(row.get("direction") or "flat")
        if direction == "flat":
            continue
        signed = move if direction == "long" else -move
        scored.append({**row, "move": move, "signed": signed})

    if len(scored) < 20:
        return {
            "scored": len(scored),
            "verdict": "too few to summarise",
            "hit_rate": None,
            "mean_signed": None,
        }

    frame = pd.DataFrame(scored)
    taken = frame[frame.suggested.fillna(False).astype(bool)] if "suggested" in frame else frame

    return {
        "scored": len(frame),
        "suggested": int(len(taken)),
        "hit_rate": float((frame.signed > 0).mean() * 100),
        "mean_signed": float(frame.signed.mean()),
        "hit_rate_suggested": (
            float((taken.signed > 0).mean() * 100) if len(taken) >= 20 else None
        ),
        "mean_signed_suggested": (
            float(taken.signed.mean()) if len(taken) >= 20 else None
        ),
        "horizon_days": horizon_days,
        "verdict": "scored",
    }


def _path(config=None, account: str | None = None) -> Path:
    from cfdinvest.persist.storage import account_root

    return account_root(config, account) / FORWARD_DIR / "observations.jsonl"
