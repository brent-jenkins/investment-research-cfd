"""Vintage-mode correctness: biased, unbiased and point-in-time sourcing."""

from __future__ import annotations

from dataclasses import replace

import pandas as pd
import pytest

from cfdinvest import storage
from cfdinvest.config import MacroDataConfig
from cfdinvest.ingestion.macro import (
    FRED_REALTIME_MAX,
    FRED_REALTIME_MIN,
    FredError,
    MacroDataIngestor,
    build_observation_params,
    resolve_vintage_mode,
)
from cfdinvest.repository import DataRepository
from cfdinvest.schema import DEFAULT_VINTAGE_MODE, PUBLICATION_DATE
from tests.fred_fixtures import (
    GDPC1_INITIAL_RELEASE,
    GDPC1_LATEST,
    GDPC1_VINTAGE_2020_04_01,
    FredStub,
)


def macro_cfg(**changes) -> MacroDataConfig:
    """A macro config with a key set, plus per-test overrides.

    ``min_request_interval=0`` disables the throttle: these tests exercise
    vintage semantics, not rate limiting, and real sleeps would make the
    suite slow for no coverage. The limiter has its own tests.
    """
    return replace(
        MacroDataConfig(
            api_key="test-key",
            observation_start="2019-01-01",
            min_request_interval=0.0,
        ),
        **changes,
    )


# --------------------------------------------------------------------------- #
# Mode resolution
# --------------------------------------------------------------------------- #
def test_default_mode_is_publication_lag():
    """The global default is point-in-time, not the biased latest."""
    assert resolve_vintage_mode(macro_cfg()) == "publication_lag"
    assert DEFAULT_VINTAGE_MODE == "publication_lag"


def test_default_holds_for_a_bare_config():
    # Not just for the test helper: a plain MacroDataConfig too.
    assert resolve_vintage_mode(MacroDataConfig()) == "publication_lag"


@pytest.mark.parametrize(
    "changes",
    [
        {"realtime_start": "2020-04-01"},
        {"realtime_end": "2020-04-01"},
        {"realtime_start": "2020-04-01", "realtime_end": "2020-04-01"},
    ],
)
def test_realtime_bounds_derive_vintage_mode(changes):
    # Backward compatibility: configs written before vintage_mode existed set
    # only the realtime bounds, and must still be reported accurately.
    assert resolve_vintage_mode(macro_cfg(**changes)) == "vintage"


def test_explicit_mode_wins_over_derivation():
    settings = macro_cfg(vintage_mode="initial_release", realtime_start="2020-04-01")
    assert resolve_vintage_mode(settings) == "initial_release"


def test_mode_is_case_and_whitespace_tolerant():
    assert resolve_vintage_mode(macro_cfg(vintage_mode=" Initial_Release ")) == (
        "initial_release"
    )


def test_unknown_mode_is_rejected():
    with pytest.raises(FredError, match="Unknown vintage_mode"):
        resolve_vintage_mode(macro_cfg(vintage_mode="whatever"))


# --------------------------------------------------------------------------- #
# Parameters are applied EXACTLY as configured
# --------------------------------------------------------------------------- #
def test_latest_mode_sends_no_realtime_parameters():
    params = build_observation_params("GDPC1", macro_cfg(vintage_mode="latest"))
    assert "realtime_start" not in params
    assert "realtime_end" not in params
    assert "output_type" not in params


def test_vintage_mode_forwards_bounds_verbatim():
    settings = macro_cfg(realtime_start="2020-04-01", realtime_end="2020-04-01")
    params = build_observation_params("GDPC1", settings)

    # Exactly as configured: not widened, not defaulted, not shifted.
    assert params["realtime_start"] == "2020-04-01"
    assert params["realtime_end"] == "2020-04-01"
    assert "output_type" not in params


def test_vintage_mode_forwards_a_lone_bound():
    params = build_observation_params("GDPC1", macro_cfg(realtime_start="2015-06-30"))
    assert params["realtime_start"] == "2015-06-30"
    assert "realtime_end" not in params


