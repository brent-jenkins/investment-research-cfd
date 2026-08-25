# Analytics and backtesting

Scoring completed runs, sweeping parameters, walking forward, and resampling
trades. Reads the lake, runs the existing pipeline, and writes only into
`<data root>/analytics/`.

```bash
cfdinvest-analytics run-once     --tickers SPY,GLD
cfdinvest-analytics sweep        --grid '{"execution.max_leverage": [2, 5]}'
cfdinvest-analytics walkforward  --train 365D --test 90D
cfdinvest-analytics montecarlo   --paths 2000
```

```python
from cfdinvest.analytics import run_backtest, compute_all_metrics

result = run_backtest(config)
result.metrics["sharpe_ratio"]
result.warnings          # why not to believe it, if there is a reason
```

---

## This layer sits on top of `cfdinvest/backtest/`, it does not replace it

The maths for Sharpe, Sortino, Calmar, drawdown, win rate, expectancy, R
multiples, walk-forward windows and block-bootstrap resampling was already
built in [`cfdinvest/backtest/`](../backtest/README.md). **None of it is
reimplemented here.** A second implementation is not a second opinion — it is
a second number that can disagree with the first while both claim to be the
Sharpe ratio, and a test asserts the delegation directly.

What this package adds:

| | Where |
| --- | --- |
| A surface that takes an `ExecutionFrame` and returns everything at once | `metrics.py` |
| Metrics that only exist once stops and currency are modelled — gap-through incidence, currency exposure, leverage against its cap | `metrics.py` |
| P&L attribution, and the instrument-versus-currency split | `attribution.py` |
| Parameter sweeps, scenario sets, and warnings on every result | `backtest.py` |
| Walk-forward over **date ranges** rather than positional slices | `walkforward.py` |
| Trade-level resampling with per-path metrics | `montecarlo.py` |
| Persisted results, and the readers the dashboard uses | `store.py` |
| A command line | `cli.py` |

Names collide in three places, and they mean different things:
`cfdinvest.execution.run_backtest` *simulates*,
`cfdinvest.backtest.run_backtest_pipeline` *runs the pipeline*, and
`cfdinvest.analytics.run_backtest` *runs and scores*.

---

## Attribution, and the one split that is not free

Every decomposition here sums to the total, and the suite asserts it rather
than trusting it:

```
instrument_pnl + fx_pnl + spread_pnl + slippage_pnl
              + financing_pnl + fees_pnl  ==  total_pnl
```

This holds with mid-bar exits, stops and currency conversion in play, because
the terms are groupings of the engine's own six components rather than a
recalculation of them.

**"How much came from the instrument and how much from the currency" cannot
be read off a completed run.** The engine restates every price into the
account currency *before* the loop, so by the time P&L exists the two are
already added together. Recovering them needs the instrument-currency prices
and the per-bar exchange rate, which means handing the module the analysis
frame the run was built from:

```python
compute_pnl_attribution(execution, analysis_frame=analysis)
```

For a position of `u` units held across a bar, with instrument price `P` and
rate `r`, the account-currency move decomposes exactly:

```
u(P₁r₁ − P₀r₀) = u·r₀(P₁ − P₀)     ← the instrument moved
               + u·P₀(r₁ − r₀)     ← the currency moved
               + u(P₁−P₀)(r₁−r₀)   ← both, at once
```

The cross term is small but real, and is reported rather than folded away. It
is attributed to the currency leg, because it only exists because the currency
moved.

This is a **reconstruction, not a reading**: it marks close-to-close while the
engine splits each bar at the fill, so it will not reproduce the engine's
market leg to the penny. The difference is reported as
`reconstruction_error`, and the headline identity stays exact because the
approximation is confined to the instrument leg.

Without the analysis frame, `fx_pnl` is **not** set to zero and left there —
the split reports `available=False` with a reason. A zero would read as "no
currency effect", which is a different claim from "not measured".

### Reading a run the pipeline already wrote

The dashboard has no `pnl` frame — the pipeline does not persist one — so it
uses `attribution_from_trades(trades, equity)`. Every cost is charged on a
trade and recorded there, so the cost legs are exact sums and the market leg
is the remainder:

```
market = total + costs
```

The identity is exact by construction. What is lost is the split between the
overnight gap and the intraday move.

---

## Warnings are part of the result

A backtest that ran cleanly and reported a Sharpe of 3 on eleven trades is not
a good result; it is a result nobody should act on. The conditions that make a
number untrustworthy are enumerable, so every `BacktestResult` carries them:

