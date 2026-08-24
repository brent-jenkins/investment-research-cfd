# Backtesting and analytics

Runs the full pipeline and evaluates the result. The two halves are kept
apart on purpose: the runner orchestrates and owns no modelling; the metrics
evaluate and touch no simulator parameter.

```python
from cfdinvest.backtest import (BacktestConfig, MonteCarloConfig,
                                rolling_windows, run_backtest_pipeline)

result = run_backtest_pipeline(
    repository,
    BacktestConfig(tickers=["SPY", "QQQ", "TLT", "GLD"], start="2016-01-01"),
    windows=rolling_windows(n_bars, train_bars=400, test_bars=200),
    monte_carlo=MonteCarloConfig(simulations=2000, seed=7),
)

print(result.report())
result.summary()          # headline metrics
result.window_metrics     # one row per walk-forward window
result.stability          # how much each metric moved between windows
result.monte_carlo        # percentiles of the resampled distribution
result.round_trips()      # completed trades, net of apportioned financing
```

---

## Pipeline flow

```
DataRepository
      │  load_market / load_macro          (publication-aware)
      ▼
AnalysisFrame      preprocessing   causal, computed once
      ▼
RegimeFrame        regimes         causal, computed once
      ▼
SignalFrame        signals         causal, computed once
      ▼
ExecutionFrame     execution       path-dependent, re-run per window
      ▼
BacktestFrame      metrics         read-only
```

Two entry points, and the split matters:

| Function | Does I/O | Use for |
| --- | --- | --- |
| `run_pipeline(repository, config)` | yes | A normal run from the lake |
| `run_from_analysis(analysis, config)` | no | Walk-forward, sweeps, tests |

Preprocessing and regimes are causal but expensive; execution is
path-dependent and cheap. Walk-forward therefore runs the first half once
and the second half many times — only possible because the boundary is a
function argument rather than a step buried inside one procedure.

`evaluate()` re-scores a result without re-running it, so a different
risk-free rate or `r_basis` costs nothing and cannot change what was
simulated.

---

## Metric definitions

Every one of these has several defensible definitions. The convention used
is stated here and pinned by a test, because a Sharpe ratio computed on a
different convention is not comparable to one computed on this one — and the
difference is usually larger than the effect being measured.

### Equity curve

| Metric | Definition |
| --- | --- |
| `total_return` | `final / initial − 1` |
| `cagr` | Annualised over the **calendar** span, not a bar count, so a partial year is not annualised as if complete |
| `max_drawdown_fraction` | Deepest peak-to-trough decline against a **running** peak |
| `drawdown_bars` | Longest stretch spent below a prior peak |
| `annualised_volatility` | Sample stdev (`ddof=1`) of bar returns × √periods |

### Risk-adjusted

**Sharpe** — arithmetic bar returns, sample stdev, risk-free rate divided
evenly across bars, √time scaling. That last assumption is the weak one: it
treats returns as serially uncorrelated, which flatters a trending strategy
and penalises a mean-reverting one.

**Sortino** — the downside deviation divides by the count of **all**
observations, not only the losing ones. The other variant inflates the ratio
for a strategy that rarely loses, and the two numbers are not comparable.

**Calmar** — CAGR over the depth of the worst drawdown.

All three return `NaN` rather than infinity when the denominator is zero. A
flat account has no risk-adjusted return; reporting `inf` would rank it top.

### Trade level

Trades are reconstructed into **round trips** by walking the trade log per
asset: a trip opens when the position leaves flat and closes when it
returns. A flip closes one and opens another on the same bar, so a reversal
counts as two decisions.

| Metric | Definition |
| --- | --- |
| `win_rate` | Fraction with positive net P&L. A scratch counts as a loss — it paid the spread and returned nothing |
| `expectancy` | Mean net P&L per trip |
| `payoff_ratio` | Average win ÷ average loss |
| `profit_factor` | Gross profit ÷ gross loss |
| `average_r` | Mean outcome in units of risk — see below |
| `median_bars_held` | Churn diagnostic; a median of 1–2 means the hysteresis band is too narrow |

### ⚠️ The R multiple is reconstructed, not recorded

**Execution does not model per-trade stops**, so the risk a trade was
*intended* to carry is not stored anywhere. Three bases are offered, and
which one you pick changes the answer:

| `r_basis` | `1R` is | Caveat |
| --- | --- | --- |
| `equity_fraction` (default) | `risk_fraction × equity at entry` | Only meaningful if `risk_fraction` matches the signal layer's |
| `average_loss` | Mean loss of this run's losing trades | Self-referential — cannot be known in advance |
| `notional` | `stop_fraction × notional at entry` | Assumes one stop distance for everything |

