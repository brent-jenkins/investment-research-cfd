# Engineering note — ingestion layer hardening

**Date:** 2026-08-24
**Scope:** Closing the six outstanding ingestion issues. No preprocessing or
signal logic was written.
**Result:** 192 offline tests (4.5s) + 11 live FRED tests (10.5s), all passing.

---

## Summary

Six issues were raised. Four were genuine gaps in the layer, one was a latent
test-harness defect found while profiling, and one was already correct and is
now pinned by tests.

| # | Issue | Outcome |
| --- | --- | --- |
| 1 | Live FRED validation | Validated against the real API; 11 live tests added |
| 2 | Vintage-mode correctness | **Gap** — modes were unnamed and unenforced; now explicit, guarded, exposed |
| 3 | Publication-lag boundaries | **Gap** — publication dates were not stored at all; now captured |
| 4 | Anomaly-flag consistency | **Gap** — flags were logged then discarded; now persisted and readable |
| 5 | Repository contract | Already correct; now pinned by 27 tests |
| 6 | UTC normalisation edges | Mostly correct; hardened for mixed-offset indexes |

---

## What changed, and why

### 1. Live FRED validation confirmed the design — and corrected one assumption

Probing the real API settled how point-in-time sourcing actually works:

```
GDPC1 2019Q4, latest revision      → 20985.448
GDPC1 2019Q4, as of 2020-04-01     → 19221.97     (-8.4%)
```

A backtest reading the first number while simulating April 2020 is trading on
information that did not exist. The gap is large enough to flip a macro-regime
classifier's output, not just perturb it.

The correcting discovery: `output_type=4` (initial release) returns each
observation's **first print stamped with its true publication date**, which is
strictly more useful than a fixed vintage snapshot:

```
UNRATE 2020-01-01 = 3.6   published 2020-02-07
UNRATE 2020-03-01 = 4.4   published 2020-04-03
UNRATE 2020-04-01 = 14.7  published 2020-05-08
```

That is the publication calendar, direct from the vendor — no hand-maintained
lag table needed. **Constraint found:** FRED rejects this mode for series with
more than 2000 vintages, which rules out daily series (DGS10 has 5092). Those
are effectively never revised, so `latest` is correct for them. The cryptic
vendor 400 is now translated into an error that names the fix.

### 2. Vintage mode is now explicit, enforced, and exposed

Previously `realtime_start`/`realtime_end` were forwarded but nothing recorded
what a file *was*. Three named modes now exist — `latest`, `vintage`,
`initial_release` — resolved by one pure function
(`resolve_vintage_mode`). Leaving `vintage_mode` unset derives it from the
realtime bounds, so existing configs behave exactly as before.

`vintage` bounds are forwarded **verbatim** — never widened or defaulted.
`initial_release` widens only the bounds the caller left unset.

**The guardrail that matters:** ingestion now refuses to merge rows fetched
under one mode into a file built under another. Silently appending first-print
rows onto a latest-revision file yields a series that is neither, with no way
to tell which row came from where — a corruption that would surface as
inexplicable backtest results months later. Changing mode requires
`allow_mode_change=True`, which *replaces* the file rather than merging.

Every file's mode is readable via `repo.macro_vintage_mode(series_id)` and
`repo.macro_coverage()`.

### 3. Publication dates are now stored

This was the real gap behind issue 3. Ingestion preserved gaps correctly but
threw away FRED's `realtime_start`, so nothing downstream could tell *when* a
value became public. A `publication_date` column (UTC) is now captured, and
`repo.publication_calendar()` returns it as a `timestamp × series_id` panel
aligned to the value panel.

This makes the look-ahead question checkable rather than a matter of trust. In
`latest` mode FRED stamps every row with today's date — so a publication date
equal to the ingest date is now a *detectable signature* of a biased file.

Confirmed unchanged: ingestion still does not forward-fill, does not resample,
and preserves `.` as NaN.

### 4. Anomaly flags are persisted, not just logged

`validate_ohlcv` computed flag counts and dropped them on the floor. They now
travel in `SymbolResult.anomalies`, land in the market manifest, and surface as
`repo.anomalies(interval)`.

One deliberate refinement: flags are computed against the **merged** file, not
the incremental slice. Validating the slice would have quietly erased every
historical anomaly from the manifest on each incremental run — a bug that would
have made the lake look cleaner the longer it ran. Bars remain stored
unrepaired; a test asserts the inverted bars are still inverted after a
round trip.

