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
- **The paper book is gone, and the demo account is the forward record.**
  Owner's decision, 2026-08-30. Removed in full: `advisor/papertrade.py`,
  `advisor/papertrade_run.py`, the `paper-book` job, the arms, the store and
  every page that read it.

  It was kept for two stated reasons and neither survived. The four arms
  were to answer whether raising the confidence bar helps — a question the
  engine now sweeps in three minutes, and the arms were set at 0.50/0.60/0.70,
  all inside the range measured the same day as destructive (0.45 returns
  82.8%, 0.55 returns 36.1%). And the `c35` arm was meant to hold the same
  intended positions as the demo account, so the gap between them would
  measure how wrong the simulation's fills were. It did not hold the same
  positions: the book applied the time stop, cooldown, blackout and
  confidence gate and none of the breakout gate, size hysteresis, decayed
  trim, leverage cap or concentration cap. Measured through the engine, its
  rule set returns 163.7% at −52.8% against the engine's 130.5% at −40.3%.
  So the gap measured code divergence and fill error inseparably, and could
  be read as neither.

  Worse, `trade-capital` mirrored the book, so the **demo account traded
  those rules too**. Every forward figure it produced described a strategy
  nobody chose.

  What replaces it is better evidence, not a substitute: real fills, real
  spreads, real financing, confirmed by a counterparty with no stake in the
  answer. `persist/intent.py` is the single owner of what should be held —
  it reads the engine's own persisted run and expresses positions as units,
  so every gate and cap is already inside them and no caller re-states a
  rule by name. A rule a caller knows by name is a rule that can drift.

  One capability was genuinely lost: the account trades one configuration,
  so forward A/B comparison is gone. That is accepted — the backtest does it
  faster and, since the cost model was corrected, trustworthily.
- **Position size on a real account comes from the balance, not from a
  constant.** The fixed £1,000 stake belonged to the paper book, where it
  was the point: the only thing varying between trades should be whether the
  call was right. That book is gone. An account has a balance, a margin requirement and a limit
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
- **Once a series is being collected, it is not stopped.** Owner's
  instruction, 2026-08-30. The intraday fetch takes the configured
  candidates *union everything already on disk*, so removing a name from
  `universe.candidates` cannot silently stop its collection. A hole cannot
  be backfilled once the vendor's retention window passes, and minute data
  has the shortest retention of any resolution served.

  **The one exception is a series that has genuinely ended** — delisted,
  suspended, or the vendor stopping. Recorded in `metadata/delisted.json`
  and skipped. Marking is deliberate rather than inferred: a holiday, an
  outage and a delisting all look like "no new bars today" from here, and
  guessing would stop collecting a live instrument, which is this rule's
  own failure arrived at from the other side.
- **One engine, and it is the production one.** Owner's instruction,
  2026-08-30. Backtesting, research, sweeps, the paper book and anything
  else that decides what a position should be goes through
  `execution.run_backtest` and expresses its rules as `ExecutionConfig`
  settings the engine reads. A second implementation of a rule is not
  allowed to exist, however convenient — not in a scratch harness, not in
  the shadow book, not in a notebook.

  This is stronger than the older "measure through the engine before
  shipping", and it replaces it as the binding form. That rule permitted
  screening an idea on an overlay so long as it was re-measured before
  adoption, and the permission is what did the damage: three findings in
  one weekend were measured one way and implemented the other, each
  reproducing to the decimal, because a correctly-implemented wrong
  measurement looks exactly like a right one.

  **It reached the dealing path, and that is the serious part.**
  `advisor.papertrade_run` re-implements the time stop, the re-entry
  cooldown, the earnings blackout and the confidence gate — and, more to the
  point, implements *none* of the breakout gate, the size hysteresis, the
  decayed trim, the leverage cap or the concentration cap. Then
  `_command_trade_capital` takes the account's intended positions **from
  that book** rather than from the engine, on the reasoning that the book
  had already applied the rules. It had applied a different set.

  So the demo account has not been trading the strategy any backtest
  describes. Measured through the engine on 2026-08-30, the book's rule set
  returns 163.7% at −52.8% where the engine's returns 130.5% at −40.3%: more
  levered, less risk-controlled, worse risk-adjusted. Every forward figure
  taken from that account measures a strategy nobody chose.

  This is the third form the same root cause took in one weekend — a
  measurement that never became behaviour, a gate silently overwritten by
  another gate, and now a second rule set placing real orders. The first two
  cost wrong numbers. This one placed trades.

  Also open: any research script that computes a position, a fill or a cost
  itself rather than reading one back from a run.

  The paper book is the sharper case because it is the *forward record*.
  Its value is being evidence nobody could edit after the outcome; if it
  runs different code from the engine, a disagreement between book and
  backtest measures code divergence and simulation error mixed together,
  and neither can be recovered from the other. Sharing decision logic does
  not breach "the book neither imports nor knows about" dealing — that
  constraint is about placing orders, and the engine places none.

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

**Measure through the engine before shipping, never through a research
overlay.** A scratch harness that edits the signal frame -- multiplying
``size``, replacing ``direction``, masking entries -- is not measuring the
code that runs. The engine applies its rules *after* ``shift_for_execution``,
*after* the hysteresis band, and with access to ``units``, which the signal
frame does not have. Three findings in one weekend were measured one way and
implemented the other:

* the breakout gate: harness required confirmation where a signal *started*,
  the engine required it on every entry. 78.1% against 63.0%.
* the decayed-position trim: harness halved the signal, which can push it
  under ``entry_size`` and suppress an entry; the engine halves target units
  for positions already held and never affects entry. 77.4% against 73.6%.
* the confidence-sizing family: normalised over signal bars, 18% of which
  can never open a position, so the arm ran more real exposure than the
  baseline it was compared against.

Every one of them reproduced to the decimal across separate harness runs,
which is what a correctly-implemented wrong measurement looks like.
Reproducibility says nothing about whether the right thing was measured.

So overlays are for *screening* an idea cheaply, and nothing may be adopted
on one. Before a finding is shipped it is re-measured with the rule
expressed as ``ExecutionConfig`` settings and applied by the engine -- which
usually means building the engine support first and testing second, the
reverse of the tempting order. Where a parameter cannot be swept that way,
add the field so it can be (see ``decayed_threshold``) rather than reaching
for the signal frame.

**A figure from an invalidated test is retracted everywhere, not just in the
next sentence.** When a measurement turns out to have been wrong -- the arm
was inert, the harness measured a different rule than the code implements,
the correlation was against a quantity containing its own predictor -- the
number does not become "approximate" or "indicative". It becomes *unknown*,
and every place it was written down has to be corrected or deleted.

This is not pedantry. A retracted number that survives in a docstring is
worse than no number, because the next reader has no way to tell it apart
from a measured one, and will build on it. Several figures in this file have
already had to be superseded in place; the ones that did the damage were the
ones quoted confidently somewhere else while the correction lived only in a
conversation.

The rule, in order:

1. **Say what the test actually measured**, not what it was named for.
   "Trending-only breakout returns 78.1%" was true of a harness that gated
   the first bar of a signal run, and false of the engine that gates every
   entry -- which returns 63.0%. Same name, different rule, 15 points apart.
2. **Correct the source, not just the summary.** Docstrings, config
   comments, commit messages that get read as documentation, the state
   section here, and any README that quoted it.
3. **Leave the retraction visible.** Strike the figure and say why it was
   wrong, rather than silently deleting it -- otherwise the same test gets
   run again and the same number gets believed.
4. **Prefer no figure to a stale one.** "Not currently measured" is a
   statement somebody can act on. A wrong number is not.

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

### What changed on 2026-09-10

The day the last look turned out not to be looking, and a permitted action
turned out to jam the dealer. Both were found by measurement rather than by
reading, and the first was found only because a suspiciously perfect number
was treated as a bug.

**THE LAST LOOK COULD NOT REACH ITS OWN PASS MARK, AND THEN COULD NOT MOVE
AT ALL.** Two faults in one component, the second introduced while fixing
the first.

*The original.* `ProvisionalRepository` appended the broker's quote as a new
bar with open, high, low and close all equal -- zero range by construction.
Every range-scaled input read that bar as motionless, which barely moves a
weak signal and collapses a strong one. Measured across the 50-name universe
against the engine's own reading on the same bar:

    engine band   n    mean engine   mean live      gap
    >= 0.75       2        0.816       0.492     -0.324
    0.60-0.75     1        0.650       0.656     +0.006
    0.40-0.60    17        0.469       0.475     +0.006
    < 0.40       26        0.316       0.306     -0.010

Agreement to a hundredth everywhere except at the top, where it collapsed by
a third. **Not one of the 46 could reach 0.75; the highest live reading in
the book was 0.656.** The gate had a pass mark the check could not reach, so
the account could barely open a position -- and the ones that did get
through were the pathological cases where the dilution inverts.

It went unnoticed because the last look was validated at `entry_confidence`
0.35, where a shift of 0.32 rarely crosses the line -- the 18.7% modelled
against 20.7% measured recorded on 2026-09-03 was taken on that scale.
Raising the gate to 0.75 on 2026-09-04 silently moved the mark onto a scale
the check could not reach.

*The one I introduced while fixing it.* The fix updates the bar in progress
instead of appending, and `load_market` then returned on
`if not rows: return frame` -- so once every name took the update branch,
`rows` was empty on every pass and the whole check returned the lake
untouched. **The quote never reached the computation.** Between ab182c6 and
67518a3 the last look approved whatever the engine proposed, which is
fail-open in the one place this project has been careful to keep
fail-closed.

**The measurement that hid it was mine.** The first calibration fed the
lake's own close in as the live price, so the update was a no-op by
construction and every gap read exactly 0.000 -- which reads as a fix
working perfectly. Re-run against real broker quotes it read 0.000 again,
and that was the tell: the quotes differed from the lake on all 50 names, so
zero was impossible. The decisive test is not a value but a response:

    moving every quote by 5%, before the second fix    0 of 50 readings move
    after                                             45 of 50 readings move
                                                      mean absolute move 0.322

The six that do not move read confidence 0.000 and direction flat -- names
with no signal, where there is nothing to re-check and the gate refuses them
anyway. Two of those (CAT, WMT) sit at 0.15 in the engine against 0.00 live,
a real difference at the bottom of the range where nothing can open.

After both fixes, band by band, against real quotes:

    engine band   n    mean engine   mean live      gap
    >= 0.75       1        0.803       0.804     +0.001
    0.60-0.75     1        0.641       0.643     +0.002
    0.40-0.60    20        0.463       0.464     +0.000
    < 0.40       24        0.318       0.308     -0.010

The gate is reachable again: the highest live reading is 0.804 against 0.656
before.

**This is the inert-gate failure again, and it arrived twice in one day
wearing opposite signs.** First a gate so strict nothing could pass, then
one so inert everything did. Both reported plausible numbers.
`tests/test_livecheck_responds.py` therefore asserts the *property* -- two
quotes that differ must produce panels that differ -- rather than a value,
because a test asserting "the close equals the quote" passes against a write
that lands on a throwaway copy.

**THE CONCENTRATION CAP WAS DEFEATED, AND NOTHING NOTICED.** Found at 19:00
on the owner's observation that the day's trades looked like poor choices
made from misinterpreted data. They were, and not in the way either of us
first thought: the names were fine and the SIZING was not.

Read off the account at 19:05, with the cap set to 20% of gross:

    name   legs   notional GBP   share of gross
    SPGI      3          5,275           100.0%

    gross 5,275 = 1.10x equity, comfortably inside max_leverage 3.5

One name, the whole book, against a cap that never fired once.

**Capital nets only OPPOSING trades.** The account is in netting mode --
confirmed by the owner on 2026-09-03 and verified with a real order on
2026-09-04 -- and that means a BUY closes a SELL. It does not mean two
SELLs merge. Same-direction opens on one epic stay separate positions with
separate deal ids:

    17:36:52  SPGI SELL 1.99 @ 414.93
    18:18:36  SPGI SELL 8.64 @ 413.14
    18:40:14  SPGI SELL 8.67 @ 412.13     total 19.30 units

`_command_trade_capital` builds its view as ``{p.epic: ... for p in held}``
-- a dict keyed by epic -- so three legs collapsed to whichever came last.
**Every downstream limit was then measuring 8.67 units where the account
held 19.30**, including the concentration cap, which is why it passed a
book that was 100% one name.

**And the collapse is what CREATED the legs.** Each pass saw a third of the
position, measured it against the target and topped it up again. The three
legs are the bug's own footprints; left running it would have kept adding.

**A close shut one leg**, which is the half that had to be fixed during the
session. `close_position` takes a single deal id, so the flatten before the
bell would have closed 8.67 units and carried 10.63 overnight -- in the
configuration whose entire purpose is not to hold overnight, on a record
that would have said flat. `_other_legs` now closes the rest, each one
recorded so the order store still matches the account.

**It missed by four minutes.** The flatten ran at 19:01:57 under the old
process; the fix restarted the service at 19:05:36. That pass closed TMUS,
HD, LULU and one SPGI leg and left two.

**The sizing half is NOT fixed, deliberately.** The collapsed dict still
under-states what is held. Fixing it changes what the engine believes it
owns, which changes sizing on every pass, and that is a measured change
rather than a third edit to the dealing path minutes before a close.

**What this says about the other limits.** `max_leverage` sums notional
across the dict, so it under-states gross the same way -- it read 1.10x
where the real figure was the same only because SPGI was the only name
left. With several multi-leg positions it would read low and permit more.
`reconcile` compares epics rather than sizes, so it cannot see this at all;
that is recorded on 2026-09-04 as a known blind spot and this is the first
time it has cost anything. Anything in this file that describes the
concentration cap as binding was measured in a backtest, where one
instrument is one position by construction, and has never been tested
against a venue that splits a holding.

**The day's realised loss was not the selection.** -66.62 GBP, entirely two
positions closed within minutes of opening: TGT at eight minutes (a hand
closure, mine, on the owner's instruction -- the short would have been
roughly flat if held: 156.10 against 156.06 at 19:00) and TMUS at twenty
minutes (the engine exiting on a half-filled bar). Measured against the
four names that cleared the gate at 13:00 and were missed, the two that
were taken moved BETTER -- +1.18% against +0.35% held from 13:00. The fault
was holding period and sizing, not choice of name.

**WHICH BAR DOES THE DEALER DECIDE ON? Asked twice, answered wrongly twice,
and the second answer was mine.** The owner's read was "TMUS was a bad
choice, it should never have been chosen". They were right that something
was wrong and right to say so before I had finished measuring -- and the
specific claim turned out not to hold: on completed data TMUS clears at
0.779. What was wrong was the BASIS of the decision, not its answer. The
retraction below is part of the entry rather than a footnote to it.

**RETRACTED WITHIN THE HOUR, and the retraction is the point.** This first
said "TMUS read 0.398, 0.464 and 0.415 across today's three completed
hourly bars", and that 0.415 is not a completed reading. It is the 15:00
bar at thirty-one minutes old, taken from the 15:31 run and mistaken for
the finished value. **Completed, the 15:00 bar reads 0.779, and TMUS clears
the gate legitimately.** So the position the account holds is correct, and
"TMUS should never have been chosen" is NOT supported.

The correct completed sequence, and the same bar read repeatedly as it
filled in:

    completed bars    13:00  0.398    14:00  0.464    15:00  0.779

    run                newest bar   age of that bar   TMUS confidence
    20260910T144601Z   14:00        complete                    0.445
    20260910T151118Z   15:00        11 min                      0.771   <- opened
    20260910T153148Z   15:00        31 min                      0.415   <- exited
    20260910T155248Z   15:00        52 min                      0.770   <- opened again
    20260910T165458Z   15:00        complete                    0.779

**What survives is the churn, and it is enough on its own.** One bar drove
three account actions -- open, exit, re-open -- inside forty minutes, on
readings of 0.771, 0.415 and 0.770 that are the same hour being sampled
before it finished. That the hour happened to END above the gate is luck,
not correctness: at 15:31 the engine exited a position it would have kept
had it waited, paying a spread each way to find out.

Eleven minutes of an hour is not an hour. The decision was invalid however
the bar closed, and a rule that is right by coincidence is the shadow-book
failure with better manners.

I nearly wrote the wrong number into this file permanently, an hour after
writing the section above about doing exactly that. The figure was read
from a run whose newest bar was still in progress -- which is the very
fault the entry is about.

`INTENT_EXECUTION_LAG = 0` was shipped on 2026-09-09 to stop the dealer
acting on an eighteen-hour-old signal -- the TGT trade -- and it does. But
the newest bar is the bar IN PROGRESS, so the two faults are one question
with two wrong answers: lag 1 gives a bar eighteen hours stale at the open,
lag 0 gives a bar eleven minutes old. Every figure this project holds was
measured on completed bars.

**The intent run now stops at the newest bar that has FINISHED.**
`repository.slice_range` is inclusive, so an end of 14:00 keeps the 14:00
bar and drops the partial 15:00. The lag stays at 0, so the dealer acts on
the newest bar it is given, and the page reads the same run -- page and
dealer still agree, which is what the lag change was for. No new threshold
and no new clock: the rule is that a decision uses data that exists.

**The first hour falls out rather than needing a rule**, which is what says
the shape is right. Before 14:00 UTC no bar of the session has finished, so
the newest completed bar is the previous session's last -- and the book is
flattened there, so the intent is "hold nothing" and nothing can open. The
first entry becomes possible when the 13:00 bar completes at 14:00, thirty
minutes after the 13:30 open: the cadence the owner asked for on
2026-09-09, arrived at from the data rather than from `open_delay_minutes`,
which stays at 0 and is now redundant rather than wrong.

**IT SHIPPED INERT TWICE, AND THAT IS THE MORE USEFUL HALF OF THIS ENTRY.**

    16:04  deployed.  The run at 16:13 still had a newest bar of 16:00.
    16:22  deployed.  The run at 16:34 still had a newest bar of 16:00.
    16:45  deployed.  Trimmed.

First, `--interval` defaults to `None`, which does not mean "no bars" but
"whatever `market.interval` says" -- so the trim read `None` on every
scheduled run and took the deliberate "leave it alone rather than guess"
path. Second, `market.interval` is **`capital_1h`**, a LAKE STORE NAME kept
distinct so Capital's hourly bars never collide with yfinance's `1h`;
`pd.Timedelta("capital_1h")` is a ValueError, and the same safe fallback
fired again.

**Every test passed through both**, because every test handed the helper a
frequency string of its own choosing. The live path never passes one. So
the test that matters reads the SHIPPED CONFIGURATION and asserts that
whatever `market.interval` declares resolves to a bar span -- "test the
property, not the constant" applied to a fixture rather than a threshold:
the input has to come from where the live path gets it.

`capital_prices.resolution_for` is the existing single owner of the
store-name mapping and is used rather than a second parser.

Both inert deploys were caught by reading the run on the box, minutes
apart, rather than by trusting a green suite. This is the project's oldest
failure -- a gate reporting success while doing nothing -- committed twice
in ninety minutes by the person writing about it.

**And the deploying itself cost the session.** Each deploy restarts the
daemon, and a restart runs ingestion, macro, earnings and filings before it
can deal again -- about nine minutes. Nine restarts today, and **five
dealing passes ran where roughly nine were due**:

    13:36  opened TGT, refused 4        <- the broken last look
    14:18  NOT TRADING (TGT jam)
    14:46  NOT TRADING (TGT jam)
    15:12  opened TMUS, refused 5       <- an eleven-minute bar
    15:32  closed TMUS, refused 5
    16:14  refused 5
    16:34  refused 5

**Do not deploy during a session.** The fix is worth less than the passes it
costs, unless what is running is actively placing wrong trades -- which was
the case today, and is the only reason these went out.

**What the refusals were, and why they were right.** BLK, HD, HON and SPGI
all cleared the gate at 13:00 and collapsed within the hour -- 0.815 to
0.471, 0.787 to 0.445, 0.763 to 0.398, 0.846 to 0.517. The engine entered
them in simulation at that bar and went on intending them; the account was
jammed and flat, so every later pass tried to OPEN them fresh and the last
look correctly refused. Those trades existed for one pass and were gone by
14:00. The 83% refusal rate on those passes is not the gate misfiring: it
is the account trying to buy yesterday's opportunity.

