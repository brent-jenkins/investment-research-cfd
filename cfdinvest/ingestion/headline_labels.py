"""Have a language model read each stored headline and say what happened.

WHY THIS EXISTS
---------------
The headline experiment (CLAUDE.md, 2026-09-27) scores stories with a word
list. A word list reads tone, and tone is not news: "Why is Intel up over
33%?" scores positive and describes a move already made, while "Intel cuts
guidance" is an event the market has to price. On 2026-09-28 the scored
stories also turned out to be filed under the wrong company often enough to
matter. A language model reading the title can say which company the story
is about, what kind of event it reports, and whether that event is good or
bad for the company -- which is the question the test actually asks.

WHAT IT MAY AND MAY NOT SEE
---------------------------
The TITLE, the filed ticker and the company name. Nothing else: no price,
no date, no later story. Every story it labels was first seen by the archive
after the model's training data ends (``START``), so it has no hindsight to
leak -- it cannot know what happened next because it has never seen it.

WHAT IT NEVER DOES
------------------
It never edits the headline archive; labels go to their own store with the
model and prompt version on every row. It never invents a label: a batch the
API does not answer, or an item the answer omits, is simply not labelled and
is asked again next hour. It never raises. And it decides nothing about
trades -- the labels are read once, by the pre-registered test, on its date.

It is an experiment with a decision date: futility look 2026-12-28,
decision 2027-03-29. On a null this module is deleted.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Iterable, Mapping

import pandas as pd

logger = logging.getLogger(__name__)

__all__ = [
    "MODEL", "PROMPT_VERSION", "START", "EVENTS", "ABOUT", "IMPACT",
    "make_client", "pending", "label_stories",
]

#: The model, named in full on every label so a later reader knows which
#: judgement it is reading. Haiku on the owner's decision: the task is
#: short-form classification at about five thousand titles a day.
MODEL = "claude-haiku-4-5"

#: Changing the prompt or the categories means a new version, and the new
#: version labels every story again rather than overwriting the old labels.
PROMPT_VERSION = "2026-09-30.1"

#: Only stories the archive first saw on or after this date. The forward
#: record began on 2026-09-27; anything older came from a backfill whose
#: timestamps leaked (CLAUDE.md, 2026-09-27).
START = "2026-09-27"

#: How old a story may be, in days between its publication date and the
#: time the archive first saw it, and still count as news. Publication dates
#: are day-level for most stories, so this is a coarse bound by design.
MAX_AGE_DAYS = 3

EVENTS = (
    "rating_up", "rating_down", "guidance_up", "guidance_down",
    "earnings_beat", "earnings_miss", "deal", "legal_regulatory",
    "product", "management", "price_move", "commentary", "other",
)
ABOUT = ("company", "other", "both")
IMPACT = ("good", "neutral", "bad")

#: Titles per request. Large enough that the instructions are a small share
#: of each call, small enough that one failed call costs little.
BATCH_SIZE = 40

#: A ceiling on one hourly run, so a backlog is worked through over several
#: hours rather than in one long job in the housekeeping unit.
MAX_STORIES_PER_RUN = 2000

#: Published per-million-token prices for the model, used only to log what a
#: run cost. Absent from any decision.
PRICE_IN_PER_MTOK = 1.00
PRICE_OUT_PER_MTOK = 5.00

SYSTEM = """You label financial news headlines for a research record.

Each item gives a stock ticker, the company's name and one headline that a
news search returned for that company. For each item decide three things,
from the headline text alone:

about: "company" if the headline is mainly about the named company; "other"
if it is mainly about a different company or the market (including when the
named company is only the analyst, broker, fund or source commenting on
someone else); "both" if it is genuinely about the named company and another.

event: the single best fit.
  rating_up / rating_down      an analyst upgrade, downgrade or target change
  guidance_up / guidance_down  the company raises or cuts its own outlook
  earnings_beat / earnings_miss  reported results against expectations
  deal            merger, acquisition, contract, partnership, large order
  legal_regulatory  lawsuit, investigation, fine, approval, regulation
  product         launch, recall, trial result, outage
  management      executive change, layoffs, buyback, dividend
  price_move      the headline reports the share price moving and nothing
                  more (it describes the market, not news)
  commentary      opinion, valuation, "should you buy", listicles, previews
  other           anything else

impact: what the EVENT means for the named company: "good", "bad" or
"neutral". Judge the event, not the headline's tone. Use "neutral" for
price_move, commentary and other, and whenever the headline does not say.

Answer for every item, using its id."""


def _schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "labels": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "about": {"type": "string", "enum": list(ABOUT)},
                        "event": {"type": "string", "enum": list(EVENTS)},
                        "impact": {"type": "string", "enum": list(IMPACT)},
                    },
                    "required": ["id", "about", "event", "impact"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["labels"],
        "additionalProperties": False,
    }


def make_client():
    """An API client, or ``None`` when there is no key or no SDK.

    ``None`` is the normal state of a machine nobody has given a key to,
    and it means "label nothing", never "label with defaults".
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
    except ImportError:
        logger.warning("The anthropic package is not installed; headlines not labelled")
        return None
    return anthropic.Anthropic()


def _plain_title(title: str) -> str:
    """The headline without the trailing " - Publisher" the feed appends."""
    text = " ".join(str(title or "").split())
    return text.rsplit(" - ", 1)[0] if " - " in text else text


