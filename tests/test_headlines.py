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


# --------------------------------------------------------------------------- #
# History, and a rolling window
# --------------------------------------------------------------------------- #
def test_windows_cover_the_retention_period_without_gaps():
    """One large query would silently truncate.

    The feed caps at about a hundred items however much is asked for, so a
    single six-month request returns the most recent hundred and drops the
    rest -- which looks exactly like a successful backfill of a quiet
    period. Windows make the truncation per-month and visible.
    """
    import datetime as dt

    from cfdinvest.ingestion.news_rss import RETENTION_DAYS, monthly_windows

    today = dt.date(2026, 8, 31)
    windows = monthly_windows(today=today)
    assert windows, "no windows produced"
    assert windows[0][0] == today - dt.timedelta(days=RETENTION_DAYS)
    assert windows[-1][1] == today
    for (_, end), (start, _) in zip(windows, windows[1:]):
        assert start == end, f"gap between windows at {end}"


def test_the_query_is_recorded_with_every_story():
    """A ticker search can match the wrong thing. Storing the query makes a
    bad search visible as a bad search rather than as noise in a model."""
    import datetime as dt

    from cfdinvest.ingestion import news_rss

    query = news_rss.headline_query("CAT", dt.date(2026, 6, 1), dt.date(2026, 7, 1))
    assert "CAT stock" in query
    assert "after:2026-06-01" in query and "before:2026-07-01" in query


def test_an_undated_story_is_dropped_rather_than_dated_now():
    """A headline whose moment is unknown cannot be aligned to a bar, and
    stamping it "now" would date six-month-old news to today."""
    import io

    from cfdinvest.ingestion import news_rss

    xml = (
        b'<?xml version="1.0"?><rss><channel>'
        b"<item><title>Dated</title>"
        b"<pubDate>Mon, 31 Aug 2026 09:00:00 GMT</pubDate></item>"
        b"<item><title>Undated</title></item>"
        b"</channel></rss>"
    )

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    rows = news_rss.fetch_window("AAA", opener=lambda *a, **k: Response(xml))
    titles = [r["title"] for r in rows]
    assert titles == ["Dated"], f"expected the undated story dropped, got {titles}"


def test_a_failed_window_loses_only_that_window():
    """One bad month must not lose the other five."""
    from cfdinvest.ingestion import news_rss

    def explode(*a, **k):
        raise OSError("feed down")

    assert news_rss.fetch_window("AAA", opener=explode) == []


def test_pruning_keeps_the_window_and_drops_what_is_past_it(tmp_path, monkeypatch):
    """A store that only grows is one that has to be dealt with in a hurry."""
    import datetime as dt

    from cfdinvest.persist import headlines as store

    path = tmp_path / "headlines" / "stories.jsonl"
    monkeypatch.setattr(store, "_path", lambda config=None, account=None: path)

    now = dt.datetime(2026, 8, 31, tzinfo=dt.timezone.utc)
    fresh = (now - dt.timedelta(days=10)).isoformat()
    ancient = (now - dt.timedelta(days=400)).isoformat()
    store.record_headlines([
        {"ticker": "AAA", "title": "recent", "published": fresh},
        {"ticker": "AAA", "title": "ancient", "published": ancient},
    ])

    result = store.prune_headlines(days=183, now=now)
    assert result["dropped"] == 1 and result["kept"] == 1
    assert [r["title"] for r in store.load_headlines()] == ["recent"]


def test_an_unreadable_date_is_kept_not_deleted(tmp_path, monkeypatch):
    """"I cannot read this date" is not evidence the story is old. Dropping
    on a parse failure would quietly delete whatever a format change
    touches."""
    import datetime as dt

    from cfdinvest.persist import headlines as store

    path = tmp_path / "headlines" / "stories.jsonl"
    monkeypatch.setattr(store, "_path", lambda config=None, account=None: path)
    store.record_headlines([{"ticker": "AAA", "title": "odd", "published": "hmm"}])

    result = store.prune_headlines(days=1, now=dt.datetime(2026, 8, 31,
                                                           tzinfo=dt.timezone.utc))
    assert result["dropped"] == 0
    assert [r["title"] for r in store.load_headlines()] == ["odd"]


# --------------------------------------------------------------------------- #
# Noise: flagged at collection, filtered at use
# --------------------------------------------------------------------------- #
def test_boilerplate_is_flagged():
    """Calibrated on 235 real stories: the dominant noise is not clickbait
    but auto-generated quote pages and forum indexes, three aggregators
    producing over half the volume."""
    from cfdinvest.ingestion.news_rss import classify, company_names

    for title in (
        "Advanced Micro Devices Inc (AMD) Stock Price, Quote, News & History",
        "Advanced Micro Devices (AMD) Stock Forum and Discussion",
    ):
        assert "boilerplate" in classify(title, "AMD", names=company_names("AMD"))


