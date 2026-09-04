# cfdinvest

A multi-asset CFD backtesting engine, a forward-testing shadow book, and a
manual-trading assistant. It models strategies, measures risk, deals on a
demo account, and tells you what it did.

**It deals, on one demo account, and nowhere else.** From 2026-08-27 the
daemon may open and close positions on a Capital.com *demo* account. A live
account needs a second environment variable, in different words, so the
mistake has to be made twice. Everything else is read-only by construction.
See [Execution](#execution-what-can-and-cannot-happen).

---

## What it does, end to end

Every fifteen minutes while a US market is open, a resident daemon:

1. **tops up the lake** with fresh hourly bars from yfinance
2. **rebuilds the panels** — prices, returns, volatility, correlations
3. **classifies the regime** — volatility, trend, correlation, macro
4. **produces signals** — direction, size and a confidence per instrument
5. **simulates fills** with real spread, financing, FX and slippage
6. **deals on the demo account** — mirroring the engine's own target positions
7. **emails you** anything it opened or closed

Once a day, at 03:00 and 03:05 UTC, it refreshes the full daily history
and the macro series, then takes health and risk snapshots and prunes old
artefacts. There is no daily *pipeline* run: signals come from the intraday
job above, which is the only thing that produces one.

The risk snapshot describes a real book — the trades confirmed on the
account if any exist, otherwise what the engine intends to hold, and
otherwise nothing at all, reported as nothing. Intent and confirmation are
distinguished: a breach on positions nobody has confirmed is reported as
advisory rather than raised.

You read the dashboard, take the emails, and place a selection by hand.

---

## The state of the evidence

This is a research system, and the honest summary matters more than the
feature list.

**There is a real directional signal.** Take the identical trades with
identical costs and choose the direction at random two hundred times: random
returns about 0% while the real signal returns 851% (z = +8.7, none of the
two hundred draws beat it). Shorts make money in a market that rose ~25% a
year, which is the hardest thing to fake.

**The margin is thin.** The edge breaks even at roughly **6 basis points**
of execution friction. The backtest charges 2bp. Nobody has measured the
real figure, because no order has ever been placed.

**The backtest cannot rank close configurations.** Removing one instrument
from twenty swings CAGR by a standard deviation of **18 percentage points**.
Any figure between roughly 90% and 155% is consistent with the same data, so
differences smaller than that are noise — and several apparent findings this
size have already dissolved under proper testing.

**The universe was assembled with hindsight.** Buy-and-hold of the same
twenty returns ~25% a year over the window. Every strategy figure is quoted
against that benchmark, never alone.

The demo account exists because none of the above can be settled by more
backtesting. Its fills are confirmed by a counterparty with no stake in the
answer, which no simulation of them can be.

---

## Running it

```bash
pytest                                  # ~2,830 tests, offline, about two minutes
pytest -m live                          # hits the real FRED API; excluded by default
pytest --cov=cfdinvest                  # needs the dev extra

cfdinvest-analytics evaluate            # the standard battery (below)
cfdinvest-analytics evaluate --walkforward   # adds out-of-sample, takes minutes

cfdinvest-schedule daemon               # everything, on its own cadence
cfdinvest-schedule run-once --refresh   # one pipeline pass
cfdinvest-schedule prune-runs           # bound the artefact store

cfdinvest-helper "long AAPL 0.68 entry=234.8 stop=228" --balance 2000
cfdinvest-confirm open AAPL --price 234.8 --quantity 0.684

python -m streamlit run cfdinvest/ui/app.py --server.address 127.0.0.1
```

`--server.address 127.0.0.1` is load-bearing in deployment: without it
Streamlit binds every interface and answers past Caddy.

---

## Evaluating performance

One command, so the same questions get asked the same way every time. Every
figure here used to come from a throwaway script, which is how two tables
came to disagree about the same universe while both were correct.

```bash
cfdinvest-analytics evaluate
```

It answers five questions, in the order they matter:

| question | how |
| --- | --- |
| Is there anything there? | Shuffle control — same trades, random directions, 200 draws |
| Is it beta? | Benchmark against buy-and-hold, plus the long/short split |
| Can it be harvested? | Slippage curve and the break-even against the benchmark |
| Does it survive selection? | Walk-forward, scored only on unseen stretches |
| What did it cost? | Spread, slippage, financing and FX as shares of what was charged |

The report refuses to flatter. A curve too short to judge reports absence
rather than zero. The break-even is measured where the strategy stops
beating the benchmark, not where it hits zero. The benchmark is printed
beside every headline. A shuffle control that fails says so.

---

## The strategy, as configured

| | |
| --- | --- |
| Universe | 8 US shares, one trading session (14:30–21:00 UTC) |
| Bars | Hourly |
| Entry / exit confidence | 0.75 / 0.20 |
| Time stop | 0.29 days — one session |
| Re-entry cooldown | 2 hours |
| Max gross exposure | 3.5× equity (≈70% of the balance as margin) |
| Max in one instrument | 20% of the gross ceiling |
| Slippage assumed | 2 bp |

**Why hourly.** A daily bar gave one decision per instrument per session,
which for a CFD is far too slow. Over two years: daily returned 347 trips a
year at 169 hours held; hourly returned 1,626 at 22 hours. Lower per trade,
higher in total, and the mechanism is the holding period — financing was 72%
of all cost at daily holding periods and is 12% now.

**Why one session.** The analysis layer builds one matrix across every
instrument and timestamp. On hourly bars a mixed universe is mostly holes —
FX trades around the clock, the Nikkei overnight, the FTSE closes before New
York — leaving each instrument absent from four rows in five. Measured on 41
mixed instruments at 1h: returns 19% populated, realised volatility 0.3%,
and **zero** active signals across 16,026 bars. Restricted to one session it
is dense again.

**Why individual shares only.** Every source of non-price evidence — EDGAR
filings, earnings dates, company news, sentiment — exists for a company and
cannot exist for an index. Measured: the three US indices lost money on
their own and their removal took the return from 81% a year to 127%.

---

## Execution: what can and cannot happen

Trading 212 has no CFD API, so it is never automated — driving its web UI
would breach their terms. That records a limitation of one venue and was
never a rule about this project; see `CLAUDE.md`, which is explicit about
having listed it as one by mistake.

The venue is **Capital.com**, on a demo account.

- `broker/capital/client.py` and `prices.py` are **read-only by
  construction**. No function places, amends or cancels; tests assert the
  dealing endpoints appear nowhere and that the only POST is the session.
- `broker/capital/dealing.py` **can** deal, and says so in its name. Two
  gates, checked inside every order rather than once at start-up:
  `CFD_CAPITAL_DEALING_ENABLED` must be explicitly truthy, and a live
  account needs `CFD_CAPITAL_ALLOW_LIVE` as well. Anything that is not the
  exact string `live` resolves to demo.
- `broker/capital/bridge.py` decides and places nothing. It mirrors the
  shadow book rather than reading the signals itself, so there is one
  decision and not two that can disagree.
- The **shadow book imports none of them**, and a test enforces it. Its
  worth is being a record nobody could edit once the outcome was known.

**IG was measured, rejected, and removed on 2026-08-28.** CFD commission was
a $15 minimum per side — 222bp round trip on £1,000, against a ~6bp budget.
Spread betting had no commission but a 12.2bp spread and a £7,490 minimum
position, against an intended account of £2,000. The API was fine; the venue
did not fit the size of account this project trades. Capital.com charges no
commission, quotes a bid and an ask without dealing, and finances at 7.86%/yr
long against Trading 212's 25.33%.

---

## The forward record

The **Capital.com demo account** is the forward record. The daemon reads
the engine's own persisted run, converts each position to a share of the
account's balance, and places what survives the bridge's guards. There is
one decision path: `persist/intent.py` reads what the engine decided and no
caller restates a strategy rule by name.

**The paper book was removed on 2026-08-30.** It ran eight simulated arms
at a fixed £1,000 stake, and it was kept for two reasons that did not hold
up. The arms were to test whether raising the confidence bar helps — a
question the engine now sweeps directly, and the arms sat at 0.50/0.60/0.70,
all inside the range since measured as destructive. And its baseline arm was
meant to hold the same positions as the account, so the gap between them
would measure how wrong the simulated fills were. It did not hold the same
positions: the book applied the time stop, cooldown, blackout and confidence
gate and none of the breakout gate, size hysteresis, decayed trim, leverage
cap or concentration cap. Measured through the engine, its rules return
163.7% at −52.8% against the engine's 130.5% at −40.3%. Worse, the account
mirrored the book, so it traded those rules too.

What is lost is forward A/B comparison: one account holds one
configuration. That is accepted — the backtest answers those questions
faster, and since the cost model was corrected it answers them honestly.

---

## Layer map

Read a package's README before changing it; each explains the failure it
prevents.

| | |
| --- | --- |
| `ingestion/` `repository.py` `storage.py` | The lake |
| `preprocessing/` `regimes/` `signals/` | Panels, classifications, targets |
| `execution/` `backtest/` | Fills, spread, slippage, FX, financing, stops |
| `analytics/` | Metrics, attribution, sweeps, walk-forward, Monte Carlo, **evaluate** |
| `universe/` | Which instruments to trade, by rule and point-in-time |
| `persist/` | Every artefact store. Account-aware. |
| `schedule/` | Daemon, market calendar, notifications, dealing |
| `risk/` `monitoring/` | Book-level limits; operational health |
| `livehelper/` | The T212 safety core: intent, safety, sizing, broker rules |
| `advisor/` | Suggestions, sentiment, the live check |
| `broker/capital/` | Read-only market data and prices; gated dealing; the bridge |
| `confirm/` `journal/` `insights/` `bias/` | What you did, and what it says |
| `simulate/` `metastrategy/` | Rehearsal; risk posture |
| `accounts/` | Registry and router |
| `ui/` | Four Streamlit pages. Optional install. |

**Computed once, read everywhere:** expectancy in `analytics/`, regime
labels in `risk.compute_regime_risk`, scheduler status in
`risk.scheduler_status`, fills in `execution/`. Never add a second.

---

## The dashboard

Five pages, deliberately. It carried twenty-three and almost none were
read.

- **Dashboard** — the demo account in summary: balance, equity, open P&L,
  what it holds, and whether the scheduled jobs are keeping up. Detail
  lives on the Broker page rather than being repeated here.
- **System status** — whether the machine is doing its job. Every daemon job,
  when it last ran, and what is overdue.
- **Signals** — what the model wants now, highest conviction first, with a
  red bar and a cross on anything below the entry threshold.

---

## Deployment

A single VM. Caddy terminates HTTPS and proxies to Streamlit on 127.0.0.1;
`cfdinvest.service` is a resident daemon owning every scheduled job;
`cfdinvest-deploy.path` watches for a GitHub webhook and runs `deploy.sh`,
which pulls, installs unit files, enables timers, restarts everything and
then **proves it took** — services active, dashboard answering, disk under
85%, and the running config read back from the process rather than the file.

That last part exists because code was once copied to the server without a
restart and ran eleven hours stale, reporting the wrong settings into its own
metadata while nobody looked.

Secrets live in `/etc/cfdinvest/env` and never in the repository.

---

## Testing

Tests are the deliverable, not a formality. ~2,830 of them, offline, about
two minutes. Coverage is 88%.

- **Mutation-check every suite.** Reintroduce each property as a bug and
  confirm a test fails. Roughly 200 mutations have been checked; each
  package's README lists its own. Several tests that looked fine passed
  against their own mutation and had to be rewritten — a structural test
  asserting an identifier appears in a file will happily pass while that
  code is unreachable.
- **Test the property, not the constant.**
- **Run the real thing on real data.** Most of the serious bugs here
  surfaced from smoke tests on the real lake, not from unit tests.
- **Treat a surprising number as a bug.** A 2,135% backtest turned out to be
  unowned leverage; a 42bn turnover figure turned out to be an 855% run
  showing up in price × volume.

---

## Known limits

- Two years of hourly history is all the vendor serves. One market regime.
- The universe is hindsight-selected; the benchmark is always quoted with it.
- Spread is estimated per asset class, not measured per instrument. It is the
  largest single cost line.
- Real slippage is unmeasured, and the edge breaks even near 6bp.
- `schedule/app.py` is 73% covered — the daemon dispatch, where several
  defects have hidden.

The root of all of these is the same: the system has barely traded. The
Capital.com demo account is the instrument built to change that, and it
needs weeks rather than attention. Note that it has only been trading the
engine's own rules since 2026-08-30 -- before that it mirrored a book with
a different rule set, so anything it recorded earlier describes a strategy
nobody chose.
