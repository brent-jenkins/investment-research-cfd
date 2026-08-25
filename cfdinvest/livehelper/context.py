"""What the refinements read: performance, regime, margin, bias, plan.

The refinement layer adjusts a trade for things the per-trade checks cannot
see — that you are three weeks into a drawdown, that correlations have broken
down, that your last six exits were early. None of that is in the trade you
typed, and all of it is somewhere on disk.

This module gathers it. Every refinement takes a :class:`TradingContext` and
nothing else, so there is one place that knows how to read the artefacts and
one shape for the tests to construct.

Two of the five inputs do not exist yet
---------------------------------------
:class:`BiasProfile` and :class:`MetaStrategyPlan` are contracts for layers
that are queued and unbuilt. They are defined here so the refinements can be
written against a real shape rather than a guess, and so the layers that fill
them have something to satisfy.

Both are optional and **absent by default**. A refinement with no bias profile
does not assume you are unbiased — it says the bias input was not available,
in ``missing``, and makes no adjustment for it. Assuming absence means
"nothing wrong" is how a system starts flattering the person using it.

Nothing here recomputes anything. Expectancy comes from the analytics layer,
regimes from :mod:`cfdinvest.risk`, margin from the assessment the safety
engine already produced. Every figure is read.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Mapping

logger = logging.getLogger(__name__)

__all__ = [
    "PerformanceTrends",
    "RegimeProfile",
    "BiasProfile",
    "MetaStrategyPlan",
    "MarginState",
    "TradingContext",
    "load_context",
]


def _finite(value: Any) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _fraction(value: Any) -> float | None:
    return float(value) if _finite(value) else None


@dataclass(frozen=True, slots=True)
class PerformanceTrends:
    """How the strategy has actually been doing, from the analytics layer.

    Attributes
    ----------
    expectancy, recent_expectancy:
        Average outcome per trade, over everything and over the recent
        window. The *pair* is the signal: an edge that has gone is not the
        same as one that was never there.
    current_drawdown, max_drawdown:
        Negative fractions. Current is what matters for sizing; max is
        context for whether the current one is unusual.
    win_rate, trades:
        ``trades`` gates everything — an expectancy from four trades is not
        an expectancy, and is the number most likely to be acted on anyway.
    """

    expectancy: float | None = None
    recent_expectancy: float | None = None
    win_rate: float | None = None
    current_drawdown: float | None = None
    max_drawdown: float | None = None
    trades: int = 0
    recent_trades: int = 0
    available: bool = False

    @property
    def edge_is_measurable(self) -> bool:
        """Enough trades for expectancy to mean anything.

        Thirty is not a law; it is the point below which the number moves
        more than the thing it measures. Below it the refinements leave size
        alone rather than acting on noise.
        """
        return self.available and self.trades >= 30 and _finite(self.expectancy)

    @property
    def deteriorating(self) -> bool:
        """Recent results materially worse than a long run that was positive.

        The positivity check is load-bearing. With a negative expectancy,
        ``expectancy * 0.5`` is *greater* than ``expectancy`` -- so almost any
        recent figure satisfies "below half the long run", and a strategy that
        has simply been losing reads as one whose edge is fading. Those want
        different responses, and the second is the gentler of the two.
        """
        if not (self.edge_is_measurable and _finite(self.recent_expectancy)):
            return False
        if self.recent_trades < 10:
            return False
        if float(self.expectancy) <= 0:
            return False
        return float(self.recent_expectancy) < float(self.expectancy) * 0.5


@dataclass(frozen=True, slots=True)
class RegimeProfile:
    """The market's state, as the regime classifier last saw it.

    A thin wrapper over what :func:`cfdinvest.risk.compute_regime_risk`
    returns, so there is no second opinion about what counts as elevated.
    """

    regimes: dict[str, str] = field(default_factory=dict)
    elevated: tuple[str, ...] = ()
    as_of: Any = None
    available: bool = False

    def label(self, classifier: str) -> str | None:
        return self.regimes.get(classifier)

    @property
    def volatility(self) -> str | None:
        return self.regimes.get("volatility_regime")

    @property
    def trend(self) -> str | None:
        return self.regimes.get("trend_regime")

    @property
    def correlation(self) -> str | None:
        return self.regimes.get("correlation_regime")

    @property
    def combined(self) -> str | None:
        return self.regimes.get("combined_regime")

    @property
    def high_volatility(self) -> bool:
        return str(self.volatility or "").lower() in {"high", "crisis"}

    @property
    def correlations_broken(self) -> bool:
        """Diversification is not doing what the position sizes assume."""
        return str(self.correlation or "").lower() in {"breakdown", "high"}


@dataclass(frozen=True, slots=True)
class BiasProfile:
    """What the behavioural layer found, if it has run.

    Filled by :mod:`cfdinvest.bias`. Everything is optional and ``available``
    is ``False`` until something fills it in -- which is the state on a fresh
    install and the state whenever the record is too short to say anything.

    Severities are fractions in ``[0, 1]``: ``0`` means no evidence, ``1``
    means the pattern is unmistakable. A ``None`` means *not measured*, which
    is not the same as zero — and the refinements treat it as not measured.
    """

    overconfidence: float | None = None
    revenge_trading: float | None = None
    fear_driven_underexposure: float | None = None
    risk_avoidance: float | None = None
    loss_aversion: float | None = None
    premature_exits: float | None = None
    late_exits: float | None = None
    leverage_creep: float | None = None
    margin_blindness: float | None = None
    as_of: Any = None
    available: bool = False

    def severity(self, name: str) -> float | None:
        """One bias's severity, or ``None`` when it was not measured."""
        if not self.available:
            return None
        return _fraction(getattr(self, name, None))

    @property
    def measured(self) -> tuple[str, ...]:
        if not self.available:
            return ()
        return tuple(
            name
            for name in (
                "overconfidence", "revenge_trading", "fear_driven_underexposure",
                "risk_avoidance", "loss_aversion", "premature_exits",
                "late_exits", "leverage_creep", "margin_blindness",
            )
            if _finite(getattr(self, name))
        )


