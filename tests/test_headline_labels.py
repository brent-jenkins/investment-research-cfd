"""The headline-event labels: read-only, point-in-time, and never invented.

An experiment pre-registered in CLAUDE.md on 2026-09-30, before any label
existed. What matters is that the labels can be trusted when the test reads
them: each is tied to the story it describes, made from the title alone,
recorded with the model and prompt that made it, and absent rather than
guessed when the model did not answer.

Mutations checked:

  M1900 labels matched to stories by POSITION rather than by id
  M1901 a missing key labelling anyway, or failing loudly
  M1902 already-labelled stories sent again under the same prompt
  M1903 stories first seen before the forward record began being sent
  M1904 an invalid or truncated answer recorded rather than dropped
  M1905 anything beyond ticker, company and title sent to the model
  M1906 the cadence slowed past hourly, or gated on the market
  M1907 a resurfaced old story, or an undated one, labelled as news
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pandas as pd
import pytest

from cfdinvest.ingestion import headline_labels as labeller
from cfdinvest.persist import headline_labels as store


class FakeClient:
    """Answers with fixed labels, deliberately in REVERSE order."""

    def __init__(self, answer=None, stop_reason="end_turn"):
        self.calls = []
        self.answer = answer
        self.stop_reason = stop_reason
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        items = [json.loads(line) for line in kwargs["messages"][0]["content"].splitlines()]
        if self.answer is not None:
            labels = self.answer(items)
        else:
            labels = [
                {"id": it["id"],
                 "about": "company",
                 "event": "rating_down" if "cut" in it["headline"].lower() else "commentary",
                 "impact": "bad" if "cut" in it["headline"].lower() else "neutral"}
                for it in items
            ][::-1]
        return SimpleNamespace(
            stop_reason=self.stop_reason,
            content=[SimpleNamespace(type="text", text=json.dumps({"labels": labels}))],
            usage=SimpleNamespace(input_tokens=1000, output_tokens=200),
        )


def story(ticker, title, first_seen="2026-09-29T12:00:00+00:00", published="2026-09-29"):
    return {"ticker": ticker, "title": title, "first_seen": first_seen, "published": published}


def test_a_label_belongs_to_the_story_it_was_asked_about():
    """M1900: the fake answers in reverse; a positional join would swap them."""
    stories = labeller.pending([
        story("NKE", "Bank of America cuts Nike price target - thestreet.com"),
        story("NKE", "Is Nike stock a buy? - Motley Fool"),
    ], set())
    rows, _ = labeller.label_stories(stories, FakeClient(), names={"NKE": "Nike"}, now="t")

    by_title = {r["title"]: r for r in rows}
    assert by_title["Bank of America cuts Nike price target - thestreet.com"]["event"] == "rating_down"
    assert by_title["Is Nike stock a buy? - Motley Fool"]["event"] == "commentary"


def test_every_label_names_the_model_and_prompt_that_made_it():
    stories = labeller.pending([story("NKE", "Nike cuts guidance")], set())
    rows, _ = labeller.label_stories(stories, FakeClient(), names={}, now="2026-09-30T20:00Z")
    assert rows[0]["model"] == labeller.MODEL
    assert rows[0]["prompt_version"] == labeller.PROMPT_VERSION
    assert rows[0]["labelled_at"] == "2026-09-30T20:00Z"
    assert rows[0]["key"] == store.story_key(story("NKE", "Nike cuts guidance"))


def test_without_a_key_nothing_is_labelled_and_nothing_fails(tmp_path, monkeypatch):
    """M1901: no key is the normal state of an unconfigured machine."""
    from cfdinvest.schedule import app

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(store, "_path", lambda config=None, account=None: tmp_path / "labels.jsonl")
    assert labeller.make_client() is None
    assert app._command_label_headlines(SimpleNamespace(account=None), None) == app.EXIT_OK
    assert not (tmp_path / "labels.jsonl").exists()


def test_a_story_is_labelled_once_per_prompt_version():
    """M1902: done is keyed on the prompt, so a new prompt relabels."""
    s = story("NKE", "Nike cuts guidance")
    done = {store.story_key(s)}
    assert labeller.pending([s], done) == []
    assert len(labeller.pending([s], set())) == 1


def test_stories_from_before_the_forward_record_are_never_sent():
    """M1903: older stories came from a backfill whose dates leaked."""
    old = story("NKE", "Nike cuts guidance", first_seen="2026-09-20T12:00:00+00:00",
                published="2026-09-19")  # fresh, so only the start date can exclude it
    unknown = {**story("NKE", "Nike beats"), "first_seen": ""}
    assert labeller.pending([old, unknown], set()) == []


def test_an_invalid_or_truncated_answer_is_absent_not_guessed():
    """M1904: an item the model mislabels or omits is asked again later."""
    stories = labeller.pending([story("NKE", "a"), story("NKE", "b"), story("NKE", "c")], set())

    def bad(items):
        return [
            {"id": 0, "about": "company", "event": "not_an_event", "impact": "good"},
            {"id": 99, "about": "company", "event": "deal", "impact": "good"},
            # id 1 and 2 omitted
        ]

    rows, usage = labeller.label_stories(stories, FakeClient(answer=bad), names={}, now="t")
    assert rows == []
    assert usage["answered"] == 0

    rows, _ = labeller.label_stories(stories, FakeClient(stop_reason="max_tokens"), names={}, now="t")
    assert rows == [], "a truncated answer was recorded"


def test_the_model_sees_the_title_and_nothing_later():
    """M1905: no price, no date, no publication stamp in what is sent."""
    client = FakeClient()
    stories = labeller.pending([story("NKE", "Nike cuts guidance - Reuters")], set())
    labeller.label_stories(stories, client, names={"NKE": "Nike"}, now="t")

    sent = [json.loads(line) for line in client.calls[0]["messages"][0]["content"].splitlines()]
    assert set(sent[0]) == {"id", "ticker", "company", "headline"}
    assert sent[0]["headline"] == "Nike cuts guidance", "the publisher suffix was sent"
    assert client.calls[0]["model"] == labeller.MODEL


def test_the_same_story_from_several_publishers_is_asked_once():
    client = FakeClient()
    stories = labeller.pending([
        story("NKE", "Nike cuts guidance - Reuters", published="2026-09-29"),
        story("NKE", "Nike cuts guidance - Yahoo Finance", published="2026-09-28"),
    ], set())
    rows, usage = labeller.label_stories(stories, client, names={}, now="t")
    assert usage["asked"] == 1 and len(rows) == 2


def test_labelling_runs_hourly_whatever_the_market_is_doing():
    """M1906."""
    from cfdinvest.schedule import daemon

    last = pd.Timestamp("2026-09-30T10:00Z")
    history = {"label-headlines": last.isoformat()}
    step = pd.Timedelta(minutes=daemon.LABEL_INTERVAL_MINUTES)
    assert daemon.LABEL_INTERVAL_MINUTES <= 60
    for market_open in (False, True):
        assert "label-headlines" in daemon.jobs_due(last + step, history, market_open=market_open)
        assert "label-headlines" not in daemon.jobs_due(last + step / 2, history,
                                                        market_open=market_open)


def test_labels_are_appended_and_read_back(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_path", lambda config=None, account=None: tmp_path / "labels.jsonl")
    row = {"key": "k", "prompt_version": labeller.PROMPT_VERSION, "event": "deal"}
    assert store.record_labels([row]) == 1
    assert store.record_labels([{**row, "prompt_version": "later"}]) == 1
    assert len(store.load_labels()) == 2
    assert store.labelled_keys(labeller.PROMPT_VERSION) == {"k"}


def test_a_resurfaced_old_story_is_not_news():
    """M1907: the feed served an April earnings beat as "first seen" in
    September. Only stories published within MAX_AGE_DAYS of first sight are
    labelled, and a story whose date cannot be read is not assumed fresh."""
    seen = "2026-09-29T12:00:00+00:00"
    fresh = story("NOC", "Northrop beats", first_seen=seen, published="2026-09-28")
    old = story("NOC", "Northrop Q1 beat", first_seen=seen, published="2026-04-22")
    undated = story("NOC", "Northrop undated", first_seen=seen, published="")
    kept = labeller.pending([fresh, old, undated], set())
    assert [s["title"] for s in kept] == ["Northrop beats"]
