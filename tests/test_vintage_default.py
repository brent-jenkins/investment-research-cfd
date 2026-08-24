"""Issue 1: publication_lag as the global default, plus per-series overrides.

The default moved from ``latest`` (revised, look-ahead biased) to
``publication_lag`` (every observation carries a real publication date).
These tests pin that decision so it cannot regress silently.
"""

from __future__ import annotations

import logging
from dataclasses import replace

import pytest

from cfdinvest import storage
from cfdinvest.config import AppConfig, MacroDataConfig, load_config
from cfdinvest.ingestion.macro import (
    MacroDataIngestor,
    build_observation_params,
    release_lag,
    resolve_vintage_mode,
    series_settings,
)
from cfdinvest.repository import DataRepository
from cfdinvest.schema import DEFAULT_VINTAGE_MODE, POINT_IN_TIME_MODES, VINTAGE_MODES
from tests.fred_fixtures import GDPC1_INITIAL_RELEASE, GDPC1_LATEST, FredStub


def macro(**changes) -> MacroDataConfig:
    """Macro config with a key and the throttle disabled."""
    return replace(
        MacroDataConfig(
            api_key="test-key",
            observation_start="2019-01-01",
            min_request_interval=0.0,
        ),
        **changes,
    )


def ingest(config: AppConfig, series, stub=None, **kwargs):
    with MacroDataIngestor(config, session=stub or FredStub()) as ingestor:
        return ingestor.ingest(series, **kwargs)


# --------------------------------------------------------------------------- #
# The default itself
# --------------------------------------------------------------------------- #
def test_module_constant_is_publication_lag():
    assert DEFAULT_VINTAGE_MODE == "publication_lag"
    assert "publication_lag" in VINTAGE_MODES
    assert POINT_IN_TIME_MODES == {"initial_release", "publication_lag"}


def test_bare_config_resolves_to_publication_lag():
    assert resolve_vintage_mode(MacroDataConfig()) == "publication_lag"


def test_loaded_config_defaults_to_publication_lag(tmp_path):
    """A config file that says nothing about vintages gets the safe default."""
    config_file = tmp_path / "config.toml"
    config_file.write_text('[macro]\nseries_ids = ["UNRATE"]\n', encoding="utf-8")

    config = load_config(config_file, env={}, dotenv_path=None)
    assert resolve_vintage_mode(config.macro) == "publication_lag"


def test_default_requests_first_prints():
    params = build_observation_params("UNRATE", macro())
    assert params["output_type"] == "4"


def test_default_yields_real_publication_dates(config):
    cfg = replace(config, macro=macro(series_ids=("GDPC1",)))
    ingest(cfg, ["GDPC1"])

    stacked = DataRepository(cfg).load_macro(["GDPC1"], wide=False)
    # First print, not the revised figure -- the default is unbiased.
    assert stacked.loc["2019-10-01", "value"] == pytest.approx(
        float(GDPC1_INITIAL_RELEASE["2019-10-01"][0])
    )
    assert stacked.loc["2019-10-01", "value"] != pytest.approx(
        float(GDPC1_LATEST["2019-10-01"])
    )


def test_realtime_bounds_still_win_over_the_default():
    """Backward compatibility: an old vintage config is unaffected."""
    settings = macro(realtime_start="2020-04-01", realtime_end="2020-04-01")
    assert resolve_vintage_mode(settings) == "vintage"


def test_latest_must_now_be_opted_into_explicitly(config):
    cfg = replace(config, macro=macro(series_ids=("GDPC1",), vintage_mode="latest"))
    ingest(cfg, ["GDPC1"])

    values = DataRepository(cfg).load_macro(["GDPC1"])["GDPC1"]
    assert values.loc["2019-10-01"] == pytest.approx(float(GDPC1_LATEST["2019-10-01"]))
    assert DataRepository(cfg).macro_vintage_mode("GDPC1") == "latest"


def test_latest_mode_logs_a_warning(config, caplog):
    cfg = replace(config, macro=macro(series_ids=("GDPC1",), vintage_mode="latest"))
    ingest(cfg, ["GDPC1"])
    assert "NOT what a trader saw" in caplog.text


