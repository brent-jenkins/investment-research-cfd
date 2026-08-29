# cfdinvest

A multi-asset CFD backtesting engine, a **forward-testing shadow book**, and
a manual-trading assistant. It models strategies, measures risk, deals on a
Capital.com **demo** account, and tells you what it did.

It placed its first order on 2026-08-27. Everything below about gates,
naming and demo-before-live stopped being ceremony on that date.

---

## Hard constraints

These are standing instructions from the project owner, not preferences.

- **Trading 212 is never automated.** No T212 endpoints, public or private,
  and no automation of its interface. Trading 212 has no CFD API, so any
  automation would mean driving the web UI, which breaches their terms.

  **This was never a rule, and listing it here as one was a mistake**
  (owner, 2026-08-28). It records a *limitation of what T212 makes
  available*, not a choice about how this project should work. Automation
  was wanted; it simply could not be had, and the manual-trading assistant
  exists because hand-placing trades from its notifications was the only
  route left. Reading it as a principle is what made the rest of the
  project T212-shaped — a venue's missing API became the architecture.

  It still holds, because the API still does not exist. It constrains T212
  and nothing else, and it is not a reason to keep any other component
  named, built or focused around that broker.
- **Execution is opt-in, explicit, and off by default.** A broker client may
  exist; it may not place an order because a config file drifted. Anything
  that can trade says so in its name, is gated on an explicit setting, and
  refuses to act against a live account without one. This constraint stops
  being decorative from 2026-08-27, when the daemon was permitted to place
  orders: it is now the only thing between a configuration mistake and a
  funded account.
- **Automated dealing is permitted on a demo account, and nowhere else
  without a second deliberate act.** Owner's decision, 2026-08-27. The
  Capital.com demo account is the venue; the daemon may open and close
  positions on it unattended. Live requires its own environment variable, in
  different words from the first, so the mistake has to be made twice.
- **The paper book stays a simulation, and stays running.** It must never
  become the thing that places orders — its whole value is being a record
  nobody could have edited after the outcome, and a book that sometimes
  trades is not that. Dealing lives in a separate path that the book neither
  imports nor knows about.

  It keeps running alongside the demo account rather than being replaced by
  it, and its stake stays at a fixed £1,000. Two reasons. The four arms
  answer whether raising the confidence bar helps, and one account can only
  trade one of them. And the book's `c35` arm holds the same intended
  positions as the demo account, so the difference between them measures how
  wrong the simulation's fills and costs are — which nothing else measures.
- **Position size on a real account comes from the balance, not from a
  constant.** The £1,000 stake is the *paper book's*, where a fixed stake is
  the point: the only thing varying between trades should be whether the
  call was right. An account has a balance, a margin requirement and a limit
  on how much of it may be committed, and a book that ignores those is
  unbounded rather than realistic. The dealing path reads the balance from
  the broker and sizes within the risk limits; it refuses rather than
  truncating when there is no headroom.
- **No credentials in code, in logs, or in the repository.** Secrets are read
  from the environment or `/etc/cfdinvest/env`. The dashboard redacts. This
  matters more now than it did: a broker API key and password are not a
  FRED key, and a leak is a funded account rather than a rate limit.
- **Demo before live.** A broker integration is developed and measured
  against the demo environment. Pointing it at a live account is a separate,
  deliberate act.
- **Read-only except for the layer's own writes.** Each layer names exactly
  what it may write in its README.
- **No config mutation.**

Several packages enforce their own isolation structurally — `tests/` parses
their imports and fails the build on `requests`, `urllib`, `selenium`, the
backtest engine, or the scheduler's runner. Those restrictions are
package-scoped and still stand: the analysis, bias, insights and refinement
layers have no business talking to a network whatever the broker situation
is. Keep them passing rather than adjusting them.

**What changed, and when.** Until 2026-08-26 this section forbade broker API
calls and trade execution outright. That constraint existed because Trading
212 cannot be automated, not because automation was unwanted — so it was
contingent, and it lapsed when an IG demo CFD account was opened. It is
replaced rather than deleted, because most of what it protected is
unchanged and the rest matters more once real credentials exist.

