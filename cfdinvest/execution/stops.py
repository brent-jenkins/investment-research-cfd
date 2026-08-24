"""Intrabar stop-loss and take-profit handling.

The execution engine marks positions bar by bar, so without this module a
position that traded through its stop and recovered by the close is never
stopped out — and reported drawdowns are smaller than the ones that actually
happened.

Four things have to be right, and they are separable:

**Detection.** Did the bar's range reach the level? For a long, the stop is
hit when ``low <= stop`` and the target when ``high >= take_profit``; for a
short, mirrored.

**Gap-through.** A level is only worth what you can get. If the bar *opens*
beyond the level, the order fills at the open, not at the level — a stop at
95 on a market that opens at 88 fills at 88. Modelling every stop as filling
at its level is the single most common way a backtest invents money it never
had. :attr:`StopConfig.stop_mode` selects between that optimistic reading,
the always-conservative one, and the realistic hybrid.

**Priority.** Within one bar, OHLC does not say *when* the high and the low
occurred. If a bar breaches both levels intrabar, which came first is
unknowable from the data — a convention, not a calculation. If both are
*gapped* through, the one nearer the open is reached first, which is a fact
rather than a convention.

**Slippage.** Applied after the fill price is determined, in basis points,
capped so a violent gap cannot compound into an absurd assumed fill.

Everything here is pure. The engine calls it; it calls nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

__all__ = [
    "STOP_MODES",
    "DISTANCE_MODES",
    "BREACH_TYPES",
    "STOP_EVENT_COLUMNS",
    "StopConfig",
    "BreachEvent",
    "StopEvent",
    "TakeProfitEvent",
    "level_distance",
    "stop_levels",
    "detect_breaches",
    "resolve_breach",
    "recommended_orders",
]

#: How a fill price is chosen once a level is reached.
#:
#: ``"hybrid"`` (default)
#:     Fill at the level when it was traded through during the bar; fill at
#:     the open when the bar had already gapped past it. The realistic
#:     reading, and the only one that is right in both cases.
#: ``"breach"``
#:     Always fill at the level. **Optimistic** — it assumes a stop at 95
#:     fills at 95 on a market that opened at 88, which no venue offers.
#:     Useful only as a best-case bound.
#: ``"gap"``
#:     Always fill at the open. Conservative: it charges gap treatment even
#:     to levels that were reached calmly mid-session.
STOP_MODES: tuple[str, ...] = ("hybrid", "breach", "gap")

#: How a level's distance from entry is expressed.
#:
#: ``"atr"``      distance x ATR at entry — adapts to each instrument's noise
#: ``"fixed"``    distance in price units — same absolute stop for everything
#: ``"percent"``  distance x entry price — same relative stop for everything
DISTANCE_MODES: tuple[str, ...] = ("atr", "fixed", "percent")

#: How the level was reached.
BREACH_TYPES: tuple[str, ...] = ("intrabar", "gap")

#: Columns of the stop/take-profit event log.
STOP_EVENT_COLUMNS: tuple[str, ...] = (
    "timestamp",
    "asset",
    "kind",
    "direction",
    "breach_type",
    "gap_through",
    "level",
    "reference_price",
    "fill_price",
    "recommended_fill",
    "slippage_applied",
    "slippage_bps",
    "units_closed",
    "ambiguous",
)


@dataclass(frozen=True, slots=True)
class StopConfig:
    """Stop-loss and take-profit policy.

    Attributes
    ----------
    enabled:
        ``False`` by default. Turning it on changes the result of every
        backtest — positions that previously rode through a spike now exit —
        so it is opt-in rather than a silent revision of numbers already
        recorded.
    stop_mode:
        See :data:`STOP_MODES`. ``"hybrid"`` is the realistic choice.
    tp_priority:
        When a bar breaches **both** levels intrabar, whether the target is
        assumed reached first. ``True`` follows the profit-first rule and is
        the optimistic reading: it books the win on every bar wide enough to
        have done both. ``False`` assumes the stop, which cannot flatter the
        result. This flag is the single most consequential setting here.
    stop_distance_mode, tp_distance_mode:
        See :data:`DISTANCE_MODES`.
    stop_distance, tp_distance:
        Magnitude in the corresponding mode's units. ``tp_distance=None``
        runs stops only — the usual trend-following setup: cut losers, let
        winners run.
    slippage_bps:
        Adverse slippage on a **stop** fill, in basis points of price,
        applied *after* the fill price is determined. A stop is a market
        order once triggered, so it pays for immediacy.
    take_profit_slippage_bps:
        The same for a **target** fill. Zero by default because a target is a
        limit order: it fills at the level *or better*, never worse.
    max_slippage_bps:
        Ceiling on the slippage actually applied, so a configuration error
        or a violent gap cannot compound into an absurd assumed fill.

        It **clamps silently** rather than raising: ``slippage_bps=100`` with
        the default ceiling behaves as 50. That mirrors the broker, which
        applies its own guaranteed-fill ceiling and does not reject the order
        for asking for more. The cost of the choice is that an over-ambitious
        slippage assumption is quietly ignored, so raise the ceiling
        deliberately when a stress run needs it.
    record_recommended_fill:
        Whether every event carries a ``recommended_fill`` for manual
        execution. ``True`` always, for this deployment.
    reenter_same_bar:
        Whether a stopped position may be reopened on the bar it was stopped
        on. ``False`` by default: re-entering at that bar's fill reference
        would mean entering at a price that occurred *before* the exit.
    """

    enabled: bool = False
    stop_mode: str = "hybrid"
    tp_priority: bool = True

    stop_distance_mode: str = "atr"
    stop_distance: float = 2.0
    tp_distance_mode: str = "atr"
    tp_distance: float | None = 4.0

    slippage_bps: float = 0.0
    take_profit_slippage_bps: float = 0.0
    max_slippage_bps: float = 50.0

    record_recommended_fill: bool = True
    reenter_same_bar: bool = False

    def __post_init__(self) -> None:
        if self.stop_mode not in STOP_MODES:
            raise ValueError(
                f"unknown stop_mode {self.stop_mode!r}; expected one of {STOP_MODES}"
            )
        for name, mode in (
            ("stop_distance_mode", self.stop_distance_mode),
            ("tp_distance_mode", self.tp_distance_mode),
        ):
            if mode not in DISTANCE_MODES:
                raise ValueError(
                    f"unknown {name} {mode!r}; expected one of {DISTANCE_MODES}"
                )
        if self.stop_distance <= 0:
            raise ValueError(f"stop_distance must be > 0, got {self.stop_distance}")
        if self.tp_distance is not None and self.tp_distance <= 0:
            raise ValueError(
                f"tp_distance must be > 0 or None, got {self.tp_distance}"
            )
        for name, value in (
            ("slippage_bps", self.slippage_bps),
            ("take_profit_slippage_bps", self.take_profit_slippage_bps),
            ("max_slippage_bps", self.max_slippage_bps),
        ):
            if value < 0:
                raise ValueError(f"{name} must be >= 0, got {value}")

    @property
    def stop_slippage_fraction(self) -> float:
        """Stop slippage as a fraction of price, after the cap."""
        return min(self.slippage_bps, self.max_slippage_bps) / 10_000.0

    @property
    def take_profit_slippage_fraction(self) -> float:
        """Target slippage as a fraction of price, after the cap."""
        return min(self.take_profit_slippage_bps, self.max_slippage_bps) / 10_000.0


@dataclass(frozen=True, slots=True)
class BreachEvent:
    """One stop or target being reached.

    Attributes
    ----------
    kind:
        ``"stop"`` or ``"take_profit"``.
    breach_type:
        ``"gap"`` when the bar opened beyond the level, ``"intrabar"`` when
        the level was reached during the session.
    level:
        Where the order was resting.
    reference_price:
        The price the fill is struck against before slippage — the level, or
        the open on a gap.
    fill_price:
        What it is assumed to have filled at, after slippage.
    recommended_fill:
        The price to work when executing this manually. Identical to
        ``fill_price``; named separately because it is what a human acts on
        rather than what the simulator booked.
    ambiguous:
        ``True`` when the bar also breached the other level intrabar, so
        which came first was decided by ``tp_priority`` rather than by data.
    """

    asset: str
    timestamp: pd.Timestamp
    kind: str
    direction: str
    breach_type: str
    level: float
    reference_price: float
    fill_price: float
    recommended_fill: float
    slippage_applied: float
    slippage_bps: float
    ambiguous: bool = False

    @property
    def gap_through(self) -> bool:
        """Whether the level was gapped through rather than traded through."""
        return self.breach_type == "gap"

    def as_row(self, units_closed: float = 0.0) -> dict:
        """Flatten to an event-log row."""
        return {
            "timestamp": self.timestamp,
            "asset": self.asset,
            "kind": self.kind,
            "direction": self.direction,
            "breach_type": self.breach_type,
            "gap_through": self.gap_through,
            "level": self.level,
            "reference_price": self.reference_price,
            "fill_price": self.fill_price,
            "recommended_fill": self.recommended_fill,
            "slippage_applied": self.slippage_applied,
            "slippage_bps": self.slippage_bps,
            "units_closed": units_closed,
            "ambiguous": self.ambiguous,
        }


@dataclass(frozen=True, slots=True)
class StopEvent(BreachEvent):
    """A stop-loss being reached."""


@dataclass(frozen=True, slots=True)
class TakeProfitEvent(BreachEvent):
    """A take-profit being reached."""


# --------------------------------------------------------------------------- #
# Levels
# --------------------------------------------------------------------------- #
def level_distance(entry_price, atr, mode: str, distance: float) -> np.ndarray:
    """Convert a configured distance into price units.

    Pure and vectorised.

    ``atr``
        ``distance x ATR``. Sizes the level against *this* instrument's own
        noise, so one setting works across a quiet bond ETF and a violent
        volatility product.
    ``fixed``
        ``distance``, already in price units.
    ``percent``
        ``distance x entry price``.

    Returns
    -------
    numpy.ndarray
        NaN where the inputs cannot support the chosen mode.
    """
    entry = np.asarray(entry_price, dtype="float64")
    ranges = np.asarray(atr, dtype="float64")

    if mode == "atr":
        return np.where(np.isfinite(ranges) & (ranges > 0), distance * ranges, np.nan)
    if mode == "fixed":
        return np.full(entry.shape, float(distance))
    if mode == "percent":
        return np.where(np.isfinite(entry) & (entry > 0), distance * entry, np.nan)
    raise ValueError(f"unknown distance mode {mode!r}; expected {DISTANCE_MODES}")


def stop_levels(
    entry_price,
    atr,
    direction,
    config: StopConfig | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Place the stop and target relative to entry.

    Pure and vectorised across assets.

    Parameters
    ----------
    entry_price:
        Weighted-average entry of each position. NaN where flat.
    atr:
        Average true range at entry, in price units. Used by ``"atr"`` mode.
    direction:
        ``+1`` long, ``-1`` short, ``0`` flat.

    Returns
    -------
    tuple
        ``(stop, take_profit)``. NaN where there is no position, or the
        chosen distance mode has no usable input.
    """
    settings = config or StopConfig()

    entry = np.asarray(entry_price, dtype="float64")
    side = np.asarray(direction, dtype="float64")
    open_position = (side != 0) & np.isfinite(entry)

    stop_gap = level_distance(
        entry, atr, settings.stop_distance_mode, settings.stop_distance
    )
    stop = np.where(open_position & np.isfinite(stop_gap), entry - side * stop_gap, np.nan)

    if settings.tp_distance is None:
        target = np.full(stop.shape, np.nan)
    else:
        target_gap = level_distance(
            entry, atr, settings.tp_distance_mode, settings.tp_distance
        )
        target = np.where(
            open_position & np.isfinite(target_gap), entry + side * target_gap, np.nan
        )
    return stop, target


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #
def detect_breaches(
    direction,
    open_price,
    high,
    low,
    stop,
    take_profit,
    config: StopConfig | None = None,
) -> dict[str, np.ndarray]:
    """Decide, per asset, whether a level was reached and at what price.

    Pure and vectorised. The order of resolution is what makes the answer
    defensible:

    1. **Gaps first.** If the bar opened beyond a level, that level was
       reached at the open and nothing on the bar can have preceded it. When
       *both* levels were gapped through, the one nearer the open was reached
       first — a fact, not a convention.
    2. **Then the intrabar range**, which may contain both levels.
    3. **Then ``tp_priority``**, but only for the genuinely ambiguous case.

    Returns
    -------
    dict of numpy.ndarray
        ``breached``, ``is_take_profit``, ``breach_type`` (object),
        ``level``, ``reference_price``, ``fill_price``, ``recommended_fill``,
        ``slippage_applied``, ``slippage_bps``, ``ambiguous``.
    """
    settings = config or StopConfig()

    side = np.asarray(direction, dtype="float64")
    opens = np.asarray(open_price, dtype="float64")
    highs = np.asarray(high, dtype="float64")
    lows = np.asarray(low, dtype="float64")
    stops = np.asarray(stop, dtype="float64")
    targets = np.asarray(take_profit, dtype="float64")

    shape = side.shape
    long, short = side > 0, side < 0
    has_stop = np.isfinite(stops) & (side != 0)
    has_target = np.isfinite(targets) & (side != 0)
    has_open = np.isfinite(opens)

    # -- gap-through: the open is already beyond the level ------------------ #
    gap_stop = has_stop & has_open & (
        (long & (opens <= stops)) | (short & (opens >= stops))
    )
    gap_target = has_target & has_open & (
        (long & (opens >= targets)) | (short & (opens <= targets))
    )

    # Both gapped: the nearer level to the open was crossed first. Only
    # reachable with unusually placed levels, but it must resolve to
    # something, and "nearest" is the only answer the data supports.
    both_gapped = gap_stop & gap_target
    target_nearer = both_gapped & (
        np.abs(opens - targets) <= np.abs(opens - stops)
    )
    gap_stop = np.where(both_gapped, ~target_nearer, gap_stop)
    gap_target = np.where(both_gapped, target_nearer, gap_target)

    # -- intrabar: the range reached the level ------------------------------ #
    hit_stop = has_stop & (
        (long & np.isfinite(lows) & (lows <= stops))
        | (short & np.isfinite(highs) & (highs >= stops))
    )
    hit_target = has_target & (
        (long & np.isfinite(highs) & (highs >= targets))
        | (short & np.isfinite(lows) & (lows <= targets))
    )

    gapped_any = gap_stop | gap_target
    ambiguous = hit_stop & hit_target & ~gapped_any
    target_wins_ambiguous = ambiguous if settings.tp_priority else np.zeros(
        shape, dtype=bool
    )

    take_target = gap_target | (hit_target & ~hit_stop & ~gap_stop) | target_wins_ambiguous
    take_stop = gap_stop | (hit_stop & ~take_target)

    breached = take_stop | take_target
    is_take_profit = take_target & ~take_stop

    gapped = (take_stop & gap_stop) | (is_take_profit & gap_target)
    level = np.where(is_take_profit, targets, stops)

    # -- the fill reference, per stop_mode ---------------------------------- #
    if settings.stop_mode == "breach":
        # Optimistic: always the level, even when the market never offered it.
        reference = level
    elif settings.stop_mode == "gap":
        # Conservative: always the open, even for a calm mid-session touch.
        reference = np.where(has_open, opens, level)
    else:
        reference = np.where(gapped, opens, level)

    breach_type = np.where(breached, np.where(gapped, "gap", "intrabar"), "")

    fraction = np.where(
        is_take_profit,
        settings.take_profit_slippage_fraction,
        settings.stop_slippage_fraction,
    )
    # Slippage is always against the position: a long exits lower, a short
    # exits higher. Never in the trader's favour.
    fill = np.where(breached, reference * (1.0 - side * fraction), np.nan)
    slippage = np.where(breached, np.abs(fill - reference), 0.0)

    recommended = fill if settings.record_recommended_fill else np.full(shape, np.nan)

    return {
        "breached": breached,
        "is_take_profit": is_take_profit,
        "breach_type": breach_type,
        "level": np.where(breached, level, np.nan),
        "reference_price": np.where(breached, reference, np.nan),
        "fill_price": fill,
        "recommended_fill": recommended,
        "slippage_applied": slippage,
        "slippage_bps": np.where(breached, fraction * 10_000.0, 0.0),
        "ambiguous": ambiguous,
    }


