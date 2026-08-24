"""Direction resolution: total, deterministic, and explicitly ordered."""

from __future__ import annotations

from itertools import product

import numpy as np
import pandas as pd
import pytest

from cfdinvest.signals.combined import (
    DIRECTION_PRECEDENCE,
    DIRECTIONS,
    RAW_STATES,
    SignalKey,
    build_reasons,
    confidence_score,
    direction_decision_table,
    raw_direction,
    resolve_directions,
    resolve_key,
    verify_direction_totality,
)

ALL_KEYS = [
    SignalKey(*combo)
    for combo in product(RAW_STATES, (True, False), (True, False), (True, False))
]


# --------------------------------------------------------------------------- #
# Totality
# --------------------------------------------------------------------------- #
def test_the_domain_is_32_combinations():
    assert len(RAW_STATES) == 4
    assert len(ALL_KEYS) == 32


def test_every_combination_maps_to_exactly_one_direction():
    directions = {key: resolve_key(key)[0] for key in ALL_KEYS}

    assert len(directions) == 32
    assert set(directions.values()) <= set(DIRECTIONS)


def test_decision_table_covers_the_domain():
    table = direction_decision_table()

    assert len(table) == 32
    assert table["key"].nunique() == 32
    assert table["direction"].notna().all()


def test_verify_direction_totality():
    summary = verify_direction_totality()
    assert summary == {"combinations": 32, "unique_keys": 32, "rules": 7}


def test_the_table_and_the_predicates_agree():
    table = direction_decision_table().set_index("key")
    for key in ALL_KEYS:
        direction, rule = resolve_key(key)
        row = table.loc[
            f"{key.raw}|{int(key.allow_long)}|{int(key.allow_short)}|{int(key.size_ok)}"
        ]
        assert row["direction"] == direction
        assert row["rule"] == rule


def test_precedence_ends_in_a_catch_all():
    assert all(DIRECTION_PRECEDENCE[-1].predicate(key) for key in ALL_KEYS)


def test_the_catch_all_is_unreachable_in_practice():
    """Every real combination is caught by a named rule before the default."""
    fired = {resolve_key(key)[1] for key in ALL_KEYS}
    assert "default_flat" not in fired


def test_every_other_rule_is_reachable():
    fired = {resolve_key(key)[1] for key in ALL_KEYS}
    declared = {r.name for r in DIRECTION_PRECEDENCE} - {"default_flat"}
    assert fired == declared


def test_classification_is_deterministic():
    assert [resolve_key(k) for k in ALL_KEYS] == [resolve_key(k) for k in ALL_KEYS]


# --------------------------------------------------------------------------- #
# Precedence semantics
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("key", "direction", "rule"),
    [
        (("no_data", True, True, True), "flat", "no_data"),
        (("flat", True, True, True), "flat", "below_entry_threshold"),
        (("long", False, True, True), "flat", "blocked_long"),
        (("short", True, False, True), "flat", "blocked_short"),
        (("long", True, True, False), "flat", "below_min_size"),
        (("short", True, True, False), "flat", "below_min_size"),
        (("long", True, True, True), "long", "trend_long"),
        (("short", True, True, True), "short", "trend_short"),
    ],
)
def test_worked_examples(key, direction, rule):
    assert resolve_key(SignalKey(*key)) == (direction, rule)


def test_a_block_is_reported_before_the_size_rule():
    """The reason must name the actual cause, not the size that followed."""
    key = SignalKey("long", allow_long=False, allow_short=True, size_ok=False)
    assert resolve_key(key)[1] == "blocked_long"


def test_no_data_outranks_everything():
    for allow_long, allow_short, size_ok in product((True, False), repeat=3):
        key = SignalKey("no_data", allow_long, allow_short, size_ok)
        assert resolve_key(key)[1] == "no_data"


def test_permission_only_gates_its_own_direction():
    """Forbidding shorts must not block a long."""
    assert resolve_key(SignalKey("long", True, False, True))[0] == "long"
    assert resolve_key(SignalKey("short", False, True, True))[0] == "short"


def test_only_two_combinations_yield_each_direction():
    table = direction_decision_table()
    assert (table["direction"] == "long").sum() == 2
    assert (table["direction"] == "short").sum() == 2
    assert (table["direction"] == "flat").sum() == 28


# --------------------------------------------------------------------------- #
# raw_direction
# --------------------------------------------------------------------------- #
@pytest.fixture
def index() -> pd.DatetimeIndex:
    return pd.bdate_range("2024-01-01", periods=5, tz="UTC", name="timestamp")


def test_raw_direction_thresholds(index):
    strength = pd.DataFrame(
        {"A": [0.5, -0.5, 0.1, -0.1, np.nan]}, index=index
    )
    result = raw_direction(strength, entry_threshold=0.3)
    assert list(result["A"]) == ["long", "short", "flat", "flat", "no_data"]


