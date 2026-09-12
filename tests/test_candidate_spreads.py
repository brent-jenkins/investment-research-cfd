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
        # Ingestion has just written, which is what makes the analysis owed:
        # since the jobs moved into separate units the pipeline is gated on
        # the lake having been refreshed rather than on a shared clock.
        "ingest-intraday": "2026-08-28T14:59:00Z",
        "ingest-daily": "2026-08-28T03:00:00Z",
        "monitoring-snapshot": "2026-08-28T03:05:00Z",
        "screen-universe": "2026-08-01T03:10:00Z",
        "record-candidate-spreads": "2026-08-28T16:00:00Z",
    }

    # Mid-session, with a market open: the trading jobs are due and this is not.
    trading = daemon.jobs_due(
        pd.Timestamp("2026-08-28T15:00:00Z"), history, market_open=True
    )
    # `run-intraday` stands in for "the trading cadence is live" here.
    # Dealing used to be the marker, but it left the daemon on 2026-09-11
    # for its own timer -- see tests/test_dealer_is_separate.py. What this
    # test is actually about is the CANDIDATE pass not riding that cadence.
    assert "run-intraday" in trading
    assert "record-candidate-spreads" not in trading, (
        "the candidate pass fired on the trading cadence"
    )

    # Once a day, on its own appointment.
    due = daemon.jobs_due(
        pd.Timestamp("2026-08-29T16:01:00Z"), history, market_open=True
    )
    assert "record-candidate-spreads" in due


def test_the_candidate_spreads_are_read_while_the_exchange_is_open():
    """M-live: forty-one readings taken with the market shut.

    The job used to share the daily ingest's appointment at 03:00 UTC,
    which is 23:00 the previous evening in New York. Every candidate
    reading came back ``session_open=False``: the quote was the
    out-of-hours one, several times the session spread on a name nobody is
    making a market in. The screen filters to core-session hours precisely
    so it does not rank on that, so the job was building a history that its
    only consumer discards.

    The property is the hour, not the frequency -- one reading a day is
    ample for a ninety-day median. It must fall inside
    ``screen.SPREAD_HOURS`` in *both* daylight-saving regimes, since the
    appointment is fixed in UTC and New York moves under it.
    """
    from cfdinvest.schedule import daemon
    from cfdinvest.universe import screen

    hour, minute = daemon.CANDIDATE_SPREAD_AT_UTC
    low, high = screen.SPREAD_HOURS
    for winter_or_summer in ("2026-01-15", "2026-07-15"):
        moment = pd.Timestamp(f"{winter_or_summer}T{hour:02d}:{minute:02d}:00Z")
        local = moment.tz_convert("America/New_York")
        assert low <= local.hour <= high, (
            f"{hour:02d}:{minute:02d} UTC is {local.hour}:00 in New York on "
            f"{winter_or_summer}, outside the core session the screen reads "
            f"({low}-{high}) -- the readings would be discarded"
        )

    # And it must not be the housekeeping appointment, which is the bug.
    assert daemon.CANDIDATE_SPREAD_AT_UTC != daemon.INGEST_AT_UTC


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


def test_the_screen_reads_recorded_spreads_when_the_archive_is_absent(tmp_path, monkeypatch):
    """M-live: a screen reading a store nothing writes.

    ``capital_1h`` bars carry a spread on every bar, and the screen read
    only those. Nothing on a deployment writes them -- they are a research
    archive built from a backfill -- while ``record-candidate-spreads``
    filled a different store beside it. So the cost gate and the profile
    demotion were both dormain in production, reporting "cost unverified"
    perfectly correctly, for want of anything joining the two.
    """
    import pandas as pd

    from cfdinvest.persist import spreads as store
    from cfdinvest.universe import screen

    path = tmp_path / "spreads" / "observations.jsonl"
    monkeypatch.setattr(store, "_path", lambda config=None, account=None: path)

    rows = []
    for day in range(30):
        stamp = pd.Timestamp("2026-06-01T16:00:00Z") + pd.Timedelta(days=day)
        rows.append({"ticker": "MU", "at": str(stamp), "spread_bp": 7.0,
                     "session_open": True, "market_status": True})
        # Shut *inside* core hours -- a holiday. This is the reading the
        # hour filter cannot catch, so it is the one that tests the session
        # guard rather than passing for the wrong reason.
        rows.append({"ticker": "MU", "at": str(stamp + pd.Timedelta(minutes=5)),
                     "spread_bp": 90.0,
                     "session_open": False, "market_status": False})
    store.record_spreads(rows)

    series = screen._spread_series_from_observations(["MU"])

    assert "MU" in series, "recorded observations did not reach the screen"
    assert float(series["MU"].median()) == 7.0, (
        "an out-of-hours quote reached the ranking -- it is several times "
        "the session spread and the screen must not rank on it"
    )