def resolve_breach(
    asset: str,
    timestamp,
    direction: str,
    open_price: float,
    high: float,
    low: float,
    stop: float | None,
    take_profit: float | None = None,
    config: StopConfig | None = None,
) -> BreachEvent | None:
    """Scalar form of :func:`detect_breaches`, returning a typed event.

    The convenient entry point for a single position — a test, a notebook,
    or a manual check of what a level would have done.

    Returns
    -------
    StopEvent, TakeProfitEvent or None
        ``None`` when neither level was reached.

    Raises
    ------
    ValueError
        On a direction other than ``"long"`` or ``"short"``.
    """
    if direction not in {"long", "short"}:
        raise ValueError(f"direction must be 'long' or 'short', got {direction!r}")

    result = detect_breaches(
        np.array([1.0 if direction == "long" else -1.0]),
        np.array([open_price], dtype="float64"),
        np.array([high], dtype="float64"),
        np.array([low], dtype="float64"),
        np.array([np.nan if stop is None else stop], dtype="float64"),
        np.array([np.nan if take_profit is None else take_profit], dtype="float64"),
        config,
    )

    if not bool(result["breached"][0]):
        return None

    kind = "take_profit" if bool(result["is_take_profit"][0]) else "stop"
    factory = TakeProfitEvent if kind == "take_profit" else StopEvent
    return factory(
        asset=asset,
        timestamp=pd.Timestamp(timestamp),
        kind=kind,
        direction=direction,
        breach_type=str(result["breach_type"][0]),
        level=float(result["level"][0]),
        reference_price=float(result["reference_price"][0]),
        fill_price=float(result["fill_price"][0]),
        recommended_fill=float(result["recommended_fill"][0]),
        slippage_applied=float(result["slippage_applied"][0]),
        slippage_bps=float(result["slippage_bps"][0]),
        ambiguous=bool(result["ambiguous"][0]),
    )


