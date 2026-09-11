# Risk dashboard

What the book is carrying, and what it is carrying it into — read from what
the pipeline already wrote.

```bash
cfdinvest-schedule risk-snapshot --balance 2000
cfdinvest-schedule risk-snapshot --watch --interval-minutes 15
```

```python
from cfdinvest.risk import RiskLimits, snapshot_from_disk

snapshot = snapshot_from_disk(config, limits=RiskLimits(account_balance_gbp=2_000))
print(snapshot.severity, snapshot.message)
for flag in snapshot.flags:
    print(" !", flag)
```

Also a page in the dashboard: **Risk**.

---

## It cannot trade, and it cannot start a run

There is no broker client here, no HTTP call, no credential, and no import of
the backtest engine, the repository or the scheduler's runner. It reads
persisted artefacts and writes one kind of file — a risk snapshot, into its
own directory, which nothing that trades ever reads back.

That is enforced, not promised. `tests/test_risk.py` parses every module in
this package and fails if any of them imports `cfdinvest.backtest`,
`cfdinvest.repository`, `cfdinvest.schedule.runner`, `cfdinvest.sources`,
`cfdinvest.vendors`, `requests`, `urllib`, `httpx`, `socket`, `selenium`,
`playwright`, `subprocess` or `webbrowser`, or mentions `https://`,
`trading212.com` or an API key.

The reason is narrower than general caution. A monitor that could start a run
would be reading artefacts it had caused, so "what was the book carrying at
04:00" would become "what did the monitor make it carry at 04:00". The
dashboard is only worth trusting if it is downstream of everything.

---

## Which book, and never the backtest

The positions come from a real book. Until 2026-08-27 they came from
`positions.parquet` in the newest run — a simulation whose equity had
compounded for two years — measured against a £2,000 account's limits. That
morning it reported **gross exposure of £810,945 against a £3,000 limit**,
five breaches, critical. Nothing anybody held was involved, and the figure
had never been converted out of dollars either.

A limit breached every day is a limit nobody reads. A real breach would have
arrived on a page that was already red, while the monitoring snapshot said
green in the same minute — the two-owners split this project is otherwise
careful about.

| Source | When it is used | What it is |
| --- | --- | --- |
| `confirmed` | any confirmation has ever been recorded | the real account, replayed |
| `paper:<arm>` | **never — the paper book was removed on 2026-08-30** | kept in the table because old snapshots on disk still carry the label |
| absent | neither | reported absent, not flat |

**Only the account's own book can breach the account's limits.** The paper
book had no position cap on purpose — it measured signal quality, not
portfolio construction — so its gross exposure passes a £2,000 account's
£3,000 limit as soon as it holds four positions and stays past it for good.
On the afternoon this shipped the baseline arm held eight, £8,010 against
£3,000.

So a simulated book's breaches are demoted to `advisory`: reported, never
raised, and never `critical`. The comparison is worth seeing — "following
every c35 signal would have you carrying four times your account" is a real
thing to know — it is just not a statement about what you are carrying.

And the account is then `unverified`, not flat. No confirmation on file is
not evidence of a flat account; it is the absence of evidence either way,
which is this layer's oldest rule applied to the account rather than to one
of its artefacts.

That is the third time this page has been made to shout something
structural — the backtest's compounded positions, then an empty arm reading
as unknown, then this. Each one would have made a real breach arrive on a
page already red.

An arm that holds nothing is **flat and checked**, not unknown. The raised
arms are legitimately empty for days at a time, and calling each of those
days a warning is how a status page teaches you to ignore it. The
distinction is a book that was read and is empty against no book at all --
which is the same absence rule as everywhere else, one level in.

The baseline arm only. The four arms are independent experiments at
different confidence bars; adding them together describes a book that trades
every threshold at once, which is nobody's and four times too big.

A confirmed book that holds nothing is still the confirmed book. Having
closed everything — or having recorded only decisions *not* to trade — is a
fact about the real account, and falling back to the simulation there would
report positions that were never taken.