**A HAND CLOSURE JAMMED THE DEALER FOR A WHOLE SESSION AND NOTHING COULD
CLEAR IT.** The account opened nothing all day and the last look was not
why. Every pass from 13:36 halted on:

    Capital: NOT TRADING -- the account and the record disagree
    - missing: TGT. Opening nothing until a person has looked.

**CORRECTED 2026-09-10, and the correction matters.** This first said the
OWNER closed TGT by hand. They did not, and they said so. Read off the
broker's own activity log rather than assumed the second time:

    13:36:14  WORKING_ORDER  EXECUTED  USER  SELL 28.7  -> POSITION @ 156.10
    13:44:55  WORKING_ORDER  EXECUTED  USER  BUY  28.7  -> closed @ 158.43

    transaction: "Trade closed", -49.47 GBP, 13:44:55 UTC

`source` is USER on every row, so it was neither a broker stop-out nor a
margin close, and the app's own record holds no `close` plan or result for
TGT at all -- so the dealing path never placed one either. **It was closed
by ME, through the API, on the owner's instruction to close it.** Going
round the dealing path instead of through it is what left no record.

So the benign cause here was not a person at a screen. It was the assistant
placing an order by a side channel, and that is a worse habit than the one
originally written down, because it is entirely avoidable: a close the app
places should go through the app.

The **permitted action** the design must survive is still real -- the
owner's stated mode is that the app manages things except for an emergency
closure -- and it is now joined by a second cause with the same signature.
Either way no result row was written, the record went on believing TGT was
held, and the bridge refused to open anything for the rest of the day.

The guard was doing exactly what it says. The fault is that it had no way
back, and that the jam was indistinguishable from routine: one warning line
every fifteen minutes while the book sat flat through a trading session.

**The two directions are not symmetrical and are now treated differently.**
An UNEXPLAINED position is money at risk nobody chose and still halts
everything. A MISSING position is the record being behind the account, and
the account is the truth about what is held.

What makes healing the second safe was checked rather than assumed:
**`placed` is read by `reconcile` and by nothing else in `plan_orders`.**
Every position figure -- sizing, exposure, the leverage and concentration
caps -- comes from `positions`, the broker's own answer. So a record behind
the account cannot produce a wrong order; it can only stop the account
trading. The bridge's docstring claimed a missing position means "the engine
is reasoning about a position that does not exist", and that is **not true
of this code**. The claim is struck.

`persist.orders.record_departure` writes the close that happened, with the
exit price **absent rather than invented** -- this app did not place the
close, and `completed_trips` already skips a close with no level, so an
unknown exit yields no P&L rather than one computed against zero. The row
carries `reconciled: True` so a hand closure can never be read as a close
the app placed, and so the two causes can be counted apart: a rising count
of these is an order path recording fills it is not getting, which is worse
and looks identical from here.

**It worked on the first pass after the deploy, at 15:12 UTC:**

    15:12:01  1 position(s) in the record are not on the account (TGT).
              Recording them as closed
    15:12:04  opened 1, resized 0, closed 0, refused 5, balance 4775.99

    TGT   close    reconciled
    BLK   refused  no longer clears the bar
    HD    refused  no longer clears the bar
    HON   refused  no longer clears the bar
    MRK   refused  no longer clears the bar
    SPGI  refused  no longer clears the bar
    TMUS  open

The session's first position, and the last look discriminating rather than
blocking everything or approving everything.

**What to watch, and it is not reassuring yet.** Five refusals to one open
is 83% on this pass against 18.7% modelled and 20.7% measured in the
account's first week. One pass is not a rate, and the engine's run bar was
14:00 while the pass dealt at 15:12, so an hour of price movement separates
the two readings -- which is the gate doing its job. But this file's own
watch-list says a sustained rate far above 20.7% is the gate misfiring
rather than protecting, and the refusal rate is the first thing to read
tomorrow.

**Left open.** The departure row carries no exit price, because
`record_departure` refuses to invent one. The broker's activity log HAS it
-- 158.43, against the 156.10 open -- so this trip could be paired properly
rather than dropped from `completed_trips`. Reading the close back from the
broker when a position departs is the right fix and is not built; until it
is, an out-of-band close costs the forward record one trip, and the forward
record is the only trustworthy evidence this project holds.

`MIN_SESSION_MINUTES = 30.0` in the maturity guard is still
provisional and unmeasured. And the 2026-09-03 last-look figures -- 18.7%
modelled, 20.7% measured -- describe the 0.35-gate configuration on a check
that has since been shown unable to reach 0.75. They must not be quoted
against the current setup.

### What changed on 2026-09-08

The first live session of the day-trading configuration, and the day the
size curve turned out never to have described this account.

**The account opened its first trade at 15:22 UTC**, and the record of how
it got there is the useful part:

    14:41  BKNG   refused   no longer clears the bar
    15:02  BKNG   refused   no longer clears the bar
    15:22  BKNG   open
    15:22  CMCSA  refused   no longer clears the bar
    15:22  INTC   refused   no longer clears the bar
    15:22  ISRG   refused   no longer clears the bar
    15:22  LLY    refused   no longer clears the bar

That is the last look working: refused twice, then opened when the live
quote still agreed, with four other candidates refused on the same pass.
Earlier refusals were `within the opening window` -- `open_delay_minutes`
correctly declining to deal before 14:00.

**The owner's observation was that nothing was happening, and that the
25-name cap was holding it back. It was, and it is measurable exactly.**
Clearing the 0.75 gate is a property of the instrument and the bar, not of
the universe size -- the screen decides which names are ELIGIBLE and the
gate then decides which of those open. So counting how many of all 100
candidates clear it says what the cap costs, with no alternative history to
simulate:

    bar (UTC)          clears 0.75   inside the 25   refused by the cap
    2026-09-08 13:00            17               6                   11
    2026-09-08 14:00             4               1                    3

**The mechanism is that the screen and the gate select on different
timescales.** The screen ranks on liquidity, volatility and continuity --
slow properties recorded once a day -- while the gate asks whether an
instrument has a signal on this bar. Nothing makes those coincide, so
narrowing the eligible set by the slow criterion discards names the fast one
would have taken. That reframes the size curve: widening buys FEWER MISSED
SIGNALS rather than diversification, which is what the idle rate falling
from 65% of bars at 25 names to 43% at 100 was describing all along.

**Raising the cap to 70 put the account on EIGHT names.** `max_per_sector`
is 5 and there are nine sectors, so the screen can fill at most 38 of the
hundred candidates. It returned 38, `traded_universe` refuses a selection
whose size does not match the intended target -- refuses *whole*, by
design -- and fell back to `market.tickers`. Minutes only, and solely
because the change was verified afterwards; no dealing pass ran in the
window. Reverted to 30, which the screen could deliver.

**THE ROOT CAUSE IS LARGER THAN THE INCIDENT, AND IT INVALIDATES EVERY SIZE
CURVE THIS PROJECT HAS RECORDED.** The screen does not take the top N of its
ranking. It walks the ranking skipping names whose sector is already full,
and reaches further down. Every sweep that produced a size curve took the
raw ranking's first N.

    harness top-30   MU INTC AMD LRCX AMAT PLTR NOW KLAC ORCL QCOM TSLA COIN
                     PANW AVGO ANET SHOP IBM META CRM TXN INTU CAT ADBE MSFT
                     NVDA CSCO AMZN GOOGL UBER BKNG

    screen's real 30 MU INTC AMD PLTR LRCX TSLA CAT LLY BKNG ISRG NKE GS MPC
                     LULU SLB CMCSA MRK TMO TMUS DHR DE COP XOM WMT HON SPGI
                     UPS EOG LMT PGR

**Eight names in common.** The harness held twenty-two technology names; the
account holds a book across nine sectors. Two different strategies wearing
one number, and the error is at every point rather than only at the large
sizes:

    target   raw top-N   screen (cap 5)     error   overlap
        12       82.5%           67.2%    -15.2p     6/12
        16      104.9%           75.9%    -28.9p     6/16
        20      130.5%           89.0%    -41.5p     6/20
        25      159.2%          101.2%    -58.1p     7/25
        30      179.7%          116.0%    -63.7p     8/30

The overlap sits at six to eight names however large the target grows,
because the ranking's head is almost entirely technology: the screen fills
its five-per-sector allowance immediately and then diverges completely.
Asking for more names adds divergence rather than agreement.

**Swept jointly -- the cap and the target together, because the cap decides
which targets are reachable at all:**

    cap      t8     t12     t16     t20     t25     t30     t40     t50     t70    t100
      3   48.9%   61.1%   71.8%   81.5%   94.3%      --      --      --      --      --
      5   52.9%   67.2%   75.9%   89.0%  101.2%  116.0%      --      --      --      --
      8   57.0%   79.9%   94.3%  100.8%  120.2%  137.3%  167.4%  191.0%      --      --
     12   57.0%   82.5%  104.7%  121.4%  129.8%  155.0%  191.6%  226.3%  270.3%      --
   none   57.0%   82.5%  104.9%  130.5%  159.2%  179.7%  240.9%  297.6%  401.3%  490.4%

    reachable   cap 3 -> 26 names   cap 5 -> 38   cap 8 -> 54   cap 12 -> 70

**The uncapped row reproduces the retracted curve to the decimal**, and that
is what identified the error: taking the ranking's first N is precisely what
an uncapped screen does. So the old figures were measuring
`max_per_sector = None` while the account has run 5 throughout. They were
not stale. They never described this account.

**The battery, on four points of the grid:**

    arm                       CAGR   maxDD   lev      H1      H2   2023  2024  2025  2026
    shipped   cap 5 / t30   116.0%    7.2%  0.71    105%    128%   128%   84%  102%  189%
    moderate  cap 8 / t50   191.0%    6.6%  0.89    171%    213%   249%  110%  175%  312%
    loose    cap 12 / t70   270.3%    7.7%  1.06    230%    316%   306%  170%  255%  479%
    extreme  none / t100    490.4%    7.6%  1.31    384%    621%   435%  348%  450% 1131%

    leverage-matched control, against the shipped universe re-run at each
    arm's own mean leverage -- the test that retired `rebalance_threshold`:

    moderate  cap 8 / t50   control 142.3%   arm 191.0%    +48.7pp
    loose    cap 12 / t70   control 164.4%   arm 270.3%   +106.0pp
    extreme  none / t100    control 190.2%   arm 490.4%   +300.3pp

    block bootstrap, 4,000 paths at 20 bars, against the shipped arm's
    MEDIAN path: 99.9%, 100%, 100% of paths ahead.

**`max_per_sector = 8` and `universe.max_target = 50` are adopted.** Every
arm is positive in both halves and in 4 of 4 calendar years; cap 8 / t50 has
the lowest drawdown of any arm at 6.6%; and the differential survives the
leverage control, which is what says the universe is doing the work rather
than the dial.

**The concentration objection was mine and it was wrong.** Raising the cap
widens the book rather than tilting it, because the target rises alongside:

    shipped   cap 5 / t30    30 names, 7 sectors, largest technology 17%
    moderate  cap 8 / t50    50 names, 8 sectors, largest technology 16%
    loose    cap 12 / t70    70 names, 9 sectors, largest technology 17%
    extreme  none / t100    100 names, 9 sectors, largest technology 33%

Only the uncapped corner concentrates -- and there the screen selects all
100 candidates, so it stops being a selection at all and the ranking does
nothing. That is why cap 12 / t70 and none / t100 are not taken despite
measuring better.

**What is NOT established is the drawdown, and it is the one figure here
that should not be believed.** The bootstrap reports P(drawdown > 25%) at
**0.0% across 4,000 paths** with a worst path of 16.3%. Part of that is
real: this file records gaps as 83% of tail losses, and the book now
flattens before the bell, so the channel is gone -- the earlier bootstrap of
the carry-overnight book gave p50 45.4% and p90 59.3%, and collapsing to
6.6% is mechanically what removing overnight exposure does. But a resampling
can only redraw risk its sample contains, and a correlated shock happens
about once in 3.65 years. **Treat 6-9% as what the model can see, not as a
risk estimate.**

**Three structural fixes, so this class of error cannot recur.**
`select_within_sector_cap` now holds the selection rule and both the screen
and any harness call it -- research and production cannot disagree about
what the screen selects. `reachable_size` answers what a sector map and cap
can fill, and `tests/test_reachable_universe.py` fails the build when
`max_target` exceeds it. And `effective_sector_cap` resolves the ambiguity
that caused the incident: `universe.max_per_sector = None` does **not** mean
"no cap", it means "leave `ScreenConfig`'s default of 5 in force". The first
version of the reachability guard read the value literally, concluded a
hundred names were selectable, and waved through the very target that broke
the session. It survived its mutation, which is the only reason it was
found.

**Two judgements of mine were wrong today and both are worth keeping.** I
argued the change should wait for the weekend to protect a clean first week
of forward record; the owner's counter was that a book taking zero trades
produces no forward evidence either way, so there was no clean week to
protect. And I argued against relaxing the sector cap on concentration
grounds, which the composition table above refutes.

**Also fixed, from the owner reading the Instruments page.** It showed
"Instruments held 25 / Chosen by the curve (largest)" beside a caption
saying "peak 70 at 215.7%; 2 size(s) within 8pp: 70, 100". Both halves were
individually correct and the page was false: the metric came from
`resolve_target`, which applies the ceilings, and the caption from
`operating_size` with none. `explain_target` now computes the decision once
and returns the reasoning with the answer, and a test fails the build if the
page recomputes. Writing its mutations found that `max_target` was applied
only on the viability ceiling's success path, so an outage that emptied the
margin store would have bypassed the cap entirely -- correct by coincidence,
since the fallback is smaller than the cap in every shipped configuration.

### Where things stand, 2026-09-08 evening

Supersedes the 2026-09-04 evening block, which was correct when written and
describes a configuration the account ran for one session.

    engine      corrected. No rule decides on a bar's close and fills at its
                open; tests/test_no_intrabar_lookahead.py guards it.
    gate        entry_confidence 0.75
    universe    50 names, chosen BY THE CURVE rather than by a hand-set stop
                for the first time. max_per_sector 8, max_target 50, and
                cap 8 fills 54 so the target is reachable.
    dealing     DAY TRADING. Opens from 30 minutes after the open, stops
                opening 60 minutes before the close, flattens at the bell.
    book        4,780.79 GBP. First trade of the new configuration opened
                15:22 UTC, BKNG, after the last look refused it twice.
    forward     restarts today, again -- the configuration changed twice
                during the session, so the record starts from the close.

**Every performance figure in this file above the 2026-09-08 entry is
measured on a universe the screen cannot select.** That includes the size
curves, and it includes anything that quoted them. The `cap 8` row of the
joint grid is the only measurement this project holds that was taken on
books the screen actually produces, and it has had the battery. Nothing
else has.

**What to watch, in the order it should appear.**

1. *Whether the count of open positions rises.* The whole case for 50 names
   is fewer missed signals -- 65% of bars idle at 25, 43% at 100. If the
   book still sits flat most of the session, the mechanism is not what the
   measurement says it is.
2. *The last look's refusal rate.* Five refusals to one open on the 15:22
   pass. Modelled at 18.7% and measured at 20.7% in the account's first
   week; a sustained rate far above that is the gate misfiring rather than
   protecting.
3. *The win/loss ratio.* Losses ran 2.3x wins under the flawed engine, which
   was the look-ahead's signature. It should invert before any return figure
   means anything.
4. *Returns.* Months. About 47 weeks before a forward result is
   distinguishable from noise, which has not changed.

**Left deliberately undone, in priority order.**

* `position_capacity` caps ELIGIBILITY by how many SIMULTANEOUS positions
  the balance can fund. Those differ by a factor of thirty -- at target 100
  the book holds a median of three. The premise is wrong, not just the
  inputs.
* `risk.capacity.NOISE_FLOOR_PP` is 8.0 while the research floor is 9.3pp.
  Two owners for one threshold.
* The deployment runs **Python 3.11.0rc1**, a release candidate, against a
  suite that passes on 3.14 -- and prod is ahead of dev on pandas, pyarrow,
  streamlit and yfinance. `deployment/RUNBOOK-python-3.14.md` has the plan;
  it is a Saturday job.
* Every remaining parameter is still fitted to the flawed engine: exposure,
  the hysteresis band, the earnings blackout, the breakout lookback,
  conviction sizing, the decayed trim. The gate, the universe and the sector
  cap are the only three re-derived.

### What changed on 2026-09-07

**The owner's question: a book with free cash should not open lower-quality
trades just because it can.** It does not, and checking why turned up
something else.

`_target_units` sizes each position independently -- `signal size x
exposure_per_unit_size x equity / price` -- and nothing normalises across the
book. No slot count divides the equity, nothing scales up to deploy idle
cash, and free cash is never a trigger to open anything. If three names clear
the 0.75 gate the book holds three and the rest stays cash; a fourth eligible
name does not shrink the first three. So the universe target says how many
names may be *considered*, not how many must be held and not how the money is
split. That is also why the size curve rises with universe size: more
eligible names means more independent opportunities found, not the same
capital spread thinner.

**The mirror case was wrong, in two ways, and both were divergences from the
dealing path.** At the ceiling `_apply_leverage_cap` scaled the WHOLE book
proportionally, holdings included, so a trade that just scraped the entry bar
was never refused -- it opened and every existing position was trimmed pro
rata to fund it, conviction-blind, paying spread to shrink the good ones.

    order    bridge.plan_orders defers surplus opens (line 434) and THEN
             applies the gross ceiling (462), so survivors are sized against
             the room the refusals freed. The engine capped and then deferred.
    scope    the bridge shrinks only `open` and `resize` intents, so a held
             position at `none` passes through untouched. The engine scaled
             everything.

**Each comment cited the other as its authority and the order between them
was inverted.** `test_engine_models_dealing.py` could not catch it because
the parameter exists on both sides; only the sequence differed. The engine
moved to match the account, and the bridge's false claim about it is struck.

**The cap fires, and selecting instead of scaling is worth nothing
measurable.** The first detector said the cap never binds -- 0.0% of bars at
the ceiling -- which was the detector being too tight rather than a null.
Counted at the call:

    target   bars   gross cap fires   concentration fires   worst single cut
        25  6,404      44   0.7%          834   13.0%              56.6%
        50  6,404     131   2.0%        1,287   20.1%              69.8%
        70  6,404     194   3.0%        1,534   24.0%              73.0%
       100  6,404     319   5.0%        1,780   27.8%              75.9%

So the dilution is real and severe when it happens -- one bar in a hundred at
25 names, cutting up to 57% of the book -- and rare enough that the aggregate
is noise. Across eleven universe sizes `leverage_cap_selects` moves the
result by -5.3pp to +1.9pp with the sign flipping, every value inside the
9.3pp floor. **No evidence either way.**

`ExecutionConfig.leverage_cap_selects` therefore exists, is applied by the
engine, is tested with six mutations checked, and **defaults to False** --
the behaviour every recorded figure was measured on. It is the right rule and
it is not worth turning on for return. The concentration cap is what actually
binds here, 13% to 28% of bars against the gross cap's 0.7% to 5%.

**The find is the size curve, and it was describing the wrong book.** Its
value at 25 names is 103.8, which is exactly what the same harness reports
for the CARRY-OVERNIGHT arm. The account has flattened at the bell since
2026-09-06, so the table that chooses the universe size was fitted to the
book the account stopped running two days earlier.

