# cfdinvest

A multi-asset CFD backtesting engine, an automated dealer on a Capital.com
**demo** account, and a manual-trading assistant. It models strategies,
measures risk, deals, and tells you what it did.

The **demo account is the forward record**. The shadow book that used to
fill that role was removed on 2026-08-30 -- it ran a different rule set from
the engine, so every forward figure it produced described a strategy nobody
chose. This sentence described it for twelve days after it was deleted,
which is worth noticing: the opening line of the most-read file in the
project was advertising a component that did not exist.

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

  It was kept for two stated reasons and neither survived. The book ran
  **eight** arms — four on the confidence bar and four varying one other
  setting each (`ts14`, `ts60`, `cool0`, `blk0`). The four confidence arms
  were to answer whether raising the bar helps — a question the
  engine now sweeps in three minutes, and the raised ones sat at 0.50/0.60/0.70,
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
pytest                      # 3,528 tests, offline; 85% covered
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

**The services, since 2026-09-11.** `cfdinvest.service` runs the analysis;
`cfdinvest-dealer.service` deals every five minutes in its own process;
`cfdinvest-monitor.service` checks health every thirty minutes out of
process and alerts. Exactly one of them places orders, and **all three are
`Type=oneshot` on timers** — nothing is resident, so nothing accumulates.

### THE STALE-INTENT LOOP IS REAL, LARGE, AND COSTS NOTHING, 2026-09-19

Asked to look at it as the last open item. Measured at last, using the
`entered_bars_ago` field added on 2026-09-12 for exactly this and left
unread since. **The loop exists and is bigger than anyone guessed. It also
explains none of what it has been credited with.**

Refusal events joined to the intent line of their own pass, five sessions,
`LAST_LOOK_REASONS` deciding which refusals belong to the gate:

    last-look refusal events                     790
      age 0   a fresh signal the quote refused   233   29.5%
      age >0  the engine chasing an entry        557   70.5%

    age of the chase, completed bars   1:163  2:143  3:138  4:113
                                       median 2, max 4

**Seven in ten refusals are the same decision being re-taken.** Per day the
inflation runs 2.3x to 4.8x -- on 2026-09-18, 80 fresh refusals produced 291
chases.

**AND IT CREATES NOT ONE ADDITIONAL BLOCKED INSTRUMENT-DAY.**

    instrument-days refused only while STALE      0
    instrument-days refused only while FRESH     10
    both in the same day                         20

    fresh refusal came first                     20 of 20, no exceptions
    hours from first fresh refusal to last chase median 4.6, max 4.7

Every chase is an ECHO. The engine enters on a bar, the last look refuses it
there, and the engine then holds a position the account never got while the
account re-attempts the same open for the rest of the session. The decision
was taken once, at age 0; everything after it is repetition.

**So the claim this file has carried since 2026-09-10 is WRONG, and it was
repeated on the morning of 2026-09-19 before this was measured.** That claim:
the last look's blocked-outright rate -- 20.7% in week one, 47.1% on 09-12,
52.8% this week -- is "what the stale-intent loop is measuring".

It is not. `refusal_summary` collapses passes to INSTRUMENT-DAYS, and the
loop does not create instrument-days; it only repeats within one. The
cross-check is exact and comes from two independent readings: 10 fresh-only
plus 20 both is 30 refused instrument-days, and `refusal_summary`
independently reports 19 blocked plus 11 opened-anyway, also 30.

**The elevated refusal rate is therefore unexplained again**, and it is the
real open item. 30 of 36 instrument-days refused, against 18.7% modelled,
with `not re-checked` appearing zero times -- so the gate is forming
opinions and refusing on them, and the cause is not the loop.

**WHAT ENFORCEMENT WOULD BUY: NOTHING.** Refusing an open on a non-zero age
is the obvious fix and was left unenforced pending this measurement. It
would remove 557 repeated refusals and **zero trades** -- no stale-only
instrument-day exists, so none could have opened later. The objection
recorded against it (that it suppresses entries in a book whose profit is
concentrated in a few trips, which this week's two-trips-for-86%-of-gross-
wins keeps alive) turns out not to apply either, because there is nothing to
suppress.

**It is not adopted, and the reason is the reverse of the old one.** Not
because it is risky -- it is measurably free -- but because it is worth
nothing and the noise it removes is the only thing making the divergence
visible. Silencing a symptom nobody has explained is how a gate ends up
inert. `tests/test_stale_intent_is_visible.py` keeps failing the build if it
is enforced without somebody saying so, and that guard now has a measurement
behind it rather than a caution.

**THIS IS NOT THE LARGEST OPEN ITEM IN THE DEALING PATH, AND CALLING IT THAT
WAS WRONG.** It is an observability item: log volume, not money.

**What the measurement DID surface, and it is new.** For a median of 4.6
hours -- most of a session -- **the engine's book and the account's book
disagree**: the engine holds positions the account never got. Everything the
engine then computes is computed over that phantom. The concentration cap
measures a share of a gross that includes it, the leverage cap sums a
notional that includes it, and `persist.intent` divides by an equity curve
that has been compounding it. The account is sized against a book it does
not hold.

Whether that costs anything is **not measured**. It is the question worth
asking next, and it is a different question from the one this entry closes.

**THE PHANTOM BOOK: TWO OF THREE CHANNELS WERE WRONG, AND THE THIRD IS
NARROWER THAN CLAIMED.** Measured 2026-09-19, same day, on the owner's ask.
The claim was that because the engine holds positions the account never got,
"the account is sized against a book it does not hold". Three channels were
named for it and two do not exist.

* *The bridge is not sized against it.* `plan_orders` computes
  `wanted_notional = share x exposure_per_unit_size x BALANCE` -- the
  account's own -- and its gross accumulates over the broker's positions.
  This file already says so in the 2026-09-10 retraction: "every position
  figure comes from `positions`, which is the broker's own answer."
* *The share does not shrink when the engine holds more names.*
  `_target_units` sizes each position independently and "no slot count
  divides the equity", so share_i carries no dependence on the rest of the
  book.
* *The concentration cap cannot transmit it either*, and this is the one
  that had to be read rather than assumed:

      ceiling = max_leverage x equity x concentration

  The per-name ceiling is a share of the gross CEILING, not of gross held --
  the function's docstring says exactly that, "so the limit does not tighten
  as the book empties". A phantom changes gross and therefore changes
  nothing here. The cap is NOT inert (removing it doubles SLB's target, from
  -0.109 to -0.220 of equity); it simply does not depend on the rest of the
  book.

**What survives is the gross cap alone, and it binds on 2.25% of bars** --
20 of 889 in the live window, pinned exactly at 3.500 against a mean
leverage of 0.858. That matches the 0.7-5% recorded on 2026-09-07. On those
bars the whole book is scaled, so composition does reach the account.

**AND THE FRAMING ITSELF WAS WRONG, WHICH MATTERS MORE.** The live intent
run is an 889-bar simulation from scratch; the account holds whatever it
managed to open since 2026-08-27. The engine's book has NEVER matched the
account's and was never meant to -- the refusals do not create a divergence,
they make a structural one visible. Sizing what that costs needs an engine
that tracks actual fills, which is the shadow book, deleted on 2026-08-30 on
measured grounds. So this is not a defect with a fix; it is the architecture,
and the one narrow channel through it is 2.25% of bars.

**Two method notes.** The first comparison produced two "different" arms
returning books identical to the decimal -- the signature of an inert arm --
because `max_leverage` is an input to BOTH caps, so raising it a hundredfold
disabled the concentration cap as a side effect. It was not a second
intervention, it was the first one twice. And before reading any of it the
shipped configuration was run three times unchanged: 0 names differing,
largest move 0.00000000. A difference between two runs is not a measurement
until identical runs are known to agree.

**What was built.** `Intent.entered_bars_ago` is carried onto every
last-look refusal and `record_plan` writes it, so `refusal_summary` now
reports `fresh_events`, `chase_events` and `age_unknown_events` from the
STORE rather than from a timestamp join against the journal. Absence is
counted as unknown rather than fresh: every row written before today has no
age, and reading that gap as "no chases" is the reassuring half of a
distinction this file insists on. Five mutations checked, each caught by the
test it was aimed at. The field is recorded and still never acted on --
`tests/test_stale_intent_is_visible.py` continues to fail the build if a
non-zero age starts refusing an open.

Two things that made the measurement possible are worth keeping. The join is
by timestamp because `entered_bars_ago` is logged and **not** written to the
order store -- adding it to the refusal row would make this reproducible
from the record instead of from a scratch parse of the journal, which is the
cheap improvement here. And `journalctl` returns nothing to the service
user, so the first run reported "no intent lines" rather than a permission
error: a silent empty result, read correctly only because the count was
zero rather than small. The journal is now dumped by root and read by the
service user, which also keeps the 2026-09-15 lesson -- research on this box
runs as `cfdinvest` -- intact.

### A TIDY-UP FAILED AND CALLED IT A FAILED RUN, 2026-09-15/16

Reported as a flood of error mail. The run was fine, the account was never
at risk, and the alarm was true about the thing it measured and false about
everything a person would infer from it.

**The sequence, from the journal, three milliseconds apart:**

    19:53:36.028  Persisted 10 artefact(s) to .../runs/20260915T195329Z
    19:53:36.031  Scheduled run 20260915T195329Z failed
                  run_pipeline -> _prune_old_runs -> item.unlink()
                  PermissionError: .../runs/20260911T200634Z/trades.parquet

`_prune_old_runs` is called from inside `run_pipeline`'s try block **after**
`_persist`. So the run did its whole job -- analysis, regimes, signals,
execution, and the artefacts the dealer reads on disk -- and then the
tidy-up raised, skipping `_record_finish`. `last_success` never advanced.

    39   "RUN FAILED" mails on the afternoon of the 15th
    120  monitoring alerts over the next 14 hours, one every 30 minutes
    0    positions at risk
    0    stale decisions -- every run persisted normally throughout

`check_scheduler_health` then said "The last run started and never
completed. Everything on the dashboard is from an earlier run." Both
sentences were false, and it is the kind of false that sends somebody to
look at the wrong thing during a session.

**THE CAUSE WAS MINE, FROM FOUR DAYS EARLIER.** The 2026-09-11 leak hunt ran
the pipeline three times in one process to measure retained memory. It was
run as **root**, so it left 43 artefacts owned by root -- three run
directories at 20:05:48, 20:06:11 and 20:06:34, plus four monitoring
snapshots. They sat harmlessly until retention reached back far enough to
delete them, which happened on the 15th.

**Research on the box runs as the service user.** `sudo -u cfdinvest -E` was
used for every measurement this week and was not used on 09-11. The cost was
not the files; it was that they were a delayed-action fault in the one
process that must not fail.

**THE SECOND HALF IS WORSE AND WAS NEARLY MISSED.** The loop had no
per-directory handling, so the first undeletable file abandoned the entire
sweep. Retention did not partially work -- it stopped. **143MB was reclaimed
the moment it could run again**, and `runs/` went 223MB to 86MB. A
housekeeping job that silently stops is precisely the failure its own
docstring warns about: "a VM fills its disk after a few months and the
failure surfaces as something unrelated."