Live confirmation: `EURUSD=X` reports 23 `close_outside_range` bars, a known
yfinance FX quirk, recorded and readable rather than silently swallowed.

### 5. Repository contract pinned

No behaviour change; 27 tests now assert what downstream layers may rely on:
tz-aware UTC on every index, canonical lowercase schema, float64 prices,
deterministic ordering independent of request order, anomaly flags, provenance
metadata, and multi-exchange behaviour on disjoint calendars.

### 6. UTC normalisation hardened for mixed offsets

`ensure_utc_index` handled naive and aware indexes but would have degraded on
an **object index holding mixed offsets** — exactly what concatenating two
exchanges produces. A single `pd.to_datetime(..., utc=True)` now covers all
three cases and is simpler than the branch it replaced.

Tests cover US spring-forward and fall-back (both repeated 01:00 local hours
survive as distinct UTC instants — a `tz_localize` would have dropped one),
the US/EU DST offset window, Tokyo/New York alignment, exchange holidays, and
daily/hourly joins.

### Bonus: a test-harness defect found while profiling

`retry_call` bound `sleep: Callable = time.sleep` as a **default argument**,
capturing the function at import time. Every `monkeypatch.setattr(... time.sleep)`
in the retry tests was therefore a no-op, and those tests were performing ~6
seconds of real sleeping while appearing to mock it. Resolving `time.sleep` at
call time fixed both the speed and the deception. The offline suite went from
~11s to 4.5s.

---

## How it was validated

**Live, against the real FRED API** (`pytest -m live`, 11 tests):

- Metadata fetch returns correct title/units/frequency for DGS10.
- Missing values preserved: DGS10 holidays (2020-01-01, 2020-12-25) come back
  as NaN, and the wide panel keeps a row for them — the `pivot_table` defect is
  confirmed dead on real data.
- Vintage mode: GDPC1 2019Q4 = 19221.97 as of 2020-04-01, materially below
  latest.
- Initial release: UNRATE January 2020 = 3.6 published 2020-02-07, with all
  lags between 1 and 60 days.
- Initial release on DGS10 fails with the actionable error, not the vendor's.
- 400 on a bad series id: exactly **one** HTTP attempt (asserted by counting
  requests), confirming permanent errors are not retried.
- Rate limiter: four concurrent series take at least `3 × min_request_interval`.

**429 retry is deliberately not live-tested** — deliberately provoking a real
rate-limit block would be antisocial and could get the key throttled. It is
covered offline with a stubbed 429, asserting exactly `max_retries` attempts.

**Offline** (192 tests, 4.5s, no network): a new `FredStub` dispatches on
`output_type` and `realtime_start` exactly as the live API does, with payloads
transcribed from real responses — so an offline pass exercises the same branch
production takes.

**End-to-end**: `examples/run_ingestion.py` run live in `initial_release` mode
over `EURUSD=X` + UNRATE + CPIAUCSL. Observed publication lags: CPI 38–78 days,
UNRATE 31–80 days. A point-in-time check on 2026-08-10 returned June CPI
(332.568), while a naive observation-date fill would have leaked July (332.813)
— published 2026-08-12, two days *after* the as-of date.

---

## Backward compatibility

- `vintage_mode` defaults to `None` and derives the old behaviour. Existing
  configs are unaffected.
- Files written before this change lack `publication_date`; they still load,
  `publication_calendar()` returns empty rather than raising, and re-ingesting
  in the same mode backfills the column. Covered by three tests.
- Series with no recorded mode report `latest` — what the old code did.
- No public function was removed or had its signature narrowed. `ingest()`
  gained one keyword-only argument with a safe default.

## Known limitations

1. `initial_release` is unavailable for high-vintage daily series. Not a
   defect — those series are not revised — but it means a mixed universe needs
   per-series modes, which the current config expresses per-run. If that
   becomes awkward, per-series overrides are the natural next step.
2. `latest` remains the default. Changing it would silently alter behaviour for
   an existing lake; the warning logged on every `latest` run plus the
   `macro_coverage()` guardrail seemed the better trade. **Worth an explicit
   decision before signals are built.**
3. The as-of join logic lives in tests, not in library code — deliberately, as
   preprocessing is out of scope. `value_available_at` in
   `tests/test_publication_lag.py` is the reference implementation to lift.
