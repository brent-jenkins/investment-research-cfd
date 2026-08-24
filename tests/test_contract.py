"""The contract every downstream layer may rely on.

Preprocessing, signals, execution and analytics are all written against
:class:`DataRepository`. These tests pin what it guarantees so a later change
to ingestion cannot quietly break that promise.
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from cfdinvest import storage
from cfdinvest.config import MacroDataConfig
from cfdinvest.ingestion.macro import MacroDataIngestor
from cfdinvest.ingestion.market import MarketDataIngestor
from cfdinvest.repository import DataRepository
from cfdinvest.schema import (
    MACRO_VALUE,
    OHLCV_COLUMNS,
    PUBLICATION_DATE,
    SERIES_ID,
    TICKER,
    TIMESTAMP,
)
from tests.conftest import make_yf_frame
from tests.fred_fixtures import FredStub


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def anomalous_payload(sessions: pd.DatetimeIndex) -> pd.DataFrame:
    """Two tickers; CLEAN is well-formed, BROKEN carries real defects."""
    clean = make_yf_frame(sessions, 100.0)

    broken = make_yf_frame(sessions, 200.0)
    # An inverted bar and a close outside its own range -- the exact shape of
    # the live EURUSD=X defect this layer flags but does not repair.
    broken.iloc[1, broken.columns.get_loc("High")] = broken["Low"].iloc[1] - 5.0
    broken.iloc[3, broken.columns.get_loc("Close")] = broken["High"].iloc[3] + 50.0
    broken.iloc[5, broken.columns.get_loc("Volume")] = -1.0

    return pd.concat({"CLEAN": clean, "BROKEN": broken}, axis=1)


@pytest.fixture
def lake(config, sessions):
    """A populated lake: two market tickers (one anomalous) and one series."""
    market = MarketDataIngestor(
        config, downloader=lambda tickers, **kw: anomalous_payload(sessions)
    )
    market.ingest(["CLEAN", "BROKEN"])

    macro_cfg = replace(
        config,
        macro=MacroDataConfig(
            series_ids=("UNRATE",),
            api_key="test-key",
            observation_start="2019-01-01",
            vintage_mode="initial_release",
            min_request_interval=0.0,
        ),
    )
    with MacroDataIngestor(macro_cfg, session=FredStub()) as macro:
        macro.ingest(["UNRATE"])

    return DataRepository(macro_cfg)


# --------------------------------------------------------------------------- #
# Timestamps
# --------------------------------------------------------------------------- #
def test_market_index_is_tz_aware_utc(lake):
    tidy = lake.load_market()
    stamps = tidy.index.get_level_values(TIMESTAMP)

    assert isinstance(stamps, pd.DatetimeIndex)
    assert str(stamps.tz) == "UTC"


def test_panel_index_is_tz_aware_utc(lake):
    assert str(lake.load_panel("close").index.tz) == "UTC"


def test_macro_index_is_tz_aware_utc(lake):
    assert str(lake.load_macro().index.tz) == "UTC"
    assert str(lake.publication_calendar(wide=True).index.tz) == "UTC"


def test_bundle_indexes_share_one_clock(lake):
    bundle = lake.load_bundle()
    market_tz = bundle.market.index.get_level_values(TIMESTAMP).tz
    assert str(market_tz) == str(bundle.macro.index.tz) == "UTC"


# --------------------------------------------------------------------------- #
# Canonical schema
# --------------------------------------------------------------------------- #
def test_market_columns_are_canonical(lake):
    tidy = lake.load_market()

    assert set(OHLCV_COLUMNS) <= set(tidy.columns)
    assert all(name.islower() for name in tidy.columns)
    assert "Adj Close" not in tidy.columns          # vendor spelling never leaks
    assert tidy.index.names == [TIMESTAMP, TICKER]


def test_market_prices_are_float64(lake):
    tidy = lake.load_market()
    for column in OHLCV_COLUMNS:
        assert tidy[column].dtype == np.float64


def test_macro_columns_are_canonical(lake):
    stacked = lake.load_macro(wide=False)
    assert {MACRO_VALUE, SERIES_ID, PUBLICATION_DATE} <= set(stacked.columns)
    assert stacked[MACRO_VALUE].dtype == np.float64


def test_panel_columns_are_tickers(lake):
    panel = lake.load_panel("close")
    assert panel.columns.name == TICKER
    assert set(panel.columns) == {"BROKEN", "CLEAN"}


# --------------------------------------------------------------------------- #
# Deterministic ordering
# --------------------------------------------------------------------------- #
def test_market_ordering_is_deterministic(lake):
    first = lake.load_market()
    second = lake.load_market(["BROKEN", "CLEAN"])   # reversed request order

    pd.testing.assert_frame_equal(first, second)
    assert first.index.is_monotonic_increasing


def test_panel_axes_are_sorted(lake):
    panel = lake.load_panel("close")
    assert panel.index.is_monotonic_increasing
    assert list(panel.columns) == sorted(panel.columns)


def test_macro_panel_axes_are_sorted(config):
    macro_cfg = replace(
        config,
        macro=MacroDataConfig(
            api_key="test-key", observation_start="2019-01-01",
            min_request_interval=0.0,
        ),
    )
    with MacroDataIngestor(macro_cfg, session=FredStub()) as macro:
        macro.ingest(["UNRATE", "DGS10"])

    panel = DataRepository(macro_cfg).load_macro()
    assert list(panel.columns) == sorted(panel.columns)
    assert panel.index.is_monotonic_increasing


def test_repeated_loads_are_byte_identical(lake):
    pd.testing.assert_frame_equal(lake.load_panel("close"), lake.load_panel("close"))


# --------------------------------------------------------------------------- #
# Anomaly flags
# --------------------------------------------------------------------------- #
def test_anomalies_are_flagged_in_metadata(lake):
    flags = lake.anomalies("1d")

    assert flags.loc["BROKEN", "high_below_low"] == 1
    assert flags.loc["BROKEN", "close_outside_range"] >= 1
    assert flags.loc["BROKEN", "negative_volume"] == 1


def test_clean_ticker_reports_zero_anomalies(lake):
    flags = lake.anomalies("1d")
    assert (flags.loc["CLEAN"] == 0).all()


def test_anomalous_bars_are_stored_without_repair(lake, sessions):
    tidy = lake.load_market(["BROKEN"])
    broken = tidy.xs("BROKEN", level=TICKER)

    # The inverted bar is still inverted: ingestion recorded it, never fixed it.
    assert broken["high"].iloc[1] < broken["low"].iloc[1]
    assert broken["close"].iloc[3] > broken["high"].iloc[3]
    assert broken["volume"].iloc[5] == -1.0
    assert len(broken) == len(sessions)          # nothing was dropped either


def test_anomaly_flags_survive_a_second_ingest(config, sessions):
    """Incremental runs must not blank historical flags from the manifest."""
    ingestor = MarketDataIngestor(
        config, downloader=lambda tickers, **kw: anomalous_payload(sessions)
    )
    ingestor.ingest(["BROKEN"])
    ingestor.ingest(["BROKEN"])

    flags = DataRepository(config).anomalies("1d")
    assert flags.loc["BROKEN", "high_below_low"] == 1


def test_anomalies_empty_when_nothing_ingested(config):
    assert DataRepository(config).anomalies("1d").empty


# --------------------------------------------------------------------------- #
# Metadata / provenance
# --------------------------------------------------------------------------- #
def test_market_coverage_reports_status_and_span(lake, sessions):
    coverage = lake.coverage("1d")

    assert set(coverage.index) == {"BROKEN", "CLEAN"}
    assert (coverage["status"] == "ok").all()
    assert (coverage["rows_total"] == len(sessions)).all()


def test_macro_coverage_reports_vintage_mode(lake):
    coverage = lake.macro_coverage()
    assert coverage.loc["UNRATE", "vintage_mode"] == "initial_release"
    assert coverage.loc["UNRATE", "status"] == "ok"


def test_series_metadata_is_persisted(lake, config):
    metadata = storage.read_json(
        storage.metadata_path(config.paths, "fred_UNRATE"), default={}
    )
    assert metadata["title"] == "Unemployment Rate"
    assert metadata["frequency"] == "Monthly"


def test_bundle_metadata_records_the_lake_root(lake, config):
    bundle = lake.load_bundle()
    assert bundle.metadata["data_root"] == str(config.paths.data_root)
    assert bundle.interval == "1d"


def test_discovery_matches_what_loads(lake):
    tickers = lake.available_tickers("1d")
    tidy = lake.load_market()
    assert set(tickers) == set(tidy.index.get_level_values(TICKER).unique())


# --------------------------------------------------------------------------- #
# Multi-exchange alignment through the repository
# --------------------------------------------------------------------------- #
@pytest.fixture
def multi_exchange(config):
    """Two tickers on partially disjoint calendars, as real exchanges are."""
    us_sessions = pd.DatetimeIndex(["2024-07-03", "2024-07-04", "2024-07-05"])
    uk_sessions = pd.DatetimeIndex(["2024-07-03", "2024-07-05", "2024-07-08"])

    payloads = {
        "US": make_yf_frame(us_sessions, 100.0),
        "UK": make_yf_frame(uk_sessions, 200.0),
    }

    def downloader(tickers, **kwargs):
        return pd.concat({t: payloads[t] for t in tickers}, axis=1)

    MarketDataIngestor(config, downloader=downloader).ingest(["US", "UK"])
    return DataRepository(config)


def test_disjoint_calendars_union_into_the_panel(multi_exchange):
    panel = multi_exchange.load_panel("close")

    assert len(panel) == 4                                  # union of both
    assert panel.index.is_monotonic_increasing
    assert pd.isna(panel.loc[pd.Timestamp("2024-07-04", tz="UTC"), "UK"])
    assert pd.isna(panel.loc[pd.Timestamp("2024-07-08", tz="UTC"), "US"])


def test_holiday_gaps_are_not_filled_on_read(multi_exchange):
    """The repository reports absence; filling is preprocessing's decision."""
    panel = multi_exchange.load_panel("close")
    assert panel.isna().sum().sum() == 2


def test_tidy_frame_omits_missing_sessions_entirely(multi_exchange):
    tidy = multi_exchange.load_market()
    uk = tidy.xs("UK", level=TICKER)

    assert len(uk) == 3
    assert pd.Timestamp("2024-07-04", tz="UTC") not in uk.index


def test_date_bounds_apply_per_exchange(multi_exchange):
    panel = multi_exchange.load_panel("close", start="2024-07-05", end="2024-07-08")

    assert len(panel) == 2
    assert panel["US"].notna().sum() == 1
    assert panel["UK"].notna().sum() == 2


def test_strict_mode_is_the_only_way_to_get_an_error(multi_exchange):
    from cfdinvest.repository import DataNotFoundError

    assert len(multi_exchange.load_market(["US", "MISSING"])) == 3
    with pytest.raises(DataNotFoundError):
        multi_exchange.load_market(["MISSING"], strict=True)
