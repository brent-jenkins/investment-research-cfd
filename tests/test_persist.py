"""Run-artefact persistence: layout, schema, writes, reads, scheduler state.

The properties worth asserting here are about **not losing things**. A write
that fails must not take the run down with it; a rerun must not delete what
the previous attempt recorded; a corrupt file must read as absent rather than
raise in a web request. Those are the failures that only show up months later,
on an unattended machine, when the record is the only thing left.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from cfdinvest import persist
from cfdinvest.config import AppConfig, PathConfig
from cfdinvest.execution.frame import ExecutionFrame
from cfdinvest.persist import readers, schema, scheduler_state, storage, writers

RUN_ID = "20260115T090000Z"


@pytest.fixture
def lake(tmp_path) -> AppConfig:
    return AppConfig(paths=PathConfig(data_root=tmp_path / "data"))


def pnl_frame(bars: int = 40, seed: int = 2) -> pd.DataFrame:
    """A P&L frame shaped exactly like the engine's."""
    rng = np.random.default_rng(seed)
    index = pd.bdate_range("2026-01-01", periods=bars, tz="UTC", name="timestamp")
    parts = {
        "gap": rng.normal(5, 40, bars),
        "intraday": rng.normal(3, 30, bars),
        "spread": -np.abs(rng.normal(2, 1, bars)),
        "slippage": -np.abs(rng.normal(0.5, 0.2, bars)),
        "financing": -np.abs(rng.normal(1, 0.3, bars)),
        "fx": -np.abs(rng.normal(0.8, 0.2, bars)),
    }
    frame = pd.DataFrame(parts, index=index)
    frame["total"] = frame.sum(axis=1)
    return frame


@pytest.fixture
def execution() -> ExecutionFrame:
    pnl = pnl_frame()
    index = pnl.index
    return ExecutionFrame(
        positions=pd.DataFrame({"SPY": np.linspace(0, 10, len(index))}, index=index),
        pnl=pnl,
        equity_curve=pd.Series(
            100_000 + pnl["total"].cumsum(), index=index, name="equity"
        ),
        trades=pd.DataFrame(
            {
                "timestamp": [index[5], index[20]],
                "asset": ["SPY", "SPY"],
                "action": ["open_long", "close"],
                "units": [10.0, 0.0],
                "traded_units": [10.0, -10.0],
                "price": [100.0, 105.0],
                "fill_price": [100.05, 104.95],
                "spread_cost": [1.0, 1.0],
                "slippage": [0.0, 0.0],
                "financing": [0.0, 2.0],
                "realized_pnl": [0.0, 48.0],
                "notional": [1000.0, 0.0],
                "equity": [100_000.0, 100_048.0],
                "reason": ["signal", "signal"],
            }
        ),
        metadata={"initial_equity": 100_000.0, "max_leverage": 5.0},
    )


# --------------------------------------------------------------------------- #
# Layout
# --------------------------------------------------------------------------- #
def test_an_explicit_config_wins_over_the_environment(lake, monkeypatch):
    monkeypatch.setenv("CFD_DATA_ROOT", "/somewhere/else")
    assert storage.data_root(lake) == Path(lake.paths.data_root)


def test_the_environment_wins_over_the_platform_default(tmp_path, monkeypatch):
    monkeypatch.setenv("CFD_DATA_ROOT", str(tmp_path / "chosen"))
    assert storage.data_root() == tmp_path / "chosen"


def test_an_unconfigured_install_has_a_platform_default(monkeypatch):
    monkeypatch.delenv("CFD_DATA_ROOT", raising=False)
    root = storage.data_root()
    assert root.name == "cfdinvest"
    assert root.is_absolute()


def test_runs_live_beside_the_lake_not_in_a_second_tree(lake):
    """One run store: two would give every reader two answers."""
    assert storage.runs_root(lake) == Path(lake.paths.data_root) / "runs"


def test_a_run_directory_is_not_created_by_asking_where_it_is(lake):
    directory = storage.get_run_directory(RUN_ID, lake)
    assert not directory.exists()


def test_ensuring_a_run_directory_creates_it(lake):
    directory = storage.ensure_run_directory(RUN_ID, lake)
    assert directory.is_dir()
    assert directory.name == RUN_ID


