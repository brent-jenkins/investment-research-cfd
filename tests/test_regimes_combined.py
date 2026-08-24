"""The combined mapping: total, deterministic, and explicitly ordered.

This is the module whose correctness is a property of the whole domain
rather than of a few examples, so the tests enumerate all 384 combinations
rather than sampling them.
"""

from __future__ import annotations

from itertools import product

import pandas as pd
import pytest

from cfdinvest.regimes.combined import (
    AMBIGUITY_RULES,
    COMBINED_LABELS,
    COMBINED_REGIMES,
    PRECEDENCE,
    RegimeKey,
    classify_combined,
    classify_key,
    decision_table,
    verify_totality,
)
from cfdinvest.regimes.correlation import CORRELATION_LABELS
from cfdinvest.regimes.labels import UNKNOWN
from cfdinvest.regimes.macro import MACRO_LABELS
from cfdinvest.regimes.trend import TREND_LABELS
from cfdinvest.regimes.volatility import VOLATILITY_LABELS

ALL_KEYS = [
    RegimeKey(*combo)
    for combo in product(
        VOLATILITY_LABELS, TREND_LABELS, CORRELATION_LABELS, MACRO_LABELS
    )
]


# --------------------------------------------------------------------------- #
# Totality
# --------------------------------------------------------------------------- #
def test_the_domain_is_384_combinations():
    assert len(VOLATILITY_LABELS) == 4
    assert len(TREND_LABELS) == 4
    assert len(CORRELATION_LABELS) == 4
    assert len(MACRO_LABELS) == 6
    assert len(ALL_KEYS) == 384


def test_every_combination_maps_to_exactly_one_label():
    labels = {key: classify_key(key)[0] for key in ALL_KEYS}

    assert len(labels) == 384
    assert all(isinstance(label, str) for label in labels.values())
    assert set(labels.values()) <= set(COMBINED_LABELS)


def test_decision_table_covers_the_whole_domain():
    table = decision_table()

    assert len(table) == 384
    assert table["key"].nunique() == 384
    assert table["combined_regime"].notna().all()
    assert table["rule"].notna().all()


def test_verify_totality_reports_a_clean_bill():
    summary = verify_totality()

    assert summary["combinations"] == 384
    assert summary["labelled"] == 384
    assert summary["unique_keys"] == 384
    assert summary["ambiguous"] > 0


def test_the_table_and_the_predicates_agree():
    """The vectorised lookup must reproduce the rule evaluation exactly."""
    table = decision_table().set_index("key")

    for key in ALL_KEYS:
        label, rule = classify_key(key)
        row = table.loc["|".join(key)]
        assert row["combined_regime"] == label
        assert row["rule"] == rule


def test_precedence_ends_in_a_catch_all():
    assert PRECEDENCE[-1].predicate(RegimeKey("low", "sideways", "normal", "expansion"))
    assert all(
        PRECEDENCE[-1].predicate(key) for key in ALL_KEYS
    ), "the final rule must match everything"


def test_every_rule_is_reachable():
    """A rule no combination reaches is dead code, not a decision."""
    fired = {classify_key(key)[1] for key in ALL_KEYS}
    declared = {rule.name for rule in PRECEDENCE}
    assert fired == declared


def test_every_declared_label_is_produced():
    produced = {classify_key(key)[0] for key in ALL_KEYS}
    assert produced == set(COMBINED_LABELS)


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #
def test_classification_is_deterministic():
    first = [classify_key(key) for key in ALL_KEYS]
    second = [classify_key(key) for key in ALL_KEYS]
    assert first == second


def test_decision_table_is_stable_across_calls():
    pd.testing.assert_frame_equal(decision_table(), decision_table())


def test_rules_are_order_dependent_by_design():
    """Crisis and risk_off both match the same key; precedence decides."""
    key = RegimeKey("high", "downtrend", "breakdown", "expansion")

    crisis = next(r for r in PRECEDENCE if r.name == "crisis")
    risk_off = next(r for r in PRECEDENCE if r.name == "risk_off")

    assert crisis.predicate(key)
    assert risk_off.predicate(key)
    # The earlier rule wins, and it is earlier in the tuple.
    assert classify_key(key)[0] == "crisis"
    assert PRECEDENCE.index(crisis) < PRECEDENCE.index(risk_off)


