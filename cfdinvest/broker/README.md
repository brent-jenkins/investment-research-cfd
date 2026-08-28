# `broker/` — the only code that can move money

## The failure this prevents

Until 2026-08-26 this project forbade broker API calls outright. That was
never a principle: Trading 212 has no CFD API, so automating it would mean
driving the web UI, which breaches their terms. It recorded a *limitation of
what one venue makes available* and it was mistaken for a rule — see
`CLAUDE.md`, which says so at length. It lapsed the moment a broker with an
API was reachable.

What replaced it is narrower and, in the ways that matter, stricter. The
failure being prevented is not "an order is placed" — it is **an order placed
because a configuration drifted**, by code nobody thought could trade.

## The shape

Four modules, and the first split is the whole design.

`client.py` is **read-only by construction**. Sessions, instrument metadata,
prices, the account balance. There is no function that could place, amend or
cancel — not one that is disabled, one that does not exist. A test asserts
the dealing endpoints appear nowhere in it, that the only `POST` is the
session, and that no `DELETE` or `PUT` exists at all.

`prices.py` sits on the read-only side and walks historical bars, because
Capital quotes a bid and an ask on every one of them. Two things about the
walk were measured rather than assumed: a range wider than a thousand bars
is *refused* rather than truncated, so it walks forward from a start instead
of requesting a span; and a REST session lasts ten minutes against a
backfill that runs for an hour, so it renews mid-walk. Without that it would
return short histories and call them complete.

`dealing.py` **can** deal, and is named so that nothing imports it by
accident. Two gates, checked inside every order rather than once at
start-up:

- `CFD_CAPITAL_DEALING_ENABLED` must be explicitly truthy — and "truthy" is
  a closed set, so `maybe` and `2` are refusals. A gate that opens on any
  non-empty value is not a gate.
- A **live** account needs `CFD_CAPITAL_ALLOW_LIVE` as well. One switch is a
  typo away from a funded account; two mean the mistake has to be made
  twice, in different words.

Closing is gated identically to opening. Safety that depends on which
function you happened to call is not safety.

`bridge.py` decides what to place and places nothing. It is pure: it reads
the shadow book, sizes against the balance the broker reports, applies every
limit, and returns intents. Since 2026-08-28 it does not decide *what* to
hold — the shadow book does, and the bridge mirrors it. Two decision paths
reading the same signals could disagree, and the value of running a
simulation beside an account is that a difference means the simulation was
wrong, not that the two made different calls.

## Judgement calls

**Demo unless `live` is spelled exactly.** A typo, an empty string,
`production` — all resolve to demo. A misspelt environment silently reaching
a funded account is the failure worth being paranoid about, and the cost of
the paranoia is that somebody typing `liv` gets the demo host.

**Secrets carry `repr=False`.** These objects reach debug output and
exception context, and a broker key is not a FRED key — the blast radius is
an account rather than a rate limit.

**A deal reference is not a fill.** The broker accepts the request and
decides afterwards; an accepted-but-unconfirmed order is recorded with `ok`
false and its error intact, never as a position. Believing otherwise would
put the record ahead of the account, and reconciliation would then report
the account as missing something that was never there.

**The calendar decides the session, never the broker's flag.** Capital
reported eighteen of twenty instruments as tradeable forty-six minutes after
the close. Spreads widen a median of 1.85× out of session and JNJ by 14.9×,
against a strategy that breaks even near 6bp of friction.

## Why it exists: to measure, not to trade

Every instrument was charged an *estimated* spread — a flat constant, the
largest single line in the cost model, against an edge that breaks even near
6bp of further friction. That figure has been wrong in this repository three
times: written as 4bp, corrected to 10bp, and finally measured as **5.0bp
round trip** actually charged. `CLAUDE.md` records all three and why the
second correction was itself wrong.

Capital quotes a bid and an offer without dealing at all, which is what let
the measurement finally happen. Against 253,283 of its own hourly bars the
model **undercharges** spread on nineteen of twenty instruments at the
opening hour — the hour this strategy deals — by 1.85× at the median.

## What the measurement found about IG

**IG was measured, rejected, and its integration removed on 2026-08-28.**
The finding is kept because it cost real orders to learn, and because it is
the reason this package talks to Capital instead.

| | CFD | Spread bet |
| --- | --- | --- |
| Spread (AAPL) | 2.6 bp | 12.2 bp |
| Commission | **£11.12 per side** | none |
| Minimum exposure | ~£310 | **£7,490** |

The CFD commission is a $15 minimum charged both ways — **222 bp round trip
on £1,000**, against a ~6bp budget, and it only falls to 4bp at a position of
£55,600. Spread betting has no commission but its spread is three times the
break-even and its minimum bet is 3.7× the intended account. IG also would
not show that account share prices at all
(`unauthorised.access.to.equity.exception`), so its spreads could not be
quoted without dealing.

The API had nothing to do with any of it. The code worked; the venue did not
fit the size of account this project trades.

## Traps

- **Session tokens come back in the response headers**, not the body.
  Reading the body yields a session that authenticates once and then fails.
- **A REST session lasts ten minutes.** Any walk longer than that must renew
  or it returns a short answer and reports success.
- **The documented rate limit is ten requests a second**, and this client
  paces at half of it from one constant. A second copy of that number
  somewhere else is a limit waiting to drift.
- **`GET /markets?epics=a,b,c` reads twenty instruments in one call.**
  Resolving symbols one at a time makes forty calls for the same answer.

## Mutations checked

| | |
| --- | --- |
| M267 | A dealing endpoint added to the read-only client |
| M268 | Any `l`-word treated as live |
| M269 | The API key or password in a repr |
| M270 | A fuzzy search match accepted as the instrument |
| M271 | Instruments that failed to read dropped from the table |
| M272 | Spread compared in points across instruments of different price |
| M273 | Session tokens read from the body instead of the headers |
| M278 | Dealing on by default, or the gate opening on any value |
| M279 | Live permitted on one switch |
| M280 | The gates checked once at start-up instead of per order |
| M303 | A sizing rule invented here rather than the engine's |
| M304 | A size rounded up to a dealing increment |
| M305 | A size below the minimum raised to it instead of refused |
| M306 | An order placed outside the exchange session |
| M307 | The per-instrument cap not applied |
| M308 | The gross cap applied before the keep rather than after |
| M309 | A reversal netted into one order |
| M310 | A drifted position resized inside the hysteresis |
| M311 | A mismatch between account and record ignored |
| M312 | The halt still letting orders through |
| M321-328 | The price walk: expiry, pagination, spread, coverage |

M177-M189 were retired with the IG integration. They are not renumbered:
the numbers are a record of what was checked and when, and reusing them
would make two different findings share a name.

## What it writes

Nothing. `client.py` and `prices.py` are pure network readers, `bridge.py`
is a pure decision, and `dealing.py` returns what the broker said. Callers
decide what to record — the order store in `persist/orders.py` is where it
lands.
