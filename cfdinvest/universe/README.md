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

## The runner

```bash
cfdinvest-universe screen            # rank, report the difference, record
cfdinvest-universe screen --as-of 2026-06-30 --no-record
cfdinvest-universe history
cfdinvest-universe drift
```

Plus a monthly job in the daemon, on the first at 03:10 UTC. Monthly because
the answer barely moves: across twenty-five monthly screens the selection
changed by one or two names a month and consecutive rankings correlated at
+0.97 to +0.99.

Until 2026-08-27 there was no runner at all. Every screen this project had
run was a script written for the occasion and then deleted, so the rule was
reproducible in principle and, in practice, only by whoever still had the
file — the same defect the screen exists to fix, one level up. And
`record_screen` had been written and never called, so the store held nothing
to compare against.

**It never edits the configuration.** The screen ranks; a person decides. A
scheduled job that rebalanced the universe on a ranking never shown to
predict return would be acting on far more confidence than the evidence
carries — and a test asserts the module cannot write a config at all.

**The sector cap now binds, and says so.** Every screen before the map
existed reported "no sector map supplied, so the sector cap was not
applied". The first run with one displaced sixteen instruments, nine of them
currently traded. A constraint that strong must never arrive quietly, so the
command names what it displaced and why.

## Mutations checked

| | |
| --- | --- |
| M171 | The screen peeks past `as_of` |
| M172 | Liquidity priced along the path again, so returns reach the rank |
| M173 | Too little history scored zero instead of left unranked |
| M174 | The sector cap silently skipped when no map is supplied |
| M175 | The snapshot records only the winners |
| M175b | The snapshot rewrites instead of appending |
| M260 | The runner edits the universe it is meant to report on |
| M261 | A screen that failed to record reporting success |
| M262 | The pool losing the instruments actually traded |
| M263 | The sector cap binding silently |
| M264 | The monthly screen owed every day |
| M265 | A selection recorded with no decision date |
| M266 | The profile demotion applied before a score exists to demote |
| M267 | The demotion multiplier a no-op |
| M268 | The profile quantiles formed from too few names |

## The profile demotion, and how it nearly passed as free

The screen halves the score of a name that is at once volatile, expensive
and mean-reverting — top quartile on volatility and on spread, bottom
quartile on variance ratio, all judged against the pool that day rather
than against fixed numbers, because spreads and volatility move together
across the whole market.

Choppiness is a **variance ratio**, not the more obvious net-move over
distance-travelled. Path efficiency needs a cumulative return, and a
cumulative return encodes which way the instrument went; a screen that
ranks on that quietly becomes "hold whatever went up". A variance ratio is
built only from dispersions, so it is blind to sign. `M172` and
`test_the_screen_reads_no_return_series_at_all` guard this, and the first
implementation tripped them — correctly.

Worth recording, because it is the more useful failure. The rule was first
applied by multiplying `Candidate.score` *before* the scoring pass ran. It
halved `None`, and the scoring pass then overwrote the result outright. It
reported its flags perfectly the whole time, so every diagnostic agreed it
was working, and a 43-month backtest of it returned a change of exactly
zero. That zero was written up as "the rule costs nothing" — an
interpretation which is what a genuinely harmless rule looks like, and also
what a rule that never executes looks like. The two were told apart only by
asking where flagged names *ranked*, and finding INTC at first place in a
month the rule claimed to have flagged it.

Once it ran: 72.6% → 75.5% CAGR, −34.3% → −33.1% drawdown, both halves
improving. Ten changed selections in forty-three months, all of them COIN,
INTC or PLTR. **A thin result** — three names, a profile designed after
watching INTC misbehave, so not an out-of-sample test of the idea.

The strength is not tuned and cannot be. Swept from 0.75 to 0.0, every
value gives an identical result: with a hundred candidates the scores pack
tightly enough that even a quarter off the score displaces a flagged name
from the top eight, and nothing weaker keeps it. At this pool size a
demotion and a refusal are the same policy. It stays a multiplier for the
case where they stop being the same — a smaller or more thinly scored pool,
where a name far enough ahead should still be able to win despite the
profile.

## What it writes

`<account root>/universe/selections.jsonl`, append-only. Each row holds the
decision date, what was selected, what the sector cap displaced, any notes,
and **every candidate with the numbers it was judged on** — because being
able to re-run the rule is not the same as being able to see what it saw.
Recomputing a ranking later reads a lake that has moved, and would quietly
justify whatever was chosen.

## Following the screen

`universe.follow_screen` (default **off**) lets the daemon trade the
screen's latest recorded selection instead of `market.tickers`.

The constraint that shaped this is *no config mutation*, so the traded list
is never rewritten. `market.tickers` stays exactly as a person left it and
is the fallback; the selection is read from `selections.jsonl`, the
append-only record the screen already kept. Configuration is rewritten,
observations are appended — the setting reads an observation and leaves the
configuration alone.

`traded.py` is the single owner. `market.tickers` was read directly in
seven places in the scheduler, and following the screen in six of them
would have been indistinguishable from following it in all seven until
something went wrong; a test asserts no direct read survives.

Rank inside the selection is deliberately *not* used for anything. Measured
over 3.65 years, ranks 1–4 earned £476,774 and ranks 5–8 earned £484,668,
with no trend between them — the screen decides membership well and orders
it not at all.

Four judgement calls worth naming:

- **A stale selection is used, and said out loud.** Falling back to the
  configured list is the tempting design and the wrong one: a scheduler
  outage would swap the whole book back, closing and opening eight
  positions for a reason unrelated to any instrument. An old decision is
  still a decision. Only silence about its age is unacceptable.
- **An empty selection row is skipped, not obeyed.** A screen that chose
  nothing is a failed screen, not an instruction to hold nothing.
- **An unreadable store falls back rather than emptying the book.** "Trade
  nothing" and "the record could not be read" are different statements and
  only one is an instruction.
- **Absent is not empty.** `_open_universe` answers `None` for nothing
  configured (permissive) and `frozenset()` for a configured empty list
  (permits nothing). The resolver returns a tuple either way, so consulting
  it unconditionally flattened the first into the second — silently
  stopping the book from opening anything. Caught by the existing suite,
  which is what that distinction is there for.

| | |
| --- | --- |
| M269 | An empty selection row obeyed instead of skipped |
| M270 | An unreadable selection store emptying the book |
| M271 | A stale selection discarded for the configured list |
| M272 | A scheduler path reading `market.tickers` directly |
| M273 | The book opening in the old universe while the daemon follows the screen |
| M274 | Absence flattened into emptiness in `_open_universe` |