> **RETRACTED IN FULL, 2026-09-08.** Both columns below. The diagnosis in
> this entry was right about the carry-overnight book and missed the larger
> fault: BOTH tables were swept over the raw ranking's first N, which is
> what an UNCAPPED screen selects, while the account runs
> `max_per_sector = 5`. The re-measured column reproduces the uncapped row
> of the joint cap/target grid to the decimal. On the shipped cap the
> harness's top thirty and the screen's real thirty share EIGHT names, so
> neither column ever described this account -- at 25 the true figure is
> 101.2%, not 159.2%. See the 2026-09-08 entry. Nothing below this line may
> be quoted.

    target  shipped  re-measured   maxDD  Calmar   lev   cost    worst year
         8     40.5        57.0     7.6%    7.46  0.54  20.8%       +38%
        10     48.4        69.8     7.6%    9.12  0.58  22.4%       +46%
        12     54.5        82.5     7.6%   10.79  0.62  22.8%       +62%
        16     70.8       104.9     7.6%   13.72  0.68  24.2%       +77%
        20     93.1       130.5     7.6%   17.15  0.74  24.2%       +92%
        25    103.8       159.2     7.3%   21.82  0.78  24.2%      +114%
        30    110.7       179.7     7.4%   24.41  0.84  25.8%      +128%
        40    139.4       240.9     6.6%   36.31  0.92  26.9%      +159%
        50    161.7       297.6     6.6%   45.31  1.00  29.4%      +207%
        70    215.7       401.3     6.6%   60.63  1.14  32.0%      +282%
       100    209.7       490.4     7.6%   64.66  1.31  34.7%      +348%

**The shape changed, and that is the consequence.** The old curve peaked at
70 and turned over at 100. This one is monotone across the whole range with
no peak, so "largest within the noise floor" now selects **100 -- the entire
candidate pool** -- where before it selected 70. Drawdown does not pay for
it: 6.6% to 7.6% with no trend. Every size is positive in every year. What
rises is the cost share, 20.8% of gross to 34.7%, and mean leverage, 0.54x to
1.31x.

**Nothing in the return curve argues for a stop any more.** `max_target` is
the only thing that does, which makes it load-bearing where it used to be a
tidy-up. Shipping is unchanged: at a ceiling of 25 both the old and new
curves select 25, so the account holds what it held.

**The viability ceiling still does not bind, and that is what keeps the
constant alive.** It is designed to answer exactly this from the balance and
the broker's minimum deal sizes, and `position_capacity` is optimistic in two
ways that compound -- it sizes against `max_leverage` 3.5x where the book
runs about 1.3x even at a hundred names, and takes the ninetieth-percentile
minimum ticket rather than one it might actually be handed. On the recorded
balance it computes **83 slots**, above every curve entry but 100. So
removing `max_target` today would not hand the decision to the balance; it
would hand it to the curve.

**At 100 names the account could not hold the book at all.** A slot is worth
about 62 against a dearest recorded minimum ticket of 137, so the dear names
could not be opened, let alone sized by conviction. The curve and the account
now disagree by a factor of four to six, and every figure above 25 in
`size_curve` describes a book this balance cannot fund. That is the fifth
time the engine has modelled something the account does not do, and the first
time it has been the *sizing* rather than a gate.

**Two thresholds that should be one.** `risk.capacity.NOISE_FLOOR_PP` is 8.0
and `operating_size` reads it, while the research floor re-derived for the
corrected book is 9.3pp. Both are in use, they disagree, and "one owner per
threshold" says they must not. It changes nothing at a ceiling of 25 -- only
25 is within either floor -- so it is recorded rather than changed blind.

**The engine was blind to the account balance, and now is not.** The owner's
requirement: whether it is 1,000 or 100,000 or 1,000,000, the app should
decide the best approach rather than be told a number. That could not be
answered, because every cost the engine charges is a fraction of notional and
nothing else responded to scale. Measured, with the increments switched off:

    balance        t8      t12      t16      t25      t40      t70     t100
      1,000     57.0%    82.5%   104.9%   159.2%   240.9%   401.3%   490.4%
    100,000     57.0%    82.5%   104.9%   159.2%   240.9%   401.3%   490.4%
  1,000,000     57.0%    82.5%   104.9%   159.2%   240.9%   401.3%   490.4%

Identical to the decimal. A model that cannot tell a thousand pounds from a
million cannot be asked which of them should hold more names.

`ExecutionConfig.min_deal_sizes` closes the bottom end -- the 63 increments
recorded on the account, in units, running 0.001 to 1.0, applied last in the
sizing chain where `bridge._round_down` sits. Down, never up; an open under
one increment is refused; a held position is never closed by the rounding;
a deliberate close survives; an unrecorded increment leaves the name
unconstrained rather than refusing it. Six mutations checked.

**THE TOP END IS STILL BLIND AND CANNOT BE FIXED FROM THIS LAKE.**
`slippage_fraction` is a scalar, so a million-pound order pays the same basis
points as a thousand-pound one. Market impact needs average daily volume, and
Capital's volume field is a tick count about 414x off the real figure. So
100,000 and 1,000,000 read identically above, and that is a statement about
the model rather than about the strategy. Anything about capacity at size is
**not measured** and must not be inferred from these figures.

**RETRACTED THE SAME DAY, and the retraction is the useful part.** Earlier in
this entry: "at 100 names on the recorded balance a slot is worth about 62
against a dearest minimum ticket of 137, so the dear names could not be
opened at all". That divides exposure by the TARGET -- and the target is how
many names the screen may CHOOSE FROM, not how many are held.

    target   flat    p25   med   p75   p90   p99   max   held/eligible
         8  81.5%      1     1     2     2     5     6            12%
        12  76.7%      1     1     2     3     6     9             8%
        16  72.1%      1     1     2     4     7    11             6%
        25  65.2%      1     1     3     5     8    14             4%
        40  56.6%      1     2     3     6    11    19             5%
        70  46.8%      1     2     5     7    15    26             3%
       100  42.6%      2     3     5     9    17    36             3%

**At target 100 the book holds a median of THREE positions and is flat on
42.6% of bars.** The two quantities differ by a factor of thirty, so the slot
arithmetic answered a question nobody asked -- and `position_capacity` has
the same flaw in its premise, capping eligibility by how many simultaneous
positions the balance can fund.

**That table also answers the owner's objection directly.** "Having 100 x
low-value trades open isn't ideal" -- it does not do that. The count already
varies with conditions rather than filling to capacity: p25 of one or two
positions against a p99 of five to seventeen, because the 0.75 gate decides
how many qualify on the bar and nothing normalises across the book. Widening
the universe buys *fewer idle bars* (flat 81.5% down to 42.6%) and genuine
diversification when it matters (the largest position is 100% of gross at the
median below 40 names, and 50% at 100) rather than more simultaneous trades.

**Measured properly, the constraint does not bind at any balance tested.**
The whole-run comparison is misleading here -- a 5,000 book at these rates is
a 500,000 book within two years, so the minimum deal size binds at the start
and then stops mattering. The first six months, at target 100:

    balance   increments   return    ending   active bars   held med   per posn
      1,000     modelled   120.3%     2,203           540          3        447
      1,000      ignored   120.2%     2,202           540          3        455
      5,000     modelled   119.9%    10,997           540          3      2,269
      5,000      ignored   120.2%    11,010           540          3      2,273

Same active bars, same median holding, 0.1pp of return. **Nothing is refused
for being too small, down to a 1,000 balance.**

So `universe.max_target` is not holding back a book the account cannot fund.
It is holding back one nobody has decided to run yet, which is a different
and more honest reason to keep it. The decision is now the owner's on
strategy grounds rather than blocked on arithmetic.

**What remains genuinely unknown before removing it.** The 490.4% at 100
names is one path on one dataset with no battery -- no leverage-matched
control, no block bootstrap, no per-quarter cut -- and the cost share rises
from 20.8% of gross to 34.7% across the range, which is the direction that
has killed every other idea in this file. A monotone curve with no peak is
also exactly what a measurement looks like when the thing that should stop it
is unmodelled, and one such thing is known to be unmodelled here: capacity at
size.

**Left where it is, deliberately.** `leverage_cap_selects` stays False and
`min_deal_sizes` is not wired into `_execution_config` -- both are modelling
switches, and a shipped figure must not move because a new field appeared.
`max_target` stays 25, now on strategy grounds rather than arithmetic: the
balance can fund the wide book, and whether it should run one is a decision
about risk appetite that has not been taken and is not taken here.

### What changed on 2026-09-03

**A stale figure was justifying a live parameter, and correcting it reversed
the claim.** `config.py`, `execution/engine.py` and `config/config.toml` all
said confidence scores **+0.248** rank correlation against realised trip P&L
where "a purpose-built sixteen-feature model managed +0.129" — quoted to
explain why sizing follows the score the strategy already produces rather
than a model fitted to outcomes.

Both numbers were read on the **fill bar**, which is the basis retracted on
2026-08-30. `execution_lag` is 1, so the engine gates on the previous bar's
value, and the gap is not small:

    confidence against trip P&L    decision bar (correct)   +0.023
                                   fill bar (retracted)     +0.160

Sevenfold. The figure had survived the 2026-08-30 retraction because that
retraction was written about *buckets and thresholds* and these two
correlations lived in a docstring nobody re-read — which is precisely the
failure the retraction rule exists to prevent, appearing again in the same
file that states it.

**Rebuilt properly, the model beats confidence rather than halving it.**
Sixteen features on the decision bar, directional ones signed by the side
the engine actually took, walk-forward with the model refitted each quarter
on everything before it and every prediction made from information the
daemon would genuinely have had. Two quarters skipped and said to be
skipped, for a training set under 400 trips; 3,419 trips scored.

    walk-forward, no lookahead      vs P&L   vs return   vs win
    16-feature model               +0.0777     +0.0698   +0.0767
    confidence alone               +0.0202     +0.0145   +0.0415

So the claim was not mis-scaled, it was **backwards**: the model is roughly
four times better against P&L, not half as good.

**It changes nothing, and the reasons are the interesting part.**

*Nothing to filter.* No band of the model's estimate loses money out of
sample. The bottom fifth earns **+1.6M at a 42.7% win rate**:

    band       n   mean p    total P&L   mean P&L   win rate
       1     684    0.415   +1,613,641     +2,359      42.7%
       2     684    0.468   +7,116,477    +10,404      46.3%
       3     683    0.501   +6,833,517    +10,005      49.8%
       4     684    0.538   +6,669,275     +9,750      52.0%
       5     684    0.599   +6,387,677     +9,339      53.4%

This is the same wall every selection idea in this project has hit, reached
by a new route: a better *ranking* is only worth having if some subset is
unprofitable, and none is. It is why raising the entry gate costs money.

*And its skill points the wrong way.* Win rate climbs monotonically 42.7% to
53.4% while **mean P&L falls** from +10,404 to +9,339 across bands two to
five. The model was trained to predict whether a trip wins, and in this book
win rate runs against money above the bottom band — 20% of trips produce all
the profit, so a model that is right more often trades the fat tail away.
That is now the stated reason sizing follows confidence rather than a fitted
classifier, replacing an assumption with a measurement.

**A two-half screen said the opposite, and was wrong for an instructive
reason.** Splitting the trips in half and testing each on a model trained
on the other showed a bottom band **losing −155,385**, which read as the
first losing entry subset this project has found. Half of that construction
is lookahead — H1 trips scored by a model trained on H2 is training on the
future — and the losing band did not survive the walk-forward. Recorded
because the two-half split is the obvious thing to reach for and it
manufactured exactly the finding being looked for.

**The most useful thing to come out of it is independent confirmation of a
lead already recorded.** `hours into session` is the strongest feature in
the model and the most stable — the largest mean coefficient across the walk
and the same sign in **100%** of quarterly refits, alongside `from 12-bar
high` and `signal size`. That is the "skip the first N bars" morning filter
of 2026-09-01, found by a completely different method: there it was a sweep
returning +81.4pp at skip-4 with −8.5pp of drawdown, here it is what a
classifier leans on hardest when asked which entries work.

Two independent routes to one effect is materially better evidence than the
sweep alone, and it does not clear the filter for adoption. The sweep's
objections stand: the return response is not monotone, the skip-2 dip is
unexplained, and a time-of-day rule is exactly what fits to one 3.65-year
period. What has changed is that it is now the **best-supported untested
idea in the project**, and it needs the battery the sizing change got —
leverage control, block bootstrap, per-year and per-quarter cuts, universe
robustness — expressed as `ExecutionConfig` settings and applied by the
engine.

`confidence` itself is a mid-ranking feature in that model, sign-stable in
85% of refits. It predicts, modestly, on the correct bar. It is simply not
the best thing available, which is what the retracted figure claimed.

**The morning filter is closed, and the thing that closed it was a live gate
nothing had written down.** The owner's question did it: if entries pile up
at the boundary bar rather than disappearing, surely some of them should have
been abandoned because conditions changed — a last check before dealing.

That check already existed. `advisor/livecheck.py`, added 2026-08-27 and
wired in as `bridge.plan_orders(recheck=True)`, recomputes the signal against
the broker's live quote standing in as the bar in progress and refuses the
open if the direction no longer matches or confidence no longer clears
`entry_confidence`. It is fail-closed: an instrument absent from the readings
is refused as *not re-checked*, so a broken check opens nothing rather than
approving silently.

**The engine modelled none of it, and this file had no record it existed.**
Third time now — `open_delay_minutes` on 2026-09-01, and the shadow book
before that.

**It bites, and nothing surfaces that it does.** Read off the account's own
order records over its first week, collapsed from 15-minute passes to
instrument-days, because raw rows triple-count a name refused twice before
it opens:

    instrument-days that opened                        23
    instrument-days the last look refused               7
      of those, opened later the same day               1
      blocked outright                                  6   = 20.7%

    reasons: no longer clears the bar 7, no longer a signal 3

No email, no dashboard row, no log line — which is why it looked like nothing
was being rejected. 29 instrument-days fixes nothing about the rate; it
establishes that the gate fires.

**Modelled in the engine, it is worth +140.6pp — and it absorbs the morning
filter entirely.** `ExecutionConfig.entry_recheck` refuses an entry whose
direction no longer matches the decision or whose confidence no longer clears
the bar on the bar being dealt on. Unshifted, entries only, for the same
reason as the opening delay.

                              CAGR    maxDD   trades   Calmar
    recheck off, delay 30   327.1%    45.4%    3,957     7.21
    recheck on,  delay 30   467.7%    44.4%    3,219    10.53

The gate removes **18.7%** of entries in the model against **20.7%** measured
live — and the model *should* refuse slightly less, because live builds a
provisional bar carrying the quote as open, high, low and close while the
engine compares against a completed bar with a real range. A flat provisional
bar dilutes momentum. The two agreeing to two points from opposite directions
of an argument made before the numbers were seen is the strongest
corroboration available here.

    delay   recheck off   recheck on   gain, off   gain, on   absorbed
      120        365.8%       471.2%     +38.7pp     +3.5pp        91%
      240        409.1%       461.3%     +82.1pp     -6.3pp       108%
      300        427.1%       479.8%    +100.0pp    +12.1pp        88%

**88% to 108% absorbed, and every residual is inside the 8pp noise floor.**
So the morning filter was measuring the last look through a keyhole. Refusing
the first N bars only takes signals that persisted through the session; the
last look only takes signals that still stand at the moment of dealing.
**They are one effect**, and the clock is the crude version of it.

`open_delay_minutes` stays at 30 and the morning filter is **not adopted**.
It also explains the thing that could not be explained on 2026-09-03: why
entering a trend five hours later performed better. It was never the later
price. It was the confirmation.

**What was built, and what was deliberately not.** `entry_recheck` exists on
`ExecutionConfig`, defaults to **False**, is applied by the engine, and is
tested with four cases mutation-checked four ways — dropping the direction
test, dropping the confidence test, comparing the fresh signal against
itself, and making the mask inert all fail the suite.

It is **not** wired into `_execution_config`, and that was a reversal made
after wiring it. `_engine_rows` reads the persisted run, so the daemon's live
intents come from the same `_backtest_config` a research run uses. Setting
`entry_recheck` there would apply the engine's filter to live intents and
then the bridge's own live-quote check on top — two sequential filters where
production has one, making the account materially more selective than it is
today. Worse, in the live run the engine's version would compare the last two
*completed* bars, which corresponds to nothing the bridge does.

So the field models the combined live system correctly **in a backtest** and
would misdescribe it in the daemon. Turning it on in production is a separate,
deliberate decision about what the account does, not a modelling correction,
and it is not taken here.

**Consequences for every figure in this file.** Anything measured with
`entry_recheck` off — which is everything above this entry, including the
327.1% baseline — describes a book taking about a fifth more entries than the
account takes, and by the gate's own measurement those are the losing ones.
The honest research baseline for the system as it actually deals is
**467.7% at −44.4%**. The shipped configuration is unchanged and the account's
behaviour is unchanged; what changed is knowing which number describes it.

**The gate is now visible, and finding out why it was not is the useful
part.** The dealing mail was never missing the refusals — it has always
carried a "Not opened" line grouped by reason. It was suppressed: the mail
returns early when a pass places nothing, so a pass where the last look
refused everything sent no mail at all. A gate blocking a fifth of candidate
opens was invisible because of a branch written to keep quiet passes quiet.

`persist.orders` now owns the reading. `LAST_LOOK_REASONS` is the single
definition of which of the bridge's refusals belong to the gate rather than
to a cap — `not re-checked` included, because that is the gate failing to
form an opinion rather than declining a trade, and a run where it dominates
is a broken check that must be distinguishable on sight.
`refusal_summary` collapses passes to instrument-days and reproduces the
manual query on the live record exactly (29 considered, 6 blocked, 1 opened
later). `last_look_refusals_today` reports a name the first time it is
refused on a given day and not again, read from the store rather than from
state, so the mail cannot repeat every fifteen minutes.

The Broker page carries a **Refused before opening** panel, and the mail now
sends when the gate blocked something new even if nothing was placed. Four
mutations checked: counting rows instead of instrument-days, reporting no
record as a quiet gate, reporting every pass instead of once a day, and
counting a cap's refusal as the gate's.

**Superseded on the same page:** the spread tile read "Assumed by the model
10.0 bp". It charges **5.0bp** round trip, retracted in this file on
2026-08-28 and still standing on the page a person reads before dealing.

**The rule that would have caught all three is now enforced, and it found a
fourth immediately.** "Measure through the engine" caught none of these,
because in every case the measuring was fine and the *modelling* was
incomplete. Some of these rules cannot live in the engine at all — the last
look needs a live broker quote, which does not exist at backtest decision
time — so the engine's job is to model them, and nothing obliged it to.

`tests/test_engine_models_dealing.py` now fails the build when a parameter of
`bridge.plan_orders` has no `ExecutionConfig` counterpart. The check is total
rather than selective: **every** parameter must be classified, either mapped
to the engine field that models it or declared not-a-gate with a stated
reason, so a new parameter of any kind fails until somebody has answered "does
the engine model this?" — the question nobody asked three times. The mapping
is resolved against a real `ExecutionConfig`, so a renamed or deleted field
fails here rather than silently un-modelling a rule the account still applies.

Running it found `max_opens_per_pass = 3` unmodelled. `bridge.plan_orders`
opens at most three positions per pass and defers the rest by conviction; it
fired 11 times in the account's first week. `concentration` was flagged by a
first pass and is a false alarm — the engine models it under a different
name.

**And it does not matter, which is the useful half of the result.**

    arm                        CAGR    maxDD   trades   vs no budget
    budget off               467.7%    44.4%    3,219
    3 per bar                479.1%    44.3%    3,199        +11.5pp
    6 per bar                465.6%    44.4%    3,218         -2.1pp
    12 per bar               467.7%    44.4%    3,219         +0.0pp

The reading that matters is the last one. Production runs a dealing pass every
**fifteen minutes** and the engine bar is an **hour**, so a budget of 3 per
pass permits up to 12 opens per bar — and at 12 the gate is *exactly* inert,
identical to no budget on every figure and on all 3,219 trades. Modelling it
at 3 per bar is roughly four times tighter than the account and its +11.5pp
rests on 20 trades out of 3,219, inside the 8pp floor and measuring a
constraint nobody runs.

So the gap was real and the consequence is nil. Recorded at length anyway,
because the cheap version of this entry — "found another unmodelled gate,
worth +11.5pp" — is exactly the shape of the wrong numbers this file keeps
having to retract, and the difference is entirely in noticing that a pass and
a bar are not the same unit.

