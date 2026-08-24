# Regime classification

Labels each timestamp with a market and macro regime, using only
`AnalysisFrame` outputs. Reads from preprocessing and writes nothing back.

**A regime is a description of conditions, not an instruction to trade.** No
signal logic lives here.

```python
from cfdinvest.preprocessing import analysis_frame_from_repository
from cfdinvest.regimes import RegimeConfig, classify_regimes, attach_regimes

analysis = analysis_frame_from_repository(repo)
regimes = classify_regimes(analysis, RegimeConfig())

regimes.to_frame()          # the five headline labels
regimes.summary()           # bars and share per label
regimes.transitions()       # contiguous runs, to spot flickering
analysis = attach_regimes(analysis, regimes)   # fills the placeholder slot
```

---

## The five components

| Component | Labels | Built from |
| --- | --- | --- |
| `volatility_regime` | `low` / `medium` / `high` | realised volatility + ATR |
| `trend_regime` | `uptrend` / `sideways` / `downtrend` | moving averages + rolling returns |
| `correlation_regime` | `diversified` / `normal` / `breakdown` | rolling correlation matrix |
| `macro_regime` | `expansion` / `overheating` / `slowdown` / `stagflation` / `contraction` | forward-filled macro panel |
| `combined_regime` | `crisis` / `stagflation` / `risk_off` / `neutral` / `risk_on` / `expansion` | the four above |

Every component can also be **`unknown`**, which means the inputs were absent
or still warming up. That is never a guess — a classifier with no data says
so rather than defaulting to something plausible.

---

## Volatility

Blends both measures, because they see different things. Realised volatility
is close-to-close and misses everything inside the bar; ATR uses the high/low
range and the overnight gap, so it catches the violent session that closed
flat — the one that stops out a CFD position while realised stays calm.

ATR is divided by close first (it is in price units, so a 40-point range on a
4000-level index and on a 100-dollar stock are not comparable), then both are
converted to a **trailing percentile rank** and averaged.

Ranking against the series' own recent history rather than a fixed threshold
means "high" adapts: a market that is *always* at 60% annualised volatility
reads `medium`, not permanently `high`. And the rank is strictly trailing —
a full-sample quantile would rank today against future data and leak it into
every label.

## Trend

Two independent pieces of evidence per asset, summed into a score in
`[-2, +2]`:

- **Moving-average structure** — price above the slow MA *and* fast above
  slow. Slow to react, hard to fool with noise.
- **Rolling-return momentum** — with a dead band, so a +0.01% move does not
  register as direction.

The market-level label is then **breadth**: the share trending up minus the
share trending down. A rally carried by two names out of fifty is not an
uptrend, and breadth is what says so. Assets labelled `unknown` are excluded
from the denominator rather than diluting it.

## Correlation

Two distinct failure modes, and they are not the same thing:

1. **Broad correlation spike** — everything moves together, so position count
   stops buying diversification. Preprocessing already measures and flags
   this; this module consumes those flags rather than recomputing them.

2. **Equity-bond decoupling** — bonds normally hedge equities, which is the
   entire premise of holding TLT against SPY. When that correlation turns
   *positive*, both legs fall at once and the hedge is not merely weak but
   inverted. 2022 is the worked example: broad correlation barely moved while
   the hedge that mattered stopped working.

Only the cross-group block of each matrix is averaged, never the within-group
pairs, which would drown the signal in the always-high equity-equity
correlations. Either condition forces `breakdown`.

Equity and bond membership comes from `RegimeConfig.equity_tickers` /
`bond_tickers`; tickers absent from the universe are ignored, so a lake
without bonds simply reports the hedge as `unknown`.

## Macro

Three readings from the publication-aware forward-filled panel:

| Reading | Labels | Rule |
| --- | --- | --- |
| growth | `expansion` / `slowdown` / `contraction` | YoY real GDP, and whether it is decelerating |
| inflation | `rising` / `stable` / `falling` | change *in* the YoY CPI rate |
| labour | `tightening` / `loosening` | direction of smoothed unemployment |

Two deliberate choices:

**Growth uses level *and* momentum.** `slowdown` means still growing but
decelerating — 2% growth on the way down is a different world from 2% on the
way up, and a level-only rule cannot tell them apart.

**Inflation judges the change in the rate, not the rate.** 3% heading to 5%
and 3% heading to 1% call for opposite positioning.

The headline `macro_regime` is the growth-inflation quadrant:

| growth | inflation | macro_regime |
| --- | --- | --- |
| expansion | rising | `overheating` |
| expansion | stable / falling | `expansion` |
| slowdown | rising | `stagflation` |
| slowdown | stable / falling | `slowdown` |
| contraction | rising | `stagflation` |
| contraction | stable / falling | `contraction` |

Labour is carried alongside rather than folded in: it lags, and folding a
lagging series into the headline would smear the transitions.

Series names are resolved by preference list (`GDPC1` → `GDP` → `INDPRO`,
etc.), so a partial lake still produces readings, and a missing series makes
exactly one component `unknown`.

---

## Combined regime — precedence rules

The mapping is a **precedence list of predicates**, evaluated in order, with
a catch-all last. That shape buys three properties:

- **Deterministic** — first match wins, so overlapping rules have a defined
  outcome instead of depending on statement order inside a function.
