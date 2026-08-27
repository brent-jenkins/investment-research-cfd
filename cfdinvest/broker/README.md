# `broker/` — the only code that can move money

## The failure this prevents

Until 2026-08-26 this project forbade broker API calls outright. That was not
squeamishness: Trading 212 has no CFD API, so automating it would mean
driving the web UI, which breaches their terms. The constraint was
*contingent* on that, and it lapsed when an IG demo account was opened.

What replaced it is narrower and, in the ways that matter, stricter. The
failure being prevented is not "an order is placed" — it is **an order placed
because a configuration drifted**, by code nobody thought could trade.

## The shape

Two modules, and the split is the whole design.

`client.py` is **read-only by construction**. Sessions, instrument metadata,
prices. There is no function that could place, amend or cancel — not one
that is disabled, one that does not exist. A test asserts IG's dealing
endpoints appear nowhere in the source, that the only `POST` is the session,
and that no `DELETE` or `PUT` exists at all.

`dealing.py` **can** deal, and is named so that nothing imports it by
accident. Two gates, checked inside every order rather than once at start-up:

- `CFD_IG_DEALING_ENABLED` must be explicitly truthy — and "truthy" is a
  closed set, so `maybe` and `2` are refusals. A gate that opens on any
  non-empty value is not a gate.
- A **live** account needs `CFD_IG_ALLOW_LIVE` as well. One switch is a typo
  away from a funded account; two mean the mistake has to be made twice, in
  different words.

Closing is gated identically to opening. Safety that depends on which
function you happened to call is not safety.

`prices.py` sits on the read-only side and walks historical bars. Its reason
for existing is the bid and ask on every one of them: spread is the largest
single item in the modelled cost and is currently one estimated constant for
every instrument in every hour. These bars replace that guess with a series.
Two things about the walk are not obvious and both were measured rather than
assumed — a range wider than a thousand bars is *refused* rather than
truncated, so it walks forward from a start instead of requesting a span;
and a REST session lasts ten minutes against a backfill that runs for far
longer, so it renews mid-walk. Without that it would return short histories
and call them complete.

## Judgement calls

**Demo unless `live` is spelled exactly.** A typo, an empty string,
`production` — all resolve to demo. A misspelt environment silently
selecting the real account is the worst possible way to learn about a typo.

**Secrets never appear in a repr or an error string.** These objects reach
debug output and exception context, and IG's error bodies echo detail not
worth logging beside a username. A broker key is not a FRED key: the blast
radius is a funded account.

**A deal reference is not a fill.** IG accepts the request and decides
afterwards, so an order returns `SUBMITTED` and only a confirmation carries a
level. A rejected deal gets no level at all rather than a zero.

**A probe that cannot close shouts.** The worst outcome is not a failed
measurement — it is a position opened by one and left running, because
nobody watches a measurement for risk. It reports `OPENED BUT NOT CLOSED`
and names the deal id.

**The paper book imports none of this**, and a test enforces it. Its worth is
being a record nobody could edit once the outcome was known, and a simulation
that sometimes places an order is not one.

## Why it exists: to measure, not to trade

Every instrument charged an *estimated* spread — a constant of **10bp round
trip**, the largest single line in the cost model at 46.6% of it, against an
edge that breaks even near 6bp of further friction. That figure was written
here as 4bp and as 79% of cost; both were wrong, and the thing that exposed
them was finally measuring. See `CLAUDE.md`.

IG will not show this account share prices
(`unauthorised.access.to.equity.exception`), so the spread cannot be quoted
there. **Capital.com will**, which is why the measurement finally happened
without dealing at all — see `capital/`.

It can be **filled**. A buy fills at the offer, a sell at the bid, and the
difference is what was actually charged. That is a better measurement than a
quote anyway, because a quote never includes slippage.

## What the measurement found

**IG is not viable for this strategy**, and the API had nothing to do with
it.

| | CFD | Spread bet |
| --- | --- | --- |
| Spread (AAPL) | 2.6 bp | 12.2 bp |
| Commission | **£11.12 per side** | none |
| Minimum exposure | ~£310 | **£7,490** |

The CFD commission is a $15 minimum charged both ways — **222 bp round trip
on £1,000**, against a ~6bp budget, and it only falls to 4bp at a position of
£55,600. Spread betting has no commission but its spread is three times the
break-even and its minimum bet is 3.7× the intended account.

Trading 212 wins because it charges no commission at all. At 22-hour holds
that is worth far more than its expensive financing costs — which is why
carry is 12% of cost here and not the 72% it was on daily bars.

## Traps

- Switching accounts with `PUT /session` **reissues the session tokens**.
  Carry them forward or everything after fails with
  `error.security.account-token-invalid`.
- A daily funded bet needs `expiry: "DFB"`, not `"-"`. Getting it wrong is
  accepted and *then* rejected as
  `REJECT_CFD_ORDER_ON_SPREADBET_ACCOUNT`, which reads like an account fault
  and is a malformed order.
- IG's demo rate limits are strict enough that a dozen quick calls earn a
  401 indistinguishable from an auth failure.

## Mutations checked

| | |
| --- | --- |
| M177 | A dealing endpoint added to the read-only client |
| M178 | Any `l`-word treated as live |
| M179 | The API key put back in the repr |
| M180 | Spread compared in points across instruments of different price |
| M181 | Instruments that failed to read dropped from the table |
| M182 | A tradeable market with no prices returning an empty error |
| M183 | Dealing defaulting on; the gate opening on any value |
| M184 | Live permitted on one switch |
| M185 | A deal reference reported as a fill |
| M187 | An opened-but-not-closed probe reported quietly |
| M188 | The rejection reason discarded |
| M189 | The spread-bet expiry hardcoded back |

## What it writes

Nothing. Both modules are pure network clients returning values; callers
decide what to record.