def test_ensuring_twice_never_clears_what_is_there(lake):
    """A retry must not destroy the record of the attempt it was retrying."""
    directory = storage.ensure_run_directory(RUN_ID, lake)
    (directory / "keep.txt").write_text("evidence", encoding="utf-8")

    storage.ensure_run_directory(RUN_ID, lake)
    assert (directory / "keep.txt").read_text(encoding="utf-8") == "evidence"


def test_runs_are_listed_newest_first(lake):
    for run_id in ("20260101T000000Z", "20260301T000000Z", "20260201T000000Z"):
        storage.ensure_run_directory(run_id, lake)

    assert readers.list_runs(lake) == [
        "20260301T000000Z", "20260201T000000Z", "20260101T000000Z",
    ]
    assert readers.load_latest_run(lake) == "20260301T000000Z"


def test_no_runs_is_an_empty_list_not_an_error(lake):
    assert readers.list_runs(lake) == []
    assert readers.load_latest_run(lake) is None


def test_an_account_inserts_one_path_segment(lake):
    """Anticipated but off: None resolves to today's layout."""
    assert storage.runs_root(lake, None) == Path(lake.paths.data_root) / "runs"
    assert storage.runs_root(lake, "live") == (
        Path(lake.paths.data_root) / "live" / "runs"
    )


def test_an_unknown_account_is_refused(lake):
    with pytest.raises(ValueError, match="unknown account"):
        storage.runs_root(lake, "imaginary")


def test_a_run_id_is_a_utc_timestamp():
    assert storage.generate_run_id(
        pd.Timestamp("2026-01-15 09:00", tz="UTC")
    ) == RUN_ID


# --------------------------------------------------------------------------- #
# Atomic writes
# --------------------------------------------------------------------------- #
def test_a_write_leaves_no_temporary_behind(tmp_path):
    path = tmp_path / "thing.json"
    storage.atomic_write_text('{"a": 1}', path)

    assert path.read_text(encoding="utf-8") == '{"a": 1}'
    assert list(tmp_path.glob("*.tmp")) == []


def test_the_temporary_sits_in_the_same_directory(tmp_path, monkeypatch):
    """A rename across filesystems is a copy, and loses atomicity."""
    seen: list[Path] = []
    real = os.replace

    def watched(source, target):
        seen.append(Path(source).parent)
        return real(source, target)

    monkeypatch.setattr(os, "replace", watched)
    storage.atomic_write_text("x", tmp_path / "nested" / "thing.txt")

    assert seen == [tmp_path / "nested"]


def test_writing_creates_missing_parents(tmp_path):
    path = tmp_path / "a" / "b" / "c.txt"
    storage.atomic_write_text("x", path)
    assert path.is_file()


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #
def test_every_artefact_has_a_spec_and_a_filename():
    for name, spec in schema.ARTEFACTS.items():
        assert spec.name == name
        assert spec.filename
        assert spec.kind in ("parquet", "json")
        assert spec.description


def test_an_unknown_artefact_lists_the_known_ones():
    """A typo would otherwise write a file nothing ever reads."""
    with pytest.raises(KeyError, match="known artefacts"):
        schema.spec_for("not_an_artefact")


def test_missing_required_columns_are_reported():
    frame = pd.DataFrame({"gap": [1.0], "total": [1.0]})
    missing = schema.missing_columns(frame, "pnl")

    assert "intraday" in missing
    assert "gap" not in missing


def test_extra_columns_are_fine():
    """The pipeline grows columns; a schema that refused them would break."""
    frame = pnl_frame().assign(something_new=1.0).reset_index()
    assert schema.missing_columns(frame, "pnl") == ()


def test_an_empty_frame_is_exempt():
    """"The run produced no trades" is a valid outcome, not a failure."""
    assert schema.missing_columns(pd.DataFrame(), "trades") == ()


def test_strict_validation_raises():
    with pytest.raises(ValueError, match="missing required column"):
        schema.validate_frame(pd.DataFrame({"gap": [1.0]}), "pnl", strict=True)


def test_the_registry_renders_as_a_table():
    table = schema.describe_run()
    assert "pnl" in table.index
    assert "filename" in table.columns


