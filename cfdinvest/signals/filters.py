"""Regime, correlation and macro filters.

A filter never creates a signal — it can only permit, forbid, or shrink one.
That asymmetry is deliberate: the trend module decides *what* looks tradable,
and everything here decides whether the environment allows acting on it. No
filter is allowed to return a multiplier above 1.0, so no combination of
conditions can lever the book up.

Each filter returns a :class:`FilterOutcome` carrying three panels and an
audit trail:

``allow_long`` / ``allow_short``
    Boolean permission, per asset per bar.
``long_multiplier`` / ``short_multiplier``
    Factors in ``[0, 1]`` applied to the position size, kept separate
    by direction. This matters more than it looks: a correlation
    breakdown is a reason to stop *adding risk*, but it is also the
    condition that defines a crisis, and a crisis is when shorts are
    permitted. A single shared multiplier would suppress the only
    direction the regime allows, and shorts could never fire at all.
``flags``
    Named boolean masks recording *where each rule bit*, which is what lets
    the combined signal explain itself rather than merely produce a number.

Outcomes compose with :func:`combine_outcomes`: permissions AND together,
multipliers multiply, flags merge. All functions are pure and vectorised.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

__all__ = [
    "ASSET_CLASSES",
    "REASON_FLAGS",
    "DEFAULT_LONG_REGIMES",
    "DEFAULT_SHORT_REGIMES",
    "LONG_REGIME_TILTS",
    "SHORT_REGIME_TILTS",
    "FilterOutcome",
    "broadcast",
    "classify_assets",
    "permissive_outcome",
    "regime_filter",
    "volume_filter",
    "correlation_filter",
    "macro_filter",
    "diversification_multiplier",
    "combine_outcomes",
]

#: Default ticker -> asset class map, covering the universe in config.toml.
#: Unlisted tickers fall through to ``"other"`` and are never special-cased
#: by the macro tilts, which is the safe default for an unknown instrument.
ASSET_CLASSES: dict[str, str] = {
    **{
        t: "equity"
        for t in (
            "SPY", "QQQ", "IWM", "DIA", "XLK", "XLF", "XLE", "XLV",
            "EFA", "EEM", "EWJ", "EWG", "^GSPC",
        )
    },
    **{t: "bond" for t in ("TLT", "IEF", "HYG", "LQD", "AGG", "BND")},
    **{t: "commodity" for t in ("GLD", "SLV", "USO", "DBC", "USCI")},
    **{
        t: "fx"
        for t in ("EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X", "USDCHF=X")
    },
    **{t: "volatility" for t in ("^VIX", "VIXY")},
}

#: Combined regimes in which new longs are permitted.
DEFAULT_LONG_REGIMES: frozenset[str] = frozenset({"risk_on", "expansion"})

#: Combined regimes in which new shorts are permitted.
DEFAULT_SHORT_REGIMES: frozenset[str] = frozenset({"crisis"})

#: Size tilts per regime, for :func:`regime_filter` in ``"tilt"`` mode.
#:
#: A gate answers "may I?"; a tilt answers "how much?". The difference
#: matters because the gate throws away the engine's own ranking: a strong
#: trend in a middling regime and a weak one in the same regime are both
#: refused outright, and a marginal trend in a favourable regime gets full
#: size. Tilting keeps the ordering and scales it.
#:
#: The numbers are monotone in how favourable the regime is for the
#: direction, and deliberately round -- they are a hypothesis about shape,
#: not a fitted parameter set. ``crisis`` still forbids new longs and
#: ``expansion`` still forbids new shorts, so the extremes stay absolute.
LONG_REGIME_TILTS: dict[str, float] = {
    "risk_on": 1.0, "expansion": 1.0,
    "neutral": 0.6, "overheating": 0.6,
    "slowdown": 0.4, "stagflation": 0.3,
    "risk_off": 0.25, "crisis": 0.0, "unknown": 0.0,
}

SHORT_REGIME_TILTS: dict[str, float] = {
    "crisis": 1.0, "risk_off": 0.6,
    "stagflation": 0.5, "slowdown": 0.4,
    "neutral": 0.25, "overheating": 0.25,
    "risk_on": 0.0, "expansion": 0.0, "unknown": 0.0,
}



#: Flags worth naming in a signal's reason string. The regime block flags
#: are omitted deliberately: the rule name already says "blocked_long",
#: so repeating it adds length without information.
REASON_FLAGS: tuple[str, ...] = (
    "high_volatility",
    "correlation_breakdown",
    "inflation_tilt",
    "labor_risk_off",
    "contraction_blocks_equity_long",
)


@dataclass(frozen=True, slots=True)
class FilterOutcome:
    """Permissions, per-direction size multipliers, and the audit trail."""

    allow_long: pd.DataFrame
    allow_short: pd.DataFrame
    long_multiplier: pd.DataFrame
    short_multiplier: pd.DataFrame
    flags: dict[str, pd.DataFrame] = field(default_factory=dict)

    def multiplier_for(self, raw_direction: pd.DataFrame) -> pd.DataFrame:
        """Select the multiplier that applies to what was wanted.

        Cells wanting a long take ``long_multiplier``; everything else
        takes ``short_multiplier``. Flat cells are zeroed downstream, so
        which one they receive does not matter.
        """
        wants_long = raw_direction.reindex_like(self.long_multiplier).eq(
            "long"
        )
        return self.long_multiplier.where(wants_long, self.short_multiplier)

    def reason_flags(self) -> dict[str, pd.DataFrame]:
        """The subset of flags that belong in a human-readable reason."""
        return {k: v for k, v in self.flags.items() if k in REASON_FLAGS}


def broadcast(
    series: pd.Series | None,
    index: pd.Index,
    columns: pd.Index,
    *,
    fill: str = "unknown",
) -> pd.DataFrame:
    """Spread a market-level series across every asset column.

    Regimes are computed once for the whole market; filters apply per asset.
    This is the bridge, and it exists as a named function so the broadcast is
    visible rather than an incidental ``np.repeat`` buried in a filter.
    """
    if series is None or len(series) == 0:
        return pd.DataFrame(fill, index=index, columns=columns, dtype="object")

    values = series.reindex(index).astype("object").fillna(fill).to_numpy()
    return pd.DataFrame(
        np.repeat(values[:, None], len(columns), axis=1),
        index=index,
        columns=columns,
        dtype="object",
    )


def classify_assets(
    columns: pd.Index, mapping: dict[str, str] | None = None
) -> pd.Series:
    """Map each ticker to an asset class, defaulting to ``"other"``."""
    resolved = ASSET_CLASSES if mapping is None else mapping
    return pd.Series(
        [resolved.get(str(ticker), "other") for ticker in columns],
        index=columns,
        dtype="object",
        name="asset_class",
    )


def permissive_outcome(index: pd.Index, columns: pd.Index) -> FilterOutcome:
    """An outcome that forbids nothing and shrinks nothing.

    The identity element for :func:`combine_outcomes`, and what a filter
    returns when its inputs are unavailable — an absent regime reading must
    not silently block trading.
    """
    return FilterOutcome(
        allow_long=pd.DataFrame(True, index=index, columns=columns),
        allow_short=pd.DataFrame(True, index=index, columns=columns),
        long_multiplier=pd.DataFrame(1.0, index=index, columns=columns),
        short_multiplier=pd.DataFrame(1.0, index=index, columns=columns),
        flags={},
    )


def regime_filter(
    combined_regime: pd.Series | None,
    volatility_regime: pd.Series | None,
    macro_regime: pd.Series | None,
    index: pd.Index,
    columns: pd.Index,
    *,
    long_regimes: frozenset[str] = DEFAULT_LONG_REGIMES,
    short_regimes: frozenset[str] = DEFAULT_SHORT_REGIMES,
    short_macro_regimes: frozenset[str] = frozenset({"contraction"}),
    high_volatility_multiplier: float = 0.5,
    allow_when_unknown: bool = False,
    mode: str = "gate",
    long_tilts: dict[str, float] | None = None,
    short_tilts: dict[str, float] | None = None,
) -> FilterOutcome:
    """Gate direction on the market regime and shrink size when volatile.

    Rules, per the engine's mandate:

    * Longs are permitted only in ``risk_on`` or ``expansion``.
    * Shorts are permitted in ``crisis``, or when the macro regime is
      ``contraction``.
    * ``high`` volatility scales size by ``high_volatility_multiplier``.

    **The macro term in the short rule is MEASURED AND NOT SUPPORTED, and it
    does not run in the shipped configuration.** Both halves matter to
    anyone reading this line.

    It does not run because the shipped ``regime_mode`` is ``tilt``, where
    ``allow_short`` is replaced below by ``short_scale > 0.0`` off the
    combined regime and the macro term is discarded. Macro still reaches the
    decision as an INPUT to ``classify_combined``; it simply does not veto
    here.

    It is not supported because the cut it rests on points the wrong way.
    Measured 2026-09-12 over 5,590 completed trips, grouping the engine's
    own round trips by the macro label standing at entry:

        shorts entered under contraction     n=1450   mean  -23   win 29.7%
        shorts entered under anything else   n=1074   mean  -15   win 34.1%
                                             difference -8, t = -1.39

    The trades this term PERMITS are worse than the ones it would refuse.
    And the mirror says the label is not directional at all: longs under
    ``contraction`` average -17 against -15 elsewhere, so both sides are
    marginally worse under the same label rather than one side being better.
    A cut that moves longs and shorts the same way is marking a period, not
    a direction.

    ``contraction`` also covers 3,836 of 6,713 bars -- 57% of the span -- so
    it is not a selective gate even when it is switched on.

    Two things that measurement cannot settle, stated so the figures are not
    over-read. The harness this ran on does not reproduce the recorded
    battery (-73.6% where +191.0% was recorded), so every band loses money
    and only the RELATIVE ordering within the run is worth anything. And
    ``t = -1.39`` on overlapping trips is nothing in either direction: this
    is an absence of support, not evidence of harm.

    So the term is left exactly as it is -- unused in production and
    unchanged -- rather than deleted on a null. Switching ``regime_mode``
    back to ``gate`` would bring it live, and nothing here justifies that.

    Parameters
    ----------
    allow_when_unknown:
        Whether an ``unknown`` regime permits trading. ``False`` (default)
        means the warm-up period trades nothing, which is the conservative
        reading: no evidence is not evidence of safety.

    Returns
    -------
    FilterOutcome
    """
    if not 0.0 <= high_volatility_multiplier <= 1.0:
        raise ValueError(
            f"high_volatility_multiplier must be in [0, 1], got "
            f"{high_volatility_multiplier}"
        )

    combined = broadcast(combined_regime, index, columns)
    volatility = broadcast(volatility_regime, index, columns)
    macro = broadcast(macro_regime, index, columns)

    allow_long = combined.isin(long_regimes)
    allow_short = combined.isin(short_regimes) | macro.isin(short_macro_regimes)


    if allow_when_unknown:
        unknown = combined.eq("unknown")
        allow_long = allow_long | unknown
        allow_short = allow_short | unknown

    high_volatility = volatility.eq("high")
    multiplier = pd.DataFrame(1.0, index=index, columns=columns)
    multiplier = multiplier.mask(high_volatility, high_volatility_multiplier)

    long_multiplier = multiplier
    short_multiplier = multiplier

    if mode == "tilt":
        # Permission becomes a matter of degree: everything above zero is
        # allowed, at a size proportional to how much the regime supports
        # it. A zero tilt still forbids outright, which is what keeps new
        # longs out of a crisis.
        long_scale = combined.replace(
            long_tilts or LONG_REGIME_TILTS
        ).apply(pd.to_numeric, errors="coerce").fillna(0.0)
        short_scale = combined.replace(
            short_tilts or SHORT_REGIME_TILTS
        ).apply(pd.to_numeric, errors="coerce").fillna(0.0)

        allow_long = long_scale > 0.0
        allow_short = short_scale > 0.0
        long_multiplier = multiplier * long_scale
        short_multiplier = multiplier * short_scale
    elif mode != "gate":
        raise ValueError(f"unknown regime filter mode {mode!r}")

    return FilterOutcome(
        allow_long=allow_long,
        allow_short=allow_short,
        long_multiplier=long_multiplier,
        short_multiplier=short_multiplier,
        flags={
            "regime_blocks_long": ~allow_long,
            "regime_blocks_short": ~allow_short,
            "high_volatility": high_volatility,
        },
    )


def volume_filter(
    volume: pd.DataFrame | None,
    index: pd.Index,
    columns: pd.Index,
    *,
    window: int = 20,
    heavy_ratio: float = 1.5,
    heavy_multiplier: float = 0.6,
    quiet_ratio: float = 0.8,
) -> FilterOutcome:
    """Shrink a position when trading is unusually heavy. Pure.

    Volume is the conviction behind a price -- how much money was willing to
    take the other side -- and it is the best predictor of the next day's
    range this project has measured, by a distance:

        shares    volume ratio -> next day's range   r=+0.177  t=+39.03
        indices   volume ratio -> next day's range   r=+0.168  t=+24.06

    Monotone across every decile: the quietest fifth of share sessions is
    followed by a 2.28% range and the heaviest by 3.41%. For comparison the
    filing-rate signal, the next best thing here, measured t=+5.13.

    What it does **not** do is say which way. Volume against the next
    return is r=-0.013 -- detectable on 47,000 observations and worth
    nothing. And a move up on heavy volume is followed by *worse* returns
    than the same move on light volume (-0.133%, t=-2.58), so the intuition
    that conviction persists is not merely unsupported, it is backwards.

    So this shrinks and never grows. Heavy volume means a wider range is
    coming, which is a reason to hold less of something, not a reason to
    hold more of it or to pick a side.

    Measured against each instrument's own recent median, so a company
    whose turnover has doubled over a decade compares with itself rather
    than with Apple.

    Unlike the other filters here it applies equally to both directions:
    a wider range is a wider range whichever way the position points.
    """
    if not 0.0 <= heavy_multiplier <= 1.0:
        raise ValueError(
            f"heavy_multiplier must be in [0, 1], got {heavy_multiplier}"
        )

    ones = pd.DataFrame(1.0, index=index, columns=columns)
    if volume is None or getattr(volume, "empty", True):
        # No volume is not quiet trading. An index CFD whose vendor reports
        # nothing must not be sized as though it were calm.
        return permissive_outcome(index, columns)

    aligned = volume.reindex(index=index, columns=columns)
    typical = aligned.rolling(window, min_periods=max(window // 2, 2)).median()
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = aligned / typical.replace(0.0, np.nan)

    heavy = ratio > heavy_ratio
    multiplier = ones.mask(heavy, heavy_multiplier)
    # Anything unmeasurable stays at full size rather than being penalised
    # for a gap in the data.
    multiplier = multiplier.where(ratio.notna(), 1.0)

    return FilterOutcome(
        allow_long=pd.DataFrame(True, index=index, columns=columns),
        allow_short=pd.DataFrame(True, index=index, columns=columns),
        long_multiplier=multiplier,
        short_multiplier=multiplier,
        flags={"heavy_volume": heavy.fillna(False)},
    )


def correlation_filter(
    correlation_regime: pd.Series | None,
    index: pd.Index,
    columns: pd.Index,
    *,
    breakdown_multiplier: float = 0.0,
    applies_to_shorts: bool = False,
    diversification: pd.DataFrame | None = None,
) -> FilterOutcome:
    """Shrink or suppress signals when diversification has failed.

    A correlation breakdown means position count has stopped buying
    diversification: ten "independent" signals are one bet. The default
    multiplier of ``0.0`` suppresses entirely; raise it to merely shrink.

    Parameters
    ----------
    applies_to_shorts:
        Whether the breakdown suppression also applies to shorts.
        ``False`` by default, and the default matters: the regime layer
        defines ``crisis`` as *requiring* a correlation breakdown, and
        crisis is the regime that permits shorts. Suppressing both
        directions would make shorts unreachable in every regime that
        allows them. Set ``True`` to suppress regardless, accepting a
        long-only engine.
    diversification:
        Optional per-asset multiplier from
        :func:`diversification_multiplier`, folded in here so all
        correlation-driven sizing lives in one place.

    Returns
    -------
    FilterOutcome
        Permissions are untouched — this filter only ever scales size, so a
        breakdown reduces exposure rather than inverting a view.
    """
    if not 0.0 <= breakdown_multiplier <= 1.0:
        raise ValueError(
            f"breakdown_multiplier must be in [0, 1], got {breakdown_multiplier}"
        )

    regime = broadcast(correlation_regime, index, columns)
    breakdown = regime.eq("breakdown")

    long_multiplier = pd.DataFrame(1.0, index=index, columns=columns)
    long_multiplier = long_multiplier.mask(breakdown, breakdown_multiplier)

    short_multiplier = pd.DataFrame(1.0, index=index, columns=columns)
    if applies_to_shorts:
        short_multiplier = short_multiplier.mask(
            breakdown, breakdown_multiplier
        )

    if diversification is not None and not diversification.empty:
        # Crowding is direction-agnostic: ten correlated shorts are as
        # concentrated as ten correlated longs.
        spread = (
            diversification.reindex(index=index, columns=columns)
            .fillna(1.0)
            .clip(0.0, 1.0)
        )
        long_multiplier = long_multiplier * spread
        short_multiplier = short_multiplier * spread

    return FilterOutcome(
        allow_long=pd.DataFrame(True, index=index, columns=columns),
        allow_short=pd.DataFrame(True, index=index, columns=columns),
        long_multiplier=long_multiplier,
        short_multiplier=short_multiplier,
        flags={"correlation_breakdown": breakdown},
    )


def macro_filter(
    growth_regime: pd.Series | None,
    inflation_regime: pd.Series | None,
    labor_regime: pd.Series | None,
    index: pd.Index,
    columns: pd.Index,
    *,
    asset_classes: dict[str, str] | None = None,
    inflation_hedge_classes: frozenset[str] = frozenset({"commodity", "fx"}),
    inflation_penalty: float = 0.5,
    loosening_labor_multiplier: float = 0.75,
    block_equity_longs_in_contraction: bool = True,
) -> FilterOutcome:
    """Tilt by asset class on the macro backdrop.

    Three rules:

    * **Rising inflation** favours real assets. Rather than boosting
      commodities and FX — which would lever the book up — everything
      *outside* ``inflation_hedge_classes`` is scaled by
      ``inflation_penalty``. The relative tilt is the same; the gross
      exposure is not.
    * **GDP contraction** blocks equity longs outright.
    * **Loosening labour** (rising unemployment) applies a broad risk-off
      haircut.

    Returns
    -------
    FilterOutcome
    """
    for name, value in (
        ("inflation_penalty", inflation_penalty),
        ("loosening_labor_multiplier", loosening_labor_multiplier),
    ):
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"{name} must be in [0, 1], got {value}")

    classes = classify_assets(columns, asset_classes)
    class_row = pd.DataFrame(
        np.repeat(classes.to_numpy()[None, :], len(index), axis=0),
        index=index,
        columns=columns,
        dtype="object",
    )

    growth = broadcast(growth_regime, index, columns)
    inflation = broadcast(inflation_regime, index, columns)
    labor = broadcast(labor_regime, index, columns)

    multiplier = pd.DataFrame(1.0, index=index, columns=columns)

    rising_inflation = inflation.eq("rising")
    not_a_hedge = ~class_row.isin(inflation_hedge_classes)
    inflation_penalised = rising_inflation & not_a_hedge
    multiplier = multiplier.mask(inflation_penalised, multiplier * inflation_penalty)

    loosening = labor.eq("loosening")
    multiplier = multiplier.mask(loosening, multiplier * loosening_labor_multiplier)

    contraction = growth.eq("contraction")
    is_equity = class_row.eq("equity")
    blocked_equity_long = (
        contraction & is_equity
        if block_equity_longs_in_contraction
        else pd.DataFrame(False, index=index, columns=columns)
    )

    return FilterOutcome(
        allow_long=~blocked_equity_long,
        allow_short=pd.DataFrame(True, index=index, columns=columns),
        long_multiplier=multiplier,
        short_multiplier=multiplier,
        flags={
            "inflation_tilt": inflation_penalised,
            "labor_risk_off": loosening,
            "contraction_blocks_equity_long": blocked_equity_long,
        },
    )


def diversification_multiplier(
    rolling_correlation: pd.DataFrame | None,
    index: pd.Index,
    columns: pd.Index,
    *,
    floor: float = 0.4,
) -> pd.DataFrame:
    """Shrink assets that are crowded into a correlated cluster.

    For each asset, the mean absolute correlation to every *other* asset is a
    proxy for how much independent information the position adds. A name
    moving with everything else is scaled toward ``floor``; a genuinely
    independent one keeps full size.

    Pure and vectorised via a single reshape into a ``(time, asset, asset)``
    tensor, rather than iterating matrices.

    Returns
    -------
    pandas.DataFrame
        Multipliers in ``[floor, 1]``; ``1.0`` wherever correlation is
        unknown, so a warm-up period is not penalised.
    """
    if not 0.0 <= floor <= 1.0:
        raise ValueError(f"floor must be in [0, 1], got {floor}")
    if rolling_correlation is None or rolling_correlation.empty:
        return pd.DataFrame(1.0, index=index, columns=columns)

    assets = list(rolling_correlation.columns)
    timestamps = pd.DatetimeIndex(
        rolling_correlation.index.get_level_values(0).unique()
    )
    tensor = rolling_correlation.to_numpy(dtype="float64").reshape(
        len(timestamps), len(assets), len(assets)
    )

    off_diagonal = ~np.eye(len(assets), dtype=bool)
    magnitudes = np.abs(np.where(off_diagonal[None, :, :], tensor, np.nan))

    valid = np.isfinite(magnitudes)
    counts = valid.sum(axis=2)
    totals = np.where(valid, magnitudes, 0.0).sum(axis=2)
    mean_correlation = np.divide(
        totals, counts, out=np.full(counts.shape, np.nan), where=counts > 0
    )

    # Fully independent -> 1.0; moving with everything -> floor.
    scaled = 1.0 - (1.0 - floor) * np.clip(mean_correlation, 0.0, 1.0)
    frame = pd.DataFrame(scaled, index=timestamps, columns=assets)
    return frame.reindex(index=index, columns=columns).fillna(1.0)


def combine_outcomes(*outcomes: FilterOutcome) -> FilterOutcome:
    """Compose filters: permissions AND, multipliers multiply, flags merge.

    Order-independent by construction — ``and`` and ``*`` are both
    commutative — so the result does not depend on which filter ran first.
    That is what makes the combined signal reproducible regardless of how the
    filter list is assembled.

    Raises
    ------
    ValueError
        If called with no outcomes, since there would be no shape to return.
    """
    if not outcomes:
        raise ValueError("combine_outcomes requires at least one outcome")

    allow_long = outcomes[0].allow_long
    allow_short = outcomes[0].allow_short
    long_multiplier = outcomes[0].long_multiplier
    short_multiplier = outcomes[0].short_multiplier
    flags: dict[str, pd.DataFrame] = dict(outcomes[0].flags)

    for outcome in outcomes[1:]:
        allow_long = allow_long & outcome.allow_long
        allow_short = allow_short & outcome.allow_short
        long_multiplier = long_multiplier * outcome.long_multiplier
        short_multiplier = short_multiplier * outcome.short_multiplier
        flags.update(outcome.flags)

    return FilterOutcome(
        allow_long=allow_long.fillna(False).astype(bool),
        allow_short=allow_short.fillna(False).astype(bool),
        long_multiplier=long_multiplier.fillna(0.0).clip(0.0, 1.0),
        short_multiplier=short_multiplier.fillna(0.0).clip(0.0, 1.0),
        flags=flags,
    )
