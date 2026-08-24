# Preprocessing layer

Turns the raw Parquet lake into aligned, derived panels on a single trading
clock. Reads through `DataRepository`'s public API and **writes nothing** —
a test asserts the lake is byte-identical after a full pass.

```python
from cfdinvest import load_config, DataRepository
from cfdinvest.preprocessing import PreprocessConfig, analysis_frame_from_repository

repo = DataRepository(load_config())
frame = analysis_frame_from_repository(repo, config=PreprocessConfig())

frame.close                        # timestamp x ticker
frame.returns["simple"]
frame.volatility["atr"]
frame.correlations["breakdown"]
frame.macro                        # publication-aware forward-filled
```

---

## The one guarantee that matters

**A macro value enters the feature set on its publication date, never its
observation date.**

April 2020 unemployment (14.7%) describes a period ending 2020-04-30 but was
not published until 2020-05-08. A naive `.reindex(calendar).ffill()` on the
observation date hands a backtest that number a week early — a strategy that
"predicts" the COVID crash.

```python
# What was genuinely knowable on 2020-04-05:
honest = frame.macro["UNRATE"].loc["2020-04-05"]     # 4.4  (March, published 04-03)
# A naive observation-date fill would have given:    # 14.7 (April, published 05-08)
```

`publication_aware_ffill` is the implementation; `value_available_at` is the
scalar form for spot checks and assertions.

### How it works

For each series, order observations by **publication** date, walk that order
keeping the index of the largest *observation* date seen so far, then binary
search each calendar instant into the publication timeline. One sort and one
`searchsorted` per series — no Python loop over timestamps.

Keeping a running best by observation date (rather than just taking the last
publication) makes the result correct when a late revision of an old period
arrives after a newer period's first print.

---

## Modules

| Module | Contents |
| --- | --- |
| `alignment.py` | Trading calendar, OHLCV resampling, publication-aware macro fill |
| `returns.py` | `simple`, `log`, `rolling`, `overnight`, `intraday`, `cumulative` |
| `volatility.py` | `rolling_volatility`, `realized_volatility`, `true_range`, `average_true_range` |
| `correlations.py` | Rolling matrix, average pairwise, breakdown flags |
| `frame.py` | `AnalysisFrame`, `PreprocessConfig`, builders |

Every transform is a pure function on wide `timestamp x ticker` panels. The
only object that holds state is the `AnalysisFrame` collecting the results.

---

## AnalysisFrame

```
prices        {open, high, low, close, volume}     timestamp x ticker
returns       {simple, log, rolling, overnight}    timestamp x ticker
volatility    {rolling, realized, atr}             timestamp x ticker
correlations  {rolling, average, breakdown}        see below
macro         publication-aware forward-filled     timestamp x series_id
regimes       placeholder (empty, carries calendar)
metadata      interval, windows, universe, calendar bounds, provenance
```

Every panel shares one index — the trading calendar — and one column
convention. `correlations["rolling"]` is MultiIndexed `(timestamp, ticker) x
ticker`, one full matrix per bar.

A panel is present only when its inputs are. No open prices → no `overnight`.
No high/low → no `atr`. Fewer than two assets → no correlations at all.

---

## Design decisions worth knowing

**Gaps stay gaps.** Reindexing onto the calendar never forward-fills prices. A
ticker that did not trade on a session gets NaN, not a carried price. What to
do about it is a modelling choice this layer does not make silently.

**A gap never becomes a zero return.** `pct_change(fill_method=None)` is
explicit: pandas' historical default forward-filled prices before
differencing, manufacturing a 0% return across a gap the asset never traded
through.

**Non-positive prices are masked out.** A zero or negative print is a vendor
defect, not a tradable level; feeding one to a logarithm poisons every rolling
window that touches it. Ingestion flags these bars — this is the numerical
guard.

**Three volatility measures, because they answer different questions.**
`rolling` subtracts the mean (dispersion around drift). `realized` does not
(treats drift as risk — the right input to a variance-targeting sizer). `atr`
uses the high/low range, so it sees intrabar movement the other two are blind
to, which is what a CFD stop-loss should be sized against: a stop is triggered
by the low, not by the close.

**Empty resample buckets are dropped.** A week with no sessions produces no
bar, rather than a zero-volume phantom one.

**Correlation breakdown flags are diagnostics, not signals.** Three
independent conditions, reported separately so you can see *why* a bar was
flagged: `elevated` (z-score vs its own trailing baseline), `spike` (absolute
level), `breakdown` (rate of change). The baseline is computed on data
strictly *before* each bar, so a flag never uses the bar it is flagging.

**Pair names are canonical.** `pairwise_correlations` sorts each pair's
members, so a column identifies the relationship rather than the order the
assets arrived in.

---

## Backward compatibility

Macro files written before `publication_date` existed have no such column.
Rather than silently substituting the observation date — which would
reintroduce exactly the look-ahead this layer prevents — the caller must
choose:

```python
PreprocessConfig(missing_publication="error")   # default: raise
PreprocessConfig(missing_publication="lag", fallback_lag_days=1)
```

A series ingested in `latest` mode has every row stamped with the *ingest*
date, so nothing is ever "published" during the backtest window and its
aligned column is correctly all-NaN. That is a baffling result to debug, so
`align_macro` diagnoses it and says so in the log.

---

## Configuration

```python
PreprocessConfig(
    resample_rule=None,             # e.g. "W-MON" to go weekly
    price_field="close",
    rolling_return_window=21,
    volatility_window=21,
    realized_volatility_window=21,
    atr_window=14,
    atr_method="wilder",            # or "sma"
    correlation_window=63,
    periods_per_year=None,          # inferred from bar spacing
    missing_publication="error",
)
```

Annualisation is inferred from the median bar spacing (252 daily, 52 weekly),
because `pandas.infer_freq` returns `None` for any real trading calendar —
weekends and holidays make the spacing irregular by construction.

---

## Testing

```bash
pytest tests/test_preprocessing_*.py     # 162 tests, no network
```

| File | Covers |
| --- | --- |
| `test_preprocessing_alignment.py` | Publication boundaries, resampling, legacy files, latest-mode diagnosis |
| `test_preprocessing_returns.py` | Exact values, NaN discipline, overnight/intraday composition |
| `test_preprocessing_volatility.py` | Closed-form checks, Wilder vs SMA, ATR in price units |
| `test_preprocessing_correlations.py` | Perfect/inverse recovery, breakdown detection, determinism |
| `test_preprocessing_frame.py` | Assembly, purity, and end-to-end from a real Parquet lake |

The frame tests build an actual lake through the ingestion layer's offline
stubs, so the two layers are proven to agree on the contract rather than on a
shared assumption in a fixture.

---

## Next layer

Regime classification fills `AnalysisFrame.regimes`, which today is an empty
frame carrying only the calendar — the shape downstream code can already be
written against.
