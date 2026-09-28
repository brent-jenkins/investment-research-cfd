"""The client-sentiment record: forward, point-in-time, and never guessed.

Capital serves only the CURRENT share of its clients long and short, so
this record is the only history the reading will ever have. That makes four
things matter: an unanswered market stays absent rather than becoming a
number; one refused identifier cannot lose a batch; every reading carries
the time it was taken; and nothing ever prunes or rewrites it.

Mutations checked:

  M1870 an unanswered market recorded as 0% rather than absent
  M1871 a refused batch dropped, rather than retried one market at a time
  M1872 the recording time missing, or the rows deduplicated
  M1873 the cadence slowed past thirty minutes, or gated on the market
"""
from __future__ import annotations

import urllib.error
import urllib.parse

import pandas as pd
import pytest

from cfdinvest.broker.capital import sentiment
from cfdinvest.persist import client_sentiment as store


class Session:
    ok = True
    host = "https://example.invalid/api/v1"

    def headers(self, api_key):
        return {"X-CAP-API-KEY": api_key}


class Config:
    api_key = "not-a-key"


KNOWN = {"NVDA": (90.16, 9.84), "TSLA": (88.27, 11.73), "AAPL": (91.84, 8.16)}


def broker(refuse_batches_containing=None):
    """Answers known markets, omits unknown ones, can refuse a whole batch."""
    calls = []

    def read(url, headers=None, **kwargs):
        ids = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["marketIds"][0].split(",")
        calls.append(ids)
        if refuse_batches_containing and refuse_batches_containing in ids and len(ids) > 1:
            raise urllib.error.HTTPError(url, 400, "Bad Request", {}, None)
        if refuse_batches_containing in ids:
            raise urllib.error.HTTPError(url, 400, "Bad Request", {}, None)
        return {"clientSentiments": [
            {"marketId": i, "longPositionPercentage": KNOWN[i][0],
             "shortPositionPercentage": KNOWN[i][1]}
            for i in ids if i in KNOWN
        ]}, {}

    read.calls = calls
    return read


def test_a_market_the_broker_does_not_answer_is_absent_not_zero():
    """M1870: 0% long is a confident claim that the crowd is all short."""
    got = sentiment.fetch_client_sentiment(["NVDA", "ZZZZ"], Session(), Config(), opener=broker())

    assert got["NVDA"] == {"long": 90.16, "short": 9.84}
    assert got["ZZZZ"] is None, "an unanswered market was given a value"


def test_one_refused_identifier_does_not_lose_the_batch():
    """M1871: the batch is retried one market at a time."""
    read = broker(refuse_batches_containing="BADX")
    got = sentiment.fetch_client_sentiment(["NVDA", "BADX", "TSLA"], Session(), Config(), opener=read)

    assert got["NVDA"] is not None and got["TSLA"] is not None, (
        "one refused identifier lost the readings for the rest of its batch"
    )
    assert got["BADX"] is None


def test_no_session_reads_nothing_and_says_so():
    class Closed(Session):
        ok = False

    got = sentiment.fetch_client_sentiment(["NVDA"], Closed(), Config(), opener=broker())
    assert got == {"NVDA": None}


def test_batches_stay_within_the_request_size():
    read = broker()
    many = [f"N{i}" for i in range(sentiment.MAX_IDS_PER_REQUEST * 2 + 3)]
    sentiment.fetch_client_sentiment(many, Session(), Config(), opener=read)
    assert max(len(c) for c in read.calls) <= sentiment.MAX_IDS_PER_REQUEST


def test_every_row_carries_its_time_and_nothing_is_deduplicated(tmp_path, monkeypatch):
    """M1872: two identical readings at two moments are two observations."""
    monkeypatch.setattr(store, "_dir", lambda config=None, account=None: tmp_path)
    reading = {"NVDA": {"long": 90.0, "short": 10.0}, "ZZZZ": None}

    monkeypatch.setattr(store, "_now_utc", lambda: "2026-09-28T13:30:00+00:00")
    assert store.record_client_sentiment(reading) == 2
    monkeypatch.setattr(store, "_now_utc", lambda: "2026-09-28T14:00:00+00:00")
    assert store.record_client_sentiment(reading) == 2

    rows = store.load_client_sentiment()
    assert len(rows) == 4
    assert {r["at"] for r in rows} == {"2026-09-28T13:30:00+00:00", "2026-09-28T14:00:00+00:00"}
    absent = [r for r in rows if r["ticker"] == "ZZZZ"]
    assert all(r["long_pct"] is None and r["short_pct"] is None for r in absent), (
        "an unanswered market was written as a number"
    )


def test_the_record_is_recorded_every_thirty_minutes_market_open_or_not():
    """M1873: positions change before the open, and exist only when read."""
    from cfdinvest.schedule import daemon

    last = pd.Timestamp("2026-09-27T10:00:00Z")
    history = {"record-client-sentiment": last.isoformat()}
    interval = pd.Timedelta(minutes=daemon.CLIENT_SENTIMENT_INTERVAL_MINUTES)

    assert daemon.CLIENT_SENTIMENT_INTERVAL_MINUTES <= 30
    for market_open in (False, True):
        assert "record-client-sentiment" in daemon.jobs_due(last + interval, history,
                                                            market_open=market_open)
        assert "record-client-sentiment" not in daemon.jobs_due(last + interval / 2, history,
                                                                market_open=market_open)


def test_the_scheduled_job_records_every_name_it_covers(tmp_path, monkeypatch):
    """The job asks for traded, held, selected and candidate names alike."""
    from cfdinvest.broker.capital import client as capital
    from cfdinvest.schedule import app

    monkeypatch.setattr(store, "_dir", lambda config=None, account=None: tmp_path)
    monkeypatch.setattr(app, "_traded", lambda cfg, account=None: ["NVDA"])
    monkeypatch.setattr(app, "_with_held", lambda names, cfg, account=None: list(names))
    monkeypatch.setattr(app, "_ever_selected", lambda cfg, account=None: ["TSLA"])
    monkeypatch.setattr(app, "_screen_candidates", lambda cfg: ["AAPL", "ZZZZ"])

    class Settings:
        complete = True
        api_key = "not-a-key"

    monkeypatch.setattr(capital.CapitalConfig, "from_env", staticmethod(lambda: Settings()))
    monkeypatch.setattr(capital, "open_session", lambda settings: Session())
    monkeypatch.setattr(capital, "_read", broker())

    class Args:
        account = None

    app._command_record_client_sentiment(Args(), None)
    rows = {r["ticker"]: r for r in store.load_client_sentiment()}
    assert set(rows) == {"NVDA", "TSLA", "AAPL", "ZZZZ"}
    assert rows["NVDA"]["long_pct"] == pytest.approx(90.16)
    assert rows["ZZZZ"]["long_pct"] is None
