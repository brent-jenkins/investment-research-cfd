"""Live FRED validation against the real API.

Excluded from the default run (``-m "not live"`` in ``pytest.ini``) because it
needs a network and a key. Run it deliberately:

    pytest -m live

Or the whole suite including it:

    pytest -m ""

Every test here is scoped to a handful of small requests so the run stays
polite: FRED allows 120 requests/minute and this file issues well under ten.
"""

from __future__ import annotations

import os
import time
from dataclasses import replace

import pandas as pd
import pytest
import requests

from cfdinvest.config import (
    AppConfig,
    MacroDataConfig,
    NetworkConfig,
    PathConfig,
    load_dotenv,
)
from cfdinvest.ingestion.macro import (
    FredError,
    MacroDataIngestor,
    resolve_vintage_mode,
)
from cfdinvest.repository import DataRepository
from cfdinvest.schema import PUBLICATION_DATE

pytestmark = pytest.mark.live


def api_key() -> str | None:
    """Read the key from the environment, seeding from .env if needed."""
    load_dotenv(".env")
    return os.environ.get("FRED_API_KEY")


requires_key = pytest.mark.skipif(
    api_key() is None, reason="FRED_API_KEY not set; skipping live FRED tests"
)


@pytest.fixture
def live_config(tmp_path):
    """Real-session config rooted at a throwaway lake."""

    def _make(**macro_changes) -> AppConfig:
        defaults = {
            "api_key": api_key(),
            "observation_start": "2019-01-01",
            "observation_end": "2020-12-31",
            "min_request_interval": 0.55,
            "max_workers": 2,
        }
        macro = MacroDataConfig(**(defaults | macro_changes))
        return AppConfig(
            paths=PathConfig(data_root=tmp_path / "live"),
            macro=macro,
            network=NetworkConfig(timeout=30.0, max_retries=3),
        )

    return _make


# --------------------------------------------------------------------------- #
# Core round trip
# --------------------------------------------------------------------------- #
@requires_key
def test_live_ingest_round_trip(live_config):
    config = live_config(series_ids=("DGS10",))
    with MacroDataIngestor(config) as ingestor:
        report = ingestor.ingest(["DGS10"])

    assert report.results[0].status == "ok"
    assert report.results[0].rows_total > 400

    series = DataRepository(config).load_macro(["DGS10"])["DGS10"]
    assert str(series.index.tz) == "UTC"
    assert series.index.is_monotonic_increasing
    assert series.notna().sum() > 400


@requires_key
def test_live_metadata_fetch(live_config):
    config = live_config(series_ids=("DGS10",))
    with MacroDataIngestor(config) as ingestor:
        metadata = ingestor.fetch_series_metadata("DGS10", config.macro)

    assert metadata["id"] == "DGS10"
    assert "Treasury" in metadata["title"]
    assert metadata["units"] == "Percent"
    assert metadata["frequency"] == "Daily"


@requires_key
def test_live_missing_values_are_preserved(live_config):
    """DGS10 reports '.' on US market holidays; those must survive as NaN."""
    config = live_config(series_ids=("DGS10",))
    with MacroDataIngestor(config) as ingestor:
        ingestor.ingest(["DGS10"])

    series = DataRepository(config).load_macro(["DGS10"])["DGS10"]

    # 2020-01-01 and 2020-12-25 are holidays with no print.
    assert pd.isna(series.loc["2020-01-01"])
    assert pd.isna(series.loc["2020-12-25"])
    # And the gap survived the wide pivot, which pivot_table used to drop.
    assert series.isna().sum() > 10
    assert len(series) > series.notna().sum()


@requires_key
def test_live_pivot_keeps_gap_rows_in_the_panel(live_config):
    """Regression: the wide panel must have a row for a NaN observation."""
    config = live_config(series_ids=("DGS10",))
    with MacroDataIngestor(config) as ingestor:
        ingestor.ingest(["DGS10"])

    repo = DataRepository(config)
    stacked = repo.load_macro(["DGS10"], wide=False)
    wide = repo.load_macro(["DGS10"])

    assert len(wide) == len(stacked)
    assert pd.Timestamp("2020-01-01", tz="UTC") in wide.index


# --------------------------------------------------------------------------- #
# Vintage modes against real ALFRED data
# --------------------------------------------------------------------------- #
@requires_key
def test_live_vintage_differs_from_latest(live_config):
    """GDPC1 2019Q4 was 19221.97 in April 2020 and is far higher today."""
    as_of = live_config(
        series_ids=("GDPC1",),
        realtime_start="2020-04-01",
        realtime_end="2020-04-01",
        observation_start="2019-10-01",
        observation_end="2019-12-31",
    )
    latest = replace(
        as_of,
        paths=replace(as_of.paths, data_root=as_of.paths.data_root / "latest"),
        macro=replace(as_of.macro, realtime_start=None, realtime_end=None),
    )

    assert resolve_vintage_mode(as_of.macro) == "vintage"
    assert resolve_vintage_mode(latest.macro) == "latest"

    with MacroDataIngestor(as_of) as ingestor:
        ingestor.ingest(["GDPC1"])
    with MacroDataIngestor(latest) as ingestor:
        ingestor.ingest(["GDPC1"])

    vintage_value = DataRepository(as_of).load_macro(["GDPC1"])["GDPC1"].iloc[0]
    latest_value = DataRepository(latest).load_macro(["GDPC1"])["GDPC1"].iloc[0]

    assert vintage_value == pytest.approx(19221.97, abs=0.01)
    assert latest_value > vintage_value * 1.05      # rebased upward since


