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
        path="universe.max_per_sector",
        value=8,
        feed=CURRENT_FEED,
        cost_model=CURRENT_COST_MODEL,
        source="scratchpad/battery.py and battery2.py: the cap swept jointly "
               "with the target, because the cap decides which targets are "
               "REACHABLE at all -- 3 fills 26 of the candidates, 5 fills 38, "
               "8 fills 54, 12 fills 70. Sweeping the target against a fixed "
               "cap is what produced tables describing books the screen "
               "cannot select",
        figure="at a target of 50, +48.7pp over a leverage-matched control on "
               "the shipped 30-name universe, maxDD 6.6%, positive in both "
               "halves and 4 of 4 years. Was None, which does NOT mean no cap "
               "-- it left ScreenConfig's default of 5 in force, and that "
               "ambiguity let a reachability check read 100 names as "
               "selectable and wave through a target of 70 that filled to 38, "
               "putting the account on 8 names mid-session. Explicit now. The "
               "looser arms measure better still (cap 12 / t70 at +106.0pp, "
               "uncapped / t100 at +300.3pp) and are NOT taken: at target 100 "
               "from a 100-name pool the screen selects everything and stops "
               "being a selection, and that arm is the only one that "
               "concentrates -- 33% technology against 16% here",
    ),
    Justification(
        path="universe.size_curve",
        value={"8": 57.0, "12": 79.9, "16": 94.3, "20": 100.8, "25": 120.2,
               "30": 137.3, "40": 167.4, "50": 191.0},
        feed=CURRENT_FEED,
        cost_model=CURRENT_COST_MODEL,
        source="scratchpad/battery.py: the sector cap and the screen target "
               "swept JOINTLY, with each universe built through the screen's "
               "own select_within_sector_cap rather than by taking the "
               "ranking's first N. This table is the max_per_sector = 8 row "
               "of that grid; the curve is per-cap and stops at 50 because "
               "8 across 9 sectors fills 54 of the 100 candidates",
        figure="57.0% at 8 names rising to 191.0% at 50, monotone. Adopted "
               "at 50 with the cap at 8, on a leverage-matched control "
               "against the shipped universe -- the test that retired "
               "rebalance_threshold -- worth +48.7pp, with maxDD 6.6% (the "
               "lowest of any arm), positive in both halves and in 4 of 4 "
               "calendar years, and above the shipped arm's MEDIAN on 99.9% "
               "of 4,000 bootstrap paths. It does not concentrate the book: "
               "largest sector 16% across 8 sectors, against 17% across 7 at "
               "cap 5 / t30. RETRACTS both earlier tables (40.5/48.4/54.5/"
               "70.8/93.1/103.8/110.7/139.4/161.7/215.7/209.7, then 57.0/"
               "69.8/82.5/104.9/130.5/159.2/179.7/240.9/297.6/401.3/490.4). "
               "Neither was stale -- both were measuring max_per_sector = "
               "None, because taking the ranking's first N is what an "
               "uncapped screen does. The second reproduces the uncapped row "
               "of the new grid to the decimal, which is what identified the "
               "error. On the shipped cap of 5 the harness's top thirty and "
               "the screen's real thirty shared EIGHT names, so the old "
               "figures never described this account at any size. NOT "
               "VALIDATED: the bootstrap puts P(drawdown>25%) at 0.0% over "
               "4,000 paths, which a resampling cannot support -- it can "
               "only redraw risk its sample contains, and a correlated shock "
               "occurs about once in 3.65 years",
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
