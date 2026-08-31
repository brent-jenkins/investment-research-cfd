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

**Two related ideas were not built, deliberately.** Detecting genuinely
serious headlines — the aircraft-accident, tariff-announcement, price-rise
class — and separating reported fact from rumour are both the same bet with
a harder classifier in front of it, and both would need article bodies this
project does not collect. Sharpening a filter is the right move when a signal
is *attenuated*. Six formulations returning nothing, including one aimed at
confidence's blind spot, says the signal is *absent*. No amount of filtering
recovers what is not there.

What continues: collection runs, off-hours and cheap, and the archive is what
made these six answers possible rather than arguable. Sentiment appears on
the Signals page as a display-only indicator, wired to nothing.

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
