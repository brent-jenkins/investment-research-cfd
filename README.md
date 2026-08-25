# cfdinvest — multi-asset CFD backtesting engine

**Status:** all layers are now built, tested, mutation-checked and
integrated.

Major layers now present:

- T212 broker-aware safety engine
- Refinement layer
- Journal, insights, bias detection
- Simulation mode
- Meta-Strategy layer
- Account registry and router
- Monitoring and risk dashboard
- 21-page Streamlit UI
- Full persistence and scheduler

The Meta-Strategy layer is fully implemented; all refinement inputs exist.

The lake freshness warning appears until ingestion is run; monitoring
reports stale data as a warning.

---

## Quick start

```bash
pip install -e ".[dev]"          # or: pip install -r requirements.txt
cp .env.example .env             # then paste your FRED API key into .env

python examples/run_ingestion.py --tickers SPY GLD ^VIX --start 2024-01-01 --skip-macro
python examples/run_ingestion.py                     # full universe from config.toml
python examples/run_ingestion.py --vintage-mode initial_release   # point-in-time macro

pytest                    # 192 tests, offline, ~4.5s
pytest -m live            # 11 tests against the real FRED API
```

A free FRED key takes about a minute:
<https://fred.stlouisfed.org/docs/api/api_key.html>. The market leg needs no
key at all.

---

## Architecture

```
                config.toml + .env + CFD_* env vars
                              |
                        AppConfig  (frozen dataclasses)
                              |
        +---------------------+---------------------+
        |                                           |
  MarketDataIngestor                        MacroDataIngestor
  (yfinance, batched)                       (FRED REST, thread pool)
        |                                           |
        +---------------- storage.py ---------------+
                   (atomic Parquet upsert)
                              |
                      data/raw/{market,macro}
                              |
                       DataRepository
                              |
              +---------------+---------------+
              |                               |
        tidy DataFrame                  wide panel
     (timestamp, ticker)             timestamp x ticker
              |                               |
        [ preprocessing -> signals -> execution -> analytics ]
                          (next layers)
```

### The layers

| Module | Responsibility |
| --- | --- |
| [config.py](cfdinvest/config.py) | Frozen dataclasses; TOML → env → explicit overrides. No path is ever hardcoded. |
| [schema.py](cfdinvest/schema.py) | The canonical vocabulary: column names, UTC index rule, `IngestionReport`. |
| [storage.py](cfdinvest/storage.py) | Atomic Parquet write/read/upsert. Owns *how* bytes hit disk. |
| [retry.py](cfdinvest/retry.py) | Exponential backoff with full jitter, shared by every network call. |
| [ingestion/market.py](cfdinvest/ingestion/market.py) | yfinance → canonical OHLCV. |
| [ingestion/macro.py](cfdinvest/ingestion/macro.py) | FRED REST → canonical macro series. |
| [repository.py](cfdinvest/repository.py) | The single read API everything downstream depends on. |
| [docs/ingestion-hardening-note.md](docs/ingestion-hardening-note.md) | Engineering note: vintage modes, publication dates, anomaly flags. |
| [docs/publication-lag-note.md](docs/publication-lag-note.md) | Engineering note: the publication_lag default and daily-series fallback. |
| [logging_utils.py](cfdinvest/logging_utils.py) | Handler setup for entry points only; libraries just `getLogger`. |

### Safety rules

Risk posture may request increases but cannot exceed the safety engine's
approved maximum; overrules are reported explicitly.

Broker checks report pass / not checked / unverified; silence is never
treated as safe.

### Design decisions worth knowing

**Everything is UTC, everywhere.** Every stored index is a tz-aware UTC
`DatetimeIndex` named `timestamp`. yfinance hands back tz-naive daily bars and
exchange-local hourly bars; normalising at the boundary kills an entire class
of alignment bug when a backtest joins SPY, the Nikkei and EURUSD on one clock.

**One Parquet file per instrument.** `raw/market/1d/SPY.parquet`,
`raw/macro/DGS10.parquet`. Incremental updates rewrite one small file instead
of a monolith, partial failures stay isolated, and pyarrow still reads a list
of files as one dataset. Writes go to a `.tmp` sibling and are `os.replace`d
into place, so a crash mid-write can never leave a truncated file.

**Incremental by default, with a deliberate overlap.** Each symbol resumes
from its last stored timestamp minus an overlap window (5 days for bars, 45 for
macro) because vendors *revise* recent data. On merge, incoming rows win at
duplicate timestamps. Tickers that share a resume date are batched together, so
adding one new symbol to a 50-asset universe does not trigger a full-history
refetch of the other 49.

