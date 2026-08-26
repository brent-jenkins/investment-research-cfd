# `universe/` — which instruments to trade

## The failure this prevents

The universe was assembled by hand. Its stated basis — *"liquidity,
volatility and sector spread, not for having gone up or down"* — was
recorded in a commit message rather than executed as a rule, so it could not
be re-run, could not be audited, and could not be checked for drift.

That was tolerable for a list set once. It stops being tolerable the moment
the list is refreshed monthly or quarterly, because every refresh carries the
same gap — and unconscious selection bias is precisely the thing you cannot
detect without a rule to compare against.

Tracing the one revision that did happen makes the point. On 2026-08-25,
PYPL, DIS and BA went out; COST, AVGO, NFLX, ADBE and ORCL came in. Checked
afterwards, ADBE was **down 51% over two years** when it was added, which is
fairly decisive against return-chasing. But over five years the removals
averaged about −38% and the additions about +179%. Neither reading can be
confirmed, because there was no rule to check the decision against. That
ambiguity is the defect.

## Judgement calls

**Returns are not an input.** Ranking on recent performance is a momentum
overlay: legitimate, possibly good, and a *strategy* decision that must be
measured rather than smuggled in through the universe — where nobody sees
it, because a universe looks like a list of names rather than like a rule.

**Liquidity is priced at the decision date, not along the path.** The first
version used the median of the daily turnover series, so a stock that
doubled through the window had doubling turnover through it and the median
rewarded the rise: 0.92 against 0.07 with identical share volume and
volatility. Median share volume times the closing price on the last bar
depends on where an instrument is, not on how it travelled there.

**What is *not* claimed:** that liquidity is uncorrelated with having gone
up. It plainly is — large liquid companies became large by rising, and
dollar turnover is the right measure of whether a position can be exited.
That correlation is the price of measuring tradability properly. What is
excluded is the avoidable part: rewarding the move itself.

**The candidate pool is the remaining judgement call, and it is not hidden.**
Selecting mechanically from a hand-drawn pool moves the bias rather than
removing it. What it buys is that the *selection within* the pool is
reproducible, and that the pool is one visible list somebody can argue with
rather than nineteen decisions nobody wrote down.

**Weights.** Liquidity 0.5 — an instrument you cannot exit at a sane price is
not tradable whatever else is true of it. Volatility 0.35 — a trend system
needs something that moves, and this is not a risk penalty. Continuity 0.15 —
a disqualifier more than a preference.

**Rank-based scores, not raw z-scores**, so one outlier cannot own the
ranking. The cost is that with very few candidates a rank turns any
difference at all into a full separation; tests assert on the metrics rather
than the scores where that matters.

## Mutations checked

| | |
| --- | --- |
| M171 | The screen peeks past `as_of` |
| M172 | Liquidity priced along the path again, so returns reach the rank |
| M173 | Too little history scored zero instead of left unranked |
| M174 | The sector cap silently skipped when no map is supplied |
| M175 | The snapshot records only the winners |
| M175b | The snapshot rewrites instead of appending |

## What it writes

`<account root>/universe/selections.jsonl`, append-only. Each row holds the
decision date, what was selected, what the sector cap displaced, any notes,
and **every candidate with the numbers it was judged on** — because being
able to re-run the rule is not the same as being able to see what it saw.
Recomputing a ranking later reads a lake that has moved, and would quietly
justify whatever was chosen.
