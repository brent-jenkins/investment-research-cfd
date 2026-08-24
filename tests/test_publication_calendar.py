"""Issue 3: the unified publication calendar downstream layers consume.

``repo.publication_calendar()`` is the contract preprocessing will forward-fill
against. It must be complete, deterministic, and honest about where each
publication date came from.
"""

from __future__ import annotations

from dataclasses import replace

import pandas as pd
import pytest

from cfdinvest import storage
from cfdinvest.config import MacroDataConfig
from cfdinvest.ingestion.macro import MacroDataIngestor
from cfdinvest.repository import DataRepository
from cfdinvest.schema import PUBLICATION_DATE, SERIES_ID
from tests.fred_fixtures import (
    DGS10_LATEST,
    GDPC1_INITIAL_RELEASE,
    UNRATE_INITIAL_RELEASE,
    FredStub,
)

CALENDAR_COLUMNS = [SERIES_ID, "observation_date", PUBLICATION_DATE, "vintage_mode"]


def macro(**changes) -> MacroDataConfig:
    return replace(
        MacroDataConfig(
            api_key="test-key",
            observation_start="2019-01-01",
            min_request_interval=0.0,
        ),
        **changes,
    )


@pytest.fixture
def mixed_lake(config):
    """Three series, three frequencies, three provenances.

    * ``UNRATE``  monthly,   real first prints          -> initial_release
    * ``GDPC1``   quarterly, real first prints          -> publication_lag
    * ``DGS10``   daily,     synthetic lag after a fallback
    """
    cfg = replace(
        config,
        macro=macro(
            series_ids=("UNRATE", "GDPC1", "DGS10"),
            series_overrides={"UNRATE": {"vintage_mode": "initial_release"}},
        ),
    )
    with MacroDataIngestor(cfg, session=FredStub()) as ingestor:
        ingestor.ingest(["UNRATE", "GDPC1", "DGS10"])
    return DataRepository(cfg), cfg


# --------------------------------------------------------------------------- #
# Shape
# --------------------------------------------------------------------------- #
def test_calendar_has_the_specified_columns(mixed_lake):
    repo, _ = mixed_lake
    assert list(repo.publication_calendar().columns) == CALENDAR_COLUMNS


def test_calendar_includes_all_series(mixed_lake):
    repo, _ = mixed_lake
    calendar = repo.publication_calendar()

    assert set(calendar[SERIES_ID].unique()) == {"UNRATE", "GDPC1", "DGS10"}
    assert set(calendar[SERIES_ID].unique()) == set(repo.available_series())


def test_row_count_matches_the_observations(mixed_lake):
    repo, _ = mixed_lake
    calendar = repo.publication_calendar()
    counts = calendar.groupby(SERIES_ID).size().to_dict()

    assert counts["UNRATE"] == len(UNRATE_INITIAL_RELEASE)
    assert counts["GDPC1"] == len(GDPC1_INITIAL_RELEASE)
    assert counts["DGS10"] == len(DGS10_LATEST)


def test_calendar_index_is_a_clean_range(mixed_lake):
    repo, _ = mixed_lake
    calendar = repo.publication_calendar()
    assert list(calendar.index) == list(range(len(calendar)))


def test_dates_are_tz_aware_utc(mixed_lake):
    repo, _ = mixed_lake
    calendar = repo.publication_calendar()

    assert str(calendar["observation_date"].dt.tz) == "UTC"
    assert str(calendar[PUBLICATION_DATE].dt.tz) == "UTC"


def test_empty_lake_returns_typed_empty_calendar(config):
    calendar = DataRepository(config).publication_calendar()
    assert calendar.empty
    assert list(calendar.columns) == CALENDAR_COLUMNS


# --------------------------------------------------------------------------- #
# Determinism and ordering
# --------------------------------------------------------------------------- #
def test_calendar_is_sorted_by_series_then_observation(mixed_lake):
    repo, _ = mixed_lake
    calendar = repo.publication_calendar()

    expected = calendar.sort_values([SERIES_ID, "observation_date"], kind="stable")
    pd.testing.assert_frame_equal(calendar, expected.reset_index(drop=True))


