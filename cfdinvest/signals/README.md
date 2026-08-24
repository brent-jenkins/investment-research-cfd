# Signal engine

Produces a target position per asset per bar from preprocessing and regime
outputs. Reads from `AnalysisFrame` and `RegimeFrame`; writes to neither.

**A signal is an intention, not a fill.** No execution, cost or P&L logic
lives here.

```python
from cfdinvest.signals import SignalConfig, generate_signals

signals = generate_signals(analysis_frame, regime_frame, SignalConfig())

signals.direction        # timestamp x ticker: long / short / flat
signals.size             # [0, 1], unsigned
signals.confidence       # [0, 1]
signals.reason           # "trend_long|high_volatility @risk_on"
signals.signed_size      # what the execution layer wants
signals.to_long()        # tidy view of active signals only
```

---

## Guarantees

Asserted by the test suite, because the execution layer will consume these
panels directly and a NaN direction becomes a phantom trade:

- Every cell has a direction in `{long, short, flat}` — never NaN.
- `size` and `confidence` are finite and within bounds.
- `direction == "flat"` **iff** `size == 0`.
- Every active signal clears `min_size`.
- Every cell carries a non-empty `reason`.
- `signed_size` agrees with `direction` in sign and with `size` in magnitude.

---

## Trend

Three independent readings per asset, blended into a signed **strength** in
`[-1, +1]`:

| Component | What it sees |
| --- | --- |
| `crossover` | Fast MA above slow MA. Slow to turn, hard to fool with noise. |
| `breakout` | Price clearing the trailing Donchian band. Catches moves the averages have not reached. |
| `momentum` | Trailing return, scaled so `momentum_full_scale` counts as full conviction. |

Two details that matter:

**The Donchian bands exclude the current bar.** Without the `shift(1)`,
today's own high is part of the band it is tested against, price can never
exceed it, and no breakout ever fires. This is the classic off-by-one, and
there is a test pinning it.

**Components stay separate until the last step.** `component_agreement`
reports what fraction concurred, which is what makes `confidence` mean
something beyond "the average was large" — a strength of 0.5 from unanimous
mild readings is a different proposition from 0.5 with one component pulling
the other way. Components reading exactly zero abstain rather than diluting.

---

## Sizing

Both methods size **inversely to volatility**, targeting constant risk
rather than constant notional:

| Method | Formula | Blind spot |
| --- | --- | --- |
| `volatility_target_size` | `target / realised` | Close-to-close; misses intrabar movement |
| `atr_risk_size` | `risk / (stop_multiple × atr%)` | Needs high/low data |

`blend_sizes` takes the **minimum**, not the mean: when two risk measures
disagree about how dangerous something is, the cautious one should win.

Both floor their denominator. Without a floor, an asset in an unusually
quiet stretch gets an enormous size on the strength of a temporarily small
number — which is exactly when the estimate is least trustworthy.

Sizes are then scaled by conviction (`|strength|`, with a floor so a
weak-but-valid signal is not scaled to nothing), and optionally capped at a
gross-exposure budget.

---

## Filters

A filter can **permit, forbid, or shrink** — never create. No filter returns
a multiplier above 1.0, so no combination of conditions can lever the book
up.

| Filter | Effect |
| --- | --- |
| **Regime** | Longs only in `risk_on` / `expansion`. Shorts in `crisis` or macro `contraction`. `high` volatility halves size. |
| **Correlation** | `breakdown` suppresses size (default 0.0 for longs). Optional diversification haircut for crowded names. |
| **Macro** | Rising inflation penalises non-hedges. `contraction` blocks equity longs. Loosening labour applies a broad haircut. |

Outcomes compose commutatively — permissions AND, multipliers multiply — so
the result does not depend on filter order.

### ⚠️ Why multipliers are direction-specific

The first working build produced **zero shorts, ever**. The cause is
structural and worth recording:

> The regime layer defines `crisis` as *requiring* a correlation breakdown.
> `crisis` is the regime that permits shorts. The correlation filter zeroes
> size on breakdown. So the only regime allowing shorts was exactly the
> regime forcing size to zero — the two requirements cancelled out.