@dataclass(frozen=True, slots=True)
class MetaStrategyPlan:
    """**A contract for a layer that does not exist yet.**

    What a meta-strategy optimiser would say about how hard to push right
    now, and which instruments it wants exposure in.

    ``target_scale`` is a multiplier on normal size. It is deliberately a
    *request*: the refinements treat anything above 1.0 as a proposal that
    still has to clear the existing safety engine, because a meta-layer that
    could raise size past the limits would make the limits advisory.
    """

    target_scale: float | None = None
    preferred_instruments: tuple[str, ...] = ()
    avoided_instruments: tuple[str, ...] = ()
    rationale: str | None = None
    as_of: Any = None
    available: bool = False

    def stance_on(self, instrument: str) -> str | None:
        """``"preferred"``, ``"avoided"`` or ``None``."""
        if not self.available:
            return None
        name = str(instrument).upper()
        if name in {i.upper() for i in self.preferred_instruments}:
            return "preferred"
        if name in {i.upper() for i in self.avoided_instruments}:
            return "avoided"
        return None


@dataclass(frozen=True, slots=True)
class MarginState:
    """Where the account's margin stands, and where it has been.

    ``history`` is the margin usage from recent risk snapshots, oldest first,
    which is what makes "rising" a measurement rather than a feeling.
    """

    usage_pct: float | None = None
    required_gbp: float | None = None
    free_gbp: float | None = None
    effective_leverage: float | None = None
    history: tuple[float, ...] = ()
    leverage_history: tuple[float, ...] = ()
    available: bool = False

    @property
    def trend(self) -> float | None:
        """Change in usage across the recorded history, as a fraction.

        ``None`` with fewer than three readings: two points are a line
        through anything, and a monitor that called every second reading a
        trend would cry wolf on the first quiet day.
        """
        if len(self.history) < 3:
            return None
        return float(self.history[-1]) - float(self.history[0])


@dataclass(frozen=True, slots=True)
class TradingContext:
    """Everything the refinements read, and what could not be read.

    ``missing`` is the point of the object. A refinement that adjusted for
    four inputs out of six should say so, because the adjustment it made is
    smaller than the one it would have made knowing everything.
    """

    performance: PerformanceTrends = field(default_factory=PerformanceTrends)
    regime: RegimeProfile = field(default_factory=RegimeProfile)
    bias: BiasProfile = field(default_factory=BiasProfile)
    plan: MetaStrategyPlan = field(default_factory=MetaStrategyPlan)
    margin: MarginState = field(default_factory=MarginState)
    run_id: str | None = None

    @property
    def missing(self) -> tuple[str, ...]:
        """Which inputs were not available, in plain words."""
        absent = []
        if not self.performance.available:
            absent.append("past performance")
        elif not self.performance.edge_is_measurable:
            absent.append(
                f"a measurable edge (only {self.performance.trades} trades)"
            )
        if not self.regime.available:
            absent.append("the market regime")
        if not self.bias.available:
            absent.append("a behavioural profile")
        if not self.plan.available:
            absent.append("a meta-strategy plan")
        if not self.margin.available:
            absent.append("the margin history")
        return tuple(absent)

    @property
    def complete(self) -> bool:
        return not self.missing


