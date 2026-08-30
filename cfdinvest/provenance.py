"""What justifies each tuned parameter, and whether that evidence still stands.

The failure this exists to prevent
----------------------------------
On 2026-08-28 this project measured that its cost model undercharged spread
by 1.85x at the median, wrote that down, and went on charging the old
number for two more days. Nothing connected the finding to the code. The
same shape appeared three more times in one weekend: a fill path believed
to use the broker's prices and never tested, a cost gate whose docstring
records a measurement nobody could re-run, and a breakout lookback whose
docstring claims +7pp where the engine now measures -3.9pp.

The project already had strong discipline about *measuring* and about
*retracting*. It had none that made a conclusion **binding**, so prose and
behaviour drifted for weeks with nothing to catch it.

This is that binding. Every performance parameter names the measurement
behind it and the conditions that measurement was taken under. When the
conditions change -- a different feed, a different cost model -- every
justification taken under the old ones goes stale automatically, and
``tests/test_provenance.py`` fails the build unless the staleness is
declared. Debt becomes visible and enumerable instead of invisible and
unbounded.

Why conditions rather than a date
---------------------------------
A date says when somebody looked. It does not say whether what they looked
at still resembles what runs. The two conditions that actually invalidate a
strategy measurement here are the **decision feed** and the **cost model**,
because both change every number rather than one:

* switching yfinance to Capital changed which bars a decision is taken on;
* charging measured spreads rather than a flat 5bp moved the eight-name
  baseline from 64.7% to 27.0% CAGR.

A parameter chosen by comparing arms that all undercharged the largest cost
line by half was not chosen on the evidence its docstring claims.

What this deliberately does not do
----------------------------------
It does not re-run the measurements. A full sweep is minutes to hours and a
test suite has to stay usable. It asserts that every tuned parameter *has*
a re-runnable justification, that the recorded value still matches the
configured one, and that any justification taken under superseded
conditions is declared rather than forgotten.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "CURRENT_FEED",
    "CURRENT_COST_MODEL",
    "Justification",
    "JUSTIFICATIONS",
    "PENDING_REMEASUREMENT",
    "stale",
    "current",
]

#: The intraday bars decisions are taken on. Changing this invalidates every
#: strategy measurement, because it changes what a lookback sees.
CURRENT_FEED = "capital_1h"

#: How trading cost is charged. ``"measured"`` reads the broker's own quoted
#: spread per bar; ``"flat_5bp"`` is the constant every run before
#: 2026-08-30 used, which undercharged by 2.26x on the eight-name book.
CURRENT_COST_MODEL = "measured"


@dataclass(frozen=True)
class Justification:
    """The evidence behind one tuned parameter.

    ``source`` must name something re-runnable. "It was measured" is not a
    justification anybody can check; a command is.
    """

    #: Dotted path into BacktestConfig, as ``analytics.apply_overrides``
    #: addresses it. Verified to resolve, so a renamed field fails the build
    #: rather than leaving a justification pointing at nothing.
    path: str
    #: The value this evidence supports. Checked against what is actually
    #: configured, so drifting the config without revisiting the evidence
    #: fails.
    value: object
    #: Decision feed the measurement was taken on.
    feed: str
    #: Cost model the measurement was taken under.
    cost_model: str
    #: Something that can be run again to reproduce the figure.
    source: str
    #: What it showed, in one line. Kept short deliberately: the detail
    #: belongs in the parameter's own docstring, and two long copies drift.
    figure: str


#: Every parameter whose value was chosen by measurement rather than by
#: definition. A knob that is merely a safety bound (``max_open_positions``
#: unset, ``max_leverage`` as a cap) belongs here too if a measurement moved
#: it, and not if it was simply picked as a limit.
JUSTIFICATIONS: tuple[Justification, ...] = (
    Justification(
        path="execution.breakout_lookback",
        value=12,
        feed="1h",
        cost_model="flat_5bp",
        source="config/config.toml comment table; original sweep not retained",
        figure="79.5/80.5/80.2/80.3% at 6/12/24/48 bars against 73.6% for no "
               "gate -- contradicted on 2026-08-30 by a Capital sweep giving "
               "64.7% at 12 against 68.6% at 0, opposite sign",
    ),
    Justification(
        path="execution.decayed_position_multiplier",
        value=0.5,
        feed="1h",
        cost_model="flat_5bp",
        source="scratch sweep, not retained",
        figure="74.4% -> 80.3% with drawdown -34.4% -> -26.1%, measured "
               "through the engine after an initial harness result of 77.4% "
               "was found to express a different rule",
    ),
    Justification(
        path="execution.thresholds.entry_confidence",
        value=0.35,
        feed="1h",
        cost_model="flat_5bp",
        source="scratch sweep, not retained",
        figure="raising the gate is +1.6pp at 0.40 and -12pp at 0.45 on the "
               "screened path; the middle confidence buckets are "
               "indistinguishable so the gate cannot separate them",
    ),
    Justification(
        path="signal.high_volatility_multiplier",
        value=0.5,
        feed="1h",
        cost_model="flat_5bp",
        source="scratch sweep, not retained",
        figure="raising above 0.5 worsens drawdown monotonically, -34.4% to "
               "-41.9%, and halves second-half performance",
    ),
    Justification(
        path="execution.time_stop_days",
        value=0.29,
        feed="1h",
        cost_model="flat_5bp",
        source="scratch sweep, not retained",
        figure="longer holds monotonically worse; edge decay rather than "
               "carry, since the same result appears with financing off",
    ),
    Justification(
        path="execution.earnings_blackout_days",
        value=1,
        feed="1h",
        cost_model="flat_5bp",
        source="scratch sweep, not retained",
        figure="chosen on risk grounds explicitly and not on return, which "
               "reverses between halves of the span",
    ),
    Justification(
        path="execution.stops.enabled",
        value=False,
        feed="1h",
        cost_model="flat_5bp",
        source="scratch sweep, not retained",
        figure="stops of every asymmetry are worse than none, monotonically "
               "tighter-is-worse; they convert prospective long winners into "
               "short losers, and 20% of trips produce all of the profit",
    ),
    Justification(
        path="execution.max_leverage",
        value=3.5,
        feed=CURRENT_FEED,
        cost_model=CURRENT_COST_MODEL,
        source="run_parameter_sweep on execution.max_leverage over [3.5, 5.0, "
               "7.0], full Capital history, eight names, measured spreads",
        figure="INCONCLUSIVE. 3.5 -> 27.0%; 5.0 -> 31.4%; 7.0 -> 34.1%, all "
               "on the pre-FX-fix baseline. The 4.4pp between 3.5 and 5.0 is "
               "inside the 8.0pp noise floor, so this does not discriminate "
               "and the value is a risk choice rather than a finding. What "
               "the run did establish: the cap is reached but rarely "
               "binding, mean gross leverage being 0.708 against it with the "
               "book flat on 50.6% of bars -- the constraint is not the cap. "
               "Capital fixes stock CFDs at 5x, so 3.5 leaves headroom "
               "unused deliberately rather than by venue limit",
    ),
    Justification(
        path="universe.size_curve",
        # String keys: TOML table keys are strings, and the registry
        # compares against what is configured rather than what a reader
        # would type.
        value={"6": 62.5, "8": 76.9, "10": 82.3, "12": 73.8, "16": 50.4,
               "20": 42.9, "30": -6.3},
        feed=CURRENT_FEED,
        cost_model=CURRENT_COST_MODEL,
        source="scratchpad/screen_walk.py then universe_sweep.py: the screen "
               "replayed point in time over 917 days and 100 candidates, its "
               "daily selection expressed as an entry_gate, seven target "
               "sizes through the engine with measured spreads",
        figure="6 -> 62.5%/-19.1%; 8 -> 76.9%/-30.4%; 10 -> 82.3%/-41.3%; "
               "12 -> 73.8%/-49.4%; 16 -> 50.4%; 20 -> 42.9%; 30 -> -6.3% "
               "with cost at 105.9% of gross. 8/10/12 span 8.5pp and are not "
               "separable at an 8.0pp floor, so this is a band not a peak; "
               "resolve_target picks 10 because size_preference is 'largest'. "
               "Measured on the currently tradeable list, which is the only "
               "universe there is: a delisted name cannot be traded, so a "
               "2023-vintage candidate list would not be a better estimate "
               "but an unusable one. Owner's standing position, and not an "
               "unexamined omission -- whether forward results match is what "
               "the demo account is for",
    ),
    Justification(
        path="execution.thresholds.entry_size",
        value=0.02,
        feed=CURRENT_FEED,
        cost_model=CURRENT_COST_MODEL,
        source="scratchpad/entrysize.py: seven entry/exit bands through the "
               "engine, screen-gated at target 10, measured spreads, plus a "
               "uniform-leverage control and an entry/exit decomposition",
        figure="0.02/0.01 -> 130.5% at -40.3%, Calmar 3.24, flat 6.5% of "
               "bars; 0.10/0.05 -> 82.3% at -41.3%, Calmar 1.99, flat 52.5%. "
               "+48.2pp against an 8.0pp floor. CONTROL: levering the old "
               "book by the same 1.62x returns 101.8% at -43.4%, so under "
               "half the gain is exposure and the rest is holding 4.12 names "
               "instead of 1.45. 0.02 is signal.min_size, so this removes "
               "the floor rather than lowering it, and because exit must not "
               "exceed entry it retires the size-based exit too -- exit "
               "alone is worth +11.5pp, the pair is not separable here",
    ),
    Justification(
        path="execution.thresholds.exit_size",
        value=0.01,
        feed=CURRENT_FEED,
        cost_model=CURRENT_COST_MODEL,
        source="scratchpad/entrysize.py, same sweep -- the band moves as a "
               "pair because the engine refuses exit_size > entry_size",
        figure="loosening the exit alone (0.10/0.01) returns 93.8% at -43.1% "
               "against 82.3% at -41.3%, so +11.5pp of the pair's +48.2pp. "
               "At or below signal.min_size the size-based exit cannot fire, "
               "so positions leave on a direction flip or the time stop",
    ),
    Justification(
        path="execution.rebalance_threshold",
        value=3.0,
        feed="1h",
        cost_model="flat_5bp",
        source="scratch sweep, not retained",
        figure="3.0 disables drift rebalancing; 0.5 beat it in 9 of 10 "
               "out-of-sample windows, so this is a known-open question "
               "rather than a settled choice",
    ),
)

#: Justifications known to predate the current conditions, listed so the
#: build stays green while the debt stays visible. This is the re-measurement
#: queue, in the order worth doing: undercharging spread specifically
#: subsidised trading more often, so the parameters that most affect trade
#: count are the ones most likely to have been chosen wrongly.
#:
#: Remove a path from here when its justification is re-recorded under the
#: current feed and cost model. The test fails if a path listed here is no
#: longer stale, so this cannot rot with entries that have been fixed.
PENDING_REMEASUREMENT: tuple[str, ...] = (
    "execution.breakout_lookback",
    "execution.decayed_position_multiplier",
    "execution.thresholds.entry_confidence",
    "execution.time_stop_days",
    "execution.rebalance_threshold",
    "execution.stops.enabled",
    "signal.high_volatility_multiplier",
    "execution.earnings_blackout_days",
)


def stale(
    justifications: tuple[Justification, ...] | None = None,
) -> tuple[Justification, ...]:
    """Those whose evidence predates the current feed or cost model."""
    wanted = JUSTIFICATIONS if justifications is None else justifications
    return tuple(
        j for j in wanted
        if j.feed != CURRENT_FEED or j.cost_model != CURRENT_COST_MODEL
    )


def current(
    justifications: tuple[Justification, ...] | None = None,
) -> tuple[Justification, ...]:
    """Those measured under the conditions that actually run."""
    wanted = JUSTIFICATIONS if justifications is None else justifications
    return tuple(j for j in wanted if j not in stale(wanted))
