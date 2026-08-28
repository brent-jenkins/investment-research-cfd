"""Measured spread per bar, and the honesty of saying which bars were measured.

The spread constant is the largest single item in the modelled cost and is
uniform across instruments whose real spreads differ by eight times. Broker
bars carry a bid and an ask, so it can be read instead of assumed -- but only
where a quote exists, and a run that blended measured and assumed figures
without saying so would turn an assumption into an apparent measurement.

M329 a measured spread ignored, so the panel is decorative
M330 a missing quote silently charged as zero, making trading free
M331 measured and estimated bars blended with no way to tell them apart
M332 the panel misaligned, charging one instrument's spread to another
M333 a second fill engine, rather than the one owner extended
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from cfdinvest.execution import fills


def frame(index, columns, values):
    return pd.DataFrame(values, index=index, columns=columns)


@pytest.fixture
def grid():
    index = pd.date_range("2026-08-10", periods=3, freq="h", tz="UTC")
    columns = pd.Index(["AAPL", "MSFT"], name="ticker")
    return index, columns


def test_a_measured_spread_replaces_the_constant(grid):
    """M329: the panel supplied, resolved, and then not actually charged."""
    index, columns = grid
    constant = fills.resolve_spreads(columns, default_bps=5.0).to_numpy()

    panel = frame(index, columns, [[20.0, 1.0]] * 3)
    matrix, measured, total = fills.resolve_spread_matrix(
        index, columns, constant, panel
    )

    assert matrix.shape == (3, 2)
    assert matrix[0, 0] == pytest.approx(20.0 / 10_000), "AAPL kept the constant"
    assert matrix[0, 1] == pytest.approx(1.0 / 10_000), "MSFT kept the constant"
    assert measured == 6 and total == 6


def test_a_missing_quote_falls_back_rather_than_trading_free(grid):
    """M330: the worst data producing the cheapest trades.

    A NaN or a zero is not a tighter market. Charging it as one would make
    exactly the bars with the poorest data the most profitable.
    """
    index, columns = grid
    constant = fills.resolve_spreads(columns, default_bps=5.0).to_numpy()

    panel = frame(index, columns, [[np.nan, 0.0], [-3.0, 8.0], [12.0, np.nan]])
    matrix, measured, total = fills.resolve_spread_matrix(
        index, columns, constant, panel
    )

    assert matrix[0, 0] == pytest.approx(5.0 / 10_000), "NaN was charged"
    assert matrix[0, 1] == pytest.approx(5.0 / 10_000), "a zero spread was charged"
    assert matrix[1, 0] == pytest.approx(5.0 / 10_000), "a negative spread was charged"
    assert matrix[1, 1] == pytest.approx(8.0 / 10_000)
    assert matrix[2, 0] == pytest.approx(12.0 / 10_000)
    assert (matrix > 0).all(), "some bar trades with no spread at all"
    assert measured == 2, f"{measured} cells counted as measured, expected 2"


def test_the_measured_share_is_reported_not_inferred(grid):
    """M331: an assumption presented as a measurement.

    Two runs -- one charging a constant, one charging real quotes -- produce
    identically shaped results. This split is the only thing that tells them
    apart, so it has to be carried out of the engine rather than logged.
    """
    from cfdinvest.execution import engine

    assert engine._spread_coverage(None) == {
        "measured_cells": 0, "total_cells": 0, "measured_fraction": 0.0,
    }
    assert engine._spread_coverage((3, 12))["measured_fraction"] == pytest.approx(0.25)

    index, columns = grid
    constant = fills.resolve_spreads(columns, default_bps=5.0).to_numpy()
    _, measured, total = fills.resolve_spread_matrix(
        index, columns, constant, frame(index, columns, [[7.0, np.nan]] * 3)
    )
    assert engine._spread_coverage((measured, total))["measured_fraction"] == (
        pytest.approx(0.5)
    )


def test_no_panel_reproduces_the_constant_exactly(grid):
    """A run predating the panel must produce bit-for-bit what it did."""
    index, columns = grid
    constant = fills.resolve_spreads(columns, default_bps=5.0).to_numpy()

    matrix, measured, total = fills.resolve_spread_matrix(
        index, columns, constant, None
    )
    assert np.array_equal(matrix, np.tile(constant, (3, 1)))
    assert measured == 0 and total == 6

    empty, measured, _ = fills.resolve_spread_matrix(
        index, columns, constant, pd.DataFrame()
    )
    assert np.array_equal(empty, np.tile(constant, (3, 1)))
    assert measured == 0


def test_the_panel_is_aligned_by_label_not_by_position(grid):
    """M332: one instrument charged another's spread.

    A panel whose columns arrive in a different order, or which covers only
    some of the traded instruments, must line up by name. Positional
    alignment would silently charge INTC's spread to MSFT.
    """
    index, columns = grid
    constant = fills.resolve_spreads(columns, default_bps=5.0).to_numpy()

    # Reversed columns, and an extra instrument that is not traded.
    panel = pd.DataFrame(
        {"MSFT": [1.0, 1.0, 1.0], "NVDA": [99.0] * 3, "AAPL": [30.0] * 3},
        index=index,
    )
    matrix, measured, _ = fills.resolve_spread_matrix(
        index, columns, constant, panel
    )

    assert matrix[0, 0] == pytest.approx(30.0 / 10_000), "AAPL got the wrong column"
    assert matrix[0, 1] == pytest.approx(1.0 / 10_000), "MSFT got the wrong column"
    assert measured == 6


def test_a_panel_covering_only_some_bars_keeps_the_constant_elsewhere(grid):
    index, columns = grid
    constant = fills.resolve_spreads(columns, default_bps=5.0).to_numpy()

    panel = pd.DataFrame([[9.0, 9.0]], index=index[1:2], columns=columns)
    matrix, measured, total = fills.resolve_spread_matrix(
        index, columns, constant, panel
    )

    assert matrix[0].tolist() == pytest.approx([5.0 / 10_000] * 2)
    assert matrix[1].tolist() == pytest.approx([9.0 / 10_000] * 2)
    assert matrix[2].tolist() == pytest.approx([5.0 / 10_000] * 2)
    assert (measured, total) == (2, 6)


def test_the_engine_charges_the_panel_end_to_end():
    """The property that matters: a wider quote costs more money.

    Resolving a matrix nobody charges would satisfy every test above.
    """
    from cfdinvest.execution import engine

    index = pd.date_range("2026-08-10", periods=2, freq="h", tz="UTC")
    columns = pd.Index(["AAPL"], name="ticker")
    constant = fills.resolve_spreads(columns, default_bps=5.0).to_numpy()

    cheap, _, _ = engine.fills_module.resolve_spread_matrix(
        index, columns, constant, frame(index, columns, [[1.0], [1.0]])
    )
    dear, _, _ = engine.fills_module.resolve_spread_matrix(
        index, columns, constant, frame(index, columns, [[50.0], [50.0]])
    )

    traded, price = np.array([10.0]), np.array([100.0])
    cheap_cost, _ = fills.transaction_costs(traded, price, cheap[0], 0.0)
    dear_cost, _ = fills.transaction_costs(traded, price, dear[0], 0.0)

    assert dear_cost[0] > cheap_cost[0] * 10, (
        "a fifty-fold wider quote did not cost proportionally more"
    )
    assert cheap_cost[0] == pytest.approx(1000 * (1.0 / 10_000) / 2)


def test_there_is_still_only_one_fill_engine():
    """M333: a second implementation of fills, free to disagree.

    The house rule is that fills live in ``execution/`` and there is never a
    second. A Capital-specific backtest is a caller of this engine, not a
    copy of it.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "cfdinvest"
    offenders = [
        path for path in root.rglob("*.py")
        if path.name != "fills.py"
        and "def transaction_costs" in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"a second cost engine exists: {offenders}"


def test_the_coverage_reaches_the_frames_metadata():
    """M409: a measured run that reports itself as unmeasured.

    The engine counted the split correctly and `run_backtest` dropped it on
    the floor, so a run charging measured spreads on 99.9% of cells
    reported 0.0%. That is the failure the count exists to prevent, in the
    one place nobody looks: a run charging a constant and one charging
    measured spreads are otherwise identical objects, and this figure is
    the only thing that tells them apart.
    """
    from cfdinvest.execution import ExecutionConfig, run_backtest
    from cfdinvest.preprocessing.frame import AnalysisFrame
    from cfdinvest.signals.frame import SignalFrame

    index = pd.date_range("2026-08-10", periods=40, freq="h", tz="UTC")
    columns = ["AAA", "BBB"]
    price = pd.DataFrame(100.0, index=index, columns=columns)
    signals = SignalFrame(
        direction=pd.DataFrame("long", index=index, columns=columns),
        size=pd.DataFrame(1.0, index=index, columns=columns),
        confidence=pd.DataFrame(0.9, index=index, columns=columns),
        reason=pd.DataFrame("", index=index, columns=columns),
    )
    analysis = AnalysisFrame(
        prices={"open": price, "high": price, "low": price, "close": price}
    )
    panel = pd.DataFrame(20.0, index=index, columns=columns)

    bare = run_backtest(signals, analysis, ExecutionConfig()).metadata
    measured = run_backtest(
        signals, analysis, ExecutionConfig(spread_panel=panel)
    ).metadata

    assert "spread_coverage" in bare and "spread_coverage" in measured, (
        "the frame does not carry the measured/estimated split at all"
    )
    assert bare["spread_coverage"]["measured_fraction"] == 0.0
    assert measured["spread_coverage"]["measured_fraction"] > 0.99, (
        "a fully measured run reported "
        f"{measured['spread_coverage']['measured_fraction']:.3f}"
    )
    assert measured["spread_coverage"]["total_cells"] > 0
    assert (measured["spread_coverage"]["total_cells"]
            == bare["spread_coverage"]["total_cells"])
