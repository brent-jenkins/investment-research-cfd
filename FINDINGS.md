# Findings

What ten weeks of building and testing a short-horizon CFD strategy found,
from 2026-08-24 to 2026-11-04. The short version: **the strategy had no
edge, and nothing tried in its place found one.** This file is the summary.
`CLAUDE.md` holds the full record, with every figure, every
pre-registration and every retraction, in the order they happened.

None of this is financial advice. Every trade was placed on a demo account.

---

## What was built

- **A backtesting engine** for CFDs. It models spread, slippage, overnight
  financing, FX conversion, stops and the broker's minimum deal sizes.
- **An automated dealer.** It traded a Capital.com demo account from
  2026-08-27, taking its positions from that engine's output and nothing
  else.
- **A manual-trading helper**, for a broker with no API.

The strategy was a trend follower on about a hundred US large-cap shares,
using hourly bars. Its signal combined three inputs: a 20/100-bar
moving-average crossover, a 55-bar breakout and 21-bar momentum. It opened
positions during the session and flattened everything before the close.

## The result

**The demo account went from £5,000.00 to £4,531.50.** Opening was paused
on 2026-10-01 and never resumed.

The backtest was checked against the live account before its verdict was
trusted. Over two weeks the account's engine recorded 88 entries it wanted
to make:

- 70 matched the backtest's entries exactly, by bar, name and side.
- 17 were the same trades opened late by the account.
- 1 is unexplained.
- The backtest made no entries that the live engine never intended.

On that faithful backtest, from July 2023 to September 2026, the strategy:

    lost 0.52% of equity a day (t = -7.65), in every year and both halves
    lost money BEFORE costs: gross slightly negative, costs about 0.44% a day

**The problem was not costs eating a small edge. There was no edge for
costs to eat.** A trade's direction was right about half the time, and
every round trip paid the spread: about 0.13% for these names, more at the
open.

## Seven routes to the direction of a trade, all a coin flip

Picking which names would move most worked. The busiest fifth moved about
twice as far as the quietest. Picking which way they would move never did.

| Source of direction | Sample | Result |
| --- | --- | --- |
| The engine's own inputs (crossover, breakout, momentum) | 6 months, 50 names | nothing reaches t = 1 |
| Headline sentiment, recorded live from 2026-09-27 | 475 calls | gross -0.17% a call; stopped for futility |
| Claude Haiku reading each company's own 8-K filing | 587 name-days | gross -0.04% (t -0.44) |
| Claude Sonnet given everything knowable at 10:00 New York | 300 name-days | 50.3% right |
| A model refitted every day on the last two weeks | 17,378 calls | 49.5% right; worse than the same model on shuffled labels |
| The last hour's minute bars, against the next 1-4 hours | 4,628 calls | 48-50% right at every horizon |
| Nasdaq's signed order flow, whether trades hit the bid or lifted the offer | 17,877 calls | identical to the same signs shuffled |

Each was registered in writing before its data was read: the test, the pass
mark and what each outcome would mean. Nothing was chosen after seeing a
result.

## Changing what it traded did not help either

- **Fewer names.** Losses shrank as the universe narrowed, but the book
  was never positive after costs. At five names it was still a third of
  the spread short.
- **Low, middling or high volatility.** Each band was run separately with
  15 names spread across sectors. All three lost money before costs, in
  every year.
- **Names it had been good at.** The best performers in the first half of
  the period did slightly *worse* than average in the second half (rank
  correlation -0.18).
- **Holding for days instead of hours.** This halved the cost, because a
  position held for days pays one round trip instead of one a day.
  Overnight financing added only 0.013% a session. It left the gross
  unchanged, so the result was still a loss of 0.30% a day.
- **Choosing which days to trade.** No reading known before the open
  picked out the good days: not recent performance, the VIX, market
  trendiness, dispersion or the opening move.
- **Smaller companies.** The research literature finds news signals
  strongest in small caps. On Capital, their spreads were 50-66bp a round
  trip, which closed that idea before any test.
- **Trend following on 21 other markets** (indices, commodities and
  currencies, on daily bars, 2005-2026). Before costs it showed a small
  positive, almost all of it from 2005-2015. After costs it lost money.

## Real effects that were too small to trade

- **Pre-market moves partly reverse by 10:00 New York.** This is
  statistically real (t -3.1, in both halves and net of the market's own
  move). The reversal is smaller than one round trip, and it happens in
  the hour when spreads are widest.
- **Volume and regulatory filings predict how far a share moves, never
  which way.** The same was true of every input tested.
- **Bitcoin and Ethereum trend following made about 21% a year.** On the
  twelve largest coins of January 2018, chosen before anyone knew which
  would survive, it made 1.2%. The result came from the two winners, not
  from the rule.

## Lessons about method, which outlive the strategy

1. **A correctly implemented wrong measurement looks exactly like a right
   one.** The engine's most-used exit looked at a bar's close and then
   filled at that same bar's open. Fixing that one look-ahead took the
   headline backtest from **+467.7% a year to -47.5%**. Every figure
   produced before the fix had to be withdrawn.
2. **One engine, or two strategies.** A separate paper-trading book
   re-implemented some of the rules and left out others. For a while the
   demo account traded that book's rules, which no backtest described.
   Anything that decides a position now goes through one function.
3. **Anchor the simulator to real fills.** The backtest was trusted only
   once it reproduced the live account's own entries bar for bar. Before
   that, a harness that could not be reproduced had produced +191% a year.
   The faithful one produced -74%.
4. **Write the test down before reading the data.** Every late test here
   named its pass mark and the action for each outcome in advance. That is
   what makes a null result trustworthy rather than a thing to tune away.
5. **Hindsight is in the universe as well as in the model.** The shares
   were chosen in 2026, from companies that had already done well. That
   flatters any backtest that leans long, so it made the results here look
   better, not worse. The crypto test above shows the size of the effect.
6. **Retract a wrong figure everywhere it was written.** A withdrawn
   number that survives in a docstring is worse than no number, because
   the next reader cannot tell it from a measured one.

## The repository

The code is kept as it stood at the close, under the MIT licence. Its 3,923
tests run offline. `CLAUDE.md` explains the system's principles and records
every finding in sequence. Each package's README explains the failure that
package exists to prevent. Nothing in the repository runs or deals any more.
