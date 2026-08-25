"""Patterns in how you have traded — described, not diagnosed.

What this layer must not become
-------------------------------
**It does not tell you what kind of person you are.** It reports observable
patterns in a trade record and leaves the interpretation where it belongs.

The distinction is not decoration. "You are overconfident" is a claim about
someone's character made from a few dozen rows of a spreadsheet, and it is the
kind of claim people internalise and then trade around. "Six of your last eight
trades were larger than the one before, and five followed a win" is the same
evidence, stated as evidence, and it leaves room for the obvious innocent
explanation — that you meant to.

So every finding here names the pattern and never the person, offers the
innocent reading alongside the worrying one, and proposes a change to a
*process* rather than to a temperament. "Be less emotional" is not an action.
"Write the size down before you look at the last result" is.

Why the sample floor is higher than elsewhere
---------------------------------------------
Two reasons.

Behavioural inference is weaker evidence than P&L arithmetic: the same
sequence of sizes is consistent with a rule, a mood, and a coincidence, and
nothing in the record separates them.

And these severities are *acted on*. :mod:`cfdinvest.livehelper.refine_sizing`
cuts real position sizes when a bias severity passes 0.5, so a false positive
here does not merely insult you — it shrinks the next trade. That consequence
is what justifies being slow to assert anything.

Nine patterns, and one of them deliberately does nothing
--------------------------------------------------------
Fear-driven under-exposure is detected and reported and **never used to
increase a position**. A system that talked a frightened person into trading
larger would be the worst thing in this repository, and the asymmetry is
enforced in the refinement layer rather than trusted here.

Pure: frames in, findings out.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Sequence

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

__all__ = [
    "BIASES",
    "MINIMUM_OBSERVATIONS",
    "CONFIDENT_OBSERVATIONS",
    "ASSERT_THRESHOLD",
    "BiasFinding",
    "detect_overconfidence",
    "detect_revenge_trading",
    "detect_fear_driven_underexposure",
    "detect_risk_avoidance",
    "detect_loss_aversion",
    "detect_premature_exits",
    "detect_late_exits",
    "detect_leverage_creep",
    "detect_margin_blindness",
    "model_detectors",
]

#: The patterns, and what each is called in plain words. The keys match
#: :class:`cfdinvest.livehelper.context.BiasProfile` exactly -- that contract
#: was written first and this layer fills it rather than defining a second.
BIASES: dict[str, str] = {
    "overconfidence": "Sizing up after wins",
    "revenge_trading": "Trading again quickly after a loss",
    "fear_driven_underexposure": "Taking much less than the plan",
    "risk_avoidance": "Declining trades after losses",
    "loss_aversion": "Holding losers longer than winners",
    "premature_exits": "Closing winners early",
    "late_exits": "Closing losers late",
    "leverage_creep": "Leverage drifting upwards",
    "margin_blindness": "Trading on with margin already high",
}

#: Below this, no severity is produced at all. Higher than the insights
#: layer's floor because behavioural inference is weaker evidence, and because
#: a severity here shrinks a real position.
MINIMUM_OBSERVATIONS = 15

#: Above this the finding is confident rather than provisional.
CONFIDENT_OBSERVATIONS = 40

#: The severity at which the refinement layer starts cutting size. Nothing
#: here reaches it without both a clear pattern and enough of a record --
#: crossing it is a decision to change how someone trades.
ASSERT_THRESHOLD = 0.5


def _finite(value: Any) -> bool:
    try:
        return value is not None and np.isfinite(float(value))
    except (TypeError, ValueError):
        return False


@dataclass(frozen=True, slots=True)
class BiasFinding:
    """One pattern, the evidence for it, and what could be done differently.

    Attributes
    ----------
    severity:
        ``0``–``1``, or ``None`` when there was not enough record. ``None`` is
        **not** zero: "not measured" and "no evidence" are different, and only
        the second is reassuring.
    evidence:
        What was actually counted, in a sentence. Names the pattern, never
        the person.
    innocent_reading:
        The other explanation. Always present when a finding is asserted,
        because the same numbers usually are consistent with having meant to.
    action:
        A change to a process, not to a temperament.
    """

    key: str
    title: str
    severity: float | None = None
    observations: int = 0
    confidence: str = "insufficient"
    evidence: str = ""
    innocent_reading: str = ""
    action: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def asserted(self) -> bool:
        """Whether this is strong enough to act on."""
        return (
            self.severity is not None
            and self.severity >= ASSERT_THRESHOLD
            and self.confidence != "insufficient"
        )

    @property
    def measured(self) -> bool:
        return self.severity is not None


def _confidence(observations: int) -> str:
    if observations >= CONFIDENT_OBSERVATIONS:
        return "confident"
    if observations >= MINIMUM_OBSERVATIONS:
        return "provisional"
    return "insufficient"


def _unmeasured(key: str, why: str) -> BiasFinding:
    """The answer when the record cannot support one.

    Severity stays ``None``. A zero would read as "no evidence of this", which
    is a reassurance the record has not earned.
    """
    return BiasFinding(
        key=key,
        title=BIASES[key],
        severity=None,
        confidence="insufficient",
        evidence="",
        details={"reason": why},
        action="",
        innocent_reading="",
    )


def _severity(rate: float, floor: float, ceiling: float) -> float:
    """Turn a rate into a severity, scaled between two rates.

    Linear and clamped. A curve would imply a precision the underlying
    evidence does not have.
    """
    if ceiling <= floor:
        return 0.0
    return float(min(max((rate - floor) / (ceiling - floor), 0.0), 1.0))


def _numeric(frame, column: str) -> pd.Series:
    if frame is None or getattr(frame, "empty", True) or column not in frame.columns:
        return pd.Series(dtype="float64")
    return pd.to_numeric(frame[column], errors="coerce").dropna()


# --------------------------------------------------------------------------- #
# Sizing patterns
# --------------------------------------------------------------------------- #
def detect_overconfidence(confirmations=None) -> BiasFinding:
    """Does size go up after a win?

    Measured against the *previous outcome*, not against the average, because
    the pattern of interest is a reaction rather than a level. A trader who
    always trades large is not overconfident; one whose size tracks the last
    result is reacting to it.
    """
    key = "overconfidence"
    if confirmations is None or getattr(confirmations, "empty", True):
        return _unmeasured(key, "nothing has been confirmed")

    frame = confirmations.copy()
    if "quantity" not in frame.columns or "timestamp" not in frame.columns:
        return _unmeasured(key, "the confirmation log records no sizes")

    frame = frame.sort_values("timestamp")
    sizes = pd.to_numeric(frame["quantity"], errors="coerce").abs()
    outcomes = pd.to_numeric(
        frame.get("realised_pnl", pd.Series(dtype=float)), errors="coerce"
    )

    if outcomes.notna().sum() < MINIMUM_OBSERVATIONS:
        return _unmeasured(
            key,
            "outcomes are not recorded against confirmations, so size cannot "
            "be compared with the result before it",
        )

    after_win = sizes.shift(-1)[outcomes > 0].dropna()
    after_loss = sizes.shift(-1)[outcomes <= 0].dropna()
    observations = int(len(after_win) + len(after_loss))

    if observations < MINIMUM_OBSERVATIONS or after_win.empty or after_loss.empty:
        return _unmeasured(
            key, f"{observations} usable pair(s), below {MINIMUM_OBSERVATIONS}"
        )

    ratio = float(after_win.mean() / after_loss.mean()) if after_loss.mean() else 1.0
    severity = _severity(ratio, 1.15, 2.0)

    return BiasFinding(
        key=key,
        title=BIASES[key],
        severity=severity,
        observations=observations,
        confidence=_confidence(observations),
        evidence=(
            f"Trades taken after a win averaged {after_win.mean():,.4f} units; "
            f"after a loss, {after_loss.mean():,.4f} — {ratio:,.2f} times "
            f"larger, over {observations} trades."
        ),
        innocent_reading=(
            "Sizing up while an approach is working is a deliberate strategy "
            "for some people. The record cannot tell a rule from a reaction."
        ),
        action=(
            "Write the size down before you look at how the last trade ended. "
            "If it is the same number either way, there is nothing here."
        ),
        details={"ratio": ratio, "after_win": float(after_win.mean()),
                 "after_loss": float(after_loss.mean())},
    )


def detect_fear_driven_underexposure(confirmations=None) -> BiasFinding:
    """Are you repeatedly taking much less than the plan called for?

    **Reported and never acted on to increase size.** The refinement layer
    refuses to size up on this finding, and that refusal is deliberate: a
    system that talked a frightened person into a larger position would be
    the worst thing in this repository.
    """
    key = "fear_driven_underexposure"
    deviations = _numeric(confirmations, "quantity_deviation")

    if len(deviations) < MINIMUM_OBSERVATIONS:
        return _unmeasured(
            key,
            f"{len(deviations)} confirmed trade(s) with a recorded "
            f"recommendation, below {MINIMUM_OBSERVATIONS}",
        )

    under = deviations[deviations < 0.75]
    rate = float(len(under) / len(deviations))
    severity = _severity(rate, 0.35, 0.80)

    return BiasFinding(
        key=key,
        title=BIASES[key],
        severity=severity,
        observations=int(len(deviations)),
        confidence=_confidence(int(len(deviations))),
        evidence=(
            f"{rate:.0%} of {len(deviations)} confirmed trades were under "
            "three-quarters of the recommended size, averaging "
            f"{deviations.mean():.0%} of it."
        ),
        innocent_reading=(
            "Trading smaller than a model suggests is often simply correct, "
            "and the model does not know what else you have on."
        ),
        action=(
            "Nothing here will ever tell you to trade larger. If the "
            "recommended size is one you would not take, the limit that "
            "produced it is the thing to change — deliberately, not in the "
            "moment."
        ),
        details={"rate": rate, "mean_deviation": float(deviations.mean())},
    )


# --------------------------------------------------------------------------- #
# Timing patterns
# --------------------------------------------------------------------------- #
def detect_revenge_trading(confirmations=None, *, window_hours: float = 4.0) -> BiasFinding:
    """Do you trade again quickly after a loss, and bigger?

    Both conditions together. Trading again soon after a loss is normal in a
    day of trading; trading again soon *and larger* is the pattern.
    """
    key = "revenge_trading"
    if confirmations is None or getattr(confirmations, "empty", True):
        return _unmeasured(key, "nothing has been confirmed")
    if not {"timestamp", "quantity"} <= set(confirmations.columns):
        return _unmeasured(key, "the confirmation log records no times or sizes")

    frame = confirmations.copy().sort_values("timestamp")
    times = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
    sizes = pd.to_numeric(frame["quantity"], errors="coerce").abs()
    outcomes = pd.to_numeric(
        frame.get("realised_pnl", pd.Series(dtype=float)), errors="coerce"
    )

    if outcomes.notna().sum() < MINIMUM_OBSERVATIONS:
        return _unmeasured(
            key, "outcomes are not recorded, so a trade cannot be tied to the "
                 "result before it"
        )

    gaps = times.diff().shift(-1).dt.total_seconds() / 3600.0
    typical = float(sizes.median()) if len(sizes) else 0.0

    losses = outcomes <= 0
    quick = gaps < window_hours
    bigger = sizes.shift(-1) > typical * 1.25

    candidates = int(losses.sum())
    if candidates < MINIMUM_OBSERVATIONS:
        return _unmeasured(
            key, f"{candidates} loss(es) recorded, below {MINIMUM_OBSERVATIONS}"
        )

    hits = int((losses & quick & bigger).sum())
    rate = hits / candidates if candidates else 0.0
    severity = _severity(rate, 0.20, 0.60)

    return BiasFinding(
        key=key,
        title=BIASES[key],
        severity=severity,
        observations=candidates,
        confidence=_confidence(candidates),
        evidence=(
            f"After {candidates} losses, {hits} were followed within "
            f"{window_hours:,.0f} hours by a trade more than a quarter larger "
            f"than your median — {rate:.0%}."
        ),
        innocent_reading=(
            "A loss and the next setup often arrive together in a busy "
            "session, and a bigger size may simply be a better setup."
        ),
        action=(
            "Put a fixed gap between a loss and the next order — long enough "
            "to have to decide again rather than continue."
        ),
        details={"rate": rate, "hits": hits, "window_hours": window_hours},
    )


def detect_risk_avoidance(confirmations=None) -> BiasFinding:
    """Do you decline trades disproportionately after a loss?

    Readable only because declining is recorded: ``cfdinvest-confirm ignore``
    writes it down. Without that, a skipped trade leaves no trace at all and
    this pattern is invisible.
    """
    key = "risk_avoidance"
    if confirmations is None or getattr(confirmations, "empty", True):
        return _unmeasured(key, "nothing has been confirmed")
    if "action" not in confirmations.columns:
        return _unmeasured(key, "the confirmation log records no actions")

    frame = confirmations.copy().sort_values("timestamp")
    actions = frame["action"].astype(str)
    outcomes = pd.to_numeric(
        frame.get("realised_pnl", pd.Series(dtype=float)), errors="coerce"
    )

    if (actions == "ignore").sum() == 0:
        return _unmeasured(
            key,
            "no declined trades have been recorded. That is not evidence of "
            "not declining — an unrecorded pass leaves no trace",
        )
    if outcomes.notna().sum() < MINIMUM_OBSERVATIONS:
        return _unmeasured(key, "outcomes are not recorded against decisions")

    declined_next = (actions.shift(-1) == "ignore")
    after_loss = declined_next[outcomes <= 0]
    after_win = declined_next[outcomes > 0]
    observations = int(len(after_loss) + len(after_win))

    if observations < MINIMUM_OBSERVATIONS or after_loss.empty or after_win.empty:
        return _unmeasured(
            key, f"{observations} usable pair(s), below {MINIMUM_OBSERVATIONS}"
        )

    loss_rate, win_rate = float(after_loss.mean()), float(after_win.mean())
    excess = loss_rate - win_rate
    severity = _severity(excess, 0.15, 0.50)

    return BiasFinding(
        key=key,
        title=BIASES[key],
        severity=severity,
        observations=observations,
        confidence=_confidence(observations),
        evidence=(
            f"You declined {loss_rate:.0%} of the setups that followed a loss, "
            f"against {win_rate:.0%} of those that followed a win."
        ),
        innocent_reading=(
            "Standing aside after a loss is a recognised discipline, and the "
            "setups themselves may genuinely have been worse."
        ),
        action=(
            "Note the reason at the moment you decline. A reason about the "
            "setup and a reason about the last trade look identical a week "
            "later."
        ),
        details={"after_loss": loss_rate, "after_win": win_rate, "excess": excess},
    )


# --------------------------------------------------------------------------- #
# Exit patterns
# --------------------------------------------------------------------------- #
def detect_loss_aversion(trades=None) -> BiasFinding:
    """Are losers held longer than winners?

    The classic asymmetry, and the one most visible in a trade log: a losing
    position is a decision deferred, and deferring is free until it is not.
    """
    key = "loss_aversion"
    from cfdinvest.analytics.metrics import as_round_trips

    try:
        trips = as_round_trips(trades)
    except Exception as error:  # noqa: BLE001
        return _unmeasured(key, f"trades could not be paired: {error}")

    if trips is None or trips.empty or "net_pnl" not in trips.columns:
        return _unmeasured(key, "no completed round trips")

    column = next(
        (name for name in ("bars_held", "duration", "holding_bars")
         if name in trips.columns),
        None,
    )
    if column is None:
        return _unmeasured(
            key, "the trade log records no holding period, so winners and "
                 "losers cannot be compared on it"
        )

    held = pd.to_numeric(trips[column], errors="coerce")
    values = pd.to_numeric(trips["net_pnl"], errors="coerce")
    usable = held.notna() & values.notna()

    if int(usable.sum()) < MINIMUM_OBSERVATIONS:
        return _unmeasured(
            key, f"{int(usable.sum())} usable trade(s), below "
                 f"{MINIMUM_OBSERVATIONS}"
        )

    winners = held[usable & (values > 0)]
    losers = held[usable & (values <= 0)]
    if winners.empty or losers.empty:
        return _unmeasured(key, "the record has only winners or only losers")

    ratio = float(losers.mean() / winners.mean()) if winners.mean() else 1.0
    severity = _severity(ratio, 1.25, 3.0)

    return BiasFinding(
        key=key,
        title=BIASES[key],
        severity=severity,
        observations=int(usable.sum()),
        confidence=_confidence(int(usable.sum())),
        evidence=(
            f"Losing trades were held {losers.mean():,.1f} bars on average and "
            f"winning ones {winners.mean():,.1f} — {ratio:,.2f} times longer."
        ),
        innocent_reading=(
            "A trend strategy holds winners to a target and cuts losers at a "
            "stop, which produces the *opposite* asymmetry — so this direction "
            "is worth a look, though a wide stop and a near target would "
            "explain it mechanically."
        ),
        action=(
            "Check whether the stop distance and the target distance are the "
            "ones you meant. This pattern is often a plan, not a flinch."
        ),
        details={"ratio": ratio, "loser_bars": float(losers.mean()),
                 "winner_bars": float(winners.mean())},
    )


def _exit_distance(trades, *, winners: bool) -> tuple[float, int] | None:
    """How far exits landed from the level that was set, as a fraction."""
    from cfdinvest.analytics.metrics import as_round_trips

    try:
        trips = as_round_trips(trades)
    except Exception:  # noqa: BLE001
        return None
    if trips is None or trips.empty or "net_pnl" not in trips.columns:
        return None

    level = "take_profit_level" if winners else "stop_level"
    if level not in trips.columns or "exit_price" not in trips.columns:
        return None

    values = pd.to_numeric(trips["net_pnl"], errors="coerce")
    chosen = trips[values > 0] if winners else trips[values <= 0]
    if chosen.empty:
        return None

    target = pd.to_numeric(chosen[level], errors="coerce")
    exit_price = pd.to_numeric(chosen["exit_price"], errors="coerce")
    entry = pd.to_numeric(
        chosen.get("entry_price", pd.Series(dtype=float)), errors="coerce"
    )

    usable = target.notna() & exit_price.notna() & entry.notna()
    if int(usable.sum()) == 0:
        return None

    planned = (target - entry).abs()
    actual = (exit_price - entry).abs()
    share = (actual[usable] / planned[usable].where(planned[usable] > 0)).dropna()
    if share.empty:
        return None
    return float(share.mean()), int(len(share))


def detect_premature_exits(trades=None) -> BiasFinding:
    """Are winners closed well before the target they were given?"""
    key = "premature_exits"
    measured = _exit_distance(trades, winners=True)
    if measured is None:
        return _unmeasured(
            key, "the trade log does not record both a target and an exit "
                 "price, so exits cannot be compared with the plan"
        )

    share, count = measured
    if count < MINIMUM_OBSERVATIONS:
        return _unmeasured(
            key, f"{count} winning trade(s) with a target, below "
                 f"{MINIMUM_OBSERVATIONS}"
        )

    severity = _severity(1.0 - share, 0.25, 0.70)
    return BiasFinding(
        key=key,
        title=BIASES[key],
        severity=severity,
        observations=count,
        confidence=_confidence(count),
        evidence=(
            f"Winning trades were closed at {share:.0%} of the distance to "
            f"their target, on average, over {count} trades."
        ),
        innocent_reading=(
            "Taking profit before a target is often the right call, and a "
            "target you never reach may simply be too far away."
        ),
        action=(
            "If the target is rarely reached, move the target rather than "
            "holding harder. The plan is the thing to change, not the nerve."
        ),
        details={"mean_share_of_target": share},
    )


def detect_late_exits(trades=None) -> BiasFinding:
    """Are losers closed well past the stop they were given?

    The more expensive of the two exit patterns: a stop that is not honoured
    is not a stop, and every risk figure computed from it was wrong.
    """
    key = "late_exits"
    measured = _exit_distance(trades, winners=False)
    if measured is None:
        return _unmeasured(
            key, "the trade log does not record both a stop and an exit price"
        )

    share, count = measured
    if count < MINIMUM_OBSERVATIONS:
        return _unmeasured(
            key, f"{count} losing trade(s) with a stop, below "
                 f"{MINIMUM_OBSERVATIONS}"
        )

    severity = _severity(share - 1.0, 0.10, 0.60)
    return BiasFinding(
        key=key,
        title=BIASES[key],
        severity=severity,
        observations=count,
        confidence=_confidence(count),
        evidence=(
            f"Losing trades were closed at {share:.0%} of the distance to "
            f"their stop, on average — past it where this is over 100%."
        ),
        innocent_reading=(
            "A gap through the stop closes past it with nobody making a "
            "decision at all, and the trade log records both the same way."
        ),
        action=(
            "Where the stop was on the platform rather than in your head, "
            "this is gap risk and not a choice. Where it was in your head, "
            "put it on the platform."
        ),
        details={"mean_share_of_stop": share},
    )


# --------------------------------------------------------------------------- #
# Exposure patterns
# --------------------------------------------------------------------------- #
def detect_leverage_creep(risk_history=None) -> BiasFinding:
    """Is effective leverage drifting up across the recorded snapshots?

    A run of individually reasonable sizing decisions. Each one passed; the
    sequence did not, and no single check can see a sequence.
    """
    key = "leverage_creep"
    values = _numeric(risk_history, "effective_leverage")

    if len(values) < MINIMUM_OBSERVATIONS:
        return _unmeasured(
            key, f"{len(values)} risk snapshot(s) with a leverage figure, "
                 f"below {MINIMUM_OBSERVATIONS}"
        )

    half = len(values) // 2
    before, after = float(values.iloc[:half].mean()), float(values.iloc[half:].mean())
    growth = (after - before) / before if before > 0 else 0.0
    severity = _severity(growth, 0.15, 0.75)

    return BiasFinding(
        key=key,
        title=BIASES[key],
        severity=severity,
        observations=int(len(values)),
        confidence=_confidence(int(len(values))),
        evidence=(
            f"Effective leverage averaged {before:,.2f}x over the first "
            f"{half} snapshots and {after:,.2f}x over the last "
            f"{len(values) - half} — {growth:+.0%}."
        ),
        innocent_reading=(
            "A rising balance with steady position sizes moves leverage the "
            "other way, so a rise here may be exposure growing on purpose."
        ),
        action=(
            "Set the leverage you are willing to carry as a number, and check "
            "the book against it weekly. Creep is only visible against a line."
        ),
        details={"before": before, "after": after, "growth": growth},
    )


def detect_margin_blindness(assessments=None) -> BiasFinding:
    """Do you keep opening trades when margin is already high?

    Read from the helper's own recorded verdicts: it flags margin at the
    moment of asking, and this counts how often that flag was there.
    """
    key = "margin_blindness"
    if assessments is None or getattr(assessments, "empty", True):
        return _unmeasured(key, "no helper assessments have been recorded")

    usage = _numeric(assessments, "margin_usage_pct_after_trade")
    if len(usage) < MINIMUM_OBSERVATIONS:
        return _unmeasured(
            key, f"{len(usage)} assessment(s) with a margin figure, below "
                 f"{MINIMUM_OBSERVATIONS}"
        )

    high = usage[usage > 0.4]
    rate = float(len(high) / len(usage))
    severity = _severity(rate, 0.25, 0.70)

    return BiasFinding(
        key=key,
        title=BIASES[key],
        severity=severity,
        observations=int(len(usage)),
        confidence=_confidence(int(len(usage))),
        evidence=(
            f"{rate:.0%} of {len(usage)} checked trades would have left margin "
            f"usage above 40%, averaging {usage.mean():.0%}."
        ),
        innocent_reading=(
            "Running margin high is a choice some strategies require, and the "
            "helper flags the level rather than judging it."
        ),
        action=(
            "Trading 212 closes on its own ratio without warning and picks "
            "the position itself. Decide the usage you will not exceed while "
            "nothing is at stake."
        ),
        details={"rate": rate, "mean_usage": float(usage.mean())},
    )


def model_detectors(
    *, confirmations=None, trades=None, risk_history=None, assessments=None
):
    """Every detector as ``(key, callable)``, in reading order.

    Listed here so adding a pattern is one change in one file, and so the
    controller cannot fall out of step with what exists.
    """
    return (
        ("overconfidence", lambda: detect_overconfidence(confirmations)),
        ("revenge_trading", lambda: detect_revenge_trading(confirmations)),
        ("fear_driven_underexposure",
         lambda: detect_fear_driven_underexposure(confirmations)),
        ("risk_avoidance", lambda: detect_risk_avoidance(confirmations)),
        ("loss_aversion", lambda: detect_loss_aversion(trades)),
        ("premature_exits", lambda: detect_premature_exits(trades)),
        ("late_exits", lambda: detect_late_exits(trades)),
        ("leverage_creep", lambda: detect_leverage_creep(risk_history)),
        ("margin_blindness", lambda: detect_margin_blindness(assessments)),
    )
