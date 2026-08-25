"""What the record says, sliced — and how much of it is worth believing.

Nine questions: how expectancy has moved, what the win and loss distribution
looks like, which regimes and instruments made money, and how sensitive the
results are to volatility, spread, exchange rates, execution quality and
margin.

The problem with all nine
-------------------------
**Slicing a small record makes it smaller.** Two hundred trades is a
reasonable sample; the same two hundred split across four regimes and eight
instruments is thirty-two buckets averaging six trades each, and six trades
say nothing at all. The most dangerous thing this layer could do is print a
number from six trades in the same typeface as one from six hundred, because
the reader would act on both.

So **every insight carries its sample size and a confidence, and one below the
threshold is reported as "not enough to say" rather than as a number**. The
number is still available in ``details`` for anyone who wants it; what is
withheld is the *finding*, because a finding is a claim.

The second problem is subtler. Nine dimensions with several buckets each is
dozens of comparisons, and some will look striking by chance alone. Nothing
here corrects for that arithmetically — the honest correction is to be
conservative and to say so — but the sample gate is set higher than a single
comparison would need, and a "best" bucket is never named without its runners
up.

Nothing is recomputed
---------------------
Expectancy comes from :mod:`cfdinvest.analytics`, round trips from its
pairing, regimes from :mod:`cfdinvest.risk`. This layer *groups*: it decides
which trades belong together and asks the existing functions about each group.
A second expectancy formula here would be a second answer.

Pure: frames in, insights out.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

__all__ = [
    "MINIMUM_SAMPLE",
    "CONFIDENT_SAMPLE",
    "Insight",
    "Bucket",
    "expectancy_trend",
    "win_loss_distribution",
    "regime_performance",
    "instrument_performance",
    "volatility_sensitivity",
    "spread_sensitivity",
    "fx_sensitivity",
    "execution_quality_trend",
    "margin_behaviour_trend",
    "model_insights",
]

#: Below this, a bucket produces no finding at all. Not a statistical
#: threshold -- it is the point below which one unusual trade moves the
#: average more than the thing being measured.
MINIMUM_SAMPLE = 10

#: Above this, a finding is reported as confident. Between the two it is
#: reported as provisional, which is a different claim and reads differently.
CONFIDENT_SAMPLE = 30


def _finite(value: Any) -> bool:
    try:
        return value is not None and np.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _clean(value: Any) -> float | None:
    return float(value) if _finite(value) else None


@dataclass(frozen=True, slots=True)
class Bucket:
    """One slice of the record, and how much of it there is."""

    label: str
    trades: int
    expectancy: float | None = None
    total: float | None = None
    win_rate: float | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def enough(self) -> bool:
        return self.trades >= MINIMUM_SAMPLE

    @property
    def confident(self) -> bool:
        return self.trades >= CONFIDENT_SAMPLE


@dataclass(frozen=True, slots=True)
class Insight:
    """One question asked of the record, and what could honestly be said.

    Attributes
    ----------
    finding:
        The claim, in English. **Empty when the sample is too small** — the
        figures stay in ``buckets`` and ``details``, and what is withheld is
        the assertion.
    confidence:
        ``"confident"``, ``"provisional"`` or ``"insufficient"``. Three words
        rather than a p-value, because the useful question is whether to act
        and a p-value does not answer it.
    """

    key: str
    title: str
    finding: str = ""
    confidence: str = "insufficient"
    trades: int = 0
    buckets: tuple[Bucket, ...] = ()
    caveats: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        """Whether this says anything you could act on."""
        return self.confidence != "insufficient" and bool(self.finding)

    @property
    def mark(self) -> str:
        return {
            "confident": "●", "provisional": "◐", "insufficient": "○",
        }[self.confidence]


def _confidence(trades: int) -> str:
    if trades >= CONFIDENT_SAMPLE:
        return "confident"
    if trades >= MINIMUM_SAMPLE:
        return "provisional"
    return "insufficient"


def _insufficient(key: str, title: str, trades: int, why: str = "") -> Insight:
    """The answer when there is not enough record to answer.

    Deliberately not an empty result: "we did not look" and "we looked and
    found nothing" are different, and only one of them means keep trading.
    """
    return Insight(
        key=key,
        title=title,
        finding="",
        confidence="insufficient",
        trades=trades,
        caveats=(
            why
            or f"{trades} trade(s) — below the {MINIMUM_SAMPLE} needed before "
            "one unusual result stops dominating the average.",
        ),
    )


def _round_trips(trades) -> pd.DataFrame:
    """Completed trades, through the analytics layer's own pairing."""
    from cfdinvest.analytics.metrics import as_round_trips

    try:
        return as_round_trips(trades)
    except Exception as error:  # noqa: BLE001
        logger.warning("Could not pair trades into round trips: %s", error)
        return pd.DataFrame()