# --------------------------------------------------------------------------- #
# Writing and reading
# --------------------------------------------------------------------------- #
def test_a_run_round_trips(lake, execution):
    directory = storage.ensure_run_directory(RUN_ID, lake)
    metadata = persist.build_run_metadata(
        RUN_ID, started_at="2026-01-15T09:00:00Z",
        finished_at="2026-01-15T09:00:42Z", tickers=["SPY"], bars=40,
        warnings=["a synthetic warning"],
    )
    written = writers.write_all(
        directory, execution_frame=execution, run_metadata=metadata
    )

    assert {"metadata", "pnl", "trades", "positions", "equity"} <= set(written)
    assert readers.list_runs(lake) == [RUN_ID]

    read = readers.load_run_metadata(RUN_ID, lake)
    assert read["run_id"] == RUN_ID
    assert read["duration_seconds"] == pytest.approx(42.0)
    assert readers.load_run_warnings(RUN_ID, lake) == ["a synthetic warning"]


def test_the_pnl_identity_survives_the_round_trip(lake, execution):
    """The artefact this layer exists for."""
    directory = storage.ensure_run_directory(RUN_ID, lake)
    writers.write_pnl_frame(directory, execution)

    read = readers.load_pnl_frame(RUN_ID, lake)
    parts = ["gap", "intraday", "spread", "slippage", "financing", "fx"]

    assert len(read) == 40
    assert np.allclose(read[parts].sum(axis=1), read["total"])


def test_execution_metadata_records_what_the_run_used(lake, execution):
    directory = storage.ensure_run_directory(RUN_ID, lake)
    writers.write_execution_metadata(directory, execution)

    read = readers.load_execution_metadata(RUN_ID, lake)
    assert read["initial_equity"] == 100_000.0
    assert read["max_leverage"] == 5.0


def test_regimes_are_written_as_one_column_per_classifier(lake):
    """They are market-level Series, not per-instrument panels."""
    index = pd.bdate_range("2026-01-01", periods=10, tz="UTC")

    class Frame:
        volatility_regime = pd.Series(["low"] * 10, index=index)
        trend_regime = pd.Series(["uptrend"] * 10, index=index)
        correlation_regime = pd.Series(dtype="object")
        macro_regime = pd.Series(dtype="object")
        combined_regime = pd.Series(["risk_on"] * 10, index=index)

    directory = storage.ensure_run_directory(RUN_ID, lake)
    writers.write_regime_frame(directory, Frame())

    read = readers.load_regime_frame(RUN_ID, lake)
    assert len(read) == 10
    assert "volatility_regime" in read.columns
    assert "correlation_regime" not in read.columns, "an empty series is not a column"


def test_panels_of_different_shapes_all_stack(lake):
    """Correlations already carry a MultiIndex; assuming two levels broke it."""
    index = pd.bdate_range("2026-01-01", periods=5, tz="UTC")
    pairs = pd.MultiIndex.from_product([index, ["SPY", "GLD"]])

    class Analysis:
        prices = {"close": pd.DataFrame({"SPY": range(5), "GLD": range(5)},
                                        index=index)}
        returns = {}
        volatility = {}
        correlations = {
            "rolling": pd.DataFrame(
                {"SPY": np.zeros(10), "GLD": np.zeros(10)}, index=pairs
            )
        }

    # The MultiIndex property is asserted against `_to_long` DIRECTLY,
    # because `write_analysis_frame` no longer passes correlations to it --
    # see below. The bug this test was written for lives in `_to_long`, and
    # testing it through a caller that stopped calling it would have quietly
    # stopped testing anything.
    long = writers._to_long(
        {"prices.close": Analysis.prices["close"],
         "correlations.rolling": Analysis.correlations["rolling"]},
        "value",
    )
    assert set(long.columns) >= {"timestamp", "panel", "series", "value"}
    assert {"prices.close", "correlations.rolling"} <= set(long["panel"])
    assert any("|" in name for name in long["series"]), "a pair should be joined"

    directory = storage.ensure_run_directory(RUN_ID, lake)
    writers.write_analysis_frame(directory, Analysis())

    read = readers.load_analysis_frame(RUN_ID, lake)
    assert set(read.columns) >= {"timestamp", "panel", "series", "value"}
    assert "prices.close" in set(read["panel"])