def test_each_series_block_is_chronological(mixed_lake):
    repo, _ = mixed_lake
    for _, block in repo.publication_calendar().groupby(SERIES_ID):
        assert block["observation_date"].is_monotonic_increasing


def test_calendar_is_deterministic_across_calls(mixed_lake):
    repo, _ = mixed_lake
    pd.testing.assert_frame_equal(
        repo.publication_calendar(), repo.publication_calendar()
    )


def test_request_order_does_not_change_the_result(mixed_lake):
    repo, _ = mixed_lake
    forward = repo.publication_calendar(["DGS10", "GDPC1", "UNRATE"])
    reverse = repo.publication_calendar(["UNRATE", "GDPC1", "DGS10"])

    pd.testing.assert_frame_equal(forward, reverse)


def test_ingestion_order_does_not_change_the_result(config):
    """Two lakes, same series, opposite ingest order -> identical calendars."""
    calendars = []
    for order in (["UNRATE", "GDPC1"], ["GDPC1", "UNRATE"]):
        cfg = replace(
            config,
            paths=replace(
                config.paths, data_root=config.paths.data_root / "-".join(order)
            ),
            macro=macro(series_ids=tuple(order)),
        )
        with MacroDataIngestor(cfg, session=FredStub()) as ingestor:
            ingestor.ingest(order)
        calendars.append(DataRepository(cfg).publication_calendar())

    pd.testing.assert_frame_equal(calendars[0], calendars[1])


# --------------------------------------------------------------------------- #
# Publication dates match the vendor
# --------------------------------------------------------------------------- #
def test_dates_match_vendor_output_type_4(mixed_lake):
    """First prints must carry FRED's own realtime_start, unmodified."""
    repo, _ = mixed_lake
    calendar = repo.publication_calendar(["UNRATE"]).set_index("observation_date")

    for observation, (_, published) in UNRATE_INITIAL_RELEASE.items():
        stamp = pd.Timestamp(observation, tz="UTC")
        assert calendar.loc[stamp, PUBLICATION_DATE] == pd.Timestamp(
            published, tz="UTC"
        )


def test_quarterly_dates_match_vendor_output_type_4(mixed_lake):
    repo, _ = mixed_lake
    calendar = repo.publication_calendar(["GDPC1"]).set_index("observation_date")

    for observation, (_, published) in GDPC1_INITIAL_RELEASE.items():
        stamp = pd.Timestamp(observation, tz="UTC")
        assert calendar.loc[stamp, PUBLICATION_DATE] == pd.Timestamp(
            published, tz="UTC"
        )


def test_publication_always_follows_observation(mixed_lake):
    repo, _ = mixed_lake
    calendar = repo.publication_calendar()
    assert (calendar[PUBLICATION_DATE] > calendar["observation_date"]).all()


# --------------------------------------------------------------------------- #
# vintage_mode provenance
# --------------------------------------------------------------------------- #
def test_vintage_mode_is_carried_per_row(mixed_lake):
    repo, _ = mixed_lake
    calendar = repo.publication_calendar()
    modes = calendar.groupby(SERIES_ID)["vintage_mode"].unique().to_dict()

    assert modes["UNRATE"].tolist() == ["initial_release"]
    assert modes["GDPC1"].tolist() == ["publication_lag"]
    assert modes["DGS10"].tolist() == ["publication_lag"]   # after the fallback


def test_vintage_mode_agrees_with_the_repository_accessor(mixed_lake):
    repo, _ = mixed_lake
    calendar = repo.publication_calendar()

    for series_id, block in calendar.groupby(SERIES_ID):
        assert block["vintage_mode"].iloc[0] == repo.macro_vintage_mode(series_id)


def test_biased_series_is_labelled_latest(config):
    """A latest-mode file still appears, flagged so callers can exclude it."""
    cfg = replace(config, macro=macro(series_ids=("DGS10",), vintage_mode="latest"))
    with MacroDataIngestor(cfg, session=FredStub()) as ingestor:
        ingestor.ingest(["DGS10"])

    calendar = DataRepository(cfg).publication_calendar()
    assert (calendar["vintage_mode"] == "latest").all()


