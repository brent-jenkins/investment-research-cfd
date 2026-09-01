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

### What changed on 2026-09-01

**The shipped baseline is now 307.6%/yr at −44.1%.** `confidence_sizing_
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
