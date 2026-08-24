"""Resolve trend, filters and sizing into a direction, size and confidence.

The direction resolution is a **precedence list over an enumerable domain**,
exactly as in the regime layer. The continuous inputs (strength, size) are
reduced to a small set of discrete facts first:

``raw``        what the trend alone wants: long / short / flat / no_data
``allow_long`` whether the filters permit a long here
``allow_short`` whether they permit a short
``size_ok``    whether the surviving size clears the minimum

That gives ``4 x 2 x 2 x 2 = 32`` combinations, every one of which maps to
exactly one final direction and one named rule.
:func:`direction_decision_table` enumerates them, and the tests assert the
count, the uniqueness and the agreement between the vectorised path and the
scalar predicates.

Ordering matters and is deliberate: a blocked signal reports *why it was
blocked* rather than being swallowed by the size check that follows it.

No execution logic lives here. A signal is an intention, not a fill.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from itertools import product
from typing import Callable, NamedTuple

import numpy as np
import pandas as pd

__all__ = [
    "DIRECTIONS",
    "RAW_STATES",
    "SignalKey",
    "DirectionRule",
    "DIRECTION_PRECEDENCE",
    "direction_decision_table",
    "resolve_key",
    "verify_direction_totality",
    "raw_direction",
    "resolve_directions",
    "confidence_score",
    "build_reasons",
]

#: Final direction labels, ordered short -> flat -> long.
DIRECTIONS: tuple[str, ...] = ("short", "flat", "long")

#: What the trend score alone asks for, before any filter is applied.
RAW_STATES: tuple[str, ...] = ("no_data", "short", "flat", "long")


class SignalKey(NamedTuple):
    """One combination of the discrete facts that decide a direction."""

    raw: str
    allow_long: bool
    allow_short: bool
    size_ok: bool


@dataclass(frozen=True, slots=True)
class DirectionRule:
    """A named precedence rule mapping a fact combination to a direction."""

    name: str
    direction: str
    predicate: Callable[[SignalKey], bool]
    rationale: str


#: Evaluated in order; first match wins. The last rule matches everything.
DIRECTION_PRECEDENCE: tuple[DirectionRule, ...] = (
    DirectionRule(
        name="no_data",
        direction="flat",
        predicate=lambda k: k.raw == "no_data",
        rationale="No trend reading — warm-up, or a gap in the price history.",
    ),
    DirectionRule(
        name="below_entry_threshold",
        direction="flat",
        predicate=lambda k: k.raw == "flat",
        rationale=(
            "The trend components blended to something too weak to act on. "
            "Not an error: most bars, for most assets, should land here."
        ),
    ),
    DirectionRule(
        name="blocked_long",
        direction="flat",
        predicate=lambda k: k.raw == "long" and not k.allow_long,
        rationale=(
            "A long was wanted but the regime or macro filters forbid it. "
            "Checked before the size rule so the reason names the actual "
            "cause rather than the size that followed from it."
        ),
    ),
    DirectionRule(
        name="blocked_short",
        direction="flat",
        predicate=lambda k: k.raw == "short" and not k.allow_short,
        rationale="A short was wanted but the filters forbid it.",
    ),
    DirectionRule(
        name="below_min_size",
        direction="flat",
        predicate=lambda k: not k.size_ok,
        rationale=(
            "Permitted, but the surviving size is too small to be worth the "
            "spread. Taking it would pay costs for negligible exposure."
        ),
    ),
    DirectionRule(
        name="trend_long",
        direction="long",
        predicate=lambda k: k.raw == "long",
        rationale="Trend is up, filters permit it, size clears the minimum.",
    ),
    DirectionRule(
        name="trend_short",
        direction="short",
        predicate=lambda k: k.raw == "short",
        rationale="Trend is down, filters permit it, size clears the minimum.",
    ),
    DirectionRule(
        name="default_flat",
        direction="flat",
        predicate=lambda k: True,
        rationale="Catch-all. Unreachable in practice; it makes the map total.",
    ),
)


def resolve_key(key: SignalKey) -> tuple[str, str]:
    """Return ``(direction, rule_name)`` for one combination of facts."""
    for rule in DIRECTION_PRECEDENCE:
        if rule.predicate(key):
            return rule.direction, rule.name
    raise AssertionError(  # pragma: no cover - the list ends in a catch-all
        f"no rule matched {key!r}"
    )


@lru_cache(maxsize=1)
def direction_decision_table() -> pd.DataFrame:
    """Enumerate every combination of the discrete facts and its direction.

    The complete specification of direction resolution, as data.

    Returns
    -------
    pandas.DataFrame
        32 rows: ``raw``, ``allow_long``, ``allow_short``, ``size_ok``,
        ``direction``, ``rule``, and the ``key`` used for vectorised lookup.
    """
    rows = []
    for raw, allow_long, allow_short, size_ok in product(
        RAW_STATES, (True, False), (True, False), (True, False)
    ):
        key = SignalKey(raw, allow_long, allow_short, size_ok)
        direction, rule = resolve_key(key)
        rows.append(
            {
                "key": _key_string(raw, allow_long, allow_short, size_ok),
                "raw": raw,
                "allow_long": allow_long,
                "allow_short": allow_short,
                "size_ok": size_ok,
                "direction": direction,
                "rule": rule,
            }
        )
    return pd.DataFrame(rows)


def _key_string(raw, allow_long, allow_short, size_ok) -> str:
    """Build the lookup key. Kept in one place so both paths agree."""
    return f"{raw}|{int(bool(allow_long))}|{int(bool(allow_short))}|{int(bool(size_ok))}"


def verify_direction_totality() -> dict[str, int]:
    """Check the direction map is total and unambiguous.

    Raises
    ------
    AssertionError
        If any combination is missing, duplicated, or maps outside
        :data:`DIRECTIONS`.
    """
    table = direction_decision_table()
    expected = len(RAW_STATES) * 2 * 2 * 2

    if len(table) != expected:
        raise AssertionError(f"expected {expected} combinations, got {len(table)}")
    if table["key"].nunique() != expected:
        raise AssertionError("duplicate keys in the direction table")
    unexpected = set(table["direction"]) - set(DIRECTIONS)
    if unexpected:
        raise AssertionError(f"directions outside the declared set: {unexpected}")

    return {
        "combinations": expected,
        "unique_keys": int(table["key"].nunique()),
        "rules": int(table["rule"].nunique()),
    }


# --------------------------------------------------------------------------- #
# Vectorised resolution
# --------------------------------------------------------------------------- #
def raw_direction(
    strength: pd.DataFrame, entry_threshold: float = 0.3
) -> pd.DataFrame:
    """Reduce the continuous strength to ``long`` / ``short`` / ``flat`` / ``no_data``.

    Parameters
    ----------
    entry_threshold:
        Absolute strength that must be reached to want a position. A dead
        band around zero is what stops the engine churning on noise.

    Returns
    -------
    pandas.DataFrame
        Strings from :data:`RAW_STATES`.
    """
    if strength is None or strength.empty:
        return pd.DataFrame()
    if not 0.0 <= entry_threshold <= 1.0:
        raise ValueError(f"entry_threshold must be in [0, 1], got {entry_threshold}")

    values = strength.to_numpy(dtype="float64")
    labels = np.select(
        [
            np.isnan(values),
            values >= entry_threshold,
            values <= -entry_threshold,
        ],
        ["no_data", "long", "short"],
        default="flat",
    )
    return pd.DataFrame(labels, index=strength.index, columns=strength.columns)


def resolve_directions(
    raw: pd.DataFrame,
    allow_long: pd.DataFrame,
    allow_short: pd.DataFrame,
    size_ok: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Map the discrete facts to a direction and the rule that decided it.

    Vectorised by lookup through :func:`direction_decision_table`, so the
    table that documents the behaviour is the one that produces it.

    Returns
    -------
    tuple
        ``(direction, rule)`` panels of strings.
    """
    if raw is None or raw.empty:
        return pd.DataFrame(), pd.DataFrame()

    keys = (
        raw.astype("object")
        + "|"
        + allow_long.astype(int).astype(str)
        + "|"
        + allow_short.astype(int).astype(str)
        + "|"
        + size_ok.astype(int).astype(str)
    )

    table = direction_decision_table().set_index("key")
    direction_map = table["direction"].to_dict()
    rule_map = table["rule"].to_dict()

    # ``.replace`` on a whole frame keeps this a single vectorised pass rather
    # than a per-column map, and the key space is only 32 entries.
    direction = keys.replace(direction_map)
    rule = keys.replace(rule_map)
    return direction, rule