On 2026-08-27 the owner permitted **automated dealing on the Capital.com
demo account**. That is a larger step than the last one and it is worth
being plain about what it does and does not change.

It does not make the project a trading system. It makes the demo account the
place the forward test runs, because a broker's own fills are a better
record than our simulation of them — they are confirmed by somebody with no
stake in the answer. The IG episode is the argument: every cost figure this
project held was an estimate until an actual number turned up, and two of
them were wrong by more than a factor of two.

What it does change is that a mistake now has consequences. Every constraint
above that reads as ceremony — the gates, the naming, demo-before-live,
credentials out of the repository — is load-bearing from today. The
protection is no longer that the code cannot trade; it is that the code will
only trade where it has been told to, twice, in different words.

The first use is read-only, and on 2026-08-27 it did what it was for: all
twenty spreads were measured on a Capital.com demo account without placing
an order, because that broker returns bid and offer where IG would not.

**Two figures previously stated here were wrong**, and the measurement is
what exposed them. The estimated spread was described as a per-class
constant of 4bp round trip. `spread_bps` is unset and no asset-class map is
supplied, so the lookup falls through to `default_spread_bps = 5.0` and the
`equity: 2.0` entry in `DEFAULT_SPREAD_BPS` never applies to anything. And
it is 46.6% of modelled cost, not 79%: measured over the whole backtest,
spread 46.6%, slippage 37.3%, financing 12.2%, FX 3.8%.

**A third figure was wrong, and it was the correction above.** This section
said the fall-through charges 10bp round trip, reasoning that 5.0 is a
*half* spread. It charges **5.0bp round trip** — 2.5bp crossed per
transaction. `resolve_spreads` and `DEFAULT_SPREAD_BPS` both document their
values as half-spreads, and `effective_fill_price` then halves them again;
the naming and the arithmetic disagree and the arithmetic is what runs.
Verified two ways on 2026-08-28, through the fill price and through the cost
accounting. Anything comparing a quoted spread against "what the model
charges" must use 5.0bp.

That inverts the conclusion below. Measured against Capital's own quotes on
253,283 hourly bars, the model **undercharges** spread on nineteen of twenty
instruments at the opening hour — the hour the strategy actually deals —
1.85x at the median and 5.8x for INTC. The core-session median is 7.14bp
against 5.0bp charged, ranging from 3.49bp (MSFT) to 28.73bp (INTC).

The older reading, kept because it was measured and is still true of what it
measured: on a single day's live snapshot the median was 5.3bp round trip,
ranging from 2.8bp (META, COST) to 28.6bp (NKE). Compared against the 10bp
that was believed to be charged, the model looked to be overstating
the largest line rather than understating it. Every price checked against
the vendor to within 0.04%, which is what says the instruments resolved
correctly rather than to lookalike listings.

### Before every commit

`.env` holds a live FRED API key and is git-ignored. Verify all three:

```bash
git check-ignore -v .env                          # must be ignored
git diff --cached --name-only | grep -x '\.env'   # must find nothing
KEY=$(grep -oE '[A-Za-z0-9]{32,}' .env | head -1)
git grep -I --cached -l -F -- "$KEY"              # must find nothing
```

The owner's Trading 212 basic-auth password has also been shared in
conversation. It is not in the repository and must not enter it — the Caddy
config reads a bcrypt hash from the environment, and
`tests/test_deployment.py` fails the build if anything password-shaped appears
under `deployment/`.

---

## Commands

```bash
pytest                      # ~2,500 tests, offline, about two minutes
pytest -m live              # hits the real FRED API; excluded by default
pytest tests/test_risk.py -q

python -m streamlit run cfdinvest/ui/app.py --server.address 127.0.0.1
cfdinvest-helper "long SPX500 0.68 entry=4434.8 stop=4390" --balance 2000
cfdinvest-confirm open SPX500 --price 4434.8 --quantity 0.684
cfdinvest-schedule run-once | monitor | risk-snapshot
```

`--server.address 127.0.0.1` is load-bearing in deployment: without it
Streamlit binds every interface and answers past Caddy's basic auth.

---

## The principles every layer follows

These recur because they were each learned from a bug. They are the house
style, and a new layer that departs from one should say why.

