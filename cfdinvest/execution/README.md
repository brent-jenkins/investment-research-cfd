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

## Intrabar stops and take-profits

**Disabled by default** (`ExecutionConfig().stops.enabled is False`). Turning
them on changes the result of every backtest — positions that previously rode
through a spike now exit — so it is opt-in rather than a silent revision of
numbers already recorded.

```python
from cfdinvest.execution import ExecutionConfig, StopConfig, run_backtest

execution = run_backtest(
    signal_frame, analysis_frame,
    ExecutionConfig(stops=StopConfig(enabled=True, stop_distance=2.0)),
)

execution.stop_events      # one row per breach: level, fill, gap flag
execution.stop_summary()   # counts and mean slippage per kind and type
execution.working_orders() # the orders to place by hand, for the open book
```

Levels are placed from the **entry price and the ATR at entry**, and stay put
until the position changes. They do not trail; see *Known limitations*.

### Part 1 — The gap-through design note

Close-to-close marking cannot see a stop. A bar that opened at 100, traded
down to 94 and closed at 101 records a gain, and the drawdown that actually
happened is invisible. Checking `low <= stop` fixes the detection. It does
not fix the **fill**, and the fill is where the money is.

A resting stop is not a price you are owed. It is an instruction to sell at
market once a level trades. If the level trades *inside* the bar, the two are
close enough to treat as equal. If the market **gapped past** the level
between the previous close and this open, the level never traded at all, and
the first price available was the open.

```
   level 95, bar opens at 88            level 95, bar trades 100 -> 94

   prev close ●102                      prev close ●102
                                                    ●100  open
        95 ┄┄┄┄┄┄┄┄┄┄┄  <- never             95 ┄┄┄┄┄┼┄┄┄┄  <- traded
                ●88  open     traded                 ●94

   fill = 88, breach_type = "gap"       fill = 95, breach_type = "intrabar"
```

**A level is worth what you can get, not what you asked for.** Filling a
gapped stop at its level books a price the market never offered — over a long
backtest that is a systematic, one-directional overstatement of every losing
trade, concentrated in exactly the bars that matter most.

Four rules follow, and each is pinned by a test:

1. **Opening beyond the level is a gap.** `open <= stop` for a long,
   `open >= stop` for a short. Opening *exactly on* the level counts as a gap
   too: the level was reached at the open, so the open is the fill.
2. **A gap resolves the ordering of events for free.** If the bar opened past
   a level, that level was crossed at the open — before anything else in the
   bar could happen. `ambiguous` is `False` on a gapped bar by construction.
3. **Only an intrabar bar can be ambiguous.** If both the stop and the target
   trade within the same bar and neither gapped, daily OHLC does not record
   which came first. That is genuinely unknowable, `ambiguous` is set to
   `True`, and `tp_priority` decides — a policy, flagged as one.
4. **If both levels gap through, the level nearer the open decides the
   label.** Both were crossed before any trading, so the fill is the open
   either way; the only open question is which order name goes in the log,
   and the nearer level is the one price was still adjacent to when the bar
   opened. This is only reachable with an inverted pair — a long whose stop
   sits above its target — which is pathological, but the resolution has to
   be defined rather than arbitrary.

Slippage is applied **after** the fill price is chosen, and always adversely:
a long sells lower, a short buys higher. A take-profit is a *limit* order, so
it does not slip by default (`take_profit_slippage_bps` defaults to 0) — you
either get your price or you do not get filled.

**A stopped bar does not re-enter.** Re-entering at that bar's open would be
entering at a price that occurred *before* the exit. `reenter_same_bar` exists
for experiments and defaults to `False`.

### Part 2 — Configuration spec

```python
StopConfig(
    enabled=False,                  # opt-in: enabling moves every number

    stop_mode="hybrid",             # "breach" | "gap" | "hybrid"
    tp_priority=True,               # take-profit wins an ambiguous bar

    stop_distance_mode="atr",       # "atr" | "fixed" | "percent"
    stop_distance=2.0,              # 2 x ATR at entry
    tp_distance_mode="atr",
    tp_distance=4.0,                # None disables the target entirely

    slippage_bps=0.0,               # applied after the fill price, adverse
    take_profit_slippage_bps=0.0,   # a limit order does not slip
    max_slippage_bps=50.0,          # cap, and it binds on slippage_bps

    record_recommended_fill=True,   # always true for manual execution
    reenter_same_bar=False,
)
```

**`stop_mode`** — what price a breach fills at:

