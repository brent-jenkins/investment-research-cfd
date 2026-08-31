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


def prune_headlines(config=None, account: str | None = None,
                    *, days: int | None = None, now=None) -> dict:
    """Drop stories older than the retention window. Returns what it did.

    The archive is append-only in the sense that matters -- no story is ever
    edited, and what a headline said at the time is never rewritten -- but a
    store that only grows is a store that eventually has to be dealt with in
    a hurry. Six months is what the feed will serve and what a model at this
    horizon can use, so beyond that the rows are cost without value.

    Rewritten wholly rather than trimmed in place: a partial write that
    failed halfway would leave a file that parses and is missing an
    arbitrary middle, which is worse than either keeping everything or
    losing everything. Written beside and moved over, so an interrupted
    prune leaves the original intact.

    Never raises. Failing to prune is untidy; failing to *collect* is
    unrecoverable, and only one of those is worth risking.
    """
    import datetime as dt
    import json
    import os

    from cfdinvest.ingestion.news_rss import RETENTION_DAYS

    horizon = int(RETENTION_DAYS if days is None else days)
    moment = now or dt.datetime.now(dt.timezone.utc)
    if isinstance(moment, str):
        moment = dt.datetime.fromisoformat(moment)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    cutoff = moment - dt.timedelta(days=horizon)

    rows = load_headlines(config, account)
    if not rows:
        return {"kept": 0, "dropped": 0, "cutoff": cutoff.isoformat()}

    kept = []
    dropped = 0
    for row in rows:
        published = str(row.get("published") or "")
        try:
            stamp = dt.datetime.fromisoformat(published.replace("Z", "+00:00"))
        except ValueError:
            # Undated rows are KEPT. "I cannot read this date" is not
            # evidence the story is old, and discarding on a parse failure
            # would quietly delete whatever a future format change touches.
            kept.append(row)
            continue
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=dt.timezone.utc)
        if stamp >= cutoff:
            kept.append(row)
        else:
            dropped += 1

    if not dropped:
        return {"kept": len(kept), "dropped": 0, "cutoff": cutoff.isoformat()}

    try:
        path = _path(config, account)
        temporary = path.with_suffix(".jsonl.pruning")
        with temporary.open("w", encoding="utf-8") as handle:
            for row in kept:
                handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")
        os.replace(temporary, path)
    except Exception as error:  # noqa: BLE001 - tidiness is not worth a failure
        logger.warning("Could not prune the headline archive: %s", error)
        return {"kept": len(rows), "dropped": 0, "cutoff": cutoff.isoformat(),
                "error": str(error)}

    logger.info(
        "Headlines pruned: kept %d, dropped %d older than %s",
        len(kept), dropped, cutoff.date(),
    )
    return {"kept": len(kept), "dropped": dropped, "cutoff": cutoff.isoformat()}


def usable_headlines(config=None, account: str | None = None,
                     *, allow: tuple[str, ...] = ()) -> list[dict]:
    """Stories with nothing flagged against them.

    The filter lives at the *read* side deliberately. Collection keeps
    everything the feed said, because the noise rules are a first attempt
    calibrated on one sample and a headline discarded at collection cannot
    be recovered once it leaves the feed's six-month window. Whoever is
    modelling decides what to exclude, and can change their mind without
    losing data.

    ``allow`` re-admits named reasons, so a later study can ask "does the
    listicle actually hurt?" rather than having that answered for it by a
    rule written today.
    """
    permitted = {str(reason) for reason in allow}
    return [
        row for row in load_headlines(config, account)
        if not (set(row.get("noise") or ()) - permitted)
    ]


def reclassify_headlines(config=None, account: str | None = None) -> dict:
    """Recompute the noise flags on every stored story.

    This is why the title is stored and the verdict is not treated as final.
    The first noise rules discarded 87% of Gilead's headlines because
    "GILD" appears in none of them -- the company is called Gilead
    Sciences -- and the same held for four instruments in five, because the
    name table covered 23 of 104. Had collection dropped the flagged rows,
    the fix would have meant re-fetching six months of news and hoping the
    feed still served it. Because they were kept, the fix is arithmetic.

    Rewritten beside and moved over, like the prune, so an interruption
    leaves the original rather than a half-file.

    Never raises: a failed reclassification leaves the old flags, which are
    conservative rather than wrong in the dangerous direction.
    """
    import json
    import os

    from cfdinvest.ingestion.news_rss import classify, company_names

    rows = load_headlines(config, account)
    if not rows:
        return {"stories": 0, "changed": 0}

    # Resolved once per instrument, not once per story: the lookup may reach
    # the vendor, and there are fifty thousand rows.
    names: dict[str, tuple[str, ...]] = {}
    for row in rows:
        ticker = str(row.get("ticker") or "")
        if ticker and ticker not in names:
            names[ticker] = company_names(ticker, config=config, resolve=True)

    changed = 0
    for row in rows:
        ticker = str(row.get("ticker") or "")
        before = list(row.get("noise") or [])
        after = list(classify(str(row.get("title") or ""), ticker,
                              names=names.get(ticker, ())))
        if before != after:
            row["noise"] = after
            changed += 1

    if not changed:
        return {"stories": len(rows), "changed": 0, "instruments": len(names)}

    try:
        path = _path(config, account)
        temporary = path.with_suffix(".jsonl.reclassifying")
        with temporary.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")
        os.replace(temporary, path)
    except Exception as error:  # noqa: BLE001
        logger.warning("Could not reclassify the headline archive: %s", error)
        return {"stories": len(rows), "changed": 0, "error": str(error)}

    logger.info("Headlines reclassified: %d of %d changed", changed, len(rows))
    return {"stories": len(rows), "changed": changed, "instruments": len(names)}