# --------------------------------------------------------------------------- #
# Precedence semantics
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("key", "expected"),
    [
        # Everything failing at once.
        (("high", "downtrend", "breakdown", "contraction"), "crisis"),
        (("high", "downtrend", "breakdown", "expansion"), "crisis"),
        # Stagflation outranks a calm tape.
        (("low", "uptrend", "diversified", "stagflation"), "stagflation"),
        (("medium", "sideways", "normal", "stagflation"), "stagflation"),
        # Any single stress signal.
        (("high", "sideways", "normal", "expansion"), "risk_off"),
        (("low", "downtrend", "normal", "expansion"), "risk_off"),
        (("low", "sideways", "breakdown", "expansion"), "risk_off"),
        (("low", "sideways", "normal", "contraction"), "risk_off"),
        # Macro-confirmed advance.
        (("low", "uptrend", "normal", "expansion"), "expansion"),
        (("medium", "uptrend", "diversified", "overheating"), "expansion"),
        # Advance without macro confirmation.
        (("low", "uptrend", "normal", "unknown"), "risk_on"),
        (("medium", "uptrend", "diversified", "slowdown"), "risk_on"),
        # Nothing stressed, nothing confirmed.
        (("low", "sideways", "normal", "expansion"), "neutral"),
        (("medium", "sideways", "normal", "unknown"), "neutral"),
        # No evidence at all.
        ((UNKNOWN, UNKNOWN, UNKNOWN, UNKNOWN), UNKNOWN),
    ],
)
def test_worked_examples(key, expected):
    assert classify_key(RegimeKey(*key))[0] == expected


def test_crisis_requires_all_three_market_components():
    """Dropping any one condition demotes crisis to risk_off."""
    assert classify_key(RegimeKey("high", "downtrend", "breakdown", "expansion"))[0] == (
        "crisis"
    )
    for weakened in (
        ("medium", "downtrend", "breakdown", "expansion"),
        ("high", "sideways", "breakdown", "expansion"),
        ("high", "downtrend", "normal", "expansion"),
    ):
        assert classify_key(RegimeKey(*weakened))[0] == "risk_off"


def test_crisis_outranks_stagflation():
    key = RegimeKey("high", "downtrend", "breakdown", "stagflation")
    assert classify_key(key)[0] == "crisis"


def test_stagflation_outranks_every_market_state():
    for volatility, trend, correlation in product(
        ("low", "medium"), ("uptrend", "sideways"), ("diversified", "normal")
    ):
        key = RegimeKey(volatility, trend, correlation, "stagflation")
        assert classify_key(key)[0] == "stagflation"


def test_expansion_outranks_risk_on():
    """Both match; the macro-confirmed label wins."""
    key = RegimeKey("low", "uptrend", "normal", "expansion")
    risk_on = next(r for r in PRECEDENCE if r.name == "risk_on")

    assert risk_on.predicate(key)
    assert classify_key(key)[0] == "expansion"


def test_risk_on_needs_an_uptrend_and_calm():
    assert classify_key(RegimeKey("low", "sideways", "normal", "unknown"))[0] == "neutral"
    assert classify_key(RegimeKey("high", "uptrend", "normal", "unknown"))[0] == "risk_off"


def test_unknown_label_requires_all_four_unknown():
    """Three unknowns still classify on the one component that is known."""
    assert classify_key(RegimeKey(UNKNOWN, UNKNOWN, UNKNOWN, "stagflation"))[0] == (
        "stagflation"
    )
    assert classify_key(RegimeKey("high", UNKNOWN, UNKNOWN, UNKNOWN))[0] == "risk_off"
    assert classify_key(RegimeKey(UNKNOWN, UNKNOWN, UNKNOWN, UNKNOWN))[0] == UNKNOWN


def test_risk_off_is_deliberately_the_widest_label():
    """Asymmetry by design: one stress signal reduces risk, adding risk
    requires agreement. Recorded so a future change is a visible decision."""
    counts = decision_table()["combined_regime"].value_counts()
    assert counts["risk_off"] > counts["risk_on"] + counts["expansion"]


# --------------------------------------------------------------------------- #
# Ambiguity flags
# --------------------------------------------------------------------------- #
def test_ambiguity_rules_all_fire_somewhere():
    reasons = set()
    for key in ALL_KEYS:
        for rule in AMBIGUITY_RULES:
            if rule.predicate(key):
                reasons.add(rule.name)
    assert reasons == {rule.name for rule in AMBIGUITY_RULES}


@pytest.mark.parametrize(
    ("key", "reason"),
    [
        (("high", "uptrend", "normal", "expansion"), "volatile_advance"),
        (("low", "uptrend", "normal", "contraction"), "recovery_rally"),
        (("low", "uptrend", "normal", "overheating"), "late_cycle_calm"),
        (("low", "uptrend", "normal", "stagflation"), "stagflation_with_a_strong_tape"),
        (("low", "sideways", "breakdown", "expansion"), "correlation_only_stress"),
        ((UNKNOWN, UNKNOWN, "normal", "expansion"), "thin_evidence"),
    ],
)
def test_specific_ambiguities_are_flagged(key, reason):
    row = decision_table().set_index("key").loc["|".join(key)]
    assert row["ambiguous"]
    assert reason in row["ambiguity_reason"]


