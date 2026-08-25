"""Position state machine with hysteresis.

Signals are sparse and noisy; positions must not be. A target that oscillates
either side of a single threshold would open and close every other bar, and
the round-trip spread would eat the edge long before the strategy did.

Hysteresis fixes that with **two thresholds instead of one**:

* ``entry_*`` — what a signal must clear to *open* a position.
* ``exit_*``  — what it must fall below to *close* one.

Between them lies a hold band. A position already open survives it; a flat
book does not enter in it. The wider the band, the fewer trades and the more
stale the average position — that is the tunable trade-off, and it is
explicit rather than emergent.

Transitions are deterministic and exhaustive. Every ``(held, desired)`` pair
maps to exactly one action, enumerated by :func:`transition_table`.

The step function is vectorised **across assets**: one call advances the
whole universe by one bar. The time axis genuinely cannot be vectorised —
today's position depends on yesterday's, and the risk engine's equity cap
depends on the running equity path — so the engine loops over bars and each
iteration is a handful of numpy operations over the asset vector.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product

import numpy as np
import pandas as pd

__all__ = [
    "ACTIONS",
    "FLAT",
    "LONG",
    "SHORT",
    "HysteresisThresholds",
    "classify_actions",
    "desired_direction",
    "transition_table",
    "next_direction",
    "needs_resize",
]

#: Internal direction encoding. Signed so a flip is a sign change.
SHORT, FLAT, LONG = -1, 0, 1

#: Every action the machine can emit.
ACTIONS: tuple[str, ...] = (
    "hold",
    "open_long",
    "open_short",
    "close",
    "flip_long_to_short",
    "flip_short_to_long",
    "resize",
    # Exits the position did not choose: a resting level was reached
    # intrabar. Named separately so a trade log distinguishes "the model
    # changed its mind" from "the market took me out".
    "stop_loss",
    "take_profit",
)

#: Direction label -> internal code, for reading a SignalFrame.
_DIRECTION_CODES = {"long": LONG, "short": SHORT, "flat": FLAT}


@dataclass(frozen=True, slots=True)
class HysteresisThresholds:
    """The two-threshold band that keeps positions stable.

    Attributes
    ----------
    entry_size, exit_size:
        Signal size required to open, and the level below which an open
        position is closed.
    entry_confidence, exit_confidence:
        The same band applied to signal confidence.
    exit_on_opposing_signal:
        When the signal flips to the other side but is too weak to open a
        new position, close to flat rather than holding a position the model
        no longer supports. ``True`` by default: holding a long while the
        model says short is harder to defend than paying one spread.

    Raises
    ------
    ValueError
        If an exit threshold exceeds its entry threshold, which would invert
        the band and cause exactly the churn hysteresis exists to prevent.
    """

    entry_size: float = 0.10
    exit_size: float = 0.05
    entry_confidence: float = 0.35
    exit_confidence: float = 0.20
    exit_on_opposing_signal: bool = True
    #: Whether a signal going flat closes an open position.
    #:
    #: ``True`` is the recorded behaviour. ``False`` reads flat as "no view
    #: this bar" rather than "get out", so a position is closed only by a
    #: signal that has decayed while still pointing somewhere, or by one
    #: that has turned against it.
    #:
    #: The distinction is invisible from the hold band, which is why it
    #: needs its own switch: a flat cell scores zero size and zero
    #: confidence by construction, so it fails the band as well. Widening
    #: exit_size and exit_confidence therefore cannot lengthen a hold that
    #: ends this way -- tested down to 0.001 with no effect at all.
    exit_on_flat: bool = True

    def __post_init__(self) -> None:
        if self.exit_size > self.entry_size:
            raise ValueError(
                f"exit_size ({self.exit_size}) must not exceed entry_size "
                f"({self.entry_size}); an inverted band churns on every bar"
            )
        if self.exit_confidence > self.entry_confidence:
            raise ValueError(
                f"exit_confidence ({self.exit_confidence}) must not exceed "
                f"entry_confidence ({self.entry_confidence})"
            )


def desired_direction(direction_row) -> np.ndarray:
    """Encode a row of signal labels as ``-1`` / ``0`` / ``+1``.

    Pure. Anything unrecognised — including NaN — becomes flat, so a
    malformed signal cannot open a position by accident.
    """
    labels = np.asarray(direction_row, dtype=object)
    codes = np.zeros(labels.shape, dtype=np.int8)
    codes[labels == "long"] = LONG
    codes[labels == "short"] = SHORT
    return codes


def next_direction(
    held: np.ndarray,
    desired: np.ndarray,
    size: np.ndarray,
    confidence: np.ndarray,
    thresholds: HysteresisThresholds,
) -> np.ndarray:
    """Advance the direction of every asset by one bar.

    Pure and vectorised across assets. The rules, applied in this order:

    1. **Close** anything whose signal has fallen out of the hold band, or
       gone flat -- the latter only when ``exit_on_flat``.
    2. **Close** anything whose signal now opposes the position but is too
       weak to justify flipping (when ``exit_on_opposing_signal``).
    3. **Open or flip** anything whose signal clears the entry band.

    Closes are evaluated before opens so that a genuine flip resolves in one
    step rather than needing an intermediate flat bar.

    Parameters
    ----------
    held:
        Current direction per asset.
    desired:
        What the signal wants, from :func:`desired_direction`.
    size, confidence:
        Signal strength measures for this bar.

    Returns
    -------
    numpy.ndarray
        The new direction per asset.
    """
    maintains = (size >= thresholds.exit_size) & (
        confidence >= thresholds.exit_confidence
    )
    enters = (size >= thresholds.entry_size) & (
        confidence >= thresholds.entry_confidence
    )

    result = held.copy()

    # 1. Signal decayed out of the hold band, or went flat.
    #
    # With exit_on_flat off, a flat bar is treated as silence rather than
    # instruction, and the band is only consulted where the signal still
    # points somewhere. Testing the band on a flat cell would close the
    # position anyway: flat scores zero on both measures by construction.
    if thresholds.exit_on_flat:
        result[(held != FLAT) & (~maintains | (desired == FLAT))] = FLAT
    else:
        result[(held != FLAT) & (desired != FLAT) & ~maintains] = FLAT

    # 2. Signal turned against the position but is too weak to flip on.
    if thresholds.exit_on_opposing_signal:
        opposing = (held != FLAT) & (desired == -held) & ~enters
        result[opposing] = FLAT

    # 3. Anything clearing the entry band takes (or flips to) its direction.
    opening = (desired != FLAT) & enters
    result[opening] = desired[opening]

    return result


def classify_actions(
    held: np.ndarray, new: np.ndarray, resize: np.ndarray | None = None
) -> np.ndarray:
    """Name the transition each asset just made.

    Pure. ``resize`` marks assets whose direction is unchanged but whose
    target size moved enough to warrant adjusting — reported separately so a
    trade log distinguishes a new view from a rebalance of an existing one.

    Returns
    -------
    numpy.ndarray
        One label from :data:`ACTIONS` per asset.
    """
    actions = np.full(held.shape, "hold", dtype=object)

    actions[(held == FLAT) & (new == LONG)] = "open_long"
    actions[(held == FLAT) & (new == SHORT)] = "open_short"
    actions[(held != FLAT) & (new == FLAT)] = "close"
    actions[(held == LONG) & (new == SHORT)] = "flip_long_to_short"
    actions[(held == SHORT) & (new == LONG)] = "flip_short_to_long"

    if resize is not None:
        unchanged = (held == new) & (held != FLAT)
        actions[unchanged & resize] = "resize"

    return actions


def needs_resize(
    current_units: np.ndarray,
    target_units: np.ndarray,
    *,
    threshold: float = 0.25,
    absolute_floor: float = 1e-12,
) -> np.ndarray:
    """Whether each position has drifted far enough from target to adjust.

    Rebalancing to the exact target every bar would reintroduce the churn
    hysteresis just removed, since the volatility-scaled size moves a little
    on every bar. A position is only adjusted once it is ``threshold`` away
    in relative terms.

    Pure and vectorised.

    Returns
    -------
    numpy.ndarray
        Boolean per asset.
    """
    if threshold < 0:
        raise ValueError(f"threshold must be >= 0, got {threshold}")

    current = np.abs(np.asarray(current_units, dtype="float64"))
    target = np.abs(np.asarray(target_units, dtype="float64"))

    reference = np.maximum(current, absolute_floor)
    drift = np.abs(target_units - current_units) / reference

    # Going flat is a change of state, not a drift, and a relative measure
    # cannot express it: closing a position out is a drift of exactly 1.0,
    # so at any threshold of 1.0 or more `drift > threshold` is False and
    # the position can never be reduced to nothing. Opening from nothing
    # divides by the floor and always fires. The pair is a one-way ratchet:
    # positions grow and are never retired, notional compounds, and a run
    # at threshold 1.0 booked 45,398 trades and £320,712 of financing on
    # its way to losing everything.
    #
    # So the endpoints are decided absolutely and only the middle is a
    # matter of degree.
    closing = (current > absolute_floor) & (target <= absolute_floor)
    opening = (current <= absolute_floor) & (target > absolute_floor)

    return (drift > threshold) | closing | opening


def transition_table() -> pd.DataFrame:
    """Enumerate every ``(held, desired, band)`` case and its action.

    The state machine's specification as data. The band column records where
    the signal sits relative to the hysteresis thresholds: ``below_exit``,
    ``hold_band`` (between the two) or ``above_entry``.

    Returns
    -------
    pandas.DataFrame
        27 rows: three held states x three desired states x three bands.
    """
    thresholds = HysteresisThresholds()
    bands = {
        "below_exit": (0.0, 0.0),
        "hold_band": (
            (thresholds.exit_size + thresholds.entry_size) / 2,
            (thresholds.exit_confidence + thresholds.entry_confidence) / 2,
        ),
        "above_entry": (1.0, 1.0),
    }

    rows = []
    for held, desired, (band, (size, confidence)) in product(
        (SHORT, FLAT, LONG), (SHORT, FLAT, LONG), bands.items()
    ):
        new = next_direction(
            np.array([held], dtype=np.int8),
            np.array([desired], dtype=np.int8),
            np.array([size]),
            np.array([confidence]),
            thresholds,
        )
        action = classify_actions(
            np.array([held], dtype=np.int8), new, np.array([False])
        )
        rows.append(
            {
                "held": held,
                "desired": desired,
                "band": band,
                "new": int(new[0]),
                "action": action[0],
            }
        )
    return pd.DataFrame(rows)
