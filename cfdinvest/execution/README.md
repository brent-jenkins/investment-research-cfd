# Execution simulator

Converts a `SignalFrame` into CFD trades, costs and an equity curve. Reads
from `SignalFrame` and `AnalysisFrame`; writes to neither.

```python
from cfdinvest.execution import ExecutionConfig, run_backtest

execution = run_backtest(signal_frame, analysis_frame, ExecutionConfig())

execution.equity_curve      # mark-to-market account value
execution.trades            # one row per executed change
execution.pnl               # per-bar decomposition
execution.positions         # signed units held
execution.holding_periods() # churn diagnostic
```

Performance *analytics* — Sharpe, Sortino, win rate, expectancy — are
deliberately absent. This layer's job is to be an honest simulator; judging
the strategy is a separate concern, and mixing the two makes it tempting to
tune the simulator until the numbers look good.

---

## Hysteresis: why positions are stabler than signals

Signals are computed independently per bar, so a target hovering near a
threshold flips flat and back repeatedly. Each flip pays the round-trip
spread. Hysteresis uses **two thresholds instead of one**:

```
                 exit_size            entry_size
   ────────────────┼────────────────────┼────────────────
     close a          hold what's         open a new
     position         already open        position
```

An open position survives the middle band; a flat book does not enter in it.
The same band applies independently to `confidence`, and either falling
through its exit closes the position.

| Held | Signal | Band | Action |
| --- | --- | --- | --- |
| flat | long | above entry | `open_long` |
| flat | long | hold band | `hold` — not enough to enter |
| long | long | hold band | `hold` — enough to stay |
| long | long | below exit | `close` |
| long | short | above entry | `flip_long_to_short` |
| long | short | hold band | `close` — too weak to flip, too contrary to hold |
| any | flat | any | `close` |

`transition_table()` enumerates all 27 cases (3 held × 3 desired × 3 bands).

**Weak opposing signals close rather than flip.** Holding a long while the
model says short is harder to defend than paying one spread. Set
`exit_on_opposing_signal=False` to hold instead.

**Existing positions are not rebalanced every bar.** Volatility-scaled sizes
drift slightly on every bar; resizing each time would reintroduce exactly
the churn the band removed. A position only adjusts once it is
`rebalance_threshold` (default 25%) away from target, reported as a
`resize` action so the trade log distinguishes a new view from a rebalance.

Wider band → fewer trades, staler positions. That trade-off is explicit and
tunable, and two tests pin the direction of it.

---

## Fill assumptions

**The one-bar lag is the point.** A signal computed from bar *t*'s close
cannot be acted on until bar *t+1*. The engine shifts the whole SignalFrame
forward before the state machine sees it.

Without that shift the equity curve books a bar of impossible profit on
every trade, and the backtest looks brilliant for reasons unrelated to the
strategy. `execution_lag=0` is available for experiments, logs a warning,
and a test asserts it produces a *different* curve so it can never be
silently equivalent. A negative lag is refused outright.

| `fill_mode` | Price | Honesty |
| --- | --- | --- |
| `"open"` (default) | Bar open | Achievable — the decision predates the bar |
| `"mid"` | `(high + low) / 2` | **Not knowable at the open.** Mildly optimistic |
| `"close"` | Bar close | More optimistic still |

Costs are charged on the **change** in position, so holding is free:

- **Spread** — half the quoted spread crossed per transaction, so a round
  trip pays the full spread. Resolved per asset: explicit override → asset
  class default → `default_spread_bps`.
- **Slippage** — everything the spread misses. Defaults to **zero**, so a
  run's costs are exactly the spread until you say otherwise.

Fills are always adverse: buys fill above the reference, sells below.

P&L books at the reference price with spread as a separate line, while the
trade log's `fill_price` shows the effective price including the spread.
Same total, two views — no double counting.

---

## Financing model

A CFD is a financed position, so carry is quoted as a benchmark plus a
broker markup, charged in the same direction regardless of side:

```
long  pays  benchmark + markup
short pays  markup - benchmark
```

The short leg is the one that catches people out. At a 5% benchmark and a 3%
markup a short **receives** 2%; with rates near zero the same short **pays**
3%. Modelling it as one signed rate rather than two independent inputs makes
that sign impossible to get wrong by accident.

**Financing accrues per night, not per bar.** A Friday position carried into
Monday pays three nights. Charging one night per bar would understate carry
by roughly 40% on a daily strategy, compounding into a materially wrong
equity curve over a decade. Intraday bars accrue nothing, so an intraday
round trip is free.