def test_the_lake_is_preferred_but_absence_falls_through(monkeypatch):
    """Two stores, one answer: the archive wins where it exists, and its
    absence is a fall-through rather than an empty result."""
    from cfdinvest.universe import screen

    monkeypatch.setattr(screen, "_spread_series_from_lake",
                        lambda repo, cands: {"MU": "from-lake"})
    monkeypatch.setattr(screen, "_spread_series_from_observations",
                        lambda cands, config=None, account=None: {"MU": "live"})
    assert screen._spread_series(None, ["MU"]) == {"MU": "from-lake"}

    monkeypatch.setattr(screen, "_spread_series_from_lake", lambda repo, cands: {})
    assert screen._spread_series(None, ["MU"]) == {"MU": "live"}


def test_the_candidate_pass_does_not_run_when_the_market_is_shut():
    """M-live: 92 readings taken on a Saturday.

    Moving the job to 16:00 UTC fixed the hour and not the day. On a
    Saturday every hour is out of session, so it went on recording
    out-of-hours quotes -- two days in seven, plus holidays -- which the
    screen filters out on the way in. Correct behaviour downstream, and a
    store filling with rows nothing can use.
    """
    from cfdinvest.schedule import daemon

    hour, minute = daemon.CANDIDATE_SPREAD_AT_UTC
    moment = pd.Timestamp(f"2026-08-31T{hour:02d}:{minute + 1:02d}:00Z")
    history = {
        "ingest-daily": "2026-08-31T03:00:00Z",
        "monitoring-snapshot": "2026-08-31T03:05:00Z",
        "screen-universe": "2026-08-31T03:10:00Z",
        "record-candidate-spreads": "2026-08-28T16:00:00Z",
    }

    assert "record-candidate-spreads" in daemon.jobs_due(
        moment, history, market_open=True
    ), "the job did not run at its appointment with the market open"

    assert "record-candidate-spreads" not in daemon.jobs_due(
        moment, history, market_open=False
    ), "a spread reading was taken with the exchange shut"

    # Unknown is treated as shut, for the same reason trading is: a reading
    # taken on a guess about the session is not a measurement.
    assert "record-candidate-spreads" not in daemon.jobs_due(
        moment, history, market_open=None
    )


def test_minute_bars_are_collected_after_the_close_not_during_it():
    """The opposite gating from the spread job, for the opposite reason.

    A spread must be read while the market is open -- it describes a live
    quote. A minute bar is a completed fact, so fetching mid-session
    collects a partial day the next run must refetch. And it must NOT be
    gated on the market being open, or an after-close job would never run
    at all.
    """
    from cfdinvest.schedule import daemon

    hour, minute = daemon.MINUTE_INGEST_AT_UTC
    history = {
        "ingest-daily": "2026-08-31T03:00:00Z",
        "monitoring-snapshot": "2026-08-31T03:05:00Z",
        "screen-universe": "2026-08-31T03:10:00Z",
        "record-candidate-spreads": "2026-08-31T16:00:00Z",
        "ingest-minute": "2026-08-30T23:00:00Z",
    }
    after_close = pd.Timestamp(f"2026-08-31T{hour:02d}:{minute + 1:02d}:00Z")

    assert "ingest-minute" in daemon.jobs_due(after_close, history, market_open=False), (
        "gated on an open market, so an after-close job could never run"
    )
    # And not on the trading cadence during the session.
    assert "ingest-minute" not in daemon.jobs_due(
        pd.Timestamp("2026-08-31T15:00:00Z"), history, market_open=True
    )
    # Not twice in one evening.
    ran = dict(history, **{"ingest-minute": str(after_close)})
    assert "ingest-minute" not in daemon.jobs_due(
        after_close + pd.Timedelta(minutes=30), ran, market_open=False
    )


def test_the_minute_appointment_is_past_the_close_in_both_dst_regimes():
    """Fixed in UTC while New York moves under it. The US close is 20:00 UTC
    in summer and 21:00 in winter; fetching before either collects a partial
    day."""
    from cfdinvest.schedule import daemon

    hour, minute = daemon.MINUTE_INGEST_AT_UTC
    for day in ("2026-01-15", "2026-07-15"):
        moment = pd.Timestamp(f"{day}T{hour:02d}:{minute:02d}:00Z")
        local = moment.tz_convert("America/New_York")
        assert local.hour >= 16 or local.hour < 4, (
            f"{hour:02d}:{minute:02d} UTC is {local.hour}:00 in New York on "
            f"{day} -- the session has not closed"
        )


def test_minute_collection_covers_departed_names_too():
    """Same rule as the intraday top-up: minute retention is the shortest
    the vendor offers, so a name that left the list still accrues history."""
    import inspect

    from cfdinvest.schedule import app as schedule_app

    source = inspect.getsource(schedule_app._command_ingest_minute)
    assert "_ever_selected(" in source, (
        "minute collection stops the day a name leaves the universe, and "
        "that history is not recoverable later"
    )
    assert "_with_held(" in source
    assert 'resolution="1m"' in source and 'interval="capital_1m"' in source


def test_minute_collection_refuses_without_credentials():
    """It reaches a broker. No credentials must mean it declines rather than
    raising into the daemon loop."""
    import inspect

    from cfdinvest.schedule import app as schedule_app

    source = inspect.getsource(schedule_app._command_ingest_minute)
    assert "complete" in source and "No Capital credentials" in source
