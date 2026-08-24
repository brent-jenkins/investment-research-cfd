"""Issue 2: initial_release falls back for series FRED cannot serve first prints for.

DGS10 has 5092 vintage dates; FRED's cap for a vintage-spanning request is
2000, so ``output_type=4`` is refused. Rather than failing the series, the
ingestor refetches the latest values -- which for an unrevised daily series
*are* the first prints -- and stamps ``observation_date + release_lag``.
"""

from __future__ import annotations

import logging
from dataclasses import replace

import pandas as pd
import pytest

from cfdinvest.config import MacroDataConfig
from cfdinvest.ingestion.macro import (
    FredVintageLimitError,
    MacroDataIngestor,
    apply_synthetic_publication_dates,
    release_lag,
)
from cfdinvest.repository import DataRepository
from cfdinvest.schema import PUBLICATION_DATE
from tests.fred_fixtures import (
    DGS10_LATEST,
    UNRATE_INITIAL_RELEASE,
    FredStub,
)


def macro(**changes) -> MacroDataConfig:
    return replace(
        MacroDataConfig(
            api_key="test-key",
            observation_start="2019-01-01",
            min_request_interval=0.0,
        ),
        **changes,
    )


def ingest(config, series, stub=None, **kwargs):
    stub = stub or FredStub()
    with MacroDataIngestor(config, session=stub) as ingestor:
        return ingestor.ingest(series, **kwargs), stub


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #
def test_vintage_limit_raises_a_recoverable_error(config):
    """The cap must surface as its own class, not a generic FredError."""
    cfg = replace(config, macro=macro(series_ids=("DGS10",)))
    with MacroDataIngestor(cfg, session=FredStub()) as ingestor:
        with pytest.raises(FredVintageLimitError):
            ingestor.fetch_observations(
                "DGS10", cfg.macro, mode="initial_release"
            )


def test_series_that_supports_first_prints_does_not_raise(config):
    cfg = replace(config, macro=macro(series_ids=("UNRATE",)))
    with MacroDataIngestor(cfg, session=FredStub()) as ingestor:
        frame = ingestor.fetch_observations(
            "UNRATE", cfg.macro, mode="initial_release"
        )
    assert len(frame) == len(UNRATE_INITIAL_RELEASE)


# --------------------------------------------------------------------------- #
# Fallback happens, and only where needed
# --------------------------------------------------------------------------- #
def test_initial_release_falls_back_for_a_daily_series(config):
    cfg = replace(
        config, macro=macro(series_ids=("DGS10",), vintage_mode="initial_release")
    )
    report, _ = ingest(cfg, ["DGS10"])

    result = report.results[0]
    assert result.status == "ok"
    assert result.fell_back is True
    assert result.vintage_mode == "publication_lag"


def test_no_fallback_for_a_series_that_supports_first_prints(config):
    cfg = replace(
        config, macro=macro(series_ids=("UNRATE",), vintage_mode="initial_release")
    )
    report, _ = ingest(cfg, ["UNRATE"])

    result = report.results[0]
    assert result.status == "ok"
    assert result.fell_back is False
    assert result.vintage_mode == "initial_release"


def test_publication_lag_default_also_falls_back(config):
    cfg = replace(config, macro=macro(series_ids=("DGS10",)))
    report, _ = ingest(cfg, ["DGS10"])

    assert report.results[0].status == "ok"
    assert report.results[0].vintage_mode == "publication_lag"


def test_fallback_is_not_a_failure(config):
    """The whole point: a daily series must still land, not error out."""
    cfg = replace(
        config,
        macro=macro(series_ids=("DGS10", "UNRATE"), vintage_mode="initial_release"),
    )
    report, _ = ingest(cfg, ["DGS10", "UNRATE"])

    assert len(report.succeeded) == 2
    assert report.failed == ()


def test_latest_mode_never_attempts_first_prints(config):
    cfg = replace(config, macro=macro(series_ids=("DGS10",), vintage_mode="latest"))
    _, stub = ingest(cfg, ["DGS10"])

    assert all("output_type" not in c for c in stub.observation_calls)


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
def test_fallback_from_initial_release_warns(config, caplog):
    caplog.set_level(logging.INFO, logger="cfdinvest.ingestion.macro")
    cfg = replace(
        config, macro=macro(series_ids=("DGS10",), vintage_mode="initial_release")
    )
    ingest(cfg, ["DGS10"])

    assert "falling back to publication_lag" in caplog.text
    assert "DERIVED, not vendor-reported" in caplog.text
    # Explicitly asked for first prints and did not get them -> warn.
    assert any(
        r.levelno == logging.WARNING and "falling back" in r.message
        for r in caplog.records
    )


def test_fallback_message_names_the_lag(config, caplog):
    caplog.set_level(logging.INFO, logger="cfdinvest.ingestion.macro")
    cfg = replace(
        config,
        macro=macro(
            series_ids=("DGS10",),
            vintage_mode="initial_release",
            series_overrides={"DGS10": {"release_lag_days": 3}},
        ),
    )
    ingest(cfg, ["DGS10"])
    assert "3-day release lag" in caplog.text


def test_publication_lag_fallback_is_informational_not_a_warning(config, caplog):
    """Under the default mode a fallback is expected, not alarming."""
    caplog.set_level(logging.INFO, logger="cfdinvest.ingestion.macro")
    cfg = replace(config, macro=macro(series_ids=("DGS10",)))
    ingest(cfg, ["DGS10"])

    assert "falling back to publication_lag" in caplog.text
    assert not any(
        r.levelno >= logging.WARNING and "falling back" in r.message
        for r in caplog.records
    )


