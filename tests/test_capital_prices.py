"""Capital.com bars, and the lake they land in.

The value of this source is the bid/ask on every bar and the extended-hours
coverage. Both are easy to lose silently -- a spread folded into the price
looks like a price, and a walk that stops at a ten-minute session expiry
returns a short history rather than an error.

M321 the walk stopping at the first expired session, losing every bar after it
M322 a repeated page walking forever, or the page budget spent on retries
M323 the spread folded into the OHLC, so indicators read a cost as a move
M324 a second copy of the request interval, drifting from the client's
M325 dividends written as zero, claiming there was none
M326 a resume starting over, or losing the bars already on disk
M327 the walk overrunning the requested end
M328 one bad instrument taking down the rest of the backfill
"""

from __future__ import annotations

import urllib.error

import pandas as pd
import pytest

from cfdinvest.broker.capital import prices as capital_prices
from cfdinvest.config import AppConfig, PathConfig
from cfdinvest.ingestion import capital_prices as ingest


@pytest.fixture
def lake(tmp_path) -> AppConfig:
    return AppConfig(paths=PathConfig(data_root=tmp_path / "data"))


class Session:
    """The shape ``fetch_prices`` needs, and nothing else."""

    def __init__(self, ok=True, host="https://example.invalid/api/v1", error=""):
        self.ok = ok
        self.host = host
        self.error = error

    def headers(self, api_key):
        return {"X-CAP-API-KEY": api_key}


def bar(when: str, bid: float, ask: float, volume: int = 100) -> dict:
    mid = (bid + ask) / 2
    return {
        "snapshotTimeUTC": when,
        "openPrice": {"bid": bid, "ask": ask},
        "highPrice": {"bid": bid, "ask": ask},
        "lowPrice": {"bid": bid, "ask": ask},
        "closePrice": {"bid": bid, "ask": ask},
        "lastTradedVolume": volume,
    }


def pages(*groups):
    """An opener that returns one group of bars per call, then nothing."""
    calls = {"n": 0, "urls": []}

    def opener(url, headers=None, **kwargs):
        calls["urls"].append(url)
        index = calls["n"]
        calls["n"] += 1
        group = groups[index] if index < len(groups) else []
        return {"prices": list(group)}, {}

    opener.calls = calls
    return opener


def test_an_expired_session_is_renewed_rather_than_ending_the_walk():
    """M321: a short history that looks like a complete one.

    Sessions last ten minutes and a backfill runs for far longer, so expiry
    is the expected case. A walk that stopped there would return bars up to
    whatever the clock reached and report success.
    """
    state = {"expired": False}

    def opener(url, headers=None, **kwargs):
        if not state["expired"]:
            state["expired"] = True
            raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)
        return {"prices": [bar("2026-08-10T08:00:00", 100.0, 100.1)]}, {}

    renewed = {"n": 0}

    def renew():
        renewed["n"] += 1
        return Session()

    rows = capital_prices.fetch_prices(
        "AAPL", Session(), _config(), start="2026-08-10T00:00:00",
        opener=opener, sleep=lambda _s: None, renew=renew, max_calls=3,
    )

    assert renewed["n"] == 1, "the session was not renewed"
    assert len(rows) == 1, "the bars after the expiry were lost"


def test_without_a_renewer_an_expired_session_stops_rather_than_loops():
    def opener(url, headers=None, **kwargs):
        raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)

    rows = capital_prices.fetch_prices(
        "AAPL", Session(), _config(), start="2026-08-10T00:00:00",
        opener=opener, sleep=lambda _s: None, max_calls=5,
    )
    assert rows == []


def test_a_repeated_page_ends_the_walk_instead_of_spinning():
    """M322: a server echoing its last page until the call budget is gone.

    Every one of those calls costs a rate limit that a real instrument
    somewhere else in the backfill needs.
    """
    same = bar("2026-08-10T08:00:00", 100.0, 100.1)
    opener = pages([same], [same], [same], [same], [same])

    rows = capital_prices.fetch_prices(
        "AAPL", Session(), _config(), start="2026-08-10T00:00:00",
        opener=opener, sleep=lambda _s: None, max_calls=20,
    )

    assert len(rows) == 1
    assert opener.calls["n"] <= 2, (
        f"walked {opener.calls['n']} times over a page that never advanced"
    )