**AND THE RIGHT CONTRACT ALREADY EXISTED, ONE MODULE OVER.**
`persist.retention.prune_runs`, which the standalone `prune-runs` job calls,
documents itself as **"Never fails the caller"** and returns its error in the
result. So one responsibility is implemented twice with different contracts,
and the weaker one sat on the critical path. That duplication is **recorded
rather than resolved**: the two also disagree about policy -- `RunConfig.
keep_runs` deletes, while `retention.DEFAULT_KEEP_FULL` slims and
`DEFAULT_KEEP_RUNS` deletes -- so collapsing them changes what is kept, and
that is a decision rather than a bug fix.

**What was done on the box**, market shut, nothing touching the account or
its config:

    chown 43 root-owned artefacts back to cfdinvest   runs/ and monitoring/
    cfdinvest-schedule prune-runs                     slimmed 6, freed 143MB

The chown hands them to the owning user so the app's own retention removes
them on its own schedule, rather than deleting data by hand.

**What was changed in code.** `_prune_old_runs` now
catches `OSError` per directory, carries on to the next, and returns the
refusals with the error each raised; `run_pipeline` logs one warning naming
them and finishes the run normally. A warning rather than a failure, because
the artefacts are written and the decision is fresh -- what is wrong is the
disk.

`tests/test_housekeeping_cannot_fail_a_run.py`, five mutations checked:
re-raising, `break` instead of `continue`, ignoring the keep window,
swallowing the refusal silently, and the call site dropping the result.
All five caught by the test each was aimed at. The undeletable file is
monkeypatched rather than chmod'ed, so the test means the same thing on
Windows, where a read-only bit does not reproduce a POSIX denial.

**THE MUTATION HARNESS RESTORED THE WRONG BASELINE, AND IT IS THE THIRD
TIME THIS CLASS HAS APPEARED IN A WEEK.** It restored with `git checkout --`
against a fix that was not committed, so the first mutation silently
reverted the fix and the next three reported "anchor not found" while one
reported a pass against the *old* code. A harness that restores the wrong
baseline is the same error as a mutation that does not mutate, and it fails
the same way: green for the wrong reason. It now restores from a pristine
copy, and every mutation was re-run against it.

**RETRACTED WITHIN THE HOUR: "NOT DEPLOYED" IS NOT A STATE THIS PROJECT
CAN BE IN.** This entry said the fix was committed and not deployed, and the
commit message for 586f2d4 ends with the words "Not deployed." Both are
false. `deployment/webhook_listener.py` deploys on **every push to main** --
`should_deploy` is "a push to main, and nothing else" -- and it had already
deployed the fix **two seconds after the push**, before either sentence was
written:

    06:41:30  === deploy starting ===        (webhook, ~30s after the push)
    06:41:32  Now at 586f2d4 (was b329d18)
    06:45:04  === deploy starting ===        (the deliberate deploy)
    06:45:06  Now at 586f2d4 (was 586f2d4)   <- already there

The same is true of every commit this week. Checking the box immediately
after a push and reading `8fb034a` proved nothing except that the webhook
had not fired *yet*; it fired seconds later. So "committed, not deployed,
your call" -- said repeatedly this week -- was never available, and saying
it was the assistant describing a workflow the deployment does not have.

**THERE IS NO SESSION GUARD, AND THE RULE HAS ALREADY BEEN BROKEN TWICE.**
This file states "Do not deploy during a session" as a lesson that cost five
dealing passes on 2026-09-10. Nothing enforces it. Read off the deploy log:

    2026-09-14T19:25:00Z  a88497b   35 minutes before the close, market OPEN
    2026-09-14T19:39:33Z  a88497b   21 minutes before the close, market OPEN

Both were pushes of a docs-and-tests commit, so nothing behavioural shipped
into a live session and no pass is known to have been lost -- the units are
`Type=oneshot` now, so a restart no longer costs the nine minutes the
2026-09-10 entry measured. That is luck rather than design: the same
mechanism would ship an engine change mid-session just as readily.

**The distinction worth keeping.** "Recorded rather than adopted" is still
real and still safe -- `leverage_cap_selects`, `entry_recheck`,
`max_opens_per_pass` and the decayed trim are all code that deploys and
changes no behaviour, because they are defaults nothing wires in. What does
not exist is **"changed but not deployed"**. A push is a deploy, and the
only thing standing between a commit and a live trading host is whether the
market happens to be open.

Not fixed here. A guard belongs either in the listener (refuse while a
market in the universe is open, and say so) or in `deploy.sh`, and choosing
which -- and whether an urgent fix may override it -- is the owner's
decision rather than a side effect of finding this.

**The alarm clears by itself at market open** and was not silenced by hand.
`last_success` is a true record of the last successful run; writing a
success that did not happen to stop the mail would be falsifying the one
store that says whether this machine is working. The first `run-intraday`
after 13:30 UTC records success and the monitor goes quiet.

**Checked and benign, because it looks alarming in the log.** The intent
line reads "Engine intends 11 position(s) on **equity 7861497.74**" against
an account balance of £4,747. That is the engine's own compounded research
equity used only as a denominator -- `persist.intent` computes `share =
units x price / (equity x exposure_per_unit_size)` and the dealing path
applies the resulting SHARE to the broker's real balance. Not a fault.

**What the account actually did on the 15th:** `opened 0, resized 0, closed
0, refused 11, balance 4747.13` on every pass through the afternoon. All
eleven intents were entered on an earlier bar and refused by the last look.
That is the stale-intent loop this file already records as safe, noisy and
unfixed -- unrelated to the mail, and still the largest open item in the
dealing path.

### THE TURNOVER GAP WAS TWO DIFFERENT STRATEGIES, 2026-09-15

Chased the residual the flatten measurement left behind -- the
reconstruction trading four to five times the recorded 577 round trips.
Most of it is a comparison error, the rest is still open, and on the way
the engine turned out to be running a rule nobody described.

**FIRST, THE TURNOVER IS NOT ANOMALOUS.** There is no churn to explain:

    names held per bar        2.03      bars per session      7.0
    mean hold                 5.0 bars  trips per session     2.81
    2.03 * 7.0 / 5.0                                       -> 2.83 predicted

The arithmetic closes to two decimal places. A book doing more trips is a
book holding more names, and nothing else is happening.

**SECOND, THE RECORDED 577 DESCRIBES A TEN-NAME BOOK.** Commit 56d0b13 is
docs-only, so the sweep script is gone -- but the configuration at that
commit is not. `universe.max_target` **did not exist** (added two hours
later at b6696dd, as 25), so `resolve_target` fell through to the size
curve, which peaked at 10 (82.3) with `size_preference = "largest"` and an
8pp floor putting 12 (73.8) outside it. The reconstruction ran twenty-five.

    arm                     trips   vs record   cost/gross     gross      cost
    RECORDED 2026-09-04       577          --        37.2%       n/a       n/a
    cap5/t25                2,604       4.51x       862.4%    12,247   105,621
    cap5/t10                1,337       2.32x       320.6%    28,010    89,803
    cap5/t8                 1,151       1.99x       219.4%    38,397    84,249
    cap5/t10 PINNED         1,352       2.34x       332.0%    29,429    97,714

Matching the universe halves both gaps. **It closes neither**, and 2.32x on
the count with 8.6x on the cost share is what is left.

**GROSS FALLS AS THE UNIVERSE WIDENS** -- 38,397 to 12,247 -- while cost
rises only a quarter. So the cost share is driven by collapsing gross
rather than by trading more, which is the opposite of what a turnover
explanation predicts, and it reproduces from a new direction the
2026-09-12 finding that the size curve's ordering reverses.

**A PREDICTION WAS STATED AND FAILED.** The last structural difference
between the two experiments was that a research run from the production
config pins ONE list across the span while the harness re-selects weekly
across forty distinct names. Predicted, before the run: pinning lands well
under 1,337 trips and well under 320%. Measured: **1,352 and 332.0%** --
fractionally *worse* on both. Re-selection is not the remaining cause, and
recording the failed prediction is the point of having made it.

The sizing sequence in the engine loop is byte-identical between 56d0b13
and today, so what remains are the five engine commits that landed after
the sweep, the lake having been re-ingested since, and whatever the lost
script did. **The battery still does not reproduce and is still the top
open item.**

**CAGR IS UNREADABLE ON THESE ARMS.** Every one ends with equity at or
below zero, so `arm_metrics` reports `nan` and only trips, gross and cost
mean anything. That is itself a statement about the reconstruction.

**AND THE ENGINE IS RUNNING A RULE NOBODY DESCRIBED: THE DECAYED TRIM
COMPOUNDS.** Four lines of the loop, in this order:

    resize       = needs_resize(units, target_units, threshold=3.0)
    keep         = (new_held == held) & (new_held != 0) & ~resize
    target_units = np.where(keep, units, target_units)      <- target := HELD
    target_units = _trim_decayed(target_units, units, confidence, config)

`rebalance_threshold` is 3.0, so a held position is almost never "drifted",
so `keep` is True and the third line **replaces the freshly computed target
with what is already held**. The trim then multiplies that by 0.5. Next bar
the held figure is the trimmed one and it is halved again.

The threshold cannot stop it and makes it worse: `needs_resize` measures
drift relative to CURRENT units, so as a position shrinks its distance from
full conviction grows, and once the ratio passes 3.0 the position is
restored to full size and the decay restarts. Over 11,058 held-bar pairs:

    shrank past half   29.2%   <- 0.375x, being conviction 0.75 x trim 0.5
    halved              2.2%
    roughly unchanged  25.8%
    grew               34.3%   <- the restore

    units at exit / at entry              median 0.1844
    trips exiting under 10% of entry size          29.2%

**The median trip exits holding 18% of the size it entered with.** The rule's
own commit says the opposite in as many words: "Trimming to half leaves a
position that continues to run."

**Confirmed on the live persisted run**, not only in a research arm --
three consecutive bars of one position, 0.75 conviction times 0.5 trim:

    2026-08-07 14:00   6945.28   1.617x
    2026-08-07 15:00   2604.48   0.375x
    2026-08-07 16:00    976.68   0.375x
    2026-08-07 17:00    366.25   0.375x

**THE ACCOUNT DOES NOT DO THIS, AND THAT IS THE PART THAT MATTERS.**
`bridge.plan_orders` scores the same step with the same `needs_resize` at
the same threshold: 0.375x is a drift of 0.625 against 3.0, so the intent
is `action="none", reason="inside the rebalance threshold"` and no order is
placed. **The engine's book decays toward nothing while the account holds
full size**, so the backtest models a fraction of the exposure the account
carries. That is the direction that understates gross, and this file
records the reconstruction generating gross worth 4.7% of average equity
where the record generates 250%.

It also explains a cost channel measured today and not previously priced:
**resizes are 58% of the entire spread bill in every arm**, 1.4x what the
round trips themselves cost, while drift rebalancing is switched off.

**IT WAS LATENT UNTIL THE GATE MOVED.** The trim's threshold is
`entry_confidence` by deliberate design -- "one owner, and raising the entry
bar raises this with it". It was swept at 0.35, where a held position's
confidence often recovers above the bar and the trim stops firing. At the
0.75 shipped on 2026-09-04, against a median confidence of 0.34 among names
with a direction, it essentially never recovers. **This is the third time
the same mechanism has been found in eight days** -- the breakout gate's
0.667 ceiling, the last look's unreachable pass mark, and now this. The
gate moved and nothing re-checked what moved with it.