# --------------------------------------------------------------------------- #
# publication_date semantics are unchanged by the fallback
# --------------------------------------------------------------------------- #
def test_synthetic_dates_are_observation_plus_lag(config):
    cfg = replace(config, macro=macro(series_ids=("DGS10",)))
    ingest(cfg, ["DGS10"])

    stacked = DataRepository(cfg).load_macro(["DGS10"], wide=False)
    lag = pd.Timedelta(days=1)

    assert (stacked[PUBLICATION_DATE] == stacked.index + lag).all()


def test_synthetic_dates_honour_a_per_series_lag(config):
    cfg = replace(
        config,
        macro=macro(
            series_ids=("DGS10",),
            series_overrides={"DGS10": {"release_lag_days": 4}},
        ),
    )
    ingest(cfg, ["DGS10"])

    stacked = DataRepository(cfg).load_macro(["DGS10"], wide=False)
    assert (stacked[PUBLICATION_DATE] == stacked.index + pd.Timedelta(days=4)).all()


def test_fallback_keeps_the_column_dtype_and_timezone(config):
    """Same schema as a real first-print file: nothing downstream can tell."""
    fallback_cfg = replace(config, macro=macro(series_ids=("DGS10",)))
    ingest(fallback_cfg, ["DGS10"])
    real_cfg = replace(config, macro=macro(series_ids=("UNRATE",)))
    ingest(real_cfg, ["UNRATE"])

    repo = DataRepository(fallback_cfg)
    stacked = repo.load_macro(wide=False)

    assert str(stacked[PUBLICATION_DATE].dt.tz) == "UTC"
    assert stacked[PUBLICATION_DATE].notna().all()


def test_fallback_publication_dates_are_always_after_the_observation(config):
    cfg = replace(config, macro=macro(series_ids=("DGS10",)))
    ingest(cfg, ["DGS10"])

    stacked = DataRepository(cfg).load_macro(["DGS10"], wide=False)
    assert (stacked[PUBLICATION_DATE] > stacked.index).all()


def test_fallback_overwrites_the_meaningless_ingest_stamp(config):
    """Latest mode stamps *today*; the fallback must replace that."""
    cfg = replace(config, macro=macro(series_ids=("DGS10",)))
    ingest(cfg, ["DGS10"])

    stacked = DataRepository(cfg).load_macro(["DGS10"], wide=False)
    # Every date is in 2020, not the stub's 2026 "ingest date".
    assert (stacked[PUBLICATION_DATE].dt.year == 2020).all()


def test_fallback_preserves_missing_values(config):
    """Gaps must survive the fallback exactly as on the primary path."""
    cfg = replace(config, macro=macro(series_ids=("DGS10",)))
    ingest(cfg, ["DGS10"])

    series = DataRepository(cfg).load_macro(["DGS10"])["DGS10"]
    assert len(series) == len(DGS10_LATEST)
    assert series.isna().sum() == 1          # the New Year's Day holiday


def test_apply_synthetic_publication_dates_is_pure():
    index = pd.DatetimeIndex(["2020-01-02", "2020-01-03"], tz="UTC", name="timestamp")
    frame = pd.DataFrame({"value": [1.0, 2.0]}, index=index)
    before = frame.copy()

    result = apply_synthetic_publication_dates(frame, pd.Timedelta(days=2))

    pd.testing.assert_frame_equal(frame, before)
    assert result[PUBLICATION_DATE].iloc[0] == pd.Timestamp("2020-01-04", tz="UTC")


def test_apply_synthetic_publication_dates_handles_empty():
    empty = pd.DataFrame({"value": pd.Series(dtype="float64")})
    assert apply_synthetic_publication_dates(empty, pd.Timedelta(days=1)).empty


def test_zero_lag_is_permitted():
    settings = macro(series_overrides={"X": {"release_lag_days": 0}})
    from cfdinvest.ingestion.macro import series_settings

    assert release_lag(series_settings(settings, "X")) == pd.Timedelta(0)


# --------------------------------------------------------------------------- #
# The unsupported answer is cached
# --------------------------------------------------------------------------- #
def test_unsupported_result_is_recorded(config):
    cfg = replace(config, macro=macro(series_ids=("DGS10",)))
    ingest(cfg, ["DGS10"])

    with MacroDataIngestor(cfg, session=FredStub()) as ingestor:
        assert ingestor.stored_initial_release_support()["DGS10"] is False


def test_supported_result_is_recorded(config):
    cfg = replace(config, macro=macro(series_ids=("UNRATE",)))
    ingest(cfg, ["UNRATE"])

    with MacroDataIngestor(cfg, session=FredStub()) as ingestor:
        assert ingestor.stored_initial_release_support()["UNRATE"] is True


def test_second_run_skips_the_doomed_request(config):
    """One wasted round-trip per daily series, not one per run."""
    cfg = replace(config, macro=macro(series_ids=("DGS10",)))
    ingest(cfg, ["DGS10"])

    _, stub = ingest(cfg, ["DGS10"])
    assert all("output_type" not in c for c in stub.observation_calls)


def test_first_run_does_attempt_the_request(config):
    cfg = replace(config, macro=macro(series_ids=("DGS10",)))
    _, stub = ingest(cfg, ["DGS10"])
    assert any(c.get("output_type") == "4" for c in stub.observation_calls)


def test_cached_fallback_does_not_trip_the_mode_guardrail(config):
    """initial_release + known-unsupported must not conflict with its own file."""
    cfg = replace(
        config, macro=macro(series_ids=("DGS10",), vintage_mode="initial_release")
    )
    first, _ = ingest(cfg, ["DGS10"])
    assert first.results[0].vintage_mode == "publication_lag"

    second, _ = ingest(cfg, ["DGS10"])
    assert second.results[0].status == "ok"