def test_raw_direction_boundary_is_inclusive(index):
    strength = pd.DataFrame({"A": [0.3, -0.3, 0.29, -0.29, 0.0]}, index=index)
    result = raw_direction(strength, entry_threshold=0.3)
    assert list(result["A"]) == ["long", "short", "flat", "flat", "flat"]


def test_raw_direction_rejects_a_bad_threshold(index):
    with pytest.raises(ValueError, match="entry_threshold"):
        raw_direction(pd.DataFrame({"A": [0.0]}, index=index[:1]), entry_threshold=1.5)


def test_raw_direction_of_empty_input():
    assert raw_direction(pd.DataFrame()).empty


# --------------------------------------------------------------------------- #
# Vectorised resolution
# --------------------------------------------------------------------------- #
def test_vectorised_path_agrees_with_the_scalar_path():
    """Every one of the 32 combinations, through the panel API."""
    keys = ALL_KEYS
    idx = pd.RangeIndex(len(keys))
    raw = pd.DataFrame({"A": [k.raw for k in keys]}, index=idx)
    allow_long = pd.DataFrame({"A": [k.allow_long for k in keys]}, index=idx)
    allow_short = pd.DataFrame({"A": [k.allow_short for k in keys]}, index=idx)
    size_ok = pd.DataFrame({"A": [k.size_ok for k in keys]}, index=idx)

    direction, rule = resolve_directions(raw, allow_long, allow_short, size_ok)

    assert list(direction["A"]) == [resolve_key(k)[0] for k in keys]
    assert list(rule["A"]) == [resolve_key(k)[1] for k in keys]


def test_resolve_directions_of_empty_input():
    direction, rule = resolve_directions(
        pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    )
    assert direction.empty and rule.empty


# --------------------------------------------------------------------------- #
# Confidence
# --------------------------------------------------------------------------- #
def test_confidence_multiplies_its_three_factors(index):
    strength = pd.DataFrame({"A": [0.8] * 5}, index=index)
    agreement = pd.DataFrame({"A": [0.5] * 5}, index=index)
    multiplier = pd.DataFrame({"A": [0.5] * 5}, index=index)
    direction = pd.DataFrame({"A": ["long"] * 5}, index=index)

    score = confidence_score(strength, agreement, multiplier, direction)
    assert score["A"].iloc[0] == pytest.approx(0.8 * 0.5 * 0.5)


def test_confidence_distinguishes_consensus_from_disagreement(index):
    """Same strength, different agreement, different confidence."""
    strength = pd.DataFrame({"A": [0.5] * 5}, index=index)
    direction = pd.DataFrame({"A": ["long"] * 5}, index=index)
    ones = pd.DataFrame({"A": [1.0] * 5}, index=index)

    unanimous = confidence_score(strength, ones, ones, direction)
    split = confidence_score(
        strength, pd.DataFrame({"A": [0.5] * 5}, index=index), ones, direction
    )
    assert unanimous["A"].iloc[0] > split["A"].iloc[0]


def test_flat_cells_have_zero_confidence(index):
    strength = pd.DataFrame({"A": [0.9] * 5}, index=index)
    ones = pd.DataFrame({"A": [1.0] * 5}, index=index)
    direction = pd.DataFrame({"A": ["flat"] * 5}, index=index)

    assert (confidence_score(strength, ones, ones, direction)["A"] == 0.0).all()


def test_confidence_is_bounded_and_never_nan(index):
    strength = pd.DataFrame({"A": [2.0, -2.0, np.nan, 0.5, 0.1]}, index=index)
    direction = pd.DataFrame({"A": ["long"] * 5}, index=index)

    score = confidence_score(strength, None, None, direction)
    assert score.notna().all().all()
    assert ((score >= 0) & (score <= 1)).all().all()


# --------------------------------------------------------------------------- #
# Reasons
# --------------------------------------------------------------------------- #
def test_reason_starts_with_the_rule(index):
    rule = pd.DataFrame({"A": ["trend_long"] * 5}, index=index)
    reasons = build_reasons(rule)
    assert (reasons["A"] == "trend_long").all()


def test_reason_appends_active_flags(index):
    rule = pd.DataFrame({"A": ["trend_long"] * 5}, index=index)
    flags = {
        "high_volatility": pd.DataFrame({"A": [True, False, True, False, False]},
                                        index=index),
    }
    reasons = build_reasons(rule, flags)

    assert reasons["A"].iloc[0] == "trend_long|high_volatility"
    assert reasons["A"].iloc[1] == "trend_long"


def test_reason_appends_context(index):
    rule = pd.DataFrame({"A": ["trend_long"] * 5}, index=index)
    context = pd.DataFrame({"A": ["risk_on"] * 5}, index=index)
    assert build_reasons(rule, None, context)["A"].iloc[0] == "trend_long @risk_on"


def test_reason_is_never_empty(index):
    rule = pd.DataFrame({"A": [None] * 5}, index=index)
    reasons = build_reasons(rule)
    assert (reasons["A"].str.len() > 0).all()


def test_reason_of_empty_input():
    assert build_reasons(pd.DataFrame()).empty