def test_the_company_name_rescues_a_real_story():
    """The false positive this lookup exists to prevent.

    22% of a real sample names the company rather than the symbol, so a
    ticker-only rule bins genuine news. Verified against the failure: when
    the instrument table was read under the wrong attribute name the lookup
    returned empty *silently*, and this headline was flagged incidental.
    """
    from cfdinvest.ingestion.news_rss import classify, company_names

    assert company_names("AMD") == ("Advanced Micro Devices",), (
        "the instrument table is not being read; the incidental rule will "
        "flag every story that names the company rather than the symbol"
    )
    title = "Advanced Micro Devices Stock Ran The Roadmap It Had Already Published"
    assert classify(title, "AMD", names=company_names("AMD")) == ()


def test_an_unrelated_story_is_flagged_incidental():
    from cfdinvest.ingestion.news_rss import classify, company_names

    got = classify("RXT Stock Surges 25% Premarket", "AMD",
                   names=company_names("AMD"))
    assert "incidental" in got


def test_a_cross_mention_is_kept():
    """"Nvidia Just Delivered Bad News for AMD" is about AMD. A rule that
    binned comparisons would throw away most sector news."""
    from cfdinvest.ingestion.news_rss import classify, company_names

    assert classify("Nvidia Just Delivered Bad News for AMD and Intel", "AMD",
                    names=company_names("AMD")) == ()


def test_noise_is_filtered_at_the_read_side_not_at_collection(tmp_path, monkeypatch):
    """Collection keeps everything; the reader decides.

    The rules are a first attempt and a headline dropped at collection
    cannot be recovered once it leaves the feed's six-month window, so the
    irreversible step is the one not taken.
    """
    from cfdinvest.persist import headlines as store

    path = tmp_path / "headlines" / "stories.jsonl"
    monkeypatch.setattr(store, "_path", lambda config=None, account=None: path)
    store.record_headlines([
        {"ticker": "AAA", "title": "real news", "published": "2026-08-30", "noise": []},
        {"ticker": "AAA", "title": "quote page", "published": "2026-08-30",
         "noise": ["boilerplate"]},
        {"ticker": "AAA", "title": "round-up", "published": "2026-08-30",
         "noise": ["listicle"]},
    ])

    assert len(store.load_headlines()) == 3, "collection discarded something"
    assert [r["title"] for r in store.usable_headlines()] == ["real news"]
    # And a later study can re-admit a reason rather than having it decided
    # for them by a rule written today.
    titles = {r["title"] for r in store.usable_headlines(allow=("listicle",))}
    assert titles == {"real news", "round-up"}


def test_reclassifying_fixes_flags_without_refetching(tmp_path, monkeypatch):
    """The payoff of flagging rather than deleting.

    The first noise rules discarded 87% of Gilead's headlines because
    "GILD" appears in none of them. Had collection dropped those rows the
    fix would have meant re-fetching six months of news; because the titles
    were kept, it is arithmetic.
    """
    from cfdinvest.ingestion import news_rss
    from cfdinvest.persist import headlines as store

    path = tmp_path / "headlines" / "stories.jsonl"
    monkeypatch.setattr(store, "_path", lambda config=None, account=None: path)
    # Collected when the name was unknown, so wrongly flagged incidental.
    store.record_headlines([
        {"ticker": "GILD", "title": "Gilead Sciences Wins Approval",
         "published": "2026-08-30", "noise": ["incidental"]},
    ])
    assert store.usable_headlines() == [], "precondition: the story is excluded"

    monkeypatch.setattr(
        news_rss, "company_names",
        lambda t, config=None, resolve=False: ("Gilead Sciences",),
    )
    result = store.reclassify_headlines()
    assert result["changed"] == 1
    assert [r["title"] for r in store.usable_headlines()] == [
        "Gilead Sciences Wins Approval"
    ], "the story is still excluded after the name became known"


def test_the_query_searches_the_company_name_not_the_symbol():
    """Searching the symbol looked reasonable and was not.

    Measured across a six-month backfill: "GILD stock" returned gold-mining
    stories -- New Gold, Coeur Mining, "Why Is Gold Getting Cheaper" --
    because the feed fuzzy-matches GILD to gold, and 87% of Gilead's fetched
    headlines were about a different industry. "SHOP stock" returned
    CrowdStrike, Marvell and municipal bin collections.

    Found because the query is stored with every story, which is exactly
    what that field was added for.
    """
    import datetime as dt

    from cfdinvest.ingestion.news_rss import headline_query

    named = headline_query("GILD", dt.date(2026, 6, 1), dt.date(2026, 7, 1),
                           name="Gilead Sciences")
    assert '"Gilead Sciences"' in named, "the company name is not quoted"
    assert "GILD stock" not in named, "still searching the ambiguous symbol"
    assert "after:2026-06-01" in named and "before:2026-07-01" in named

    # No name known: the symbol is the only thing left, and is still better
    # than searching nothing.
    assert headline_query("XYZ") == "XYZ stock"