def test_correlations_are_not_written_to_the_analysis_artefact(lake):
    """Quadratic in the universe, and it stalled the live daemon.

    A rolling correlation panel is every asset against every other: 109
    instruments is 11,881 series per bar, and over 6,400 bars roughly 76
    million values, each given a string label by `_to_long`. On 2026-09-11
    the pipeline entered this call at 13:35 and was still in it at 14:24, in
    uninterruptible sleep, thrashing against its memory cgroup -- RSS 3.55GB
    at 14:19 and 5.59GB four minutes later. `trade-capital` runs after
    `run-intraday` in the same tick, so the account did not deal at all that
    session, and nothing alerted because the daemon was alive and busy.

    Nothing is lost but a sweep-time convenience: correlations are derived
    from `returns`, which IS written, so a consumer can recompute them.

    Asserted through the artefact rather than the constant, so renaming or
    reordering `ANALYSIS_PANEL_GROUPS` cannot make this pass while the panel
    comes back.
    """
    index = pd.bdate_range("2026-01-01", periods=5, tz="UTC")
    pairs = pd.MultiIndex.from_product([index, ["SPY", "GLD"]])

    class Analysis:
        prices = {"close": pd.DataFrame({"SPY": range(5), "GLD": range(5)},
                                        index=index)}
        returns = {"simple": pd.DataFrame({"SPY": range(5), "GLD": range(5)},
                                          index=index)}
        volatility = {}
        correlations = {
            "rolling": pd.DataFrame(
                {"SPY": np.zeros(10), "GLD": np.zeros(10)}, index=pairs
            ),
            "average": pd.DataFrame({"average": np.zeros(5)}, index=index),
        }

    directory = storage.ensure_run_directory(RUN_ID, lake)
    writers.write_analysis_frame(directory, Analysis())
    read = readers.load_analysis_frame(RUN_ID, lake)

    panels = set(read["panel"])
    assert not any(p.startswith("correlations") for p in panels), (
        f"a correlation panel reached the artefact: {sorted(panels)}"
    )
    assert {"prices.close", "returns.simple"} <= panels, (
        "the cheap panels must still be written; dropping the artefact "
        "entirely was not the fix"
    )


def test_an_empty_frame_is_not_written(lake):
    """Absent says "none of these", and costs nothing to check."""
    directory = storage.ensure_run_directory(RUN_ID, lake)
    assert writers.write_frame(pd.DataFrame(), directory, "pnl") is None
    assert readers.load_pnl_frame(RUN_ID, lake).empty


def test_reading_a_run_that_does_not_exist(lake):
    assert readers.load_pnl_frame("NOPE", lake).empty
    assert readers.load_run_metadata("NOPE", lake) is None
    assert readers.load_run_warnings("NOPE", lake) == []


def test_a_corrupt_document_reads_as_absent_not_as_a_crash(lake):
    """This feeds a dashboard whose job includes reporting breakage."""
    directory = storage.ensure_run_directory(RUN_ID, lake)
    (directory / "metadata.json").write_text("{not json", encoding="utf-8")

    assert readers.load_run_metadata(RUN_ID, lake) is None


def test_a_corrupt_frame_reads_as_empty(lake):
    directory = storage.ensure_run_directory(RUN_ID, lake)
    (directory / "pnl.parquet").write_bytes(b"not parquet at all")

    assert readers.load_pnl_frame(RUN_ID, lake).empty


def test_a_write_failure_does_not_take_the_run_down(lake, execution, monkeypatch):
    """Failing to record a run must not also destroy it."""
    monkeypatch.setattr(
        writers.lake_storage, "write_parquet",
        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
    )
    directory = storage.ensure_run_directory(RUN_ID, lake)
    written = writers.write_all(
        directory,
        execution_frame=execution,
        run_metadata=persist.build_run_metadata(RUN_ID),
    )
    assert "metadata" in written, "the document still lands"


