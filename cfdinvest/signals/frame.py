"""The :class:`SignalFrame`: what to hold, how much, and why.

Reads only from :class:`~cfdinvest.preprocessing.AnalysisFrame` and
:class:`~cfdinvest.regimes.RegimeFrame`, and writes to neither. The output is
an *intention* — a target position per asset per bar. Turning intentions into
fills, costs and P&L is the execution layer's job, and nothing here assumes
anything about how that happens.

Guarantees the frame makes, all asserted by the test suite:

* Every cell has a direction in ``{long, short, flat}`` — never NaN.
* ``size`` and ``confidence`` are finite and within their bounds.
* ``direction == "flat"`` if and only if ``size == 0``.
* Every cell carries a non-empty ``reason``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Sequence

import numpy as np
import pandas as pd

from cfdinvest.signals import combined as combined_module
from cfdinvest.signals import filters as filters_module
from cfdinvest.signals import sizing as sizing_module
from cfdinvest.signals import trend as trend_module
from cfdinvest.schema import TICKER, TIMESTAMP

logger = logging.getLogger(__name__)

__all__ = ["SignalConfig", "SignalFrame", "generate_signals"]


@dataclass(frozen=True, slots=True)
class SignalConfig:
    """Windows, thresholds and filter policy for the signal engine."""

    # -- trend ------------------------------------------------------------ #
    fast_ma: int = 20
    slow_ma: int = 100
    breakout_window: int = 55
    momentum_full_scale: float = 0.10
    #: Relative weight of each trend component in the blended strength.
    component_weights: dict[str, float] = field(
        default_factory=lambda: {"crossover": 1.0, "breakout": 1.0, "momentum": 1.0}
    )
    #: Hold instruments with no Trading 212 CFD flat. Off only for
    #: research on the raw signal, never for a run you would act on.
    require_tradable: bool = True
    #: Absolute strength required to want a position.
    entry_threshold: float = 0.3

    # -- sizing ----------------------------------------------------------- #
    target_volatility: float = 0.15
    floor_volatility: float = 0.02
    risk_fraction: float = 0.01
    stop_multiple: float = 2.0
    floor_atr_percent: float = 0.002
    max_size: float = 1.0
    #: Sizes below this are not worth the spread; the signal goes flat.
    min_size: float = 0.02
    #: Floor on conviction scaling. 1.0 disables it.
    conviction_floor: float = 0.25
    #: Cap on summed gross exposure per bar. ``None`` disables.
    max_gross_exposure: float | None = None

    # -- regime filter ---------------------------------------------------- #
    long_regimes: frozenset[str] = filters_module.DEFAULT_LONG_REGIMES
    short_regimes: frozenset[str] = filters_module.DEFAULT_SHORT_REGIMES
    short_macro_regimes: frozenset[str] = frozenset({"contraction"})
    high_volatility_multiplier: float = 0.5
    trade_when_regime_unknown: bool = False
    #: Per-asset-class overrides of ``long_regimes``, as
    #: ``{class: regimes}``. ``None`` keeps one verdict for the whole book,
    #: which is what every recorded run used.
    #: Whether the environment multiplier is folded into the confidence
    #: score. ``True`` is the recorded behaviour; ``False`` keeps confidence
    #: a statement about the signal and leaves the environment to sizing.
    environment_in_confidence: bool = True
    #: ``"gate"`` (default, and what every recorded run used) permits a
    #: direction or forbids it. ``"tilt"`` scales size by how favourable the
    #: regime is instead, keeping the engine's own ranking rather than
    #: discarding it at a boundary.
    regime_mode: str = "gate"
    long_tilts: dict[str, float] | None = None
    short_tilts: dict[str, float] | None = None
    long_regimes_by_class: dict[str, frozenset[str]] | None = None
    #: Ticker -> class map for the above. ``None`` uses the module default.
    asset_classes: dict[str, str] | None = None

    # -- correlation filter ----------------------------------------------- #
    breakdown_multiplier: float = 0.0
    #: Whether breakdown suppression also applies to shorts. ``False``
    #: keeps shorts reachable; see ``filters.correlation_filter``.
    breakdown_applies_to_shorts: bool = False
    use_diversification: bool = False
    diversification_floor: float = 0.4

    # -- macro filter ----------------------------------------------------- #
    asset_classes: dict[str, str] | None = None
    inflation_hedge_classes: frozenset[str] = frozenset({"commodity", "fx"})
    inflation_penalty: float = 0.5
    loosening_labor_multiplier: float = 0.75
    block_equity_longs_in_contraction: bool = True


@dataclass(frozen=True, slots=True)
class SignalFrame:
    """Target positions with their justification.

    Attributes
    ----------
    direction:
        ``timestamp x ticker`` of ``long`` / ``short`` / ``flat``.
    size:
        Target position size in ``[0, max_size]``. Unsigned — the sign lives
        in ``direction``, so a caller cannot accidentally read a short as a
        negative long and double-count it.
    confidence:
        ``[0, 1]``. Conviction, component agreement and environmental
        support, multiplied.
    reason:
        Compact per-cell explanation: the rule that fired plus the filters
        that bit.
    diagnostics:
        The intermediate panels — strength, component agreement, raw
        direction, the surviving size multiplier — kept for inspection.
    metadata:
        Windows, thresholds, universe and calendar bounds.
    """

    direction: pd.DataFrame = field(default_factory=pd.DataFrame)
    size: pd.DataFrame = field(default_factory=pd.DataFrame)
    confidence: pd.DataFrame = field(default_factory=pd.DataFrame)
    reason: pd.DataFrame = field(default_factory=pd.DataFrame)
    diagnostics: dict[str, pd.DataFrame] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    # -- views ------------------------------------------------------------ #
    @property
    def index(self) -> pd.Index:
        """The calendar every panel shares."""
        return self.direction.index

    @property
    def tickers(self) -> tuple[str, ...]:
        """The universe."""
        return tuple(self.direction.columns)

    @property
    def signed_size(self) -> pd.DataFrame:
        """Size with the direction's sign applied: ``+`` long, ``-`` short.

        The form the execution layer wants; kept as a derived view rather
        than a stored panel so the two can never disagree.
        """
        if self.direction.empty:
            return pd.DataFrame()
        sign = pd.DataFrame(
            np.select(
                [self.direction == "long", self.direction == "short"], [1.0, -1.0],
                default=0.0,
            ),
            index=self.direction.index,
            columns=self.direction.columns,
        )
        return self.size * sign

    def active(self) -> pd.DataFrame:
        """Boolean panel of cells holding a position."""
        if self.direction.empty:
            return pd.DataFrame()
        return self.direction != "flat"

    def to_long(self, *, only_active: bool = True) -> pd.DataFrame:
        """Return a tidy ``(timestamp, ticker)`` view of every signal.

        Parameters
        ----------
        only_active:
            Drop flat cells, which are the overwhelming majority.
        """
        if self.direction.empty:
            return pd.DataFrame(
                columns=["direction", "size", "confidence", "reason"]
            )

        tidy = pd.DataFrame(
            {
                "direction": self.direction.stack(),
                "size": self.size.stack(),
                "confidence": self.confidence.stack(),
                "reason": self.reason.stack(),
            }
        )
        tidy.index.names = [TIMESTAMP, TICKER]
        if only_active:
            tidy = tidy[tidy["direction"] != "flat"]
        return tidy.sort_index()

    def summary(self) -> pd.DataFrame:
        """Per-ticker counts and averages, for a sanity check."""
        if self.direction.empty:
            return pd.DataFrame()

        active = self.active()
        return pd.DataFrame(
            {
                "long_bars": (self.direction == "long").sum(),
                "short_bars": (self.direction == "short").sum(),
                "flat_bars": (self.direction == "flat").sum(),
                "active_share": active.mean(),
                "mean_size": self.size.where(active).mean(),
                "mean_confidence": self.confidence.where(active).mean(),
            }
        ).sort_index()

    def reason_counts(self, top: int = 15) -> pd.Series:
        """Most common reasons across the panel — where signals go to die."""
        if self.reason.empty:
            return pd.Series(dtype="int64")
        flat = self.reason.to_numpy().ravel()
        return pd.Series(flat).value_counts().head(top)


# --------------------------------------------------------------------------- #
# Builder
# --------------------------------------------------------------------------- #
def generate_signals(
    analysis_frame,
    regime_frame=None,
    config: SignalConfig | None = None,
) -> SignalFrame:
    """Produce target positions from preprocessing and regime outputs.

    Pure with respect to its inputs: both frames are read, never mutated.

    Parameters
    ----------
    analysis_frame:
        A :class:`~cfdinvest.preprocessing.AnalysisFrame`.
    regime_frame:
        A :class:`~cfdinvest.regimes.RegimeFrame`. ``None`` runs unfiltered,
        which is useful for isolating the trend engine in a test but should
        not be the production path.
    config:
        Thresholds and policy.

    Returns
    -------
    SignalFrame
    """
    settings = config or SignalConfig()
    close = analysis_frame.prices.get("close", pd.DataFrame())

    if close.empty:
        logger.warning("No close prices; returning an empty SignalFrame")
        return SignalFrame(metadata={"bars": 0, "tickers": ()})

    index, columns = close.index, close.columns

    # -- trend --------------------------------------------------------------
    components = trend_module.trend_components(
        close,
        analysis_frame.returns.get("rolling"),
        analysis_frame.prices.get("high"),
        analysis_frame.prices.get("low"),
        fast=settings.fast_ma,
        slow=settings.slow_ma,
        breakout_window=settings.breakout_window,
        momentum_full_scale=settings.momentum_full_scale,
    )
    strength = trend_module.trend_strength(components, settings.component_weights)
    agreement = trend_module.component_agreement(components)

    if strength.empty:
        logger.warning("No trend components could be computed")
        strength = pd.DataFrame(np.nan, index=index, columns=columns)

    strength = strength.reindex(index=index, columns=columns)
    agreement = (
        agreement.reindex(index=index, columns=columns)
        if not agreement.empty
        else pd.DataFrame(1.0, index=index, columns=columns)
    )

    # -- sizing -------------------------------------------------------------
    base_size = _base_size(analysis_frame, settings, index, columns)

    # -- filters ------------------------------------------------------------
    outcome = _build_filters(analysis_frame, regime_frame, settings, index, columns)

    raw = combined_module.raw_direction(strength, settings.entry_threshold)
    applicable_multiplier = outcome.multiplier_for(raw)

    size = (base_size * applicable_multiplier).clip(0.0, settings.max_size)
    size = sizing_module.scale_by_conviction(
        size, strength, min_scale=settings.conviction_floor, max_size=settings.max_size
    )
    if settings.max_gross_exposure is not None:
        size = sizing_module.cap_gross_exposure(
            size, max_gross=settings.max_gross_exposure
        )
    size = size.fillna(0.0)

    # -- resolution ---------------------------------------------------------
    size_ok = size >= settings.min_size

    direction, rule = combined_module.resolve_directions(
        raw, outcome.allow_long, outcome.allow_short, size_ok
    )

    # -- tradability --------------------------------------------------------
    # An instrument with no Trading 212 CFD is held flat, always. It is in
    # the universe because something needs it -- the VIX drives the regime
    # classification -- and taking a position in it would be worse than
    # useless: the recommendation could not be acted on, and the backtest
    # would be scoring P&L from a position that could never have existed.
    untradable = [
        column for column in columns
        if not _is_tradable(column, settings)
    ]
    if untradable:
        direction.loc[:, untradable] = "flat"
        logger.info(
            "Held flat, not tradable on Trading 212: %s", ", ".join(untradable)
        )

    is_flat = direction == "flat"
    size = size.mask(is_flat, 0.0)

    confidence = combined_module.confidence_score(
        strength, agreement, applicable_multiplier, direction,
        include_environment=settings.environment_in_confidence,
    )

    context = (
        filters_module.broadcast(regime_frame.combined_regime, index, columns)
        if regime_frame is not None and len(regime_frame.index)
        else None
    )
    reason = combined_module.build_reasons(
        rule, outcome.reason_flags(), context
    )

    metadata = {
        "bars": len(index),
        "tickers": tuple(columns),
        "calendar_start": index.min() if len(index) else None,
        "calendar_end": index.max() if len(index) else None,
        "components": tuple(sorted(components)),
        "entry_threshold": settings.entry_threshold,
        "min_size": settings.min_size,
        "max_size": settings.max_size,
        "regime_filtered": regime_frame is not None,
        "direction_table_size": len(combined_module.direction_decision_table()),
        "windows": {
            "fast_ma": settings.fast_ma,
            "slow_ma": settings.slow_ma,
            "breakout": settings.breakout_window,
        },
        "active_bars": int((direction != "flat").to_numpy().sum()),
        "untradable": tuple(untradable),
    }

    return SignalFrame(
        direction=direction,
        size=size,
        confidence=confidence,
        reason=reason,
        diagnostics={
            "strength": strength,
            "agreement": agreement,
            "raw_direction": raw,
            "base_size": base_size,
            "size_multiplier": applicable_multiplier,
            "long_multiplier": outcome.long_multiplier,
            "short_multiplier": outcome.short_multiplier,
            "rule": rule,
        },
        metadata=metadata,
    )


def _is_tradable(ticker: str, settings) -> bool:
    """Whether a position in this instrument could actually be opened.

    Unknown instruments are treated as tradable. The mapping table is a
    record of what has been looked up, not a permission list, and a ticker
    nobody has classified yet should not be silently dropped from a
    backtest -- that failure would be invisible.
    """
    if not getattr(settings, "require_tradable", True):
        return True
    try:
        from cfdinvest.livehelper.t212_instruments import mapping_for

        return mapping_for(str(ticker)).relationship != "none"
    except Exception:  # noqa: BLE001 - never fail signal generation over this
        return True


def _base_size(analysis_frame, settings, index, columns) -> pd.DataFrame:
    """Blend the volatility-target and ATR-risk sizes, most conservative first."""
    realized = analysis_frame.volatility.get("realized", pd.DataFrame())
    atr = analysis_frame.volatility.get("atr")
    close = analysis_frame.prices.get("close")

    vol_size = sizing_module.volatility_target_size(
        realized,
        target_volatility=settings.target_volatility,
        max_size=settings.max_size,
        floor_volatility=settings.floor_volatility,
    )

    atr_size = pd.DataFrame()
    if atr is not None and not atr.empty and close is not None:
        atr_pct = atr / close.where(close > 0)
        atr_size = sizing_module.atr_risk_size(
            atr_pct,
            risk_fraction=settings.risk_fraction,
            stop_multiple=settings.stop_multiple,
            max_size=settings.max_size,
            floor_atr_percent=settings.floor_atr_percent,
        )

    blended = sizing_module.blend_sizes(vol_size, atr_size, max_size=settings.max_size)
    if blended.empty:
        # No volatility estimate at all: fall back to a flat unit size and let
        # the filters and the minimum-size rule do the work.
        logger.debug("No volatility inputs; using a flat base size")
        blended = pd.DataFrame(settings.max_size, index=index, columns=columns)
    return blended.reindex(index=index, columns=columns).fillna(0.0)


def _build_filters(analysis_frame, regime_frame, settings, index, columns):
    """Compose the regime, correlation and macro filters."""
    if regime_frame is None or len(regime_frame.index) == 0:
        logger.debug("No regime frame; signals will be unfiltered")
        return filters_module.permissive_outcome(index, columns)

    components = regime_frame.components

    regime = filters_module.regime_filter(
        regime_frame.combined_regime,
        regime_frame.volatility_regime,
        regime_frame.macro_regime,
        index,
        columns,
        long_regimes=settings.long_regimes,
        short_regimes=settings.short_regimes,
        short_macro_regimes=settings.short_macro_regimes,
        high_volatility_multiplier=settings.high_volatility_multiplier,
        allow_when_unknown=settings.trade_when_regime_unknown,
        mode=settings.regime_mode,
        long_tilts=settings.long_tilts,
        short_tilts=settings.short_tilts,
    )

    diversification = None
    if settings.use_diversification:
        diversification = filters_module.diversification_multiplier(
            analysis_frame.correlations.get("rolling"),
            index,
            columns,
            floor=settings.diversification_floor,
        )

    correlation = filters_module.correlation_filter(
        regime_frame.correlation_regime,
        index,
        columns,
        breakdown_multiplier=settings.breakdown_multiplier,
        applies_to_shorts=settings.breakdown_applies_to_shorts,
        diversification=diversification,
    )

    macro = filters_module.macro_filter(
        components.get("growth_regime"),
        components.get("inflation_regime"),
        components.get("labor_regime"),
        index,
        columns,
        asset_classes=settings.asset_classes,
        inflation_hedge_classes=settings.inflation_hedge_classes,
        inflation_penalty=settings.inflation_penalty,
        loosening_labor_multiplier=settings.loosening_labor_multiplier,
        block_equity_longs_in_contraction=settings.block_equity_longs_in_contraction,
    )

    return filters_module.combine_outcomes(regime, correlation, macro)