**Two read shapes, because backtests need both.**
`load_market()` returns tidy `MultiIndex(timestamp, ticker)` for per-asset
groupby work; `load_panel("close")` returns wide `timestamp x ticker` so a
50-asset momentum screen is one `.pct_change()` call rather than a loop. The
panel path pushes the column projection down into Parquet, so a close-only
load never reads the OHLV columns off disk.

**Ingestion records anomalies; it never silently repairs them.**
`validate_ohlcv` flags inverted bars, non-positive prices, negative volume and
>50% single-bar moves, and writes the rows anyway. Quietly dropping bars at
ingest would hide genuine vendor problems from the layer best placed to judge
them. Counts are persisted to the manifest and readable via
`repo.anomalies("1d")`; on a live run `EURUSD=X` reports 23
`close_outside_range` bars, a known yfinance FX quirk.

Flags are computed against the *merged* file, not the incremental slice —
otherwise each incremental run would erase every historical anomaly from the
manifest and the lake would look cleaner the longer it ran.

**Missing macro observations stay NaN.** FRED's `.` sentinel becomes NaN and
survives to the reader. The gap *is* information; forward-filling requires
knowing each series' publication calendar — which ingestion now *stores*, in
the `publication_date` column, so preprocessing can fill without look-ahead.

**Vintage modes are named, enforced and auditable.** Every macro file records
whether it holds latest revisions, a fixed ALFRED vintage, or first prints.
Modes are never mixed inside one file. See the section below.

**Failures are isolated and reported, not raised.** One dead ticker or a 400
from FRED yields a `SymbolResult(status="failed")` inside the run's
`IngestionReport`; the other 49 symbols still land. Transient failures (429,
5xx, connection resets) retry with jittered backoff; permanent ones (bad series
id, bad API key) fail immediately rather than burning four attempts.

**Purity where it pays.** Every transform — `normalize_yfinance_frame`,
`normalize_fred_observations`, `merge_frames`, `resolve_start_dates`,
`to_panel`, `slice_range` — takes data and returns data with no I/O. Both
ingestors accept an injected downloader/session, so the entire suite runs
offline in about four seconds.

---

## ⚠️ Look-ahead bias in macro data

By default FRED returns the **latest revision** of every observation. This is
not a rounding concern — measured live:

```
GDPC1 2019Q4, latest revision   20985.448
GDPC1 2019Q4, as of 2020-04-01  19221.97     -8.4%
```

A backtest reading the first number while simulating April 2020 is trading on
information that did not exist. Set `vintage_mode` in `[macro]`:

| Mode | What you get | Use for |
| --- | --- | --- |
| **`publication_lag`** (default) | A real publication date on every observation: first prints where FRED serves them, otherwise latest values stamped `observation_date + lag`. | Almost everything |
| `initial_release` | Strictly first prints. Falls back to `publication_lag` when FRED refuses, and warns. | When you want to know if first prints were unavailable |
| `latest` | Newest revision. Look-ahead biased. | Opt in deliberately |
| `vintage` | ALFRED snapshot as of a fixed date. Bounds forwarded verbatim. | Reproducing a specific historical run |

The default gives you the publication calendar straight from the vendor:

```
UNRATE 2020-01-01 = 3.6    published 2020-02-07
UNRATE 2020-03-01 = 4.4    published 2020-04-03
UNRATE 2020-04-01 = 14.7   published 2020-05-08
```

```python
# Tidy calendar across every series -- what preprocessing consumes.
repo.publication_calendar()
#   series_id  observation_date  publication_date  vintage_mode
#   CPIAUCSL   2019-01-01        2019-02-13        publication_lag
#   ...   sorted by (series_id, observation_date), always

# Or the panel form, aligned element-wise with load_macro():
values = repo.load_macro(["UNRATE"])["UNRATE"]
published = repo.publication_calendar(["UNRATE"], wide=True)["UNRATE"]

# What was genuinely knowable on 2020-04-05:
as_of = pd.Timestamp("2020-04-05", tz="UTC")
knowable = values[published <= as_of].iloc[-1]     # 4.4  (March, published 04-03)
leaky = values.dropna().iloc[-1]                   # 14.7 (April, published 05-08!)
```

### The daily-series fallback

FRED refuses first prints for series with more than 2000 vintages — DGS10
has 5092. Rather than failing those series, ingestion refetches the latest
values (which, for an unrevised daily series, *are* the first prints) and
stamps `observation_date + release_lag`. Measured live over 2019–2021:

```
series      mode              lag (min/median/max days)   source
DGS10       publication_lag     1 /   1 /   1             derived (fell back)
UNRATE      publication_lag    31 /  34 /  40             vendor first prints
CPIAUCSL    publication_lag    37 /  41 /  44             vendor first prints
GDPC1       publication_lag   115 / 119 / 121             vendor first prints
```

The fallback is announced in the log and recorded as `fell_back` in the
manifest, and the answer is cached so later runs skip the doomed request.

