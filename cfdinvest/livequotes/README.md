# Live quotes

The current price of what you are holding, and nothing else.

```python
from cfdinvest.livequotes import held_instruments, poll_live_quotes, write_quotes

held = held_instruments(config)              # from the confirm layer
write_quotes(poll_live_quotes(held), config) # latest.json, rewritten
```

```bash
cfdinvest-schedule poll-live-quotes
```

---

## The gap this closes

The lake is daily. Between one close and the next, every price this project
reasons about is yesterday's — which is right for a backtest and wrong for
someone about to place a trade. At 15:00 the helper was sizing a position
against a number set at last night's close, and in a moving market that is
not a small difference.

So: current price, for the open book, every five minutes. That is the whole
feature.

---

## Why only the current price

**Because an intraday series beside the lake would be a second source of
truth for the same instrument**, and the first time the two disagreed it
would be found by a trade rather than by a test. The lake is written by the
ingestion layer, once a day, from one vendor, with anomaly flags and a
manifest. Nothing here goes near it.

There is also nothing to be gained. Signals, regimes and backtests are all
computed on daily bars. A five-minute series would not feed any of them — it
would sit there accumulating, waiting for someone to use it by mistake.

So the store keeps **one price per instrument** and overwrites it on every
poll. This is the only store in the project that is not append-only, and it
gets the same exemption the account registry gets: it says what is true
*now*. A price from five minutes ago is not an observation worth keeping.

## Why no broker call

Trading 212 has no public price API, and this project makes no broker calls
of any kind — that constraint predates this package and is not weakened by
it. The quote comes from a public market data endpoint. Nothing here places,
prepares, or describes an order.

`yfinance` is not used either: it is a bar-history library, and pulling it in
to read one number would put an intraday-history fetcher one call away from
code that must never make one. `urllib` and a JSON parse are the whole
dependency.

### The endpoint, and a deviation worth recording

The spec asked for `/v7/finance/quote`. That endpoint now answers:

```
401 Unauthorized — "User is unable to access this feature"
```

without a cookie and a crumb. Getting past it means scraping a credential to
defeat an access control that was added deliberately, and it breaks whenever
the scheme changes. The price is instead read from `meta.regularMarketPrice`
on the chart endpoint, which answers unauthenticated.

That response happens to carry a bar array. **This package never reads it.**
The rule was "no intraday bars", and no intraday bar is read, parsed or
stored — but the URL is not the one the spec named, and that is recorded here
rather than buried.

---

## The split that holds the safety property

| Module | May open a socket |
| --- | --- |
| `sources.py` | **yes** — the only one |
| `storage.py` | no |
| `poller.py` | no |
| `__init__.py` | no |

`storage.py` is imported by the live helper, the risk layer and monitoring.
All three sit behind structural tests forbidding them a network. If reading a
quote pulled in the fetcher, those tests would keep passing and would have
stopped meaning anything — so a test asserts the split directly, in both
directions.

The live helper reading a quote is also the reason it may not fetch one: a
price the helper had to go and get is a price it can block on, at exactly the
moment someone is waiting to trade.

---

## Absence, and staleness

Three different nothings, kept apart:

- **No price could be read.** The instrument is left out of `latest.json`
  rather than written as `null`. A null in that file would sit there looking
  like a measurement.
- **A price exists but is old.** Refused, not used with a caveat. A price
  that is wrong in a fast market does more damage than one that is a day old
  and known to be. Fifteen minutes — three missed polls; one miss is a
  hiccup, three is the poller not running.
- **The file cannot say when it was taken.** Also refused. Unknown age is not
  "fine", and this project does not read silence as reassurance.

Zero is treated as missing. Nothing here trades at zero, so a zero is a
missing field that arrived as a number — and it would size a position as
though the instrument were free.

Every refusal falls back to the daily bar, which is what the helper used
before this package existed. **The fallback direction is always towards the
older, duller, better-understood number.**

The helper says which it used, and why the better one was not available:

```
Using live quote — live quote, 1 minutes old
Using daily bar — close of 24/08/2026; the live quote is 30 minutes old (limit 15)
```

"Using daily bar" on its own would leave you wondering whether quotes are
broken or simply not running.

---

## Where the instruments come from

The confirm layer, which is the only record of what you actually hold. Not
`accounts/registry.py` — that says what accounts *exist*, not what is open in
them. Not the simulated book either: this exists to price what you are
carrying, and the two differ exactly when it matters.

---

## Monitoring

Stale quotes raise a **warning**, never a critical. The readers already fall
back on their own, so nothing is broken. What is worth saying is that the
fallback is happening, because from the outside a quiet poller and a quiet
market look identical.

A deployment that has never polled raises nothing at all. Warning about a
feature nobody switched on trains you to ignore the banner.

The risk snapshot records `live_price_used`, because a snapshot priced from a
live quote and one priced from yesterday's close cannot be compared — and the
difference between them looks exactly like a market move.

---

## Tests

```bash
python -m pytest tests/test_livequotes.py -q
```

35 tests, offline. `fetch_quote` takes an `opener` seam, so the parsing is
exercised against real response shapes without a network.

Mutation-checked — each of these makes the suite fail:

- **Accepting a zero price** instead of treating it as missing.
- **Using a stale quote** rather than refusing it.
- **Treating an undated quote file as fresh.**
- **Guessing a Trading 212 instrument** for a bond ETF that has no CFD.
- **Marking the shipped suggestions as confirmed.**
- **Keying the daily jobs on elapsed time** rather than on the last run's
  date — which re-runs ingestion after a restart.
- **Ignoring the last run entirely**, so a daily job fires every tick.

The structural tests are the ones worth keeping honest: they assert that
`storage`, `poller` and `__init__` cannot reach `urllib`, that `sources` can,
and that neither the live helper nor monitoring nor `persist/risk.py` imports
`sources`.