# --------------------------------------------------------------------------- #
# Per-series overrides
# --------------------------------------------------------------------------- #
def test_override_changes_one_series_only():
    settings = macro(series_overrides={"DGS10": {"vintage_mode": "latest"}})

    assert resolve_vintage_mode(series_settings(settings, "DGS10")) == "latest"
    assert resolve_vintage_mode(series_settings(settings, "UNRATE")) == (
        "publication_lag"
    )
    # The run-level config is untouched -- series_settings is pure.
    assert resolve_vintage_mode(settings) == "publication_lag"


def test_override_reaches_the_request_parameters():
    settings = macro(series_overrides={"DGS10": {"vintage_mode": "latest"}})

    dgs = build_observation_params("DGS10", series_settings(settings, "DGS10"))
    unrate = build_observation_params("UNRATE", series_settings(settings, "UNRATE"))

    assert "output_type" not in dgs
    assert unrate["output_type"] == "4"


def test_override_can_set_release_lag():
    settings = macro(series_overrides={"DGS10": {"release_lag_days": 3}})

    assert release_lag(series_settings(settings, "DGS10")).days == 3
    assert release_lag(series_settings(settings, "UNRATE")).days == 1


def test_override_accepts_several_fields_at_once():
    settings = macro(
        series_overrides={
            "DGS10": {"vintage_mode": "latest", "release_lag_days": 2},
        }
    )
    resolved = series_settings(settings, "DGS10")

    assert resolve_vintage_mode(resolved) == "latest"
    assert release_lag(resolved).days == 2


def test_unknown_override_key_warns_but_does_not_raise(caplog):
    settings = macro(series_overrides={"DGS10": {"nonsense": 1}})
    resolved = series_settings(settings, "DGS10")

    assert resolve_vintage_mode(resolved) == "publication_lag"
    assert "Ignoring unknown override" in caplog.text


def test_override_for_an_uningested_series_is_inert():
    settings = macro(series_overrides={"NOT_REQUESTED": {"vintage_mode": "latest"}})
    assert resolve_vintage_mode(series_settings(settings, "UNRATE")) == (
        "publication_lag"
    )


def test_overrides_survive_toml_round_trip(tmp_path):
    """`[macro.series_overrides.DGS10]` must reach the dataclass intact."""
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        "[macro]\n"
        'series_ids = ["DGS10", "UNRATE"]\n'
        "\n"
        "[macro.series_overrides.DGS10]\n"
        'vintage_mode = "latest"\n'
        "release_lag_days = 2\n",
        encoding="utf-8",
    )

    config = load_config(config_file, env={}, dotenv_path=None)

    assert config.macro.series_overrides["DGS10"]["vintage_mode"] == "latest"
    assert resolve_vintage_mode(series_settings(config.macro, "DGS10")) == "latest"
    assert resolve_vintage_mode(series_settings(config.macro, "UNRATE")) == (
        "publication_lag"
    )


def test_overrides_apply_end_to_end(config):
    cfg = replace(
        config,
        macro=macro(
            series_ids=("GDPC1", "UNRATE"),
            series_overrides={"GDPC1": {"vintage_mode": "latest"}},
        ),
    )
    report = ingest(cfg, ["GDPC1", "UNRATE"])
    assert len(report.succeeded) == 2

    repo = DataRepository(cfg)
    assert repo.macro_vintage_mode("GDPC1") == "latest"
    assert repo.macro_vintage_mode("UNRATE") == "publication_lag"

    # And the values differ accordingly: GDPC1 revised, UNRATE first prints.
    assert repo.load_macro(["GDPC1"])["GDPC1"].loc["2019-10-01"] == pytest.approx(
        float(GDPC1_LATEST["2019-10-01"])
    )


def test_per_series_modes_are_logged(config, caplog):
    caplog.set_level(logging.INFO, logger="cfdinvest.ingestion.macro")
    cfg = replace(
        config,
        macro=macro(
            series_ids=("GDPC1",),
            series_overrides={"GDPC1": {"vintage_mode": "latest"}},
        ),
    )
    ingest(cfg, ["GDPC1"])
    assert "Per-series mode overrides" in caplog.text


