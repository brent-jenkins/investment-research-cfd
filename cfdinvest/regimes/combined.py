"""Combined regime: one label per timestamp from the four components.

The mapping is a **precedence list of predicates**, evaluated in order, with
a catch-all last. That shape buys three properties the classifier needs:

*Deterministic* -- the first matching rule wins, so overlapping rules have a
defined outcome instead of depending on statement order in a function body.

*Total* -- the final rule matches everything, so every one of the
``4 x 4 x 4 x 6 = 384`` component combinations maps to exactly one label.
:func:`decision_table` enumerates all of them, and the test suite asserts the
count and the uniqueness rather than trusting the claim.

*Inspectable* -- because the whole domain is small and enumerable, the table
*is* the specification. You can read it, diff it, and see exactly which rule
fired for any combination, instead of reverse-engineering nested branches.

Precedence runs most-severe-first, because a market can be in several states
at once and the dangerous reading should win. Where a mapping is a genuine
judgement call, it is labelled *and* flagged: see :data:`AMBIGUITY_RULES`.

No signal logic lives here. A regime is a description of conditions, not an
instruction to trade.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from itertools import product
from typing import Callable, NamedTuple

import pandas as pd

from cfdinvest.regimes.correlation import CORRELATION_LABELS
from cfdinvest.regimes.labels import UNKNOWN, as_regime, with_unknown
from cfdinvest.regimes.macro import MACRO_LABELS
from cfdinvest.regimes.trend import TREND_LABELS
from cfdinvest.regimes.volatility import VOLATILITY_LABELS

__all__ = [
    "COMBINED_REGIMES",
    "COMBINED_LABELS",
    "RegimeKey",
    "RegimeRule",
    "AmbiguityRule",
    "PRECEDENCE",
    "AMBIGUITY_RULES",
    "decision_table",
    "classify_key",
    "classify_combined",
    "verify_totality",
]

#: Ordered from most defensive to most constructive. ``stagflation`` sits near
#: the bottom because it is hostile to equities and bonds simultaneously --
#: there is no obvious defensive asset -- and ``unknown`` is last because it
#: is an absence of evidence, not a position on the scale.
COMBINED_REGIMES: tuple[str, ...] = (
    "crisis",
    "stagflation",
    "risk_off",
    "neutral",
    "risk_on",
    "expansion",
)
COMBINED_LABELS: tuple[str, ...] = with_unknown(COMBINED_REGIMES)


class RegimeKey(NamedTuple):
    """One combination of the four component regimes."""

    volatility: str
    trend: str
    correlation: str
    macro: str


@dataclass(frozen=True, slots=True)
class RegimeRule:
    """A named precedence rule mapping a component combination to a label."""

    name: str
    label: str
    predicate: Callable[[RegimeKey], bool]
    rationale: str


@dataclass(frozen=True, slots=True)
class AmbiguityRule:
    """A named condition under which the assigned label is a judgement call."""

    name: str
    predicate: Callable[[RegimeKey], bool]
    reason: str


def _unknown_count(key: RegimeKey) -> int:
    """How many components have no verdict."""
    return sum(component == UNKNOWN for component in key)


#: Evaluated in order; the first match wins. The final rule matches
#: everything, which is what makes the mapping total.
PRECEDENCE: tuple[RegimeRule, ...] = (
    RegimeRule(
        name="no_evidence",
        label=UNKNOWN,
        predicate=lambda k: _unknown_count(k) == 4,
        rationale=(
            "Nothing is known -- typically the warm-up period before any "
            "rolling window has filled. Labelling this 'neutral' would assert "
            "a calm market on the strength of no data at all."
        ),
    ),
    RegimeRule(
        name="crisis",
        label="crisis",
        predicate=lambda k: (
            k.volatility == "high"
            and k.correlation == "breakdown"
            and k.trend == "downtrend"
        ),
        rationale=(
            "All three market components fail together: violent, falling, and "
            "with diversification gone. The combination is what distinguishes "
            "a crisis from an ordinary drawdown."
        ),
    ),
    RegimeRule(
        name="stagflation",
        label="stagflation",
        predicate=lambda k: k.macro == "stagflation",
        rationale=(
            "Weakening growth with accelerating inflation is hostile to "
            "equities and bonds at once, so it outranks the market-state "
            "rules below: a calm tape does not make the backdrop benign."
        ),
    ),
    RegimeRule(
        name="risk_off",
        label="risk_off",
        predicate=lambda k: (
            k.volatility == "high"
            or k.trend == "downtrend"
            or k.correlation == "breakdown"
            or k.macro == "contraction"
        ),
        rationale=(
            "Any single stress signal is enough. Deliberately asymmetric: "
            "one indicator turning hostile should reduce risk, whereas "
            "adding risk should require broad agreement."
        ),
    ),
    RegimeRule(
        name="expansion",
        label="expansion",
        predicate=lambda k: (
            k.macro in {"expansion", "overheating"}
            and k.trend == "uptrend"
            and k.volatility in {"low", "medium"}
        ),
        rationale=(
            "Macro-confirmed advance: the economy and the tape agree, with no "
            "stress. Ranked above risk_on because the macro confirmation is "
            "the stronger claim."
        ),
    ),
    RegimeRule(
        name="risk_on",
        label="risk_on",
        predicate=lambda k: (
            k.trend == "uptrend"
            and k.volatility in {"low", "medium"}
            and k.correlation in {"diversified", "normal"}
        ),
        rationale=(
            "A calm, broad advance without macro confirmation -- the macro "
            "reading is absent, or simply not expansionary."
        ),
    ),
    RegimeRule(
        name="default_neutral",
        label="neutral",
        predicate=lambda k: True,
        rationale=(
            "Catch-all. Nothing is stressed and nothing is confirmed "
            "constructive: typically a sideways tape. This rule is what makes "
            "the mapping total."
        ),
    ),
)


#: Conditions under which the assigned label is defensible but contestable.
#: Flagged rather than special-cased, because the alternative reading is a
#: modelling choice that belongs to whoever consumes the regime.
AMBIGUITY_RULES: tuple[AmbiguityRule, ...] = (
    AmbiguityRule(
        name="volatile_advance",
        predicate=lambda k: k.volatility == "high" and k.trend == "uptrend",
        reason=(
            "High volatility during an advance is labelled risk_off, but a "
            "melt-up and a panic look identical to a volatility measure that "
            "does not care about sign."
        ),
    ),
    AmbiguityRule(
        name="recovery_rally",
        predicate=lambda k: k.macro == "contraction" and k.trend == "uptrend",
        reason=(
            "Markets normally turn up before the economy does. Labelled "
            "risk_off on the macro reading, but this is exactly what the "
            "start of a recovery looks like."
        ),
    ),
    AmbiguityRule(
        name="late_cycle_calm",
        predicate=lambda k: k.macro == "overheating" and k.volatility == "low",
        reason=(
            "Overheating with low volatility is labelled expansion, but it is "
            "also the classic late-cycle setup that precedes a turn."
        ),
    ),
    AmbiguityRule(
        name="stagflation_with_a_strong_tape",
        predicate=lambda k: k.macro == "stagflation" and k.trend == "uptrend",
        reason=(
            "The macro rule overrides a rising market. Defensible, but the "
            "market may be discounting an inflation peak the data has not "
            "reached yet."
        ),
    ),
    AmbiguityRule(
        name="correlation_only_stress",
        predicate=lambda k: (
            k.correlation == "breakdown"
            and k.volatility == "low"
            and k.trend != "downtrend"
        ),
        reason=(
            "Diversification has failed while volatility is low and the tape "
            "is not falling. Risk_off is driven by one component alone."
        ),
    ),
    AmbiguityRule(
        name="thin_evidence",
        predicate=lambda k: 2 <= _unknown_count(k) <= 3,
        reason=(
            "Half or more of the components have no verdict, so the label "
            "rests on one or two readings."
        ),
    ),
)


def classify_key(key: RegimeKey) -> tuple[str, str]:
    """Return ``(label, rule_name)`` for one component combination.

    Pure. Guaranteed to return: the final rule in :data:`PRECEDENCE` matches
    unconditionally.
    """
    for rule in PRECEDENCE:
        if rule.predicate(key):
            return rule.label, rule.name
    raise AssertionError(  # pragma: no cover - PRECEDENCE ends in a catch-all
        f"no rule matched {key!r}; PRECEDENCE lost its catch-all"
    )


def _ambiguity(key: RegimeKey) -> tuple[bool, str]:
    """Return ``(is_ambiguous, reasons)`` for one combination."""
    hits = [rule for rule in AMBIGUITY_RULES if rule.predicate(key)]
    return bool(hits), "; ".join(rule.name for rule in hits)


@lru_cache(maxsize=1)
def decision_table() -> pd.DataFrame:
    """Enumerate every component combination and its label.

    The complete specification of the combined regime, as data. Cached, since
    it depends only on module-level constants.

    Returns
    -------
    pandas.DataFrame
        One row per combination (384 with the standard label sets), with
        columns ``volatility_regime``, ``trend_regime``,
        ``correlation_regime``, ``macro_regime``, ``combined_regime``,
        ``rule``, ``ambiguous``, ``ambiguity_reason`` and the ``key`` used for
        vectorised lookup.
    """
    rows = []
    for volatility, trend, correlation, macro in product(
        VOLATILITY_LABELS, TREND_LABELS, CORRELATION_LABELS, MACRO_LABELS
    ):
        key = RegimeKey(volatility, trend, correlation, macro)
        label, rule = classify_key(key)
        ambiguous, reason = _ambiguity(key)
        rows.append(
            {
                "key": "|".join(key),
                "volatility_regime": volatility,
                "trend_regime": trend,
                "correlation_regime": correlation,
                "macro_regime": macro,
                "combined_regime": label,
                "rule": rule,
                "ambiguous": ambiguous,
                "ambiguity_reason": reason,
            }
        )
    return pd.DataFrame(rows)


def verify_totality() -> dict[str, int]:
    """Check the mapping is total and unambiguous, returning a summary.

    Raises
    ------
    AssertionError
        If any combination is missing, duplicated, or carries a label outside
        :data:`COMBINED_LABELS`.

    Returns
    -------
    dict
        ``{"combinations": n, "labelled": n, "unique_keys": n, "ambiguous": n}``
    """
    table = decision_table()
    expected = (
        len(VOLATILITY_LABELS)
        * len(TREND_LABELS)
        * len(CORRELATION_LABELS)
        * len(MACRO_LABELS)
    )

    if len(table) != expected:
        raise AssertionError(f"expected {expected} combinations, got {len(table)}")
    if table["key"].nunique() != expected:
        raise AssertionError("decision table contains duplicate keys")
    if table["combined_regime"].isna().any():
        raise AssertionError("decision table contains unlabelled combinations")

    unexpected = set(table["combined_regime"]) - set(COMBINED_LABELS)
    if unexpected:
        raise AssertionError(f"labels outside the declared set: {sorted(unexpected)}")

    return {
        "combinations": expected,
        "labelled": int(table["combined_regime"].notna().sum()),
        "unique_keys": int(table["key"].nunique()),
        "ambiguous": int(table["ambiguous"].sum()),
    }


def classify_combined(
    volatility: pd.Series | None = None,
    trend: pd.Series | None = None,
    correlation: pd.Series | None = None,
    macro: pd.Series | None = None,
    *,
    index: pd.Index | None = None,
) -> pd.DataFrame:
    """Map four component regime Series to the combined regime.

    Vectorised by lookup: the component labels are concatenated into a key
    and mapped through :func:`decision_table`. No per-row Python evaluation
    of the predicates, and the same table that documents the behaviour is the
    one that produces it.

    Any component passed as ``None`` (or missing a timestamp) is treated as
    ``unknown``, so a lake without macro data still classifies on market
    state alone.

    Returns
    -------
    pandas.DataFrame
        Columns ``combined_regime`` (categorical over
        :data:`COMBINED_LABELS`), ``rule``, ``ambiguous`` and
        ``ambiguity_reason``.
    """
    components = {
        "volatility": volatility,
        "trend": trend,
        "correlation": correlation,
        "macro": macro,
    }

    resolved = index
    if resolved is None:
        for series in components.values():
            if series is not None and len(series):
                resolved = series.index
                break
    if resolved is None or len(resolved) == 0:
        return pd.DataFrame(
            {
                "combined_regime": as_regime([], COMBINED_LABELS, index=pd.Index([])),
                "rule": pd.Series(dtype="object"),
                "ambiguous": pd.Series(dtype="bool"),
                "ambiguity_reason": pd.Series(dtype="object"),
            }
        )

    parts = []
    for series in components.values():
        if series is None or len(series) == 0:
            aligned = pd.Series(UNKNOWN, index=resolved, dtype="object")
        else:
            aligned = series.reindex(resolved).astype("object").fillna(UNKNOWN)
        parts.append(aligned)

    keys = parts[0].str.cat(parts[1:], sep="|")

    table = decision_table().set_index("key")
    labels = keys.map(table["combined_regime"])
    rules = keys.map(table["rule"])
    ambiguous = keys.map(table["ambiguous"]).fillna(False).astype(bool)
    reasons = keys.map(table["ambiguity_reason"]).fillna("")

    return pd.DataFrame(
        {
            "combined_regime": as_regime(
                labels, COMBINED_LABELS, index=resolved, name="combined_regime"
            ),
            "rule": rules,
            "ambiguous": ambiguous,
            "ambiguity_reason": reasons,
        },
        index=resolved,
    )
