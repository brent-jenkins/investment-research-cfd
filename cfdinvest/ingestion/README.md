# `ingestion/` — filling the lake, and knowing when it did not

## The failure this prevents

Everything downstream trusts the lake. A gap in it does not announce itself:
a missing ticker is skipped with a log line, a stale interval keeps serving
yesterday's answer, and a vendor that silently returns nothing looks exactly
like a quiet market. The failures here are all the same shape — **not an
error, just an absence that reads as data**.

Three of them have actually happened:

- The engine moved to hourly bars, so `config.market.interval` became `1h`,
  and **the daily lake stopped being refreshed by anything at all**. Nothing
  broke. The universe screen carried on ranking on months-old turnover.
- A ticker was added to the configuration and had **no bars for hours** —
  the universe said twenty and nineteen were traded.
- A configured start of `2005-01-01` on hourly bars returns **nothing**,
  because the vendor refuses a request reaching past its window rather than
  truncating it. The lake stayed empty with a warning to show for it.

## What is in here

`market.py` — yfinance OHLCV, per ticker per interval, incremental by
default.

`macro.py` — FRED series, point-in-time by publication date.

`earnings.py` / `filings.py` — reporting dates and EDGAR 8-K events.

`capital_prices.py` — Capital.com hourly bars into a lake of their own,
under the interval label `capital_1h`. Separate from `1h` on purpose: the
two sources disagree about what a bar is, and merging them makes a third
thing that is neither. It is the only source here that carries a bid and an
ask on every bar.

## Judgement calls

**Intervals coexist.** The lake is `raw/market/<interval>/<TICKER>.parquet`,
so hourly and daily live side by side. That matters because they answer
different questions: the engine trades hourly, and the universe screen reads
daily, where liquidity and volatility have years of history rather than two.

**The daily job tops up both intervals.** Not the intraday job — a daily bar
changes once a day, and re-fetching it every fifteen minutes would ask the
vendor for the same row ninety times a session.

**An intraday start is clamped into the vendor's window.** Yahoo serves
roughly 730 days of hourly bars and *refuses* anything reaching further, so
the whole response comes back empty. Clamped rather than raised: the
configured start says how far back to go if the vendor allows it, and one
interval being unable to reach that far is not a configuration error. The
warning still reports what is being given up.

**Point-in-time macro by default.** FRED revises. Using today's value of a
series for a decision made two years ago is lookahead wearing a
respectable-sounding name, so `publication_lag` is the default rather than
an option.

**A symbol that fails is reported, not dropped.** One bad ticker never fails
the run — but it appears in the report with its reason, because a silently
missing instrument is a universe that quietly shrank.

## Traps

- **A categorical column index does not survive a Parquet round trip.**
  `storage.write_parquet` flattens it; every `positions.parquet` was
  unreadable before that fix.
- **`x or fallback` treats a legitimate zero as missing.** This hid a
  zero-bar run from the monitoring layer.
- yfinance serves ~730 days at `1h`, ~85 days at `15m` and `5m`. Only
  hourly has enough history to validate a strategy against.
- A part-finished session's volume compared against full-day medians reads
  low for **every** instrument at once. That is a clock artefact, not a
  quiet market, and it once returned 0.08× across the whole universe.
- **A Capital.com REST session lasts ten minutes.** A full backfill runs far
  longer, so expiry is the expected case rather than a fault. A walk that
  did not renew would return a *short* history and report success, which is
  the dangerous kind of wrong.
- **A resume cannot infer that the caller wants more history.** The earliest
  bar on disk is normally later than the requested start, because the
  server's history begins where it begins — so comparing the two refetches
  three years on every ordinary resume. Widening the window is `refetch=True`,
  asked for rather than guessed.
- **Capital's `volume` is its own clients, not the market** — a few hundred
  contracts on an AAPL hourly bar. It must never reach the universe screen,
  which ranks on turnover and would end up ranking by what one broker's
  customers happen to like.

## Mutations checked

| | |
| --- | --- |
| M151 | A configured start that returns nothing at all |
| M176 | A lake that quietly stops updating one interval |
| M193 | A universe of twenty where nineteen are traded |
| M131 | A partial bar compared against full-day medians |
| M321 | The price walk stopping at the first expired session |
| M322 | A repeated page walking until the call budget is gone |
| M323 | The spread folded into the price, read as a move |
| M324 | A second copy of the request interval, free to drift |
| M325 | Dividends written as zero, claiming there was none |
| M326 | A resume that starts over, or drops what is on disk |
| M327 | A requested end treated as advisory |
| M328 | One bad instrument taking down the whole backfill |

## What it writes

`<data root>/raw/market/<interval>/<TICKER>.parquet` (including
`capital_1h/`),
`<data root>/raw/macro/<SERIES>.parquet`, and the earnings and filings
stores. Nothing else in the project writes to `raw/`.