# --------------------------------------------------------------------------- #
# Mixed frequencies
# --------------------------------------------------------------------------- #
def test_mixed_frequencies_coexist(mixed_lake):
    repo, _ = mixed_lake
    calendar = repo.publication_calendar()

    spacing = {
        series_id: block["observation_date"].diff().dropna().min()
        for series_id, block in calendar.groupby(SERIES_ID)
    }
    assert spacing["DGS10"] < pd.Timedelta(days=7)        # daily
    assert pd.Timedelta(days=27) < spacing["UNRATE"] < pd.Timedelta(days=32)
    assert spacing["GDPC1"] >= pd.Timedelta(days=89)      # quarterly


def test_mixed_frequency_lags_differ_appropriately(mixed_lake):
    repo, _ = mixed_lake
    calendar = repo.publication_calendar()
    calendar["lag"] = calendar[PUBLICATION_DATE] - calendar["observation_date"]
    lags = calendar.groupby(SERIES_ID)["lag"].max().to_dict()

    assert lags["DGS10"] == pd.Timedelta(days=1)          # synthetic
    assert lags["UNRATE"] > pd.Timedelta(days=30)         # real release lag
    assert lags["GDPC1"] > pd.Timedelta(days=100)         # quarterly reporting


def test_date_bounds_filter_on_observation_date(mixed_lake):
    repo, _ = mixed_lake
    calendar = repo.publication_calendar(start="2020-02-01", end="2020-03-31")

    assert not calendar.empty
    assert calendar["observation_date"].min() >= pd.Timestamp("2020-02-01", tz="UTC")
    assert calendar["observation_date"].max() <= pd.Timestamp("2020-03-31", tz="UTC")


def test_subset_selection(mixed_lake):
    repo, _ = mixed_lake
    calendar = repo.publication_calendar(["UNRATE"])
    assert set(calendar[SERIES_ID].unique()) == {"UNRATE"}


# --------------------------------------------------------------------------- #
# No forward-fill at ingestion
# --------------------------------------------------------------------------- #
def test_no_forward_fill_at_ingestion(mixed_lake):
    """Values keep their gaps; the calendar does not invent observations."""
    repo, cfg = mixed_lake

    series = repo.load_macro(["DGS10"])["DGS10"]
    assert series.isna().sum() == 1

    on_disk = storage.read_parquet(storage.macro_path(cfg.paths, "DGS10"))
    assert on_disk["value"].isna().sum() == 1


def test_calendar_row_exists_even_for_a_missing_value(mixed_lake):
    """A gap still has a publication date -- the absence is itself dated."""
    repo, _ = mixed_lake
    calendar = repo.publication_calendar(["DGS10"])
    holiday = pd.Timestamp("2020-01-01", tz="UTC")

    row = calendar[calendar["observation_date"] == holiday]
    assert len(row) == 1
    assert pd.notna(row[PUBLICATION_DATE].iloc[0])


def test_calendar_does_not_resample_onto_a_common_grid(mixed_lake):
    """Each series keeps its own observation dates, not a padded union."""
    repo, _ = mixed_lake
    calendar = repo.publication_calendar()

    unrate_dates = set(
        calendar.loc[calendar[SERIES_ID] == "UNRATE", "observation_date"]
    )
    dgs_dates = set(calendar.loc[calendar[SERIES_ID] == "DGS10", "observation_date"])
    assert unrate_dates != dgs_dates
    assert len(calendar) == len(UNRATE_INITIAL_RELEASE) + len(
        GDPC1_INITIAL_RELEASE
    ) + len(DGS10_LATEST)


# --------------------------------------------------------------------------- #
# The panel form still works
# --------------------------------------------------------------------------- #
def test_wide_form_aligns_with_load_macro(mixed_lake):
    repo, _ = mixed_lake
    values = repo.load_macro()
    panel = repo.publication_calendar(wide=True)

    assert values.index.equals(panel.index)
    assert list(values.columns) == list(panel.columns)


def test_wide_and_long_agree(mixed_lake):
    repo, _ = mixed_lake
    long = repo.publication_calendar(["UNRATE"]).set_index("observation_date")
    wide = repo.publication_calendar(["UNRATE"], wide=True)["UNRATE"].dropna()

    for stamp, published in wide.items():
        assert long.loc[stamp, PUBLICATION_DATE] == published
