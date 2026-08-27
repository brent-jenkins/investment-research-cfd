"""Spread readings: appended, tagged by session, and claimed only with a sample.

Spread is 46.6% of modelled cost and was one constant of 10bp for every
instrument in every condition. Measuring it once showed the constant was
roughly double the median -- but a single reading characterises nothing,
because a spread is a property of an instrument *at a moment*:

* 2.8bp to 28.6bp across instruments, a factor of ten
* a median 1.85x wider outside the session, and JNJ 14.9x
* AVGO 5.1 -> 3.2 and TSLA 4.5 -> 5.9 between two in-session readings
  minutes apart

M288 a reading that overwrites rather than appends
M289 in-session and out-of-session readings pooled
M290 a median claimed from too small a sample
M291 an instrument with no readings omitted rather than reported
M292 the funding rate converted on the way in, before its unit is known
M293 the reading taken off the trading cadence
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cfdinvest.config import AppConfig, PathConfig
from cfdinvest.persist import spreads


@pytest.fixture
def lake(tmp_path) -> AppConfig:
    return AppConfig(paths=PathConfig(data_root=tmp_path / "data"))


def reading(ticker="AAPL", bp=5.0, session_open=True, at="2026-08-28T15:00:00Z"):
    return {
        "at": at, "ticker": ticker, "epic": ticker, "spread_bp": bp,
        "session_open": session_open, "funding_long_raw": -0.0215402,
    }


# --------------------------------------------------------------------------- #
# Recording
# --------------------------------------------------------------------------- #
def test_readings_are_appended_never_rewritten(lake):
    """M288: a history that only ever holds the latest reading.

    The whole point is the distribution. A store that overwrote would leave
    exactly the single snapshot this exists to replace.
    """
    assert spreads.record_spreads([reading(bp=4.0)], lake) == 1
    assert spreads.record_spreads([reading(bp=6.0)], lake) == 1

    rows = spreads.load_spreads(lake)
    assert [row["spread_bp"] for row in rows] == [4.0, 6.0]


def test_a_recording_failure_is_not_an_outage(lake, tmp_path, monkeypatch):
    """A lost reading must not stop the run that was taking it.

    The unwritable path is a file with a path *inside* it, which fails the
    same way on every platform. An earlier version used "/nope/x.jsonl" and
    passed on POSIX while quietly creating C:\nope on Windows -- so the test
    asserted a failure it had just caused not to happen.
    """
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        spreads, "_path", lambda config=None, account=None: blocker / "x.jsonl"
    )

    assert spreads.record_spreads([reading()], lake) == 0
    assert blocker.is_file(), "the blocker was replaced rather than refused"


def test_a_truncated_line_loses_one_reading_not_the_history(lake):
    spreads.record_spreads([reading(bp=4.0), reading(bp=6.0)], lake)
    path = spreads._path(lake)
    path.write_text(path.read_text(encoding="utf-8") + "{not json\n", encoding="utf-8")

    assert len(spreads.load_spreads(lake)) == 2


def test_no_readings_at_all_is_an_empty_list_not_an_error(lake):
    assert spreads.load_spreads(lake) == []


# --------------------------------------------------------------------------- #
# Summarising
# --------------------------------------------------------------------------- #
def test_out_of_session_readings_are_not_pooled_with_in_session_ones():
    """M289: a median describing trading nobody does.

    Outside the session the median instrument costs 1.85x more, and JNJ went
    from 5.6bp to 83.3bp. Pooling the two would produce a figure that is not
    the cost of trading at any moment the engine actually trades.
    """
    rows = (
        [reading(bp=5.0) for _ in range(30)]
        + [reading(bp=80.0, session_open=False) for _ in range(30)]
    )
    summary = spreads.spread_summary(rows)

    assert summary["AAPL"]["median_bp"] == pytest.approx(5.0)
    assert summary["AAPL"]["observations"] == 30

    pooled = spreads.spread_summary(rows, in_session_only=False)
    assert pooled["AAPL"]["observations"] == 60
    assert pooled["AAPL"]["median_bp"] > 5.0, "the fixture does not distinguish them"


def test_a_median_is_not_claimed_from_too_few_readings():
    """M290: three readings is arithmetic, not a measurement.

    The figure is still reported -- withholding it invites the same sum done
    worse elsewhere -- but ``usable`` says whether it is a claim about the
    instrument or just what happened three times.
    """
    few = spreads.spread_summary([reading(bp=5.0) for _ in range(3)])
    assert few["AAPL"]["median_bp"] == pytest.approx(5.0)
    assert few["AAPL"]["observations"] == 3
    assert few["AAPL"]["usable"] is False
    assert few["AAPL"]["p90_bp"] is None

    enough = spreads.spread_summary(
        [reading(bp=5.0) for _ in range(spreads.MIN_OBSERVATIONS)]
    )
    assert enough["AAPL"]["usable"] is True


def test_an_instrument_with_no_readings_is_reported_not_omitted():
    """M291: a gap that looks identical to not having asked.

    A caller iterating the summary cannot tell a ticker that was left out
    from one with nothing to say, and only one of those is worth chasing.
    """
    summary = spreads.spread_summary(
        [reading(ticker="AAPL")], tickers=["AAPL", "MSFT"]
    )

    assert set(summary) == {"AAPL", "MSFT"}
    assert summary["MSFT"]["observations"] == 0
    assert summary["MSFT"]["median_bp"] is None
    assert summary["MSFT"]["usable"] is False


def test_an_unusable_reading_is_dropped_rather_than_counted():
    """A zero or missing spread is not a spread of zero."""
    rows = [reading(bp=5.0), reading(bp=0.0), reading(bp=None),
            {"ticker": "AAPL", "session_open": True}]
    summary = spreads.spread_summary(rows)

    assert summary["AAPL"]["observations"] == 1


# --------------------------------------------------------------------------- #
# What the reading carries
# --------------------------------------------------------------------------- #
def test_the_funding_rate_is_recorded_raw():
    """M292: converting before the unit is known.

    The broker returns -0.0215402 with no documented unit: 7.9% a year read
    one way, 786% read the other. Recording a converted figure would bake the
    guess into the history, so the day it is settled against a real charge
    every past reading would have to be recomputed from an assumption that
    turned out wrong.
    """
    source = Path("cfdinvest/schedule/app.py").read_text(encoding="utf-8")
    body = source[source.index("def _command_record_spreads("):]
    body = body[: body.index("\ndef ")]

    assert "funding_long_raw" in body, "the raw rate is not recorded"
    for conversion in ("* 365", "/ 100", "* 100 * 365"):
        assert conversion not in body, (
            f"the reading applies {conversion!r} to a rate whose unit is unknown"
        )


def test_the_reading_is_taken_on_the_trading_cadence():
    """M293: a spread read at a moment the engine never decided on.

    The reading exists to say what a trade would have cost on the bar the
    signal was formed from. Off that cadence it describes a different moment.
    """
    import pandas as pd

    from cfdinvest.schedule import daemon

    history = {
        "poll-live-quotes": "2026-08-28T14:59:00Z",
        "ingest-daily": "2026-08-28T03:00:00Z",
        "monitoring-snapshot": "2026-08-28T03:05:00Z",
        "screen-universe": "2026-08-01T03:10:00Z",
    }
    due = daemon.jobs_due(
        pd.Timestamp("2026-08-28T15:00:00Z"), history, market_open=True
    )
    assert "record-spreads" in due
    assert due.index("run-intraday") < due.index("record-spreads")

    shut = daemon.jobs_due(
        pd.Timestamp("2026-08-28T22:00:00Z"), history, market_open=False
    )
    assert "record-spreads" not in shut, (
        "spreads were read with every market closed, which is a different "
        "measurement from the one this makes"
    )
