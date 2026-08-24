# Engineering note — publication_lag default, fallback, unified calendar

**Date:** 2026-08-24
**Scope:** The three issues left open after the hardening pass. No preprocessing
or signal logic was written.
**Result:** 267 offline tests (8s) + 14 live FRED tests, all passing.

---

## A premise correction up front

The brief described `publication_lag` as an existing mode. It was not. The
layer supported `latest`, `vintage` and `initial_release`. So this note
describes a **new mode being created**, not a default being switched between
existing options — which matters, because the design question ("what should the
default *be*?") was still open rather than already settled.

Defining it was the crux of the work. The obvious reading —
*latest values plus a guessed lag* — is actively dangerous: it launders
look-ahead bias behind a plausible-looking publication date, which is worse
than `latest`, because `latest` at least announces itself (FRED stamps the
ingest date, so bias is detectable). Making *that* the global default would
have been a mistake.

The definition adopted instead:

> **`publication_lag`** — every observation carries a real publication date,
> obtained the best way available for that series: FRED's first prints where it
> serves them, otherwise the latest values stamped
> `observation_date + release_lag`.

This is safe as a global default because of a fact confirmed live: the fallback
can *only* trigger for series that breach FRED's 2000-vintage cap, and only
near-daily series do that — which are precisely the series that are never
revised. The dangerous combination (revised series + synthetic lag) cannot
arise in practice. For a revised series like GDP or CPI, the mode delivers
genuine vendor first prints.

---

## Issue 1 — Default vintage mode

**Changed.** `DEFAULT_VINTAGE_MODE = "publication_lag"`. `vintage_mode` left
unset now resolves to it, except when ALFRED real-time bounds are set, which
still derives `vintage` — so pre-existing vintage configs are untouched.

**Per-series overrides** land as `[macro.series_overrides.<SERIES>]` in TOML,
resolved by a pure `series_settings(settings, series_id)` that returns an
ordinary `MacroDataConfig`. Everything downstream keeps operating on a plain
config object and never learns overrides exist. `release_lag_days` is accepted
as a friendlier per-series spelling of `fallback_release_lag_days`; unknown
keys warn rather than raise, so a typo in one series' table cannot abort a
50-series run.

`_coerce` in `config.py` needed a `dict` branch — without it the override table
would have hit the `str()` fallback and been stringified.

**Mode mixing** was already refused; the guardrail now works per series and
compares against the mode a run will *actually produce*, so a series known to
fall back is not reported as conflicting with the file it wrote last time. The
rejection message now also suggests the per-series pin, which is the cheaper
fix than a full re-ingest.

## Issue 2 — Daily series lacking initial_release

**Changed.** The vintage-cap rejection now raises its own
`FredVintageLimitError` — a recoverable class, distinct from `FredError` — and
`fetch_point_in_time` answers it by refetching in `latest` mode and applying
`apply_synthetic_publication_dates`. The series lands instead of failing.

Three details worth stating:

- **The fallback overwrites FRED's `realtime_start`.** In `latest` mode FRED
  stamps *today*, which is not a publication date at all. Leaving it would have
  quietly poisoned the calendar.
- **Log level reflects intent.** Under `initial_release` (you asked for first
  prints and did not get them) it is a WARNING. Under `publication_lag` (a
  fallback is expected for daily series) it is INFO. Both say the dates are
  DERIVED, not vendor-reported.
- **The answer is cached** as `initial_release_supported` in the manifest, so
  the doomed request costs one round-trip per series ever, not one per run.

`publication_date` semantics are unchanged by the fallback: same column, same
UTC dtype, always strictly after the observation date, gaps still preserved.
Nothing downstream can distinguish the two paths except by reading
`vintage_mode` / `fell_back`, which is the point.

## Issue 3 — Publication calendar exposure

**Changed.** `repo.publication_calendar()` now returns the tidy four-column
form specified — `series_id`, `observation_date`, `publication_date`,
`vintage_mode` — sorted by `(series_id, observation_date)` on a clean
`RangeIndex`.

`vintage_mode` is carried **per row** so a caller can tell, without a second
lookup, whether a date is vendor-reported, derived from a lag, or meaningless
because the series was ingested biased.

> **Breaking change.** This method previously returned the wide
> `timestamp × series_id` panel. That form is still available as
> `publication_calendar(wide=True)` and is what you want when aligning
> element-wise against `load_macro()`. Nine call sites in the existing tests
> were updated.

Determinism is tested against both request order and *ingestion* order — two
lakes built with the series ingested in opposite orders produce byte-identical
calendars.

---

## How it was validated

**Live, against the real FRED API** (`pytest -m live`, 14 tests) and an
end-to-end run over 2019–2021:

```
series      mode              lag min/median/max (days)   source
DGS10       publication_lag     1 /   1 /   1             derived (fell back)
UNRATE      publication_lag    31 /  34 /  40             vendor first prints
CPIAUCSL    publication_lag    37 /  41 /  44             vendor first prints
GDPC1       publication_lag   115 / 119 / 121             vendor first prints
```

DGS10 fell back exactly as designed (FRED reported 5092 vintages against a cap
of 2000); the other three received genuine first prints with their real release
lags. Those lags match the published BLS/BEA calendars — CPI about six weeks
after month end, GDP about four months after quarter end.

**Offline** (267 tests, 8s): `FredStub` now refuses `output_type=4` for a set of
high-vintage series with the vendor's verbatim error message, so the fallback
branch is exercised deterministically and offline tests hit the same code path
production does.

New coverage: `test_vintage_default.py` (24), `test_fallback.py` (24),
`test_publication_calendar.py` (26).

---

## Backward compatibility, and one deliberate break

Compatible: `vintage_mode=None` still derives `vintage` from realtime bounds;
manifests without per-series modes report `latest` (what that code did); files
without `publication_date` still load; `macro_vintage_mode()` prefers the new
per-series value and falls back to the old run-level params.

**The deliberate break:** an existing lake whose series were ingested under the
old `latest` default will have its **next ingest refused**, because the default
now resolves to `publication_lag` and the guardrail will not mix vintages in one
file. This is the correct behaviour — silently appending first-print rows to a
revised-value file would produce a series that is neither — but it *is* a
disruption on upgrade. Two fixes, both documented in the rejection message:
pin the series with a per-series override, or re-ingest with
`allow_mode_change=True` to replace the files.

## Known limitations

1. **The synthetic lag is a single number per series**, not a release calendar.
   Correct for daily rate series (the only ones that use it); it would not be
   correct for a monthly series, which is why nothing steers a monthly series
   onto that path.
2. **`publication_lag` costs one extra failed request** the first time it meets
   a high-vintage series. Cached thereafter. Pinning known-daily series to
   `latest` in `series_overrides` avoids it entirely; `config.toml` shows how.
3. **The as-of join still lives in tests, not library code** —
   `value_available_at` in `tests/test_publication_lag.py` — because
   preprocessing remains out of scope. It is the reference implementation to
   lift.