def _expectancy_of(trips: pd.DataFrame) -> dict[str, float]:
    """Expectancy, from the analytics layer. Not a second formula."""
    from cfdinvest.analytics.metrics import compute_expectancy

    try:
        return compute_expectancy(trips)
    except Exception as error:  # noqa: BLE001
        logger.warning("Could not compute expectancy: %s", error)
        return {}


def _bucket(label: str, trips: pd.DataFrame, **details) -> Bucket:
    """One group of trades, scored with the shared functions."""
    if trips is None or trips.empty:
        return Bucket(label=label, trades=0, details=details)

    values = pd.to_numeric(trips["net_pnl"], errors="coerce").dropna()
    stats = _expectancy_of(trips)
    return Bucket(
        label=label,
        trades=int(len(values)),
        expectancy=_clean(stats.get("expectancy")),
        total=float(values.sum()) if len(values) else None,
        win_rate=float((values > 0).mean()) if len(values) else None,
        details={**details, **{k: _clean(v) for k, v in stats.items()}},
    )


def _by(trips: pd.DataFrame, column: str) -> list[Bucket]:
    """Group the round trips by a column, largest sample first."""
    if trips is None or trips.empty or column not in trips.columns:
        return []
    buckets = [
        _bucket(str(label), group)
        for label, group in trips.groupby(trips[column].astype(str))
    ]
    return sorted(buckets, key=lambda b: -b.trades)


def _compare(
    key: str,
    title: str,
    buckets: Sequence[Bucket],
    *,
    noun: str,
    trades: int,
) -> Insight:
    """Name the best and worst buckets that have enough trades to be named.

    A "best" is never reported alone: with several buckets, one of them is
    always the best, and printing only the winner turns an ordering into a
    discovery.
    """
    usable = [b for b in buckets if b.enough and b.expectancy is not None]
    caveats: list[str] = []

    thin = [b for b in buckets if not b.enough]
    if thin:
        caveats.append(
            f"{len(thin)} {noun}(s) had fewer than {MINIMUM_SAMPLE} trades and "
            f"were left out of the finding: {', '.join(b.label for b in thin)}."
        )

    if len(usable) < 2:
        return Insight(
            key=key, title=title, finding="", confidence="insufficient",
            trades=trades, buckets=tuple(buckets),
            caveats=tuple(
                caveats
                or (f"Fewer than two {noun}s have enough trades to compare.",)
            ),
        )

    ordered = sorted(usable, key=lambda b: -(b.expectancy or 0))
    best, worst = ordered[0], ordered[-1]

    finding = (
        f"Best {noun}: {best.label} ({best.expectancy:,.2f} per trade over "
        f"{best.trades}). Worst: {worst.label} ({worst.expectancy:,.2f} over "
        f"{worst.trades})."
    )
    if len(ordered) > 2:
        middle = ", ".join(
            f"{b.label} {b.expectancy:,.2f}" for b in ordered[1:-1]
        )
        finding += f" Between them: {middle}."

    caveats.append(
        f"With {len(usable)} {noun}s compared, one of them is the best by "
        "construction. Treat the ordering as a question rather than a result."
    )

    return Insight(
        key=key,
        title=title,
        finding=finding,
        confidence=_confidence(min(best.trades, worst.trades)),
        trades=trades,
        buckets=tuple(ordered),
        caveats=tuple(caveats),
        details={"compared": len(usable), "skipped": len(thin)},
    )