def test_initial_release_widens_only_unset_bounds():
    params = build_observation_params("GDPC1", macro_cfg(vintage_mode="initial_release"))

    assert params["output_type"] == "4"
    assert params["realtime_start"] == FRED_REALTIME_MIN
    assert params["realtime_end"] == FRED_REALTIME_MAX


def test_initial_release_respects_explicit_bounds():
    settings = macro_cfg(vintage_mode="initial_release", realtime_start="2000-01-01")
    params = build_observation_params("GDPC1", settings)

    assert params["realtime_start"] == "2000-01-01"      # caller's value kept
    assert params["realtime_end"] == FRED_REALTIME_MAX   # only the gap filled


def test_observation_bounds_survive_every_mode():
    settings = macro_cfg(vintage_mode="initial_release", observation_end="2021-01-01")
    params = build_observation_params("GDPC1", settings, observation_start="2020-01-01")
    assert params["observation_start"] == "2020-01-01"
    assert params["observation_end"] == "2021-01-01"


# --------------------------------------------------------------------------- #
# The three modes return materially different numbers
# --------------------------------------------------------------------------- #
def _ingest(config, stub, series, **kwargs):
    with MacroDataIngestor(config, session=stub) as ingestor:
        return ingestor.ingest(series, **kwargs)


def test_biased_mode_returns_revised_values(config):
    cfg = replace(
        config,
        macro=macro_cfg(series_ids=("GDPC1",), vintage_mode="latest"),
    )
    _ingest(cfg, FredStub(), ["GDPC1"])

    values = DataRepository(cfg).load_macro(["GDPC1"])["GDPC1"]
    q4_2019 = values.loc["2019-10-01"]

    assert q4_2019 == pytest.approx(float(GDPC1_LATEST["2019-10-01"]))
    # This is the number a naive backtest would trade on, and it is ~9% above
    # what anyone could actually see at the time.
    assert q4_2019 > float(GDPC1_INITIAL_RELEASE["2019-10-01"][0])


def test_unbiased_vintage_mode_returns_the_as_of_value(config):
    cfg = replace(
        config,
        macro=macro_cfg(
            series_ids=("GDPC1",),
            realtime_start="2020-04-01",
            realtime_end="2020-04-01",
        ),
    )
    _ingest(cfg, FredStub(), ["GDPC1"])

    values = DataRepository(cfg).load_macro(["GDPC1"])["GDPC1"]
    assert values.loc["2019-10-01"] == pytest.approx(
        float(GDPC1_VINTAGE_2020_04_01["2019-10-01"])
    )


def test_initial_release_returns_first_prints_with_publication_dates(config):
    cfg = replace(
        config, macro=macro_cfg(series_ids=("GDPC1",), vintage_mode="initial_release")
    )
    _ingest(cfg, FredStub(), ["GDPC1"])

    stacked = DataRepository(cfg).load_macro(["GDPC1"], wide=False)
    row = stacked.loc["2019-10-01"]

    assert row["value"] == pytest.approx(float(GDPC1_INITIAL_RELEASE["2019-10-01"][0]))
    assert row[PUBLICATION_DATE] == pd.Timestamp("2020-01-30", tz="UTC")


def test_the_three_modes_disagree(config):
    """The whole point: mode choice changes the numbers a backtest sees."""
    seen = {}
    for mode_kwargs, label in [
        ({"vintage_mode": "latest"}, "latest"),
        ({"realtime_start": "2020-04-01", "realtime_end": "2020-04-01"}, "vintage"),
        ({"vintage_mode": "initial_release"}, "initial_release"),
    ]:
        cfg = replace(
            config,
            paths=replace(config.paths, data_root=config.paths.data_root / label),
            macro=macro_cfg(series_ids=("GDPC1",), **mode_kwargs),
        )
        _ingest(cfg, FredStub(), ["GDPC1"])
        seen[label] = DataRepository(cfg).load_macro(["GDPC1"])["GDPC1"].loc["2019-10-01"]

    assert len({round(v, 3) for v in seen.values()}) == 3