- **Total** — every one of the `4 × 4 × 4 × 6 = 384` combinations maps to
  exactly one label. `verify_totality()` checks it; the tests assert it.
- **Inspectable** — `decision_table()` enumerates the whole domain, so the
  table *is* the specification. You can read it, diff it, and see which rule
  fired for any combination.

| # | Label | Rule | Condition | Combinations |
| --- | --- | --- | --- | --- |
| 1 | `unknown` | `no_evidence` | all four components unknown | 1 |
| 2 | `crisis` | `crisis` | volatility `high` **and** correlation `breakdown` **and** trend `downtrend` | 6 |
| 3 | `stagflation` | `stagflation` | macro is `stagflation` | 63 |
| 4 | `risk_off` | `risk_off` | volatility `high` **or** trend `downtrend` **or** correlation `breakdown` **or** macro `contraction` | 207 |
| 5 | `expansion` | `expansion` | macro `expansion`/`overheating` **and** trend `uptrend` **and** volatility `low`/`medium` | 12 |
| 6 | `risk_on` | `risk_on` | trend `uptrend` **and** volatility `low`/`medium` **and** correlation not `breakdown` | 8 |
| 7 | `neutral` | `default_neutral` | everything else | 87 |

### Why this order

**Crisis before risk_off.** Both match a violent, falling, correlated market;
crisis is the more specific reading and wins. Weaken any one of its three
conditions and it demotes to `risk_off`.

**Stagflation before the market rules.** Weakening growth with accelerating
inflation is hostile to equities and bonds *simultaneously* — there is no
obvious defensive asset — so a calm tape does not make the backdrop benign.

**`risk_off` is deliberately the widest label** (207 of 384 combinations). Any
single stress signal is enough. The asymmetry is the point: one indicator
turning hostile should reduce risk, whereas adding risk should require broad
agreement. `risk_on` and `expansion` together cover only 20 combinations.

**Expansion before risk_on.** Both match a calm advance; the macro-confirmed
label is the stronger claim.

**`unknown` is a label, not a fallback to `neutral`.** Calling the warm-up
period "neutral" would assert a calm market on the strength of no data.

---

## Ambiguity flags

158 of the 384 combinations are flagged as **defensible but contestable**.
The flag annotates; it never changes the label. Each is available per-bar in
`components["ambiguous"]` and `components["ambiguity_reason"]`.

| Flag | Combinations | Why it is contestable |
| --- | --- | --- |
| `volatile_advance` | 24 | High volatility in an *uptrend* is labelled `risk_off`, but a melt-up and a panic look identical to a measure that ignores sign |
| `recovery_rally` | 16 | Macro `contraction` with an uptrend is labelled `risk_off` — but markets lead the economy out of recessions |
| `late_cycle_calm` | 16 | `overheating` with low volatility is labelled `expansion`, and is also the classic late-cycle setup |
| `stagflation_with_a_strong_tape` | 16 | The macro rule overrides a rising market, which may be discounting an inflation peak the data has not reached |
| `correlation_only_stress` | 18 | Diversification has failed while volatility is low and the tape is not falling; `risk_off` rests on one component |
| `thin_evidence` | 86 | Two or three components are `unknown`, so the label rests on one or two readings |

These are exactly the places where your judgement may differ from the
default. Overriding one is a config change or a rule edit, not a rewrite.

---

## Configuration

```python
RegimeConfig(
    volatility_window=252,          # trailing window for the percentile rank
    volatility_low_threshold=1/3,
    volatility_high_threshold=2/3,
    realized_weight=0.5, atr_weight=0.5,

    fast_ma=50, slow_ma=200,
    momentum_threshold=0.02,
    trend_score_threshold=1.0,      # raise to 2.0 to demand both measures agree
    breadth_threshold=0.2,          # strictly exceeded for a directional call

    diversified_threshold=0.3,
    hedging_threshold=-0.1, co_moving_threshold=0.2,
    equity_tickers=..., bond_tickers=...,

    gdp_series=("GDPC1", "GDP", "INDPRO"),
    cpi_series=("CPIAUCSL", "CPILFESL", "PCEPI"),
    unemployment_series=("UNRATE", "U6RATE"),
    macro_lookback=None,            # defaults to the frame's periods_per_year
    inflation_threshold=0.005,      # 0.5pp change in the annual rate
)
```

---

## Testing

```bash
pytest tests/test_regimes_*.py      # 160 tests, no network
```

| File | Covers |
| --- | --- |
| `test_regimes_combined.py` | Totality over all 384 combinations, precedence, ambiguity flags |
| `test_regimes_components.py` | Each classifier against constructed inputs with known answers |
| `test_regimes_frame.py` | Assembly, engineered crisis/stagflation scenarios, no-look-ahead |

The combined tests enumerate the entire domain rather than sampling it,
because the correctness claim is about the whole mapping: every rule is
reachable, every declared label is produced, and the vectorised lookup path
agrees with the scalar predicate path on all 384 keys.

---

## Look-ahead

Every window is trailing. `test_regimes_frame.py` asserts the end-to-end
property directly: classifying a truncated prefix produces the same labels
over that prefix as classifying the full history.

Macro inputs arrive already publication-aligned from preprocessing, so a
value only exists here from the date it became public. This layer never
reaches back to an observation date.
