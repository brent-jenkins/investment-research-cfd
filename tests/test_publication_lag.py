"""Publication-lag boundaries: no forward-fill at ingest, and enough
information stored for preprocessing to forward-fill without look-ahead.

These tests deliberately implement the as-of logic *inline*. Preprocessing is
not built yet; the point here is to prove the ingested data already supports
it, so the next layer has a contract to code against.
"""

from __future__ import annotations

from dataclasses import replace

import pandas as pd
import pytest

from cfdinvest import storage
from cfdinvest.config import MacroDataConfig
from cfdinvest.ingestion.macro import MacroDataIngestor
from cfdinvest.repository import DataRepository
from cfdinvest.schema import PUBLICATION_DATE
from tests.fred_fixtures import INGEST_DATE, UNRATE_INITIAL_RELEASE, FredStub


@pytest.fixture
def pit_repo(config):
    """A lake holding UNRATE first prints with true publication dates."""
    cfg = replace(
        config,
        macro=MacroDataConfig(
            series_ids=("UNRATE",),
            api_key="test-key",
            observation_start="2019-01-01",
            vintage_mode="initial_release",
            min_request_interval=0.0,
        ),
    )
    with MacroDataIngestor(cfg, session=FredStub()) as ingestor:
        ingestor.ingest(["UNRATE"])
    return DataRepository(cfg), cfg


def value_available_at(
    values: pd.Series, published: pd.Series, as_of: str
) -> float | None:
    """Latest observation whose publication date is on or before ``as_of``.

    This is the primitive the preprocessing layer will use to forward-fill a
    macro series onto the trading calendar without look-ahead.
    """
    moment = pd.Timestamp(as_of, tz="UTC")
    eligible = values[published.notna() & (published <= moment)]
    return None if eligible.empty else float(eligible.iloc[-1])


# --------------------------------------------------------------------------- #
# Ingestion must not fill anything in
# --------------------------------------------------------------------------- #
def test_ingestion_does_not_forward_fill_gaps(config):
    """FRED's '.' sentinel must survive to the reader as NaN, not be filled."""
    cfg = replace(
        config,
        macro=MacroDataConfig(
            series_ids=("DGS10",), api_key="test-key",
            observation_start="2019-01-01", min_request_interval=0.0,
        ),
    )
    with MacroDataIngestor(cfg, session=FredStub()) as ingestor:
        ingestor.ingest(["DGS10"])

    series = DataRepository(cfg).load_macro(["DGS10"])["DGS10"]

    # 2020-01-01 is a market holiday: FRED reports ".", we must report NaN.
    assert pd.isna(series.loc["2020-01-01"])
    assert series.loc["2020-01-02"] == pytest.approx(1.88)
    # A forward-fill would have made the holiday 1.88 or carried 1.80 forward.
    assert series.isna().sum() == 1


def test_ingestion_does_not_resample_onto_a_calendar(pit_repo):
    """Monthly observations stay monthly; no daily grid is invented."""
    repo, _ = pit_repo
    series = repo.load_macro(["UNRATE"])["UNRATE"]

    assert len(series) == len(UNRATE_INITIAL_RELEASE)
    assert list(series.index.strftime("%Y-%m-%d")) == sorted(UNRATE_INITIAL_RELEASE)


def test_gaps_survive_the_parquet_round_trip(config):
    cfg = replace(
        config,
        macro=MacroDataConfig(
            series_ids=("DGS10",), api_key="test-key",
            observation_start="2019-01-01", min_request_interval=0.0,
        ),
    )
    with MacroDataIngestor(cfg, session=FredStub()) as ingestor:
        ingestor.ingest(["DGS10"])

    on_disk = storage.read_parquet(storage.macro_path(cfg.paths, "DGS10"))
    assert on_disk["value"].isna().sum() == 1


# --------------------------------------------------------------------------- #
# Publication dates are stored
# --------------------------------------------------------------------------- #
def test_publication_dates_are_persisted(pit_repo):
    repo, cfg = pit_repo
    on_disk = storage.read_parquet(storage.macro_path(cfg.paths, "UNRATE"))

    assert PUBLICATION_DATE in on_disk.columns
    assert str(on_disk[PUBLICATION_DATE].dt.tz) == "UTC"
    assert on_disk[PUBLICATION_DATE].notna().all()


def test_publication_dates_match_the_real_release_calendar(pit_repo):
    repo, _ = pit_repo
    calendar = repo.publication_calendar(["UNRATE"], wide=True)["UNRATE"]

    for observation, (_, published) in UNRATE_INITIAL_RELEASE.items():
        assert calendar.loc[observation] == pd.Timestamp(published, tz="UTC")


def test_publication_lag_is_positive_and_realistic(pit_repo):
    repo, _ = pit_repo
    calendar = repo.publication_calendar(["UNRATE"], wide=True)["UNRATE"]

    lag = calendar - calendar.index
    # Every value is published after the period it describes, by 3-6 weeks.
    assert (lag > pd.Timedelta(0)).all()
    assert lag.max() < pd.Timedelta(days=60)


def test_latest_mode_publication_date_reveals_the_bias(config):
    """In latest mode FRED stamps the ingest date -- a detectable red flag."""
    cfg = replace(
        config,
        macro=MacroDataConfig(
            series_ids=("UNRATE",), api_key="test-key",
            observation_start="2019-01-01", min_request_interval=0.0,
            vintage_mode="latest",
        ),
    )
    with MacroDataIngestor(cfg, session=FredStub()) as ingestor:
        ingestor.ingest(["UNRATE"])

    calendar = DataRepository(cfg).publication_calendar(
        ["UNRATE"], wide=True
    )["UNRATE"]

    # Publication date == fetch date for every row: these are revised values,
    # not first prints. A point-in-time check can assert against exactly this.
    assert (calendar == pd.Timestamp(INGEST_DATE, tz="UTC")).all()