`ExecutionConfig.max_opens_per_pass` defaults to `None` and is not wired into
`_execution_config`, for the same reason `entry_recheck` is not: the daemon's
live intents come from that config, and applying the budget there would
throttle opens the bridge is already throttling.

**Margin is now read, recorded and bounded — and it was none of those.**
The owner asked whether Capital's variable margin is covered. It was not, in
three separate ways:

* `marginFactor` is fetched per instrument on every dealing pass and was
  **discarded** — written into a snapshot row and never used.
* The dealing path sizes from `balance`, while `available` (what is left
  after committed margin) sat unread beside it on the same object.
* `max_margin_usage_pct = 0.50` exists but belongs to `livehelper`, the
  *manual* helper. Nothing enforced it on the automated path. Utilisation was
  **66.6%** on 2026-09-01 and **44.2%** on 2026-09-03, so the account had
  been past that limit with nothing to notice.

Capital varies its factors — tiered by position size, raised in volatile
conditions and around events — so a factor can rise **under a position that
is already open**, increasing required margin without the book changing at
all. Nothing in the app could see that happen.

**The series had to start before the question could be asked.** The API
serves a current value with no history, so a change that happened yesterday
is gone; `persist/margin.py` records factors when they move plus a daily
heartbeat, and account utilisation every pass. The heartbeat is the point —
without it a gap means "unchanged" or "unobserved" and only one is evidence.
`factor_changes` is the direct answer to whether the broker moves it, and
`usage_summary` reports the sample alongside, because an empty list after a
short run is not an answer. A factor that moves also logs a warning: it is
the mechanism that can force a close.

**The guard fails closed.** `plan_orders` gains `available` and
`max_margin_usage`; an open whose margin would breach the ceiling is refused,
and so is one where headroom is unreadable or the broker quotes no factor. A
margin guard that disables itself when a field is missing reads as protection
while providing none — which is this project's own inert-gate failure wearing
a different hat. Closes are never refused: a book short of margin is exactly
the one that most needs to reduce.

**The engine has a margin model at last, and it is deliberately crude.**
`margin_factors` (per instrument, because the case a constant cannot express
is a factor rising on semiconductors and not on staples), `default_margin_
factor = 0.20` (the FCA retail requirement, a real number rather than a
guess), and `max_margin_usage`. A missing factor falls back rather than to
zero: zero means "requires no margin", which sizes without limit — absence
treated as permission.

It bounds what the book may **commit** and does not simulate a **close-out**,
so it answers "would the account have had room" and still not "would it have
been liquidated". That needs the broker's close-out level and a path
simulation. Every drawdown figure in this file therefore remains an upper
bound on what a funded account would survive.

**`max_margin_usage` is unset**, and turning it on changes what the account
does — at 0.50 it would have refused opens on 2026-09-01. Deliberately not
inherited from `RiskLimits.max_margin_usage_pct`, because that number belongs
to the manual helper and one number answering two questions is how this
project has been caught before.

The new enforcement check earned itself immediately: it failed on `available`
and `max_margin_usage` the moment they were added, before either was wired.

**A units bug, found by reading the account rather than the code.** Capital
returns `marginFactor` as a **percentage** — every one of the eight
instruments reads **20.0**, being 20% and 5x — and `CapitalMarket` documented
that from the start. Both consumers written on 2026-09-03 treated it as a
fraction, so the ceiling would have multiplied a notional by 20.0 instead of
0.20, demanded twenty times the position in margin, and **refused every open**
on an account showing 44% utilisation.

The tests passed, because they were written with the same wrong unit. That is
the third form of this project's oldest failure: a correctly-implemented
wrong measurement, checked against a fixture that agrees with it. It surfaced
only because the account was queried directly to see whether every instrument
quotes a factor at all — the check was about *coverage* and the units fell
out of it.

Converted once, at the boundary: `CapitalMarket.margin_fraction`. No consumer
holds the percentage, the fake in the tests carries the broker's own unit,
and a test asserts the conversion against the real client type so the fake
cannot drift back into agreeing with a bug. `None` stays `None` — an unquoted
requirement is unknown, and the gate refuses rather than assuming.

It cost nothing because the ceiling is unset. Had it been set to the 0.80
that was written into `config.toml` as an illustration, the account would
have stopped opening positions entirely and the cause would have looked like
a margin problem rather than an arithmetic one.

**Resizing is implemented, disabled, and was broken.** Asked whether open
positions can be resized, and whether it would help under a margin squeeze.

The `resize` action exists end to end -- the bridge produces the intent, the
dealing pass places it -- and `rebalance_threshold = 3.0` disables it, so no
resize intent has ever reached the account. Which is fortunate, because the
side came from `target_units` where the size came from the delta. Trimming a
long from 100 units to 60 has a positive target, so it sent **BUY 40** and
took the position to **140**: doubling down in the exact circumstance
somebody had asked it to cut. Armed behind a config flag, and the first
person to lower that threshold would have found it. Fixed to take the side
from the delta, which also handles shorts -- held −100 reducing to −60 is a
delta of +40, and buying back is how a short shrinks.

**Capital has no partial close, and the account is in netting mode.**
`close_position` sends `DELETE /positions/{dealId}` with no size, so it shuts
the position in full; a reduction has to go as an opposing order on the same
epic. The owner confirmed the setting on 2026-09-03: **Normal**, where
"trades in different direction will close existing trade first", as opposed
to Hedging where both would stay open and both would pay overnight funding.

That is the answer that unblocks resizing for margin defence -- an opposing
order genuinely reduces and genuinely releases the margin, rather than
opening a second position and doubling it. It is a fact about the account
that nothing in the repository can tell you, which is why it is written here.

**Verified with a real order on 2026-09-04**, because a setting in a UI and a
behaviour at the venue are different claims. BKNG was held SELL 21.123; an
`Intent` was built exactly as the bridge builds one, the side taken by the
dealing pass's own expression, and `BUY 1.123` was placed at 192.94:

    account before   SELL 21.123        record   SELL 21.123
    account after    SELL 20.000        record   SELL 20.000
    BKNG positions on the account: 1    -- netted, not hedged
    margin released  35.40 against 43.34 predicted (prices moved)

A short being *reduced* is the case the old code got wrong -- target −20 is
negative, so it would have sent SELL and taken the short to −22.246. It sent
BUY. And the record now says SELL 20.0 where before yesterday's fix it would
have said BUY 1.123: wrong size, inverted side, and invisible to `reconcile`,
which compares epics rather than sizes.

**One imperfection, recorded rather than fixed.** After a resize the record's
`deal_id` is the *order's* id, not the surviving position's. Harmless today
because the bridge takes `deal_id` from the broker's own positions when it
builds a close, never from the record -- and a bug the day anything closes
from the record instead.

**And it exposed two more latent bugs in the same disabled path.** Trimming a
long from 100 units to 60 sends SELL 40, and `record_result` stored the
ORDER: `units` from `result.size` and `direction` from `result.direction`. So
the record would have said the position was **40 units SHORT** where the
account held **60 LONG** -- wrong in size and inverted in side.

`reconcile` could not have caught it. It compares which epics are held
against which are recorded, not how much of each, so the divergence would
have been silent while every downstream reader believed it. The result row
now carries `position_units` and `position_direction` -- what the account
holds after the order, computed from the filled size so a partial fill
records what happened rather than what was asked for -- and `open_orders`
prefers them, falling back for rows written before today, where the order and
the position agreed anyway.

**For drift rebalancing it stays rejected**, on the measurement already
recorded: 0.5 returns 178.9% against 254.9%, and 1.0's apparent +11.5pp is
about half exposure with the remainder failing the split (+19.1pp then
+2.6pp). Turnover is what got dearer when spread was measured properly.

**For margin deleveraging the question is genuinely open, and the reason is
uncomfortable.** Every test this project holds against reducing into stress
-- the four automatic pauses, the stops, the weekend flattening, the overnight
exits, the proportional trim -- was run in a backtest with **no margin model
and no close-out**. They measured return in a world where the harm
deleveraging prevents cannot happen. "It costs money to reduce" is true and
does not answer "would the account have survived".

The trim tests are also the wrong shape for this. They paid a premium on
every gap to catch a rare one, and lost by about 2.3x. A margin-triggered
reduction fires only when utilisation actually breaches, is not restored
automatically, and pays spread on the delta rather than on the whole book --
so the "insurance premium exceeds the average payout" argument does not
transfer. It needs its own measurement, and that needs a close-out model the
engine does not have.

**One thing still outstanding.** The last look's docstring measurement — 9.5% refused, refused
trips averaging −0.331% against +0.011% kept, t=+4.63 — dates from
2026-08-27, before the Capital feed switch, the corrected cost model and
conviction sizing. Direction and existence stand; the magnitudes are on a
superseded basis and should not be quoted as current.

### What changed on 2026-09-04

**Overnight profit-taking, the last untested member of its family, and the
owner's conditional version of it survives.** The observation that started it:
a short in AMAT stood at **+141.77** on Thursday's last bar and **+2.43** on
Friday morning, an overnight gap of +18.3 points costing 131 on 7.3 units.
The book went 5,005.75 to 4,809.60 the same night.

The counter-fact is in the same position's own history. The two preceding
nights gapped *favourably* -- -3.06 (+22.37) and -7.70 (+56.25), the largest
single move in its life. Over three nights the overnight exposure netted
-52.4, and Thursday's +56 looked exactly like Friday's -131 the evening
before.

**The threshold alone is monotonically harmful**, exactly as the
holding-period table predicts -- trips held 16+ bars earn 18.7M, so
truncating a winner costs:

    bank at   CAGR     vs baseline
      +1%     95.5%      -372.2pp
      +2%    270.1%      -197.6pp
      +3%    386.1%       -81.5pp
      +5%    437.2%       -30.5pp
      +8%    508.2%       +40.5pp

The +8% arm is not evidence: it fires 74 times in 3,219 trades, its H1 is
-8.0pp, and pushing further gives -13.0, +5.6, -4.1 and +13.3 at
10/12/15/20% -- every one negative in H1 and positive in H2, which is a
period rather than a rule, and at +20% only 8 trades separate it from the
baseline.

**Conditioning it on the trend having stalled reverses the result.** The
owner's actual hypothesis was the combination, not the threshold: bank a gain
only once it has stopped paying. `overnight_profit_stall_bars` requires no
gain over the last N completed bars in the direction held.

    arm                 vs base   trades   H1 diff   H2 diff   maxDD
    +2% / 2 flat        +34.2pp     +275   +26.2pp   +42.2pp   44.1%
    +2% / 3 flat        +56.5pp     +224   +59.4pp   +53.1pp   41.0%
    +3% / 2 flat        +45.0pp     +195   +50.4pp   +39.6pp   43.4%
    +3% / 3 flat        +57.1pp     +153   +19.2pp   +99.1pp   37.5%
    +5% / 2 flat        +54.9pp      +97   +14.5pp   +99.8pp   44.0%
    +5% / 3 flat        +44.6pp      +81   -13.8pp  +111.8pp   41.8%

Positive in both halves for five of the six, drawdown improving rather than
worsening, and mean leverage falling 1.71 to 1.61-1.65 -- the second effect
the owner predicted, since a position banked at the close carries no margin
overnight. `+2% / 3 flat` is the most balanced; `+3% / 3 flat` has the best
drawdown at -6.9pp and Calmar 14.00 against 10.53.

**RETRACTED before it was believed: the first measurement said +165.0pp and
+161.3pp.** That version decided on `close[bar]` while the engine fills at
`open[bar]`, so it used the bar's own outcome to choose an exit at the bar's
start. A rule gated on a price threshold is precisely the kind that benefits
from that, and it was carrying about four-fifths of the effect: +165.0pp
became +34.2pp on the previous completed bar. The corrected rule decides on
`close[bar-1]`, which is what the daemon genuinely has at the last bar of a
session.

**The same flaw exists in the time stop**, which also tests `price_now`
against `entry_price` and fills at the open. It is pre-existing, it is in the
shipped baseline, and it is *not* changed here -- but it means the time
stop's measured contribution is an upper bound, and the parameter this file
calls "the most load-bearing in the book" has never been measured without it.

**Not adopted.** It has passed the split and nothing else. It still needs the
battery conviction sizing and the morning filter got: leverage-matched
control, block bootstrap, per-year and per-quarter cuts, universe robustness.
And one question specific to this rule -- whether it is the **time stop in
disguise**, since a stalled position at 0.29 days is one the time stop would
close shortly anyway, and the two would then be measuring the same exit.


**The battery, run on the owner's push-back.** The response to "it has passed
one test" is to run the others, not to defend the shelf. On `+2% / 3 flat`:

    leverage-matched control   +81.5pp   against exposure 3.5 at the same
                                         mean leverage (1.64 vs 1.61)
    split-half                 +59.4pp / +53.1pp
    block bootstrap            beats the baseline median on 60.7% of paths,
                               worse drawdown on only 26.8%
    per calendar year          +57, +44, +225, **-53**  -- 3 of 4
    universe robustness        +11.0, +56.5, +39.4, +35.9 at targets
                               8/10/12/16 -- 4 of 4
    the time stop in disguise  no

The leverage control is the strongest part and the one that retired
`rebalance_threshold`. Throttling exposure alone *loses*: the baseline at
exposure 3.5 returns 442.6% against 467.7%. The arm returns 524.1% while
carrying less leverage than the baseline, so the +81.5pp differential is the
rule rather than the dial. Conviction sizing's equivalent was +35.8pp.

Drawdown improves rather than being paid: bootstrap p50 41.9% to 37.0%, p90
53.8% to 48.2%, and only 26.8% of paths draw down past the baseline's median
against conviction sizing's 56.3%. This is the first intervention in this
family to improve return, drawdown and leverage at once; every other one lost
on at least one.

**The weakness, stated plainly: 2026 is -53pp**, and it is the most recent
year and the closest to what the account trades now. It is a partial year of
eight months and -53pp against a base of 349% is a 15% relative shortfall
rather than a reversal, but conviction sizing was 3 of 3 and the morning
filter 4 of 4, and this is 3 of 4 with the miss in the worst place. Target 8
is also only +11.0pp, barely past the floor.

`+3% / 3 flat` has the better headline drawdown (37.5%) and is **2 of 4** on
years -- 2023 at -32pp as well as 2026. It is the worse candidate despite
looking better on the single-path figure, which is the distinction between a
realisation and a distribution.

**Not the time stop in disguise**, though the test came out weaker than
intended. `time_stop_days` of 0.29 and 0.50 produce *identical* books,
because the panel is session-restricted: consecutive bars inside a day are an
hour apart and the first bar of the next session is about 0.75 days from the
last of the previous one, so both thresholds fire on exactly the same bar.
The real comparison is therefore two points, not three. At 1.00 the benefit
falls to +17.7pp on a much worse book (186.6%), which is 9.5% in relative
terms against 12.1% at the configured value. A rule doing the time stop's job
would grow more valuable as the time stop slowed; this one does not.

**The confidence version of the owner's idea fails, and the way it fails is
the useful part.** Asked what confidence showed for AMAT into Thursday's
close:

    09-03 13:00   430.50   short   0.885
    09-03 15:00   435.58   short   0.477
    09-03 18:00   435.77   short   0.490
    09-03 19:00   436.01   short   0.490   <- last bar
    09-04 13:00   459.60   flat    0.000   <- after the gap

Confidence at the close was **0.490 against a 0.35 entry gate**, direction
still `short`. An absolute confidence check would not have fired. The signal
died *because of* the gap, not before it -- the same shape as NVDA and AVGO
sitting at the 21st percentile of their own correlation history the day
before they fell 11% together. A shock does not announce itself; it creates
the reading afterwards.

What *was* visible is that confidence halved from 0.885 and then sat flat at
0.477-0.499 for five bars while price sat flat at 435.58-436.01. The warning
was the flatness in both series, not the level in either -- which is the
stall condition and not a confidence threshold. A third variant, **decay from
the peak**, is neither of the two measured and remains untested.

**On AMAT itself the rule fires**: at Thursday's session-end decision bar
(the 18:00 close, 435.77) the short from 455.43 was up 4.32% and had moved
-0.04% over three bars. It would have banked about +141 against the +2.43
the position now shows. It would also have fired on Wednesday, forgoing the
+56.25 favourable gap that followed, and the Friday gap has already largely
reverted -- 459.60 at the open back to about 453.7 -- which is why one
position over three nights argues both ways and the aggregate is what
decides.


**Decay from the peak, confidence delta and flatness: all null, and one of
them points the wrong way.** The owner asked whether a combination of the
three signals anything usable. Screened over **4,211 positions held into a
session close**, with the outcome being the OVERNIGHT LEG ALONE -- that close
to the next session's open, signed by the side held -- rather than trip P&L,
which would contain the gain being measured.

    feature            rho vs the overnight leg
    decay_share                +0.0380      n=3,159
    decay_from_peak            +0.0303
    conf_delta3                +0.0234
    flat3                      +0.0217
    gain                       -0.0114
    conf_decay                 +0.0038
    move3                      -0.0043
    conf                       +0.0067

Nothing above 0.04 on a standard error near 0.016. The mean overnight leg is
-0.0092% and the median -0.0468%, so holding overnight does cost a little,
which was the premise -- but no candidate sorts it.

**Decay from the peak points the OPPOSITE way to the intuition.** Positions
that have given nothing back do *worse* overnight than positions that have
already come off their high:

    decay_share      n     mean gap   share losing
    band 1 (none)  1,264    -0.1061%      52.8%
    band 2           631    +0.0006%      51.7%
    band 3           632    +0.1113%      46.8%
    band 4           632    +0.0508%      49.2%

A position still at its peak is the extended, crowded one, and it is the one
that reverts. A position that has already given back has already reverted.
That is the reverse of "bank it before it fades" and it is worth keeping,
because the intuition is strong and wrong.

`conf_delta3` is the only one pointing the right way, and it is really "rising
confidence is good" rather than "falling confidence is bad": bands 1-4 sit
between -0.031% and -0.048% and only band 5 differs at +0.111%.

**That screen measured the wrong window, and the owner caught it.**
`conf_delta3` reads the three bars ending at the close. AMAT's collapse ran
0.885 to 0.477 between 13:00 and 15:00 and the score then sat at 0.477-0.499
for four hours, so the delta read **+0.008** -- a perfectly stable signal, on
a position that had halved earlier the same day. Re-measured within the
session, the owner's case does appear:

    cut                          n      overnight   next full day   losing
    halved within the session  230       -0.2194%       -0.5504%     50.4%
    halved, and in profit       44       -0.4026%       -1.0629%     50.0%
    everything else          3,139       -0.0532%       -0.0097%     51.5%

Halved-and-in-profit loses 7.6x what everything else does overnight and
clears the 0.24% round trip. **And it fails all three robustness checks**,
in the same pattern that retired the volatility-conditioned gap insurance:

    excluding the worst gap       -0.4026% becomes -0.1276%, below cost
    excluding the worst two       becomes +0.0023% -- holding was profitable
    mean against median           -0.4026% against -0.0502%, a tail
    first half / second half      +0.7437% then -1.5490%, sign reversed

One night carries it: AMD on 2026-07-29, gapping -12.227%. The 230-strong
wider cut reverses too, +0.3198% then -0.7587%. The quintiles were already
incoherent -- `conf_ratio_session` band 3, barely changed at 0.956, is the
worst at -0.215%.

**This is the fourth independent route to one conclusion**, after the
automatic pauses, the overnight exit and the proportional trim: gap losses
are not spread across identifiably risky nights, they are concentrated in a
few unforecastable events, and to catch the one that matters you pay the
premium on all the others. Confidence decay is a real reading and an
unusable one -- 12 events a year, right half the time, profitable only
through a tail.

It also weakens the stall rule further. That rule's stated mechanism is gap
avoidance, and gap avoidance has now failed from four directions.

**And the arithmetic settles it regardless of ranking.** The worst band loses
**0.106%** overnight against **22-26bp** to exit and re-enter. Even perfectly
selected, the premium exceeds the payout -- the same wall the weekend trim,
the overnight exit and the proportional trim all hit.