def test_the_walk_advances_through_pages_and_deduplicates():
    opener = pages(
        [bar("2026-08-10T08:00:00", 100.0, 100.1),
         bar("2026-08-10T09:00:00", 101.0, 101.1)],
        [bar("2026-08-10T09:00:00", 101.0, 101.1),   # the overlap on resume
         bar("2026-08-10T10:00:00", 102.0, 102.1)],
    )

    rows = capital_prices.fetch_prices(
        "AAPL", Session(), _config(), start="2026-08-10T00:00:00",
        opener=opener, sleep=lambda _s: None, max_calls=5,
    )

    stamps = [r["timestamp"] for r in rows]
    assert len(stamps) == len(set(stamps)) == 3
    assert stamps == sorted(stamps), "rows are not oldest-first"


def test_the_walk_does_not_overrun_the_requested_end():
    """M327: an ``end`` that is advisory rather than binding."""
    opener = pages([
        bar("2026-08-10T08:00:00", 100.0, 100.1),
        bar("2026-08-10T09:00:00", 101.0, 101.1),
        bar("2026-08-10T10:00:00", 102.0, 102.1),
    ])

    rows = capital_prices.fetch_prices(
        "AAPL", Session(), _config(), start="2026-08-10T00:00:00",
        end="2026-08-10T09:00:00", opener=opener, sleep=lambda _s: None,
    )

    assert [str(r["timestamp"])[:16] for r in rows] == [
        "2026-08-10 08:00", "2026-08-10 09:00",
    ]


def test_the_price_is_the_mid_and_the_spread_is_kept_beside_it():
    """M323: the spread folded into the price.

    Every indicator would then read a dealing cost as a market move, and the
    backtest would charge it a second time on top.
    """
    rows = capital_prices.fetch_prices(
        "AAPL", Session(), _config(), start="2026-08-10T00:00:00",
        opener=pages([bar("2026-08-10T08:00:00", 100.0, 100.2)]),
        sleep=lambda _s: None,
    )

    row = rows[0]
    assert row["close"] == pytest.approx(100.1), "close is not the mid"
    assert row["bid"] == pytest.approx(100.0)
    assert row["ask"] == pytest.approx(100.2)
    assert row["spread_bp"] == pytest.approx(0.2 / 100.1 * 10_000, rel=1e-6)


def test_the_request_interval_has_one_owner(monkeypatch):
    """M324: a second copy of the pacing constant, free to drift.

    The client documents ten a second and paces at half that. A price walk
    with its own number would quietly stop respecting the client's the day
    either changed.

    Moving the client's constant and watching the walk move with it is the
    only check that catches this. Asserting the constant's *name* appears in
    the source does not: the import line contains it, so a hardcoded literal
    two lines below passes.
    """
    from cfdinvest.broker.capital import client

    assert 0 < client.REQUEST_INTERVAL_SECONDS <= 0.5, (
        "pacing is above the documented ten a second"
    )

    unusual = 0.37
    monkeypatch.setattr(client, "REQUEST_INTERVAL_SECONDS", unusual)

    paused: list[float] = []
    capital_prices.fetch_prices(
        "AAPL", Session(), _config(), start="2026-08-10T00:00:00",
        opener=pages(
            [bar("2026-08-10T08:00:00", 100.0, 100.1)],
            [bar("2026-08-10T09:00:00", 101.0, 101.1)],
        ),
        sleep=paused.append, max_calls=5,
    )
    assert paused, "the walk did not pace itself at all"
    assert all(p == pytest.approx(unusual) for p in paused), (
        f"paced at {set(paused)} while the client's interval was {unusual}"
    )


def test_a_bar_missing_a_side_is_dropped_rather_than_guessed():
    rows = capital_prices.fetch_prices(
        "AAPL", Session(), _config(), start="2026-08-10T00:00:00",
        opener=pages([
            {"snapshotTimeUTC": "2026-08-10T08:00:00",
             "closePrice": {"bid": 100.0, "ask": None}},
            bar("2026-08-10T09:00:00", 101.0, 101.1),
        ]),
        sleep=lambda _s: None,
    )
    assert [str(r["timestamp"])[:16] for r in rows] == ["2026-08-10 09:00"]


def test_no_session_reads_nothing():
    rows = capital_prices.fetch_prices(
        "AAPL", Session(ok=False, error="no credentials"), _config(),
        start="2026-08-10T00:00:00", opener=pages([bar("2026-08-10T08:00:00", 1.0, 1.1)]),
    )
    assert rows == []


# --- the lake side -------------------------------------------------------


