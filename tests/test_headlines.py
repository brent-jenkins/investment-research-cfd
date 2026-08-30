"""The headline archive, which exists only to accumulate.

Sentiment is the one input in this project that has never been *tested*, as
opposed to tested and found wanting. Price, volume, macro, earnings
proximity and filing recency were given to a model together over twenty
months and predicted direction at -0.009 against a permutation floor of
+0.001. Sentiment could not join them because there was no history to fit
against: ``advisor.sentiment`` fetches headlines, scores them, shows a
person the result and discards them, and yfinance serves a rolling
three-day window with no archive.

So this collects, and does nothing else. The forecast layer it was built
alongside was abandoned on 2026-08-30 -- the model scored +0.11 against the
confidence score's +0.25, and was worst exactly where confidence was
weakest. Collection outlived it because it is the only part with a time
cost: every day not collecting is a day added to the earliest date
sentiment could be tested.
"""

from __future__ import annotations



# --------------------------------------------------------------------------- #
# Keeping the headlines, because the vendor does not
# --------------------------------------------------------------------------- #
def test_headlines_are_archived_because_the_feed_has_no_history(tmp_path, monkeypatch):
    """M-live: every headline ever scored was discarded.

    ``advisor.sentiment`` fetches, scores, displays and throws away, and
    yfinance serves a rolling three-day window with no archive. So sentiment
    is the only input in the project that has never been *tested* -- as
    opposed to tested and found wanting -- and it could not join the model
    that ruled out price, volume, macro and events because there was no
    history to fit against.
    """
    from cfdinvest.persist import headlines as store

    path = tmp_path / "headlines" / "stories.jsonl"
    monkeypatch.setattr(store, "_path", lambda config=None, account=None: path)

    first = [
        {"ticker": "AMD", "title": "AMD wins a contract", "published": "2026-08-30"},
        {"ticker": "AMD", "title": "AMD stock falls Tuesday", "published": "2026-08-30",
         "price_report": True},
    ]
    assert store.record_headlines(first) == 2

    # The feed serves a rolling window, so consecutive polls overlap almost
    # entirely. Without de-duplication the archive is mostly repeats and any
    # count taken from it measures polling rather than news flow.
    again = first + [
        {"ticker": "AMD", "title": "A third story", "published": "2026-08-31"},
    ]
    assert store.record_headlines(again) == 1, "the rolling window was re-archived"

    held = store.load_headlines()
    assert len(held) == 3
    assert sum(1 for r in held if r.get("price_report")) == 1


def test_the_archive_reports_its_own_coverage_rather_than_judging_it():
    """"Enough history to model on" is the caller's decision; this supplies
    the numbers to make it."""
    from cfdinvest.persist import headlines as store

    empty = store.coverage.__doc__
    assert "refuse to model on too little" in empty


def test_scores_are_not_frozen_into_the_record():
    """The title is the observation; scoring it is an interpretation. Word
    lists get tuned, and a stored score would be a number computed by a rule
    nobody can reconstruct."""
    import inspect

    from cfdinvest.schedule import app

    source = inspect.getsource(app._command_record_headlines)
    assert "score_headline" not in source and "emotional_heat" not in source, (
        "a sentiment score is being written into the archive; recompute it "
        "from the record instead"
    )
    # is_price_report is the exception: a property of the headline, not a
    # view about it.
    assert "is_price_report" in source


def test_headlines_are_collected_when_the_market_is_shut():
    """News does not stop when the exchange does, and a weekend story is
    exactly the kind that moves a Monday open."""
    import pandas as pd

    from cfdinvest.schedule import daemon

    hour, minute = daemon.HEADLINE_AT_UTC
    history = {
        "ingest-daily": "2026-08-30T03:00:00Z",
        "monitoring-snapshot": "2026-08-30T03:05:00Z",
        "screen-universe": "2026-08-30T03:10:00Z",
        "record-headlines": "2026-08-29T21:30:00Z",
    }
    moment = pd.Timestamp(f"2026-08-30T{hour:02d}:{minute + 1:02d}:00Z")
    assert "record-headlines" in daemon.jobs_due(moment, history, market_open=False)