**WHY FOUR PASSING TESTS MISSED IT.** `tests/test_entry_gate.py` covers this
rule four ways and every one calls `_trim_decayed` ONCE with
``target == units``. The function is correct in isolation; the defect is the
order of the calls around it, and a test of the function can never reach it.
A check on what went in, where the failure was in what came out.

**NOTHING IS CHANGED.** Correcting the order moves every recorded figure in
this file, including the sweep that chose `decayed_multiplier = 0.5` -- that
table measured the compounding version, so it describes a rule nobody
stated. `tests/test_the_decayed_trim_compounds.py` asserts the behaviour
that runs today so it cannot change quietly in either direction, the way
`entered_bars_ago` is reported and not enforced. Four mutations checked.

**ONE MUTATION DID NOT MUTATE, AND IT IS THE SECOND TIME.** M1730 was meant
to apply the trim to the fresh target instead of the held units. The first
attempt INSERTED that call and left the original in place, so the original
went on compounding and all five tests passed -- a surviving mutation
reporting an inert test that is not inert. Moving the call rather than
adding one fails three tests, and incidentally demonstrates the fix:
under that order the position settles at half and stays there.

**A FIGURE RECORDED YESTERDAY NEEDS ITS ARM NAMED.** This file says "the
time stop now closes 4.7% of positions against the 88% it once did". That
is true of the FLATTEN arm, where the bell closes everything before a
0.29-day stop can fire. In the carry arm it closes **69.4%** of positions.
The 4.7% describes what the account does, since the account flattens; as a
statement about the corrected engine it is wrong.

**Today's arms are a new series and must not be compared with the
2026-09-12 table.** That entry records 1,171 trades for cap5/t25 carry and
says it was run with `environment_in_confidence` True; the flag reads False
in `config.toml` at every commit checked, including 56d0b13. The
discrepancy is unexplained and both numbers stay where they are rather than
one being quietly adopted.

### THE 0.75 GATE IS A BREAKOUT GATE, 2026-09-14

Asked why the app opened two positions from a hundred tracked names. The
answer is not the screen, not the last look and not caution. It is that a
flat breakout component dilutes `strength` by a third, and the gate was
raised to a level that dilution puts out of reach.

**The chain, each step measured.**

    100 candidates -> 50 traded -> 40 with a direction -> 5 intended -> 2 opened

    distinct names clearing 0.75 at ANY bar, out of ALL 100:   9
    the best single bar (13:00):                               6
    median confidence among names with a direction:         0.34

So deleting the screen entirely would have offered nine candidates, not
thirty. The binding constraint is the gate, and the gate sits at roughly the
**90th percentile** of the strength distribution.

**`confidence` is `|strength|` for three-quarters of the book.**
`environment_in_confidence` ships False, so support is 1.0, and agreement is
1.0 for 53 of 68 names. The same six names clear on either measure.

**AND STRENGTH IS DILUTED BY A COMPONENT THAT ABSTAINS.**

    crossover   non-zero on 105 of 105 names
    momentum    non-zero on 105 of 105
    breakout    EXACTLY ZERO on 87 of 105 (83%)

`trend_strength` renormalises over components that are `notna()`, so a
breakout reading of **0.0 is present and counts in the denominator at full
weight**. `component_agreement` does the opposite and says so in its own
docstring -- zero "treated as abstaining... so a flat breakout does not
dilute a genuine agreement between the other two". **The two functions hold
opposite conventions for the same value**, and only one of them dilutes.

The consequence is arithmetic:

    crossover +1.0, momentum +1.0, breakout flat -> (1+1+0)/3 = 0.667
    the gate                                                   0.75

**A name with maximal trend and maximal momentum cannot clear the gate
unless the breakout component is live.** Measured on the same bar: all 6
names clearing 0.75 had a non-zero breakout, 6 of 6. Had zeros abstained as
agreement treats them, median strength would be 0.556 rather than 0.371.

**This is the `environment_in_confidence` failure in a new place**, and this
file already states its shape: "no signal can score above 0.5 in a volatile
market however strong it is, and any gate above 50% silently forbids
trading". Same mechanism, different factor, and it arrived the same way --
the gate moved and nobody re-checked what the scale could reach.

**It was harmless until 2026-09-04.** At `entry_confidence = 0.35` a 0.667
ceiling is irrelevant. Raising the gate to 0.75 made the dilution binding,
and the sweep that justified 0.75 was itself measured on diluted strength,
so it could not have seen this.

**The gate and the dilution are entangled and must not be changed apart.**
0.75 on diluted strength is roughly 0.50 on undiluted -- and 0.55 measured
at cost/gross 98.5% against 0.75's 37.2%. So "fixing" the dilution alone
lowers the effective gate into a range already measured as far worse. The
honest experiment is to correct the convention AND re-sweep the gate, and
the likely outcome is a higher nominal number giving the same selectivity:
no change in behaviour, but a knob that means what it says.

**What is genuinely untested** is the rule this has silently imposed: that
this book may not take a strong trend without a breakout. Nobody chose that,
it has never been measured, and it is not obviously wrong.

**Trade count is not arbitrary -- it tracks how far the tail reaches.**

    day          median   p90     cleared 0.75
    2026-09-08    0.394   0.727        8
    2026-09-09    0.465   0.746        7
    2026-09-10    0.420   0.558        2
    2026-09-11    0.388   0.472        1
    2026-09-14    0.371   0.739        6

The median barely moves; the p90 does, and the count follows it exactly. The
gate fires on days when signals are strong and stays quiet when they are
not, which is the behaviour the owner asked to protect when raising the
question -- and the reason not to reach for a lower bar.

**AGREEMENT EARNS ITS PLACE, AND IT MAY BE THE FIRST LOSING SUBSET THIS
PROJECT HAS FOUND.** 374 signals over five days, decision bar to session
close:

    agreement 1.000   n=325   mean +0.413%   win 66%
    agreement 0.500   n= 48   mean -0.314%   win 38%
    difference                     +0.746pp

Divided components LOSE. Every selection idea in this file has died on
"no band is unprofitable" -- the confidence bands, the sixteen-feature
model, the news programme. This one has a band that is, and it is already
computed on every bar.

It is currently used only to HALVE confidence. If divided signals genuinely
lose, halving is too generous and refusing is the rule to test.

**Both readings are screens and neither may be adopted on one.** They are
overlays over persisted runs, scored decision-bar-to-close rather than over
the engine's real holding period, on five days. The rule is that a finding
is re-measured with the rule expressed as `ExecutionConfig` settings and
applied by the engine before it ships, and three findings in one weekend were
once measured one way and implemented the other.

**Agreement is effectively binary**, which is worth knowing before anyone
tunes it: 0.5 is arithmetically impossible with three voters, and 48 of 374
observations sit at exactly 0.5 with one at 0.667. Two components vote,
almost always -- because the third is the one that abstains.

**The settings behind all of this are stale and the owner said so before the
measurement did.** `component_weights` is `{crossover: 1.0, breakout: 1.0,
momentum: 1.0}` -- never tuned. `breakout_window` is 55 hourly bars, about
eight sessions. Both predate the intrabar look-ahead correction, and this
file already lists the breakout lookback among the parameters "still fitted
to the flawed engine".

### The macro gate, measured at last, 2026-09-12

Asked to measure it and then decide. The answer is that **the gate as this
file has described it does not run**, and the cut it rests on **points the
wrong way** — so it is not adopted, not switched on, and not deleted either.

**FIRST, THE DESCRIPTION IN THIS FILE WAS WRONG ABOUT THE MECHANISM.**
Written here twice, and in a code comment I added the same day:
"`allow_short` permits a short only in a `crisis` combined regime or when
macro reads `contraction`". That expression is real and it runs **only in
`mode="gate"`**. The shipped config is `regime_mode = "tilt"`, where
`allow_short` is REPLACED by a tilt read off the combined regime and the
macro term is discarded.

So the thing the owner objected to — a monthly, lagged, year-over-year FRED
label vetoing what the book may short — **is already inert in production**.
The consequence written down was right and the route was wrong, which is the
more dangerous shape: a reader checking the claim would have found that line
of code and believed it. Corrected in `signals/filters.py` and in
`schedule/app.py`.

**Macro still reaches the decision, by the other route.** It is an input to
`classify_combined`, whose rules key off `contraction`, `stagflation` and
`overheating`. Measured on the shipped configuration, removing macro
entirely changes the live book on **3 of 5** recent session bars — on
2026-09-04 the difference between holding one short and none.

**THE CUT IT RESTS ON IS BACKWARDS.** 5,590 completed round trips, paired by
`backtest.metrics.round_trips` rather than by anything written for this, and
grouped by the macro label standing at each trip's ENTRY:

    shorts entered under contraction     n=1450   mean  -23   win 29.7%
    shorts entered under anything else   n=1074   mean  -15   win 34.1%
                                         difference -8, t = -1.39

The trades the term PERMITS are worse than the ones it would refuse, on both
mean and win rate.

**And the mirror says it is not directional at all.** Longs under
`contraction` average **-17 against -15** elsewhere. Both sides are
marginally worse under the same label, so it marks a period rather than a
direction — the mirror test this file already uses to retire hypotheses, and
it wins again here.

`contraction` also covers **3,836 of 6,713 bars, 57% of the span**, so it
would not be a selective gate even switched on.

**Over the full span the three arms are inside the noise floor:**

    arm                              CAGR    maxDD   trades      L/S
    shipped (macro in price bars)  -77.4%   -99.6%   25,829  10953/9188
    no macro (market data alone)   -79.5%   -99.7%   25,852  11340/8867
    dated   (own monthly calendar) -73.6%   -99.3%   24,545   9920/9035

    per year   2023  2024  2025  2026
    shipped     -83%  -77%  -72%  -78%
    no macro    -84%  -80%  -77%  -77%
    dated       -78%  -76%  -72%  -67%

5.9pp between best and worst against an 8pp floor. The correctly dated arm
is best in 4 of 4 years and both halves, which is more consistent than a coin
flip and still inside the floor.

**THE FIGURES ABOVE SIT ON A HARNESS THAT DOES NOT REPRODUCE**, and that is
the honest limit of all of it. The baseline is -73.6% where +191.0% was
recorded, so every band loses money and only the RELATIVE ordering within a
run is worth reading. `t = -1.39` on overlapping trips is nothing in either
direction. **This is an absence of support, not evidence of harm.**

**The decision: nothing changes.** The macro input stays, the gate-mode veto
stays off, and neither is altered on a null.

* Adopting the veto is unsupported — the one cut it rests on is negative and
  its mirror kills the mechanism.
* Removing macro entirely is equally unsupported — that arm was the *worst*
  of the three, also inside the floor. "No evidence either way" is not a
  reason to change a live input, which is how `leverage_cap_selects` was
  handled on 2026-09-07.
* What would settle it is a harness that reproduces the battery, which is
  already the top open item and blocks every parameter claim in this file.

**What the owner's objection turns out to be worth.** It was right about the
principle and the system was already safe in practice: in tilt mode nothing
backward-looking outranks the day's own signals, because the veto never
fires. The residual influence runs through one of four inputs to a combined
regime, and it shows no directional skill. If `regime_mode` is ever moved
back to `gate`, that veto comes alive and **nothing measured here justifies
it** — recorded next to the code so the next reader of that line sees it.