# --------------------------------------------------------------------------- #
# 1. Expectancy over time
# --------------------------------------------------------------------------- #
def expectancy_trend(trades, *, window: int = 30) -> Insight:
    """Has the edge moved, or has it merely wobbled?

    Compared as the most recent ``window`` trades against everything before
    them. A rolling series would look more sophisticated and say less: what
    you want to know is whether now is different from before, and two numbers
    answer that where a curve invites reading a shape into noise.
    """
    trips = _round_trips(trades)
    if trips.empty or "net_pnl" not in trips.columns:
        return _insufficient("expectancy_trend", "Expectancy over time", 0)

    values = pd.to_numeric(trips["net_pnl"], errors="coerce").dropna()
    if len(values) < MINIMUM_SAMPLE * 2:
        return _insufficient(
            "expectancy_trend", "Expectancy over time", int(len(values)),
            why=f"{len(values)} trade(s) — a before-and-after comparison needs "
                f"at least {MINIMUM_SAMPLE} on each side.",
        )

    split = min(window, len(values) // 2)
    earlier, recent = values.iloc[:-split], values.iloc[-split:]

    before = _bucket("earlier", trips.iloc[:-split])
    after = _bucket(f"last {split}", trips.iloc[-split:])

    change = (after.expectancy or 0) - (before.expectancy or 0)
    direction = "improved" if change > 0 else "fallen"
    material = abs(change) > abs(before.expectancy or 1) * 0.25

    finding = (
        f"Expectancy over the last {split} trades is "
        f"{after.expectancy:,.2f}, against {before.expectancy:,.2f} over the "
        f"{before.trades} before them — {direction} by {abs(change):,.2f}."
    )
    if not material:
        finding += " Small enough to be a normal run."

    return Insight(
        key="expectancy_trend",
        title="Expectancy over time",
        finding=finding,
        confidence=_confidence(min(len(earlier), len(recent))),
        trades=int(len(values)),
        buckets=(before, after),
        caveats=(
            "A change of this size happens often enough by chance that one "
            "comparison should not change how you trade.",
        ) if not material else (),
        details={"change": change, "window": split},
    )


# --------------------------------------------------------------------------- #
# 2. Win and loss distribution
# --------------------------------------------------------------------------- #
def win_loss_distribution(trades) -> Insight:
    """The shape of the outcomes, not just the average.

    A win rate on its own is close to meaningless: 30% winners with a 4:1
    payoff is a good strategy and 70% winners with a 1:4 payoff is not. Both
    are reported, and the finding names the pair.
    """
    trips = _round_trips(trades)
    if trips.empty or "net_pnl" not in trips.columns:
        return _insufficient("win_loss", "Win and loss distribution", 0)

    values = pd.to_numeric(trips["net_pnl"], errors="coerce").dropna()
    if len(values) < MINIMUM_SAMPLE:
        return _insufficient(
            "win_loss", "Win and loss distribution", int(len(values))
        )

    wins, losses = values[values > 0], values[values <= 0]
    stats = _expectancy_of(trips)
    payoff = _clean(stats.get("payoff_ratio"))
    rate = float(len(wins) / len(values))

    finding = (
        f"{rate:.0%} of {len(values)} trades were winners, averaging "
        f"{wins.mean():,.2f}; losers averaged {abs(losses.mean()):,.2f}."
    )
    if payoff is not None:
        finding += f" Payoff ratio {payoff:,.2f}."
        if rate < 0.5 and payoff > 1.5:
            finding += (
                " A low win rate with a payoff this size is the normal shape "
                "of a trend strategy, not a problem."
            )
        elif rate > 0.6 and payoff < 0.8:
            finding += (
                " Winning often and small while losing rarely and large is "
                "the shape that looks best and survives worst."
            )

    largest_loss = float(losses.min()) if len(losses) else None
    caveats = []
    if largest_loss is not None and len(losses):
        share = abs(largest_loss) / abs(losses.sum()) if losses.sum() else 0
        if share > 0.3:
            caveats.append(
                f"One loss of {abs(largest_loss):,.2f} is {share:.0%} of all "
                "losses. The average is describing that trade more than the "
                "strategy."
            )

    return Insight(
        key="win_loss",
        title="Win and loss distribution",
        finding=finding,
        confidence=_confidence(int(len(values))),
        trades=int(len(values)),
        buckets=(
            _bucket("winners", trips[values > 0]),
            _bucket("losers", trips[values <= 0]),
        ),
        caveats=tuple(caveats),
        details={
            "win_rate": rate,
            "payoff_ratio": payoff,
            "largest_win": float(wins.max()) if len(wins) else None,
            "largest_loss": largest_loss,
            **{k: _clean(v) for k, v in stats.items()},
        },
    )


# --------------------------------------------------------------------------- #
# 3 and 4. By regime and by instrument
# --------------------------------------------------------------------------- #
def regime_performance(trades, regime_history=None) -> Insight:
    """Which markets the strategy actually made money in.

    Needs the regime *history*, not the current label: a trade is judged by
    the regime it was taken in, and today's classification says nothing about
    a trade from March.
    """
    trips = _round_trips(trades)
    if trips.empty:
        return _insufficient("regime_performance", "Performance by regime", 0)

    labelled = _attach_regime(trips, regime_history)
    if labelled is None:
        return Insight(
            key="regime_performance",
            title="Performance by regime",
            finding="",
            confidence="insufficient",
            trades=int(len(trips)),
            caveats=(
                "No regime history was available, so trades could not be "
                "grouped by the market they were taken in. Today's regime "
                "says nothing about a trade from March.",
            ),
        )

    return _compare(
        "regime_performance", "Performance by regime",
        _by(labelled, "regime"), noun="regime", trades=int(len(trips)),
    )


def _attach_regime(trips: pd.DataFrame, regime_history) -> pd.DataFrame | None:
    """Label each trade with the regime in force when it was opened."""
    if regime_history is None or getattr(regime_history, "empty", True):
        return None
    if "timestamp" not in regime_history.columns:
        return None

    column = next(
        (
            name for name in
            ("combined_regime", "volatility_regime", "trend_regime")
            if name in regime_history.columns
        ),
        None,
    )
    if column is None:
        return None

    time_column = next(
        (name for name in ("entry_time", "timestamp", "opened_at")
         if name in trips.columns),
        None,
    )
    if time_column is None:
        return None

    history = regime_history[["timestamp", column]].copy()
    history["timestamp"] = pd.to_datetime(
        history["timestamp"], utc=True, errors="coerce"
    )
    history = history.dropna(subset=["timestamp"]).sort_values("timestamp")

    frame = trips.copy()
    frame["_when"] = pd.to_datetime(frame[time_column], utc=True, errors="coerce")
    frame = frame.dropna(subset=["_when"]).sort_values("_when")
    if frame.empty or history.empty:
        return None

    merged = pd.merge_asof(
        frame, history.rename(columns={column: "regime"}),
        left_on="_when", right_on="timestamp", direction="backward",
    )
    return merged.dropna(subset=["regime"])


def instrument_performance(trades) -> Insight:
    """Which instruments carried the record, and which cost."""
    trips = _round_trips(trades)
    if trips.empty:
        return _insufficient(
            "instrument_performance", "Performance by instrument", 0
        )

    column = next(
        (name for name in ("asset", "ticker", "instrument") if name in trips.columns),
        None,
    )
    if column is None:
        return _insufficient(
            "instrument_performance", "Performance by instrument",
            int(len(trips)),
            why="The trade log does not name the instrument.",
        )

    return _compare(
        "instrument_performance", "Performance by instrument",
        _by(trips, column), noun="instrument", trades=int(len(trips)),
    )


# --------------------------------------------------------------------------- #
# 5, 6, 7. Sensitivities
# --------------------------------------------------------------------------- #
def _sensitivity(
    trades, column: str, key: str, title: str, noun: str, *, note: str = ""
) -> Insight:
    """Split the record into thirds by one cost and compare the outcomes.

    Terciles rather than a correlation: a correlation coefficient between a
    cost and a P&L is a number almost nobody can act on, and "the third with
    the widest spreads made half as much" is.
    """
    trips = _round_trips(trades)
    if trips.empty or column not in trips.columns:
        return _insufficient(
            key, title, int(len(trips)),
            why=f"The trade log does not record {noun}.",
        )

    values = pd.to_numeric(trips[column], errors="coerce")
    usable = trips[values.notna()]
    if len(usable) < MINIMUM_SAMPLE * 3:
        return _insufficient(
            key, title, int(len(usable)),
            why=f"{len(usable)} trade(s) with a recorded {noun} — three "
                f"buckets need at least {MINIMUM_SAMPLE * 3}.",
        )

    clean = pd.to_numeric(usable[column], errors="coerce")
    if clean.nunique() < 3:
        return _insufficient(
            key, title, int(len(usable)),
            why=f"{noun.capitalize()} barely varies across the record, so "
                "there is nothing to compare.",
        )

    try:
        labels = pd.qcut(
            clean, 3, labels=["lowest third", "middle third", "highest third"],
            duplicates="drop",
        )
    except ValueError:
        return _insufficient(
            key, title, int(len(usable)),
            why=f"{noun.capitalize()} could not be split into thirds.",
        )

    grouped = usable.assign(_tercile=labels.astype(str))
    buckets = _by(grouped, "_tercile")
    insight = _compare(key, title, buckets, noun="third", trades=int(len(usable)))

    if note and insight.finding:
        insight = replace(insight, caveats=insight.caveats + (note,))
    return insight


def volatility_sensitivity(trades) -> Insight:
    """Does the strategy do better when the market is moving?"""
    trips = _round_trips(trades)
    column = next(
        (name for name in ("volatility", "atr", "stop_distance")
         if name in trips.columns),
        None,
    )
    if column is None:
        return _insufficient(
            "volatility_sensitivity", "Sensitivity to volatility",
            int(len(trips)),
            why="The trade log records no volatility measure, so this cannot "
                "be answered from it.",
        )
    return _sensitivity(
        trades, column, "volatility_sensitivity", "Sensitivity to volatility",
        "volatility",
    )


def spread_sensitivity(trades) -> Insight:
    """How much of the result the spread is eating."""
    return _sensitivity(
        trades, "spread_cost", "spread_sensitivity", "Sensitivity to spread",
        "spread cost",
        note="Spread is charged on entry and exit, so a wide-spread third is "
             "also a more-traded third. The two are hard to separate here.",
    )


def fx_sensitivity(trades) -> Insight:
    """Whether the exchange rate is doing more than the instrument."""
    trips = _round_trips(trades)
    column = next(
        (name for name in ("fx_rate", "fx_conversion_fee", "fx_spread")
         if name in trips.columns),
        None,
    )
    if column is None:
        return _insufficient(
            "fx_sensitivity", "Sensitivity to exchange rates", int(len(trips)),
            why="The trade log records no FX detail.",
        )
    return _sensitivity(
        trades, column, "fx_sensitivity", "Sensitivity to exchange rates",
        "exchange rate",
    )


# --------------------------------------------------------------------------- #
# 8 and 9. Execution and margin over time
# --------------------------------------------------------------------------- #
def execution_quality_trend(confirmations=None) -> Insight:
    """Are your fills getting closer to the plan, or further from it?

    Read from the confirmation record, which is the only artefact that knows
    what you actually did as against what was suggested.
    """
    if confirmations is None or getattr(confirmations, "empty", True):
        return Insight(
            key="execution_quality",
            title="Execution quality over time",
            finding="",
            confidence="insufficient",
            caveats=(
                "Nothing has been confirmed, so there is no record of what you "
                "actually did. That is not the same as fills matching the plan.",
            ),
        )

    frame = confirmations.copy()
    deviations = pd.to_numeric(
        frame.get("quantity_deviation"), errors="coerce"
    ).dropna() if "quantity_deviation" in frame.columns else pd.Series(dtype=float)
    slippage = pd.to_numeric(
        frame.get("slippage"), errors="coerce"
    ).dropna() if "slippage" in frame.columns else pd.Series(dtype=float)

    measured = max(len(deviations), len(slippage))
    if measured < MINIMUM_SAMPLE:
        return _insufficient(
            "execution_quality", "Execution quality over time", int(measured)
        )

    parts = []
    if len(slippage) >= MINIMUM_SAMPLE:
        half = len(slippage) // 2
        before, after = slippage.iloc[:half].mean(), slippage.iloc[half:].mean()
        moved = "worse" if after > before else "better"
        parts.append(
            f"Average slippage has moved {moved}: {before:,.4f} over the first "
            f"{half} fills, {after:,.4f} over the last {len(slippage) - half}."
        )
    if len(deviations) >= MINIMUM_SAMPLE:
        drift = float((deviations - 1.0).abs().mean())
        parts.append(
            f"Sizes have averaged {drift:.0%} away from what was recommended."
        )

    return Insight(
        key="execution_quality",
        title="Execution quality over time",
        finding=" ".join(parts),
        confidence=_confidence(int(measured)),
        trades=int(measured),
        caveats=(
            "Slippage here is what you recorded, so it measures the fills you "
            "chose to confirm as much as the fills you got.",
        ),
        details={
            "mean_slippage": _clean(slippage.mean()) if len(slippage) else None,
            "mean_deviation": _clean(deviations.mean()) if len(deviations) else None,
            "confirmations": int(len(frame)),
        },
    )


def margin_behaviour_trend(risk_history=None) -> Insight:
    """How you have actually been using margin, over the recorded snapshots."""
    if risk_history is None or getattr(risk_history, "empty", True):
        return Insight(
            key="margin_behaviour",
            title="Margin behaviour over time",
            finding="",
            confidence="insufficient",
            caveats=(
                "No risk snapshots have been recorded, so margin usage has no "
                "history to read.",
            ),
        )

    usage = pd.to_numeric(
        risk_history.get("margin_usage_pct"), errors="coerce"
    ).dropna() if "margin_usage_pct" in risk_history.columns else pd.Series(dtype=float)
    leverage = pd.to_numeric(
        risk_history.get("effective_leverage"), errors="coerce"
    ).dropna() if "effective_leverage" in risk_history.columns else pd.Series(dtype=float)

    if len(usage) < 3 and len(leverage) < 3:
        return _insufficient(
            "margin_behaviour", "Margin behaviour over time",
            int(max(len(usage), len(leverage))),
            why="Fewer than three snapshots — two points make a line through "
                "anything.",
        )

    parts, details = [], {}
    if len(usage) >= 3:
        parts.append(
            f"Margin usage has averaged {usage.mean():.0%} across "
            f"{len(usage)} snapshots, peaking at {usage.max():.0%}."
        )
        if usage.iloc[-1] > usage.iloc[0]:
            parts.append(
                f"It has risen {usage.iloc[-1] - usage.iloc[0]:+.0%} over the "
                "period."
            )
        details.update(
            {"mean_usage": float(usage.mean()), "peak_usage": float(usage.max())}
        )
    if len(leverage) >= 3:
        parts.append(
            f"Effective leverage has averaged {leverage.mean():,.1f}x, peaking "
            f"at {leverage.max():,.1f}x."
        )
        details["mean_leverage"] = float(leverage.mean())

    return Insight(
        key="margin_behaviour",
        title="Margin behaviour over time",
        finding=" ".join(parts),
        confidence="provisional" if len(usage) < CONFIDENT_SAMPLE else "confident",
        trades=int(len(usage)),
        caveats=(
            "Snapshots are taken on a schedule, not at the moments margin "
            "peaked. The true peak was probably higher than any recorded one.",
        ),
        details=details,
    )


def model_insights(
    trades, *, regime_history=None, confirmations=None, risk_history=None
):
    """Every insight as ``(key, title, callable)``, in reading order.

    The list lives here rather than in the controller so that adding an
    insight is one change in one file, and so the controller cannot quietly
    fall out of step with what exists.
    """
    return (
        ("expectancy_trend", "Expectancy over time",
         lambda: expectancy_trend(trades)),
        ("win_loss", "Win and loss distribution",
         lambda: win_loss_distribution(trades)),
        ("regime_performance", "Performance by regime",
         lambda: regime_performance(trades, regime_history)),
        ("instrument_performance", "Performance by instrument",
         lambda: instrument_performance(trades)),
        ("volatility_sensitivity", "Sensitivity to volatility",
         lambda: volatility_sensitivity(trades)),
        ("spread_sensitivity", "Sensitivity to spread",
         lambda: spread_sensitivity(trades)),
        ("fx_sensitivity", "Sensitivity to exchange rates",
         lambda: fx_sensitivity(trades)),
        ("execution_quality", "Execution quality over time",
         lambda: execution_quality_trend(confirmations)),
        ("margin_behaviour", "Margin behaviour over time",
         lambda: margin_behaviour_trend(risk_history)),
    )
