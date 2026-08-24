"""AnalysisFrame assembly, and the end-to-end path from the lake.

The integration test builds a real Parquet lake through the ingestion layer
(with its offline stubs) and preprocesses it, so the two layers are proven to
agree on the contract rather than on a shared assumption in a fixture.
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from cfdinvest.config import MacroDataConfig
from cfdinvest.ingestion.macro import MacroDataIngestor
from cfdinvest.ingestion.market import MarketDataIngestor
from cfdinvest.preprocessing import (
    AnalysisFrame,
    PreprocessConfig,
    build_analysis_frame,
)
from cfdinvest.preprocessing.alignment import AlignmentError
from cfdinvest.preprocessing.frame import (
    analysis_frame_from_repository,
    regime_placeholder,
)
from cfdinvest.repository import DataRepository
from cfdinvest.schema import SERIES_ID, TICKER, TIMESTAMP
from tests.conftest import make_yf_frame
from tests.fred_fixtures import FredStub
from tests.test_preprocessing_alignment import (
    UNRATE_OBS,
    UNRATE_PUBLISHED,
    UNRATE_VALUES,
    macro_long,
    tidy_market,
)


@pytest.fixture
def calendar() -> pd.DatetimeIndex:
    return pd.bdate_range("2023-01-02", periods=180, tz="UTC", name=TIMESTAMP)


@pytest.fixture
def market(calendar) -> pd.DataFrame:
    return tidy_market(calendar, tickers=("AAA", "BBB", "CCC"))


@pytest.fixture
def frame(market) -> AnalysisFrame:
    macro = macro_long(UNRATE_OBS, UNRATE_VALUES, UNRATE_PUBLISHED)
    return build_analysis_frame(
        market,
        macro,
        interval="1d",
        config=PreprocessConfig(correlation_window=20, volatility_window=10),
    )


# --------------------------------------------------------------------------- #
# Structure
# --------------------------------------------------------------------------- #
def test_frame_exposes_the_specified_members(frame):
    for member in (
        "prices",
        "returns",
        "volatility",
        "correlations",
        "macro",
        "regimes",
        "metadata",
    ):
        assert hasattr(frame, member)


def test_price_panels_cover_every_ohlcv_field(frame):
    assert set(frame.prices) == {"open", "high", "low", "close", "volume"}


def test_return_panels(frame):
    assert set(frame.returns) == {"simple", "log", "rolling", "overnight"}


def test_volatility_panels(frame):
    assert set(frame.volatility) == {"rolling", "realized", "atr"}


def test_correlation_panels(frame):
    assert set(frame.correlations) == {"rolling", "average", "breakdown"}


def test_every_panel_shares_the_calendar(frame):
    calendar = frame.calendar
    for group in (frame.prices, frame.returns, frame.volatility):
        for name, panel in group.items():
            assert panel.index.equals(calendar), name
    assert frame.macro.index.equals(calendar)
    assert frame.regimes.index.equals(calendar)


def test_every_panel_shares_the_universe(frame):
    for group in (frame.prices, frame.returns, frame.volatility):
        for name, panel in group.items():
            assert list(panel.columns) == list(frame.tickers), name


def test_calendar_is_sorted_and_unique(frame):
    assert frame.calendar.is_monotonic_increasing
    assert frame.calendar.is_unique


def test_index_stays_utc(frame):
    assert str(frame.calendar.tz) == "UTC"
    assert str(frame.macro.index.tz) == "UTC"


def test_convenience_accessors(frame):
    assert frame.tickers == ("AAA", "BBB", "CCC")
    assert frame.series_ids == ("UNRATE",)
    pd.testing.assert_frame_equal(frame.close, frame.prices["close"])


def test_describe_summarises_every_panel(frame):
    described = frame.describe()
    assert "prices.close" in described.index
    assert "volatility.atr" in described.index
    assert "macro" in described.index
    assert (described["rows"] == len(frame.calendar)).all()


def test_regimes_is_an_empty_placeholder(frame):
    assert frame.regimes.shape[1] == 0
    assert frame.regimes.index.equals(frame.calendar)


def test_regime_placeholder_helper(calendar):
    placeholder = regime_placeholder(calendar)
    assert placeholder.empty
    assert placeholder.index.name == TIMESTAMP


# --------------------------------------------------------------------------- #
# Metadata
# --------------------------------------------------------------------------- #
def test_metadata_records_provenance(frame):
    meta = frame.metadata

    assert meta["interval"] == "1d"
    assert meta["price_field"] == "close"
    assert meta["periods_per_year"] == 252
    assert meta["bars"] == len(frame.calendar)
    assert meta["tickers"] == frame.tickers
    assert meta["series_ids"] == ("UNRATE",)
    assert meta["calendar_start"] == frame.calendar.min()
    assert meta["calendar_end"] == frame.calendar.max()


def test_metadata_records_the_windows_used(frame):
    assert frame.metadata["windows"]["correlation"] == 20
    assert frame.metadata["windows"]["volatility"] == 10


# --------------------------------------------------------------------------- #
# Content correctness
# --------------------------------------------------------------------------- #
def test_returns_match_the_direct_computation(frame):
    from cfdinvest.preprocessing.returns import simple_returns

    pd.testing.assert_frame_equal(
        frame.returns["simple"], simple_returns(frame.close)
    )


def test_macro_carries_forward_past_the_last_release(frame):
    """The 2020 releases all predate this 2023 calendar, so the last known
    value is current throughout -- carried forward, never re-published."""
    unrate = frame.macro["UNRATE"]
    assert unrate.notna().all()
    assert np.allclose(unrate, 14.7)


def test_macro_is_forward_filled_within_the_window():
    calendar = pd.bdate_range("2020-01-01", "2020-06-01", tz="UTC", name=TIMESTAMP)
    market = tidy_market(calendar, tickers=("AAA", "BBB"))
    macro = macro_long(UNRATE_OBS, UNRATE_VALUES, UNRATE_PUBLISHED)

    built = build_analysis_frame(market, macro, config=PreprocessConfig(
        correlation_window=20, volatility_window=10
    ))
    unrate = built.macro["UNRATE"]

    assert pd.isna(unrate.loc["2020-02-06"])
    assert unrate.loc["2020-02-07"] == pytest.approx(3.6)
    assert unrate.loc["2020-05-08"] == pytest.approx(14.7)
    # Forward-filled between releases, not left as a single spike.
    assert unrate.loc["2020-02-10":"2020-03-05"].eq(3.6).all()


def test_volatility_is_annualised_with_the_recorded_factor(frame):
    from cfdinvest.preprocessing.volatility import rolling_volatility

    expected = rolling_volatility(
        frame.returns["simple"], 10, periods=frame.metadata["periods_per_year"]
    )
    pd.testing.assert_frame_equal(frame.volatility["rolling"], expected)


def test_correlation_average_is_a_single_column(frame):
    average = frame.correlations["average"]
    assert list(average.columns) == ["average_correlation"]
    assert average.index.equals(frame.calendar)


def test_breakdown_flags_are_boolean(frame):
    flags = frame.correlations["breakdown"]
    for column in ("elevated", "spike", "breakdown"):
        assert flags[column].dtype == np.bool_


# --------------------------------------------------------------------------- #
# Determinism and purity
# --------------------------------------------------------------------------- #
def test_building_twice_gives_identical_results(market):
    macro = macro_long(UNRATE_OBS, UNRATE_VALUES, UNRATE_PUBLISHED)
    config = PreprocessConfig(correlation_window=20)

    first = build_analysis_frame(market, macro, config=config)
    second = build_analysis_frame(market, macro, config=config)

    pd.testing.assert_frame_equal(first.close, second.close)
    pd.testing.assert_frame_equal(first.macro, second.macro)
    pd.testing.assert_frame_equal(
        first.correlations["average"], second.correlations["average"]
    )


def test_building_does_not_mutate_its_inputs(market):
    macro = macro_long(UNRATE_OBS, UNRATE_VALUES, UNRATE_PUBLISHED)
    market_before, macro_before = market.copy(), macro.copy()

    build_analysis_frame(market, macro, config=PreprocessConfig(correlation_window=20))

    pd.testing.assert_frame_equal(market, market_before)
    pd.testing.assert_frame_equal(macro, macro_before)


def test_ticker_order_does_not_affect_the_result(calendar):
    forward = tidy_market(calendar, tickers=("AAA", "BBB", "CCC"))
    reverse = tidy_market(calendar, tickers=("CCC", "BBB", "AAA"))

    lhs = build_analysis_frame(forward, config=PreprocessConfig(correlation_window=20))
    rhs = build_analysis_frame(reverse, config=PreprocessConfig(correlation_window=20))

    assert lhs.tickers == rhs.tickers
    pd.testing.assert_frame_equal(
        lhs.correlations["average"], rhs.correlations["average"]
    )


# --------------------------------------------------------------------------- #
# Degraded inputs
# --------------------------------------------------------------------------- #
def test_frame_without_macro(market):
    built = build_analysis_frame(market, None)

    assert built.macro.empty
    assert built.series_ids == ()
    assert not built.close.empty


def test_frame_with_a_single_asset_skips_correlations(calendar):
    built = build_analysis_frame(tidy_market(calendar, tickers=("ONLY",)))

    assert built.correlations == {}
    assert "rolling" in built.volatility


def test_frame_with_close_only_skips_atr_and_overnight(calendar):
    tidy = tidy_market(calendar, tickers=("AAA", "BBB"))
    closes_only = tidy[["close"]].copy()

    built = build_analysis_frame(closes_only)

    assert "atr" not in built.volatility
    assert "overnight" not in built.returns
    assert "rolling" in built.volatility


def test_frame_from_empty_market():
    built = build_analysis_frame(pd.DataFrame())

    assert built.prices == {}
    assert built.returns == {}
    assert built.tickers == ()
    assert len(built.calendar) == 0


def test_legacy_macro_without_publication_dates_raises(market, calendar):
    legacy = pd.DataFrame(
        {"value": [1.0, 2.0], SERIES_ID: "OLD"},
        index=pd.DatetimeIndex(["2023-01-03", "2023-02-01"], tz="UTC", name=TIMESTAMP),
    )
    with pytest.raises(AlignmentError):
        build_analysis_frame(market, legacy)


def test_legacy_macro_works_under_the_lag_policy(market):
    legacy = pd.DataFrame(
        {"value": [1.0, 2.0], SERIES_ID: "OLD"},
        index=pd.DatetimeIndex(["2023-01-03", "2023-02-01"], tz="UTC", name=TIMESTAMP),
    )
    built = build_analysis_frame(
        market, legacy, config=PreprocessConfig(missing_publication="lag")
    )

    assert built.series_ids == ("OLD",)
    assert pd.isna(built.macro["OLD"].loc["2023-01-03"])
    assert built.macro["OLD"].loc["2023-01-04"] == pytest.approx(1.0)


def test_resampling_to_weekly(market):
    weekly = build_analysis_frame(
        market, config=PreprocessConfig(resample_rule="W-MON", correlation_window=5)
    )

    assert len(weekly.calendar) < 180
    assert weekly.metadata["resample_rule"] == "W-MON"
    assert weekly.metadata["periods_per_year"] == 52


# --------------------------------------------------------------------------- #
# End-to-end through the real ingestion + repository stack
# --------------------------------------------------------------------------- #
@pytest.fixture
def lake(config):
    """A real Parquet lake, built offline through the ingestion layer."""
    sessions = pd.bdate_range("2020-01-01", periods=120, name="Date")

    def downloader(tickers, **kwargs):
        return pd.concat(
            {t: make_yf_frame(sessions, 100.0 + 25 * i) for i, t in enumerate(tickers)},
            axis=1,
        )

    MarketDataIngestor(config, downloader=downloader).ingest(["AAA", "BBB"])

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
    with MacroDataIngestor(macro_cfg, session=FredStub()) as ingestor:
        ingestor.ingest(["UNRATE"])

    return DataRepository(macro_cfg)


def test_end_to_end_from_the_repository(lake):
    built = analysis_frame_from_repository(
        lake, config=PreprocessConfig(correlation_window=20, volatility_window=10)
    )

    assert built.tickers == ("AAA", "BBB")
    assert built.series_ids == ("UNRATE",)
    assert set(built.prices) >= {"open", "high", "low", "close"}
    assert built.volatility["rolling"].notna().any().any()
    assert built.correlations["average"].notna().any().any()


def test_end_to_end_preserves_the_publication_boundary(lake):
    built = analysis_frame_from_repository(lake, config=PreprocessConfig(
        correlation_window=20
    ))
    unrate = built.macro["UNRATE"]

    # UNRATE January 2020 was first published 2020-02-07.
    assert pd.isna(unrate.loc["2020-02-06"])
    assert unrate.loc["2020-02-07"] == pytest.approx(3.6)


def test_end_to_end_metadata_carries_lake_provenance(lake):
    built = analysis_frame_from_repository(lake, config=PreprocessConfig(
        correlation_window=20
    ))

    assert "data_root" in built.metadata
    assert built.metadata["vintage_modes"]["UNRATE"] == "initial_release"


def test_end_to_end_respects_date_bounds(lake):
    built = analysis_frame_from_repository(
        lake,
        start="2020-03-01",
        end="2020-04-30",
        config=PreprocessConfig(correlation_window=5, volatility_window=5),
    )

    assert built.calendar.min() >= pd.Timestamp("2020-03-01", tz="UTC")
    assert built.calendar.max() <= pd.Timestamp("2020-04-30", tz="UTC")


def test_preprocessing_does_not_write_to_the_lake(lake, config):
    """Preprocessing is read-only; the lake must be byte-identical after."""
    before = {
        path: path.read_bytes()
        for path in sorted(config.paths.data_root.rglob("*.parquet"))
    }

    analysis_frame_from_repository(lake, config=PreprocessConfig(
        correlation_window=20
    ))

    after = {
        path: path.read_bytes()
        for path in sorted(config.paths.data_root.rglob("*.parquet"))
    }
    assert before == after