### The live decision is bounded at six months, 2026-09-12

The owner's instruction, made three times before it was acted on: this is
not a long-term platform. It predicts the next few hours from recent
movement, and a price from 2023 cannot inform that. Each time I answered by
defending the full-span run as the thing a short window must match, which is
the premise that should have been doubted.

**The live run was bounded by nothing.** `[market] start = "2005-01-01"` and
no window, so it read **6,467 bars over 3.69 years and grew by seven every
session**. 3.69 years was never chosen; it is what "no window" evaluates to
against this lake, and it would have gone on lengthening.

**Six months was the owner's figure and it is now measured.** Five recent
session bars, final-bar target as a SHARE OF EQUITY -- what `persist.intent`
carries -- against the whole-lake run on the same bar, universe pinned to
that day's recorded selection, macro dating fixed on both sides:

    window   bars   reproduces the whole-lake decision
     12m     1750   5 of 5
      9m        -   5 of 5
      6m      890   5 of 5
      4m        -   5 of 5
      3m      442   5 of 5
      2m      308   5 of 5   <- and must not be trusted
      1m      155   0 of 5

**Two months reproduces and is still wrong**, which is why the number comes
from the warm-up rather than the sweep's edge. `breakdown_baseline_window` is
252 bars over a 63-bar `correlation_window`, so 315 bars must exist before
the longest input does. Two months is 308. It passed five dates from below
its own floor, which is the shape of a result that reverses on the sixth --
this file has been caught by exactly that before. Six months is 890 bars,
2.8x the warm-up.

**The cost, which is what the owner asked for on 2026-09-11.** One live pass,
50 names:

    full   6,467 bars   17.4s   867MB peak   42% of the 2G ceiling
     12m   1,759 bars    7.0s   568MB        28%
      6m     896 bars    4.9s   565MB        28%
      3m     448 bars    4.2s   562MB        27%

3.6x faster, and since ~560MB is the interpreter and libraries the
analysis's own footprint falls from about **307MB to about 5MB**. Three
months buys a further 0.7s for half the margin, so it is not taken.

**The macro calendar fix ships with it because neither is safe alone.**
`_macro_lookback` resolves to `periods_per_year` = 1,764, so the growth and
inflation classifiers do a year-over-year change counted in PRICE BARS and
need about 12.4 months of them. Below that every macro label reads `unknown`,
and `allow_short` permits a short only on `contraction` or `crisis` with
`allow_when_unknown` False. **Shortening the window alone would have silently
stopped the book shorting** -- half of a two-sided strategy -- and the run
would have looked healthy doing it.

**Research is untouched**, which is the same split `INTENT_EXECUTION_LAG`
already makes and the reason no recorded figure moves. A backtest needs 3.65
years to MEASURE across many periods; the dealer needs recent bars to
PREDICT the next few hours. Same engine, two uses; only the second is
bounded, and `RegimeConfig.macro_on_own_calendar` still defaults False.

**The suite found a defect in the first version, and it is the useful part.**
Six months is a statement about RECENCY, so it only means the same thing at
the interval it was measured on -- 890 bars hourly, about 126 daily.
`test_run_once_through_the_cli` runs at `1d` and reported `no data to trade`:
an empty book from a starved panel, indistinguishable from an empty book from
a quiet market. `_intent_window_start` now takes the LONGER of six months and
whatever the warm-up needs at the interval in hand, so production is
unchanged and a coarser resolution still gets its longest input. A second
defect in the same helper: estimating bars-per-session by rounding gave 6
where the lake holds 7.06, stretching the production window to 6.8 months and
quietly overriding the figure that had been measured.

**Verified on the deployed code, on the box**, rather than on a script that
built the window itself -- `for_live_intent`'s own output against the
unbounded run, same bar, same pinned universe: **5 of 5 identical**. Absolute
name counts differ from those recorded on 2026-09-04 because the lake has
been re-ingested since, so the two are not comparable; what is compared here
is bounded against unbounded on identical inputs.

Eight mutations checked by breaking them. **Two of my mutations were wrong
first time** and are recorded as such: one landed in `_completed_bar_end`,
which carries the same three lines earlier in the file, and one added an
unused constant and proved nothing. A mutation that does not mutate reports a
surviving check as an inert test.

**What is still the owner's call.** Whether a monthly, lagged,
year-over-year macro label should gate this book at all. It is now correctly
dated rather than resolving to `unknown`, but the gate itself has never been
measured on its own terms, and this file's own 2026-08-30 finding -- every
intervention slower than the four-hour hold failed -- is an argument against
it rather than a measurement of it.

### The split stopped ingesting, and nothing could have said so, 2026-09-12

Asked whether the system still works the same way after the architectural
change, and what figures prove it. It did not, the answer was two days from
costing a session, and the figures are the point of this entry.

**THE SPLIT BROKE THE ONE ORDERING THE SINGLE LOOP EXISTED TO PROVIDE.**
`ingest-intraday` and `run-intraday` were both gated on **`run-intraday`'s**
last-run time. In one sequential loop that was safe: both came back from a
single `jobs_due` call and both ran before either timestamp moved. Split into
`cfdinvest-ingest` and `cfdinvest`, on independent one-minute timers, they
ask independently -- and whichever asks first records `run-intraday` and
closes the gate on the other for the whole fifteen-minute cadence.

Replayed minute by minute over a session, `jobs_due` being pure:

    job               one loop    split, over all 24 interleavings
    ingest-intraday         26    0..26      <-- starved in half of them
    run-intraday            26    26
    every other job     as-is     identical

**Twenty-six ingestions a session become zero**, and it is not even
consistently broken: it depends on which timer systemd happens to fire first,
and once the analysis unit wins it stays won, because the gate is shut every
minute thereafter. The lake would stop being refreshed intraday while the
analysis went on reading it.

**The consequence is the 2026-09-11 stall arrived at from the other side.**
Not a wedged process this time -- a healthy one, reading bars that stopped
arriving. `decision_is_stale` would refuse opens on the ageing decision bar,
so the account would have sat flat through a session with every unit
reporting `Result=success`.

**And every check in place said fine.** That is the finding worth keeping:

    all four units                Result=success, ExecMainStatus=0
    the job partition             exact, 13 of 13 owned, 7 mutations caught
    an idle tick                  0.76s, 127MB
    jobs actually run since        2 of 13

The last row is the one that matters and it is the one nobody had. The unit
files were installed at 09:57 UTC on 2026-09-12 and the market has been shut
since; in 689 invocations the split has run **`poll-live-quotes` and
`advisor-alert`, 45 times each, and nothing else**. The three intraday jobs
have never executed under this architecture at all. The first time they would
have was 13:30 UTC on Monday 2026-09-14.

**A structural check cannot see this, and that is why a behavioural one now
exists.** `test_jobs_are_partitioned.py` proves every job has an OWNER. It
passed throughout. A job can be owned by a unit that never becomes due, and
the partition stays perfect while the work stops.
`tests/test_split_runs_the_same_work.py` replays a whole day through both
architectures and compares what actually ran, under every interleaving.

**The fix states the ordering the loop got for free.** Ingestion keeps its
own clock; the analysis is owed once ingestion has written something newer
than the last analysis read. It cannot starve on a *failing* ingest, because
`_command_daemon` records a job's timestamp whether or not it raised -- only
on an ingest unit that is not running at all, which is a condition worth
halting for rather than dealing through.

**The cost is one tick of latency.** Analysis no longer rides in the same
call as ingestion, so it lands on the following minute. That is a real
behavioural change and five existing tests asserted the old shape -- each
handed `jobs_due` a mid-session state in which ingestion had never run. They
assert the requirement now (signals refresh intraday, spreads on the run's
tick) rather than the implementation that used to satisfy it.

**One mutation survived and it is recorded rather than quietly dropped.**
Claiming `ingest-intraday` in two units changes no count, because each job
now keys off its own last-run time and whichever unit asks first records it.
That is true of a simulation where units take turns and *not* a guarantee
about concurrent processes, so the claim was struck from the new test's
docstring and double-claiming stays where it was already owned, structurally,
by the partition test.

**What is still not proven.** Everything above is the scheduler's decision
layer, which is pure and therefore fully testable without a market. It says
the right jobs run at the right cadence in the right order. It says nothing
about whether a job behaves the same in its own process -- and the only
evidence for that is Monday's session.

### The battery, narrowed from 240pp to 56pp, 2026-09-12

The harness went from "does not reproduce" to "reproduces the shape and not
the magnitude", and four factors were measured on the way. It still does not
reproduce, and the shipped universe still rests on a figure nothing
regenerates -- but the search is now bounded rather than open.

**Reconstructed, with `environment_in_confidence` True:**

    arm                                CAGR     gross  trades   maxDD  cost/gross
    cap5/t25 carry (the 09-04 shape)  -7.6%    +4,048   1,171  -26.9%       723%
    cap5/t30 flatten (rec. +116.0%)   -9.3%    +1,401   1,117  -33.9%      2268%
    cap8/t50 flatten (rec. +191.0%)  -14.9%      -916   1,737  -47.6%      4793%

    recorded 09-04 gate 0.75         +48.4%       n/a     577  -13.1%      37.2%

**GROSS IS POSITIVE on the carry arm**, which is the first thing that
matches: this file records "gross P&L is positive in every arm -- the entries
predict; the signal is real", and the reconstruction now agrees. Trade count
is within 2x of the recorded 577 and drawdown within 2x of -13.1%, against
25,829 trades and -99.6% before. The signal handling is right.

**What was measured and what each was worth.**

* *The screen's membership as an entry gate*, which a run from the
  production config does not apply: 38,877 trades to 25,829, about 7pp.
* *Measured spreads against the 5bp constant*: **28pp**. Real, and a finding
  on its own.
* *`environment_in_confidence`*: **62pp, and a 15x change in trade count.**
  The gate is `confidence >= 0.75` and this decides whether confidence
  includes the 0.5 high-volatility multiplier. With it, `combined.py` says
  "no signal can score above 0.5 in a volatile market however strong it is,
  and any gate above 50% silently forbids trading". It ships as False, so
  0.75 is routinely cleared now and was a near-veto when the figures were
  taken. **Two eras of recorded figures, two meanings of the same number.**
* *The session flatten*: it **hurts** here -- cost/gross triples, 723% to
  2268% -- where this file records flattening as worth +55.4pp. That
  disagreement is the largest single piece of the residual.

  **RETRACTED 2026-09-14. The flatten is worth 1.7pp and is not the
  residual.** That comparison set the flatten against an arm differing in
  more than the flatten. Run properly -- ONE membership mask, two arms
  differing only in whether a position may live past the bell, same gate,
  same costs, same names:

      arm                 trips     CAGR    maxDD  med bars  max  8+ bars
      flatten at bell     3,234   -53.8%   -94.4%       4      6      0%
      carry overnight     2,607   -52.1%   -93.6%       5     34     16%

  1.7pp, inside the 8pp floor, and drawdown identical rather than halved.

  **And it takes two recorded findings with it.** Carry unlocks exactly the
  cohort the flatten makes impossible -- 16% of trips at eight bars or more,
  up to 34 -- and the book is no better for it. So "trips held 8+ bars earn
  GBP528k... 20% of trips produce all the profit" does not survive here
  either; that reading is from 2026-08-30, the flawed-engine era, and has
  never been re-measured since the time stop's look-ahead was corrected.
  Neither has "+55.4pp with drawdown roughly halved".

  Both are screens on a harness that does not reproduce, so neither recorded
  figure is REFUTED -- but neither can be quoted, and the flatten is no
  longer a lead worth chasing.

  **What survives as the lead is turnover, and it is large.** Both arms trade
  far more than anything recorded: 3,234 and 2,607 round trips against the
  577 recorded for gate 0.75 on 2026-09-04. Four to five times. At a measured
  7.14bp that is most of the 0.2586 percentage points per trade separating
  the reconstruction from the record -- and "the trade count is orders out"
  is how the 2026-08-31 harness error was caught.

  Also confirmed in passing, and it agrees with the corrected engine: the
  time stop now closes **4.7%** of positions against the 88% it once did.

  **QUALIFIED 2026-09-15: that is the FLATTEN arm only.** The bell closes
  everything before a 0.29-day stop can fire, so 4.7% describes what the
  account does rather than what the engine does. In the carry arm the time
  stop closes **69.4%** of positions.