def test_artefact_status_says_what_is_present(lake, execution):
    directory = storage.ensure_run_directory(RUN_ID, lake)
    writers.write_pnl_frame(directory, execution)

    status = readers.artefact_status(RUN_ID, lake)
    assert status.loc["pnl", "present"] is np.True_ or status.loc["pnl", "present"]
    assert not status.loc["regimes", "present"]


def test_the_reader_defaults_to_the_newest_run(lake, execution):
    for run_id in ("20260101T000000Z", "20260301T000000Z"):
        directory = storage.ensure_run_directory(run_id, lake)
        writers.write_run_metadata(
            directory, persist.build_run_metadata(run_id)
        )

    assert readers.load_run_metadata(None, lake)["run_id"] == "20260301T000000Z"


def test_the_scheduler_summary_reads_without_a_payload_wrapper(lake):
    """The scheduler's own summary.json is a bare document."""
    directory = storage.ensure_run_directory(RUN_ID, lake)
    (directory / "summary.json").write_text(
        json.dumps({"run_id": RUN_ID, "status": "ok"}), encoding="utf-8"
    )
    assert readers.load_document("summary", RUN_ID, lake)["status"] == "ok"


def test_nan_and_infinity_serialise_as_null():
    """JSON cannot spell them and every parser disagrees about what to do."""
    payload = writers.jsonable({"a": float("nan"), "b": float("inf"), "c": 1.5})
    assert payload["a"] is None
    assert payload["b"] is None
    assert payload["c"] == 1.5
    json.dumps(payload)


def test_writing_is_deterministic(lake, execution):
    """The same run written twice produces the same bytes."""
    first = storage.ensure_run_directory("A", lake)
    second = storage.ensure_run_directory("B", lake)
    metadata = persist.build_run_metadata(
        "X", started_at="2026-01-15T09:00:00Z", finished_at="2026-01-15T09:00:42Z"
    )
    writers.write_run_metadata(first, metadata)
    writers.write_run_metadata(second, metadata)

    assert (first / "metadata.json").read_bytes() == (
        second / "metadata.json"
    ).read_bytes()


# --------------------------------------------------------------------------- #
# Run metadata
# --------------------------------------------------------------------------- #
def test_duration_is_computed_not_taken():
    """So it cannot disagree with the timestamps beside it."""
    metadata = persist.build_run_metadata(
        RUN_ID, started_at="2026-01-15T09:00:00Z",
        finished_at="2026-01-15T09:01:30Z",
    )
    assert metadata.duration_seconds == pytest.approx(90.0)


def test_a_run_with_no_end_has_no_duration():
    assert persist.build_run_metadata(RUN_ID).duration_seconds is None


def test_naive_timestamps_are_read_as_utc():
    metadata = persist.build_run_metadata(RUN_ID, started_at="2026-01-15T09:00:00")
    assert metadata.start_time.endswith("+00:00")


def test_the_config_snapshot_redacts_credentials():
    """This document is read by a web page."""
    from cfdinvest.config import AppConfig

    metadata = persist.build_run_metadata(RUN_ID, config=AppConfig())
    for key, value in metadata.config_snapshot.items():
        if "key" in key.lower() and value:
            assert "redacted" in str(value)


def test_a_failed_run_is_not_ok():
    assert not persist.build_run_metadata(
        RUN_ID, status="failed", error="it broke"
    ).ok


# --------------------------------------------------------------------------- #
# Scheduler state
# --------------------------------------------------------------------------- #
def test_no_state_file_is_a_complete_empty_state(lake):
    """A caller should not have to guard every key."""
    state = scheduler_state.load_scheduler_state(lake)
    assert set(state) == set(scheduler_state.EMPTY_STATE)
    assert state["last_run"] is None


def test_a_start_is_recorded_before_anything_can_fail(lake):
    state = scheduler_state.update_last_run(
        "2026-01-15T09:00:00Z", run_id=RUN_ID, config=lake
    )
    assert state["last_run"].startswith("2026-01-15T09:00")
    assert state["last_run_id"] == RUN_ID
    assert state["runs"] == 1
    assert state["last_success"] is None, "starting is not succeeding"


