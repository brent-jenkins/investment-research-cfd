"""Tests for FRED ingestion. A fake requests.Session keeps everything offline."""

from __future__ import annotations

import time

import numpy as np
import pandas as pd
import pytest
import requests

from cfdinvest import storage
from cfdinvest.ingestion.macro import (
    FredError,
    MacroDataIngestor,
    build_observation_params,
    normalize_fred_observations,
    resolve_observation_start,
)
from cfdinvest.schema import MACRO_VALUE, SERIES_ID, TIMESTAMP


OBSERVATIONS = [
    {"date": "2020-01-01", "value": "1.88"},
    {"date": "2020-01-02", "value": "1.91"},
    {"date": "2020-01-03", "value": "."},      # FRED's missing-value sentinel
    {"date": "2020-01-06", "value": "1.81"},
]


# --------------------------------------------------------------------------- #
# Pure transforms
# --------------------------------------------------------------------------- #
def test_normalize_produces_canonical_schema():
    frame = normalize_fred_observations(OBSERVATIONS, "DGS10")

    assert frame.index.name == TIMESTAMP
    assert str(frame.index.tz) == "UTC"
    assert list(frame.columns) == [MACRO_VALUE, SERIES_ID]
    assert frame[MACRO_VALUE].dtype == np.float64
    assert (frame[SERIES_ID] == "DGS10").all()


def test_normalize_keeps_missing_values_as_nan():
    frame = normalize_fred_observations(OBSERVATIONS, "DGS10")

    # The gap is information: forward-filling is the preprocessor's decision.
    assert len(frame) == 4
    assert pd.isna(frame[MACRO_VALUE].iloc[2])
    assert frame[MACRO_VALUE].iloc[0] == pytest.approx(1.88)


def test_normalize_empty_payload():
    frame = normalize_fred_observations([], "DGS10")
    assert frame.empty
    assert frame.index.name == TIMESTAMP


def test_normalize_rejects_unexpected_payload():
    with pytest.raises(FredError):
        normalize_fred_observations([{"foo": "bar"}], "DGS10")


def test_build_params_includes_key_and_range(config):
    params = build_observation_params("DGS10", config.macro)
    assert params["series_id"] == "DGS10"
    assert params["api_key"] == "test-key"
    assert params["file_type"] == "json"
    assert params["observation_start"] == "2020-01-01"
    assert "realtime_start" not in params


def test_build_params_passes_alfred_vintages(config):
    from dataclasses import replace

    # vintage_mode=None so the bounds themselves derive "vintage"; the shared
    # fixture pins "latest", which deliberately sends no realtime params.
    settings = replace(
        config.macro,
        vintage_mode=None,
        realtime_start="2010-01-01",
        realtime_end="2010-01-01",
    )
    params = build_observation_params("DGS10", settings)
    assert params["realtime_start"] == "2010-01-01"
    assert params["realtime_end"] == "2010-01-01"


def test_build_params_honours_explicit_start(config):
    params = build_observation_params("DGS10", config.macro, observation_start="2022-01-01")
    assert params["observation_start"] == "2022-01-01"


def test_resolve_observation_start_rewinds_for_revisions(config):
    from dataclasses import replace

    settings = replace(config.macro, refetch_overlap_days=10)
    start = resolve_observation_start(pd.Timestamp("2024-06-30", tz="UTC"), settings)
    assert start == "2024-06-20"


def test_resolve_observation_start_never_precedes_configured_floor(config):
    start = resolve_observation_start(pd.Timestamp("2020-01-02", tz="UTC"), config.macro)
    assert start == config.macro.observation_start


def test_resolve_observation_start_for_new_series(config):
    assert resolve_observation_start(None, config.macro) == "2020-01-01"


# --------------------------------------------------------------------------- #
# Fake HTTP session
# --------------------------------------------------------------------------- #
class FakeResponse:
    def __init__(self, payload, status_code=200, reason="OK"):
        self._payload = payload
        self.status_code = status_code
        self.reason = reason
        self.text = str(payload)

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    """Routes /series/observations and /series to canned payloads."""

    def __init__(self, observations=OBSERVATIONS, status_code=200, metadata=None):
        self.observations = observations
        self.status_code = status_code
        self.metadata = metadata if metadata is not None else [
            {"id": "DGS10", "title": "10-Year Treasury", "units": "Percent",
             "frequency": "Daily", "last_updated": "2024-01-01"}
        ]
        self.calls: list[dict] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params})
        if self.status_code != 200:
            return FakeResponse({"error_message": "boom"}, self.status_code, "Error")
        if url.endswith("/series/observations"):
            return FakeResponse({"observations": self.observations})
        return FakeResponse({"seriess": self.metadata})

    def close(self):
        pass