**Eliminated by measurement rather than argument**, each with the reading
that ruled it out: exposure (mean leverage 0.71 against the recorded 0.89),
`spread_panel` and `entry_gate` being None (both resolve at run time), and
the flatten as the cause of negative gross (gross was negative both with and
without it before the confidence fix).

**The residual, stated precisely.** At similar leverage and a similar trade
count, the recorded book generates gross profit worth about 250% of average
equity over the span; the reconstruction generates 4.7%. That is roughly
100x per trade, and it is a sizing or holding-period difference rather than
a selection one -- the names and the entry count are close, the money per
trade is not. Not isolated.

**And the ordering reverses, which matters more than the level.** The
battery's case for the shipped universe is that bigger is better: cap5/t30
at 116.0% against cap8/t50 at 191.0%. The reconstruction puts cap8/t50
**worse** than cap5/t25 (-14.9% against -7.6%). If that holds, the argument
for widening the universe is not merely unreproducible, it points the other
way.

**So `max_per_sector = 8` and `universe.max_target = 50` remain unjustified
and are still not changed here.** Changing the universe changes what the
account trades, and doing it on a reconstruction that itself misses by 56pp
would be replacing one unverified number with another.

### The 2026-09-08 battery does not reproduce, 2026-09-12

A sweep harness now exists in the repository -- `cfdinvest/research/harness.py`
-- and the first thing it establishes is that the figure which chose the
shipped universe cannot be regenerated.

**What the harness does.** It rebuilds the screen's membership over history
and passes it as `entry_gate`, which is what a sweep must do and what a run
from the production config does not: in production the traded list IS today's
recorded selection and the run decides about one bar, while over 3.65 years
the screen RE-SELECTS. `compose_entry_gates` names this exactly -- "a
universe screen is another" contributor -- and records a past bug where a
caller's gate was silently discarded, which made a universe sweep "return an
identical number at every size".

It loads prices and spreads once (`select_universe` reloads the lake per
call, 4.5-5.4s, about 75 minutes an arm), applies the session flatten, keeps
the shipped `execution_lag`, and **refuses to report an arm until its
baseline reproduces a recorded figure** within the project's own 9.3pp floor.

**The results, cap 8 / target 50, against a recorded +191.0% at -6.6%:**

    production config, no screen gate        -84.2%   -99.9%   38,877 trades
    + screen membership as entry_gate        -77.4%   -99.6%   25,829
    + spreads at the 5bp constant            -49.3%   -91.8%   25,829

    85 distinct names across 192 rebalances

