"""Simulation: rehearsing the decisions, not the prices.

Two properties carry this layer. It must not become a second backtester —
the project already has one, and two fill models would mean two answers. And
a simulated artefact must never be readable as a real one, because that
failure is unrecoverable: nobody finds out they were wrong about it.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from cfdinvest.config import AppConfig, PathConfig
from cfdinvest.livehelper import LiveHelperConfig
from cfdinvest.persist import simulation as store
from cfdinvest.simulate import (
    SIMULATION_ACCOUNT,
    SimulationConfig,
    SimulationResult,
    apply_advice_to_signals,
    detect_forced_closures,
    run_simulation,
    simulate_advice,
)
from cfdinvest.simulate.controller import SimulationRun

NOW = pd.Timestamp("2026-08-25 12:00", tz="UTC")


@pytest.fixture
def lake(tmp_path) -> AppConfig:
    return AppConfig(paths=PathConfig(data_root=tmp_path / "data"))


@pytest.fixture(scope="module")
def recorded():
    """A real recorded run, built once and shared."""
    import tempfile

    from tests.test_analytics import TICKERS, build_lake
    from cfdinvest.backtest.runner import BacktestConfig
    from cfdinvest.repository import DataRepository
    from cfdinvest.schedule.runner import RunConfig
    from cfdinvest.schedule.runner import run_pipeline as run_scheduled

    directory = tempfile.mkdtemp()
    config = build_lake(Path(directory) / "data", bars=400)
    summary = run_scheduled(
        list(TICKERS),
        RunConfig(backtest=BacktestConfig(interval="1d"), monitoring=False),
        repository=DataRepository(config),
        now=pd.Timestamp("2026-08-25 07:00", tz="UTC"),
    )
    assert summary.status == "ok", summary.error
    return config


def signals_of(config):
    from cfdinvest.persist import readers

    return readers.rebuild_signal_frame(None, None, config)


def analysis_of(config):
    from cfdinvest.persist import readers

    return readers.rebuild_analysis_frame(None, None, config)


def long_analysis_of(config):
    from cfdinvest.persist import readers

    return readers.load_analysis_frame(None, config)


# --------------------------------------------------------------------------- #
# It is not a second backtester
# --------------------------------------------------------------------------- #
MODULES = sorted(Path("cfdinvest/simulate").glob("*.py"))


@pytest.mark.parametrize("module", MODULES, ids=lambda p: p.name)
def test_nothing_here_prices_a_fill(module):
    """The project has one fill model. Two would mean two answers, and the
    day they disagreed neither would be believable."""
    source = module.read_text(encoding="utf-8")
    tree = ast.parse(source)
    defined = {
        node.name for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for name in defined:
        assert "fill_price" not in name
        assert "spread" not in name or name.startswith("_")


def test_the_engine_is_reused_rather_than_reimplemented():
    source = Path("cfdinvest/simulate/engine.py").read_text(encoding="utf-8")
    assert "run_backtest" in source
    assert "from cfdinvest.execution" in source


def test_the_rebuilt_frames_produce_a_working_run(recorded):
    """The rebuild has to be usable. It is not identical -- see below."""
    from cfdinvest.execution.frame import ExecutionConfig, run_backtest
    from cfdinvest.persist import readers

    metadata = readers.load_execution_metadata(None, recorded) or {}
    rebuilt = run_backtest(
        signals_of(recorded), analysis_of(recorded),
        ExecutionConfig(initial_equity=metadata.get("initial_equity", 100_000.0)),
    )
    assert not rebuilt.equity_curve.empty
    assert len(rebuilt.equity_curve) == metadata.get("bars")


def test_the_reconstruction_gap_is_reported_rather_than_hidden(recorded):
    """The honest limit of this layer.

    Some execution settings -- stops, hysteresis, FX -- are written to the
    metadata flattened and cannot be rebuilt into the nested config the engine
    wants. Both curves share whatever settings *were* rebuilt, so comparing
    them is sound; comparing either with the original run is not. Without this
    the user could not tell how much of a difference was the advice and how
    much was the rebuild.
    """
    from cfdinvest.persist import readers

    metadata = readers.load_execution_metadata(None, recorded) or {}
    run = run_simulation(
        recorded, simulation=SimulationConfig(), helper_config=helper(),
        store=False, now=NOW,
    )
    assert run.result.recorded_final_equity == pytest.approx(
        metadata.get("final_equity")
    )
    if run.result.reconstruction_gap and abs(
        run.result.reconstruction_gap
    ) > abs(metadata["final_equity"]) * 0.005:
        assert any(
            "not with the original run" in note for note in run.result.notes
        )


def test_a_material_reconstruction_gap_is_always_reported(recorded):
    """Asserted with a figure that guarantees a gap, rather than hoping the
    fixture drifts. The conditional version passed whether or not the note
    was ever produced."""
    from cfdinvest.execution.frame import ExecutionConfig

    result = simulate_advice(
        signals_of(recorded), analysis_of(recorded),
        ExecutionConfig(initial_equity=100_000.0),
        prices=long_analysis_of(recorded),
        recorded_final_equity=1.0,      # nothing like what the rebuild gives
        helper_config=helper(),
    )
    assert result.reconstruction_gap is not None
    assert any("not with the original run" in note for note in result.notes)


def test_a_matching_reconstruction_says_nothing(recorded):
    """The note is a warning, not a decoration."""
    from cfdinvest.execution.frame import ExecutionConfig

    baseline = simulate_advice(
        signals_of(recorded), analysis_of(recorded),
        ExecutionConfig(initial_equity=100_000.0),
        prices=long_analysis_of(recorded),
        helper_config=helper(),
    )
    ended = float(baseline.baseline_equity.iloc[-1])

    matched = simulate_advice(
        signals_of(recorded), analysis_of(recorded),
        ExecutionConfig(initial_equity=100_000.0),
        prices=long_analysis_of(recorded),
        recorded_final_equity=ended,
        helper_config=helper(),
    )
    assert matched.reconstruction_gap == pytest.approx(0.0)
    assert not any(
        "not with the original run" in note for note in matched.notes
    )


def test_an_unstored_signal_cell_rebuilds_as_flat():
    """Flat cells are not stored. Leaving them NaN would carry a position
    through a bar the strategy had exited — a different strategy, with
    better-looking results."""
    from cfdinvest.persist import readers

    frame = pd.DataFrame(
        {"direction": ["long"], "size": [0.5], "confidence": [0.9],
         "reason": ["x"]},
        index=pd.MultiIndex.from_tuples(
            [(pd.Timestamp("2026-01-02", tz="UTC"), "SPY")],
            names=["timestamp", "ticker"],
        ),
    )
    # A second ticker with no row at all.
    frame = pd.concat([
        frame,
        pd.DataFrame(
            {"direction": ["short"], "size": [0.2], "confidence": [0.5],
             "reason": ["y"]},
            index=pd.MultiIndex.from_tuples(
                [(pd.Timestamp("2026-01-03", tz="UTC"), "GLD")],
                names=["timestamp", "ticker"],
            ),
        ),
    ])
    rebuilt = readers.rebuild_signal_frame(frame)

    assert rebuilt.direction.loc[pd.Timestamp("2026-01-02", tz="UTC"), "GLD"] == "flat"
    assert rebuilt.size.loc[pd.Timestamp("2026-01-02", tz="UTC"), "GLD"] == 0.0


# --------------------------------------------------------------------------- #
# The advice actually changes something
# --------------------------------------------------------------------------- #
def helper(**changes) -> LiveHelperConfig:
    fields = dict(
        account_balance_gbp=100_000.0,
        max_exposure_per_instrument_gbp=20_000.0,
        max_risk_per_trade_gbp=2_000.0,
    )
    fields.update(changes)
    return LiveHelperConfig(**fields)


def test_signal_sizes_are_turned_into_units_before_being_judged(recorded):
    """The signal size is a share of equity; the helper's limits are in units
    and money. Skipping the conversion was the difference between a rehearsal
    that adjusts nothing and one that means something.
    """
    from cfdinvest.execution.frame import ExecutionConfig

    _, adjustments, _ = apply_advice_to_signals(
        signals_of(recorded), long_analysis_of(recorded),
        helper_config=helper(),
        execution_config=ExecutionConfig(initial_equity=100_000.0),
    )
    assert not adjustments.empty
    assert (adjustments["units"] > 1.0).any()


def test_a_tight_limit_cuts_more_than_a_loose_one(recorded):
    from cfdinvest.execution.frame import ExecutionConfig

    def cuts(limit):
        _, adjustments, _ = apply_advice_to_signals(
            signals_of(recorded), long_analysis_of(recorded),
            helper_config=helper(max_exposure_per_instrument_gbp=limit),
            execution_config=ExecutionConfig(initial_equity=100_000.0),
        )
        return len(adjustments)

    assert cuts(5_000.0) >= cuts(50_000.0)


def test_limits_above_what_the_strategy_asks_change_nothing(recorded):
    """Which is itself an answer, and usually means the limits are slack."""
    from cfdinvest.execution.frame import ExecutionConfig

    _, adjustments, _ = apply_advice_to_signals(
        signals_of(recorded), long_analysis_of(recorded),
        helper_config=helper(
            max_exposure_per_instrument_gbp=10_000_000.0,
            max_risk_per_trade_gbp=1_000_000.0,
            max_leverage_per_trade=1_000.0,
            max_margin_usage_pct_after_trade=1.0,
        ),
        execution_config=ExecutionConfig(initial_equity=100_000.0),
    )
    assert adjustments.empty


def test_no_recorded_equity_means_no_adjustment_and_says_so(recorded):
    from cfdinvest.execution.frame import ExecutionConfig

    # ExecutionConfig refuses a zero equity itself, so the only way to reach
    # this path is with no config at all -- which is what a caller that
    # forgot to pass one does.
    _, adjustments, notes = apply_advice_to_signals(
        signals_of(recorded), long_analysis_of(recorded),
        helper_config=helper(),
        execution_config=None,
    )
    assert adjustments.empty
    assert any("could not be turned into units" in note for note in notes)


def test_an_adjustment_only_ever_reduces(recorded):
    """The rehearsal follows advice; the advice can only cut."""
    from cfdinvest.execution.frame import ExecutionConfig

    _, adjustments, _ = apply_advice_to_signals(
        signals_of(recorded), long_analysis_of(recorded),
        helper_config=helper(),
        execution_config=ExecutionConfig(initial_equity=100_000.0),
    )
    assert (adjustments["scale"] <= 1.0).all()
    assert (adjustments["advised_size"] <= adjustments["original_size"]).all()


# --------------------------------------------------------------------------- #
# The comparison is the output
# --------------------------------------------------------------------------- #
def test_the_rehearsal_produces_two_curves(recorded):
    from cfdinvest.execution.frame import ExecutionConfig

    result = simulate_advice(
        signals_of(recorded), analysis_of(recorded),
        ExecutionConfig(initial_equity=100_000.0),
        prices=long_analysis_of(recorded),
        helper_config=helper(),
    )
    assert result.ok
    assert not result.baseline_equity.empty
    assert not result.advised_equity.empty


def test_following_the_advice_changes_the_result(recorded):
    from cfdinvest.execution.frame import ExecutionConfig

    result = simulate_advice(
        signals_of(recorded), analysis_of(recorded),
        ExecutionConfig(initial_equity=100_000.0),
        prices=long_analysis_of(recorded),
        helper_config=helper(max_exposure_per_instrument_gbp=5_000.0),
    )
    assert result.difference("final_equity") != 0
    assert "£" in result.headline


def test_identical_curves_are_reported_as_an_answer(recorded):
    from cfdinvest.execution.frame import ExecutionConfig

    result = simulate_advice(
        signals_of(recorded), analysis_of(recorded),
        ExecutionConfig(initial_equity=100_000.0),
        prices=long_analysis_of(recorded),
        helper_config=helper(
            max_exposure_per_instrument_gbp=10_000_000.0,
            max_risk_per_trade_gbp=1_000_000.0,
            max_leverage_per_trade=1_000.0,
            max_margin_usage_pct_after_trade=1.0,
        ),
    )
    assert any("itself an answer" in note for note in result.notes)


def test_no_signals_is_refused_rather_than_returning_a_flat_curve():
    from cfdinvest.execution.frame import ExecutionConfig

    result = simulate_advice(None, None, ExecutionConfig())
    assert not result.ok
    assert "no signals" in result.error


def test_a_difference_needs_both_sides():
    result = SimulationResult(label="x", advised_metrics={"final_equity": 1.0})
    assert result.difference("final_equity") is None


# --------------------------------------------------------------------------- #
# Forced closures are reported, not applied
# --------------------------------------------------------------------------- #
class _Frame:
    def __init__(self, positions, equity):
        self.positions = positions
        self.equity_curve = equity


def test_crossing_the_brokers_ratio_is_found():
    index = pd.date_range("2026-01-01", periods=5, tz="UTC")
    frame = _Frame(
        pd.DataFrame({"SPY": [1_000.0] * 5}, index=index),
        pd.Series([100.0] * 5, index=index),
    )
    found = detect_forced_closures(
        frame, config=SimulationConfig(margin_call_ratio=0.9),
        account_balance=100.0, instrument_leverage=10.0,
    )
    assert len(found) == 5


def test_a_quiet_book_crosses_nothing():
    index = pd.date_range("2026-01-01", periods=5, tz="UTC")
    frame = _Frame(
        pd.DataFrame({"SPY": [10.0] * 5}, index=index),
        pd.Series([100_000.0] * 5, index=index),
    )
    assert detect_forced_closures(
        frame, account_balance=100_000.0, instrument_leverage=10.0
    ).empty


def test_no_leverage_means_no_imaginary_margin_calls():
    """An assumed leverage would produce a list of margin calls that never
    happened."""
    index = pd.date_range("2026-01-01", periods=3, tz="UTC")
    frame = _Frame(
        pd.DataFrame({"SPY": [1_000_000.0] * 3}, index=index),
        pd.Series([100.0] * 3, index=index),
    )
    assert detect_forced_closures(frame, instrument_leverage=None).empty


def test_forced_closures_are_reported_not_applied(recorded):
    """Closing a position mid-path would change every bar after it, and the
    two curves would stop being comparable."""
    from cfdinvest.execution.frame import ExecutionConfig

    result = simulate_advice(
        signals_of(recorded), analysis_of(recorded),
        ExecutionConfig(initial_equity=100_000.0),
        prices=long_analysis_of(recorded),
        helper_config=helper(),
    )
    if result.forced_closures.empty:
        pytest.skip("this record never crosses the ratio")
    assert any("not applied" in note for note in result.notes)


def test_an_absurd_margin_call_ratio_is_refused():
    with pytest.raises(ValueError, match="fraction in"):
        SimulationConfig(margin_call_ratio=1.5)


# --------------------------------------------------------------------------- #
# A rehearsal must never read as a real result
# --------------------------------------------------------------------------- #
def a_run(**changes) -> SimulationRun:
    fields = dict(
        result=SimulationResult(
            label="as advised",
            baseline_metrics={"final_equity": 100.0},
            advised_metrics={"final_equity": 90.0},
            advised_equity=pd.Series([90.0]),
        ),
        timestamp=NOW,
        run_id="20260825T070000Z",
    )
    fields.update(changes)
    return SimulationRun(**fields)


def test_a_simulation_document_is_marked_before_anything_else():
    """Whatever reads this sees it first."""
    document = a_run().to_document()
    assert next(iter(document)) == "simulated"
    assert document["simulated"] is True
    assert "not your results" in document["disclaimer"]


def test_a_simulation_is_written_to_its_own_tree(lake):
    path = store.write_simulation(a_run(), lake)
    assert SIMULATION_ACCOUNT in Path(path).parts


def test_a_simulation_never_lands_where_a_real_run_goes(lake):
    from cfdinvest.persist import readers

    store.write_simulation(a_run(), lake)
    assert readers.list_runs(lake) == []
    assert readers.list_runs(lake, "virtual") == []
    assert readers.list_runs(lake, "live") == []


def test_the_store_cannot_be_pointed_at_a_real_tree():
    """A caller that could choose where simulated results went would
    eventually choose wrong."""
    import inspect

    assert "account" not in inspect.signature(store.simulation_root).parameters
    assert "account" not in inspect.signature(store.write_simulation).parameters


def test_a_plain_mapping_still_comes_out_marked(lake):
    """Belt and braces: the controller sets this, and so does the store."""
    path = store.write_simulation({"timestamp": NOW.isoformat()}, lake, now=NOW)
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    assert document["simulated"] is True


def test_simulations_round_trip(lake):
    store.write_simulation(a_run(), lake)
    document = store.load_latest_simulation(lake)
    assert document["run_id"] == "20260825T070000Z"


def test_simulations_accumulate(lake):
    for hour in (9, 10, 11):
        store.write_simulation(
            a_run(timestamp=pd.Timestamp(f"2026-08-25 {hour}:00", tz="UTC")), lake
        )
    assert len(store.list_simulations(lake)) == 3


def test_two_in_the_same_second_do_not_overwrite(lake):
    store.write_simulation(a_run(), lake)
    store.write_simulation(a_run(), lake)
    assert len(store.list_simulations(lake)) == 2


def test_a_failed_write_returns_none(lake, monkeypatch):
    monkeypatch.setattr(
        store, "atomic_write_text",
        lambda *a, **k: (_ for _ in ()).throw(OSError("no space")),
    )
    assert store.write_simulation(a_run(), lake) is None


def test_no_simulations_is_an_empty_history(lake):
    assert store.load_latest_simulation(lake) is None
    assert store.load_simulation_history(lake).empty


def test_the_history_marks_every_row_simulated(lake):
    store.write_simulation(a_run(), lake)
    history = store.load_simulation_history(lake)
    assert bool(history["simulated"].all())


# --------------------------------------------------------------------------- #
# Running over a real lake
# --------------------------------------------------------------------------- #
def test_a_full_run_rehearses_and_derives(recorded):
    run = run_simulation(
        recorded, simulation=SimulationConfig(), helper_config=helper(),
        store=False, now=NOW,
    )
    assert run.ok
    assert run.insights is not None
    assert run.bias is not None


def test_the_derived_findings_are_about_the_strategy_not_the_person(recorded):
    """A trade record cannot support a model of a person changing over time.
    What is done instead is narrower and real."""
    run = run_simulation(
        recorded, simulation=SimulationConfig(), helper_config=helper(),
        store=False, now=NOW,
    )
    assert any("not a forecast about you" in note for note in run.notes)


def test_an_empty_lake_says_there_is_nothing_to_rehearse(lake):
    run = run_simulation(lake, store=False, now=NOW)
    assert not run.ok
    assert "no run has been recorded" in run.result.error


def test_the_source_run_is_left_untouched(recorded):
    """It reads the pipeline's artefacts and writes to its own tree."""
    from cfdinvest.persist import readers

    before = readers.list_runs(recorded)
    run_simulation(recorded, helper_config=helper(), store=True, now=NOW)
    assert readers.list_runs(recorded) == before