# --------------------------------------------------------------------------- #
# Ingestor
# --------------------------------------------------------------------------- #
def test_ingest_writes_parquet_and_metadata(config):
    session = FakeSession()
    with MacroDataIngestor(config, session=session) as ingestor:
        report = ingestor.ingest(["DGS10"])

    assert report.results[0].status == "ok"
    assert report.results[0].rows_total == 4
    assert storage.macro_path(config.paths, "DGS10").is_file()

    metadata = storage.read_json(storage.metadata_path(config.paths, "fred_DGS10"))
    assert metadata["title"] == "10-Year Treasury"


def test_ingest_requires_an_api_key(config):
    from dataclasses import replace

    keyless = replace(config, macro=replace(config.macro, api_key=None))
    with MacroDataIngestor(keyless, session=FakeSession()) as ingestor:
        with pytest.raises(FredError, match="No FRED API key"):
            ingestor.ingest(["DGS10"])


def test_permanent_http_error_is_not_retried(config):
    session = FakeSession(status_code=400)
    with MacroDataIngestor(config, session=session) as ingestor:
        report = ingestor.ingest(["DGS10"])

    assert report.results[0].status == "failed"
    # A 400 (bad series id / bad key) must fail on the first attempt.
    assert len(session.calls) == 1


def test_rate_limited_response_is_retried(config, monkeypatch):
    monkeypatch.setattr("cfdinvest.retry.time.sleep", lambda _: None)
    session = FakeSession(status_code=429)

    with MacroDataIngestor(config, session=session) as ingestor:
        report = ingestor.ingest(["DGS10"])

    assert report.results[0].status == "failed"
    assert len(session.calls) == config.network.max_retries


def test_empty_series_is_reported_not_raised(config):
    with MacroDataIngestor(config, session=FakeSession(observations=[])) as ingestor:
        report = ingestor.ingest(["DGS10"])

    assert report.results[0].status == "empty"


def test_metadata_failure_does_not_fail_the_series(config, monkeypatch):
    monkeypatch.setattr("cfdinvest.retry.time.sleep", lambda _: None)

    class FlakyMetadataSession(FakeSession):
        def get(self, url, params=None, timeout=None):
            if url.endswith("/series"):
                raise requests.RequestException("metadata endpoint down")
            return super().get(url, params, timeout)

    with MacroDataIngestor(config, session=FlakyMetadataSession()) as ingestor:
        report = ingestor.ingest(["DGS10"])

    assert report.results[0].status == "ok"


def test_incremental_run_rewinds_the_start_date(config):
    session = FakeSession()
    with MacroDataIngestor(config, session=session) as ingestor:
        ingestor.ingest(["DGS10"])
        session.calls.clear()
        ingestor.ingest(["DGS10"])

    observation_calls = [c for c in session.calls if c["url"].endswith("/observations")]
    # Last observation is 2020-01-06, overlap 45 days -> clamps to the floor.
    assert observation_calls[0]["params"]["observation_start"] == "2020-01-01"


def test_multiple_series_run_concurrently(config):
    session = FakeSession()
    with MacroDataIngestor(config, session=session) as ingestor:
        report = ingestor.ingest(["DGS10", "DGS2", "UNRATE"])

    assert len(report.succeeded) == 3
    assert set(ingestor.available_series()) == {"DGS10", "DGS2", "UNRATE"}


def test_no_series_returns_empty_report(config):
    with MacroDataIngestor(config, session=FakeSession()) as ingestor:
        assert ingestor.ingest([]).results == ()


# --------------------------------------------------------------------------- #
# Rate limiter (offline, deterministic)
# --------------------------------------------------------------------------- #
def test_rate_limiter_spaces_sequential_acquires():
    from cfdinvest.ingestion.macro import RateLimiter

    interval = 0.05
    limiter = RateLimiter(interval)

    started = time.monotonic()
    for _ in range(4):
        limiter.acquire()
    elapsed = time.monotonic() - started

    # First acquire is free; the next three each wait one interval.
    assert elapsed >= interval * 3


def test_rate_limiter_spaces_concurrent_acquires():
    """The ceiling is per-process, so threads must share the schedule."""
    from concurrent.futures import ThreadPoolExecutor

    from cfdinvest.ingestion.macro import RateLimiter

    interval = 0.05
    limiter = RateLimiter(interval)

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: limiter.acquire(), range(8)))
    elapsed = time.monotonic() - started

    assert elapsed >= interval * 7


def test_zero_interval_disables_the_limiter():
    from cfdinvest.ingestion.macro import RateLimiter

    limiter = RateLimiter(0.0)
    started = time.monotonic()
    for _ in range(50):
        limiter.acquire()
    assert time.monotonic() - started < 0.05
