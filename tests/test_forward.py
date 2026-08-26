"""Predictions recorded before the answer exists, and scored after.

Mutations checked (each reintroduced, each failed):

M111 the store rewritten rather than appended, so a prediction can be
     revised once the outcome is known
     -> test_a_prediction_cannot_be_revised_after_the_fact
M112 scoring uses the price on the day the prediction was made
     -> test_the_outcome_is_measured_strictly_after_the_prediction
M113 a short's outcome not sign-flipped, so every short scores backwards
     -> test_a_short_is_scored_by_whether_the_price_fell
M114 a summary produced from a handful of observations
     -> test_it_refuses_to_summarise_a_sample_too_small_to_mean_anything
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from cfdinvest.advisor import forward as module


def _prices(values, start="2026-08-03"):
    index = pd.bdate_range(start, periods=len(values))
    return pd.Series(values, index=index)


def test_a_prediction_cannot_be_revised_after_the_fact(tmp_path, monkeypatch):
    """M111: the record rewritten instead of appended.

    The single property that makes a forward record worth more than a
    backtest is that nothing in it can change once the outcome is known.
    A store that rewrites has thrown that away and kept the extra work.
    """
    monkeypatch.setattr(
        module, "_path", lambda config=None, account=None: tmp_path / "obs.jsonl"
    )

    first = [module.Observation(recorded_at="2026-08-03", ticker="AAA",
                                direction="long", confidence=0.7)]
    second = [module.Observation(recorded_at="2026-08-04", ticker="AAA",
                                 direction="short", confidence=0.6)]

    assert module.record_observations(first) == 1
    assert module.record_observations(second) == 1

    rows = module.load_observations()
    assert len(rows) == 2, "the first prediction must survive the second"
    assert rows[0]["direction"] == "long"
    assert rows[1]["direction"] == "short"


def test_the_outcome_is_measured_strictly_after_the_prediction():
    """M112: scored against the price it was made at.

    Using the same day's price scores the prediction against information
    it already had, which is the lookahead that makes any record look
    brilliant.
    """
    # The move happens ON the prediction day and reverses afterwards. If
    # scoring starts from that day the long looks right; starting from the
    # day after -- which is the only honest reading, since the prediction
    # was made with that price already visible -- it looks wrong.
    # Prices are arranged so the two readings disagree in *sign*, which a
    # fixture where both merely fall cannot detect:
    #   from the prediction day (100) to two sessions on (110) -> +10%
    #   from the session after  (130) to two sessions on (110) -> -15%
    # Only the second is honest: the 130 print was already visible when the
    # prediction was made.
    prices = {"AAA": _prices([90, 100, 130, 110, 110, 110, 110])}
    rows = [
        {"recorded_at": "2026-08-04", "ticker": "AAA", "direction": "long",
         "suggested": True}
    ] * 25

    result = module.score_observations(rows, prices, horizon_days=2)
    assert result["verdict"] == "scored"
    # From the session after 2026-08-04: 120 -> 110, so the long was wrong.
    assert result["mean_signed"] < 0, (
        "scoring must start after the prediction, not at the price it saw"
    )


def test_a_short_is_scored_by_whether_the_price_fell():
    """M113: shorts scored with the sign of the move.

    Without the flip every short is scored backwards, which would make a
    losing short side look like the best thing in the book.
    """
    falling = {"AAA": _prices([100, 100, 90, 90, 90, 90, 90])}
    shorts = [
        {"recorded_at": "2026-08-03", "ticker": "AAA", "direction": "short",
         "suggested": True}
    ] * 25
    longs = [
        {"recorded_at": "2026-08-03", "ticker": "AAA", "direction": "long",
         "suggested": True}
    ] * 25

    short_result = module.score_observations(shorts, falling, horizon_days=2)
    long_result = module.score_observations(longs, falling, horizon_days=2)

    assert short_result["mean_signed"] > 0, "a short into a fall is right"
    assert long_result["mean_signed"] < 0, "a long into a fall is wrong"
    assert short_result["mean_signed"] == pytest.approx(-long_result["mean_signed"])


def test_a_flat_prediction_is_not_scored_as_a_trade():
    """Doing nothing is not a call that can be right or wrong."""
    prices = {"AAA": _prices([100, 100, 110, 110, 110, 110, 110])}
    rows = [{"recorded_at": "2026-08-03", "ticker": "AAA", "direction": "flat"}] * 30

    assert module.score_observations(rows, prices)["scored"] == 0


def test_it_refuses_to_summarise_a_sample_too_small_to_mean_anything():
    """M114: a hit rate quoted off a handful of observations.

    Fifteen ticker-months once produced a -5.53% effect that inverted to
    +2.37% on four hundred and fifty-six. A number that small will be
    quoted anyway, so it should not be produced.
    """
    prices = {"AAA": _prices([100, 100, 110, 110, 110, 110, 110])}
    few = [
        {"recorded_at": "2026-08-03", "ticker": "AAA", "direction": "long"}
    ] * 5

    result = module.score_observations(few, prices, horizon_days=2)
    assert result["hit_rate"] is None
    assert "too few" in result["verdict"]
    assert result["scored"] == 5


def test_an_instrument_with_no_price_is_skipped_not_guessed():
    """No outcome yet is not a wrong prediction."""
    prices = {"AAA": _prices([100] * 7)}
    rows = [{"recorded_at": "2026-08-03", "ticker": "ZZZ", "direction": "long"}] * 30

    assert module.score_observations(rows, prices)["scored"] == 0


def test_a_truncated_line_does_not_cost_every_record_before_it(tmp_path, monkeypatch):
    """The file is appended to by a scheduled job; writes can be cut short."""
    path = tmp_path / "obs.jsonl"
    monkeypatch.setattr(module, "_path", lambda config=None, account=None: path)
    path.write_text(
        json.dumps({"recorded_at": "2026-08-03", "ticker": "AAA"}) + "\n"
        + '{"recorded_at": "2026-08-04", "tick\n'
        + json.dumps({"recorded_at": "2026-08-05", "ticker": "CCC"}) + "\n",
        encoding="utf-8",
    )

    rows = module.load_observations()
    assert [r["ticker"] for r in rows] == ["AAA", "CCC"]