**This leaves the stall rule working for a reason that is not the stated
one, and that is the important consequence.** `+2% / 3 flat` passed the whole
battery -- +81.5pp against a leverage-matched control, both halves, 4 of 4
universes, better drawdown -- on the hypothesis that it avoids bad overnight
gaps on stalled winners. This screen says flatness does not predict the
overnight leg at all (`move3` rho -0.0043, and its quintiles are
non-monotone). So whatever the +56.5pp is, it is not that.

Candidate explanations, none measured: banking and re-entering resets
`entry_price` and `opened_at`, which changes what the time stop and the
decayed-position trim do afterwards; or it is a composition effect from
freeing a slot; or path and compounding. The earlier "time stop in disguise"
test was weaker than it looked -- `time_stop_days` 0.29 and 0.50 give
identical books -- so that possibility is not closed.

**A rule that works for an unknown reason is the shadow-book failure with
better statistics.** The morning filter looked worth +100pp on a mechanism
that turned out to be the last look showing through. This is the same shape
and it is not adopted until the mechanism is established.


**Why the stall rule works: not for the stated reason, and not for any reason
established.** The rule's whole case was that it avoids bad overnight gaps on
stalled winners. The engine's own five-component decomposition says it does
the opposite:

    component        baseline    +2% / 3 flat        change
    gap           -32,760,664     -46,492,280   -13,731,615
    intraday     +111,499,558    +160,630,876   +49,131,318
    spread        -16,172,507     -24,949,482    -8,776,976
    slippage       -5,353,428      -8,367,239    -3,013,811

**Gap losses get 13.7M WORSE.** The entire gain is intraday, paid for partly
with worse gaps and 8.8M more spread. And **81% of banked positions are
re-opened within four days** -- it does not sit out the night, it closes and
buys the same thing back.

**The reset hypothesis was tested and is also wrong.** Re-entry resets
`entry_price` and `opened_at`, and the time stop measures from entry, so the
obvious explanation was that stalled winners get a fresh clock. That predicts
a large fall in time-stop exits. Measured: 2,278 to **2,356**, a rise of 78.
Signal exits rose 312 to 509, so time stops fall as a *share* of closes from
88.0% to 82.2% while rising in absolute terms. The prediction failed.

**What is established is that the benefit is contingent on the time stop's
exact value, and reverses:**

    time_stop_days   baseline   with rule   difference   relative
            0.29       467.7%      524.1%      +56.5pp     +12.1%
            0.75       467.7%      524.1%      +56.5pp     +12.1%
            1.50        96.7%       86.8%       -9.9pp     -10.3%
            3.00        22.7%        8.7%      -14.0pp     -61.7%

A +56.5pp effect that becomes -14.0pp when an unrelated parameter is loosened
is a **configuration coincidence rather than an edge**. It earns only in the
narrow corner where the time stop is about one session long, which is where
it happens to be set. (0.29 and 0.75 remain identical for the session-grid
reason: the next session's first bar is about 0.75 days from the previous
session's last, so both fire on the same bar. Three distinct points, not
four.)

Remaining untested candidate: re-entry re-sizes a stale position to current
equity and current conviction, which is drift rebalancing achieved by
close-and-reopen rather than by resize -- and `rebalance_threshold = 3.0`
disables the direct version deliberately, on a measurement that used
continuous resizing rather than a session-boundary round trip.

**Not adopted, and the reason is now specific rather than cautious.** It does
the opposite of what it claims, earns through a channel nobody can explain,
and inverts when a neighbouring parameter moves.

**And it points at the more valuable piece of work.** The rule's entire value
sits on top of the time stop -- and the time stop carries the intra-bar
look-ahead found the same day: it tests `price_now` against `entry_price`
while filling at the open. So the parameter this file calls "the most
load-bearing in the book" has a compromised measurement, and anything
conditioned on it, this rule included, cannot be trusted until it is
re-measured cleanly.

**A book-level exposure gap, found while checking the owner's AMAT
observation.** The claim that the book was "partly self-hedged" by holding
long MU against short AMAT was wrong and is retracted. The sizes were 4:1 --
AMAT 7.3 units at 436.01 for 3,183 of notional against MU 0.8 at 958.25 for
767 -- so MU offset **19%** of AMAT's loss, -172 against +33. That is not a
hedge; it is a net short semiconductor position of about 2,416, roughly half
the account's equity, with a small long leg inside it.

The general form is worse. Read on 2026-09-04 the book was **four shorts and
one long**: gross notional 9,800 at 2.05x equity, comfortably inside the 3.5x
cap, and **net directional exposure -8,202, or -1.72x equity**. Nothing in
the system measures net direction. `max_leverage` caps gross,
`concentration` caps one name at a share of gross, `max_per_sector` counts
names. The book may be entirely one-way and no limit notices.

That is not automatically wrong for a trend follower -- being one-way in a
trending market is the design -- but it is unmeasured, and the correlation
work of 2026-08-31 does not cover it. That work concluded a correlation cap
prices what was visible beforehand and beforehand there was nothing to price.
Net directional exposure is not a forecast; it is an accounting fact known at
every pass.

`overnight_profit_take` and `overnight_profit_stall_bars` default to `None`
and `0`, are applied by the engine, and are not wired into
`_execution_config`.






### Where things stand, 2026-09-04 evening

The account restarts from a clean slate on Monday. Everything below is
deployed and verified on the box.

    engine      corrected -- no rule decides on a bar's close and fills at
                its open. tests/test_no_intrabar_lookahead.py guards it.
    gate        entry_confidence 0.75, was 0.35
    universe    25 names from the screen, was 10
    dealing     DAY TRADING. Opens from 30 minutes after the open, stops
                opening 60 minutes before the close, and flattens everything
                at the bell. It carried 94% of its entries overnight before.
    book        FLAT. 4,780.79 GBP, no margin in use, no open positions
    forward     restarts Monday from 4,780.79

**The overnight carry was never a decision.** ``time_stop_days`` of 0.29 is
6.96 hours against a session of about six hours of bars, so a position opened
in the first openable hour could not reach its stop before the close -- it
reached it at the next session, past the gap. That parameter was fitted on
the flawed engine, so nobody ever chose the exposure that cost the AMAT
trade. Flattening is worth +55.4pp with drawdown roughly halved, on LESS
leverage, and every earlier test of it lost only because it competed with an
exit that could see the fill bar.

The 25 names are the first genuinely diversified book this project has
traded: MU, INTC, AMD, PLTR, LRCX, TSLA, CAT, LLY, BKNG, ISRG, NKE, LULU,
GS, SLB, CMCSA, MPC, MRK, TMUS, DHR, TMO, DE, XOM, COP, WMT, HON. Energy,
healthcare, retail, telecoms, financials and industrials, against the
previous eight which were five semiconductors and three others -- the "one
bet wearing several tickers" this file records as the live risk the
concentration cap cannot see. That is the mechanism behind the size curve's
improvement rather than a coincidence of it.

**Week one closed at -4.38%**, on a configuration measured wrongly. It is a
record of the old setup and not a baseline for the new one.

**What to watch, in the order it should appear.**

1. *The win/loss ratio.* Losses ran 2.3x wins all week where the design says
   winners should be larger. That inversion is the live signature of the
   look-ahead -- it made winners big by exiting losers before they fell -- so
   it should correct first, and before any return figure means anything.
2. *Trade frequency.* Roughly three a week at this gate against nine before.
   A fortnight is six trades. Uninformative on returns; resist reading them.
3. *How the book builds.* At 25 names with `max_opens_per_pass = 3` it takes
   several passes to fill, and that interaction is unmeasured. Worth reading
   the first day's order records rather than assuming.
4. *Returns.* Months. The win-rate tripwire needs 50 completed trips, which
   at this rate is well into the new year.

**Left deliberately undone, in priority order.**

* `position_capacity` sizes against `max_leverage` and the 90th-percentile
  minimum ticket. It should use realised leverage and the dearest ticket:
  measured, 70 names would put the dear instruments at 1x their minimum deal
  size where the model requires 4x. Fixing it lets the viability ceiling bind
  on its own and `universe.max_target = 25` can be deleted rather than
  maintained by hand.
* Every remaining parameter is still fitted to the flawed engine: exposure,
  the hysteresis band, the earnings blackout, the breakout lookback,
  conviction sizing, the decayed trim. The gate and the universe are the only
  two re-derived.
* The overnight profit take stays shelved. It measured +7.5pp against a
  9.3pp floor, and the bridge plumbing for it was reverted rather than left
  half-built.
* The 9.3pp floor itself is derived from one method (selection lag) on one
  configuration. Every figure now being compared against it inherits that.

### A working configuration on the corrected engine, and its caveats

**The gate is roughly half where it should be.** Swept on the clean engine,
with `cost/gross` as the criterion because every arm at the shipped gate
loses and CAGR would merely rank them by speed:

    gate    gross      cost   cost/gross   trades     CAGR
    0.35   +98,151   188,605      192.2%    3,366   -47.5%
    0.45  +130,774   190,742      145.9%    2,178   -22.2%
    0.55  +175,807   173,233       98.5%    1,371    +0.7%
    0.65  +274,245   173,990       63.4%      986   +21.0%
    0.75  +512,433   190,482       37.2%      577   +48.4%

Monotone, and **gross rises as well as cost falling** -- selectivity is not
just saving spread, it is removing trades that lost money gross. The gate and
the hysteresis band compound; holding longer does not, and makes things
sharply worse (time stop 2.0 takes the cost share to 385.6%). The direction
is fewer, better, and *not* longer, which is what the owner said short-term
CFD trading should look like.

    gate  band        cost/gross  trades   CAGR   maxDD  Calmar
    0.75  0.02/0.01        37.2%     577  48.4%   13.1%   3.69
    0.75  0.10/0.04        26.4%     243  33.6%    6.0%   5.60
    0.65  0.20/0.08        27.5%     132  16.3%    5.6%   2.91
    0.55  0.10/0.04        42.9%     462  27.3%   22.7%   1.20

**The overnight profit take works here, and only here.** Against the gate
0.75 book:

    arm                    cost/gross    CAGR
    no profit take              37.2%   48.4%
    bank at +2% notional        33.5%   55.9%     +7.5pp
    bank at +5%                 37.3%   47.1%
    bank at +10%                37.5%   46.6%
    +2% after 3 flat            37.0%   48.6%
    +5% after 3 flat            37.0%   48.5%

+2% of notional is +10% return on margin at 5x, which is the threshold the
owner proposed. Two reversals from the flawed engine: the plain threshold was
-198pp there and is +7.5pp here, and the stall condition -- which was
essential before -- now adds nothing. Both follow from the time stop no
longer exiting before falls.

**It is not implementable as written.** The rule fires on the last bar of a
session, which completes only after the market closes, so the daemon would
see it too late to act and would exit a day later -- after the gap it exists
to avoid. That is the same defect as `open_delay_minutes` and the last look:
a rule that works in a backtest and cannot work live. It needs to fire on the
second-to-last bar, or be implemented in the bridge.

**The live account corroborates the correction, from a third direction.**
After a week: equity 4,763.38, -4.73%, 23 trips, 30% wins, mean win +16.27
against mean loss -37.88. The engine's design says winners should be LARGER
than losers -- they run 7 bars against 4 at 2.4x the size -- and live they are
2.3x smaller. That inversion was flagged on 2026-09-04 as unexplained and now
has an explanation: the look-ahead was what made winners big, because it
exited losers before they fell. Remove it and the profile inverts, which is
exactly what the account has done all week. The owner's own read -- "a
strategy struggling to stand still that slips backwards when it fails" -- is
what a 192% cost share feels like from the inside.

**What none of this has yet.** One grid search on one dataset, no battery,
and no noise floor. The 8pp floor was derived for a 300-500% book by varying
the screen's selection lag alone; on a 48% book the equivalent is unknown,
and until it is measured the +7.5pp cannot be called signal.

### The corrected engine: a real edge, and costs twice its size

**Item 2 of 2026-09-04, first pass.** With the look-ahead removed, the
question is not which time stop is best but whether the strategy makes money
at all. It does not, and the reason is specific.

    arm                        gross P&L    total cost        net   cost/gross
    as shipped                   +98,151       188,605    -90,454       192.2%
    no time stop                 +44,370       133,880    -89,510       301.7%
    exposure 1.0                 +48,519       113,665    -65,145       234.3%
    entry gate 0.5              +158,449       189,617    -31,168       119.7%

**Gross P&L is positive in every arm.** The entries predict; the signal is
real. The book then pays between 1.2x and 3x that signal in spread and
slippage. On the flawed engine the cost share was 35%.

**The time stop is now nearly inert**, which is the cleanest confirmation
available that its measured value was the bug:

    time stop   None -46.1%   0.29 -47.5%   0.5 -47.5%
                1.0  -45.9%   2.0  -50.2%   5.0 -48.7%

Four points of spread across the whole range *including removing it*. This
file calls it "the most load-bearing parameter in the book"; corrected, it
barely does anything.

**Stops remain monotonically harmful** -- -75.4% at 1.5 ATR rising to -60.6%
at 4 ATR -- which refutes a prediction made before the numbers were seen. The
reasoning was that the time stop had been doing illegitimately what a stop
does legitimately, so a real stop might finally earn its keep. It does not.
Recorded because the prediction was stated in advance and was wrong.

**Selectivity is the lever, and it reverses a recorded finding.** Raising
`entry_confidence` from 0.35 to 0.5 lifts gross from +98,151 to +158,449
while cost stays flat, taking the cost share from 192% to 120% and cutting
the net loss by two thirds. This file records the opposite -- "+1.6pp at 0.40
and -12pp at 0.45, the gate stays at 0.35" -- measured on the flawed engine,
where an exit that could see the bar rescued marginal trades and taking more
of them was nearly free. Without it, trade selection is what matters.

**The target for item 2 is therefore explicit: get cost below 100% of
gross.** It is 119.7% at the best arm so far, so roughly a fifth of the way
to go, from higher selectivity, fewer names, longer holds or lower turnover.
None of those has been swept on a clean engine.

**What the account should do, decided 2026-09-04: keep running, unchanged.**
Not because the configuration is good -- it is now believed net-negative --
but because the corrected engine makes a falsifiable prediction about it and
the account is the only instrument that can test it. If the account makes
money over the coming months, the corrected engine is wrong and everything
above needs revisiting. That reframes the forward record from "how is the
strategy doing" to "is the simulator telling the truth", which is the more
valuable question and cannot be answered any other way. It costs nothing: the
account is demo and no capital is at risk. Stopping it would destroy the test
and gain nothing.

### The state of every performance figure, as of 2026-09-04

**CONFIRMED 2026-09-04 by direct measurement.** The signed open-to-close
move, read from the trade log against the raw price panel:

    population                          n        mean      median
    bars a position was simply held  24,282   +0.1010%   +0.0624%
    bars the TIME STOP exited on      2,278   -0.9199%   -0.7948%
    difference                                -1.0209%   per exit

On an ordinary held bar a position gains 0.10%; on a time-stop exit bar it
loses 0.92%. The median is -0.79%, so half of all time-stop exits land on
bars that fall more than three-quarters of a percent -- a bias, not a tail.
No rule of the form "held long enough and has not made money" can select bars
that are about to fall 1% unless it uses the fall. Spread-free by
construction, reading raw open and close only, so the
``entry_price``-includes-spread confound is excluded.

**The live account never had this.** In production the engine runs over
completed bars and the daemon deals during the following one; filling at the
open of a bar whose close is already known exists only in the backtest. So
the account has been trading the honest version throughout, and its
roughly-flat first week is consistent with the corrected expectation rather
than with the advertised one.

**Do not quote a CAGR from this file.** The
time stop decides on the fill bar's close and fills at its open, it is 88% of
all exits, and correcting it takes the engine from +467.7% to -47.5%. That is
one afternoon's finding and one implementation of "correct", so the magnitude
is unverified -- but the *direction* is not in question, because judging on a
bar's close and filling at its open cannot understate performance.

**What is now uncertain**

* Every CAGR, drawdown, Calmar and trade-count figure in this file: 467.7%,
  327.1%, 307.6%, 254.9%, 130.5%, 76.3%, and every arm compared against them.
* Every parameter chosen by a sweep, which is nearly all of them --
  ``entry_confidence`` 0.35, ``confidence_sizing_strength`` 0.75, the screen
  target of 10, ``exposure_per_unit_size`` 4.0, ``time_stop_days`` 0.29,
  ``earnings_blackout_days`` 1, ``breakout_lookback`` 12. They were fitted
  against an engine that lets its dominant exit see the future.
* Every rejection measured the same way: the profit-take thresholds, the
  overnight exits, the weekend flattening, the proportional trim,
  ``rebalance_threshold``, the morning filter. The evidence against them came
  from the same simulator as the evidence for the strategy.

**What is unaffected**, because it is read from data or from the account
rather than produced by the simulator's P&L:

* The live account's own record: 6 of 29 candidate opens blocked by the last
  look, the margin factors at 20.0, netting confirmed by a real resize, the
  order and position record now agreeing.
* Structural measurements over the price data: the median position at a
  session close is **45.5% of equity** and the largest decile 70.1%; AMAT
  peaked at +5.48% and never reached 8%; 7.4% of trips ever reach +5% at a
  close; four of eleven traded names have **zero** overnight quotes while
  seven have nine bars a night.
* The cost arithmetic, which is a ratio and survives any rescaling: an
  overnight profit-take saves 7.64% of equity at its best threshold and pays
  17.45% in spread, losing by about 2.3x -- the same ratio the weekend trim
  lost by. Two nights carry the entire saving and the median night in the
  cohort *gains* 0.020%.

**What follows from that.** The forward record on the demo account is now the
only trustworthy evidence this project has, because it is real fills at real
spreads confirmed by a counterparty. It is also about eleven months from
being distinguishable from noise, which was true before today and matters
much more now.

The account's behaviour is unchanged and nothing has been adopted. What has
changed is that the number justifying it is in question.

**The one measurement that would settle it** is an isolated synthetic case
where the correct answer is known by construction -- a handful of bars, one
position, a time stop that must or must not fire -- rather than a difference
between two runs of the full harness. Everything else waits on that.

### The time stop sees the future, and it may be the whole edge

**Found 2026-09-04, and it is the most serious finding in this file.** The
time stop tests THIS bar's close against ``entry_price`` and the engine fills
at THIS bar's OPEN. ``fill_mode`` is ``"open"`` and
``time_stop_threshold`` is **0.0**, so on every time-stop bar the engine asks
"is this position up?" using the close, then exits at the open. A position
that fell during the bar is exited *before* the fall that was just observed;
one that rose is kept, having already seen the rise.

Time stops are **88% of all closes** -- 2,278 of 2,590.

    arm                     closes   time stops     CAGR    maxDD
    as shipped               2,590        2,278   467.7%    44.4%
    judged on the fill       2,765        2,459   -47.5%    93.9%

**+467.7% to -47.5%.** And the corrected arm fires MORE time stops, not
fewer, so it is not drifting toward the no-time-stop case that this file
records at -58.6%; it is a real correction.

Sweeping the parameter does not rescue it:

    time_stop_days   as shipped   corrected
            0.15         569.3%      -38.8%
            0.29         467.7%      -47.5%
            0.50         467.7%      -47.5%
            1.00         186.6%      -45.9%

**Consequences.** Every return figure this project holds was measured on this
engine -- 467.7%, 327.1%, 307.6%, 254.9%, and every sweep, arm and control
compared against them. They are all suspect. So, symmetrically, is every
NEGATIVE result: the monotonic harm from profit-taking thresholds, the failed
overnight exits, the weekend flattening, the proportional trim. The evidence
against those was produced by the same engine as the evidence for the
strategy.

The honest statement is that **under a corrected time stop the shipped
configuration loses money**, and whether any configuration works is unknown,
because every other parameter was fitted against the flawed one.

**What is not yet ruled out.** ``entry_price`` is the *effective* fill,
including spread, while ``reference`` is the raw fill reference. At a
threshold of exactly 0.0 that half-spread bias closes marginal positions. It
is present in both arms so the comparison should be fair, but it is the
obvious candidate for an artefact and the verification has to isolate it.
Two earlier "corrections" today produced implausible numbers before the third
was right, which is reason for caution about this one.