| Warning | Fires when |
| --- | --- |
| Too few trades | Under 30 completed round trips |
| Implausible Sharpe | Above 3 on a daily strategy — look for look-ahead first |
| Heavy gapping | Over 35% of level exits gapped through |
| Leverage against the cap | Mean leverage above 90% of the cap: the cap is sizing the book, not the signal |
| Cap breached | Any bar above the cap — the cap is not doing what it claims |
| Costs eating the edge | Costs over half of gross profit |
| Severe drawdown | Over 35% — the path matters more than the endpoint |
| Currency cost | Over 1% of traded notional; check `fx_fee_basis` |

Thresholds live in `WarningThresholds` and are all keyword arguments.

---

## Sweeps

```python
run_parameter_sweep(config, {"execution.max_leverage": [2, 5, 10]})
```

Overrides are dotted paths into the nested config, applied to a **copy** — a
sweep that mutated its base would make every run after the first depend on the
ones before it. An unknown path raises rather than being ignored: a silently
dropped override reports the same result under two different labels.

**A sweep is a multiple-comparisons machine.** Run enough points and one looks
excellent by luck alone. The best row in a sweep table is an upper bound on
what the strategy does, not an estimate of it — which is what walk-forward is
for.

Failed runs stay in the table with their error. A sweep that silently drops
the points that broke hides the shape of the failure, which is usually the
most informative thing in it.

---

## Walk-forward

```python
windows = rolling_date_windows("2022-01-01", "2025-01-01", train="365D", test="90D")
results = run_walkforward(config, windows, param_grid=grid)
```

Windows are **strictly partitioned**: a test range begins after its training
range ends, never overlaps it, and is never used to select anything. Both
properties are asserted over generated windows, and an overlapping window is
refused at construction.

**Each window runs once over the whole span and is then sliced.** Running the
test range in isolation would warm every indicator up *inside* the test
window, so the first fifty bars would be scored on a half-built ATR and a
correlation matrix that does not exist yet. Running from the training start
and slicing afterwards gives the test period the indicator state it would
genuinely have had, and costs nothing in look-ahead because every rolling
window in this system is trailing-only.

With a `param_grid`, each window's parameters are selected by running the grid
over the **training range only**. Without one, the same configuration is
evaluated in both halves, which measures stability rather than generalisation.

The column to read in the stability summary is `mean_drift` (test − train).
Consistently negative means the in-sample result did not survive. A large
`std_test` means it survives *sometimes*, which is worse than a small
consistent decay because it cannot be planned around.

---

## Monte Carlo

```python
paths = resample_trades(trades, n_paths=2000, seed=42)
summary = compute_montecarlo_metrics(paths)
summary["tail_loss"]   # 5th percentile of final return
```

Reproducible: the same trades, path count and seed always produce the same
paths, and a test pins it.

**Time ordering is deliberately destroyed, and that has a cost.** Drawing
trades independently preserves the distribution of individual outcomes exactly
and discards every relationship *between* them: the losing streak that came
from one bad fortnight, the volatility clustering that fired three stops in a
week. Real losing runs are longer than independent draws produce, so **the
drawdown distribution here is a floor, not an estimate**. For serial
structure, use the block bootstrap in `cfdinvest.backtest.montecarlo`, which
resamples bar returns in blocks and keeps it.

`replace=False` shuffles the actual trades instead, holding the total return
fixed and varying only the path — which separates "was this drawdown bad luck
in the ordering?" from "was this return luck?".

---

## Where results go

```
<data root>/analytics/<run id>/
    metrics.json              scored run: metrics, config snapshot, period
    attribution.json          the P&L decomposition
    costs.json                the cost breakdown
    warnings.json             why not to believe it
    sweep.parquet             one row per grid point
    walkforward.parquet       one row per window
    walkforward_summary.json  drift and dispersion per metric
    montecarlo.json           the resampled distribution
    montecarlo_paths.parquet  per-path metrics, for a histogram
```

**Analytics never writes into a run directory.** A pipeline run is an
immutable record of what happened, and scoring it later must not touch it — a
test asserts no `runs/` directory appears.

Writes are atomic (temporary file, then rename), so a reader never sees half a
document. Every artefact carries a schema version, so a reader can tell
"written by an older version" from "corrupt". Readers never raise: absence and
failure are distinguished, because only one of them is a fault.