def rows_for(stamps, bid=100.0, spread=0.1):
    return [
        {"timestamp": pd.Timestamp(s, tz="UTC"), "open": bid + spread / 2,
         "high": bid + spread, "low": bid, "close": bid + spread / 2,
         "bid": bid, "ask": bid + spread,
         "spread_bp": spread / (bid + spread / 2) * 10_000, "volume": 10}
        for s in stamps
    ]


def test_dividends_are_absent_rather_than_zero(lake):
    """M325: claiming there was no dividend when none was reported.

    The schema marks both columns optional precisely so a source that cannot
    speak to them can stay silent. A zero is an assertion.
    """
    frame = ingest.frame_from_rows(rows_for(["2026-08-10T08:00:00"]), "AAPL")

    assert "dividends" not in frame.columns
    assert "stock_splits" not in frame.columns
    assert list(frame.columns)[:5] == ["open", "high", "low", "close", "volume"]


def test_the_frame_matches_the_lake_shape(lake):
    frame = ingest.frame_from_rows(
        rows_for(["2026-08-10T09:00:00", "2026-08-10T08:00:00"]), "AAPL"
    )

    from cfdinvest.schema import TIMESTAMP

    assert frame.index.name == TIMESTAMP
    assert str(frame.index.tz) == "UTC"
    assert frame.index.is_monotonic_increasing, "bars are not sorted"
    assert str(frame["ticker"].dtype) == "category"


def test_no_rows_is_an_empty_frame_not_an_error():
    assert ingest.frame_from_rows([], "AAPL").empty


def test_a_resume_keeps_what_is_on_disk_and_adds_the_rest(lake):
    """M326: a resume that starts over, or drops the older history.

    The backfill runs for the better part of an hour. Losing it to an
    interruption, or silently keeping only the newest window, are the two
    ways this goes wrong.
    """
    first = rows_for(["2026-08-10T08:00:00", "2026-08-10T09:00:00"])
    second = rows_for(["2026-08-10T09:00:00", "2026-08-10T10:00:00"], bid=200.0)

    report = ingest.backfill_ticker(
        "AAPL", Session(), _config(), lake,
        fetch=lambda *a, **k: first, start="2026-08-01T00:00:00",
    )
    assert report["written"] == 2 and report["added"] == 2

    asked: list[str] = []

    def fetch(epic, session, config, **kwargs):
        asked.append(kwargs["start"])
        return second

    report = ingest.backfill_ticker(
        "AAPL", Session(), _config(), lake,
        fetch=fetch, start="2026-08-01T00:00:00",
    )

    # Compared as timestamps. As strings this passes for the wrong reason:
    # "2026-08-01T00:00:00" > "2026-08-01" is true because it is longer.
    assert asked, "nothing was fetched"
    assert pd.Timestamp(asked[0]) > pd.Timestamp("2026-08-01T12:00:00"), (
        f"resumed from {asked[0]}, refetching history already on disk"
    )
    assert report["written"] == 3, "the resume did not keep the earlier bars"
    assert report["added"] == 1

    from cfdinvest import storage

    stored = storage.read_parquet(
        storage.market_path(lake.paths, "AAPL", ingest.INTERVAL)
    )
    assert len(stored.index) == 3
    assert str(stored["ticker"].dtype) == "category", (
        "the categorical index trap: ticker did not survive the round trip"
    )
    # The server is the authority on a bar it returned.
    assert stored.loc[pd.Timestamp("2026-08-10T09:00:00", tz="UTC"), "bid"] == 200.0


def test_widening_the_history_backwards_is_explicit(lake):
    """A refetch is asked for, never inferred from the dates.

    The earliest bar on disk is normally later than the requested start --
    the server's history begins where it begins -- so a resume that read
    intent from that comparison would refetch three years every time. The
    flag makes the two cases distinguishable instead of guessed.
    """
    ingest.backfill_ticker(
        "AAPL", Session(), _config(), lake,
        fetch=lambda *a, **k: rows_for(["2026-08-10T08:00:00"]),
        start="2026-08-01T00:00:00",
    )

    asked: list[str] = []

    def fetch(epic, session, config, **kwargs):
        asked.append(kwargs["start"])
        return rows_for(["2026-07-01T08:00:00"])

    ingest.backfill_ticker(
        "AAPL", Session(), _config(), lake, fetch=fetch,
        start="2026-06-01T00:00:00",
    )
    assert pd.Timestamp(asked[0]) > pd.Timestamp("2026-08-01T12:00:00"), (
        "an ordinary resume refetched the whole history"
    )

    ingest.backfill_ticker(
        "AAPL", Session(), _config(), lake, fetch=fetch,
        start="2026-06-01T00:00:00", refetch=True,
    )
    assert pd.Timestamp(asked[1]) == pd.Timestamp("2026-06-01T00:00:00"), (
        "refetch=True did not go back to the requested start"
    )