def confidence_score(
    strength: pd.DataFrame,
    agreement: pd.DataFrame | None,
    size_multiplier: pd.DataFrame | None,
    direction: pd.DataFrame,
) -> pd.DataFrame:
    """Combine conviction, agreement and environment into ``[0, 1]``.

    Three factors, multiplied:

    * ``|strength|`` — how far past the threshold the blend went.
    * ``agreement`` — how much the individual components concurred. This is
      what stops a lukewarm consensus and a violent disagreement that happen
      to average to the same number from reading alike.
    * ``size_multiplier`` — how much the environment supported acting.

    Flat cells score zero: there is no confidence in a position not taken.

    Returns
    -------
    pandas.DataFrame
        No NaN.
    """
    if strength is None or strength.empty:
        return pd.DataFrame()

    conviction = strength.abs().clip(0.0, 1.0).fillna(0.0)
    concurrence = (
        pd.DataFrame(1.0, index=strength.index, columns=strength.columns)
        if agreement is None or agreement.empty
        else agreement.reindex_like(strength).fillna(0.0).clip(0.0, 1.0)
    )
    support = (
        pd.DataFrame(1.0, index=strength.index, columns=strength.columns)
        if size_multiplier is None or size_multiplier.empty
        else size_multiplier.reindex_like(strength).fillna(0.0).clip(0.0, 1.0)
    )

    score = (conviction * concurrence * support).clip(0.0, 1.0)
    return score.where(direction.reindex_like(strength) != "flat", 0.0).fillna(0.0)


def build_reasons(
    rule: pd.DataFrame,
    flags: dict[str, pd.DataFrame] | None = None,
    context: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Assemble a compact, human-readable reason per cell.

    Built by vectorised string concatenation — one pass per flag over the
    whole panel, rather than a per-cell format call, which matters at 50
    assets by 5,000 bars.

    Format: ``rule[|flag...][ @regime]``, e.g.
    ``blocked_long|regime_blocks_long @neutral``.

    Returns
    -------
    pandas.DataFrame
        Non-empty strings everywhere.
    """
    if rule is None or rule.empty:
        return pd.DataFrame()

    reasons = rule.astype("object").fillna("unknown")

    for name, mask in (flags or {}).items():
        if mask is None or mask.empty:
            continue
        aligned = mask.reindex_like(reasons).fillna(False).astype(bool)
        reasons = reasons.mask(aligned, reasons + f"|{name}")

    if context is not None and not context.empty:
        suffix = context.reindex_like(reasons).astype("object").fillna("unknown")
        reasons = reasons + " @" + suffix

    return reasons