`NaN` and infinities serialise as `null` — JSON has no way to spell them and
every parser disagrees about what to do instead.

---

## In the dashboard

The **Profit & Loss** page shows Sharpe, Sortino, Calmar, win rate,
expectancy, the cost breakdown and the attribution — all computed on the spot
from the trade log and equity curve the run already wrote. Nobody has to run
the command-line tool first to see a Sharpe ratio.

The **Analytics** page shows sweeps, walk-forward and Monte Carlo, which
*cannot* be computed on demand because each one re-runs the pipeline many
times. It reads what the tool wrote, and says how to produce it when there is
nothing there.

Analytics results are keyed by their own run identifier. Pass
`--run-id <pipeline run id>` to pair them with a pipeline run; otherwise the
page falls back to the newest analytics directory and says that it has.

---

## Testing

```bash
pytest tests/test_analytics.py -q      # 100 tests, no network
```

Two kinds. The **identities** — an attribution summing to its total, windows
not overlapping, a seed reproducing a distribution — are properties that must
hold for any input, and are asserted directly rather than against recorded
numbers. The **integration** tests run the real pipeline over a small
synthetic lake built on disk; they are the only ones that would catch a change
in the pipeline breaking the scoring, so the lake is built once per session
and reused.

Three bugs the tests caught during the build:

- **R multiples raised `KeyError` on a partial trip frame.** Each R basis
  needs a different column, and a dashboard holding a minimal frame should get
  "not computable" rather than a traceback.
- **Walk-forward windows were timezone-naive** while every frame they slice is
  UTC-aware, so slicing failed deep inside pandas rather than complaining at
  the boundary. Windows now normalise to UTC at construction.
- **Argparse subparser defaults clobbered the parent's values**, so
  `--tickers SPY run-once` silently lost `SPY`. The shared options now
  suppress their defaults and have them filled in after parsing.

---

## Scoring a run that already happened

```bash
cfdinvest-analytics score              # the newest run
cfdinvest-analytics score --all        # every stored run, as one table
```

```python
from cfdinvest.analytics import score_run
result = score_run(config, run_id)     # runs nothing
```

Once the pipeline persists its trades, equity and per-bar decomposition,
scoring a past run is a read. Measured on a three-asset, 900-bar run: **485 ms
to run and score, 40 ms to score what is on disk — about 12× faster**, with
identical numbers.

It also gets a *better* answer. A stored run carries the engine's own
decomposition, so the attribution separates the overnight gap from the
intraday move; scoring live without one falls back to reconstructing from the
trade log, which is exact for costs but cannot split the market leg. Runs
recorded before the persistence layer take that fallback, and say so.

Nothing is recomputed. `exposure` is not persisted, so leverage statistics
come back as NaN rather than as zero — an absence, not a number nobody stored.

---

## Sweeps share one analysis frame

A grid that only varies regimes, signals or execution produces the same
analysis frame at every point, so it is built once and each point runs from
it. `shares_analysis()` decides: a grid touching `preprocess.*`, the universe,
the interval or the date range **cannot** share, because reusing the frame
there would score every combination against the first point's indicators
while labelling them as their own.

**Be honest about the size of this.** Preprocessing is not where the time
goes:

| Universe | Preprocessing | Rest of the pipeline | Best possible speedup |
| --- | --- | --- | --- |
| 3 assets | 17% | 83% | 1.21× |
| 8 assets | 18% | 82% | 1.22× |
| 20 assets | 30% | 70% | 1.43× |

The execution loop dominates, and it is irreducibly sequential — today's
position depends on yesterday's. The saving is real and grows with the
universe, but it is tens of per cent, not an order of magnitude. Anyone
expecting a fifty-point sweep to become instant should look at the table
above first.

---

## Known limitations

**The execution loop is the cost, and it cannot be shared.** Sweeps reuse the
analysis frame, which is roughly a fifth of a small run and a third of a
twenty-asset one. The rest is the bar-by-bar simulation, which has to happen
once per configuration because that is what a configuration changes.

**The instrument-versus-currency split needs the currency map.** A run summary
records the FX *policy* but not which instrument is quoted in which currency,
and without that map every rate resolves to 1.0. The split reports
`available=False` rather than a confidently wrong zero.

**R is reconstructed, not recorded.** Execution does not model per-trade risk,
so `MetricsConfig.r_basis` decides how 1R is inferred. Two runs with different
bases are not comparable on that number, which is why the basis is returned
beside it.