What is **not** in doubt is the direction: judging on a bar's close and
filling at its open is look-ahead, and it touches 88% of exits.

**The one thing this makes more attractive rather than less.** The strategy
is already mostly intraday -- median hold about four bars, 80.5% of trips
closed within a trading day -- and its exits are the compromised part. An
end-of-day close is not an addition to that mechanism but a **replacement**
for it, and it requires no price comparison at all: "the session is ending,
close" cannot look ahead. Every measurement rejecting end-of-day closing was
made on the flawed engine and has to be redone on the corrected one.

``ExecutionConfig.time_stop_uses_completed_bar`` defaults to ``False``, which
is the flawed behaviour, deliberately: flipping it would move every recorded
figure silently as a side effect of a bug fix.

### What changed on 2026-09-01

**The shipped baseline is 327.1%/yr at −45.4%**, after two changes on the
same day: conviction sizing raised to 0.75, and the engine taught to model
the opening delay it never had.

**`open_delay_minutes` is now an `ExecutionConfig` field the engine reads.**
It had lived only in `schedule/app.py` since the bridge was written, so
production refused to open in the first half hour while the backtest happily
took those trades — 228 of them, on the opening bar, that the account would
never have placed. Modelling it is worth **+19.5pp** (307.6% to 327.1%) at
+1.3pp of drawdown, because the trades being removed were the weak ones.

That is a **modelling correction, not a strategy change**: the account's
behaviour is unchanged, since the daemon was already refusing those opens.
What changed is that the backtest stopped describing a strategy nobody runs
— which is the shadow-book failure in miniature, and the reason this is
worth more than the 19.5pp it happens to be worth.

Applied to entries only, and not shifted by `execution_lag` unlike the rest
of the entry gate: that mask travels with the signal bar, while this is a
fact about the bar being dealt on, and the live path asks the same question
when it places the order rather than when it forms a view. Both
`ExecutionConfig` constructions in `_execution_config` carry it, including
the fallback taken when the broker table cannot be read — a fallback that
dropped it would model a different strategy exactly when something else had
already gone wrong, and a test asserts the two counts match.

**Earlier note, superseded:** the baseline was 307.6%/−44.1% between the
sizing change and this one. `confidence_sizing_
strength` moved from 0.5 to 0.75 on the owner's decision, after it passed
every control this project runs — the leverage-matched control, an
independent split-half, per-year and per-quarter cuts, a 4,000-path block
bootstrap of both arms, and four universe sizes. The evidence and the
refuted reason for the old value are recorded under the sizing entry below.

Any figure in this file quoting **254.9%/−41.4% as "the baseline" describes
the configuration before that change** and is left standing because it was
correct when measured. The current one is 307.6%/−44.1%, and a harness
asserting reproduction should assert against that.

The forward record on the demo account restarts from today for the same
reason: the account is now trading a different configuration from the one it
ran on 31 August.

**Late entries are the good ones, and `open_delay_minutes` is not modelled
in the engine.** Asked whether opening near the close is wasteful — pay the
spread, carry the overnight gap and financing, little session left to earn
it back. The worry has the right shape and the data goes the other way.

    bars left   trips      total P&L      mean  win rate  held overnight
            0     429         17,886        42     56.2%           100%
            1     443      2,758,022     6,226     54.4%            86%
            2     586      6,944,751    11,851     52.2%            78%
            3     662      6,102,413     9,218     48.5%            69%
            4     907      4,524,889     4,989     48.7%            73%
            5     662      3,642,298     5,502     44.1%            47%
            6     494        400,274       810     45.1%            38%

A trade opened with two bars left averages +11,851 at a 52.2% win rate; one
opened early averages +810 at 45.1% — and the late ones earn it *despite*
being held overnight far more often. Only the final bar is weak, 429 trips
averaging +42. Gating late entries is destructive: −11.7pp for the last bar,
−77.3pp for the last two.

**A modelling gap, found on the way and worth closing on its own.**
`open_delay_minutes = 30` lives only in `schedule/app.py`, so production
refuses to open in the first half hour while **the engine does not model it
at all** — the backtest fills from 13:00 UTC on trades production would
never take. The shipped baseline therefore describes something the live
system does not do, and the "skip the first bar" arm at +27.9pp is roughly
production's actual behaviour. Backtest and account currently disagree about
when trading starts.

**And skipping more of the morning is the strongest new lead**, on the risk
side especially:

                              CAGR    maxDD   H1 diff   H2 diff
    baseline                307.6%    44.1%
    skip first 1 bar        335.5%    44.2%     +32.0     +23.7
    skip first 2 bars       309.0%    42.0%      -2.3      +5.4
    skip first 3 bars       371.7%    36.7%     +88.7     +40.5
    skip first 4 bars       389.1%    35.6%     +80.4     +83.0
    skip first 5 bars       360.8%    32.2%     +30.9     +78.0

Drawdown falls **monotonically** from 44.1% to 32.2%. Skip-4 is +81.4pp of
return and −8.5pp of drawdown, balanced across halves, and would take Calmar
from 6.97 to 10.93.

**Not adopted, and treated as a lead rather than a result.** Skip-4 means no
entries before 17:00 UTC, the last three hours only — a drastic structural
restriction found by sweeping. The return response is not monotone and the
skip-2 dip is unexplained, which is the shape of noise rather than a clean
effect. A time-of-day filter is also exactly what fits to one 3.65-year
period. It needs the battery the sizing change got — leverage control,
bootstrap, universe robustness — before it is more than a lead.

**Capital's 5x cap and higher leverage — the first answer here was wrong and
is corrected below.** Asked whether a pro account's 10x would help.

**RETRACTED**: "the 5x cap is not binding, so 10x would change nothing."
That measured the backtest's MEAN book leverage of 1.75x and concluded there
was slack. It answered a different question from the one asked. Capital's 5x
is a *margin factor* — 20% margin per position — while 1.75x is *book
leverage*, gross notional over equity. They are not the same quantity, and
the mean is not the peak.

**Measured on the live account, 2026-09-01:**

    balance                4,970.90
    max_leverage (ours)          3.5     ceiling 17,398
    actual gross              22,374     4.50x balance, 29% OVER the ceiling
    margin in use              3,312     66.6% of balance
    gross-limit refusals in three days:  0

    margin needed for that book   at  5x:  4,475  = 90% of balance
                                  at 10x:  2,237  = 45% of balance

So the account runs at **4.50x gross with two-thirds of its margin
committed**, which is not slack. The backtest agrees once the right
statistic is read: p90 3.47x and max 4.15x against a 1.75x mean.

**The gross ceiling has never fired**, and the mechanism is structural
rather than a bug. `bridge.plan_orders` shrinks only intents whose action is
`open` or `resize`; a position sitting at `none` passes through untouched,
and `rebalance_threshold = 3.0` disables resizing. So the cap bounds *new*
exposure while a book that drifts above it stays there. That is the same
deliberate choice as `max_open_positions` — never liquidate into a drawdown
— but the consequence is that the stated 3.5x is not what actually runs, and
that is worth knowing rather than assuming.

**10x would help, for survivability rather than return.** It would not let
the strategy take more risk: at 10x the margin cap permits 5x gross while
`max_leverage` still binds at 3.5x. What it buys is roughly double the
buffer before a forced close-out — 45% margin utilisation against 90% for
the same book. That matters because modelled p90 drawdown is 60%, and being
closed out partway through a drawdown the strategy would otherwise recover
from converts a survivable loss into a permanent one.

**This is the one risk in the system that nothing measured has priced**, and
the reason is plain: the backtest has no margin model. It can hold 4.5x
gross through a 44% drawdown because no counterparty ever asks it for
variation margin. A live account can be closed out first. Any figure in this
file describing drawdown tolerance is therefore an upper bound on what the
account would actually survive.

*The exposure sweep below stands and is unaffected — it asks whether the
strategy WANTS more exposure, which is a separate question from whether the
broker would fund it.*

Asked whether higher leverage would help.

    gross leverage as shipped
      mean 1.75x   p50 1.67x   p90 3.47x   p99 3.60x   max 4.15x
      bars at or above  5x:  0.00%
      bars at or above 10x:  0.00%

The book never reaches 5x. What binds is **our own `max_leverage = 3.5`**
together with the per-instrument concentration cap, both well inside the
broker's limit. Raising the broker's ceiling buys headroom the system does
not use, and the demo account's size would not qualify for pro terms anyway.

**And more exposure would not help if it were taken.** Sweeping
`exposure_per_unit_size`, which is available today with no change of
account:

    exposure  mean lev     CAGR   maxDD  Calmar  p90 DD  P(DD>75%)
         2.0      1.37   260.4%   35.4%    7.35   51.5%       0.1%
         4.0      1.75   307.6%   44.1%    6.97   59.9%       0.7%
         6.0      1.95   315.8%   48.0%    6.58   64.4%       1.7%
         8.0      2.09   298.4%   50.4%    5.92   67.8%       3.2%
        12.0      2.22   264.1%   54.6%    4.83   72.4%       6.7%

Return is **hump-shaped rather than linear**: it peaks near 6.0 and falls
away, so tripling exposure from 4 to 12 *reduces* return to 264% while
pushing drawdown to 54.6%. The +8.2pp from 4.0 to 6.0 sits on the 8pp floor
and is not evidence.

Realised leverage is also heavily damped — exposure 2 to 12 moves mean gross
only 1.37x to 2.22x — because the caps bind long before the nominal setting
does. Asking for more exposure mostly does not produce it.

**Calmar declines monotonically as exposure rises** (7.35, 6.97, 6.58, 5.92,
4.83), so the risk-adjusted optimum sits *below* the configured value. The
genuine choice is exposure 2.0 at 260.4%/−35.4% against 4.0 at
307.6%/−44.1%: 47pp of return for 8.7pp of drawdown, both outside the noise
floor. That is a preference about risk appetite rather than a measurement
saying one is right, and it is left at 4.0 pending the owner's view.

Ruin is not near at any of these — P(drawdown > 90%) is 0.0% except at
exposure 12.0 where it is 0.1% — precisely because the caps damp the dial.

**The momentum lead is closed on its third appearance, and my own screen was
what made it look alive.** It surfaced as an 85.2% control arm on
2026-08-30, again as the strongest series in the lake at +0.0844 earlier
today, and has now been tested properly.

*The correlation was direction leaking in.* +0.0844 was RAW momentum against
trip P&L, and this book runs both ways, so it conflated "longs do well when
price has risen" with "shorts do badly when price has risen". Signed by the
direction the engine actually took it is +0.0376 at best.

    lookback   raw rho  signed rho   agrees    P&L agree   P&L disagree
           5   +0.0390     +0.0188      62%   19,442,150     +4,948,383
          10   +0.0663     +0.0200      74%   21,520,275     +2,870,258
          20   +0.0879     +0.0376      95%   22,707,022     +1,683,510
          40   +0.0269     +0.0179      89%   22,605,946     +1,515,589
          65   -0.0005     +0.0082      91%   25,387,548       -903,238

At 20 bars, where the raw signal looked strongest, **momentum agrees with the
engine 95% of the time** — it describes a trend follower rather than
extending one, which is the 93.3% figure already in this file arrived at
independently. And the disagreements are **profitable at four of five
lookbacks**, so a momentum gate removes money.

*The one candidate was tested rather than dismissed*, because one arm of five
looking good is what a multiple comparison produces. 65-bar disagreements
lose 903k, so momentum agreement was expressed as an `entry_gate` and run
through the engine:

    arm                            CAGR    maxDD   trades       H1       H2
    baseline                     307.6%    44.1%    4,185   301.2%   314.5%
    momentum agree, 65 bars      309.4%    43.6%    3,906   250.7%   378.3%
    momentum agree, 40 bars      318.9%    43.6%    3,851   281.1%   360.8%
    momentum agree, 20 bars      302.0%    44.4%    4,048   298.8%   305.6%

20 bars is simply worse. The two that look positive on the whole period lose
badly in the first half and win in the second — 65 bars swings −50.5pp then
+63.8pp — which is a period rather than a parameter, by the same test that
retired `rebalance_threshold`.

`breakout_lookback = 12` already occupies this ground, and `signals/
breakout.py` already carries a withdrawn standalone case for it. Momentum
adds nothing the engine does not have.

**The mark at the change, recorded now because it is not recoverable later.**
At 16:55 UTC on 2026-09-01, immediately after the deploy: balance
**GBP 4,917.79**, being −82.21 against the GBP 5,000 opening (−1.64%), of
which −291.67 realised and +209.46 unrealised, across 9 open positions.

`CFD_CAPITAL_OPENING_BALANCE` stays at 5,000 and is not touched — it answers
"if I gave you five thousand, would you make or lose money", which is a
question about the account rather than about a strategy version. Anything
asking how *this* configuration has done should measure from 4,917.79, and
that figure exists nowhere else.

Almost all of the −291.67 realised was accrued before today, much of it
while the account was still mirroring the shadow book's rule set. It is not
evidence about the strategy now running.

### What changed on 2026-08-31

The day the strategy was retuned onto the feed it actually deals on, and the
day the news programme was closed. Both halves are worth reading; the second
is six negative results and they cost more than the first.

**The baseline, rebuilt from one source of truth.** Decisions come from
`capital_1h` now rather than yfinance, on the owner's instruction: the trades
open at Capital's prices, so the decisions should be made on Capital's bars.
Switching the feed appeared to cost 41 points, and most of a morning went on
why. It was not the feed. `capital_1h` carries no `GBPUSD=X`, so every USD
instrument was converting at 1.0 — a regression introduced with the switch,
and one I had been filtering out of my own console output as a routine
warning. With a vendor-interval fallback for the FX pairs the honest baseline
is **23.7%/yr at −45.7%**.

Three changes then raised it, each expressed as `ExecutionConfig` settings
and applied by the engine rather than by an overlay:

    baseline                              23.7%   −45.7%
    screen selects 10 from a pool of 100  82.3%
    entry size floor removed             130.5%   −40.3%
    conviction sizing, strength 0.5      254.9%   −41.4%   Calmar 6.17

The screen arm holds every name it selects, so it does not contradict
yesterday's "eligible must equal held" — it widens what the screen may
*choose from*, not the gap between chosen and held.

Conviction sizing is the one that invites scepticism, so it carries the
control yesterday's lesson demanded before the claim rather than after. A
uniform leverage increase matched to the same mean exposure returns 198.9%,
so the differential — putting more into high-confidence signals specifically
— is worth 56 points beyond what the exposure buys. Split-half is +106.6pp
and +151.9pp, positive in both.

**The news programme is closed.** Six formulations, all null, against
confidence's +0.20 on the same trips:

    news count against direction, per instrument   5 of 104 — the chance rate
    news count against trip P&L                    +0.011
    headline sentiment level                       −0.003 to +0.015
    sentiment change against the instrument's own
      trailing baseline                            +0.011 / +0.020
    confirmation inside confidence's dead middle
      bands                                        −0.011 to +0.007
    per-publisher track record, split-half         below chance

The last deserves its detail, because "only trust sources with a record of
being right" is the most reasonable-sounding of the ideas and it is testable:
the publisher is stored with every story. Twelve publishers had the volume to
speak. Two held their sign across a May split with second-half |rho| above
0.05 — MarketBeat at −0.058 then −0.056, TradingKey at −0.011 then −0.161.
Neither is a track record. MarketBeat's readings sit inside one standard
error of zero at both n=243 and n=375, and two identical readings of nothing
are still nothing; TradingKey's first half is noise whose sign is arbitrary.
And the count settles it: with sign-agreement a coin flip, chance alone
predicts three or four survivors out of twelve. **Two survived. The publisher
split performs below randomness.**

The stratification matters most. Confidence sorts its top and bottom
quartiles well (rho +0.22 and +0.19) and says nothing in the middle two
(+0.003 and −0.038) — which is precisely where a second opinion would earn
its keep, and precisely where news was measured and found empty.

**And the one surviving positive turned out to be subsumed.** News was
found to predict *magnitude* on 104 of 104 instruments, which was the last
argument for keeping the archive: the engine forecasts magnitude too, from
realised volatility, and that forecast is live — it is what the 0.50
volatility multiplier prices. Two independent estimates of one quantity are
only worth having if they disagree and the second is right when they do.

Adjudicated on 65,906 observations across 882 bars, both estimates ranked
cross-sectionally at each bar against the realised move over the next four:

    against the realised move   all obs   disagree    agree
    engine volatility rank      +0.2742    +0.2618   +0.2914
    news count rank             +0.0985    −0.1318   +0.2824
    the two against each other  +0.2266

News is not a second opinion, it is an echo. The two agree far more than
they disagree, and news's apparent skill lives entirely in the agreement
zone, where it scores +0.282 against volatility's +0.291 — a noisy proxy for
a quantity already measured directly. Both disagreement directions favour
the engine: engine-loud / news-quiet realised rank 0.614, news-loud /
engine-quiet 0.439, with the engine closer on 55.9% of disagreements. The
mechanism is not mysterious — volatile names generate headlines.

**The apparent inversion on that subset was mine, not the market's**, and it
is recorded because it nearly became a finding. News reads −0.132 there,
which invites reading the disagreement itself as a contrarian magnitude
signal. It is not one: the subset is *defined* by volatility being low on
one side, and low volatility already predicts a small move at +0.26, so the
negative reading is volatility arriving through the selection. Held still
inside volatility deciles, news does not fall toward zero — it changes sign,
positive in all ten, pooling to +0.032 against an unconditional +0.099.
Regressing volatility out gives +0.042. This is the same shape as the
2026-08-30 contamination that read +0.433 and collapsed to +0.04:
**correlate against what remains, not against the total.**

No significance figure is quoted on that residual. 65,906 observations look
decisive until you notice the four-bar forward windows overlap across 882
bars, leaving nearer 200 independent time points; the naive standard error
is not the right one and the honest statement is that the residual is small
and its error bar unknown.

So the magnitude result stands as a fact about news and is worthless as an
input: two-thirds of it is volatility in disguise and the remainder is ~0.03
beside a direct estimate of +0.274, feeding a consumer that does not exist.
It was the last thing keeping the archive.

**Two related ideas were not built, deliberately.** Detecting genuinely
serious headlines — the aircraft-accident, tariff-announcement, price-rise
class — and separating reported fact from rumour are both the same bet with
a harder classifier in front of it, and both would need article bodies this
project does not collect. Sharpening a filter is the right move when a signal
is *attenuated*. Six formulations returning nothing, including one aimed at
confidence's blind spot, says the signal is *absent*. No amount of filtering
recovers what is not there.

**The branch is removed in full.** Owner's decision, 2026-08-31, on the
standing instruction to keep the system light and free of functionality
nothing consumes. Deleted: `ingestion/news_rss.py`, `persist/headlines.py`,
the twice-daily `record-headlines` job and its schedule constants, its row
in `ui/vitals.py`, `tests/test_headlines.py`, and the 16MB archive on the
deployment. No display indicator was shipped: a reading wired to nothing,
placed beside the one score that does predict, on the page where trades are
sized by hand, is an invitation to act on measured noise.

`advisor/sentiment.py` stays. It predates this work, the manual-trading
helper reads it on demand from yfinance, and it decides nothing.

The archive is rebuildable rather than lost — the six months were backfilled
through monthly RSS windows, not accumulated, so if a use ever appears the
data comes back for the cost of one run. What does not come back cheaply is
the reasoning above, which is why it is written here at length for a
component that no longer exists.

**The drawdown review trigger was set from one path, and the path was a
mild one.** `review_drawdown_fraction` was 0.45, chosen as the single
backtest's worst drawdown plus a margin, with a docstring still citing a
stale −39.5% and a warning that told the reader 45% was "past the 45% that
backtesting ever produced". Backtesting produced 41.4%.

