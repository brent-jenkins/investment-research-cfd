"""Keep the headlines, because the vendor does not.

``advisor.sentiment`` fetches headlines, scores them, shows a person the
result and throws them away. yfinance serves a rolling window of roughly ten
stories covering the last three days and keeps no archive, so every headline
this project has ever scored is gone -- and with it any possibility of
checking whether sentiment predicts anything.

That is not a small omission. Sentiment is the only input in the project
that has never been *tested*, as opposed to tested and found wanting. Price,
volume, macro, earnings proximity and filing recency were given to a model
together over twenty months and 320,948 rows, out of sample across 38 folds,
and predicted direction at -0.009 against a permutation floor of +0.001 --
nothing. Sentiment could not join them because there was no history to fit
or score against.

So this stores what is fetched. It does nothing else: no scoring, no
inference, no opinion. In roughly three months there will be a window worth
testing, and the alternative is having this same conversation then with the
same three days of data.

Append-only, like every other observation store here. A headline describes a
moment that does not come back, and a rewrite would silently change what the
record says was known at the time -- which is the whole value of keeping it.

What is deliberately NOT recorded as a judgement
-----------------------------------------------
The scores. ``sentiment.score_headline`` and ``emotional_heat`` may change
as the word lists are tuned, and a stored score would then be a number
computed by a rule nobody can reconstruct. The *title* is the observation;
scoring it is an interpretation, and interpretations are recomputed from the
record rather than frozen into it. The one exception is
``is_price_report``, kept because it is a property of the headline rather
than a view about it -- six of sixteen Lululemon headlines over a fortnight
were reports of the price move itself, and a reader needs to see that
without re-deriving it.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Iterable, Mapping

logger = logging.getLogger(__name__)

__all__ = ["HEADLINE_DIR", "record_headlines", "load_headlines", "coverage"]

#: Where the archive lives, under the account root.
HEADLINE_DIR = "headlines"


def _path(config=None, account: str | None = None) -> Path:
    from cfdinvest.persist.storage import account_root

    return Path(account_root(config, account)) / HEADLINE_DIR / "stories.jsonl"


def record_headlines(
    rows: Iterable[Mapping[str, Any]],
    config=None,
    account: str | None = None,
) -> int:
    """Append stories, skipping ones already held. Returns how many were new.

    De-duplicated on (ticker, title, published), because the feed serves a
    rolling window and consecutive polls overlap almost entirely -- without
    this the archive would be mostly repeats and any count taken from it
    would be a polling artefact rather than a measure of news flow.

    Never raises. A lost headline is not an outage.
    """
    wanted = [dict(row) for row in (rows or ()) if row]
    if not wanted:
        return 0
    try:
        path = _path(config, account)
        path.parent.mkdir(parents=True, exist_ok=True)
        seen = {
            (r.get("ticker"), r.get("title"), r.get("published"))
            for r in load_headlines(config, account)
        }
        written = 0
        with path.open("a", encoding="utf-8") as handle:
            for row in wanted:
                key = (row.get("ticker"), row.get("title"), row.get("published"))
                if key in seen:
                    continue
                seen.add(key)
                handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")
                written += 1
        return written
    except Exception as error:  # noqa: BLE001 - collection is best-effort
        logger.warning("Could not record headlines: %s", error)
        return 0


def load_headlines(config=None, account: str | None = None) -> list[dict]:
    """Every story ever recorded, oldest first. Never raises."""
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
                logger.warning("Skipping a malformed headline in %s", path)
    except OSError as error:
        logger.warning("Headline archive unreadable at %s: %s", path, error)
    return rows


def coverage(config=None, account: str | None = None) -> dict:
    """How much history exists, so a caller can refuse to model on too little.

    Reports rather than judges: "not enough yet" is a decision for whoever
    is asking, and the numbers to make it are the span in days and the
    stories per instrument.
    """
    rows = load_headlines(config, account)
    if not rows:
        return {"stories": 0, "tickers": 0, "first": None, "last": None, "days": 0}
    stamps = sorted(str(r.get("published") or "") for r in rows if r.get("published"))
    tickers = {r.get("ticker") for r in rows if r.get("ticker")}
    days = 0
    if stamps:
        try:
            import pandas as pd

            days = int(
                (pd.Timestamp(stamps[-1]) - pd.Timestamp(stamps[0])).days
            )
        except Exception:  # noqa: BLE001
            days = 0
    return {
        "stories": len(rows),
        "tickers": len(tickers),
        "first": stamps[0] if stamps else None,
        "last": stamps[-1] if stamps else None,
        "days": days,
    }