| Mode | Fill | Reading |
| --- | --- | --- |
| `"breach"` | The level, always | **Optimistic.** Books fills the market never offered on gapped bars |
| `"gap"` | The bar open, always | **Conservative.** Charges gap risk even on bars that traded through cleanly |
| `"hybrid"` (default) | Level if it traded, open if it gapped | **Realistic.** What a resting order would actually have done |

`breach_type` is reported honestly in all three: even in `"breach"` mode a
gapped bar is logged as `"gap"`, so the event log tells you how much the mode
is flattering the result.

**`tp_priority`** — only consulted on an ambiguous bar (both levels traded
intrabar, neither gapped). `True` takes the profit; `False` takes the loss.
The pair brackets the truth rather than pretending to know it: run both, and
the difference is the size of the unknowable. A test asserts the optimistic
setting finishes richer on identical data, which is why it is flagged rather
than assumed harmless.

**Distance modes** — `atr` multiplies the ATR at entry, `fixed` is an absolute
price distance, `percent` is a fraction of the entry price. `fixed` and
`percent` work on a close-only lake; `atr` does not, and stops are disabled
with a warning if the ATR panel is missing.

**`max_slippage_bps`** caps `slippage_bps`, so a stress run cannot accidentally
be given a slippage assumption larger than any real fill. It defaults to 50
bps, which means `slippage_bps=100` silently becomes 50.

It clamps rather than raising **because that is what the broker does** — a
guaranteed-fill ceiling is applied to the order, not a reason to reject it, so
the simulator behaves the same way. The cost is that an over-ambitious
assumption is quietly ignored instead of flagged; raise the ceiling
deliberately when a stress run needs it.

**`record_recommended_fill`** keeps `recommended_fill` populated on every
event. Left `True`, because these trades are placed by hand.

### Part 3 — Test matrix

`tests/test_execution_stops.py`, 81 tests, no network. The eleven specified
cases are named after their number:

| # | Case | Expected |
| --- | --- | --- |
| 1 | Stop hit, no gap | `breach_type="intrabar"`, fill at the level |
| 2 | Stop gapped through | `breach_type="gap"`, fill at the open |
| 3 | Stop hit exactly at the open | `breach_type="gap"`, fill at the open (= the level) |
| 4 | Stop breached intrabar, price recovers by the close | Still stopped — the case close-to-close marking misses |
| 5 | Take-profit hit | `TakeProfitEvent`, fill at the target |
| 6 | Both hit intrabar | `ambiguous=True`; `tp_priority` decides which |
| 7 | Both gap through | `breach_type="gap"`, fill at the open, nearer level labels it |
| 8 | Neither hit | `None` — no event, no trade |
| 9 | Slippage applied | Fill adjusted adversely, capped by `max_slippage_bps` |
| 10 | Short mirrors long | Every case above, with the inequalities reversed |
| 11 | Manual execution | `recommended_fill` returned; nothing is sent anywhere |

On top of those: every `stop_mode` and every distance mode end to end;
vectorised detection across a mixed universe; determinism (same inputs, same
events); flat positions and missing levels never breaching; and the
integration properties that matter more than any single case —

- the five P&L components still sum to `total`, and equity still equals
  `initial + cumsum(total)`, with mid-bar exits in the path;
- a stopped bar holds zero units and zero notional afterwards;
- `stops.enabled=False` reproduces the previous behaviour exactly;
- a tighter stop distance produces strictly more exits.

The fill modes are compared **per event, not on the equity curve**. A
different fill changes equity, which changes the next position size, which
changes which bars breach at all — so `breach` mode can finish poorer than
`gap` mode on the same data even though every individual fill was better.

Two bugs these tests caught during the build, both now pinned:

- **A level was never placed if the ATR had not warmed up at entry.** A
  position opened on bar 3 of a 14-bar ATR window kept a `NaN` stop for its
  entire life. Levels are now also placed on the first bar the ATR becomes
  available.
- **`stop_summary()` called `.abs()` on a `SeriesGroupBy`**, which has no such
  method; taking the absolute value before grouping also stops a long exit
  netting against a short one.

### Part 4 — Risk-model note

**Exposure at trade time.** A stop does not cap exposure, it removes it: the
position goes to zero units on the breach bar, and the `exposure` series
reflects that from the same bar. What a stop bounds is the *loss per
position*, not the size of the book — the leverage cap is still what limits
how much can be on at once, and the two are independent. A tighter stop with
an unchanged `exposure_per_unit_size` means smaller losses per trade and more
of them, not a smaller book.