def test_a_run_that_started_and_did_not_finish_is_visible(lake):
    """The signal deriving last-run from directories could never give."""
    scheduler_state.update_last_run("2026-01-15T09:00:00Z", config=lake)
    state = scheduler_state.load_scheduler_state(lake)

    assert state["last_run"] is not None
    assert state["last_success"] is None


def test_success_clears_a_stale_error(lake):
    """A fortnight-old error beside this morning's success is noise."""
    scheduler_state.update_last_error("something broke", config=lake)
    assert scheduler_state.load_scheduler_state(lake)["last_error"]

    state = scheduler_state.update_last_success(
        "2026-01-15T09:00:42Z", duration=42.0, config=lake
    )
    assert state["last_error"] is None
    assert state["last_duration"] == pytest.approx(42.0)


def test_failures_are_counted(lake):
    scheduler_state.update_last_error("one", config=lake)
    scheduler_state.update_last_error("two", config=lake)

    state = scheduler_state.load_scheduler_state(lake)
    assert state["failures"] == 2
    assert state["last_error"] == "two"


def test_an_error_is_truncated_not_unbounded(lake):
    state = scheduler_state.update_last_error("x" * 9_000, config=lake)
    assert len(state["last_error"]) <= 2_000


def test_an_error_can_be_cleared_by_hand(lake):
    scheduler_state.update_last_error("broke", config=lake)
    assert scheduler_state.clear_last_error(config=lake)["last_error"] is None


def test_corrupt_state_reads_as_empty_rather_than_raising(lake):
    path = storage.ensure_scheduler_state(lake)
    path.write_text("{not json", encoding="utf-8")

    assert scheduler_state.load_scheduler_state(lake)["last_run"] is None


def test_the_state_file_is_not_created_by_reading(lake):
    """Absent means "nothing has run", which is worth being able to say."""
    storage.ensure_scheduler_state(lake)
    assert not storage.get_scheduler_state_path(lake).exists()


# --------------------------------------------------------------------------- #
# The runner writes all of it
# --------------------------------------------------------------------------- #
def test_the_runner_persists_every_artefact_and_records_state(tmp_path):
    """The integration this layer exists for, end to end on a real pipeline."""
    from tests.test_analytics import TICKERS, build_lake
    from cfdinvest.backtest.runner import BacktestConfig
    from cfdinvest.repository import DataRepository
    from cfdinvest.schedule.runner import RunConfig
    from cfdinvest.schedule.runner import run_pipeline as run_scheduled

    config = build_lake(tmp_path / "data", bars=260)
    summary = run_scheduled(
        list(TICKERS),
        RunConfig(backtest=BacktestConfig(interval="1d")),
        repository=DataRepository(config),
        now=pd.Timestamp("2026-08-25 07:00", tz="UTC"),
    )
    assert summary.status == "ok", summary.error

    present = readers.artefact_status(summary.run_id, config)
    for artefact in ("pnl", "metadata", "execution_metadata", "trades", "equity"):
        assert present.loc[artefact, "present"], f"{artefact} was not written"

    pnl = readers.load_pnl_frame(summary.run_id, config)
    parts = ["gap", "intraday", "spread", "slippage", "financing", "fx"]
    assert np.allclose(pnl[parts].sum(axis=1), pnl["total"])

    state = scheduler_state.load_scheduler_state(config)
    assert state["last_success"] is not None
    assert state["last_error"] is None
    assert state["last_run_id"] == summary.run_id


def test_a_failing_run_records_the_error(tmp_path):
    from cfdinvest.backtest.runner import BacktestConfig
    from cfdinvest.repository import DataRepository
    from cfdinvest.schedule.runner import RunConfig
    from cfdinvest.schedule.runner import run_pipeline as run_scheduled

    config = AppConfig(paths=PathConfig(data_root=tmp_path / "empty"))
    summary = run_scheduled(
        ["NOT_IN_THE_LAKE"],
        RunConfig(backtest=BacktestConfig(interval="1d")),
        repository=DataRepository(config),
        now=pd.Timestamp("2026-08-25 07:00", tz="UTC"),
    )
    state = scheduler_state.load_scheduler_state(config)

    assert state["last_run"] is not None, "the start is recorded regardless"
    if summary.status == "failed":
        assert state["last_error"] is not None
    else:
        assert summary.status == "empty"