Charged on **absolute** notional — a short borrows the instrument just as a
long borrows the cash.

---

## P&L decomposition

Each bar splits into five components that **must** sum to the change in
equity. The test suite asserts the identity rather than trusting it.

| Component | Earned by |
| --- | --- |
| `gap` | The position held **into** the bar, from previous close to the fill |
| `intraday` | The position held **out of** the bar, from fill to close |
| `spread`, `slippage` | Charged on the change in position |
| `financing` | Overnight carry on the position held into the bar |

Splitting the bar at the fill is what makes a mid-bar trade correct: without
it, a position opened at today's open would be credited with the overnight
gap it was not there for. A test pins exactly that.

### Order of operations within a bar

1. Accrue financing on the position carried in.
2. Earn the gap on that same old position, up to the fill.
3. Decide the new target: lagged signal → hysteresis → risk caps.
4. Trade the difference; pay costs; crystallise realised P&L on what closed.
5. Earn fill-to-close on the new position.
6. Roll equity forward.

---

## Risk engine

- **Leverage cap** — gross notional over equity, capped at `max_leverage`.
  Exceeding it scales the whole book proportionally, so the *relative*
  sizing the signal layer produced survives; the cap decides how much risk
  in total, not which asset gets it.
- **Stop-out** — when equity falls to `stop_out_level` of its running peak,
  everything is flattened. `None` disables it; `resume_after_stop_out`
  decides whether trading restarts. Trades from a stop-out are tagged
  `reason="stop_out"`.

`exposure` reports leverage **as the cap enforced it**: fill-price notional
over the equity known at trade time. Close-marked leverage can drift above
the cap after an adverse move — true of the resulting position, but not
something a cap could have prevented. Use `notionals` for that view.

---

## Why there is a loop

Every layer upstream is vectorised over the whole panel because each bar's
answer depends only on that bar's inputs. Execution is different: today's
position depends on yesterday's, the leverage cap divides by a running
equity total, and a stop-out is by definition path-dependent.

That sequence is irreducible. What *can* be vectorised is the asset axis, so
the loop walks time one bar at a time and every step inside it is a handful
of numpy operations across the whole universe at once. A 900-bar, 5-asset
run takes about 0.2s.

---

## Configuration

```python
ExecutionConfig(
    initial_equity=100_000.0,
    exposure_per_unit_size=1.0,     # signal size 1.0 -> notional == equity

    execution_lag=1,                # the only look-ahead-free setting
    fill_mode="open",
    slippage_fraction=0.0,
    default_spread_bps=5.0,
    spread_bps=None,                # per-asset overrides

    thresholds=HysteresisThresholds(
        entry_size=0.10, exit_size=0.05,
        entry_confidence=0.35, exit_confidence=0.20,
        exit_on_opposing_signal=True,
    ),
    rebalance_threshold=0.25,

    benchmark_rate=0.05,
    financing_markup=0.03,
    day_count=365.0,
    financing_rates=None,           # per-asset {(long, short)} overrides

    max_leverage=5.0,
    stop_out_level=None,
    resume_after_stop_out=True,
)
```

---

## Testing

```bash
pytest tests/test_execution_*.py     # 111 tests, no network
```

| File | Covers |
| --- | --- |
| `test_execution_components.py` | State machine, fills, financing, P&L decomposition |
| `test_execution_engine.py` | Accounting identities, lag, hysteresis, risk, costs |

The accounting tests matter most: if the components stop summing to the
change in equity, every number the analytics layer eventually reports
inherits the error.

Two bugs the tests caught during the build, both now pinned:

- **The leverage cap could be bypassed.** The rebalance threshold reverted a
  position to its previous units *after* the cap was applied, so a position
  opened when equity was higher survived a cap that should have shrunk it.
  The cap now runs last, on the final book.
- **`default_spread_bps` was dead config.** An unmapped ticker resolved to
  the `"other"` asset class, whose table entry always won, so the setting
  had no effect at all.

---

## Known limitations

**No intrabar stop-losses.** The stop-out is checked once per bar on equity,
not per position against its low. A position that traded through a stop and
recovered by the close is not stopped out here. Modelling that properly
needs the ATR-based stop distance from the signal layer and a decision about
gap-throughs, which is a real piece of work rather than a parameter.

**Margin is not modelled explicitly.** The leverage cap stands in for it. A
real CFD account posts margin per position at instrument-specific rates and
is closed out on a margin *ratio*, not on drawdown from peak.

**One equity pool, no currency.** Every instrument is assumed denominated in
the account currency. FX CFDs in particular would need conversion.