**Absence is named, never inferred.** A figure that could not be computed is
reported absent — never defaulted, never zero. "Not measured" and "measured
and fine" are different statements, and only one is reassuring. This is why
`unchecked` exists alongside `pass`, why `BiasProfile` severities stay `None`,
and why an empty rules table reports *unverified* rather than compliant.

**Anything uncomputable caps a verdict.** In the live helper, an unknown
exchange rate or leverage caps the answer at `BORDERLINE`. It never reaches
`SAFE` on incomplete information.

**One owner per threshold.** A limit lives in exactly one config object and
is read from there. `ui/health.py` takes its numbers from
`monitoring.MonitoringThresholds`; the refinement layer reads
`LiveHelperConfig`; several tests parse modules and fail the build on a
redeclared limit. Two copies drift, and the day they drift the banner says
fine while the email says critical.

**Observations are append-only; configuration is rewritten.** Snapshots,
confirmations, journal entries and assessments are appended and never edited —
they describe moments that do not come back. The account registry is the one
exception, because it says what exists *now*.

**Accounting identities are asserted, not assumed.** The P&L components sum to
the total; equity equals initial plus cumulative. Tests check this.

**Every number carries its sample.** Below a floor, no claim is made — the
figures stay available and the *assertion* is withheld. The floor is higher in
`bias/` than in `insights/` because behavioural inference is weaker evidence
and because those severities shrink real positions.

**Cutting is cheaper than raising.** Any single adverse finding may reduce
size. Increasing it requires every requirement clear at once, and the result
is still capped by the safety engine. See `metastrategy/README.md`.

**A posture may request an increase but cannot exceed the safety engine's
approved maximum**; overrules are reported explicitly.

**Broker checks report pass / not checked / unverified**; silence is never
treated as safe.

---

## Testing

Tests are the deliverable, not a formality.

- **Mutation-check every suite.** Reintroduce each property as a bug and
  confirm the test fails. Several real gaps were found this way — tautological
  assertions, unreachable guards, tests that passed either way. Each layer's
  README lists the mutations that were checked.
- **Test the property, not the constant.** Asserting `component.score ==
  UNMEASURED` moves with the constant and proves nothing.
- **Run the real thing on real data.** Most of the serious bugs in this
  project surfaced from a smoke test on `runner_lake`, not from unit tests:
  the categorical Parquet bug, the drawdown denominator, the units mismatch in
  the simulation. Treat a surprising number as a bug.
- **Prose in a wrapped block needs whitespace normalisation** to assert on —
  `" ".join(text.split())`.

---

## Layer map

Read the package README before changing a package; each explains the failure
it exists to prevent.

| | |
| --- | --- |
| `ingestion/` `repository.py` `storage.py` | The lake |
| `preprocessing/` `regimes/` `signals/` | Panels, classifications, targets |
| `execution/` `backtest/` | Fills, spread, slippage, FX, financing, stops |
| `analytics/` | Metrics, attribution, sweeps, walk-forward, Monte Carlo |
| `persist/` | Every artefact store. Account-aware. |
| `schedule/` | Runner, market calendar, notifications |
| `risk/` `monitoring/` | Book-level limits; operational health |
| `livehelper/` | The T212 safety core: intent, safety, sizing, broker rules, refinement |
| `confirm/` `journal/` `insights/` `bias/` | What you did, and what it says |
| `simulate/` `metastrategy/` | Rehearsal; risk posture |
| `accounts/` | Registry and router |
| `ui/` | 5 Streamlit pages. Optional install. |

**Where things are computed once and read everywhere:** expectancy in
`analytics/`, regime labels in `risk.compute_regime_risk`, scheduler status in
`risk.scheduler_status`, fills in `execution/`. Never add a second.

---

## Conventions

- **A README per package**, explaining the failure it prevents, the judgement
  calls, and the mutations checked. Not an API listing.
- **Comments explain *why*.** The codebase's value is largely in the reasons —
  why a stop widens in high volatility, why an outcome is a separate journal
  entry. Match that density.
- **Commit messages are prose**, several paragraphs, explaining the decisions
  and anything found while building. End with the `Co-Authored-By` trailer.