# --------------------------------------------------------------------------- #
# Manual execution
# --------------------------------------------------------------------------- #
def recommended_orders(
    positions: pd.Series,
    entry_price: pd.Series,
    stop: pd.Series,
    take_profit: pd.Series,
    last_price: pd.Series,
    *,
    timestamp=None,
) -> pd.DataFrame:
    """The working orders a human should place, for the current book.

    **Nothing here is sent anywhere.** The simulator assumes a resting order
    exists; a manually traded account only has one if you place it. This is
    the list to place, and the gap between the two is the difference between
    the backtest and the account.

    Pure.

    Returns
    -------
    pandas.DataFrame
        One row per open position: ``direction``, ``units``,
        ``entry_price``, ``last_price``, ``stop_level``, ``stop_order``,
        ``take_profit_level``, ``take_profit_order``, and the distance to
        each level as a fraction of the last price.
    """
    columns = [
        "direction", "units", "entry_price", "last_price", "stop_level",
        "stop_order", "take_profit_level", "take_profit_order",
        "distance_to_stop", "distance_to_take_profit",
    ]
    if positions is None or positions.empty:
        return pd.DataFrame(columns=columns)

    open_positions = positions[positions != 0]
    if open_positions.empty:
        return pd.DataFrame(columns=columns)

    rows = []
    for asset, units in open_positions.items():
        is_long = units > 0
        stop_level = float(stop.get(asset, np.nan))
        target_level = float(take_profit.get(asset, np.nan))
        price = float(last_price.get(asset, np.nan))
        size = abs(float(units))

        rows.append(
            {
                "asset": asset,
                "direction": "long" if is_long else "short",
                "units": float(units),
                "entry_price": float(entry_price.get(asset, np.nan)),
                "last_price": price,
                "stop_level": stop_level,
                # A stop on a long is a sell-stop below the market; on a short
                # it is a buy-stop above. Spelling out the side avoids the
                # classic manual error of placing the wrong one.
                "stop_order": (
                    f"SELL STOP {size:,.4f} @ {stop_level:,.4f}"
                    if is_long
                    else f"BUY STOP {size:,.4f} @ {stop_level:,.4f}"
                )
                if np.isfinite(stop_level)
                else "",
                "take_profit_level": target_level,
                "take_profit_order": (
                    f"SELL LIMIT {size:,.4f} @ {target_level:,.4f}"
                    if is_long
                    else f"BUY LIMIT {size:,.4f} @ {target_level:,.4f}"
                )
                if np.isfinite(target_level)
                else "",
                "distance_to_stop": (stop_level - price) / price
                if np.isfinite(stop_level) and np.isfinite(price) and price > 0
                else np.nan,
                "distance_to_take_profit": (target_level - price) / price
                if np.isfinite(target_level) and np.isfinite(price) and price > 0
                else np.nan,
            }
        )

    frame = pd.DataFrame(rows).set_index("asset")
    if timestamp is not None:
        frame.insert(0, "as_of", pd.Timestamp(timestamp))
        return frame[["as_of", *columns]]
    return frame[columns]
