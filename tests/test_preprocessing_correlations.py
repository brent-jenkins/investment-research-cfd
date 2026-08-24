"""Rolling correlation structure and breakdown diagnostics."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from cfdinvest.preprocessing.correlations import (
    BREAKDOWN_COLUMNS,
    average_pairwise_correlation,
    correlation_breakdown_flags,
    correlation_matrix,
    correlation_tensor,
    pairwise_correlations,
    rolling_correlation,
)
from cfdinvest.schema import TICKER, TIMESTAMP


@pytest.fixture
def calendar() -> pd.DatetimeIndex:
    return pd.bdate_range("2024-01-01", periods=60, tz="UTC", name=TIMESTAMP)


@pytest.fixture
def returns(calendar) -> pd.DataFrame:
    """Three assets: PLUS and CLONE are identical, MINUS is their negative."""
    base = np.sin(np.arange(len(calendar)) * 0.7) * 0.01
    frame = pd.DataFrame(
        {"PLUS": base, "CLONE": base, "MINUS": -base}, index=calendar
    )
    frame.columns.name = TICKER
    return frame


# --------------------------------------------------------------------------- #
# Rolling matrix
# --------------------------------------------------------------------------- #
def test_rolling_correlation_shape(returns):
    result = rolling_correlation(returns, window=10)

    assert result.index.names == [TIMESTAMP, TICKER]
    assert result.shape == (len(returns) * 3, 3)


def test_perfect_and_inverse_relationships_are_recovered(returns):
    result = rolling_correlation(returns, window=10)
    last = result.loc[returns.index[-1]]

    assert last.loc["PLUS", "CLONE"] == pytest.approx(1.0)
    assert last.loc["PLUS", "MINUS"] == pytest.approx(-1.0)
    assert last.loc["PLUS", "PLUS"] == pytest.approx(1.0)


def test_window_is_respected(returns):
    result = rolling_correlation(returns, window=10)
    early = result.loc[returns.index[5]]
    assert early.isna().all().all()


def test_rolling_correlation_rejects_a_tiny_window(returns):
    with pytest.raises(ValueError, match="window must be >= 2"):
        rolling_correlation(returns, window=1)


def test_rolling_correlation_of_empty_input():
    assert rolling_correlation(pd.DataFrame(), window=5).empty


def test_static_correlation_matrix(returns):
    matrix = correlation_matrix(returns)

    assert matrix.shape == (3, 3)
    assert matrix.loc["PLUS", "CLONE"] == pytest.approx(1.0)
    assert matrix.loc["PLUS", "MINUS"] == pytest.approx(-1.0)
    assert np.allclose(np.diag(matrix.to_numpy()), 1.0)


def test_correlation_matrix_is_symmetric(returns):
    matrix = correlation_matrix(returns).to_numpy()
    assert np.allclose(matrix, matrix.T)


# --------------------------------------------------------------------------- #
# Tensor view
# --------------------------------------------------------------------------- #
def test_tensor_reshape_matches_the_frame(returns):
    rolling = rolling_correlation(returns, window=10)
    tensor, timestamps = correlation_tensor(rolling)

    assert tensor.shape == (len(returns), 3, 3)
    assert timestamps.equals(returns.index)
    # Spot-check the last matrix against the frame it came from.
    assert np.allclose(
        tensor[-1], rolling.loc[returns.index[-1]].to_numpy(), equal_nan=True
    )


def test_tensor_of_empty_input():
    tensor, timestamps = correlation_tensor(pd.DataFrame())
    assert tensor.size == 0
    assert len(timestamps) == 0


# --------------------------------------------------------------------------- #
# Average pairwise
# --------------------------------------------------------------------------- #
def test_average_pairwise_excludes_the_diagonal(returns):
    rolling = rolling_correlation(returns, window=10)
    average = average_pairwise_correlation(rolling)

    # Pairs are (+1, -1, -1): mean -1/3. Including the diagonal would give 1/3.
    assert average.dropna().iloc[-1] == pytest.approx(-1 / 3)


def test_average_matches_a_manual_computation(returns):
    rolling = rolling_correlation(returns, window=10)
    average = average_pairwise_correlation(rolling)

    manual = returns.iloc[-10:].corr().to_numpy()
    off = ~np.eye(3, dtype=bool)
    assert average.iloc[-1] == pytest.approx(manual[off].mean())


def test_average_is_nan_before_the_window_fills(returns):
    average = average_pairwise_correlation(rolling_correlation(returns, window=10))
    assert average.iloc[:9].isna().all()


def test_average_emits_no_warning_on_empty_windows(returns, recwarn):
    """An all-NaN row must not trigger numpy's 'Mean of empty slice'."""
    average_pairwise_correlation(rolling_correlation(returns, window=10))
    assert not [w for w in recwarn if issubclass(w.category, RuntimeWarning)]


def test_average_of_a_single_asset_is_nan(calendar):
    single = pd.DataFrame({"ONLY": np.linspace(0, 1, len(calendar))}, index=calendar)
    average = average_pairwise_correlation(rolling_correlation(single, window=10))
    assert average.isna().all()


def test_average_of_empty_input():
    assert average_pairwise_correlation(pd.DataFrame()).empty


def test_uncorrelated_assets_average_near_zero(calendar):
    rng = np.random.default_rng(7)
    frame = pd.DataFrame(
        rng.normal(0, 0.01, (len(calendar), 4)),
        index=calendar,
        columns=["A", "B", "C", "D"],
    )
    average = average_pairwise_correlation(rolling_correlation(frame, window=40))
    assert abs(average.dropna().iloc[-1]) < 0.4