The fix is `long_multiplier` and `short_multiplier` as separate panels.
`breakdown_applies_to_shorts` defaults to **`False`**, which keeps shorts
reachable. Set it `True` to suppress both directions, accepting a long-only
engine.

**The inflation tilt never levers up.** "Prefer commodities and FX" is
implemented by *penalising everything else*, not by boosting the hedges.
Same relative tilt, no increase in gross exposure.

---

## Direction resolution — precedence rules

The continuous inputs are reduced to four discrete facts, giving
`4 × 2 × 2 × 2 = 32` combinations, each mapping to exactly one direction.
`direction_decision_table()` enumerates them; `verify_direction_totality()`
checks the map.

| # | Rule | Direction | Condition |
| --- | --- | --- | --- |
| 1 | `no_data` | flat | No trend reading — warm-up or a gap |
| 2 | `below_entry_threshold` | flat | Strength inside the dead band |
| 3 | `blocked_long` | flat | Long wanted, filters forbid |
| 4 | `blocked_short` | flat | Short wanted, filters forbid |
| 5 | `below_min_size` | flat | Permitted, but too small to be worth the spread |
| 6 | `trend_long` | **long** | Up, permitted, size clears |
| 7 | `trend_short` | **short** | Down, permitted, size clears |
| 8 | `default_flat` | flat | Catch-all; unreachable, makes the map total |

**Blocks are checked before the size rule** so the reason names the actual
cause rather than the size that followed from it. Only 4 of the 32
combinations produce a position — the engine is flat by default and by
design.

---

## Confidence

Three factors, multiplied:

```
confidence = |strength| × component_agreement × size_multiplier
```

Conviction, concurrence, and how much the environment supported acting.
Flat cells score zero — there is no confidence in a position not taken.

## Reasons

Format: `rule[|flag...] @regime`, e.g.

```
trend_long @risk_on
blocked_long|high_volatility @risk_off
trend_short|high_volatility|correlation_breakdown @crisis
below_entry_threshold @neutral
```

Only *informative* flags appear. `regime_blocks_long` is deliberately
omitted: the rule name already says `blocked_long`, so repeating it adds
length without information. It remains in `diagnostics` for inspection.

`signals.reason_counts()` shows where signals go to die, which is usually
the fastest way to find a mis-set threshold.

---

## Configuration

```python
SignalConfig(
    fast_ma=20, slow_ma=100, breakout_window=55,
    momentum_full_scale=0.10,
    entry_threshold=0.3,              # dead band around zero

    target_volatility=0.15,
    risk_fraction=0.01, stop_multiple=2.0,
    min_size=0.02, max_size=1.0,
    conviction_floor=0.25,
    max_gross_exposure=None,          # portfolio-level cap; off by default

    long_regimes=frozenset({"risk_on", "expansion"}),
    short_regimes=frozenset({"crisis"}),
    high_volatility_multiplier=0.5,
    trade_when_regime_unknown=False,  # warm-up trades nothing

    breakdown_multiplier=0.0,
    breakdown_applies_to_shorts=False,   # see the warning above
    use_diversification=False,

    inflation_penalty=0.5,
    loosening_labor_multiplier=0.75,
    block_equity_longs_in_contraction=True,
)
```

---

## Testing

```bash
pytest tests/test_signals_*.py     # 137 tests, no network
```

| File | Covers |
| --- | --- |
| `test_signals_combined.py` | Totality over all 32 combinations, precedence, confidence, reasons |
| `test_signals_components.py` | Trend, sizing and filters against constructed inputs |
| `test_signals_frame.py` | Invariants, filter behaviour end to end, no-look-ahead |

Every window is trailing, and the no-look-ahead property is asserted
directly: generating signals from a truncated prefix produces identical
directions and sizes over that prefix.

---

## Known limitations

**`long_regimes` is restrictive.** Longs are barred in `neutral`, which is
87 of the regime layer's 384 combinations. That follows the specification,
but it means the engine sits out a lot of ordinary tape. Widen
`long_regimes` if that is not the intent.

**No position persistence.** Each bar is classified independently, so a
signal can flip flat and back on threshold noise. Hysteresis (separate entry
and exit thresholds) belongs here eventually, but adding it would change
what a "signal" means, so it is left for the execution layer to smooth or
for a later, explicit decision.