def test_unambiguous_combinations_are_not_flagged():
    table = decision_table().set_index("key")
    for key in (
        ("low", "uptrend", "normal", "expansion"),
        ("high", "downtrend", "breakdown", "contraction"),
        ("medium", "sideways", "normal", "expansion"),
    ):
        assert not table.loc["|".join(key)]["ambiguous"]


def test_all_unknown_is_not_double_flagged():
    """The no-evidence label already says it; thin_evidence would be noise."""
    row = decision_table().set_index("key").loc["|".join([UNKNOWN] * 4)]
    assert "thin_evidence" not in row["ambiguity_reason"]


def test_ambiguity_does_not_change_the_label():
    """Flags annotate; they never override."""
    table = decision_table()
    flagged = table[table["ambiguous"]]
    for _, row in flagged.head(20).iterrows():
        key = RegimeKey(
            row["volatility_regime"],
            row["trend_regime"],
            row["correlation_regime"],
            row["macro_regime"],
        )
        assert classify_key(key)[0] == row["combined_regime"]


# --------------------------------------------------------------------------- #
# Vectorised classification
# --------------------------------------------------------------------------- #
@pytest.fixture
def index() -> pd.DatetimeIndex:
    return pd.bdate_range("2024-01-01", periods=5, tz="UTC", name="timestamp")


def test_classify_combined_matches_the_table(index):
    volatility = pd.Series(
        ["low", "high", "medium", "low", UNKNOWN], index=index
    )
    trend = pd.Series(
        ["uptrend", "downtrend", "sideways", "uptrend", UNKNOWN], index=index
    )
    correlation = pd.Series(
        ["normal", "breakdown", "normal", "diversified", UNKNOWN], index=index
    )
    macro = pd.Series(
        ["expansion", "contraction", "stagflation", "unknown", UNKNOWN], index=index
    )

    result = classify_combined(volatility, trend, correlation, macro)

    assert list(result["combined_regime"]) == [
        "expansion",
        "crisis",
        "stagflation",
        "risk_on",
        UNKNOWN,
    ]


def test_classify_combined_returns_the_rule_that_fired(index):
    result = classify_combined(
        pd.Series(["high"] * 5, index=index),
        pd.Series(["downtrend"] * 5, index=index),
        pd.Series(["breakdown"] * 5, index=index),
        pd.Series(["expansion"] * 5, index=index),
    )
    assert (result["rule"] == "crisis").all()


def test_missing_components_default_to_unknown(index):
    result = classify_combined(
        pd.Series(["high"] * 5, index=index), None, None, None, index=index
    )
    assert (result["combined_regime"] == "risk_off").all()


def test_all_components_missing_yields_unknown(index):
    result = classify_combined(index=index)
    assert (result["combined_regime"] == UNKNOWN).all()


def test_partial_index_coverage_is_filled_with_unknown(index):
    partial = pd.Series(["high"], index=index[:1])
    result = classify_combined(partial, index=index)

    assert result["combined_regime"].iloc[0] == "risk_off"
    assert (result["combined_regime"].iloc[1:] == UNKNOWN).all()


def test_result_is_categorical_with_the_full_label_set(index):
    result = classify_combined(index=index)
    categories = list(result["combined_regime"].cat.categories)
    assert categories == list(COMBINED_LABELS)


def test_empty_input_yields_an_empty_frame():
    result = classify_combined()
    assert result.empty
    assert list(result.columns) == [
        "combined_regime",
        "rule",
        "ambiguous",
        "ambiguity_reason",
    ]


def test_vectorised_path_agrees_with_the_scalar_path():
    """Every combination, through the Series API, must match classify_key."""
    keys = ALL_KEYS
    index = pd.RangeIndex(len(keys))
    result = classify_combined(
        pd.Series([k.volatility for k in keys], index=index),
        pd.Series([k.trend for k in keys], index=index),
        pd.Series([k.correlation for k in keys], index=index),
        pd.Series([k.macro for k in keys], index=index),
    )
    expected = [classify_key(k)[0] for k in keys]
    assert list(result["combined_regime"].astype(str)) == expected


def test_combined_regimes_declared_set_is_ordered():
    assert COMBINED_REGIMES[0] == "crisis"
    assert COMBINED_REGIMES[-1] == "expansion"
    assert COMBINED_LABELS[-1] == UNKNOWN
