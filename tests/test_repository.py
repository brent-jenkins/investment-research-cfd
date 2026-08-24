"""Tests for the unified read API."""

from __future__ import annotations

import pandas as pd
import pytest

from cfdinvest.ingestion.macro import MacroDataIngestor
from cfdinvest.ingestion.market import MarketDataIngestor
from cfdinvest.repository import (
    DataNotFoundError,
    DataRepository,
    slice_range,
    to_panel,
    to_utc,
)
from cfdinvest.schema import TICKER, TIMESTAMP
from tests.test_macro_ingestion import FakeSession
from tests.test_market_ingestion import FakeDownloader


@pytest.fixture
def populated(config, yf_payload_factory, sessions):
    """A lake containing two tickers of daily bars and one FRED series."""
    market = MarketDataIngestor(
        config, downloader=FakeDownloader(yf_payload_factory, sessions)
    )
    market.ingest(["AAA", "BBB"])
    with MacroDataIngestor(config, session=FakeSession()) as macro:
        macro.ingest(["DGS10"])
    return DataRepository(config)


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def test_to_utc_localizes_naive_and_converts_aware():
    assert str(to_utc("2020-01-01").tz) == "UTC"
    assert to_utc(pd.Timestamp("2020-01-01 12:00", tz="America/New_York")).hour == 17
    assert to_utc(None) is None


def test_slice_range_is_inclusive():
    index = pd.date_range("2020-01-01", periods=5, freq="D", tz="UTC", name=TIMESTAMP)
    frame = pd.DataFrame({"x": range(5)}, index=index)

    sliced = slice_range(frame, "2020-01-02", "2020-01-04")

    assert len(sliced) == 3
    assert sliced.index[0] == pd.Timestamp("2020-01-02", tz="UTC")


def test_slice_range_with_open_bounds():
    index = pd.date_range("2020-01-01", periods=5, freq="D", tz="UTC")
    frame = pd.DataFrame({"x": range(5)}, index=index)

    assert len(slice_range(frame, start="2020-01-04")) == 2
    assert len(slice_range(frame, end="2020-01-02")) == 2
    assert len(slice_range(frame)) == 5


def test_to_panel_pivots_and_sorts():
    index = pd.MultiIndex.from_product(
        [pd.date_range("2020-01-02", periods=2, freq="D", tz="UTC"), ["BBB", "AAA"]],
        names=[TIMESTAMP, TICKER],
    )
    tidy = pd.DataFrame({"close": [1.0, 2.0, 3.0, 4.0]}, index=index)

    panel = to_panel(tidy, "close")

    assert list(panel.columns) == ["AAA", "BBB"]
    assert panel.index.is_monotonic_increasing
    assert panel.loc[panel.index[0], "AAA"] == 2.0


def test_to_panel_rejects_unknown_field():
    index = pd.MultiIndex.from_tuples(
        [(pd.Timestamp("2020-01-01", tz="UTC"), "AAA")], names=[TIMESTAMP, TICKER]
    )
    with pytest.raises(KeyError):
        to_panel(pd.DataFrame({"close": [1.0]}, index=index), "vwap")


# --------------------------------------------------------------------------- #
# Repository
# --------------------------------------------------------------------------- #
def test_load_market_returns_tidy_multiindex(populated, sessions):
    tidy = populated.load_market(["AAA", "BBB"])

    assert tidy.index.names == [TIMESTAMP, TICKER]
    assert len(tidy) == 2 * len(sessions)
    assert tidy.index.is_monotonic_increasing
    assert set(tidy.index.get_level_values(TICKER).unique()) == {"AAA", "BBB"}


def test_load_market_defaults_to_the_whole_lake(populated, sessions):
    assert len(populated.load_market()) == 2 * len(sessions)


def test_load_market_respects_date_bounds(populated, sessions):
    bounded = populated.load_market(["AAA"], start=str(sessions[2].date()),
                                    end=str(sessions[4].date()))
    assert len(bounded) == 3


def test_load_market_column_projection(populated):
    tidy = populated.load_market(["AAA"], columns=["close"])
    # ticker is read from Parquet but ends up in the index, not the columns.
    assert set(tidy.columns) == {"close"}
    assert tidy.index.names == [TIMESTAMP, TICKER]


def test_load_market_skips_missing_ticker_by_default(populated, caplog, sessions):
    tidy = populated.load_market(["AAA", "ZZZ"])
    assert len(tidy) == len(sessions)
    assert "ZZZ" in caplog.text


def test_load_market_strict_raises_for_missing_ticker(populated):
    with pytest.raises(DataNotFoundError):
        populated.load_market(["ZZZ"], strict=True)


def test_load_market_empty_lake_returns_typed_frame(config):
    tidy = DataRepository(config).load_market(["AAA"])
    assert tidy.empty
    assert tidy.index.names == [TIMESTAMP, TICKER]


def test_load_panel_is_wide_and_vectorisable(populated, sessions):
    closes = populated.load_panel("close")

    assert list(closes.columns) == ["AAA", "BBB"]
    assert len(closes) == len(sessions)
    # The whole point of the wide shape: one call covers the universe.
    assert closes.pct_change().shape == closes.shape


def test_load_macro_wide_and_long(populated):
    wide = populated.load_macro(["DGS10"])
    assert list(wide.columns) == ["DGS10"]
    assert len(wide) == 4

    long = populated.load_macro(["DGS10"], wide=False)
    assert {"value", "series_id"} <= set(long.columns)


def test_load_macro_preserves_gaps(populated):
    # Raw reads must not forward-fill; that is a preprocessing decision.
    assert populated.load_macro(["DGS10"])["DGS10"].isna().sum() == 1


def test_load_macro_strict_raises(populated):
    with pytest.raises(DataNotFoundError):
        populated.load_macro(["NOPE"], strict=True)


def test_load_bundle_combines_both_sources(populated, sessions):
    bundle = populated.load_bundle()

    assert bundle.interval == "1d"
    assert set(bundle.tickers) == {"AAA", "BBB"}
    assert bundle.series_ids == ("DGS10",)
    assert bundle.panel("close").shape == (len(sessions), 2)


def test_bundle_describe_reports_coverage(populated, sessions):
    described = populated.load_bundle().describe()

    assert list(described.index) == ["AAA", "BBB"]
    assert (described["rows"] == len(sessions)).all()
    assert described.loc["AAA", "last"] == sessions[-1].tz_localize("UTC")


def test_discovery_and_coverage(populated):
    assert populated.available_tickers("1d") == ("AAA", "BBB")
    assert populated.available_series() == ("DGS10",)

    coverage = populated.coverage("1d")
    assert set(coverage.index) == {"AAA", "BBB"}
    assert (coverage["status"] == "ok").all()