# --------------------------------------------------------------------------- #
# Mixed-mode ingestion is rejected
# --------------------------------------------------------------------------- #
def test_mixed_mode_ingestion_is_rejected(config):
    """A latest file must not gain publication_lag rows."""
    latest = replace(config, macro=macro(series_ids=("GDPC1",), vintage_mode="latest"))
    ingest(latest, ["GDPC1"])

    default = replace(config, macro=macro(series_ids=("GDPC1",)))
    report = ingest(default, ["GDPC1"])

    assert report.results[0].status == "failed"
    assert "allow_mode_change" in report.results[0].error
    # The stored file is untouched.
    assert DataRepository(config).load_macro(["GDPC1"])["GDPC1"].loc[
        "2019-10-01"
    ] == pytest.approx(float(GDPC1_LATEST["2019-10-01"]))


def test_rejection_message_suggests_a_per_series_pin(config):
    latest = replace(config, macro=macro(series_ids=("GDPC1",), vintage_mode="latest"))
    ingest(latest, ["GDPC1"])

    report = ingest(replace(config, macro=macro(series_ids=("GDPC1",))), ["GDPC1"])
    assert "series_overrides" in report.results[0].error


def test_pinning_a_series_avoids_the_rejection(config):
    """The documented escape hatch for an existing latest-mode lake."""
    latest = replace(config, macro=macro(series_ids=("GDPC1",), vintage_mode="latest"))
    ingest(latest, ["GDPC1"])

    pinned = replace(
        config,
        macro=macro(
            series_ids=("GDPC1",),
            series_overrides={"GDPC1": {"vintage_mode": "latest"}},
        ),
    )
    report = ingest(pinned, ["GDPC1"])
    assert report.results[0].status == "ok"


def test_mixed_modes_across_series_in_one_run_are_fine(config):
    """Different files may hold different modes; one file may not."""
    cfg = replace(
        config,
        macro=macro(
            series_ids=("GDPC1", "UNRATE"),
            series_overrides={"GDPC1": {"vintage_mode": "latest"}},
        ),
    )
    report = ingest(cfg, ["GDPC1", "UNRATE"])

    assert {r.symbol: r.vintage_mode for r in report.results} == {
        "GDPC1": "latest",
        "UNRATE": "publication_lag",
    }


def test_allow_mode_change_still_works(config):
    latest = replace(config, macro=macro(series_ids=("GDPC1",), vintage_mode="latest"))
    ingest(latest, ["GDPC1"])

    default = replace(config, macro=macro(series_ids=("GDPC1",)))
    report = ingest(default, ["GDPC1"], allow_mode_change=True)

    assert report.results[0].status == "ok"
    repo = DataRepository(default)
    assert repo.macro_vintage_mode("GDPC1") == "publication_lag"
    assert repo.load_macro(["GDPC1"])["GDPC1"].loc["2019-10-01"] == pytest.approx(
        float(GDPC1_INITIAL_RELEASE["2019-10-01"][0])
    )


# --------------------------------------------------------------------------- #
# Repository exposes the mode per file
# --------------------------------------------------------------------------- #
def test_repository_reports_mode_per_file(config):
    cfg = replace(
        config,
        macro=macro(
            series_ids=("GDPC1", "UNRATE"),
            series_overrides={"GDPC1": {"vintage_mode": "latest"}},
        ),
    )
    ingest(cfg, ["GDPC1", "UNRATE"])

    repo = DataRepository(cfg)
    assert repo.macro_vintage_modes() == {
        "GDPC1": "latest",
        "UNRATE": "publication_lag",
    }
    coverage = repo.macro_coverage()
    assert coverage.loc["GDPC1", "vintage_mode"] == "latest"
    assert coverage.loc["UNRATE", "vintage_mode"] == "publication_lag"


def test_legacy_manifest_without_per_series_mode_reports_latest(config):
    """Files written before per-series tracking say what that code did."""
    storage.write_json(
        {"OLD": {"status": "ok", "rows_total": 3}},
        storage.metadata_path(config.paths, "macro_manifest"),
    )
    assert DataRepository(config).macro_vintage_mode("OLD") == "latest"