Block-bootstrapping the book's own bar returns through
`backtest.montecarlo.resample_returns`, 4,000 paths at each block size:

    block    p50     p75     p90     p95     p99   worse than 41.4%
        1   49.0%   56.4%   63.9%   68.7%   77.8%        80.7%
        5   47.3%   54.5%   61.4%   65.8%   75.1%        74.7%
       20   45.4%   52.0%   59.3%   63.3%   72.9%        68.3%
       50   43.2%   49.0%   55.4%   59.3%   66.9%        59.0%
      100   41.9%   47.8%   54.1%   58.2%   66.6%        52.9%

**Between 53% and 81% of modelled paths drew down worse than the backtest
did**, and the median modelled drawdown is 42% to 49%. So 0.45 was not
3.6 points above the worst case; it sat between the median and the 75th
percentile of the model's own range, and would have fired on an ordinary
bad patch while claiming to mean the opposite.

Trade-level resampling was **not** used, on that module's own warning: it
destroys volatility clustering and its drawdown distribution is "a floor on
how bad things get, not an estimate of it". Using it would have understated
the tail and set the trigger lower still. Worth noting that here the i.i.d.
shuffle produces *wider* drawdowns than the block bootstrap, the reverse of
`backtest.montecarlo`'s stated intuition, because this book's realised
sequence was favourable and scrambling it destroys the recovery ordering.

