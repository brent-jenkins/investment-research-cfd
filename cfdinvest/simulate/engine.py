"""Rehearsing the manual-trading loop, not simulating prices again.

What this is not
----------------
**It is not a second backtester.** :mod:`cfdinvest.execution` already fills
orders, charges spread and slippage, converts currencies, applies financing
and resolves stops intrabar. Writing any of that again here would give the
project two fill models and two answers, and the day they disagreed neither
would be believable.

So this reuses ``run_execution`` unchanged. What it adds is the layer above:
**the decisions**.

What it actually simulates
--------------------------
The pipeline produces a signal and a size. In reality you then run the helper
over it, and the helper often says something — trade smaller, the regime is
elevated, the margin is tight, this is below the instrument's minimum. Some of
those you would act on.

This replays the record with those decisions applied, and answers one question
the backtest cannot: **what would have happened if you had followed the
advice**, including every time it said to trade smaller.

The comparison is the output. A simulated equity curve on its own is another
backtest; the same run with and without the helper's adjustments is a
measurement of the advice.

What it refuses to simulate
---------------------------
**How your psychology would have evolved.** The spec asks for "bias
evolution", and a trade record cannot support a model of a person changing
over time — anything produced would be a plausible-looking invention, which is
the one thing this project is built to avoid.

What is done instead is real and narrower: the **existing** bias detectors are
run over the simulated record, which answers "would this strategy, executed
this way, have triggered them?". That is a fact about the strategy rather than
a forecast about you, and it is said in those words wherever it is reported.

Every artefact is marked
------------------------
A simulated equity curve filed where a real one goes is the worst outcome
available here, so every artefact carries ``simulated: true`` and lives under
its own account tree. See :mod:`cfdinvest.persist.simulation`.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field, replace
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

__all__ = [
    "SimulationConfig",
    "SimulationResult",
    "simulate_advice",
    "apply_advice_to_signals",
    "detect_forced_closures",
]


def _finite(value: Any) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


@dataclass(frozen=True, slots=True)
class SimulationConfig:
    """How faithfully the rehearsal follows the advice.

    Attributes
    ----------
    follow_sizing:
        Apply the safe size the helper would have recommended. The main thing
        being tested.
    follow_refinement:
        Also apply the refinement multipliers — drawdown, regime, behaviour.
        Separate from ``follow_sizing`` because they answer different
        questions, and running them together makes it impossible to say which
        did the work.
    skip_unsafe:
        Do not take a trade the helper would have called UNSAFE. Off by
        default: it is the most aggressive intervention and the one most
        likely to flatter the advice, since skipping trades in a losing period
        improves any curve.
    margin_call_ratio:
        Usage at which the broker force-closes. Trading 212's own level, not
        your limit -- this models the platform, not the policy.
    """

    follow_sizing: bool = True
    follow_refinement: bool = True
    skip_unsafe: bool = False
    margin_call_ratio: float = 0.90
    label: str = "as advised"

    def __post_init__(self) -> None:
        if not 0 < self.margin_call_ratio <= 1:
            raise ValueError(
                "margin_call_ratio is a fraction in (0, 1], got "
                f"{self.margin_call_ratio}"
            )


@dataclass(frozen=True, slots=True)
class SimulationResult:
    """The rehearsal, the original, and the difference between them."""

    label: str
    baseline_metrics: dict[str, Any] = field(default_factory=dict)
    recorded_final_equity: float | None = None
    reconstruction_gap: float | None = None
    advised_metrics: dict[str, Any] = field(default_factory=dict)
    baseline_equity: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    advised_equity: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    adjustments: pd.DataFrame = field(default_factory=pd.DataFrame)
    forced_closures: pd.DataFrame = field(default_factory=pd.DataFrame)
    trades: pd.DataFrame = field(default_factory=pd.DataFrame)
    notes: tuple[str, ...] = ()
    error: str | None = None
    simulated: bool = True

    @property
    def ok(self) -> bool:
        return self.error is None and not self.advised_equity.empty

    def difference(self, metric: str) -> float | None:
        """Advised minus baseline, for one metric. The actual output."""
        advised = self.advised_metrics.get(metric)
        baseline = self.baseline_metrics.get(metric)
        if not (_finite(advised) and _finite(baseline)):
            return None
        return float(advised) - float(baseline)

    @property
    def headline(self) -> str:
        """What the rehearsal is for, in a sentence."""
        if self.error:
            return f"The rehearsal could not be run: {self.error}"
        if self.advised_equity.empty:
            return "Nothing was simulated."

        change = self.difference("final_equity")
        if change is None:
            return f"Rehearsed {len(self.trades)} trade(s) following the advice."

        direction = "more" if change > 0 else "less"
        return (
            f"Following the advice would have ended with £{abs(change):,.2f} "
            f"{direction} than the run as executed."
        )


def apply_advice_to_signals(
    signal_frame: pd.DataFrame,
    analysis_frame=None,
    *,
    config: SimulationConfig | None = None,
    helper_config=None,
    context=None,
    execution_config=None,
) -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    """Rewrite the signal sizes as the helper would have advised.

    Returns the adjusted signals, a row per adjustment, and any notes.

    Pure with respect to its inputs. It reads the signal frame and applies the
    **existing** sizing and refinement functions bar by bar; it does not price
    anything, and the adjusted frame goes to ``run_execution`` unchanged in
    every other respect.
    """
    from cfdinvest.livehelper import (
        LiveHelperConfig,
        MarketState,
        compute_safe_sizing,
        evaluate_trade_safety,
    )
    from cfdinvest.livehelper.context import TradingContext
    from cfdinvest.livehelper.intent import TradeIntent
    from cfdinvest.livehelper.refine_sizing import refine_sizing

    settings = config or SimulationConfig()
    limits = helper_config or LiveHelperConfig()
    where = context or TradingContext()
    notes: list[str] = []

    if signal_frame is None:
        return signal_frame, pd.DataFrame(), ["No signals to rehearse."]

    from dataclasses import replace as _replace

    sizes = getattr(signal_frame, "size", None)
    direction_panel = getattr(signal_frame, "direction", None)
    if sizes is None or getattr(sizes, "empty", True):
        return signal_frame, pd.DataFrame(), [
            "The signal frame carries no sizes, so nothing could be adjusted. "
            "The rehearsal is identical to the run."
        ]

    # The signal "size" is a share of equity, not a number of units -- the
    # engine turns it into notional with exposure_per_unit_size and then into
    # units with the price. The helper's limits are all in units and money, so
    # the two have to be reconciled before asking it anything. Skipping this
    # was the difference between a rehearsal that adjusts almost nothing and
    # one that means something.
    equity = float(getattr(execution_config, "initial_equity", 0.0) or 0.0)
    per_unit = float(getattr(execution_config, "exposure_per_unit_size", 1.0) or 1.0)
    if equity <= 0:
        notes.append(
            "No account equity was recorded for the run, so signal sizes could "
            "not be turned into units. Nothing was adjusted."
        )
        return signal_frame, pd.DataFrame(), notes

    prices = _close_prices(analysis_frame)
    if prices.empty:
        notes.append(
            "No stored prices, so sizes were adjusted on the signal alone — "
            "every limit denominated in money was left unchecked."
        )

    rows: list[dict[str, Any]] = []
    adjusted_sizes = sizes.copy()

    for moment, row in sizes.iterrows():
        for ticker, size in row.items():
            if not _finite(size) or float(size) == 0:
                continue

            price = _price_at(prices, str(ticker), moment)
            direction = "long"
            if direction_panel is not None and ticker in direction_panel.columns:
                label = direction_panel.at[moment, ticker]
                direction = "short" if str(label).lower().startswith("s") else "long"

            if not _finite(price) or float(price) <= 0:
                continue

            # size -> notional -> units, the same way the engine does it.
            notional = abs(float(size)) * equity * per_unit
            units = notional / float(price)

            intent = TradeIntent(
                instrument=str(ticker),
                direction=direction,
                quantity=units,
                entry_price=price,
            )

            try:
                assessment = evaluate_trade_safety(
                    intent, limits, MarketState(last_price=price)
                )
                sizing = compute_safe_sizing(
                    intent, limits, MarketState(last_price=price)
                )
            except Exception as error:  # noqa: BLE001 - one cell, not the run
                logger.debug("Could not assess %s at %s: %s", ticker, moment, error)
                continue

            # Adjustments are applied as a *scale*, so the result goes back
            # into the signal frame in the units it came from.
            scale = 1.0
            applied: list[str] = []

            if settings.skip_unsafe and assessment.status == "UNSAFE":
                scale = 0.0
                applied.append("skipped as UNSAFE")
            else:
                if settings.follow_sizing and sizing.reduces and sizing.scale:
                    scale *= float(sizing.scale)
                    applied.append(f"sized to {sizing.scale:.0%}")
                if settings.follow_refinement:
                    refinement = refine_sizing(
                        intent, assessment, sizing, where, limits
                    )
                    if refinement.reduces:
                        scale *= refinement.multiplier
                        applied.append(f"refined to {refinement.multiplier:.0%}")

            new_size = abs(float(size)) * scale

            if applied and abs(scale - 1.0) > 1e-9:
                rows.append(
                    {
                        "timestamp": moment,
                        "ticker": str(ticker),
                        "original_size": abs(float(size)),
                        "advised_size": new_size,
                        "scale": scale,
                        "units": units,
                        "verdict": assessment.status,
                        "why": "; ".join(applied),
                    }
                )
                adjusted_sizes.at[moment, ticker] = new_size

    return (
        _replace(signal_frame, size=adjusted_sizes),
        pd.DataFrame(rows),
        notes,
    )


def _close_prices(analysis_frame) -> pd.DataFrame:
    """The stored close panel, wide. Empty when the run stored none."""
    if analysis_frame is None or getattr(analysis_frame, "empty", True):
        return pd.DataFrame()
    if not {"panel", "series", "value", "timestamp"} <= set(analysis_frame.columns):
        return pd.DataFrame()

    closes = analysis_frame[analysis_frame["panel"] == "prices.close"]
    if closes.empty:
        return pd.DataFrame()
    return closes.pivot_table(
        index="timestamp", columns="series", values="value", aggfunc="last"
    )


def _ticker_of(index, signal) -> str | None:
    if isinstance(index, tuple) and len(index) > 1:
        return str(index[-1])
    for name in ("ticker", "asset", "instrument"):
        if name in signal.index:
            return str(signal[name])
    return None


def _timestamp_of(index):
    return index[0] if isinstance(index, tuple) else index


def _price_at(prices: pd.DataFrame, ticker, index) -> float | None:
    if prices.empty or ticker is None or ticker not in prices.columns:
        return None
    moment = _timestamp_of(index)
    try:
        value = prices[ticker].asof(moment)
    except (KeyError, TypeError, ValueError):
        return None
    return float(value) if _finite(value) else None


def detect_forced_closures(
    execution_frame, *, config: SimulationConfig | None = None,
    account_balance: float | None = None, instrument_leverage: float | None = None,
) -> pd.DataFrame:
    """Where the broker would have closed the book itself.

    The execution engine caps leverage; it does not model a margin call. This
    finds the bars where usage would have crossed the platform's own ratio —
    the point at which Trading 212 closes positions **without warning and
    picks which**.

    Reported rather than applied. Rewinding the simulation to close a position
    mid-path would change every subsequent bar, and a rehearsal that quietly
    diverged from the run it is being compared against measures nothing. What
    this gives you is the list of moments the comparison stopped being valid.
    """
    settings = config or SimulationConfig()

    if execution_frame is None:
        return pd.DataFrame(columns=["timestamp", "usage", "equity"])

    equity = getattr(execution_frame, "equity_curve", None)
    positions = getattr(execution_frame, "positions", None)
    if equity is None or len(equity) == 0 or positions is None or positions.empty:
        return pd.DataFrame(columns=["timestamp", "usage", "equity"])

    if not _finite(instrument_leverage) or float(instrument_leverage) <= 0:
        # Without the instrument's leverage there is no margin figure, and an
        # assumed one would produce a list of imaginary margin calls.
        return pd.DataFrame(columns=["timestamp", "usage", "equity"])

    exposure = positions.abs().sum(axis=1)
    aligned = equity.reindex(exposure.index).ffill()
    base = float(account_balance) if _finite(account_balance) else None

    denominator = aligned.where(aligned > 0) if base is None else base
    usage = (exposure / float(instrument_leverage)) / denominator

    breached = usage[usage >= settings.margin_call_ratio].dropna()
    if breached.empty:
        return pd.DataFrame(columns=["timestamp", "usage", "equity"])

    return pd.DataFrame(
        {
            "timestamp": breached.index,
            "usage": breached.values,
            "equity": aligned.reindex(breached.index).values,
        }
    )


def simulate_advice(
    signal_frame,
    analysis_frame,
    execution_config,
    *,
    prices=None,
    recorded_final_equity: float | None = None,
    config: SimulationConfig | None = None,
    helper_config=None,
    context=None,
) -> SimulationResult:
    """Run the record twice — as executed, and as advised — and compare.

    The comparison is the point. One simulated equity curve is another
    backtest; two, differing only in whether the advice was followed, measure
    the advice.
    """
    from cfdinvest.analytics.metrics import compute_all_metrics
    from cfdinvest.execution.frame import run_backtest

    settings = config or SimulationConfig()
    notes: list[str] = []

    sizes = getattr(signal_frame, "size", None)
    if signal_frame is None or sizes is None or getattr(sizes, "empty", True):
        return SimulationResult(
            label=settings.label, error="there are no signals to rehearse"
        )

    try:
        baseline = run_backtest(signal_frame, analysis_frame, execution_config)
    except Exception as error:  # noqa: BLE001
        return SimulationResult(
            label=settings.label, error=f"the run as executed failed: {error}"
        )

    # The sizing decisions need prices in the long form the analysis artefact
    # is stored in; the engine needs the rebuilt panels. Both come from the
    # same run, and passing them separately is cheaper than making one shape
    # do two jobs badly.
    advised_signals, adjustments, advice_notes = apply_advice_to_signals(
        signal_frame,
        prices if prices is not None else analysis_frame,
        config=settings,
        helper_config=helper_config,
        context=context,
        execution_config=execution_config,
    )
    notes.extend(advice_notes)

    if adjustments.empty:
        notes.append(
            "The helper would have changed nothing. The two curves are "
            "identical, which is itself an answer."
        )

    try:
        advised = run_backtest(advised_signals, analysis_frame, execution_config)
    except Exception as error:  # noqa: BLE001
        return SimulationResult(
            label=settings.label,
            error=f"the rehearsal failed: {error}",
            baseline_metrics=_metrics(baseline, compute_all_metrics),
        )

    # How far the rebuilt baseline sits from what the run actually recorded.
    # The two curves share their settings, so the *comparison* is sound either
    # way -- but some of the execution config (stops, hysteresis, FX) is not
    # recoverable from the run metadata, and without this the user cannot tell
    # how much of any difference is the advice and how much is the rebuild.
    gap = None
    baseline_equity = getattr(baseline, "equity_curve", pd.Series(dtype=float))
    if _finite(recorded_final_equity) and len(baseline_equity):
        gap = float(baseline_equity.iloc[-1]) - float(recorded_final_equity)
        if abs(gap) > abs(float(recorded_final_equity)) * 0.005:
            notes.append(
                f"The rebuilt baseline ends {gap:+,.2f} from what the run "
                f"recorded ({float(recorded_final_equity):,.2f}). Some "
                "execution settings are not recoverable from the run "
                "metadata, so the two curves below are comparable with each "
                "other but not with the original run."
            )

    forced = detect_forced_closures(
        advised, config=settings,
        account_balance=getattr(execution_config, "initial_equity", None),
        instrument_leverage=getattr(execution_config, "max_leverage", None),
    )
    if not forced.empty:
        notes.append(
            f"The book would have crossed the platform's margin ratio on "
            f"{len(forced)} bar(s). Those are reported, not applied — closing "
            "a position mid-path would change every bar after it and the two "
            "curves would stop being comparable."
        )

    return SimulationResult(
        label=settings.label,
        baseline_metrics=_metrics(baseline, compute_all_metrics),
        advised_metrics=_metrics(advised, compute_all_metrics),
        recorded_final_equity=(
            float(recorded_final_equity) if _finite(recorded_final_equity) else None
        ),
        reconstruction_gap=gap,
        baseline_equity=getattr(baseline, "equity_curve", pd.Series(dtype=float)),
        advised_equity=getattr(advised, "equity_curve", pd.Series(dtype=float)),
        adjustments=adjustments,
        forced_closures=forced,
        trades=getattr(advised, "trades", pd.DataFrame()),
        notes=tuple(notes),
    )


def _metrics(frame, compute) -> dict[str, Any]:
    """Score a simulated run with the analytics layer's own functions."""
    try:
        metrics = dict(compute(frame))
    except Exception as error:  # noqa: BLE001
        logger.warning("Could not score the simulation: %s", error)
        return {}

    equity = getattr(frame, "equity_curve", None)
    if equity is not None and len(equity):
        metrics["final_equity"] = float(equity.iloc[-1])
    return metrics