### Per-series overrides

```toml
[macro.series_overrides.DGS10]
vintage_mode = "latest"     # this series only
release_lag_days = 1
```

### Guardrails

- Every file's mode — the one that *actually* produced it, after any
  fallback — is recorded and readable: `repo.macro_vintage_mode("UNRATE")`,
  `repo.macro_vintage_modes()`, `repo.macro_coverage()`.
- **Modes are never mixed inside one file.** Merging first prints into a
  latest-revision file yields a series that is neither, with no way to tell
  which row came from where. Changing mode requires
  `ingest(allow_mode_change=True)`, which replaces the file.
- In `latest` mode FRED stamps `publication_date` with *today* — so a
  publication date equal to the ingest date is a detectable signature of a
  biased file, not a silent one.

**Upgrading an existing `latest` lake.** The default changed, so the first
ingest after upgrading will be *refused* for series already stored as
`latest` — deliberately, rather than silently mixing vintages. Either pin
them with a per-series override, or re-ingest with
`ingest(allow_mode_change=True)` to replace the files.

---

## Configuration

Resolution order — later wins:

1. Dataclass defaults in [config.py](cfdinvest/config.py)
2. `config/config.toml` (or `$CFD_CONFIG_FILE`)
3. Environment variables / `.env` — `CFD_DATA_ROOT`, `CFD_TICKERS`,
   `CFD_INTERVAL`, `CFD_START`, `FRED_API_KEY`, …
4. An explicit `overrides=` mapping passed to `load_config()`

The API key is **env-only** and is never read from or written to TOML.

```python
from cfdinvest import load_config, DataRepository

config = load_config(overrides={"market": {"interval": "1h"}})
repo = DataRepository(config)

closes = repo.load_panel("close", start="2015-01-01")   # timestamp x ticker
returns = closes.pct_change()
vol = returns.rolling(252).std() * (252 ** 0.5)

bundle = repo.load_bundle()                             # market + macro together
```

---

## Data layout

```
data/
├── raw/
│   ├── market/1d/SPY.parquet          # index: timestamp (UTC)
│   │                                  # cols: open, high, low, close, volume,
│   │                                  #       dividends, stock_splits, ticker
│   └── macro/DGS10.parquet            # cols: value, publication_date,
│                                      #       series_id
└── metadata/
    ├── market_1d_manifest.json        # per-ticker status, coverage, run
    │                                  # params, anomaly flag counts
    ├── macro_manifest.json             # + vintage_mode per series
    └── fred_DGS10.json                # title, units, frequency, last_updated
```

---

## Scaling notes

Sized for 20–50 assets, verified against the live vendor:

- 50 tickers = 2 yfinance requests (`batch_size = 25`), not 50.
- FRED runs on a thread pool behind a shared process-wide rate limiter
  (120 req/min ceiling).
- 20 years of daily bars for 50 assets is roughly 250k rows — a few MB in
  zstd Parquet, trivially in-memory.
- Hourly is where size bites: yfinance serves only ~730 days of intraday
  history, and the ingestor warns when you ask for more.
- No per-row Python loops anywhere in the transform path.

---

## Testing

```bash
pytest              # 267 tests, ~8s, no network
pytest -m live      # 14 tests against the real FRED API (needs a key)
pytest -m ""        # everything
```

| File | Covers |
| --- | --- |
| `test_vintage.py` | Mode resolution, exact parameter forwarding, mode-mixing guardrail |
| `test_publication_lag.py` | No forward-fill, publication dates stored, as-of boundaries |
| `test_contract.py` | What downstream layers may rely on; multi-exchange alignment |
| `test_utc_normalization.py` | DST transitions, holidays, mixed-frequency joins |
| `test_vintage_default.py` | The publication_lag default and per-series overrides |
| `test_fallback.py` | initial_release -> publication_lag fallback |
| `test_publication_calendar.py` | The unified calendar's shape and determinism |
| `test_live_fred.py` | The real API (excluded by default) |

Both ingestors take an injected downloader / `requests.Session`. The FRED stub
in `tests/fred_fixtures.py` dispatches on `output_type` and `realtime_start`
exactly as the live API does, with payloads transcribed from real responses —
so an offline pass exercises the same branch production takes.

The suite has earned its keep repeatedly: it caught an index-alignment bug
that turned every FRED value into NaN, a `pivot_table` call that silently
dropped the missing observations this layer promises to preserve, and a
`retry_call` default argument that made every sleep-mocking test a no-op.

---

## Next layer

Preprocessing: calendar alignment across exchanges, macro forward-fill driven
by `publication_calendar()`, returns, realised volatility, rolling
correlations — reading exclusively through `DataRepository`.

The as-of primitive it needs already exists as a reference implementation:
`value_available_at` in `tests/test_publication_lag.py`.