# --------------------------------------------------------------------------- #
# The boundary itself
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("as_of", "expected"),
    [
        ("2020-01-31", None),   # January not yet published
        ("2020-02-06", None),   # day before release
        ("2020-02-07", 3.6),    # release day: January becomes usable
        ("2020-03-05", 3.6),    # still January until February prints
        ("2020-03-06", 3.5),    # February released
        ("2020-05-08", 14.7),   # the COVID print
    ],
)
def test_only_published_values_are_visible(pit_repo, as_of, expected):
    repo, _ = pit_repo
    values = repo.load_macro(["UNRATE"])["UNRATE"]
    published = repo.publication_calendar(["UNRATE"], wide=True)["UNRATE"]

    result = value_available_at(values, published, as_of)
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected)


def test_publication_boundary_is_inclusive_on_release_day(pit_repo):
    repo, _ = pit_repo
    values = repo.load_macro(["UNRATE"])["UNRATE"]
    published = repo.publication_calendar(["UNRATE"], wide=True)["UNRATE"]

    assert value_available_at(values, published, "2020-04-02") == pytest.approx(3.5)
    assert value_available_at(values, published, "2020-04-03") == pytest.approx(4.4)


def test_observation_date_fill_would_leak_the_future(pit_repo):
    """Demonstrates the bug the publication calendar exists to prevent."""
    repo, _ = pit_repo
    values = repo.load_macro(["UNRATE"])["UNRATE"]
    published = repo.publication_calendar(["UNRATE"], wide=True)["UNRATE"]

    as_of = "2020-04-05"
    naive = values[values.index <= pd.Timestamp(as_of, tz="UTC")].iloc[-1]
    honest = value_available_at(values, published, as_of)

    # Filling on the OBSERVATION date hands the backtest April's 14.7 -- the
    # COVID unemployment spike -- on 2020-04-05, a full month before it was
    # published on 2020-05-08. That is a strategy that "predicts" the crash.
    assert naive == pytest.approx(14.7)

    # Filling on the PUBLICATION date gives March's 4.4, released 2020-04-03,
    # which is genuinely all a trader knew that day.
    assert honest == pytest.approx(4.4)
    assert naive != honest


def test_calendar_and_values_share_an_index(pit_repo):
    """Preprocessing can align the two frames without a join."""
    repo, _ = pit_repo
    values = repo.load_macro(["UNRATE"])
    calendar = repo.publication_calendar(["UNRATE"], wide=True)

    assert values.index.equals(calendar.index)
    assert list(values.columns) == list(calendar.columns)


def test_publication_calendar_is_empty_when_lake_is(config):
    assert DataRepository(config).publication_calendar(["UNRATE"]).empty
    assert DataRepository(config).publication_calendar(
        ["UNRATE"], wide=True
    ).empty


# --------------------------------------------------------------------------- #
# Backward compatibility with files written before publication dates existed
# --------------------------------------------------------------------------- #
def test_legacy_file_without_publication_dates_still_loads(config):
    """Files written by the previous version lack the column entirely."""
    legacy = pd.DataFrame(
        {"value": [1.88, 1.80], "series_id": ["DGS10", "DGS10"]},
        index=pd.DatetimeIndex(
            ["2020-01-02", "2020-01-03"], tz="UTC", name="timestamp"
        ),
    )
    storage.write_parquet(
        legacy, storage.macro_path(config.paths, "DGS10"), config.storage
    )

    repo = DataRepository(config)
    series = repo.load_macro(["DGS10"])["DGS10"]

    assert len(series) == 2
    assert series.iloc[0] == pytest.approx(1.88)
    # No calendar is available, but asking for one must not raise.
    assert repo.publication_calendar(["DGS10"]).empty


def test_legacy_file_reports_latest_vintage_mode(config):
    """An untracked series is reported as what the old code actually did."""
    storage.write_json(
        {"DGS10": {"status": "ok", "rows_total": 2}},
        storage.metadata_path(config.paths, "macro_manifest"),
    )
    assert DataRepository(config).macro_vintage_mode("DGS10") == "latest"


def test_upgrading_a_legacy_file_merges_publication_dates(config):
    """Re-ingesting in the same mode backfills the new column."""
    legacy = pd.DataFrame(
        {"value": [1.88], "series_id": ["DGS10"]},
        index=pd.DatetimeIndex(["2020-01-02"], tz="UTC", name="timestamp"),
    )
    storage.write_parquet(
        legacy, storage.macro_path(config.paths, "DGS10"), config.storage
    )

    cfg = replace(
        config,
        macro=MacroDataConfig(
            series_ids=("DGS10",), api_key="test-key",
            observation_start="2019-01-01", min_request_interval=0.0,
        ),
    )
    with MacroDataIngestor(cfg, session=FredStub()) as ingestor:
        report = ingestor.ingest(["DGS10"], incremental=False)

    assert report.results[0].status == "ok"
    on_disk = storage.read_parquet(storage.macro_path(cfg.paths, "DGS10"))
    assert PUBLICATION_DATE in on_disk.columns
    assert on_disk[PUBLICATION_DATE].notna().any()