# --------------------------------------------------------------------------- #
# Pairwise columns
# --------------------------------------------------------------------------- #
def test_pairwise_uses_the_upper_triangle_only(returns):
    pairs = pairwise_correlations(rolling_correlation(returns, window=10))

    # Names are canonical (members sorted), so they do not depend on the
    # order the assets arrived in.
    assert list(pairs.columns) == ["CLONE|MINUS", "CLONE|PLUS", "MINUS|PLUS"]
    assert len(pairs) == len(returns)


def test_pairwise_values_are_correct(returns):
    pairs = pairwise_correlations(rolling_correlation(returns, window=10))
    assert pairs["CLONE|PLUS"].dropna().iloc[-1] == pytest.approx(1.0)
    assert pairs["MINUS|PLUS"].dropna().iloc[-1] == pytest.approx(-1.0)


def test_pairwise_names_are_independent_of_column_order(calendar):
    rng = np.random.default_rng(3)
    data = rng.normal(0, 0.01, (len(calendar), 3))
    forward = pd.DataFrame(data, index=calendar, columns=["A", "B", "C"])
    reverse = forward[["C", "B", "A"]]

    lhs = pairwise_correlations(rolling_correlation(forward, window=10))
    rhs = pairwise_correlations(rolling_correlation(reverse, window=10))

    assert list(lhs.columns) == list(rhs.columns) == ["A|B", "A|C", "B|C"]
    pd.testing.assert_frame_equal(lhs, rhs)


def test_pairwise_of_a_single_asset_has_no_columns(calendar):
    single = pd.DataFrame({"ONLY": np.linspace(0, 1, len(calendar))}, index=calendar)
    pairs = pairwise_correlations(rolling_correlation(single, window=10))
    assert pairs.shape[1] == 0


# --------------------------------------------------------------------------- #
# Breakdown flags
# --------------------------------------------------------------------------- #
@pytest.fixture
def regime_shift() -> pd.Series:
    """Correlation sits near 0.2 for a year, then jumps to 0.95."""
    index = pd.bdate_range("2023-01-02", periods=320, tz="UTC", name=TIMESTAMP)
    values = np.concatenate(
        [
            0.20 + 0.01 * np.sin(np.arange(300) * 0.3),
            np.full(20, 0.95),
        ]
    )
    return pd.Series(values, index=index, name="average_correlation")


def test_flag_frame_has_the_documented_columns(regime_shift):
    flags = correlation_breakdown_flags(regime_shift)
    assert list(flags.columns) == list(BREAKDOWN_COLUMNS)


def test_boolean_columns_are_real_booleans(regime_shift):
    flags = correlation_breakdown_flags(regime_shift)
    for column in ("elevated", "spike", "breakdown"):
        assert flags[column].dtype == np.bool_


def test_a_regime_shift_is_flagged(regime_shift):
    flags = correlation_breakdown_flags(regime_shift, baseline_window=252)

    calm = flags.iloc[:290]
    crisis = flags.iloc[300:]

    assert not calm[["elevated", "spike", "breakdown"]].to_numpy().any()
    assert crisis["elevated"].all()
    assert crisis["spike"].all()
    assert crisis["breakdown"].any()


def test_each_condition_is_independent(regime_shift):
    """A level below the absolute threshold can still be statistically extreme."""
    flags = correlation_breakdown_flags(
        regime_shift, level_threshold=0.99, zscore_threshold=2.0
    )
    crisis = flags.iloc[300:]

    assert crisis["elevated"].all()
    assert not crisis["spike"].any()      # 0.95 < 0.99


def test_the_baseline_excludes_the_bar_being_judged():
    """A shifted baseline is what lets a single jump register at all."""
    index = pd.bdate_range("2023-01-02", periods=120, tz="UTC")
    values = np.concatenate([np.full(119, 0.2), [0.9]])
    series = pd.Series(values, index=index, name="average_correlation")

    flags = correlation_breakdown_flags(series, baseline_window=60, min_periods=30)

    # The final bar is judged against a baseline of 0.2 only.
    assert flags["baseline"].iloc[-1] == pytest.approx(0.2)
    assert flags["breakdown"].iloc[-1]


def test_stable_correlation_is_never_flagged():
    index = pd.bdate_range("2023-01-02", periods=300, tz="UTC")
    steady = pd.Series(0.35, index=index, name="average_correlation")

    flags = correlation_breakdown_flags(steady)

    assert not flags[["elevated", "spike", "breakdown"]].to_numpy().any()


def test_zero_dispersion_does_not_produce_infinities():
    index = pd.bdate_range("2023-01-02", periods=300, tz="UTC")
    steady = pd.Series(0.35, index=index, name="average_correlation")

    flags = correlation_breakdown_flags(steady)

    assert not np.isinf(flags["zscore"].to_numpy()).any()


def test_flags_are_false_not_nan_where_undetermined(regime_shift):
    flags = correlation_breakdown_flags(regime_shift)
    assert flags[["elevated", "spike", "breakdown"]].notna().all().all()


def test_breakdown_flags_of_empty_input():
    flags = correlation_breakdown_flags(pd.Series(dtype="float64"))
    assert flags.empty
    assert list(flags.columns) == list(BREAKDOWN_COLUMNS)


def test_breakdown_is_deterministic(regime_shift):
    pd.testing.assert_frame_equal(
        correlation_breakdown_flags(regime_shift),
        correlation_breakdown_flags(regime_shift),
    )