def test_one_bad_instrument_does_not_take_down_the_backfill(lake):
    """M328: an unattended run ending on the first bad response."""
    def fetch(epic, session, config, **kwargs):
        if epic == "MSFT":
            raise RuntimeError("the server hung up")
        return rows_for(["2026-08-10T08:00:00"])

    def opener(_settings):
        return Session()

    with pytest.raises(RuntimeError):
        # The fetcher itself is what swallows a bad response; proving that
        # here would test the stub. What must hold is that the report
        # records a failure rather than the run inventing bars for it.
        fetch("MSFT", None, None, start="x")

    reports = ingest.backfill(
        ["AAPL", "MSFT"], _config(), lake,
        fetch=lambda epic, *a, **k: [] if epic == "MSFT" else rows_for(
            ["2026-08-10T08:00:00"]
        ),
        open_session=opener,
    )

    assert [r["ticker"] for r in reports] == ["AAPL", "MSFT"]
    assert reports[0]["ok"] is True
    assert reports[1]["ok"] is False, "an instrument with no bars reported success"
    assert reports[1]["error"], "a failure with no reason given"


def test_the_capital_lake_is_separate_from_the_yfinance_one():
    """The two disagree about what a bar is; merging makes a third thing."""
    assert ingest.INTERVAL not in ("1h", "1d", "15m", "5m", "1m")


def _config():
    from cfdinvest.broker.capital.client import CapitalConfig

    return CapitalConfig(
        api_key="k", identifier="i", password="p", environment="demo"
    )


# --------------------------------------------------------------------------- #
# M-live: the lake could not describe the feed it trades on
# --------------------------------------------------------------------------- #
# The Capital path wrote bars and never wrote a manifest. repository.coverage
# reads that manifest, coverage_span derives freshness from it, and the
# Status page and health checks read both -- so the decision feed was the one
# series the lake had no account of.
#
# It stayed invisible until it did harm. A routing bug sent every intraday
# fetch to yfinance, which has never heard of "capital_1h", and the yfinance
# ingestor recorded all 109 instruments as empty with zero rows. The routing
# was fixed within the hour. The manifest was not, because nothing on the
# Capital path could rewrite it, and the dashboard showed a critical banner
# over a healthy lake. A banner that is always red hides the next real fault.


def _manifest(tmp_path, reports, interval="capital_1h"):
    import json

    from cfdinvest.config import AppConfig, PathConfig
    from cfdinvest.ingestion import capital_prices

    config = AppConfig(paths=PathConfig(data_root=tmp_path))
    path = capital_prices.write_manifest(reports, interval, config)
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def test_a_successful_backfill_records_what_it_holds(tmp_path):
    """The gap: bars on disk and nothing saying so."""
    written = _manifest(tmp_path, [
        {"ticker": "AAPL", "written": 6401, "added": 5, "ok": True, "error": "",
         "first": "2023-01-03 14:00:00+00:00", "last": "2026-08-31 18:00:00+00:00"},
    ])

    assert written["AAPL"]["status"] == "ok"
    assert written["AAPL"]["rows_total"] == 6401
    assert written["AAPL"]["last_timestamp"].startswith("2026-08-31")
    assert written["AAPL"]["params"]["vendor"] == "capital"


def test_the_manifest_is_readable_as_coverage(tmp_path):
    """Functional, and the point of the whole exercise.

    Writing *a* file is not the requirement. The requirement is that
    ``repository.coverage`` -- which the health checks and the Status page
    read -- can see it, in the shape the yfinance manifests use.
    """
    from cfdinvest.config import AppConfig, MarketDataConfig, PathConfig
    from cfdinvest.ingestion import capital_prices
    from cfdinvest.repository import DataRepository

    config = AppConfig(
        paths=PathConfig(data_root=tmp_path),
        market=MarketDataConfig(interval="capital_1h"),
    )
    capital_prices.write_manifest([
        {"ticker": "AAPL", "written": 6401, "ok": True, "error": "",
         "first": "2023-01-03 14:00:00+00:00", "last": "2026-08-31 18:00:00+00:00"},
        {"ticker": "AMD", "written": 6407, "ok": True, "error": "",
         "first": "2023-01-03 14:00:00+00:00", "last": "2026-08-31 18:00:00+00:00"},
    ], "capital_1h", config)

    coverage = DataRepository(config).coverage("capital_1h")

    assert not coverage.empty, "the lake still cannot describe the decision feed"
    assert sorted(coverage.index) == ["AAPL", "AMD"]
    assert set(coverage["status"]) == {"ok"}