def load_context(
    config=None,
    *,
    account: str | None = None,
    run_id: str | None = None,
    bias: BiasProfile | None = None,
    plan: MetaStrategyPlan | None = None,
    assessment=None,
) -> TradingContext:
    """Read the context from persisted artefacts. Reads only, never raises.

    ``bias`` and ``plan`` are passed in because nothing writes them yet. When
    those layers land they will grow loaders here, and every refinement will
    start using them without changing.
    """
    return TradingContext(
        performance=_performance(config, account, run_id),
        regime=_regime(config, account, run_id),
        bias=bias or BiasProfile(),
        plan=plan or MetaStrategyPlan(),
        margin=_margin(config, account, assessment),
        run_id=run_id,
    )


def _performance(config, account, run_id) -> PerformanceTrends:
    """Expectancy and drawdown, from the analytics layer's own scoring."""
    try:
        from cfdinvest.analytics import score_run

        result = score_run(config, run_id, account=account)
    except Exception as error:  # noqa: BLE001
        logger.debug("No performance history: %s", error)
        return PerformanceTrends()

    if getattr(result, "error", None):
        return PerformanceTrends()

    metrics = getattr(result, "metrics", None) or {}
    expectancy = metrics.get("expectancy") or {}
    drawdown = metrics.get("drawdown") or {}
    trades = getattr(result, "trades", None)

    count = 0
    try:
        count = int(expectancy.get("trades") or (0 if trades is None else len(trades)))
    except (TypeError, ValueError):
        count = 0

    return PerformanceTrends(
        expectancy=_fraction(expectancy.get("expectancy")),
        recent_expectancy=_fraction(expectancy.get("recent_expectancy")),
        win_rate=_fraction(metrics.get("win_rate")),
        current_drawdown=_fraction(drawdown.get("current_drawdown_fraction")),
        max_drawdown=_fraction(drawdown.get("max_drawdown_fraction")),
        trades=count,
        recent_trades=count,
        available=bool(metrics),
    )


def _regime(config, account, run_id) -> RegimeProfile:
    """The classifications, through the risk layer's own reader.

    Not re-derived: :func:`cfdinvest.risk.compute_regime_risk` already decides
    what counts as elevated, and a second opinion here would be a second
    threshold to keep in step.
    """
    try:
        from cfdinvest.persist import readers
        from cfdinvest.risk import compute_regime_risk

        frame = readers.load_regime_frame(run_id, config, account)
        result = compute_regime_risk(frame)
    except Exception as error:  # noqa: BLE001
        logger.debug("No regime history: %s", error)
        return RegimeProfile()

    return RegimeProfile(
        regimes=dict(result.get("regimes") or {}),
        elevated=tuple(result.get("elevated") or ()),
        as_of=result.get("as_of"),
        available=bool(result.get("available")),
    )


def _margin(config, account, assessment) -> MarginState:
    """Current margin from the assessment, the history from risk snapshots."""
    usage = getattr(assessment, "margin_usage_pct_after_trade", None)
    required = getattr(assessment, "margin_required_gbp", None)
    leverage = getattr(assessment, "leverage", None)

    history: tuple[float, ...] = ()
    leverage_history: tuple[float, ...] = ()
    try:
        from cfdinvest.persist import risk as risk_store

        frame = risk_store.load_risk_history(config, account)
        if not frame.empty:
            if "margin_usage_pct" in frame.columns:
                history = tuple(
                    float(v)
                    for v in frame["margin_usage_pct"].dropna().tolist()
                )
            if "effective_leverage" in frame.columns:
                leverage_history = tuple(
                    float(v)
                    for v in frame["effective_leverage"].dropna().tolist()
                )
    except Exception as error:  # noqa: BLE001
        logger.debug("No margin history: %s", error)

    return MarginState(
        usage_pct=_fraction(usage),
        required_gbp=_fraction(required),
        effective_leverage=_fraction(leverage),
        history=history,
        leverage_history=leverage_history,
        available=_finite(usage) or bool(history),
    )