Marks are converted to sterling at the rate the position was struck at,
which both books record. A stale rate is a small error; no rate is a number
that is wrong by the exchange rate and looks right.

The balance is the account's, never the run's. It is the denominator of
leverage and margin usage, so taking it from a backtest would put the same
mismatch back in the denominator after removing it from the numerator.

---

## The rule that shapes everything here

**Nothing is inferred. What could not be read is named.**

Exposure needs a price, and `positions.parquet` holds units. If the run stored
no analysis panels, the page shows position *units* and reports exposure
absent. It does not reach for yesterday's close, or the entry price, or 1.0.

The same goes for the rest:

| Missing | What you get | What you never get |
| --- | --- | --- |
| Analysis panels | Units per instrument | An exposure figure |
| Instrument leverage | Effective leverage | A margin requirement |
| Currency map | Exposure by instrument, unmapped names listed | An FX exposure that looks complete |
| `regime.parquet` | Nothing, marked unavailable | `"unknown"` |
| Recorded assessments | Nothing, marked unavailable | "no unsafe trades" |

The distinction that matters most is the last one. **No record is not the same
as no unsafe trades**, and a page that showed `0 unsafe` for an empty log would
be making the strongest possible claim out of the weakest possible evidence.

This is why `severity` has three values and not two:

```
critical   a limit is breached
warning    no limit is breached, but something could not be checked
ok         no limit is breached, and everything was checked
```

"No limit breached" and "no limit could be checked" are different statements,
and collapsing them is how a dashboard ends up reassuring.

---

## Two limit sets, deliberately not merged

| | `RiskLimits` (here) | `LiveHelperConfig` (`cfdinvest.livehelper`) |
| --- | --- | --- |
| Question | How much am I carrying? | May I open this? |
| Scope | The whole book | One trade |
| When | After trades are on | Before you type it in |
| Exposure cap | Gross, across everything | Per instrument, for this trade |

A single `max_exposure` meaning both would be wrong for one of them: the most
you may put into one position and the most you may carry in total are
different numbers, and a config with one field would silently pick a side.

---

## Two leverages, also not merged

**Instrument leverage** is set by Trading 212 per instrument and divides
exposure into margin. **Effective leverage** is your gross exposure over your
balance, and it is the one you control — by trading smaller.

Trading 212 does not publish instrument leverage in machine-readable form, so
margin is computable only for instruments you have supplied it for. Partial
knowledge is reported as partial:

```python
leverage, margin = compute_leverage_and_margin(
    exposure, limits, instrument_leverage={"SPY": 20.0}
)
margin["margin_required_gbp"]   # None -- GLD's leverage is unknown
margin["complete"]              # False
margin["unknown_leverage"]      # ("GLD",)
```

An assumed leverage would produce a margin figure that is confidently wrong in
whichever direction the assumption leaned, and margin is the number Trading 212
force-closes on without warning.

---

## Did the last run finish, or did it die?

The scheduler writes `last_run` **before** the pipeline starts and
`last_success` only when it completes. That ordering is the whole point:

```
last_run = 09:00, last_success = 09:00:42   ->  OK
last_run = 09:00, last_success = none       ->  FAILED   (started and died)
last_run = 09:00, last_success = yesterday  ->  FAILED   (this one died)
nothing recorded                            ->  UNKNOWN
```

No amount of looking at run directories could tell you the middle case — a
directory exists whether or not the run that made it survived. Without this,
the dashboard shows a stale book with no sign that it is stale, which is the
one failure mode where every number on the page is wrong and none of them
looks it.

Staleness is reported *alongside* the status rather than folded into it. A run
that succeeded three days ago succeeded; conflating that with a failure would
hide which of the two happened.

---

## Drawdown is measured against the run's own equity

The run's starting equity, not the balance you hold today.

This was a real bug. A run simulated at £100,000, measured against a £2,000
operator balance, reported a **75% drawdown** for a run that never lost 3%.
The number was not noise — it was arithmetic on two different accounts.

```python
compute_pnl_risk(pnl, limits, initial_equity=100_000.0)["max_drawdown"]
# -0.0579, which is what cfdinvest.analytics reports for the same run
```