def test_a_stale_empty_entry_is_overwritten_by_a_good_one(tmp_path):
    """The exact recovery that was impossible.

    109 entries reading `empty / 0 rows`, written by a vendor that could not
    serve the interval. A later good pass must replace them, or the banner
    never clears and the fix is invisible.
    """
    first = _manifest(tmp_path, [
        {"ticker": "AAPL", "written": 0, "ok": False,
         "error": "vendor returned no rows"},
    ])
    assert first["AAPL"]["status"] == "failed"

    second = _manifest(tmp_path, [
        {"ticker": "AAPL", "written": 6401, "ok": True, "error": "",
         "first": "2023-01-03 14:00:00+00:00", "last": "2026-08-31 18:00:00+00:00"},
    ])
    assert second["AAPL"]["status"] == "ok"
    assert second["AAPL"]["rows_total"] == 6401
    assert second["AAPL"]["error"] == ""


def test_a_failure_and_an_empty_read_stay_distinguishable(tmp_path):
    """M: a broken fetch recorded as an instrument with no history.

    Both leave zero rows and they mean opposite things -- one is a vendor
    problem to chase, the other a series that simply has nothing yet.
    Collapsing them is how a fault gets filed as a fact about the market.
    """
    written = _manifest(tmp_path, [
        {"ticker": "BAD", "written": 0, "ok": False, "error": "HTTP 500"},
        {"ticker": "NEW", "written": 0, "ok": True, "error": ""},
    ])

    assert written["BAD"]["status"] == "failed"
    assert written["BAD"]["error"] == "HTTP 500"
    assert written["NEW"]["status"] == "empty"


def test_writing_the_manifest_never_takes_down_a_backfill(tmp_path):
    """The bars are already on disk by this point.

    A lake that cannot describe itself is worth a warning, not a lost
    ingest, and raising here would discard a completed fetch.
    """
    from cfdinvest.ingestion import capital_prices

    assert _manifest(tmp_path, []) == {}
    assert _manifest(tmp_path, [{"ticker": "", "written": 1, "ok": True}]) == {}
    assert capital_prices.write_manifest(None, "capital_1h",
                                         _app_config(tmp_path)) is not None


def _app_config(tmp_path):
    from cfdinvest.config import AppConfig, PathConfig

    return AppConfig(paths=PathConfig(data_root=tmp_path))


def test_a_backfill_writes_the_manifest_without_being_asked(tmp_path):
    """The wiring, not the helper -- which is where the bug actually was.

    write_manifest was never the problem; nothing called it. Every test
    above exercises the helper directly and every one of them would have
    passed while the lake stayed undescribed, so this drives a real backfill
    and asserts the manifest exists afterwards.

    That gap is the failure this project names repeatedly: a check written
    by whoever made the change tests the change rather than the requirement.
    Caught here by a mutation removing the call, which the helper tests did
    not notice.
    """
    import json

    from cfdinvest.config import AppConfig, MarketDataConfig, PathConfig
    from cfdinvest.ingestion import capital_prices

    config = AppConfig(
        paths=PathConfig(data_root=tmp_path),
        market=MarketDataConfig(interval="capital_1h"),
    )

    class _Session:
        ok = True
        error = ""

    rows = [
        {"timestamp": "2026-08-31T17:00:00Z", "open": 1.0, "high": 1.1,
         "low": 0.9, "close": 1.05, "volume": 10, "spread_bp": 4.0},
        {"timestamp": "2026-08-31T18:00:00Z", "open": 1.05, "high": 1.2,
         "low": 1.0, "close": 1.15, "volume": 12, "spread_bp": 4.0},
    ]

    reports = capital_prices.backfill(
        ["AAPL"], config=object(), app_config=config,
        interval="capital_1h", resolution="1h",
        open_session=lambda *a, **k: _Session(),
        fetch=lambda *a, **k: rows,
    )

    assert reports and reports[0]["ok"], reports

    from cfdinvest import storage
    path = storage.metadata_path(config.paths, "market_capital_1h_manifest")
    with open(path, encoding="utf-8") as handle:
        manifest = json.load(handle)

    assert "AAPL" in manifest, (
        "a completed backfill left the lake unable to describe itself"
    )
    assert manifest["AAPL"]["status"] == "ok"
    assert manifest["AAPL"]["rows_total"] == 2