**Margin interaction.** Margin is still not modelled explicitly; the leverage
cap stands in for it. That matters here because the two protections fail in
opposite conditions. A stop is a per-position instruction that works when one
instrument moves against you. A margin call is an account-level event that
arrives when *everything* moves at once — precisely the correlated-breakdown
regime, where stops across the book gap through together and the sum of their
fills is worse than any single one suggested. Stops make a margin call less
likely; they do not make the account immune to one, and this simulator will
not show you the difference.

**Gap-through risk is the residual, and it is not symmetric.** A stop bounds
the loss at the level *only if the level trades*. Overnight gaps, weekend gaps
and event risk are exactly the cases where it does not, and they are also
where the largest losses live — so the bound fails hardest at the moment it
matters most. `stop_summary()` reports gap exits as their own row for this
reason: a strategy whose exits are mostly `gap` is one whose backtested stop
distances are fiction, and `hybrid` mode is what stops that being invisible.
The residual is unhedgeable within the position; the only controls are
position size and not holding into known events.

**Overnight financing.** Financing accrues per night on the position carried
*into* a bar, so a position stopped out during bar *t* still pays for the
night before it. That is correct — it was financed overnight and the exit
happened afterwards — and it means tight stops do not reduce carry as much as
the shorter average holding period suggests. The interaction runs the other
way too: a wide stop on a financed long pays carry for the privilege of a
larger loss, while on a short that *receives* carry (benchmark above markup)
the same width is partly funded. That trade-off is not reduced to a single
number anywhere; read `pnl["financing"]` alongside `stop_summary()`.

**Multi-currency.** Every instrument is assumed denominated in the account
currency, so a stop level is compared against a price in the same units the
P&L is booked in. For an instrument quoted in another currency this is wrong
in two places: the level is a foreign-currency price, and the loss it bounds
is a foreign-currency loss, which converts at a rate that has itself moved —
most likely in the same crisis that triggered the stop. **A stop bounds the
local loss, not the account loss.** Converting properly needs an FX panel and
a decision about whether the currency exposure is hedged, which is a layer of
its own.

---

## FX conversion

**Disabled by default** (`ExecutionConfig().fx.enabled is False`), which
treats every instrument as already denominated in the account currency —
exactly what the engine assumed before this module existed, so no recorded
number moves.

```python
from cfdinvest.execution import ExecutionConfig, FXConfig, run_backtest

execution = run_backtest(
    signal_frame, analysis_frame,
    ExecutionConfig(fx=FXConfig(
        enabled=True,
        account_currency="GBP",
        instrument_currencies={"SPX": "USD"},
        fx_rate_source="lake",
        fx_pair_tickers={"USD": "GBPUSD"},
    )),
)

execution.fx_total_cost   # everything conversion took out
execution.fx_summary()    # cost per instrument, beside the rate
execution.fx_open_cost    # per bar, funding leg
execution.fx_close_cost   # per bar, repatriation leg
execution.fx_rate_used    # per instrument, as at the last bar
```

### Part 1 — Conversion logic

**The rate convention is fixed once so it cannot be read backwards.**
`fx_rate` is always *units of account currency per one unit of instrument
currency*. A GBP account trading a USD instrument at 0.79 GBP/USD has
`fx_rate = 0.79`, and a $100 price is £79. Conversion is a multiplication in
one direction and a division in the other, and there is nowhere for the sign
of an exponent to hide.

**When conversion applies.** At open, account currency is converted into
instrument currency to fund the position. At close, instrument currency is
converted back. A flip does both: the whole old position is repatriated and
the whole new one funded, which is what `split_open_close()` computes and
what the broker actually converts.

**Prices convert at the mid; the charges are separate cost lines.** This is
the same split the price spread already uses. The whole price panel — close,
open, high, low, and the ATR — is restated in the account currency before
the loop starts, so the state machine, the leverage cap and the equity curve
all work in one unit and none of them needs to know FX exists. The three
charges then sit beside the P&L as a sixth component. No double counting,
and the identity still holds.

The ATR converts too, and it has to: an ATR-multiple stop on a dollar
instrument would otherwise be placed a *dollar* distance away from a
*sterling* price.

**Price flow for one trade:**