# --------------------------------------------------------------------------- #
# Guardrail: modes must not be mixed inside one file
# --------------------------------------------------------------------------- #
def test_repository_exposes_the_vintage_mode_of_each_file(config):
    cfg = replace(
        config, macro=macro_cfg(series_ids=("GDPC1",), vintage_mode="initial_release")
    )
    _ingest(cfg, FredStub(), ["GDPC1"])

    repo = DataRepository(cfg)
    assert repo.macro_vintage_mode("GDPC1") == "initial_release"
    assert repo.macro_coverage().loc["GDPC1", "vintage_mode"] == "initial_release"


def test_vintage_mode_is_recorded_for_latest_too(config):
    cfg = replace(
        config,
        macro=macro_cfg(series_ids=("DGS10",), vintage_mode="latest"),
    )
    _ingest(cfg, FredStub(), ["DGS10"])
    assert DataRepository(cfg).macro_vintage_mode("DGS10") == "latest"


def test_unknown_series_has_no_vintage_mode(config):
    assert DataRepository(config).macro_vintage_mode("NOPE") is None


def test_changing_mode_is_refused_by_default(config):
    latest_cfg = replace(
        config,
        macro=macro_cfg(series_ids=("GDPC1",), vintage_mode="latest"),
    )
    _ingest(latest_cfg, FredStub(), ["GDPC1"])

    pit_cfg = replace(
        config, macro=macro_cfg(series_ids=("GDPC1",), vintage_mode="initial_release")
    )
    report = _ingest(pit_cfg, FredStub(), ["GDPC1"])

    assert report.results[0].status == "failed"
    assert "allow_mode_change" in report.results[0].error
    # The stored file must be untouched: still the latest-revision value.
    values = DataRepository(config).load_macro(["GDPC1"])["GDPC1"]
    assert values.loc["2019-10-01"] == pytest.approx(float(GDPC1_LATEST["2019-10-01"]))


def test_allow_mode_change_replaces_the_file(config):
    latest_cfg = replace(
        config,
        macro=macro_cfg(series_ids=("GDPC1",), vintage_mode="latest"),
    )
    _ingest(latest_cfg, FredStub(), ["GDPC1"])

    pit_cfg = replace(
        config, macro=macro_cfg(series_ids=("GDPC1",), vintage_mode="initial_release")
    )
    report = _ingest(pit_cfg, FredStub(), ["GDPC1"], allow_mode_change=True)

    assert report.results[0].status == "ok"

    repo = DataRepository(pit_cfg)
    values = repo.load_macro(["GDPC1"])["GDPC1"]
    # Replaced, not merged: no revised rows survive alongside the first prints.
    assert values.loc["2019-10-01"] == pytest.approx(
        float(GDPC1_INITIAL_RELEASE["2019-10-01"][0])
    )
    assert repo.macro_vintage_mode("GDPC1") == "initial_release"


def test_same_mode_reingest_is_not_blocked(config):
    cfg = replace(
        config,
        macro=macro_cfg(series_ids=("DGS10",), vintage_mode="latest"),
    )
    _ingest(cfg, FredStub(), ["DGS10"])
    second = _ingest(cfg, FredStub(), ["DGS10"])
    assert second.results[0].status == "ok"


def test_mode_guardrail_ignores_series_with_no_file(config):
    # A manifest entry can exist for a failed series; without a file there is
    # nothing to corrupt, so the run must proceed.
    cfg = replace(
        config,
        macro=macro_cfg(series_ids=("GDPC1",), vintage_mode="latest"),
    )
    _ingest(cfg, FredStub(), ["GDPC1"])
    storage.macro_path(cfg.paths, "GDPC1").unlink()

    pit_cfg = replace(
        config, macro=macro_cfg(series_ids=("GDPC1",), vintage_mode="initial_release")
    )
    assert _ingest(pit_cfg, FredStub(), ["GDPC1"]).results[0].status == "ok"


def test_manifest_records_point_in_time_flag(config):
    cfg = replace(
        config, macro=macro_cfg(series_ids=("GDPC1",), vintage_mode="initial_release")
    )
    report = _ingest(cfg, FredStub(), ["GDPC1"])
    assert report.params["point_in_time"] is True
    assert report.params["vintage_mode"] == "initial_release"
