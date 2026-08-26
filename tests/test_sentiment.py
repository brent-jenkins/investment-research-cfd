"""What is being said, and the ways a sentiment count can lie.

Mutations checked (each reintroduced, each failed):

M92  price-report headlines counted rather than excluded
     -> test_a_headline_reporting_the_price_is_not_news
M93  a headline matching both directions resolved by count
     -> test_a_headline_pulling_both_ways_scores_zero
M94  no news reported as neutral rather than absent
     -> test_no_news_is_absent_not_balanced
M95  the excluded count dropped from the description
     -> test_the_description_says_what_it_threw_away
M96  a failing feed raising instead of reporting nothing
     -> test_an_unreadable_feed_reports_nothing_rather_than_raising
M97  no reading rendered as a zero score rather than a dash
     -> test_the_badge_is_absent_rather_than_neutral_when_nothing_was_read
M98  the advisor consulting sentiment when sizing
     -> test_sentiment_reaches_no_calculation
"""

from __future__ import annotations

import pytest

from cfdinvest.advisor import sentiment as module


# The real Lululemon fortnight, read off MarketBeat on 2026-08-26. Kept as
# fixture data because a lexicon tuned on invented headlines is tuned on
# nothing: these are the shapes the feed actually produces.
LULU_FORTNIGHT = [
    "Stock Market Today, Aug. 11: On Holding Stock Plunges on Worst Day Ever",
    "Lululemon slides as weak outlook and lingering turnaround concerns weigh",
    "Gateway Investment Advisers LLC reduces holdings",
    "Lululemon's AI Chief Departs Retailer After Less Than a Year",
    "Lululemon (LULU) Dips More Than Broader Market",
    "lululemon athletica (LULU) Stock Price Up 4.3% - What's Next?",
    "Lululemon Athletica (LULU) Stock Faces Fresh Pressure After Soros Fund Exit",
    "lululemon athletica inc. announces second quarter earnings conference call",
    "lululemon athletica inc. (NASDAQ:LULU) Given Consensus Rating of 'Reduce'",
    "Lululemon (LULU) Increases Despite Market Slip",
    "lululemon athletica (NASDAQ: LULU) Shares Gap Down After Analyst Downgrade",
    "UBS Lowers Lululemon Price Target",
    "Lululemon Athletica Expected to Cut Fiscal 2026 Outlook on Weak US Sales",
]


def test_a_headline_reporting_the_price_is_not_news():
    """M92: the tautology that makes a naive count look predictive.

    "Stock falls Tuesday" correlates with yesterday's return by
    construction and forecasts nothing. Six of the sixteen real Lululemon
    headlines were this. Counting them is measuring the past and calling it
    a signal.
    """
    assert module.is_price_report("Lululemon stock falls Tuesday: what's happening?")
    assert module.is_price_report("Lululemon (LULU) Dips More Than Broader Market")
    assert module.is_price_report("LULU Stock Price Up 4.3% - What's Next?")
    assert module.is_price_report("Shares Gap Down After Analyst Downgrade")
    assert module.is_price_report("Lululemon (LULU) Increases Despite Market Slip")

    # And genuine news is not swept up with it.
    assert not module.is_price_report("UBS Lowers Lululemon Price Target")
    assert not module.is_price_report("Lululemon's AI Chief Departs Retailer")
    assert not module.is_price_report("Given Consensus Rating of 'Reduce'")

    reading = module.summarise_headlines("LULU", LULU_FORTNIGHT)
    assert reading.excluded >= 4, "the price reports must be set aside"
    assert reading.counted == len(LULU_FORTNIGHT) - reading.excluded


def test_the_lexicon_reads_the_real_fortnight_as_negative():
    """The whole point, on data nobody invented."""
    reading = module.summarise_headlines("LULU", LULU_FORTNIGHT)

    assert reading.score is not None
    assert reading.score < -0.15, f"expected negative, got {reading.score}"
    assert reading.negative > reading.positive
    assert reading.label in ("negative", "strongly negative")