The basis used is recorded in `metadata["metrics_convention"]`.

### Financing is apportioned across trades

Execution charges financing per bar on the whole book and does not record it
per asset, so a per-trade figure cannot be read off the trade log. It *can*
be apportioned exactly: carry accrues in proportion to notional × nights,
which is `|entry_notional| × days_held`. Weighting by that and scaling to
the known total gives per-trip figures summing to the real number.

This is not cosmetic. Before it was added, this happened on a live smoke
test:

```
actual total return                 5.5%
Monte Carlo median return          13.4%
Monte Carlo P(profit)             99.75%
```

Trade P&L was gross of financing while the equity curve was net of it, so
resampling described a strategy that never paid to hold anything. After
apportioning:

```
actual total return                 5.5%
Monte Carlo median return           5.6%
Monte Carlo P(profit)             87.85%
```

A test asserts `sum(net_pnl) == final_equity − initial_equity` whenever no
position is open at the end — the invariant that catches an accounting slip
anywhere upstream.

---

## Walk-forward

**No parameters are fitted here.** This is a stability report, not an
optimiser. The windows answer "was the headline number produced by one lucky
stretch?", which is a different question from "what settings would have
worked best" — and answering the second inside analytics is how a backtest
ends up describing its own tuning.

The `train` window is carried through and reported so an optimiser could be
dropped in later, but nothing in this module reads it.

| Splitter | Train window |
| --- | --- |
| `rolling_windows` | Fixed length, slides with the test window |
| `anchored_windows` | Expands from the start — closer to running forward in time |

Test windows do not overlap by default; overlapping ones would count the
same bar's outcome twice and understate the variance the report exists to
show.

**Each test window starts flat with fresh equity.** Carrying a position or a
balance across the boundary would make window two depend on window one,
which defeats the point of splitting them.

`stability_report()` returns mean, median, std, min, max and
`positive_share` per metric. `positive_share` is usually the most
informative: a good mean with a 40% positive share means the headline came
from a handful of windows.

---

## Monte Carlo

Two resamplers, both **seeded and deterministic**:

**`resample_trades`** shuffles completed round trips with replacement. Holds
the edge fixed and varies only the order it was realised in, which reveals
how much of a drawdown was sequencing.

**`resample_returns`** block-bootstraps bar returns. Blocks preserve
short-horizon autocorrelation — volatility clustering, momentum — that an
i.i.d. shuffle destroys. Removing it understates tail risk badly, which is
why `block_size` defaults to 5 rather than 1.

**What this does not do.** Resampling assumes trades are exchangeable draws
from a stable process. They are not: a regime shift changes the
distribution, and no reshuffling of the past contains a future that has not
happened. Treat the output as a sensitivity check on sequencing, not a
forecast.

---

## Limitations

These are inherited from the execution layer and are **not** fixed here.
They bound what any number in this package means.

**No intrabar stops.** The stop-out is a once-per-bar equity check, not a
per-position test against the low. A position that traded through its stop
and recovered by the close is not stopped out. Real drawdowns will be worse
than reported, and `average_r` is reconstructed rather than measured
precisely because no stop was ever placed.

**Margin is not modelled.** A leverage cap stands in. Real CFD accounts post
per-instrument margin and are closed out on a margin *ratio*, not on
drawdown from peak — so a real account would be stopped out earlier and more
often than this simulator is.

**Single currency.** Every instrument is assumed denominated in the account
currency. FX CFDs in particular would need conversion, and the P&L on a
non-USD instrument is currently wrong by the currency move.

**Costs are indicative.** Default spreads are conservative round numbers by
asset class, not broker quotes, and slippage defaults to zero. A strategy
that looks marginal here is probably unprofitable in reality.

**Survivorship and universe selection.** The lake holds whatever was
ingested. A universe chosen today and backtested through 2016 embeds the
knowledge that those instruments still exist and are worth watching.

---

## Testing

```bash
pytest tests/test_backtest_*.py     # 89 tests, no network
```

| File | Covers |
| --- | --- |
| `test_backtest_metrics.py` | Every metric against hand-computed answers; financing attribution |
| `test_backtest_pipeline.py` | Runner, reconciliation, walk-forward, Monte Carlo, BacktestFrame |

The reconciliation tests are the ones that matter: they tie trade-level P&L
back to the equity curve, so an accounting error anywhere in the pipeline
surfaces as a failure here rather than as a plausible-looking number.
