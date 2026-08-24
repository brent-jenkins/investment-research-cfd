"""Tests for yfinance ingestion. A fake downloader keeps everything offline."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from cfdinvest import storage
from cfdinvest.ingestion.market import (
    MarketDataIngestor,
    bucket_by_start,
    chunked,
    normalize_yfinance_frame,
    resolve_start_dates,
    split_yfinance_payload,
    validate_ohlcv,
)
from cfdinvest.schema import TICKER, TIMESTAMP
from tests.conftest import make_yf_frame


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def test_chunked_partitions_without_loss():
    assert chunked(["a", "b", "c", "d", "e"], 2) == [("a", "b"), ("c", "d"), ("e",)]
    assert chunked([], 3) == []


def test_chunked_rejects_bad_size():
    with pytest.raises(ValueError):
        chunked(["a"], 0)


@pytest.mark.parametrize("level_order", ["ticker_first", "field_first"])
def test_split_payload_handles_either_level_order(yf_payload_factory, level_order):
    payload = yf_payload_factory(["AAA", "BBB"], level_order=level_order)

    frames = split_yfinance_payload(payload, ["AAA", "BBB"])

    assert set(frames) == {"AAA", "BBB"}
    assert "Close" in frames["AAA"].columns
    # Different base prices prove the columns were attributed to the right ticker.
    assert frames["AAA"]["Close"].iloc[0] != frames["BBB"]["Close"].iloc[0]


def test_split_payload_handles_flat_single_ticker(sessions):
    frames = split_yfinance_payload(make_yf_frame(sessions), ["AAA"])
    assert set(frames) == {"AAA"}


def test_split_payload_handles_none_and_empty():
    assert split_yfinance_payload(None, ["AAA"]) == {}
    assert split_yfinance_payload(pd.DataFrame(), ["AAA"]) == {}


def test_split_payload_omits_tickers_absent_from_response(yf_payload_factory):
    payload = yf_payload_factory(["AAA"])
    frames = split_yfinance_payload(payload, ["AAA", "MISSING"])
    assert set(frames) == {"AAA"}


def test_normalize_produces_canonical_schema(sessions):
    frame = normalize_yfinance_frame(make_yf_frame(sessions), "AAA")

    assert frame.index.name == TIMESTAMP
    assert str(frame.index.tz) == "UTC"
    assert frame.index.is_monotonic_increasing
    assert list(frame.columns[:5]) == ["open", "high", "low", "close", "volume"]
    assert (frame[TICKER] == "AAA").all()
    assert frame["close"].dtype == np.float64


def test_normalize_converts_tz_aware_index_to_utc(sessions):
    raw = make_yf_frame(sessions)
    raw.index = raw.index.tz_localize("America/New_York")

    frame = normalize_yfinance_frame(raw, "AAA")

    assert str(frame.index.tz) == "UTC"
    assert len(frame) == len(raw)


def test_normalize_drops_bars_with_no_close(sessions):
    raw = make_yf_frame(sessions)
    raw.iloc[2, raw.columns.get_loc("Close")] = np.nan

    frame = normalize_yfinance_frame(raw, "AAA")

    assert len(frame) == len(sessions) - 1


def test_normalize_deduplicates_repeated_timestamps(sessions):
    raw = pd.concat([make_yf_frame(sessions), make_yf_frame(sessions, base=200.0)])

    frame = normalize_yfinance_frame(raw, "AAA")

    assert len(frame) == len(sessions)
    assert frame.index.is_unique
    assert frame["close"].iloc[0] == 200.0  # last occurrence wins


def test_normalize_empty_input_returns_typed_empty_frame():
    frame = normalize_yfinance_frame(pd.DataFrame(), "AAA")
    assert frame.empty
    assert frame.index.name == TIMESTAMP
    assert "close" in frame.columns


def test_normalize_does_not_mutate_input(sessions):
    raw = make_yf_frame(sessions)
    before = raw.copy()
    normalize_yfinance_frame(raw, "AAA")
    pd.testing.assert_frame_equal(raw, before)


def test_validate_flags_inverted_bars_and_bad_prices(sessions):
    frame = normalize_yfinance_frame(make_yf_frame(sessions), "AAA")
    frame.iloc[1, frame.columns.get_loc("high")] = 0.0   # high < low, non-positive
    frame.iloc[3, frame.columns.get_loc("volume")] = -5.0

    flags = validate_ohlcv(frame, "AAA")

    assert flags["high_below_low"] == 1
    assert flags["non_positive_price"] == 1
    assert flags["negative_volume"] == 1


def test_validate_clean_frame_reports_nothing(sessions):
    frame = normalize_yfinance_frame(make_yf_frame(sessions), "AAA")
    assert validate_ohlcv(frame, "AAA") == {}


def test_resolve_start_dates_rewinds_by_overlap():
    starts = resolve_start_dates(
        ["NEW", "OLD"],
        {"OLD": pd.Timestamp("2024-06-10", tz="UTC")},
        default_start="2005-01-01",
        overlap_days=5,
    )
    assert starts == {"NEW": "2005-01-01", "OLD": "2024-06-05"}


def test_bucket_by_start_groups_shared_resume_points():
    buckets = bucket_by_start({"A": "2020-01-01", "B": "2024-01-01", "C": "2020-01-01"})
    assert buckets == {"2020-01-01": ("A", "C"), "2024-01-01": ("B",)}


# --------------------------------------------------------------------------- #
# Ingestor (fake downloader)
# --------------------------------------------------------------------------- #
class FakeDownloader:
    """Records calls and replays a canned yfinance payload."""

    def __init__(self, payload_factory, sessions):
        self._factory = payload_factory
        self._sessions = sessions
        self.calls: list[dict] = []

    def __call__(self, tickers, **kwargs):
        self.calls.append({"tickers": list(tickers), **kwargs})
        return self._factory(list(tickers))


def test_ingest_writes_one_parquet_per_ticker(config, yf_payload_factory, sessions):
    downloader = FakeDownloader(yf_payload_factory, sessions)
    ingestor = MarketDataIngestor(config, downloader=downloader)

    report = ingestor.ingest(["AAA", "BBB"])

    assert [r.status for r in report.results] == ["ok", "ok"]
    assert report.rows_written == 2 * len(sessions)
    for ticker in ("AAA", "BBB"):
        assert storage.market_path(config.paths, ticker, "1d").is_file()


def test_ingest_respects_batch_size(config, yf_payload_factory, sessions):
    downloader = FakeDownloader(yf_payload_factory, sessions)
    ingestor = MarketDataIngestor(config, downloader=downloader)

    ingestor.ingest(["AAA", "BBB", "CCC"])  # batch_size = 2 in the fixture

    assert [len(call["tickers"]) for call in downloader.calls] == [2, 1]


def test_second_run_resumes_from_last_bar(config, yf_payload_factory, sessions):
    downloader = FakeDownloader(yf_payload_factory, sessions)
    ingestor = MarketDataIngestor(config, downloader=downloader)

    ingestor.ingest(["AAA"])
    downloader.calls.clear()
    ingestor.ingest(["AAA"])

    # Last bar is sessions[-1]; overlap is 2 days in the fixture config.
    expected = (sessions[-1] - pd.Timedelta(days=2)).strftime("%Y-%m-%d")
    assert downloader.calls[0]["start"] == expected


def test_full_refresh_ignores_stored_data(config, yf_payload_factory, sessions):
    downloader = FakeDownloader(yf_payload_factory, sessions)
    ingestor = MarketDataIngestor(config, downloader=downloader)

    ingestor.ingest(["AAA"])
    downloader.calls.clear()
    ingestor.ingest(["AAA"], incremental=False)

    assert downloader.calls[0]["start"] == config.market.start


def test_reingest_is_idempotent(config, yf_payload_factory, sessions):
    downloader = FakeDownloader(yf_payload_factory, sessions)
    ingestor = MarketDataIngestor(config, downloader=downloader)

    ingestor.ingest(["AAA"])
    second = ingestor.ingest(["AAA"])

    assert second.results[0].rows_total == len(sessions)


def test_download_failure_is_isolated_not_fatal(config):
    def explode(tickers, **kwargs):
        raise ConnectionError("network down")

    config_no_retry = config.__class__(
        paths=config.paths,
        market=config.market,
        macro=config.macro,
        network=config.network.__class__(max_retries=1),
        storage=config.storage,
        logging=config.logging,
    )
    ingestor = MarketDataIngestor(config_no_retry, downloader=explode)

    report = ingestor.ingest(["AAA"])

    assert report.results[0].status == "failed"
    assert "network down" in report.results[0].error


def test_empty_vendor_response_is_reported_not_raised(config):
    ingestor = MarketDataIngestor(config, downloader=lambda tickers, **kw: pd.DataFrame())

    report = ingestor.ingest(["AAA"])

    assert report.results[0].status == "empty"
    assert report.succeeded == ()


def test_no_tickers_returns_empty_report(config):
    ingestor = MarketDataIngestor(config, downloader=lambda *a, **k: pd.DataFrame())
    report = ingestor.ingest([])
    assert report.results == ()


def test_manifest_records_coverage(config, yf_payload_factory, sessions):
    ingestor = MarketDataIngestor(
        config, downloader=FakeDownloader(yf_payload_factory, sessions)
    )
    ingestor.ingest(["AAA"])

    manifest = storage.read_json(
        storage.metadata_path(config.paths, "market_1d_manifest"), default={}
    )
    assert manifest["AAA"]["status"] == "ok"
    assert manifest["AAA"]["rows_total"] == len(sessions)


def test_available_tickers_lists_the_lake(config, yf_payload_factory, sessions):
    ingestor = MarketDataIngestor(
        config, downloader=FakeDownloader(yf_payload_factory, sessions)
    )
    ingestor.ingest(["AAA", "BBB"])

    assert ingestor.available_tickers("1d") == ("AAA", "BBB")