@requires_key
def test_live_initial_release_carries_true_publication_dates(live_config):
    """UNRATE first prints arrive roughly five weeks after the month ends."""
    config = live_config(
        series_ids=("UNRATE",),
        vintage_mode="initial_release",
        observation_start="2020-01-01",
        observation_end="2020-06-30",
    )
    with MacroDataIngestor(config) as ingestor:
        report = ingestor.ingest(["UNRATE"])

    assert report.results[0].status == "ok"
    assert report.params["vintage_mode"] == "initial_release"
    assert report.params["point_in_time"] is True

    repo = DataRepository(config)
    stacked = repo.load_macro(["UNRATE"], wide=False)

    # January 2020 unemployment: first published 2020-02-07 at 3.6%.
    january = stacked.loc["2020-01-01"]
    assert january["value"] == pytest.approx(3.6, abs=0.05)
    assert january[PUBLICATION_DATE] == pd.Timestamp("2020-02-07", tz="UTC")

    calendar = repo.publication_calendar(["UNRATE"])["UNRATE"]
    lag = calendar - calendar.index
    assert (lag > pd.Timedelta(days=1)).all()
    assert lag.max() < pd.Timedelta(days=60)


@requires_key
def test_live_initial_release_rejects_high_vintage_daily_series(live_config):
    """DGS10 has >2000 vintages; the error must name the fix, not the vendor's."""
    config = live_config(series_ids=("DGS10",), vintage_mode="initial_release")
    with MacroDataIngestor(config) as ingestor:
        report = ingestor.ingest(["DGS10"])

    assert report.results[0].status == "failed"
    assert "vintage_mode='latest'" in report.results[0].error


@requires_key
def test_live_repository_exposes_the_vintage_mode(live_config):
    config = live_config(
        series_ids=("UNRATE",),
        vintage_mode="initial_release",
        observation_start="2020-01-01",
        observation_end="2020-06-30",
    )
    with MacroDataIngestor(config) as ingestor:
        ingestor.ingest(["UNRATE"])

    assert DataRepository(config).macro_vintage_mode("UNRATE") == "initial_release"


# --------------------------------------------------------------------------- #
# Error handling against the real endpoint
# --------------------------------------------------------------------------- #
@requires_key
def test_live_bad_series_id_fails_without_retrying(live_config):
    """A 400 is permanent: one attempt, not max_retries attempts."""
    config = live_config(series_ids=("NOT_A_REAL_SERIES",))

    attempts = 0
    real_get = requests.Session.get

    def counting_get(self, *args, **kwargs):
        nonlocal attempts
        attempts += 1
        return real_get(self, *args, **kwargs)

    session = requests.Session()
    session.get = counting_get.__get__(session, requests.Session)

    with MacroDataIngestor(config, session=session) as ingestor:
        report = ingestor.ingest(["NOT_A_REAL_SERIES"])

    assert report.results[0].status == "failed"
    assert attempts == 1


@requires_key
def test_live_missing_api_key_is_reported_clearly(live_config):
    config = live_config(series_ids=("DGS10",))
    keyless = replace(config, macro=replace(config.macro, api_key=None))

    with MacroDataIngestor(keyless) as ingestor:
        with pytest.raises(FredError, match="No FRED API key"):
            ingestor.ingest(["DGS10"])


# --------------------------------------------------------------------------- #
# Rate limiting
# --------------------------------------------------------------------------- #
@requires_key
def test_live_rate_limiter_spaces_real_requests(live_config):
    """Four concurrent series must still be spaced by min_request_interval."""
    interval = 0.55
    config = live_config(
        series_ids=("DGS10", "DGS2", "UNRATE", "FEDFUNDS"),
        min_request_interval=interval,
        max_workers=4,
        fetch_metadata=False,
        observation_start="2020-01-01",
        observation_end="2020-03-31",
    )

    started = time.monotonic()
    with MacroDataIngestor(config) as ingestor:
        report = ingestor.ingest(list(config.macro.series_ids))
    elapsed = time.monotonic() - started

    assert len(report.succeeded) == 4
    # Four requests at >=0.55s spacing cannot complete faster than ~1.65s,
    # even though the pool would happily fire them all at once.
    assert elapsed >= interval * 3