@pytest.mark.parametrize("module", MODULES, ids=lambda p: p.name)
def test_the_simulation_layer_reaches_no_broker(module):
    tree = ast.parse(module.read_text(encoding="utf-8"))
    imported = {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    forbidden = ("requests", "urllib", "httpx", "selenium",
                 "cfdinvest.schedule.runner")
    assert not [
        name for name in imported
        if any(name == bad or name.startswith(bad + ".") for bad in forbidden)
    ]


# --------------------------------------------------------------------------- #
# The digest
# --------------------------------------------------------------------------- #
def test_the_digest_says_it_is_a_rehearsal_first():
    from cfdinvest.simulate.digest import build_simulation_digest

    message = build_simulation_digest(a_run())
    body = message.get_content()

    assert "SIMULATION" in message["Subject"]
    assert body.splitlines()[0].startswith("SIMULATION")
    assert "not your results" in body


def test_the_digest_says_a_rehearsal_is_not_a_forecast():
    """Following the advice costing money on one run is not evidence that it
    would on the next."""
    from cfdinvest.simulate.digest import build_simulation_digest

    body = build_simulation_digest(a_run()).get_content()
    assert "not a forecast" in body


def test_a_failed_rehearsal_digests_cleanly():
    from cfdinvest.simulate.digest import build_simulation_digest

    run = a_run(result=SimulationResult(label="x", error="no signals"))
    body = build_simulation_digest(run).get_content()
    assert "did not complete" in body