def pending(
    stories: Iterable[Mapping[str, Any]],
    done: set[str],
    *,
    start: str = START,
) -> list[dict]:
    """Fresh stories first seen on or after ``start`` and not yet labelled.

    FRESH means published no more than ``MAX_AGE_DAYS`` before the archive
    first saw it. The feed resurfaces old stories -- an April earnings beat
    was "first seen" in September -- and an old event is not news for the
    session it reappears in. A story whose publication date cannot be read
    is not labelled: its freshness is unknown, not assumed.
    """
    from cfdinvest.persist.headline_labels import story_key

    out = []
    for story in stories or ():
        seen = str(story.get("first_seen") or "")
        if not seen or seen[:10] < start:
            continue
        try:
            age = (pd.Timestamp(seen[:10]) - pd.Timestamp(str(story.get("published") or "")[:10])).days
        except (TypeError, ValueError):
            continue
        # A day of slack before zero: a story stamped with a US date can be
        # first seen late the same evening UTC, or its date rounded forward.
        if not -1 <= age <= MAX_AGE_DAYS:
            continue
        key = story_key(story)
        if key in done:
            continue
        out.append({**dict(story), "_key": key})
    return out


def _ask(client, items: list[dict]) -> tuple[dict[int, dict], dict]:
    """One request. Returns labels by id, and the token usage."""
    import anthropic

    lines = "\n".join(
        json.dumps({"id": i, "ticker": it["ticker"], "company": it["company"],
                    "headline": it["headline"]}, ensure_ascii=False)
        for i, it in enumerate(items)
    )
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=SYSTEM,
            messages=[{"role": "user", "content": lines}],
            output_config={"format": {"type": "json_schema", "schema": _schema()}},
        )
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as error:
        # A bad key fails every batch the same way; asking forty more times
        # is noise in the log and nothing else.
        logger.warning("Headline labelling was refused the key; stopping: %s", error.message)
        raise
    except anthropic.RateLimitError as error:
        logger.warning("Headline labelling rate-limited; the rest waits an hour: %s", error)
        raise
    except anthropic.APIStatusError as error:
        logger.warning("Headline labelling refused a batch (%s): %s",
                       error.status_code, error.message)
        return {}, {}
    except anthropic.APIConnectionError as error:
        logger.warning("Headline labelling could not reach the API: %s", error)
        raise

    usage = {
        "input": int(getattr(response.usage, "input_tokens", 0) or 0),
        "output": int(getattr(response.usage, "output_tokens", 0) or 0),
    }
    if response.stop_reason != "end_turn":
        # A truncated or refused answer is not a partial label set to trust.
        logger.warning("Headline labelling stopped early (%s); batch not recorded",
                       response.stop_reason)
        return {}, usage
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        answer = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("Headline labelling returned unreadable JSON; batch not recorded")
        return {}, usage
    labels = {}
    for entry in answer.get("labels") or []:
        try:
            index = int(entry["id"])
        except (KeyError, TypeError, ValueError):
            continue
        if (0 <= index < len(items) and entry.get("about") in ABOUT
                and entry.get("event") in EVENTS and entry.get("impact") in IMPACT):
            labels[index] = entry
    return labels, usage


def label_stories(
    stories: list[dict],
    client,
    *,
    names: Mapping[str, str] | None = None,
    now: str,
    limit: int = MAX_STORIES_PER_RUN,
) -> tuple[list[dict], dict]:
    """Label up to ``limit`` stories. Returns label rows and a run summary.

    Identical titles for the same ticker -- the same story from several
    publishers -- are asked about once and the answer recorded for each.
    """
    names = dict(names or {})
    groups: dict[tuple[str, str], list[dict]] = {}
    for story in stories[:limit]:
        headline = _plain_title(story.get("title"))
        if headline:
            groups.setdefault((str(story.get("ticker") or ""), headline), []).append(story)

    unique = [
        {"ticker": ticker, "company": names.get(ticker) or ticker, "headline": headline}
        for ticker, headline in groups
    ]
    rows, usage = [], {"input": 0, "output": 0, "calls": 0, "asked": len(unique), "answered": 0}
    for start in range(0, len(unique), BATCH_SIZE):
        batch = unique[start:start + BATCH_SIZE]
        try:
            labels, used = _ask(client, batch)
        except Exception as error:  # noqa: BLE001 - stop, retry next hour
            # The API errors are logged where they are raised; anything else
            # is a fault in this code and must not stop the run silently.
            if not getattr(error, "status_code", None) and "anthropic" not in type(error).__module__:
                logger.warning("Headline labelling failed: %s: %s", type(error).__name__, error)
            break
        usage["calls"] += 1
        usage["input"] += used.get("input", 0)
        usage["output"] += used.get("output", 0)
        for index, label in labels.items():
            item = batch[index]
            usage["answered"] += 1
            for story in groups[(item["ticker"], item["headline"])]:
                rows.append({
                    "key": story["_key"],
                    "ticker": item["ticker"],
                    "first_seen": story.get("first_seen"),
                    "title": story.get("title"),
                    "model": MODEL,
                    "prompt_version": PROMPT_VERSION,
                    "labelled_at": now,
                    "about": label["about"],
                    "event": label["event"],
                    "impact": label["impact"],
                })
    usage["cost_usd"] = round(
        usage["input"] / 1e6 * PRICE_IN_PER_MTOK + usage["output"] / 1e6 * PRICE_OUT_PER_MTOK, 4
    )
    return rows, usage
