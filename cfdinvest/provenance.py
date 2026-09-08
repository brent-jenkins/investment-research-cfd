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
        feed=CURRENT_FEED,
        cost_model=CURRENT_COST_MODEL,
        source="scratchpad/breakout.py: seven lookbacks through the engine as "
               "an ExecutionConfig field, screen-gated at target 10, measured "
               "spreads, entry_size at the signal floor",
        figure="0 (off) -> 120.6%/-43.0%; 6 -> 127.3%; 12 -> 130.5%/-40.3%; "
               "24 -> 133.4%; 48 -> 134.6%/-39.9%; 96 -> 129.9%. The gate is "
               "worth +9.9pp against no gate, above the 8.0pp floor, and it "
               "lowers drawdown and cost share too. 12 kept: the peak is 48 "
               "but 12-48 spans 4.1pp and 6-96 all sit within the floor of "
               "it, so 12 is mid-plateau rather than at an edge. RESOLVES the "
               "earlier contradiction -- the -3.9pp reading was taken on the "
               "fixed eight-name book with the size floor still in place, and "
               "this gate only earns its place once the screen chooses from a "
               "hundred names and the floor is not already discarding two "
               "thirds of the signals. Both readings were right for their "
               "configuration; neither generalised",
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
        value=0.75,
        feed=CURRENT_FEED,
        cost_model=CURRENT_COST_MODEL,
        source="scratchpad/costsearch and diligence: the gate swept through "
               "the CORRECTED engine, screen-gated, measured spreads, with "
               "cost/gross as the criterion because every arm at the old "
               "gate loses and CAGR would only rank them by speed",
        figure="0.35 -> 192.2% cost/gross and -47.5%; 0.55 -> 98.5%/+0.7%; "
               "0.65 -> 63.4%/+21.0%; 0.75 -> 37.2%/+48.4%; 0.85 -> +33.9%; "
               "0.95 -> +18.4%. An INTERIOR optimum: the curve turns over "
               "after 0.75 rather than running to the edge of the sweep. "
               "Positive in both halves (+51.9%/+45.0%) and in four years of "
               "four (+87/+16/+83/+23), against a 9.3pp floor re-derived for "
               "this book by varying the screen's selection lag alone -- the "
               "old 8.0pp floor belonged to a book six times larger. Gross "
               "P&L RISES from +98k to +512k across the sweep, so this "
               "removes trades that lost money gross rather than merely "
               "saving spread. SUPERSEDES the 2026-08-30 justification for "
               "0.35, every figure of which was measured on an engine whose "
               "time stop decided on a bar's close and filled at its open",
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
        value={"8": 57.0, "10": 69.8, "12": 82.5, "16": 104.9, "20": 130.5,
               "25": 159.2, "30": 179.7, "40": 240.9, "50": 297.6,
               "70": 401.3, "100": 490.4},
        feed=CURRENT_FEED,
        cost_model=CURRENT_COST_MODEL,
        source="**RETRACTED 2026-09-08 -- MEASURED ON A UNIVERSE THE "
               "SCREEN CANNOT SELECT.** The sweep took the raw ranking's "
               "first N; the screen walks the ranking skipping names whose "
               "sector is full and reaches further down. On the day this was "
               "found the harness's top thirty and the screen's real thirty "
               "shared EIGHT names -- twenty-two technology names against a "
               "book across nine sectors. The error is at every point, 25 "
               "included, so this is not a stale figure but one that never "
               "described this account. The values remain configured because "
               "resolve_target reads them live and emptying the table would "
               "drop the universe mid-session; a replacement is being "
               "measured through select_within_sector_cap, which both the "
               "screen and any harness must now call. SUPERSEDED SOURCE: "
               "scratchpad/curve.py: the screen target swept through the "
               "corrected engine on the DAY-TRADING configuration -- "
               "entry_confidence 0.75, flatten at the bell, no opens in the "
               "last hour, entry_recheck on -- screen-gated, measured "
               "spreads, target equal to the number held so eligible equals "
               "held and the rotation failure does not apply. The baseline "
               "arm reproduced the recorded 159.2% to the decimal before any "
               "other arm was read",
        figure="MONOTONE ACROSS THE WHOLE RANGE with no peak: 57.0% at 8 "
               "names rising to 490.4% at 100. Drawdown does not pay for it "
               "-- 7.6% at 8, 6.6% at 40 to 70, 7.6% at 100, no trend -- and "
               "every size is positive in every year. What rises is the cost "
               "share, 20.8% of gross to 34.7%, and mean leverage, 0.54x to "
               "1.31x. SUPERSEDES the 2026-09-04 curve (40.5 / 48.4 / 54.5 / "
               "70.8 / 93.1 / 103.8 / 110.7 / 139.4 / 161.7 / 215.7 / 209.7), "
               "which peaked at 70 and turned over at 100. That curve was "
               "correct when measured and described the wrong book: its 103.8 "
               "at 25 names is exactly what the same harness reports for the "
               "CARRY-OVERNIGHT arm, and the account has flattened at the "
               "bell since 2026-09-06. So the table choosing the universe "
               "size was fitted to the book the account stopped running two "
               "days earlier. THE SHAPE CHANGE IS THE CONSEQUENCE: 'largest "
               "within the floor' now selects 100 rather than 70, so nothing "
               "in the return curve argues for a stop and universe.max_target "
               "is the only thing that does. SHIPPED AT 25 regardless, "
               "pending the universe question rather than because the "
               "balance cannot fund it. The engine now models the broker's "
               "minimum deal size (ExecutionConfig.min_deal_sizes, the 63 "
               "increments recorded on the account) and the constraint does "
               "not bind: the first six months at target 100 return 120.3% "
               "on a 1,000 balance against 120.2% with it switched off, same "
               "540 active bars, median three positions of about 447 each "
               "against a dearest ticket of 137. RETRACTED the same day: "
               "'at 100 names a slot is worth about 62 so the dear names "
               "could not be opened' divided exposure by the TARGET, which "
               "is how many names the screen may CHOOSE FROM rather than how "
               "many are held -- and the book holds a median of THREE at "
               "target 100, flat on 42.6% of bars",
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
        path="execution.confidence_sizing_strength",
        value=0.75,
        feed=CURRENT_FEED,
        cost_model=CURRENT_COST_MODEL,
        source="scratchpad/strength.py, strength2.py and validate.py: six "
               "strengths through the engine screen-gated at target 10, a "
               "control matched on realised leverage, an independent "
               "split-half, per-year and per-quarter cuts, a 4,000-path "
               "block bootstrap of both arms, and four universe sizes",
        figure="RAISED 0.5 -> 0.75 on 2026-09-01. Sweep: 0.0 -> 128.8%; "
               "0.25 -> 195.4%; 0.5 -> 257.6%/-41.9%; 0.75 -> 307.6%/-44.1%; "
               "1.0 -> 325.2%/-47.2%; 1.5 -> 346.5%/-49.2%. The old reason "
               "for stopping at 0.5 -- that stronger settings buy return "
               "with risk rather than earning it -- is REFUTED, not "
               "superseded: no leverage control had been run above 0.5. "
               "CONTROL: exposure raised 1.058x at strength 0.5 returns "
               "272.0%, so of 0.75's +50pp only 14.3pp is exposure and "
               "+35.8pp belongs to the rule. HALVES: +57.9pp then +11.3pp, "
               "both clear of the 8pp floor. YEARS: ahead 3 of 3 (+34.5, "
               "+32.9, +15.9pp); QUARTERS 10 of 14. BOOTSTRAP (4,000 paths, "
               "block 20): beats 0.5's median return on 63.5% of paths and "
               "its p10 improves 1870% -> 2887%, while drawdown p50 goes "
               "45.0% -> 46.4% with 56.3% of paths worse -- the gain is not "
               "free. UNIVERSE: +47.4/+50.0/+32.9/+42.7pp at targets "
               "8/10/12/16. Not taken past 0.75 although the curve keeps "
               "rising: only 0.75 has been through the controls. Calmar "
               "6.15 -> 6.98. Absolute figures are not a forecast",
    ),
    Justification(
        path="execution.confidence_sizing_floor",
        value=0.25,
        feed=CURRENT_FEED,
        cost_model=CURRENT_COST_MODEL,
        source="chosen, not swept -- a bound rather than a tuned parameter",
        figure="NOT MEASURED against alternatives. It exists so a signal "
               "that cleared the entry gate is trimmed rather than erased; "
               "sizing one to zero would be a second gate wearing a "
               "multiplier, and gates are measured separately. Recorded as "
               "a choice so nobody later reads it as a finding",
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
    "execution.decayed_position_multiplier",
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