- **Dashboard pages** live in `ui/views/`, numbered `NN_Name.py`, registered
  in `ui/app.py: PAGES` and in `tests/test_ui.py`'s layout list. Spec page
  numbers in older specs have drifted; append rather than renumber.
  **The directory must not be called `pages/`** — Streamlit auto-discovers
  that name and builds its own sidebar from it, which no amount of
  `st.navigation` suppresses; a signed-out visitor saw every page name.
- **Follow the spec's file layout** where it does not conflict with what
  exists; say so when deviating.

---

## Traps that have cost time

- **Heredoc escaping.** `\n` inside a `<<'PY'` heredoc becomes a real newline
  and corrupts f-strings and regexes. Use raw strings, or the Write/Edit tools
  for anything containing escapes. This has bitten repeatedly.
- **A categorical column index does not survive a Parquet round trip.**
  `storage.write_parquet` flattens it; every `positions.parquet` was
  unreadable before that fix.
- **A callable inside the `read_everything` bundle breaks caching.** That
  bundle goes through `st.cache_data`, which pickles what it stores, so a
  lambda in it fails to cache and takes everything else in the bundle with
  it — and the symptom is a *different* page rendering blank, not an error
  where the lambda is. Lazy artefacts go in their own module-level function.
- **Streamlit keeps `sys.modules` across reruns.** A long-running server runs
  new page scripts against old modules. Restart it after changing
  `components.py` or `sources.py`.
- **`Series == pytest.approx(scalar)` does not broadcast.** Use `np.allclose`.
- **`x or fallback` treats a legitimate zero as missing.** This hid a
  zero-bar run from the monitoring layer.
- **The signal frame's `size` is a share of equity**, not a unit count. The
  simulation was a no-op until that conversion was added.
- **Wrap prose in the helper's summary.** The block is 68 columns; unwrapped
  lines have escaped three times.

---

## Current state

All queued layers are built, tested and pushed. The suite is green.

The Meta-Strategy layer is built, tested, mutation-checked and integrated;
all refinement inputs now exist.

The lake freshness warning appears until ingestion is run; monitoring
reports stale data as a warning.

**The broker rules are filled in.** All forty instruments were read off the
Trading 212 app by the owner and recorded in `config/t212_rules.toml`, which
the deploy installs to `~/.config/cfdinvest/t212_rules.toml`. That file is the
single source: it used to be a commented-out template, a gitignored working
copy and a hand-copied live file, which is three ways for one table to go
stale. Every entry names its source, and a test fails the build on one that
does not — the fear that kept the table empty was invented figures that look
measured, and provenance is the answer to it rather than emptiness.

Minimums vary by instrument type and only seven are read so far; the rest
report *unverified*, which is correct rather than a gap.

**Account separation is opt-in.** With `CFD_ACCOUNT_MODE` unset everything
uses the shared root, exactly as before it existed. See
`deployment/README.md` for the migration.

The root `README.md` is out of date — it describes the project as
"ingestion layer complete".

### What changed on 2026-08-29

The day the universe started choosing itself, and a run of safeguards that
turned out not to be running.

**`universe.follow_screen` is on.** The daemon trades the screen's latest
recorded selection rather than `market.tickers`. The constraint that had
blocked this was never really about the screen: acting on the ranking meant
rewriting configuration, which is forbidden, so the answer was to read the
other way round. `market.tickers` is never written, stays the fallback, and
the selection comes from `selections.jsonl` — an observation, appended, as
the house rule requires. `universe/traded.py` is the single owner; a test
fails the build on any scheduler path reading the configured list directly.