def test_a_headline_pulling_both_ways_scores_zero():
    """M93: ambiguity resolved by counting terms.

    "Beats expectations but cuts outlook" is genuinely ambiguous, and a
    lexicon that picks a winner on term count is inventing a reading it
    does not have.
    """
    score, matched = module.score_headline(
        "Acme beats expectations but warns on weak demand"
    )
    assert score == 0
    assert matched, "the terms that cancelled must still be reported"

    assert module.score_headline("Acme upgraded to buy rating")[0] == 1
    assert module.score_headline("Acme downgraded on weak demand")[0] == -1
    assert module.score_headline("Acme names a new head of logistics")[0] == 0


def test_no_news_is_absent_not_balanced():
    """M94: an empty feed reported as a neutral reading.

    "Nothing was said" and "what was said balanced out" are different
    statements and only one of them is information.
    """
    empty = module.summarise_headlines("AAPL", [])
    assert empty.score is None
    assert empty.label == "no news"
    assert "no news" in empty.describe()

    balanced = module.summarise_headlines(
        "AAPL", ["Acme upgraded to buy", "Acme downgraded on weak demand"]
    )
    assert balanced.score == pytest.approx(0.0)
    assert balanced.label == "mixed"

    # Only price reports is also nothing to read.
    only_noise = module.summarise_headlines("AAPL", ["Acme stock falls Tuesday"])
    assert only_noise.score is None
    assert only_noise.excluded == 1


def test_the_description_says_what_it_threw_away():
    """M95: the excluded count omitted from the reading.

    Eleven to four looks decisive until you learn six of the sixteen were
    the price reporting itself. A filter that hides how much it dropped is
    worse than no filter.
    """
    reading = module.summarise_headlines("LULU", LULU_FORTNIGHT)
    described = reading.describe()

    assert str(reading.excluded) in described
    assert "ignored" in described


def test_an_unreadable_feed_reports_nothing_rather_than_raising():
    """M96: a vendor outage taking the dashboard down with it."""
    def explode(ticker):
        raise RuntimeError("the feed is down")

    reading = module.sentiment_for("AAPL", fetch=explode)
    assert reading.score is None
    assert reading.ticker == "AAPL"


def test_every_headline_says_why_it_scored_what_it_did():
    """A number nobody can interrogate is one nobody should act on."""
    reading = module.summarise_headlines("LULU", LULU_FORTNIGHT)
    for headline in reading.headlines:
        if headline.score != 0 and not headline.price_report:
            assert headline.matched, f"{headline.title} scored with no reason"


# --------------------------------------------------------------------------- #
# It advises, and decides nothing
# --------------------------------------------------------------------------- #
def test_the_badge_is_absent_rather_than_neutral_when_nothing_was_read():
    """M97: no reading rendered as a zero score.

    A dash and a "0.00" say different things, and only one of them is
    honest about having looked.
    """
    from cfdinvest.ui import components

    assert components.sentiment_badge(None) == "\u2014"
    assert components.sentiment_badge(module.Sentiment("AAPL")) == "\u2014"

    read = module.summarise_headlines("AAPL", ["Acme upgraded to buy rating"])
    assert components.sentiment_badge(read) != "\u2014"
    assert "+1.00" in components.sentiment_badge(read)


def test_sentiment_reaches_no_calculation():
    """The line that must not be crossed.

    Sentiment has no history to be judged against, so it may inform a
    person and must not size a position, clear a trade, or move a
    threshold. This asserts the code cannot do so even by accident.
    """
    import inspect

    from cfdinvest.advisor import actions

    # The advisor decides what to suggest and how big. It must not import
    # or consult sentiment at all.
    source = inspect.getsource(actions)
    assert "sentiment" not in source.lower(), (
        "the advisor must not consult sentiment; it decides sizes"
    )

    from cfdinvest.execution import frame as execution_frame

    assert "sentiment" not in inspect.getsource(execution_frame).lower(), (
        "the execution config must not carry a sentiment input"
    )


def test_the_overview_shows_it_beside_a_suggestion_not_inside_one():
    """It is a column and an expander, never an input to the numbers."""
    from pathlib import Path

    page = Path("cfdinvest/ui/views/00_Overview.py").read_text(encoding="utf-8")

    assert "sentiment_readings" in page
    assert "sentiment_badge" in page
    # The stake, quantity and confidence must come from the suggestion.
    for computed in ("suggestion.cash", "suggestion.quantity",
                     "suggestion.confidence"):
        assert computed in page
    # And nothing may multiply a number by a sentiment score.
    assert "* reading.score" not in page
    assert "reading.score *" not in page