**Five explanations tested and eliminated**, each measured rather than
argued: the session flatten (0.6pp), `spread_panel` being None (not a defect
-- the engine falls through to the frame's own `spread_bp`), `entry_gate`
being None (the runner composes the breakout mask at run time), the missing
screen gate (7pp of return, a third of the turnover), and measured spreads
against the 5bp constant (**28pp** -- real, and worth knowing on its own).

**240pp still unexplained, and the drawdown says it is not a setting.** The
recorded arm draws down 6.6%; every reconstruction draws down 91.8% or
worse. That is not a parameter apart, it is a different simulation. This
file already says of that number: "the one figure here that should not be
believed... treat 6-9% as what the model can see, not as a risk estimate."

**So `max_per_sector = 8` and `universe.max_target = 50` are currently
unjustified.** They were adopted on 2026-09-08 on an arm nothing can now
regenerate, by a harness that was never kept. That is a statement about the
EVIDENCE, not about the settings: the account may well be running a fine
configuration, and nobody can presently show it. The shipped values are NOT
changed here -- changing the universe changes what the account trades, and
that is the owner's decision on strategy grounds rather than a side effect
of a failed reproduction.

The same applies to the 2026-09-07 size-curve retraction one entry earlier:
that table was withdrawn for being swept over the raw ranking, and the
2026-09-08 battery which replaced it is now unreproducible. Two successive
universe measurements have failed, by different routes.

**What the harness is for now.** Finding the remaining 240pp is a bounded
search -- the arms are cheap once membership is cached -- and until it is
found, no parameter claim may be read off it. That is the harness working
as designed rather than a reason to bypass it.

### Backtesting, read on 2026-09-12

**THERE IS CURRENTLY NO REPRODUCIBLE BACKTEST OF THE SHIPPED
CONFIGURATION.** That is the state, and it is worse than "a sweep failed".

**The harness that justified the shipped universe is not in this
repository.** The 2026-09-08 battery -- `max_per_sector = 8` and
`universe.max_target = 50` adopted on cap 8 / t50 returning 191.0% at 6.6%
-- was produced by a scratch script. There is no `research/`, no `scripts/`,
and nothing outside `cfdinvest/` and `tests/` mentions
`select_within_sector_cap` or `reachable_size`. So the figure that chose the
universe the account trades cannot be regenerated by anything here.

"One engine, and it is the production one" covers the ENGINE, and the engine
is shared and corrected. It does not cover the HARNESS, and the harness is
what produced every headline figure in this file.

**What a run built from the production config returns:**

    _backtest_config, shipped settings   -84.2% at -99.9% on 38,877 trades
    the same, plus the session flatten   -84.8% at -99.9% on 35,293 trades
    recorded 2026-09-08, cap 8 / t50    +191.0% at   6.6%

Verified as correct before reading anything into that: universe 50 names,
`execution_lag` 1, `entry_confidence` 0.75.

**Three explanations were tested and ELIMINATED**, which is the useful half:

* *The missing session flatten.* This configuration day trades and the
  flatten is not a config field -- `for_live_intent` implements it as
  `execution.overnight_profit_take = -10,000`. So a plain research run
  carries positions overnight. Applying it moves -84.2% to -84.8%. **Not the
  cause.**
* *`spread_panel = None` with `use_measured_spreads = True`.* This looked
  like the live path charging a flat 5bp instead of measured spreads. It is
  not: the engine falls through to the analysis frame's own `spread_bp`
  panel at run time. Measured spreads ARE charged. **Not a defect at all.**
* *`entry_gate = None`, so the breakout gate is inert.* It is not.
  `backtest/runner.py` builds the mask from `breakout_lookback` and composes
  it into `entry_gate` during the run; `None` on the config is the pre-run
  state. **Not an inert gate.**

**The cause of -84% is UNKNOWN and is not guessed at here.** Two candidates
remain untested: the battery re-ran the SCREEN daily, where this run pins
one 50-name list across 3.65 years, and `open_delay_minutes` is 0 in the
research config where production refuses the first half hour. Neither is
established, and writing down a plausible cause as though it were measured
is the failure this file exists to prevent.

**What follows, and it is uncomfortable.** Every parameter claim in this
file is a difference between two runs of a harness. Most of those figures
also predate one correction or another -- the intrabar look-ahead, the
screen's sector cap, the cost model -- and this file already says "do not
quote a CAGR from this file". The 2026-09-08 battery was the one measurement
taken on the corrected engine over books the screen actually produces, and
it is the one that cannot be re-run.

So the shipped universe rests on a number nothing currently produces, and
the regime question asked on 2026-09-12 stays unanswered for the same
reason.

**The fix is a harness IN the repository**, reconstructing what the battery
did -- the screen's daily selection, the flatten, the shipped lag -- which
asserts it reproduces a recorded figure before any arm may be read, and
which is committed so the next person can re-run it. That is the rule this
file already states for sweeps; what was missing is that nothing enforced
the harness being kept. Not built.

### The forward record, read on 2026-09-12

Asked whether performance has diverged, and by how much. Read from the
ACCOUNT'S OWN RECORD -- real fills at real spreads confirmed by a
counterparty -- because the research harness does not currently reproduce
the battery that justified this configuration, so there is no trustworthy
modelled side and any "modelled against actual" figure would be arithmetic
on a broken number.

Against this file's own watch-list, in its order.

**1. Positions held.** Flat, which is correct outside a session for a book
that flattens at the bell. Nothing to read on a Saturday.

**2. The last look's refusal rate has diverged, and the cause is already
written down.**

    first week, 2026-09-03    6 blocked outright of 29 considered   20.7%
    seven days to 2026-09-12 16 blocked outright of 34 considered   47.1%

Against 18.7% modelled. This file says a sustained rate far above 20.7% is
the gate misfiring rather than protecting -- and the reasons say it is not:
`no longer clears the bar` 28, `no longer a signal` 4, and **zero `not
re-checked`**. A broken check fails closed and shows up as that third
reason dominating. The gate is forming opinions and refusing on them.

The recorded cause is the stale-intent loop of 2026-09-10: the engine goes
on intending positions it entered on an earlier bar that the account never
got, so every later pass attempts an open the last look correctly refuses.
Safe, noisy, and still not fixed.

**RETRACTED 2026-09-19: it is NOT what this number is measuring.** The rate
above is instrument-days, and the loop creates none -- it only repeats
within a day. Measured: 70.5% of refusal events are chases and 0 of 30
refused instrument-days were refused only while stale. The elevated rate is
unexplained again. See the 2026-09-19 entry.

**3. The win/loss ratio is correcting, which is the one the file says must
happen before any return figure means anything.**

                        week one (09-04)    now (09-12)
    trips                         23             41
    win rate                    30.0%          43.9%
    mean win                   +16.27         +26.38
    mean loss                  -37.88         -29.76
    loss/win size ratio          2.33x          1.13x

All four moved in the predicted direction. The 2.3x inversion was the live
signature of the time stop's look-ahead -- it made winners big by exiting
losers before they fell -- so removing it should invert the profile, and
halving the ratio in a fortnight is that happening. It has NOT inverted:
below 1.0 is what the design predicts and 1.13x is not there yet.

**4. Returns say nothing, and 41 trips does not change that.**

    opening 2026-08-27    5,000.00
    latest  2026-09-11    4,753.96    -246.04   -4.92%
    against the 09-08 configuration mark of 4,780.79   -26.83   -0.56%

Three days on the shipped arm and about a fortnight overall, against the 47
weeks this file measures as the point where a forward result separates from
noise. Quoting either figure as performance would be quoting noise.

It is also **consistent with the corrected engine rather than divergent from
it**: the 2026-09-04 finding is that the shipped configuration is believed
net-negative, and the account was kept running precisely because it is the
only instrument that can test that. A loss is the predicted outcome. What
would be evidence against the corrected engine is the account MAKING money,
and it is not.

**The realised total understates the loss, by design.**

    realised across 41 trips     -209.62
    balance change               -246.04
    unexplained                   -36.42

`record_departure` refuses to invent an exit price, so a close placed
outside the app yields no P&L rather than a wrong one -- TGT's -49.47 on
2026-09-10 is excluded for exactly that reason. So -209.62 is a FLOOR on
realised losses and not the total, and the gap is the cost of the
out-of-band closes rather than a bookkeeping error.

**Margin, answered for the first time with a sample.** 89 observations since
2026-09-03: peak utilisation 54.4%, latest 0.0, and **factor_changes 0**.
The question of whether Capital moves a margin factor under an open position
now has a measured answer over this window: it has not been observed to.
That is a statement about nine days, not about the venue.

### What changed on 2026-09-11

The day the architecture changed, and the day it turned out the alarm had
never been switched on.

**THE ACCOUNT DID NOT DEAL FOR NINETEEN HOURS AND NOTHING SAID SO.** Last
dealing pass 19:43 the previous evening; the market opened at 13:30 and
nothing happened. The daemon was `active` the whole time, and busy.

`run-intraday` began at 13:35 and was still inside ONE call at 14:24, in
uninterruptible sleep, thrashing against its memory cgroup. A stack dump
named it exactly:

    _to_long              persist/writers.py:256
    write_analysis_frame  persist/writers.py:277
    _persist_extras       schedule/runner.py:399

`write_analysis_frame` stacks the rolling CORRELATION panel, which is every
asset against every other and therefore QUADRATIC in the universe. Measured
from the lake rather than guessed:

    instruments                109
    session bars             6,464
    cells        6,464 x 11,881 = 76,798,784
    as float64                614 MB
    stacked to long, string labels alone         3.8 GB

It is a sweep-time cache, written on the fifteen-minute trading cadence.
`trade-capital` ran after it in the same tick, so it was never reached.

**I raised the memory limit before taking the dump, and that was wrong.**
3.55GB at 14:19 became 5.59GB four minutes later and carried on thrashing.
A limit is not a diagnosis. The dump took thirty seconds and named the
function.

**And it reframes 2026-09-09.** The unit file records the pipeline "peaking
at 1801MB against MemoryHigh=1800M". That was not a peak, it was the
ceiling. The run had been memory-starved for days and each raise simply let
it grow into the new room.

`ANALYSIS_PANEL_GROUPS` now excludes correlations. Nothing is lost but a
convenience -- they are derived from `returns`, which IS written.
Afterwards: **941MB peak, zero throttle events, the run in 26 seconds.**

**THE ALARM EXISTED, RAN OUT OF PROCESS, AND WAS NEVER SWITCHED ON.**
`check_dealing_liveness` was written after the 2026-09-09 stall and its
docstring describes this one exactly -- "the process stayed alive, the unit
stayed active, Restart=always never fired... and nothing anywhere said so".

It runs inside `monitoring-snapshot`, which the daemon schedules at
**03:05 UTC**. `market_open` is False every time it runs, so the condition
it detects is unobservable at the only moment it looks. Worse, it runs
inside the daemon's own sequential loop -- a monitor inside the thing it
monitors stops when that thing stops.

The out-of-process version already existed. `cfdinvest-monitor.service`
runs `monitor --alert` every 30 minutes on its own timer. On the box that
timer was `disabled`, `inactive`, and had last run on **2026-08-26**.

It could not be enabled: `cfdinvest.service` declared
`Conflicts=cfdinvest-pipeline.service cfdinvest-monitor.service`, so
starting it would have stopped the dealer every half hour. That line is
over-broad -- the stated reason, ingestion running twice against one lake,
is true of the PIPELINE service and false of a monitor that reads, alerts
and finishes in about a second. **One word in a Conflicts line disabled the
alarm for both stalls.**

Fixed, enabled, and proven not to stop the dealer by triggering it by hand.

**THE ARCHITECTURE CHANGED, ON THE OWNER'S DIAGNOSIS.** Their framing:
opening, monitoring and closing trades should be very light; the analysis
should look at a bounded window; nothing should need much CPU or RAM.

The defect was concrete. The daemon runs its jobs SEQUENTIALLY, so the
lightest and most time-critical work queued behind the heaviest -- and when
the pipeline wedged, the account could not be CLOSED at all.

    cfdinvest.service         every 1 min  ingest, pipeline, spreads, daily
    cfdinvest-dealer.service  every 5 min  DEALING ONLY
    cfdinvest-monitor.service every 30 min health, out of process, alerting

All three are `Type=oneshot`. Nothing is resident; each starts, does what is
owed and exits. The one-minute figure is not a job cadence -- every job owns
its own, and this only decides how often "what is owed?" is ASKED.

`trade-capital` is a command of its own with its own market gate, removed
from the daemon's JOBS and due-logic so exactly one thing deals. It
computes no rule -- it reads the units `persist.intent` owns -- which is
what keeps it inside "one engine, and it is the production one".

**Separation creates one new risk and it is guarded.** A stale run may not
OPEN and must never stop a CLOSE: `decision_is_stale` refuses opens on a
decision bar older than two bar spacings. Two rather than one because the
newest COMPLETED bar is already up to one spacing old when written.

**Proven rather than argued.** With the daemon frozen by SIGSTOP -- state
`Tsl`, unit still `active`, the exact signature of the morning's stall --
the dealer read the run, reconciled against the broker, ran the freshness
checks and the last look, and completed a full pass in about a second.

**AND THEN THE ANALYSIS STOPPED BEING RESIDENT TOO, which is the half I had
wrongly treated as blocked.** I had the periodic analysis waiting on the
six-month window measurement. They are independent and conflating them was
my error: the window question is how much CPU and RAM ONE RUN needs, and the
leak is about process LIFETIME. A pass that exits every minute cannot
accumulate 5.7GB over sixteen hours however many bars it reads. So the
window failing does not block this, and nothing was waiting on it.

It needed **no code change at all**. `daemon.jobs_due` is already pure and
already reads what ran last from `persist.scheduler_state` on disk, so a
tick that starts cold computes exactly what the loop would have; and
`--max-ticks` already existed, built for tests and dry runs. The change is
`Type=oneshot`, `ExecStart=... daemon --max-ticks 1`, and a timer that fires
one minute after each pass FINISHES rather than after each pass starts, so a
slow pass delays the next instead of queueing behind itself.

    measured on the box, a tick with nothing due   0.76s, 127MB peak
    a full intraday run, after the panel fix       26s, 941MB peak
    MemoryMax                                      2G, and NO MemoryHigh

`--max-ticks` being documented as "for tests and dry runs" is now a hazard
rather than a note: the production unit depends on it, and a tidy-up that
removed it would restore the resident loop silently. So the guard is against
the PARSER rather than the help text, and
`tests/test_analysis_is_periodic.py` fails the build on any of seven
mutations -- `Type=simple` restored, `--max-ticks` dropped from either the
unit or the parser, `Restart=always` on a oneshot, the timer pointed
elsewhere, `MemoryHigh` reintroduced, or the interval measured from the
start of a pass instead of its end. All seven were checked by breaking them.

**`Type=oneshot` and `--max-ticks` are two halves of one guarantee.** Either
alone is inert: systemd waits on the process, so a oneshot running a loop
that never returns simply activates forever and looks healthy -- the exact
signature of both stalls.

**THE DAEMON LEAKS ~360 MB/HOUR, AND THE PIPELINE IS NOT WHAT LEAKS.**
Measured from a clean start, every ten-minute sample higher than the last:
900MB at 17:23 to 1796MB at 19:53, linear. A ratchet plateaus; this does
not.

That closes the morning's diagnosis. The daemon had been up sixteen hours,
so ~5.7GB had accumulated against a 3GB ceiling -- the correlation panel
was simply the allocation that could not fit.

Running the pipeline three times in one process retains +511MB, +23MB,
+6MB: **converging to nothing.** So the leak is in the recurring jobs
around it, not in `run_pipeline`. Not yet identified. A `tracemalloc` run
did not finish -- it made a 26-second pipeline take over 900 seconds.

**Also fixed today.** The fallback provisional bar was motionless --
`open = high = low = close = quote` -- and fired whenever ingestion was
more than one bar behind, which on an hourly grid is the first ~20 minutes
of every hour. Measured at 16:04 with the lake at 15:00: ABT 0.789 -> 0.457,
LMT 0.774 -> 0.415, REGN 0.777 -> 0.438. Every intended name refused,
uniformly ~0.34 low. It now opens at the last completed close so the range
is the real move since. Verified live in the append window at 17:06:
+0.016, -0.006, +0.021.

**And the live corrections became properties of the RUN rather than of one
command.** `_command_run_continuous` called `run_pipeline` directly and
applied none of them, so a run it persisted would have reached the dealer
at `execution_lag = 1` over an untrimmed panel -- the TGT fault and the
TMUS fault at once, from a path nobody would check. Latent, never live,
found while checking a definition. `for_live_intent` now owns them and a
test parses the module and fails the build on any caller that persists a
run without them.

**Trading:** -22.01 GBP realised over five closes, two of them gains. The
three losses were all "flattened before the session close" -- positions the
clock closed, not positions the strategy gave up on. The account ended
FLAT, record and account agreeing on size and side, which is the first time
this week the close worked as designed.

**THE DOCUMENTATION PASS FOUND CODE, NOT JUST PROSE.** Asked to make the
docs lean and correct. Two of the findings are not documentation at all.

**`schedule/paper_alert.py` was still in the tree** -- 115 statements, the
email describing what the paper book just did, for a book deleted on
2026-08-30. **Zero percent covered and no importers anywhere**, which is how
it survived twelve days. It was found by measuring coverage to check a
figure in the README, not by looking for it. Deleted.

**And a test fixture referenced two things that do not exist.**
`tests/test_ui.py`'s `dashboard_with_a_position` imports
`cfdinvest.advisor.papertrade` and patches `sources.load_paper_book`; the
module was deleted and the function does not exist. It never failed because
**no test requests the fixture**, so pytest never executes it. A fixture
nothing uses is not coverage, which is precisely what its own docstring was
written to warn about. Deleted.

**Six figures were wrong, and they were wrong in the direction that
flatters.** Every one re-measured rather than adjusted:

    tests            "~2,500" / "~2,830"        3,528
    coverage         "88%"                      85%
    schedule/app.py  "73% covered"              54%  -- the worst in the
                                                project, and the file that
                                                carries dealing, the live
                                                corrections and the
                                                staleness guard
    a persisted run  "about 31MB"               54MB
      of which       "98%, mostly correlation   85%, and correlations were
                      rows"                     removed today
    light artefacts  "under half a megabyte"    8.0MB, sixteen times that
    monitoring       "nine checks"              twelve

The run-size figures were measured at a 10-name universe and never revisited
when it became 50. The consequence is operational rather than cosmetic: the
light artefacts are what retention deliberately KEEPS, and at 21 runs a
session that is about 170MB a day, so "roughly 11GB a year becomes roughly
280MB" no longer holds. No replacement figure is stated, because the right
one depends on a retention window nobody has re-chosen against these sizes.

**And the headline of the two most-read files described a component deleted
twelve days earlier.** `CLAUDE.md` and `README.md` both opened "a multi-asset
CFD backtesting engine, a **forward-testing shadow book**, and a
manual-trading assistant". `README.md` then contradicted itself: line 3
advertised the book, line 209 correctly said it was removed on 2026-08-30.

**Left open.**

* **THE LEAK IS NOT A LEAK, AND THERE IS NOTHING IN THE CODE TO FIX.**
  Chased since 2026-09-09 and settled on 2026-09-12 by measurement. The
  answer is a negative result and it retires the item.

  The split made this tractable: each job can now be run ALONE in one
  process, which is what the earlier hunt could not do -- it got as far as
  "somewhere in the recurring jobs" and stopped.

  Per call, after two warm-up calls, RSS read from /proc with a collection
  between each:

      record-spreads     +0.25 MB, then +0.0, +0.8, +0.2, +0.0   PLATEAUS
      ingest-intraday    +362 MB on call one, then -1.5, +27, -20, +6, +0.8
      poll-live-quotes   +3.3 mean, oscillating
      advisor-alert     +10.5 MB, MONOTONIC: 311.7 327.5 340.9 363.9
      run-intraday       already exonerated: +511, +23, +6, converging

  `record-spreads` is the control and it is what makes the rest readable: a
  job that merely caches plateaus at exactly +0.0. The method can tell a
  cache from a leak.

  **`advisor-alert` looked like the leak and is not.** It runs every five
  minutes, so +10.5 MB a call implies ~126 MB/hour, which would have been
  most of the story. Taken apart:

      live Python objects        75,842 -> 75,857 -> 75,858    FLAT
      bytes held by live ndarrays and DataFrames         0.0 MB
      log handlers               3, 3, 3                       FLAT
      uncollectable (gc.garbage) 0
      types growing monotonically   none, past the second call

  Fifteen objects and nine megabytes is not an object leak, and zero held
  array bytes means nothing numpy or pandas can see is retained. So the
  memory is freed at the Python level and not returned to the OS -- and it
  is held in TWO allocators, which is why one tool never found it:

      glibc malloc_trim(0)              recovers ~35 MB every call
      pyarrow pool.release_unused()     recovers ~40-52 MB every call

  **pyarrow's backend is mimalloc, not glibc**, so `malloc_trim` could never
  have touched it. Parquet reads go through that pool on every call --
  `advisor-alert` loads the signals frame each time -- and its
  `total_allocated_bytes()` is 0.0 MB throughout, so it references nothing
  either; it is simply holding pages.

  A residual of about 13 MB a call survives BOTH reclaims, with the object
  count flat. Nothing in Python holds it. That is fragmentation and arena
  growth, not a forgotten reference.

  **So "fix the leak" has no code change behind it.** A process that exits
  returns everything, and every process became `Type=oneshot` on 2026-09-11
  and 2026-09-12. The architecture change was not a mitigation for an
  unfound bug -- it was the correct fix, and this is why.

  Stated before the numbers were seen, so it could not be read to taste:
  "if RSS falls back after a trim and stays flat, the growth was never a
  reference leak". The first half held and the second did not, which is why
  the hunt continued to pyarrow rather than stopping at glibc.

  **What this says about the 2026-09-11 reading.** "900MB at 17:23 to 1796MB
  at 19:53, linear -- a ratchet plateaus, this does not" was a true
  observation and the wrong inference. In ONE process running twelve jobs
  with very different allocation patterns, arenas fragment against each
  other and the high-water mark climbs for hours before flattening. It looks
  linear precisely because it is many caches and two allocators ratcheting
  at once, not one thing growing without bound.

  **If a long-running process is ever needed again**, the levers are
  `MALLOC_ARENA_MAX`, a periodic `malloc_trim(0)`, and
  `pyarrow.default_memory_pool().release_unused()`. None is shipped, because
  nothing is resident and unmeasured configuration is how thresholds drift.

* **THE SIX-MONTH WINDOW IS BLOCKED BY ONE THING: A YEAR-OVER-YEAR MACRO
  CALCULATION MEASURED IN PRICE BARS.** Diagnosed on the owner's push-back,
  and their argument is what got it right.

  The measurement, on five recent mid-session bars, full span against six
  months, comparing the final bar's target as a SHARE OF EQUITY:

      end bar    full   six months   verdict
      09-04         5            2   DIFFERENT NAMES: BKNG, LMT, NOC
      09-08         9            9   same names, sizes differ (to 2.4x)
      09-09         9            9   same names, one size differs 2.4x
      09-10         6            6   IDENTICAL
      09-11         4            4   IDENTICAL

  **I read that as "six months is not safe" and that was the wrong
  conclusion.** It takes the full-span run as ground truth, which is exactly
  the premise worth doubting: this book flattens at the bell, its time stop
  is 0.29 days, and every session starts flat, so there is no carried state
  and no mechanism by which 2023 informs the next four hours. The owner's
  objection -- there is not a single stock where 3.5-year-old data helps
  predict tomorrow's direction -- is the right frame, and the disagreement is
  evidence about a DEPENDENCY rather than about a window.

  Bisected by stage on the 09-04 bar:

      analysis panels   close     IDENTICAL across 55 columns
                        macro     IDENTICAL across 12 columns
      regimes           growth_regime     full 'contraction'  short 'unknown'
                        inflation_regime  full 'stable'       short 'unknown'
      signals           BKNG/LMT/NOC      full 'short'        short 'flat'

  Prices identical. Macro inputs identical at the decision bar -- INDPRO
  year-on-year 0.003375 and CPIAUCSL 0.000737 in BOTH runs, to six decimals.
  Only the LABEL differs, and an unknown macro regime turns 20 of 55 names
  flat.

  **The cause, measured rather than reasoned:**

      periods_per_year          1764   (7 hourly bars x 252 sessions)
      bars in six months         891
      needed: year + momentum   1827   about 12.4 months

  `_macro_lookback` resolves to the frame's own `periods_per_year`, so the
  growth and inflation classifiers do a genuine YEAR-over-year change -- and
  they do it in PRICE BARS on the PRICE calendar. A six-month price window
  cannot resolve it at any warm-up, because the macro panel is reindexed to
  that calendar and the 1,764-bar shift falls off the start however much
  macro history is loaded. Proved by loading macro with `start=None`: the
  leading NaNs went 348 to 0 and 327 to 0, the year-on-year resolved on 639
  of 891 rows instead of 291, and **not one decision changed.** That change
  was reverted rather than kept, because its stated reason was disproved.

  **And this is what the label DOES**, which is the part that matters:

      allow_short = combined.isin(short_regimes) | macro.isin({"contraction"})

  A short is permitted only in a `crisis` combined regime or when the macro
  label reads `contraction`, and `allow_when_unknown` is False. So whether
  this book may go short on an ordinary day is decided by a year-over-year
  change in monthly FRED data published in arrears -- over a live intraday
  signal that had BKNG, LMT and NOC as shorts. That is the owner's second
  point exactly: backwards-looking data outranking the near-live data
  collected through the day.

  **RETRACTED, same session: I claimed `periods=252` meant the year-on-year
  really spanned about seven weeks.** It does not. `classify_growth` defaults
  to 252 but the pipeline passes 1,764, so it is a true year. The seven-week
  figure came from calling the function by hand at its default, and the
  disagreement I found between `regimes.components` and
  `classify_macro(analysis.macro)` -- 'contraction' against 'expansion' -- is
  that same default, not two owners for one quantity. Recorded because it was
  one step from being written down as a bug in a file whose whole discipline
  is not doing that.

  **RESOLVED 2026-09-12, and the fix took three attempts.**

  `RegimeConfig.macro_on_own_calendar` classifies on a MONTHLY grid indexed
  by PUBLICATION date, where a year is twelve rows. Measured on the same five
  bars:

      full span    5, 9, 9, 6, 4 held   macro contraction throughout
      six months   5, 9, 9, 6, 4 held   macro contraction throughout
      identical on a non-empty book: 5 of 5, against 2 of 5 before

  The full-span column is UNCHANGED from the shipped behaviour, and on 09-04
  the six-month run GAINED BKNG, LMT and NOC rather than the full run losing
  them -- it reaches the same answer by seeing more, which is the difference
  between a fix and a wash.

  **The second attempt scored 5 of 5 and was wrong**, which is the part worth
  keeping. It reindexed monthly labels onto the hourly calendar, matching
  almost no rows, so every bar fell through to `unknown` -- on BOTH runs,
  including the full span where the regime had resolved. Agreement because
  neither side can see anything is the worst kind of green, and it would have
  shipped as a fix. The acceptance test is not "do they agree"; it is "does
  the full-span answer survive AND does the short run resolve".

  The first attempt failed differently: "the series' own calendar" is the
  UNION of every series' dates, and DFF, DGS10 and VIXCLS are daily, so the
  index came out daily with 13,403 rows and a twelve-row shift moved back
  twelve DAYS onto a NaN.

  Publication dating is not cosmetic: indexing by observation date would
  back-date the August figure to before it was knowable, a lookahead in the
  one place nobody checks, since the bar panel gets this right through
  `publication_aware_ffill` and this panel bypasses it.

  **The flag defaults False.** On these five dates the live decision does not
  move -- the full-span column is identical either way -- but across 3.65
  years the labels differ materially (unknown 1,827 bars to 0, stagflation 84
  to 586, overheating 0 to 294), so every recorded research figure would
  move. Turning it on, and then shortening the window it unblocks, are the
  owner's decisions and are recorded as ready rather than taken.

  *Strategy, and it is the owner's.* Whether a monthly, lagged,
  year-over-year label should gate a book whose median position lives four
  hours. This file already records the answer from 2026-08-30 -- "every
  intervention slower than the hold failed... a label computed over a
  multi-day window cannot inform a decision about the next four hours" -- and
  a year-over-year macro series is the slowest label in the system. That is
  an argument and a prior measurement, not a measurement of THIS gate, and
  the gate has never been measured on its own. It needs the battery before it
  is removed, because removing it changes which trades the account may take.

* **A PLAIN RESEARCH RUN NO LONGER REPRODUCES THE BATTERY THAT JUSTIFIED THE
  SHIPPED CONFIGURATION, AND THAT BLOCKS EVERY SWEEP.** Found on 2026-09-11
  while trying to answer whether the regime layer earns its place.

  `_backtest_config` at the shipped settings -- universe 50, `execution_lag`
  1, `entry_confidence` 0.75, verified by printing them -- returns:

      as built                        CAGR  -84.2%   maxDD -99.9%   trades 38,877
      plus the session-end flatten    CAGR  -84.8%   maxDD -99.9%   trades 35,293
      recorded 2026-09-08, cap8/t50   CAGR +191.0%   maxDD  -6.6%   trades ~3,219

  Eleven times the trades and the wrong sign. The trade count being orders of
  magnitude out is exactly how the 2026-08-31 harness error was caught, and
  this file's own rule is that a sweep must reproduce a recorded figure
  before any arm is read. **So no arm was read, and the regime question is
  UNANSWERED rather than answered either way.**

  Two candidates and neither is established. Either the battery's harness did
  something `_backtest_config` does not -- the daily screen re-selection is
  the obvious suspect, since "the screen's daily selection IS the strategy"
  and this run pins one 50-name list across 3.65 years -- or something has
  regressed since 2026-09-08. The first would mean the production config path
  and the research path disagree, which is the shadow-book failure wearing
  new clothes; the second would mean the shipped configuration is justified
  by a figure nothing currently produces.

  **Until it is reconciled, no measured claim about a parameter can be made**,
  because every one of them is a difference between two runs of this harness.

  Two things found on the way that do NOT depend on the broken P&L, because
  they are read off the regime labels rather than the equity curve:

      macro label, over 6,467 bars of the full span
        contraction   2,724   42.1%   <- the ONLY value that permits a short
        unknown       1,827   28.3%
        expansion     1,392   21.5%
        slowdown        440    6.8%
        stagflation      84    1.3%
        overheating       0    0.0%

  The 1,827 unknown bars are exactly `periods_per_year` 1,764 plus the
  63-bar momentum -- the warm-up, to the bar, which independently confirms
  the diagnosis above. So for the first 28% of any run the book cannot short
  at all unless the combined regime reads `crisis`, and that is a property of
  the window rather than of the market.

* **RESOLVED 2026-09-12, and it was worse than "two owners".** The box was
  running `MemoryHigh=5G` and `MemoryMax=6G` on `cfdinvest` while the
  shipped unit set `MemoryMax=2G` and no `MemoryHigh` at all -- left over
  from raising the limit by hand during the 2026-09-11 stall.

  `systemctl set-property` writes to `/etc/systemd/system.control/`, which
  OUTRANKS the unit file and survives deploys, reboots and `daemon-reload`.
  So the repository, the tests and the deploy all described one
  configuration and the machine ran another.

  **And MemoryHigh is the specific directive that caused the 2026-09-09
  stall** -- it throttles instead of failing, so the process never dies,
  `Restart` never fires, the unit stays `active`, and the account silently
  stops dealing. `tests/test_analysis_is_periodic.py` asserts the FILE does
  not set it, and that test passed for a day while production did.
  **Only the box can check the box.**

  Cleared with `systemctl revert`, and `deploy.sh` now fails when any
  `MemoryHigh` is in force or any `system.control` override exists for a
  cfdinvest unit, so it cannot come back quietly.
* `MIN_SESSION_MINUTES = 30.0` remains provisional and unmeasured.

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

**THE PLUMBING, FIXED — five changes, one fault.** Done after the close on
the owner's instruction, with the market shut so a restart cost nothing.

The fault, once: **the record and the account view both modelled ONE
POSITION PER INSTRUMENT, and Capital models one per order.** Everything
below follows from that, and fixing four of five would have left the
account wrong in the fifth way.

    _aggregate_positions   the broker's legs summed per instrument, every
                           deal id carried. Sizes summed SIGNED so an
                           opposing pair nets rather than doubles.
    open_orders            rows replayed per DEAL ID and summed per epic,
                           so a close removes the leg it names. A close
                           naming no known leg still clears the epic --
                           after a resize the recorded id is the ORDER's,
                           and believing a leg is open when it is not is
                           the direction that halts dealing.
    _other_legs            a close shuts every leg on the instrument.
    actionable             **a halt stops OPENING and lets closing through.**
    record_arrival         the mirror of `record_departure`.
    _resulting_position    an open records the side the BROKER filled.

**The halt change has the most reach and is the one to argue with first.**
It permits an order on a book the app has just admitted it cannot
reconcile. The argument for it is that this module already makes it about
margin, in these words: closes are never refused, because a book short of
margin is exactly the one that most needs to reduce. A close commits no
capital, needs no sizing, cannot breach a limit, and acts on a deal id the
BROKER supplied rather than on anything the record believes. Every argument
for halting is an argument about taking on exposure.
`test_a_halted_report_places_nothing_whatever_the_order` asserted the old
contract and is rewritten rather than deleted, carrying what it cost.

**The side bug was found by running the new reader against the LIVE record,
not by a test.** Sizes agreed at 17.31 units and the side did not: the
record said SPGI BUY where the account held SELL, because
`_resulting_position` derived the side from the sign of the intent's target
and the console script that wrote those rows passed an unsigned magnitude.
`reconcile` compares epics, so the record disagreed with the account about
the side of a live position and reported clean. That is the fifth time this
week the real thing on real data found what the suite could not, and it is
why the rule exists.

**Verified on the box afterwards**, which is the whole point:

    record   SPGI 17.31 SELL, 2 legs
    account  SPGI 17.31 SELL, 2 legs
    reconcile -> unexplained: none, missing: none

**A sixth change, on the owner's objection, and it caught a bug in itself.**
Asked whether the system should evaluate a carried position before closing
it -- what if SPGI is a candidate again tomorrow? Closing a trade only to
immediately open a new one does not make sense.

It does not, and the completed-bar rule shipped hours earlier is what would
have caused it. `intended_positions` reads `positions.iloc[-1]`, and on a
session's last bar that book is FLAT BY CONSTRUCTION because the flatten is
modelled. Before this session's first bar completes, the newest COMPLETED
bar IS the previous session's last, so the intent reads "hold nothing" when
it means "yesterday ended flat". SPGI, carried overnight at 0.901 on its
last completed bar, would have been closed about 13:35 and bought back
about 14:05 -- the spread twice, to arrive where it started.

`decision_is_a_previous_session_close` declines to act on it, and waits at
most one bar. It decides nothing about the position: once this session's
first bar completes, `plan_orders` does what it always did -- target
against held, `none` when they agree, a close when they do not. That
machinery was being denied the chance to run.

**The first version of the rule asked the wrong question and its own test
caught it.** It tested whether the bar was the last of its session, which
cannot be told apart from "the last bar we happen to have, because
ingestion stopped at 15:00" -- so a stale lake would have silently stopped
the account ever closing anything. The rule is now about the harm: an
earlier day AND a flat intent, so acting would EMPTY the book on another
day's view. A stale bar still holding positions keeps the old behaviour.

The test fixture was wrong too, and in the same family: it held a position
on every bar, which is not a book this engine can produce. Testing against
a shape the code cannot see proves nothing -- the same mistake that let the
completed-bar trim ship inert twice the same afternoon.

**What is NOT fixed, and should be read as the honest state.**

* **Nothing tells the owner when the app is wrong.** Every fault this week
  was found because the owner noticed something or because I went digging.
  A daily verification -- what the page showed, what the engine intended,
  what the account did, what was refused and why, the position at the close
  -- would have surfaced every one of them on day one in a form nobody has
  to ask me about. That is worth more than any single fix here and it is
  not built.
* **The stale-intent loop is now COUNTABLE, and it is smaller than it
  looked.** Measured 2026-09-12, before changing anything.

  Raw refusal events by day, from the order store:

      2026-09-08   60        2026-09-10   62
      2026-09-09   77        2026-09-11   15

  A fourfold drop on 09-11 -- the first full day after the last look could
  reach its own 0.75 pass mark, the multi-leg fix, and the dealer moving
  into its own process. `not re-checked` appears on no day, so the gate is
  forming opinions rather than failing. Eight of 24 refused names do recur
  on a later day, which is the loop's signature.

  **A per-day RATE is not quoted, because the reading was wrong.** The first
  attempt showed 100% on two days; its opens-detection failed, so the
  denominator was refusals alone. BKNG demonstrably opened on 2026-09-08.
  The counts above are sound and the rates were discarded.

  `intended_positions` now carries **`entered_bars_ago`** per row: completed
  bars since the ENGINE entered that position, zero meaning it opened on the
  newest completed bar. That separates the two things a refusal can mean --
  the engine chasing an entry the account never got, against a signal that
  decayed and a gate doing its job -- which reading order rows cannot.

  **MEASURED 2026-09-19: the loop is 70.5% of refusal EVENTS and 0% of
  refused INSTRUMENT-DAYS, so it explains none of the rate below.** Every
  chase echoes a fresh refusal earlier the same day, 20 of 20. Enforcement
  would remove 557 repeated refusals and zero trades. See the 2026-09-19
  entry; the paragraph below is what was believed before it.

  **Reported, NOT enforced, and deliberately.** Refusing to open on a
  non-zero age is the obvious fix and it suppresses entries in a book where
  20% of trips produce all the profit. Nothing yet measures how many
  refusals are the loop, the evidence already says most of the problem went
  with the 09-10 and 09-11 fixes, and a Saturday with the market shut is no
  place to change what the account trades on a hypothesis.
  `tests/test_stale_intent_is_visible.py` fails the build if it starts being
  enforced without somebody saying so; five mutations checked.

  One of those mutations failed first time and is worth keeping: the test
  asserted `"entered_bars_ago"` appeared in the source, which is a SUBSTRING
  of `"entered_bars_ago_renamed"`, so it passed against the field being
  renamed away. Anchored with the colon. Same trap as the unit-file
  assertion that once passed against a commented-out line.
* `MIN_SESSION_MINUTES = 30.0` is still provisional and unmeasured.
* **The account carried SPGI overnight**, 17.31 units short. The flatten at
  19:01 ran four minutes before the multi-leg fix restarted the service,
  and the reconciliation halt then blocked every later close. Tomorrow's
  first pass should shut it: before 14:00 UTC the newest completed bar is
  the previous session's last, where the book is flattened, so the intent
  is "hold nothing" and opens are impossible.

**AND THE HALT BLOCKED THE CLOSE THAT WOULD HAVE FIXED IT.** The third
consequence of the same root cause, an hour after the first two, and the
one that says something about the design rather than about a bug.

At 19:22 the bridge halted on `unexplained: SPGI` -- a position in the
account and not in the record. It was right that they disagreed. The reason
is the same collapse: the flatten at 19:01 wrote ONE close row, `open_
orders` is keyed by epic, and that row removed the instrument from the
record while two legs were still open. Record flat, account not.

**`plan_orders` halts before it plans**, so no intent of any kind is
produced -- including the close. The `_other_legs` fix deployed at 19:05
would have flattened both legs and never got the chance to run. A guard
against acting on a misunderstood account prevented the one action that
reduces exposure, thirty minutes before the bell, in the configuration
whose whole purpose is to be flat overnight.

**This project already knows the answer and applies it elsewhere.** The
margin guard refuses opens and never refuses closes, for a reason written
into `plan_orders`: "a book short of margin is exactly the one that most
needs to reduce". A book the app does not understand is exactly the one
that most needs to be reducible. The reconciliation halt should stop
OPENING and let closing through -- the halt's own message already says
"Existing positions were left alone. Nothing was closed", which reads as
reassurance and is the defect.

Not changed tonight. It is a change to the one guard standing between a
misread account and a series of wrong orders, and it goes in with tests and
a measured look at what a halt should permit, not at 19:50 with the market
closing.

**How it was resolved, on the owner's decision.** The record was corrected
to match the account rather than the position closed by hand: every value
read from the broker -- size, direction, level, deal id -- one row per leg,
so nothing was averaged and no price that never traded was written. That
cleared the halt and let the app do its own closing, which is what the
owner has asked for throughout.

`record_departure` exists for the other direction and this is its mirror.
That the mirror had to be improvised at the console is itself the finding:
the record can be behind the account in both directions, and only one of
them had a way back.

**THE ROOT CAUSE, STATED ONCE.** The record models ONE POSITION PER
INSTRUMENT. Capital models one position per order, merging only opposing
trades. Every guard built on the record inherits that mismatch, and today
it produced three separate failures in six hours: a concentration cap that
measured a third of the book, a flatten that closed a third of a position,
and a halt that blocked its own remedy. Fixing the sizing dict alone would
close one of the three.

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