```
  base_fill_price          from fills.py / stops.py, instrument currency
        |
        x fx_rate          the mid, per bar, from the lake
        |
   fill_price              account currency -- what P&L books at
        |
        + fx_spread        \
        + fx_slippage       >  charged separately, never folded into price
        + conversion_fee   /
        |
   fx_adjusted_fill_price  the all-in number, for the person placing it
```

**Rates are per bar, not per run.** The `"lake"` source reads the FX series
bar by bar and forward-fills only. Using one rate for a whole decade would
put an end-of-run rate on a start-of-run trade — look-ahead, and of a kind
that is easy to miss because it hides inside a cost model rather than in a
signal. A truncation test pins it: cutting the last 100 bars off the input
must not change any of the first 200.

**An unhedged foreign position earns the currency move**, and that is not a
cost — it is a real exposure the account carries. A GBP investor holding a
USD instrument owns the dollar as well as the instrument. Converting the
price panel is what makes that show up in the equity curve at all.

### Part 2 — Fee structure

```python
FXConfig(
    enabled=False,                  # opt-in

    account_currency="GBP",
    instrument_currencies=None,     # {ticker: currency}; unmapped == domestic

    fx_conversion_fee_pct=0.005,    # 0.5%, Trading212's
    fx_spread_bps=0.0,              # applied to the rate, before conversion
    fx_slippage_bps=0.0,            # applied after the spread

    fx_rate_source="lake",          # "lake" | "config" | "static"
    fx_rates=None,                  # {currency: rate}; the lake's fallback
    fx_pair_tickers=None,           # {currency: ticker} naming the lake series
    static_fx_rate=1.0,

    fx_rounding_mode="none",        # "none" | "nearest_pip" | "nearest_cent"
    fx_fee_basis="notional",        # "notional" | "result"  <- read this one
    record_fx_details=True,         # always true for manual execution
)
```

**Three charges, deliberately not merged**, because a real statement lists
them separately and they answer different questions:

| Charge | On what | Typical |
| --- | --- | --- |
| `fx_conversion_fee_pct` | The broker's explicit percentage | 0.5% — dominant at retail size |
| `fx_spread_bps` | The two-sided quote, adverse on both sides | a few bps |
| `fx_slippage_bps` | What the quoted spread misses | 0 by default |

Order of application is fixed and is the order a broker applies it in:
**spread, then slippage, then rounding**. Rounding last matters — rounding
the mid first and widening it afterwards would quote a granularity the deal
never had. A test asserts the two orders differ.

**`fx_fee_basis` is the setting that matters, and it is worth two orders of
magnitude.**

| Basis | Charged on | A 0.5% fee on a £30k position that gains 1% |
| --- | --- | --- |
| `"notional"` (default) | The converted position value, at open **and** close | ~£300 |
| `"result"` | The realised P&L and financing, at close only | ~£1.50 |

The default is `"notional"` because it is what this module was specified to
do, and because it is the conservative reading — it cannot flatter a result.
**But `"result"` is closer to how Trading212 actually charges CFD
conversion:** a CFD never buys the underlying, so no position notional
crosses currencies at open; what crosses is the outcome. If the strategy
holds foreign instruments at any size, run both — the gap between them is
larger than most of the effects the rest of this engine models.

`fx_conversion_fee_pct` is a **fraction**, and a value at or above 0.1 is
refused: `0.5` is exactly what "0.5%" becomes when it is typed as a number,
and no broker charges a tenth of the converted amount.

### Part 3 — Integration points

| File | What it does |
| --- | --- |
| `fx.py` | The model. Pure, and calls nothing |
| `fills.py` | `convert_prices()` restates the panel; `fx_open_costs()` is the funding leg |
| `pnl.py` | `fx` joins `PNL_COMPONENTS`; `fx_close_costs()` is the repatriation leg |
| `engine.py` | Builds the per-bar rate matrix, splits open from close, logs the rate on every trade |
| `frame.py` | `FXConfig` on the config; `fx_open_cost`, `fx_close_cost`, `fx_total_cost`, `fx_rate_used`, `fx_summary()` on the frame |

The trade log gains `fx_rate`, `fx_spread`, `fx_conversion_fee`,
`fx_slippage` and `fx_adjusted_fill_price`. `bar_pnl()`'s `fx_cost` argument
defaults to zero, so every pre-existing caller produces exactly what it
produced before.

**A domestic instrument is charged nothing.** `converting_assets()` marks
which tickers actually cross a currency boundary, and the charges are masked
to those. An unmapped ticker is assumed domestic — the safe direction, since
it understates costs visibly rather than inventing them silently, and the
resolver warns when it cannot find a rate it was told to expect.