**The screen runs daily, not monthly.** Monthly was right while the
selection was only a report — consecutive rankings correlate +0.97 to +0.99
and daily buries the drift among near-identical rows. It became an
instruction as well, and an instruction a month behind its conditions is the
worse problem. Cadence costs nothing in return (daily 74.4% against
monthly's 74.9%). Legibility is handled at the read side: every screen is
recorded and `universe history` collapses unchanged runs.

**A selection can be two days old and belong to a different book.** The
screen's target derives from `len(market.tickers)`, so narrowing the traded
list left a fresh record holding twenty names, ten of them never measured
under the current strategy. Refused when the size does not match, and
refused whole rather than truncated — the first eight of a twenty-name
ranking is not the selection the screen would have made asking for eight.

**Nine positions were stranded on the demo account.** Market detail and the
session calendar were fetched for `market.tickers` while the rows to act on
come from the shadow book, which deliberately holds departed names. So
AAPL, GOOGL, META, MSFT, ORCL, ADBE, XOM, NKE and COST sat open with no
mechanism that could ever close them, refused for "no price" every quarter
hour. The traded list says what may be *opened*; what may be *closed* is
whatever the broker says is open. Intraday ingestion covers held names too,
or the exit would have been decided on bars that stopped arriving.

**The position limit closes nothing.** `max_open_positions` was zeroing
surplus incumbents while its own comment called that letting them run. It
now bounds opening only: the book may sit above the limit and drains as
positions exit on their own terms. This matters because capacity is about
to be computed from the balance, so the limit falls whenever the account
does — and the old behaviour would have liquidated positions in a drawdown.

**Two spread stores, and the screen read the empty one.** `capital_1h` bars
are a research archive nothing on a deployment writes; `record-candidate-
spreads` had been filling `persist.spreads` beside it. The resolver now
prefers the archive and falls through to the recorded observations. That
job also ran at 03:00 UTC — 23:00 in New York — so every candidate reading
was taken with the exchange shut. Moved to 16:00 UTC *and* gated on the
market being open, because fixing the hour did not fix the day.

**Confidence predicts, and the gate is already right.** Entry confidence
separates trips monotonically across five buckets — win rate 33.6% to 67.5%,
mean P&L −£260 to +£796, rank correlation +0.25. It is a product of three
factors rather than a probability, so 0.35 sits near the 23rd percentile of
bars where a view exists. Raising it looked worth +15.5pp on a fixed
universe and is worth +1.6pp on the screened path that actually runs, where
the screen already removes what the gate would. **It stays at 0.35.** The
untested lever is confidence-weighted *sizing*, which keeps the trades
instead of discarding them.

**Universe size has a real optimum near ten**, and it is one of the few
curves this project has produced that survives its own noise floor: 52.5%
at four names rising to 86.4% at ten, then falling to 26.0% at twenty-four,
monotone in both directions and far outside the ±14.6pp that changing only
a rebalance start day can manufacture. Drawdown worsens the whole way, from
−18.5% to −58.5%. Rank *within* the selection still predicts nothing
(ranks 1–4 earned £476,774 against 5–8 at £484,668), so the screen is a
threshold rather than a gradient — it separates worth-trading from not at
around rank ten and does not order the good set.

**Hold across weekends.** Flattening before every non-trading day costs
10.3pp of CAGR (74.4% to 64.1%) for no reduction in drawdown, and forces
275 extra round trips. The return gap is inside the noise floor; the extra
spread bill is arithmetic.

**`max_spread_multiple` is `None`, so no cost gate binds.** The profile
demotion is live but withholds itself until an instrument has 25 spread
readings, which the deployment will not reach until late September.

**A recurring failure, recorded because it happened six times in one day.**
A check written by whoever made the change tends to test the change rather
than the requirement. The profile demotion measured a clean zero and read as
"free" when it had never executed; a calendar assertion was satisfied by the
wrong call site; the position limit's comment contradicted its code; three
fixtures asserted "nothing owed twice a day" while describing a monthly
cadence; a progress monitor greped only for success, so a crash looked
identical to progress; and the spread job's hour was verified while its day
was not. In every case the evidence could not distinguish success from
failure, and was read as success. Prefer functional assertions over
structural ones, and when checking a fix, check the property rather than the
edit.

### What changed on 2026-08-28

A long day of measurement. Most of it produced negative results, and those
are the more useful half — several are things that looked good enough to
ship and did not survive being pushed.

**The universe is eight names, and sector no longer constrains anything.**
`max_per_sector` is off: it was measured to buy nothing it was credited
with. Capping a sector at N names also drops the worst-ranked names in a
crowded sector, and a rank-matched control ignoring sector entirely beat
it, 0.75 against 0.65. Capping sector *weight* — which has no rank effect
to borrow — did nothing at any level. Sweeping the name count directly
gives a monotone curve peaking around eight. **Superseded 2026-08-29**:
measured over a wider range the curve peaks near *ten* and falls away
sharply past twelve; eight sat mid-slope. `max_per_sector` is also back at
five.

**The screen ranks on volatility, and refuses what it cannot afford.**
Weights moved to 0.20 liquidity / 0.70 volatility / 0.10 continuity:
volatility is the only one of the three that predicts next-month movement,
and the rank correlation is monotone in how much weight it carries (0.084
at none, 0.235 at the old 0.35, 0.336 at all of it). A cost gate
(`max_spread_multiple = 2.5`) then refuses instruments above 2.5x the
universe's own median spread, trailing 90 days and never whole-history —
INTC ran 0.85x, 3.63x, 5.02x and 1.45x across four years, so a static
figure bans a name for a period it has already left. The gate became
load-bearing when the ranking moved: ranking harder on movement pulls in
volatile names and volatile names are dear.

**Departing positions run out; they are not liquidated.** A name dropped
from the traded list keeps its signals (`app._with_held`) so it exits on
its own reversal or time stop, while `papertrade_run._open_universe` stops
it being re-entered. Shipped once reading only the confirm layer, which is
empty in this deployment — inert, and the test mocked the very source that
was empty. It reads the paper book across every cohort as well now.

**One owner for the thresholds, and the earnings blackout runs at last.**
`run_paper_pass` carried its own literals — a 1-day time stop, a 14-day
cooldown, a 2-day blackout — and was never passed the configured values, so
the shadow book ran a materially different strategy from the backtest that
justifies it. Worse, the engine's blackout had never once bitten:
`_execution_config` read earnings dates from the OS application-data
directory rather than the lake, found none, and treated that as nothing to
black out. Both fixed; the blackout is 1 day, chosen on risk grounds and
explicitly not on return, which reverses between halves of the span.

**Costs are charged as quoted.** `spread_panel` and `dealing_mask` are back
from `capital-prices-archive`. Per-bar measured spreads cost about nine
points of CAGR against the flat 5bp — not the forty-four the archived note
records, because that was measured on a universe containing INTC at 28.73bp
and SMCI at 41.61bp, both of which the cost gate now refuses.

**`max_open_positions` exists in the engine and nothing configures it.**
Capacity was previously a side effect of the universe size. Separating them
measured better — all 49 eligible with 8 slots returned 13.6% against 7.5%
for a monthly list of eight, ahead in both halves — but the daemon still
keys off `market.tickers`, so pointing it at all 49 is the unfinished half.
**Closed 2026-08-29**: the daemon follows the screen, and the limit now
bounds opening without ever closing a position.

**The honest baseline is 76.3%/yr at −25.8%**, on Capital session bars over
3.65 years with per-bar measured spreads, positive in both halves. Read
`memory` notes before quoting anything else: the 139.8% headline came from
yfinance's 730-day window, and Capital's bars are mid-quotes with tick-count
volume, so no volume or order-flow analysis on them means what it appears to.

**What was measured and rejected.** Recorded because each cost hours and
would otherwise be tried again: spread-timed entry (saves £2-4k of £142k —
the 30-minute open delay already banks it); longer holds and multi-week
holds (monotonically worse, and it is edge decay rather than carry); stops
of every asymmetry (worse than none, monotonically tighter-is-worse);
hysteresis and swap caps on universe turnover (the list already changes only
8.6 times a year); Form 4 insider transactions (real — purchases predict
+1.04% next session at p=0.0009 — but 12.7 events a year on this book, a
frequency mismatch no amount of collection fixes).

**Direction appears genuinely unpredictable here, and that is a finding.**
News, earnings, attention, order-flow proxies and absorption all predict
*how far* an instrument will move and none predicts *which way*. The
magnitude forecast is good — predicted-loud names move 2.18x as far as
predicted-quiet, and 44.6% of nominated top-quintile movers land there
against a 19.6% base rate. Momentum does pay conditionally, 5x better in
predicted-high-volatility names than in general and negative in the quietest
quintile — but it agrees with the engine's own direction 93.3% of the time,
so it describes the existing design rather than extending it. Nothing at
entry separates winners from losers; the edge is structural, winners running
7 bars against losers' 4 at 2.4x the size. Anything that truncates that —
stops, tighter time stops — costs more than it saves.