**One number was answering two questions, which is why it could not be set.**
"Has the book fallen far enough to look at" is a fact about the account;
"is this worse than the model can produce" is a fact about the model. They
call for different responses — watch it, versus re-measure it — so they are
now two thresholds over the same reading: `review_drawdown_fraction` at 0.50
(halving is worth a person's attention, and the message says plainly that it
is *inside* the model's range) and `unmodelled_drawdown_fraction` at 0.60
(near the 90th–95th percentile at blocks 20–50, so about one modelled path
in ten). Both stay warnings and neither is wired to execution: four
automatic pauses were tested causally here and all four lost money.

Deliberately not the 99th percentile. This trigger stops nothing and only
asks a person to look, so being seen occasionally in a genuinely bad stretch
is the point, and a threshold nobody ever reaches is not a safeguard.

The most useful thing to come out of it is not the threshold. **It is that
the strategy's drawdown risk is materially worse than its backtest shows** —
a median modelled drawdown near 45% against a realised 41.4%, and a 90th
percentile around 60%. Any risk statement quoting 41.4% as the worst case is
quoting the luckiest half of the distribution.

`review_flat_months = 3` was checked at the same time and is fine: 4 of 41
rolling three-month windows are negative, so it fires about a tenth of the
time rather than routinely.

**Month to month the book is wildly varied, and the gains are extremely
concentrated.** Asked on 2026-09-01 and never measured before — the file
carried yearly figures and nothing shorter, while a month is the unit a
person actually checks in on.

    2023          +13.8   +3.1  -10.2   +8.9  +36.2   -2.4   -0.6  +13.9   +0.6  +54.6  +25.8  = +237.7%
    2024   +18.2   -6.1   +5.1  +18.1   -6.9   +7.2   +3.8   +6.5  +23.0   -1.0   -0.5   +9.5  = +101.1%
    2025   -35.1  +39.7   +7.6   +2.9   +5.7  +59.3  +44.7  +19.1  +13.6  +18.2  +25.6   -1.1  = +385.8%
    2026   +25.4   -2.2   -3.8  +20.7  +46.6   -2.9   +9.9   -4.8                             = +111.9%

Best month +59.3%, worst −35.1%, median +7.6%, sd 18.5 against a mean of
11.8. The interquartile range alone spans −0.8% to +19.9%. **The worst month
sits inside the best year** — January 2025 lost 35.1% and 2025 returned
385.8% — which is the single most useful fact here for reading a bad patch.

Concentration is the finding. Total growth over the span is 69.9x; drop the
best month and it is 43.9x, the best three 19.4x, the best six **7.0x**. Six
months out of 43 carry roughly nine-tenths of the compounding. That is the
monthly echo of the trip-level result already recorded — 20% of trips
produce all the profit — and it means the return depends on being in the
market when a handful of months arrive rather than on grinding.

It is rarely negative for long: 30 of 43 months positive, the longest run of
consecutive losing months is **two**, 3 of 41 rolling three-month windows are
negative (7%) and 1 of 38 six-month windows (3%). So a single down month
carries no information — nearly a third of them are down — while a negative
three-month stretch is genuinely unusual, which is what makes
`review_flat_months = 3` the right horizon.

One path, and the block-bootstrap work says the realised sequence was a mild
draw, so the true month-to-month spread is likely wider than this. The 2026
row is eight months.

**The overnight gap is mostly not a gap: Capital quotes through it, and the
engine cannot see those bars.** The strongest open lead in the project, found
2026-09-01 on the owner's observation that the book sits idle while the
shares keep moving.

`gap` means the move from the previous close to this bar's fill, on the
position held into the bar — verified as overnight rather than intrabar:
88.8% of all |gap| P&L lands on the first bar of a session, and the median
discontinuity on a held position is 0.862% there against 0.008% on later
bars. Net across the run it is **−5.8M on session opens against +0.2M on
every other bar**, so this book systematically pays to hold overnight and
earns during the session.

But Capital trades 57 of the 100 candidates from 08:00 to 00:00 UTC rather
than 13:30 to 20:00, `marketStatus` reads TRADEABLE, **and the lake already
holds those bars** — MU's raw file has 14,087 rows spanning 08:00–23:00 and
`restrict_to_session` hands the engine about 6,400 of them. More than half
the data is discarded, in exactly the window that produces the losses.

One ordinary NVDA night shows what is being thrown away. Session close
129.91, then 129.46 / 130.26 / 130.44 / 131.28 through the evening, a dead
zone from 00:00 to 07:00, then 132.43 / 132.17 / 132.41 / 133.82 / 133.53
through the morning, opening at 132.67. A continuously quoted path at
8–12bp against 8.2–8.5bp in session, booked as an unavoidable jump.

Measured over 6,406 nights on the largest extended-hours names: the median
session-close-to-session-open move is 0.52% to 1.37%, of which the quoted
leg is 0.682% and the genuinely unquoted leg 0.500% — so **about 58% of the
overnight move happens while the broker is dealing**. (The two legs exceed
the total because they are medians of absolute moves that partly offset.)
Out-of-session spread runs 1.4x to 2.2x the in-session figure for the
majors.

The likely prize is exits more than entries: a position that reverses at
22:00 currently bleeds until 13:30.

**MEASURED AND CLOSED, 2026-09-01.** The owner asked for it to be tried,
with the focus on exits rather than entries — cut losses before they become
big ones. It fails three ways, and the second is the one that settles it.

*Widening the decision panel destroys the strategy.* On the extended index
the control — wider panel, dealing restricted to the regular session, which
is what production does today — returned **−33.0% at −82.6% with cost at
176.6% of gross**, against a recorded 254.9%. 56% of the out-of-session panel
is NaN and every bar-counted lookback rescales, exactly as `repository`
warns. So the panel stays as it is, and the owner's own correction is right:
the decision view must not change, only the ability to close.

*Exits-only is a bad decision, not merely an expensive one.* Screened as an
overlay on the unchanged book: for every position held into a night, walk
the quoted extended bars and close at the first one showing a loss beyond a
threshold.

    cut at  closed   gross if cut   gross saving       spread          net
      0.5%   1,936     -3,057,561     +1,148,385   -5,570,277   -4,421,892
      1.0%   1,223     -4,220,957        -15,011   -4,021,204   -4,036,215
      2.0%     477     -5,251,530     -1,045,584   -1,949,640   -2,995,224
      3.0%     238     -5,370,717     -1,164,771   -1,015,471   -2,180,242
      5.0%      51     -4,824,890       -618,944     -232,594     -851,538

Above about 1% it is worse **with execution free**. Positions down 2% at
22:00 are on average better off by the next open than if they had been
closed: the overnight move substantially mean-reverts. Only the tightest cut
helps gross at all, by 1.1M, and pays 5.6M of spread to do it.

*And in the tail it would not have helped.* The DeepSeek weekend, which is
the worst week in the record: MU's last quote was Friday 19:00 at 102.99 and
its first quote after was Monday 09:00 at 95.85, already **−6.93%**. NVDA
−9.32%, PLTR −5.67%. The gap arrived across a weekend, where no broker
quotes. Acting at the first available price would also have sold the bottom
— PLTR ran −8.87% at 10:00 and −3.26% by 14:00 — while spreads that night
were 20–39bp against 9–17bp in session, so the cost doubles exactly when the
urge to act is strongest.

This is a screening overlay and nothing was adopted from it, but no engine
work is warranted: a rule that loses at zero cost does not become profitable
when charged for. It also leaves the same conclusion the file already
reached from the other direction — stops are monotonically harmful because
they convert prospective winners into realised losers, and an overnight stop
is simply a stop with a wider spread.

Untested and deliberately left so: overnight *profit-taking*, and overnight
entries. Both are different hypotheses and neither follows from this one.

**And the book that took the hit was right, which is worth recording because
the instinct is to assume otherwise.** Asked what was held into that weekend
and whether the system chose well.

    name    side    units    notional   conf   Fri->Mon        P&L
    PLTR    long   7390.5     583,738   0.60      -3.6%    -21,026
    UBER    long   7096.6     486,647   0.37      -0.1%       -674
    SHOP    long   4798.8     516,859   0.38      -1.5%     -7,966
    NVDA    long   2696.1     307,964   0.37     -11.0%    -42,437
    AVGO    long   1229.5     240,733   0.39     -11.5%    -34,685
                                                 total    -106,788
    gross 2,135,941 = 2.56x equity, or −12.8% of the book over one weekend

Every name was in a confirmed uptrend going in — AVGO +8.5% over a
fortnight and +42.2% over three months, PLTR +17.1% and +76.1%, NVDA +4.7%
and +1.5%. A trend follower long that complex was doing exactly what it is
built to do. There was no price information about what was coming, news
predicts direction at the chance rate, and the book exited in order over the
following week: UBER Monday, SHOP Tuesday, AVGO Wednesday, NVDA and PLTR by
Friday. No panic and no doubling down.

**Weekend leverage is not the flaw either.** Over 36,619 gaps, weekend moves
are 1.08x the median weeknight move and **0.84x at the 99th percentile** —
p95 3.73% against 3.69%. Risk does not scale with the length of the closure,
because information arrives in discrete releases rather than continuously.
So 2.56x into that Friday was ordinary leverage meeting an extraordinary
weekend, and flattening into weekends is not justified by weekends being
special. That also disposes of the overnight-flattening re-measurement
flagged earlier the same day.

**The one criticism that survives is correlation.** NVDA and AVGO fell 11.0%
and 11.5% together because they are the same trade, and PLTR is adjacent:
three of five positions were one bet wearing three names. It passed every
check the project has — no name breached the 20%-of-gross concentration cap,
and `max_per_sector = 5` did not bind because the sector map files
semiconductors and software separately.

**You cannot cut losing trades, and the reason closes that question for
good.** Asked how to reliably cut losers, since they look like a drag. They
are one — 2,099 losers cost 23.6M against 2,076 winners earning 38.7M, 61%
of gross — but they are not cuttable.

    once a trip is already down at bar N
      bar   down trips   end up losing   mean final   return if held on
        1        2,015           51.1%       +3,006             +0.13%
        2        1,420           44.3%       +6,518             +0.10%
        3        1,212           41.7%       +8,377             +0.14%
        4          981           38.2%      +11,314             +0.04%

**A position that is currently down has positive expected P&L** — +3,006 at
bar one, rising to +11,314 by bar four — and a positive return from that
moment to its own exit at every bar checked. Cutting destroys value wherever
you could do it. Same conclusion the minute study reached from features.

The drag is also not where it feels. By duration: 1–2 bars loses 5.3M on
1,621 trips, 3–4 loses 1.7M, 5–7 loses 2.4M, while 8–15 earns 5.9M and 16+
earns 18.7M. The median loser is already held **two bars** — the engine cuts
as fast as it can. The 5.3M is the cost of having *opened* those trades, not
of holding them. The drag is at entry.

**And no confidence band loses money**, which retires an idea and completes a
retraction. On the decision bar:

    band          n     total P&L      mean   win rate
    0.21-0.37   836    +2,095,532    +2,507      45.8%
    0.37-0.42   835    +1,984,586    +2,377      50.9%
    0.42-0.49   835    +2,946,305    +3,529      49.3%
    0.49-0.67   835    +3,236,911    +3,877      50.5%
    0.67-1.00   835    +4,870,056    +5,832      52.0%

The top band earns 2.3x the bottom and **every band is profitable**, so there
is nothing to cut — which is why raising the entry gate costs money. The
2026-08-30 figure of "the bottom bucket loses GBP76k at 41.6%" was measured
on the fill bar; on the decision bar that bucket **earns 2.1M at 45.8%**. The
retraction was right and the corrected picture is materially different.

**Screened every per-instrument series the lake holds, used or not**, against
trip P&L on the decision bar (n=4,176, so a standard error near 0.015):

    return, previous 20 bars     +0.0844      bar range (high-low)/close  -0.0601
    bars since session open      +0.0719      spread (bp)                 -0.0443
    hour of day (UTC)            +0.0703      realised volatility         -0.0147
    return, previous bar         +0.0450      volume (tick count)         +0.0102
    return, previous 5 bars      +0.0318      price level                 -0.0028
    entry confidence             +0.0250      average pairwise corr       +0.0231

Momentum over 20 bars is the strongest thing in the lake and beats
confidence, which is consistent with the crude momentum control already
recorded at 85.2% with no losing year and still not followed up. Confidence's
reading is attenuated by construction — positions are already sized by it, so
P&L embeds it — and momentum and hour-of-day carry no such circularity.

**`confidence_sizing_strength = 0.75` is the strongest candidate found and
passes both controls.** Sizing is the only lever that acts before a trade is
known to be bad, and the sweep is monotone: 0.0 gives 128.8%, 0.5 gives
257.6%, 0.75 gives 307.6%, 1.0 gives 325.2%, 1.5 gives 346.5%.

    arm                                CAGR       H1       H2   maxDD    lev
    strength 0.5 (configured)        257.7%   235.0%   282.3%   41.9%   1.66
    exposure 1.058x, strength 0.5    272.0%   243.3%   303.3%   41.3%   1.69
    strength 0.75                    307.7%   301.2%   314.5%   44.1%   1.75

    differential   whole +35.8pp   H1 +57.9pp   H2 +11.3pp

Positive in both halves and both above the 8pp floor, with the
leverage-matched control taking only 14.3pp of the 50pp, so about 36pp
belongs to the rule. Calmar 6.15 to 6.98. **Not adopted without the owner's
decision**, because it is a live parameter and changing it restarts the
forward test. The honest caution is that the differential decays hard across
halves, +57.9pp to +11.3pp, and H2 only just clears the floor.

**Validated further on 2026-09-01, and it survives every test this project
knows how to run.** Asked whether more could be done locally before deciding.
Three tests, each able to kill it.

*Every period.* Ahead in **3 of 3 full years** — 2024 +34.5pp, 2025 +32.9pp,
2026 +15.9pp — and in 10 of 14 quarters, the four exceptions being −4.7,
−0.2, −0.2 and −8.1. The quarterly edges are small and compound into the
annual ones, which is what a real per-trade effect looks like.

*Distributions rather than point estimates*, the test that retracted two
arguments about `rebalance_threshold` in an afternoon:

    arm      ret p10   ret p50    ret p90   DD p50   DD p90
    0.50       1870%    10518%     51492%    45.0%    58.8%
    0.75       2887%    17062%     88028%    46.4%    59.9%

0.75 beats 0.50's median return on **63.5%** of resampled paths against a
50% coin flip, and **even the tenth percentile improves**, 1870% to 2887%.
Drawdown is modestly worse — p50 +1.4pp, with 56.3% of paths beyond 0.50's
median — so the gain is not free, but it is small beside the return.

*Not fitted to the universe size.* At targets 8, 10, 12 and 16 the
difference is +47.4pp, +50.0pp, +32.9pp and +42.7pp: positive everywhere and
everywhere clear of the floor.

Set beside the change refused two days earlier, which is the useful
comparison:

                            rebalance 1.0        strength 0.75
    leverage control     half was exposure     +35.8pp differential
    split-half            +19.1 then +2.6      +57.9 then +11.3
    bootstrap return              56.2%                  63.5%
    bootstrap drawdown     coin flip 48.5%     56.3%, modestly worse
    every year                  untested                 3 of 3
    universe robustness         untested                 4 of 4

What remains is irreducible rather than untested: it is one dataset, and
drawdown is genuinely a little worse.

**`confidence_sizing_floor` is inert.** 0.0, 0.10, 0.25 and 0.50 return
identical results to the decimal. With a non-negative strength and confidence
at or above the entry bar the multiplier is always at least 1.0, so the floor
can never bind. It is dead configuration, and this file's own warning about
inert arms reporting clean nulls applies to it.

**Scaling down into a gap rather than closing was also measured, and it
fails on arithmetic rather than logic.** The owner's proposal, and the
best-formed of the family: trim every position proportionally into the
weekend, restore on Monday. It is not selective, so the mean reversion that
kills every stop does not apply to it, and it is a resize rather than a
round trip, so it does not reset the time stop or the cooldown.

    weekends only, 183 gaps
      gap P&L held through          -536,405
      notional held into gaps    912,015,189
      one crossing of the spread     996,695   (10.93bp)

      scale to   loss avoided   spread paid          net
          0.50        268,203      -996,695     -728,492
          0.00        536,405    -1,993,390   -1,456,985

**The entire weekend gap loss over 3.65 years is smaller than one crossing
of the spread**, and trimming costs two. Over every gap the average loses
9.5bp of notional against 22–26bp to trim out and back. Both sides scale
linearly with the fraction, so no scale factor changes the answer; it loses
by about 2.3x throughout.

**Conditioning on a signal does not rescue it**, and the way it fails is
instructive. Ranked by the engine's own volatility estimate — the one input
here with measured power over magnitude, at +0.274 — the top decile appears
to pay: mean gap −38.24bp against 26.08bp to trim. It does not survive.

    top decile, 88 gaps: mean −38.24bp, median −16.00bp, cost 26.08bp
      worst five           −1257, −626, −600, −444, −439
      excluding the worst  −24.24bp   (now below the 26.08bp cost)
      excluding worst three −10.38bp
      gaps that lose at all      55%
      first half +60.34bp        second half −67.24bp

One observation carries it. Remove the single worst gap and the average loss
falls below the cost of trimming; only 55% of the "dangerous" decile loses
anything; and the sign reverses across halves, which is the same test that
retired `rebalance_threshold`.

**The general form, which is the useful part:** gap losses are not
distributed across identifiably risky nights, they are concentrated in a few
unforecastable events. To catch the one that matters you must pay the
premium on all eighty-eight, and the other eighty-seven do not lose enough
to cover it. That is why every member of this family fails — the automatic
pauses, the stops, the correlation cap, the overnight exit and now the
proportional trim. **The tail is rare, large and unforecastable, so any
per-gap insurance premium exceeds the average payout.**

**And the correlation thread is closed too, on the sharpest possible
grounds: the correlation was not there beforehand.** The owner's framing was
that a static map cannot express something time-varying — semis and software
are one trade sometimes and separate at others. Both halves measured off the
engine's own rolling correlations.

The description is exactly right. Semis against software runs a median 0.32
with a p10 of 0.03 and a p90 of 0.56 — sometimes unrelated, sometimes nearly
one position. And the sector split is real on average: semis correlate 0.57
with each other against 0.32 with software, a genuine 0.25 gap, so the
taxonomy is not wrong, only too slow.

But it is not actionable, and this is the part that settles it:

    pair          24 Jan   median    p90   percentile of own history
    NVDA/AVGO       0.47     0.64   0.79        21%
    NVDA/UBER       0.12     0.39   0.64        19%
    SHOP/UBER       0.26     0.46   0.70        21%
    PLTR/SHOP       0.65     0.49   0.71        83%

NVDA and AVGO — the pair that fell 11.0% and 11.5% together — sat at **0.47
against their own median of 0.64, the 21st percentile**. Two of ten pairs
were in the top fifth. A correlation cap inspecting that book on the Friday
would have called it well diversified.

A common shock does not reveal existing correlation, it creates it. Any cap
prices what was visible beforehand, and beforehand there was nothing to
price. Worse, the two available readings pointed opposite ways that day:
cross-group correlation was above its median (0.42 against 0.32) while
semis-within was below its (0.46 against 0.57), so whichever the cap was
built on it would have said the wrong thing.

The older reason still stands underneath: the sector cap was measured on
2026-08-28 to buy nothing it was credited with, a rank-matched control
ignoring sector entirely beating it 0.75 against 0.65.

**The obstacles, kept because they still apply to anyone reopening this:**

* **Mixed calendars are a known killer here.** 57 candidates have extended
  hours and 43 do not. This file already records what happened when
  instruments with different sessions were joined into one hourly panel: the
  index became the union of two calendars, the shares went NaN outside their
  own session, and every signal in the book died. That is precisely this
  shape, and it is why `restrict_to_session` exists.
* **Every figure the project holds is measured on session bars** — the
  254.9% baseline, all five parameter sweeps, the 8pp noise floor. Extending
  roughly doubles the bar count and invalidates all of it.
* **Costs are already 35% of gross**, and more bars means more decisions at
  one and a half to two times the spread.

**Losses come from gaps; gains come from trading.** Asked what happened in
the best and worst weeks, and the answer generalises. Read from the engine's
own per-bar decomposition, whose five components sum to the equity change to
the pound in every week checked.

    bucket        weeks        total       gap  intraday   spread  slippage
    worst 10%        19   -8,708,225      83%        3%      10%        3%
    worst 25%        47  -10,911,932      78%        3%      14%        5%
    best 25%         47   20,653,333      17%       94%      -8%       -3%
    best 10%         19   16,008,018      29%       80%      -7%       -2%

(The middle 50% is omitted: its total is near zero, so component shares blow
up and say nothing.) At the level of individual weeks, 45% of losing weeks
lost more to gaps than to intraday moves against only 28% of winning weeks
that gained more from gaps.

The worst week in the record makes it concrete. 25–31 January 2025, −21.0%:
gap −170,184 of a −175,296 move, while **intraday was positive at +6,815**.
During the hours the book could act it made money; the whole loss arrived
between one bar's close and the next bar's open. The best week, 10–16 May
2025 at +43.1%, was 66% gap and 40% intraday with every position long.

Three things follow.

**The tail risk is structural rather than a signal failure.** Nothing about
the entry or exit logic went wrong in the worst week. The book was hit while
it could not react.

**It supplies the missing mechanism for stops being monotonically harmful**,
which this file records as a measured fact without explaining why. A stop
cannot protect against a gap through it — the engine carries `gap_through`
on every trade row. Against gap risk a stop pays its cost and buys nothing.

**Costs bite hardest when losing.** Spread is 10–14% of the loss in bad
weeks and a −7% drag on gains in good ones, because bad weeks are
high-turnover weeks.

The same correlated cluster drives both extremes — NVDA, AVGO, MU and PLTR
held long made the best week and lost the worst — which is the strategy's
exposure rather than a defect in it.

**One open question, flagged and not acted on.** The obvious response is to
cut overnight exposure, and that was measured and rejected: flattening
before every non-trading day cost 10.3pp of CAGR for no drawdown reduction
and 275 extra round trips. But that was measured on 2026-08-29 on yfinance
bars at a flat 5bp — the superseded basis — and it was not in the
five-parameter re-measurement queue. With gaps now shown to carry 83% of
tail losses and spread charged at 2.3x the figure that rejection assumed, it
is a legitimate candidate for re-measurement. It is not obviously going to
help: gaps also supply 29% of the best decile's gains, so removing that
exposure removes both sides of it.

**Weekly, the same shape harder, and it puts a number on how long a forward
result takes to mean anything.** 190 complete weeks: 54% positive — a coin
flip — median +0.6%, quartiles −3.3% to +9.3%, middle 90% spanning −10.9% to
+19.1%, best +43.1% and worst −21.0%, mean +2.9% at a standard deviation of
10.0.

The monthly smoothing is pure aggregation rather than mean reversion.
Signal-to-noise is 0.29 weekly against 0.64 monthly, and 0.29 × √(52/12) =
0.60 — near enough the observed figure that week-to-week returns are
effectively serially independent.

Concentration is worse at this resolution: 102.7x total, 23.9x without the
best five weeks, 8.6x without the best ten, **1.8x without the best twenty**.
Ten per cent of weeks are the whole result.

    after  1 week    t = 0.29
    after  4 weeks   t = 0.58
    after 13 weeks   t = 1.05
    after 52 weeks   t = 2.11   distinguishable from zero

**About 47 weeks — eleven months — before a forward result is distinguishable
from noise.** That is the honest answer to "how are we doing" at any short
horizon, and it applies to the demo account's forward record as much as to a
backtest.

The concrete case, worth keeping because it is the causal argument against
every safety valve tested here: the longest losing run in the record is
**seven consecutive negative weeks, ending 7 February 2025**, bracketing the
−35.1% January. The year that followed returned +385.8%, the best in the
span. Stopping during that run would have missed it.

**The currency pairs now come from Capital too, and that closes the feed
switch.** They were the last input still taken from yfinance intraday,
against the standing instruction of one source of truth — and not a small
one, since they convert every price in a book of dollar instruments held in
a sterling account.

It looked like a vendor limitation for a day. The lake spells them
`GBPUSD=X`, Capital calls the same market `GBPUSD`, so every request
returned nothing and the log line — "the broker does not quote them" —
read as fact. It quotes all five, with hourly history from 2023-01-03, the
same start as the equity bars.

Measured through the engine before adopting, on the screen-gated harness:
**257.6%/−41.9% against the recorded 254.9%/−41.4%**, a 2.7pp drift inside
the 8pp floor, with the trade count unchanged at 4,178. So the switch
changes nothing measurable, which is the outcome that makes it safe rather
than the outcome that makes it interesting.

What it does improve is coverage. The yfinance `1h` pair ran 2024-08-26 to
2026-08-26 — under two years, not even reaching the start of the backtest,
and five days stale. Capital's runs 2023-01-02 to now. The runner's
`REFERENCE_FALLBACK_INTERVAL` path fired once in the old run and fires zero
times in the new one.

Two failures on the way, both recorded because the second was caused by
fixing the first badly. The pairs stopped being fetched *at all* once the
decision feed moved to `capital_1h`, because the yfinance branch was never
reached — their bars were two days stale and decaying toward the 1.0
conversion this project has already paid eleven points of drawdown for. The
first fix routed them back to yfinance so *somebody* would fetch them: right
for the symptom, wrong for the cause, and retired within the hour.

**The five parameters that predated the corrected cost model are
re-measured.** Every figure justifying them came from yfinance bars at a
flat 5bp; decisions now come from `capital_1h` and spread is charged per bar
at roughly 2.3x that. All five swept as `ExecutionConfig` settings applied
by the engine, screen-gated at target 10, one parameter varying at a time.
The baseline reproduced the recorded 254.9%/−41.4% to the decimal, which is
what licenses reading the rest.

    time_stop_days        none −58.6%/97.5% · 0.29 254.9% · 1.0 96.1% · 2.0 17.6%
    earnings_blackout     0 days 215.9%/52.5% · 1 day 254.9%/41.4% · 2 250.8% · 3 252.8%
    stops.enabled         off 254.9%/41.4% · 4 ATR 241.4% · 3 ATR 175.5% · 2 ATR 106.6%
    decayed_multiplier    1.0 263.5% · 0.75 262.4% · 0.5 254.9% · 0.25 250.8%
    rebalance_threshold   3.0 254.9%/41.4% · 1.0 276.8%/45.5% · 0.5 178.9% · 0.25 152.3%

**Four are confirmed and one is left alone on weaker grounds than before.**
The time stop is the most load-bearing parameter in the book: removing it
does not cost return, it destroys the account, with cost running to eleven
times gross profit because nothing closes a position any more. The earnings
blackout is now load-bearing on *both* counts — switching it off costs 39pp
of return and 11.1pp of drawdown — which it could not have been before,
because until 2026-08-28 it had never once executed. Stops remain
monotonically harmful, tighter strictly worse, exactly as measured under the
old cost model. The decayed-position multiplier is the flat one: every
adjacent step sits inside the 8pp floor and the whole range spans 12.7pp, so
it is close to inert on return and stays at 0.5 because nothing says to move
it, which is a weaker justification than it used to have.

**`rebalance_threshold = 1.0` is measured better and not adopted — and the
first two reasons for refusing it were both wrong.** Recorded at length
because a right answer reached the wrong way is one that reverses the next
time somebody re-derives it, which is the 2026-08-30 lesson in a new place.

*Retracted*: "the entire gain is handed back in drawdown, 41.4% to 45.5%,
and Calmar goes 6.17 to 6.08." That compared two single realisations.
Block-bootstrapping both arms puts their drawdown distributions on top of
each other — p50 45.4% against 45.0%, with 1.0's paths landing beyond the
configured arm's median **48.5% of the time, a coin flip**. The 4.1pp was
sequencing noise inside a distribution many times wider than it. The Calmar
figures are removed from the config table rather than corrected, because
they were arithmetic on that noise.

*Retracted*: "45.5% sits above `review_drawdown_fraction`." That threshold
was itself set from a single path, and is now 0.50.

**The real reason is the leverage control, which should have been run
first.** The two arms differ by *eleven round trips out of 4,179* — a large
effect from a tiny cause, which this file already defines as something to
investigate rather than believe. What actually changes is exposure: mean
leverage 1.65 to 1.69, with held names and flat bars identical.

    3.0 (off, configured)   254.9%   H1 228.3%   H2 284.1%   maxDD 41.4%
    exposure 1.023x, off    265.2%   H1 233.3%   H2 300.4%   maxDD 40.8%
    1.0 (rebalancing on)    276.7%   H1 252.5%   H2 303.0%   maxDD 45.5%

    differential            H1 +19.1pp   H2 +2.6pp   whole +11.5pp

About half the headline gain is bought with exposure, and the half belonging
to the *rule* does not survive the split: +19.1pp then +2.6pp, the second
inside the 8pp floor and indistinguishable from nothing. A first-half-only
effect is a period, not a parameter. **`rebalance_threshold` stays at 3.0.**

The exposure arm is also **not** adopted, and it is the more tempting of the
two: positive in both halves and drawing down slightly *less*. But +10.3pp
barely clears the floor, its own halves are +5.0pp and +16.3pp, and raising
leverage on a funded account because one backtest liked it by ten points is
precisely the overfitting this file exists to prevent.
`exposure_per_unit_size` stays where it is.

The general lesson, which outlives this parameter: **a difference between
two backtests is not a measurement until you know how wide the distribution
is that both were drawn from.** Two of the arguments above stood for a day
only because nobody had asked.

The old note that drift rebalancing at 0.5 "beat it in 9 of 10 out-of-sample
windows" is now **refuted rather than superseded**: at measured spreads 0.5
returns 178.9% against 254.9%. Drift rebalancing buys precision with
turnover, and turnover is precisely what got dearer.

**A harness error worth recording, because it looked like a result.** The
first version of this sweep passed the raw hundred-name candidate list as
the traded universe with no `entry_gate`. It returned −75.9% at −99.5% on
294,530 trades, and every parameter below it would have read as a rounding
error. That is the pool-100 configuration this file already records as
destructive at −78.7%; the screen's daily selection *is* the strategy, and a
sweep that omits it measures a different system. Caught by the trade count
being two orders of magnitude out, which is the "treat a surprising number
as a bug" rule doing its job. The corrected harness asserts its baseline
reproduces a recorded figure before any arm is read.

### What changed on 2026-08-30

Twelve measurements, ten of them negative, and the negatives are the more
useful half. Two things are settled, most of a day's ideas are dead, and
two leads are worth real work.

**Eligible must equal held.** The strongest result, and mechanical rather
than statistical. Damage scales monotonically with the gap between the
candidate pool and the slots available: pool 10 with no limit returns 86.4%
on 3,861 trades; pool 16 holding 10 returns 63.4% on 9,394; pool 50 holding
10 returns -53.7% on 30,118; pool 100 holding 10 returns -78.7% on 44,383.
The last arm has *zero* universe churn -- with target equal to pool size the
screen selects everything every day -- and still does seventeen times the
baseline turnover, because the limit alone rotates ten slots among a hundred
candidates as relative signal strength shifts. Every name eligible but
unheld is a candidate to rotate in, and each rotation pays a round trip.

This retires the older note that "all 49 eligible with 8 slots returned
13.6% against 7.5%". That configuration now destroys the account. The
screen's `target` is the only capacity control; `max_open_positions` stays
unset as a safety bound rather than a tuning parameter.

**There is no optimal universe size, only a robust band.** Every year wants
a different number -- 2023 peaks at 10, 2024 at 4, 2025 at 16, 2026 at 20 --
and no year's answer is inferable from the ones before it. What 8 to 12 has
is that it never loses: no losing year at any size in that band, worst year
between +8% and +15%, while 16 and above lose 23% to 40% in 2024. Choose it
for robustness, not optimality. The returns are lumpy rather than smooth: at
ten names the good years made 186.7% and 172.8% while the flat ones made
15.5% and 9.3%.

**The median position lives four hours.** Measured: median hold 4 session
bars, mean 6.2, winners 8.8 against losers 3.3, and 80.5% of trips closed
within one trading day. That single number explains most of the day's
failures. Every intervention operating at the holding horizon worked --
entry confidence, the breakout filter. Every intervention slower than the
hold failed -- regime-conditional sizing, sideways pausing, churn demotion,
cadence tuning. A label computed over a multi-day window cannot inform a
decision about the next four hours; by the time it is confident, the
position it would have governed has closed and two more have been and gone.

**Twenty per cent of trips produce all of the profit.** Trips held 8 bars or
more earn GBP528k; those held two bars or fewer lose GBP164k; the 672 in
between earn GBP11k. Win rate climbs monotonically with duration from 30.5%
to 97.7%. The strategy is a search process paying GBP164k in small losses to
find GBP528k, which is why stops are monotonically harmful -- they convert
prospective long winners into short losers -- and why "cut losses quicker"
has no room to operate: the one-bar losers already exit as fast as possible.

**Confidence predicts and is used at half strength.** On the screened path
the bottom bucket loses GBP76k at a 41.6% win rate and the top earns GBP411k
at 67.8%. **RETRACTED IN PART, 2026-08-30 (evening).** Those buckets were
built from the confidence on the *fill* bar, not the bar the decision was
made on -- ``execution_lag`` is 1, so the engine gates on the previous bar's
value and the analysis read one bar later. That is why the bottom bucket
spans 0.00-0.37 when the entry gate is 0.35, which should have been
impossible and was noticed only when the owner asked. The *ordering* is not
in doubt -- a mirror arm that cut the top bucket instead returned 51.7%
against 85.1% for cutting the bottom -- but the bucket boundaries, the
per-bucket totals and the 0.37 threshold derived from them are all one bar
out and must be re-measured before being quoted. But the middle three buckets are indistinguishable, which is why
raising the *gate* fails -- it cuts good trades with bad, +1.6pp at 0.40 and
-12pp at 0.45. **The gate stays at 0.35.** Sizing does not have that
problem: it can shrink the bottom bucket without discarding the middle. That
is the best-supported untested idea in the project.

**What was measured and rejected.** Regime-conditional universe size loses
to a constant (78.6% against 86.4%) while doing 2,400 more trades, though
the trend labels do carry information -- the rule beat its own mirror by
18.2pp. Pausing when sideways costs 3.3pp and *worsens* drawdown; the mirror
shows trending bars worth only 1.27x sideways ones, and the trade count
moves by one, so it reshuffles entry timing rather than avoiding a regime.
Mean reversion loses in every year at every threshold. Minute-resolution
features cannot inform exits: at 34,818 decision points sampled every five
minutes, every feature's worst quartile still gains between +0.32% and
+0.49% from that moment, so closing on any of them destroys value.

**The volatility multiplier is justified.** Raising it above 0.5 worsens
drawdown monotonically (-34.4% to -41.9%) and halves H2 performance.
Independently, the minute study found realised volatility to be the one
clean monotone gradient it contains -- quiet positions gain +0.779% from
here against +0.317% for volatile ones -- which is the same effect the
multiplier already prices. **Leave it at 0.50.**

**Two leads.** ~~A breakout filter on the entry bar improves return 74.4% to
76.7% and drawdown -34.4% to -31.2%, monotone across four lookbacks.~~
**RETRACTED 2026-08-30 (evening): that was measured on a harness rule the
implementation does not match.** The harness gated the first bar of a signal
run; the engine gates every entry, and returns 63.0% rather than 78.1% on
the same parameters. See ``signals/breakout.py``. What survives is a
drawdown reduction in combination with a size cut, monotone in lookback, and
that finding is itself entangled with the normalisation flaw below. And a
crude momentum signal -- written as a *control* arm for the mean-reversion
test -- returned 85.2% with no losing year, earning 115.6% and 97.9% in the
two years the trend strategy went flat. It carries 6.8x the spread cost and
none of the machinery the baseline has, so it is a lead rather than a
result, but it is the strongest thing to come out of the session.

**A normalisation flaw, found at the end of the day and not yet resolved.**
The confidence size-cut arms were normalised so that mean size over bars
with a *non-zero signal* matched the baseline. But 18% of those bars sit
below the 0.35 entry gate and can never open a position, so normalising
across them scales the bars that *can* open by about 1.12 -- the arm runs
more real exposure than the baseline it is compared against. Return and
drawdown both rising (85.1% at -35.9% against 74.4% at -34.4%) is what that
would look like. **RESOLVED, and then the resolution was itself wrong.** Four arms, all
measured, in the order they were run:

    baseline                    74.4%   -34.4%   2,500 trades
    cut <0.37, normalised       85.1%   -35.9%   2,881 trades
    cut <0.37, NOT normalised   74.4%   -28.2%   2,420 trades
    CONTROL: 1.12x, no cut      71.9%   -44.0%   3,050 trades

The un-normalised arm returning exactly the baseline looked like proof that
the whole +10.7pp was exposure, and that was written here as a retraction
before the control had run. The control refutes it: uniform 1.12x leverage
returns 71.9%, *worse* than baseline. Exposure alone does not buy the gain.

What the arms actually separate:

* **Trimming low-confidence bars**: return unchanged, drawdown -6.2pp. A
  clean risk improvement, and it costs nothing.
* **Amplifying the rest** -- which the normalisation did as a side effect --
  +10.7pp of return for +7.7pp of drawdown.
* **Amplifying everything uniformly**: -2.5pp return, +9.6pp drawdown.
  Strictly worse on both.

So the effect is *differential*: it comes from putting more into
high-confidence signals specifically, not from more exposure. Confidence
ordering does convert into money, which was the original claim.

Two things are worth keeping about how this went. The 85.1% was correct and
the reasoning behind it was not -- it was credited to selection when the
mechanism was a mixture, and a mixture nobody had separated. And the
retraction was written from an argument rather than from the control that
was already queued to test it, which is the same haste in the opposite
direction. **Neither a finding nor its retraction should be recorded ahead
of the measurement that settles it.**

The bucket boundary of 0.37 remains untrustworthy -- it came from an
analysis measured on the fill bar rather than the decision bar -- and is
being re-derived by sweeping thresholds directly.

**On method, because a third of the tests had to be re-run.** Three arms
were inert and reported clean nulls: a demotion multiplying a score that did
not yet exist, a sweep whose parameter lived on a different config object,
and mean-reversion arms sized below the hysteresis floor so nothing could
open. One test was contaminated -- correlating the early part of a trip
against a total that contained it, producing rho +0.433 and an apparent
GBP114k saving that fell to rho +0.04 when corrected. And two mirror arms
beat their treatments, meaning the hypotheses were worse than their
inversions.

The common shape is yesterday's: **a check on what went in, where the
failure was in what came out.** Assert the trade count, not the signal
count. Correlate against what remains, not against the total. Run the
mirror, and believe it when it wins.

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