### Part 4 — Manual execution

**Nothing is sent anywhere.** `recommended_fx_fill()` returns the all-in
price to expect, beside the unconverted `instrument_price` the broker's
order ticket will actually show. Both are on every trade-log row while
`record_fx_details` is `True`, which it is by default and should stay.

Latency between the recommendation and the order being placed is accepted.
The rate will have moved a few pips, which is small beside a 0.5% conversion
fee and is not something a daily-bar backtest could model honestly anyway.
The backtest remains realistic because it models *ideal* CFD behaviour and
charges the costs that are actually predictable; it does not pretend to
predict the ones that are not.

### Test matrix

`tests/test_execution_fx.py`, 87 tests, no network.

| # | Case | Expected |
| --- | --- | --- |
| 1 | Open conversion | Account → instrument; fee, spread and slippage applied |
| 2 | Close conversion | Instrument → account; charges applied, adverse the other way |
| 3 | No conversion | Same currency ⇒ every charge exactly zero |
| 4 | Spread only | `spread_bps > 0`, `fee_pct = 0` |
| 5 | Fee only | `fee_pct > 0`, `spread_bps = 0` |
| 6 | Combined | Spread, then slippage, then rounding — and the order is not commutative |
| 7 | Rounding | `none`, `nearest_pip`, `nearest_cent` |
| 8 | Manual execution | `recommended_fill` returned; nothing is sent anywhere |
| 9 | Multi-currency prep | `convert_to_` / `convert_from_account_currency` are exact inverses |
| 10 | Determinism | Identical inputs ⇒ identical outputs |

Beyond the matrix, the tests that decide whether this is safe to switch on:

- **A constant rate with no fees changes nothing.** Position sizing is
  notional-based, so a pure rescaling of prices cancels exactly. This is
  what proves the conversion is a change of *units* and not a change of
  *model* — any difference after it is a cost, not an artefact.
- **The P&L identity survives a sixth component**, with FX and stops both on.
- **A lake rate cannot reach backwards** — the truncation test above.
- **The reported cost is exactly what the P&L was charged**, though the
  *shortfall against a fee-free run* is only approximately the fee: charging
  it lowers equity, which resizes the next position. That gap is compounding,
  not error, and the tests state which of the two is exact.

Two bugs these tests caught during the build:

- **Domestic instruments were charged a conversion fee** — the whole cost
  model applied to transactions that never happened.
- **The `"lake"` source warned on every call**, because the rate-matrix
  builder asks for the fallback first and supplies the real per-bar rates
  itself afterwards.

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
pytest tests/test_execution_*.py     # 279 tests, no network
```

| File | Covers |
| --- | --- |
| `test_execution_components.py` | State machine, fills, financing, P&L decomposition |
| `test_execution_engine.py` | Accounting identities, lag, hysteresis, risk, costs |
| `test_execution_stops.py` | Intrabar stops: the eleven-case matrix, fill modes, manual orders |
| `test_execution_fx.py` | FX conversion: the ten-case matrix, fee bases, look-ahead |

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

**Stop levels do not trail.** A level is placed from the entry price and the
ATR at entry and stays there until the position changes. A trailing stop
would ratchet on every bar; that is a separate behaviour with its own
parameters, not a mode of this one.

**Sub-bar ordering is unknowable from OHLC.** When both levels trade within
one bar and neither gapped, the bar does not record which came first. The
`tp_priority` pair brackets it; nothing recovers it short of intraday data.

**Margin is not modelled explicitly.** The leverage cap stands in for it. A
real CFD account posts margin per position at instrument-specific rates and
is closed out on a margin *ratio*, not on drawdown from peak.

**One equity pool, and conversion but not multi-currency accounting.** FX
conversion costs are modelled (above), and prices are restated into the
account currency, but the account still holds a single cash balance. A real
multi-currency account keeps a balance per currency, converts only what it
needs to, and can leave a foreign balance unconverted between trades — which
is materially cheaper than converting both ways on every round trip. The
primitives an accounting layer would need are exposed for exactly this:
`convert_to_account_currency`, `convert_from_account_currency`,
`apply_conversion_fee` and `apply_fx_spread`.

**FX exposure is unhedged and unmeasured.** A foreign position carries the
currency as well as the instrument, and the equity curve reflects it, but
nothing reports how much of a result came from the instrument and how much
from the rate. That decomposition belongs to the analytics layer.
