"""Spread readings for the screen's candidates, not only the traded list.

The screen ranks on liquidity, volatility and continuity -- properties of an
instrument, none of which see what it costs to trade. That blind spot is how
INTC stayed in the universe for months: it ranks respectably and costs
28.73bp against a median of 7.14bp, four times everything else.

M384 the candidates never read, so the screen stays blind to cost
M385 a candidate's daily series pooled with a traded name's 15-minute one
M386 the candidate pass running on the trading cadence
M387 traded instruments read twice, under two cadences and one scope
M388 a reading written without saying which list it covers
"""

from __future__ import annotations

import pandas as pd
import pytest

from cfdinvest.persist.spreads import spread_summary


def reading(ticker, bp, scope=None, session_open=True):
    row = {"ticker": ticker, "spread_bp": bp, "session_open": session_open}
    if scope is not None:
        row["scope"] = scope
    return row


def test_the_two_cadences_are_never_pooled():
    """M385: one median describing neither series.

    A traded instrument is read every fifteen minutes and a candidate once a
    day. Pooling them gives a figure dominated by whichever was sampled
    more, which is a statement about the sampling rather than the spread.
    """
    rows = (
        [reading("AAA", 5.0, "traded") for _ in range(40)]
        + [reading("AAA", 40.0, "candidate") for _ in range(3)]
    )

    traded = spread_summary(rows, scope="traded")
    candidate = spread_summary(rows, scope="candidate")

    assert traded["AAA"]["median_bp"] == pytest.approx(5.0)
    assert candidate["AAA"]["median_bp"] == pytest.approx(40.0)
    assert traded["AAA"]["observations"] == 40
    assert candidate["AAA"]["observations"] == 3


def test_a_reading_written_before_scopes_existed_counts_as_traded():
    """M388: history becoming unreadable when a field is added.

    Every row written before 2026-08-28 carries no scope, and what it was is
    not in doubt -- only the traded list was ever read.
    """
    rows = [reading("AAA", 5.0) for _ in range(25)]

    assert spread_summary(rows, scope="traded")["AAA"]["observations"] == 25
    assert spread_summary(rows, scope="candidate") == {}


def test_an_unscoped_query_still_sees_everything():
    """Scoping must be opt-in, so every existing caller is unchanged."""
    rows = [reading("AAA", 5.0, "traded"), reading("BBB", 30.0, "candidate")]
    assert set(spread_summary(rows)) == {"AAA", "BBB"}


def test_a_candidate_median_still_withholds_its_claim_below_the_floor():
    """One reading a day means a month before a claim can be made.

    That is the point of the daily cadence rather than a defect of it: the
    screen runs monthly, so a month of readings is exactly what it needs.
    """
    thin = [reading("AAA", 30.0, "candidate") for _ in range(5)]
    summary = spread_summary(thin, scope="candidate")["AAA"]
    assert summary["median_bp"] == pytest.approx(30.0), "the arithmetic is withheld"
    assert summary["usable"] is False, "a claim was made on five readings"

    thick = [reading("AAA", 30.0, "candidate") for _ in range(25)]
    assert spread_summary(thick, scope="candidate")["AAA"]["usable"] is True


def test_the_candidate_pass_excludes_the_traded_list():
    """M387: reading the traded names twice, under two cadences.

    They already have a fifteen-minute series. Reading them again on the
    daily pass would put both cadences in one file under one scope, which
    is the thing the scope exists to prevent.
    """
    import inspect

    from cfdinvest.schedule import app

    source = inspect.getsource(app._command_record_candidate_spreads)
    assert "t not in traded" in source, (
        "the candidate pass does not exclude the traded instruments"
    )
    assert '"candidate"' in source


def test_the_candidate_pass_is_daily_not_on_the_trading_cadence():
    """M386: forty-nine names every fifteen minutes for a monthly screen."""
    from cfdinvest.schedule import daemon

    history = {
        "poll-live-quotes": "2026-08-28T14:59:00Z",
        "ingest-daily": "2026-08-28T03:00:00Z",
        "monitoring-snapshot": "2026-08-28T03:05:00Z",
        "screen-universe": "2026-08-01T03:10:00Z",
    }

    # Mid-session, with a market open: the trading jobs are due and this is not.
    trading = daemon.jobs_due(
        pd.Timestamp("2026-08-28T15:00:00Z"), history, market_open=True
    )
    assert "trade-capital" in trading
    assert "record-candidate-spreads" not in trading, (
        "the candidate pass fired on the trading cadence"
    )

    # And at the daily appointment it is, after the ingest it depends on.
    daily = daemon.jobs_due(
        pd.Timestamp("2026-08-29T03:01:00Z"), history, market_open=False
    )
    assert "record-candidate-spreads" in daily
    assert daily.index("ingest-daily") < daily.index("record-candidate-spreads")


def test_the_job_is_registered_and_reachable():
    """M384: a job in the list that nothing dispatches.

    `run_daemon_job` is the seam; a name in JOBS with no branch behind it is
    a job that is due every day and never runs.
    """
    import inspect

    from cfdinvest.schedule import app, daemon

    assert "record-candidate-spreads" in daemon.JOBS
    dispatch = inspect.getsource(app.run_daemon_job)
    assert 'elif name == "record-candidate-spreads":' in dispatch
    assert "_command_record_candidate_spreads(" in dispatch