`tests/test_risk.py` asserts the two agree. Two modules computing the same
quantity from the same artefacts must produce the same number, and the test
that checks it is worth more than either implementation.

---

## The overnight split is the finding

`pnl.parquet` carries the engine's own six-way decomposition, so the gap is
separable from the intraday move without reconstructing anything:

```python
risk["gap_pnl"], risk["intraday_pnl"], risk["overnight_share"]
```

A strategy whose profit arrives overnight is carrying gap risk **a stop cannot
bound** — the position was not in the market for the move that made the money,
and will not be in the market for the move that takes it back. One whose losses
arrive overnight is being stopped out by moves it never traded.

Neither shows up in a Sharpe ratio.

---

## The periodic task

```bash
cfdinvest-schedule risk-snapshot --watch --interval-minutes 15
```

Every N minutes: read the latest positions, regimes, P&L and scheduler state;
compute a snapshot; record it. Snapshots are append-only, one timestamped file
each, because a snapshot is a *reading* and that moment does not come back —
the useful question is usually "when did this start", not "what is it now".

It is a separate loop from `cfdinvest.schedule.scheduler`, which fires on
market *session* events. Risk is worth observing on the clock instead, and
most usefully when nothing is happening: a book that drifts into a breach at
3am did so between two session events, and a snapshot taken only at the close
would date the breach to the close.

The clock and the sleeper are injected, so the loop runs a simulated day in a
test without sleeping. A failed read is contained rather than raised — an
unreadable artefact at 03:00 must not end the watch that would have caught the
breach at 04:00 — and so is a failing alert handler, because one bad mail
server should not stop the monitoring.

---

## Elevated regimes are flagged, not blocked

`crisis`, `stagflation`, `risk_off`, `high`, `breakdown`, `contraction` raise a
flag on the dashboard and nothing more. A crisis is also where the opportunity
is, and a risk page that told you to stop trading in one would be a strategy,
not a measurement.

---

## Recurring problems are grouped by check, not by sentence

The live helper's sentences carry amounts, so `Exposure £4,000 is over your
limit` and `Exposure £3,000 is over your limit` would count as two different
problems. The assessment log records *which check* failed, and the dashboard
groups on that:

```
2x  Exposure over the limit
1x  No stop-loss set
1x  Risk per trade over the limit
```

Only the blocking checks count as reasons. A warning finding is not why a
trade was refused.

---

## Layout

| Module | What it does |
| --- | --- |
| `model.py` | Every metric, and the flags. Pure, offline, deterministic. |
| `scheduler_status.py` | Completed / started-and-died / nothing recorded. Pure. |
| `snapshot.py` | The only thing that touches a filesystem, and it only reads. |
| `monitor.py` | The periodic task, and its CLI-facing loop. |

`model.py` stays pure so a test can assemble any combination of missing
artefacts without a disk. Every absence path in the table above is tested that
way, which is the only reason they are all covered — a fixture lake can be
missing one artefact at a time, but not usefully missing eleven.

---

## Tests

```bash
python -m pytest tests/test_risk.py -q
```

84 tests. Beyond the metric arithmetic, they pin:

- **The drawdown matches `cfdinvest.analytics`** on a real run, computed two
  different ways.
- **No module here can trade or drive the pipeline**, by parsing the imports.
- **The monitor leaves the lake unchanged** — `list_runs` before and after.
- **A snapshot writes nothing** — the file listing is identical afterwards.
- **A truncated log line loses one line, not the log.**
- **Positions with no prices read as unchecked**, never as within limits.

The suite was mutation-checked: reintroducing the drawdown-denominator bug,
collapsing "started and died" into `OK`, defaulting a missing price to 1.0, and
grouping unsafe reasons on prose each make it fail. So does every one of the
eight ways to get the book wrong listed above — reading the backtest again,
reporting an unknown book as flat, preferring the simulation to a
confirmation, dropping a confirmed book that holds nothing, summing the arms,
losing the sign of a short, leaving the mark in dollars, and taking the
balance from the run.
