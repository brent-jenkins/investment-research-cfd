# `advisor/` — what to do now, and the record of what was said

## The failure this prevents

Everything else in this project measures backwards. That is the right way to
find a defect and a poor way to judge a strategy: the universe was assembled
in 2026 from instruments that are large now, so a backtest over it is partly
measuring survivorship, and several of the inputs worth testing have no
history to test against at all.

This layer exists to produce a record that **cannot be overfitted**: what the
system committed to, written down before the outcome was known.

## What is in here

`actions.py` — the live suggestions: what clears the confidence bar now, at
what size, and what open positions are worth watching.

`alerts.py` — what is *newly* worth saying. Re-sending an unchanged
suggestion every five minutes is how the one that matters gets filtered into
a folder nobody opens.

`forward.py` — the prediction and the state it was made in, recorded before
the outcome exists.

`sentiment.py` — a news reading that decides nothing, recorded so the
question can be answered later.

`livecheck.py` — the last look before a trade opens.

## The last look

The engine decides on the last bar that *closed*. The fill happens now. At a
US session open those are a night apart, and the price being filled at
already contains everything that happened in between. The decision is not
wrong; it is answering a question about a world that has since changed.

So before anything opens, the signal is recomputed with the current price
standing in as the bar in progress, and the trade proceeds only if the
direction still matches and the confidence still clears the bar.

Over 2,139 round trips that check would have refused 197, 9.2% of them.
The refused won **36.0%** and averaged **-0.323%**; the kept won 50.4% and
averaged +0.005% (t=+4.18, p=0.000038).

Those are the figures with exchange rates applied. An earlier version of
this section quoted t=+4.63 from a run in which every dollar instrument was
marked at a rate of 1.0, because the conversion pairs were named in the rate
map and missing from the lake. The finding survived the correction; the
session-open rule measured beside it did not, and was turned off.

**What is deliberately not checked is the size of the move.** A rule
refusing entries because the price has run against the signal would remove
the best cohort measured, not the worst: past 0.25% adverse those trips win
57.7%, against 41.2% for entries that chased a favourable move. Getting a
better price than the signal was struck at is an advantage. What matters is
whether the signal still stands, which is why this recomputes it rather than
measuring a distance.

Judgement calls:

**Nothing is reimplemented.** It runs the same preprocessing, regime and
signal code the pipeline uses, over the same lake, with one provisional bar
appended. A second copy of the trend blend would drift from the first, and
the day it drifted the check would refuse trades the engine had every reason
to take.

**A check that could not run is not a check that passed.** An unreadable
lake, a vendor timeout, an instrument missing from the result — all refuse,
and the reason lands in the skipped counts. A gate that opens when it breaks
is not a gate.

**Once per pass, and only when something would open.** It rebuilds the
panels, so per instrument it would cost twenty times what it is worth, and
on most passes there is nothing to check at all.

**The provisional bar never rewrites the lake.** It is a guess about a bar
that has not finished, held for the length of one check. A quote older than
the newest stored bar is discarded rather than appended, because a vendor
returning a stale price is a thing that happens.

**`hold_across_session_open` is forced off inside the check.** That rule
carries the previous bar's stance across a session's first bar — and the
provisional bar *is* a session's first bar, so leaving it on would hand the
stale signal back and agree with itself every time.

One consequence worth knowing: a signal sitting exactly on the entry bar
will often be refused, because a provisional bar with little movement
dilutes the momentum component. CAT at 0.358 against a 0.35 bar goes flat on
a bar that did nothing. That is the conservative direction and arguably the
point — a signal that only clears the bar on stale data is not clearing it
now — but it means live refusals will run above the measured 9.5%. Every
refusal is recorded with its reason, so the real rate will be known within
days rather than argued about.

## The paper book is gone

Removed in full on 2026-08-30 — `papertrade.py`, `papertrade_run.py`, the
`paper-book` job, all eight arms, the store and every page that read it.
**The Capital.com demo account is the forward record.** It ran a different
rule set from the engine, and `trade-capital` mirrored it, so the account
traded rules no backtest described; `tests/test_threshold_ownership.py` now
fails the build if either module comes back. The reasoning is in CLAUDE.md.

Risk snapshots written before that date still carry `paper:<arm>` source
labels. Three are on disk. Nothing writes new ones.

## Judgement calls

**The book is a simulation and must never become the thing that places
orders.** Its entire worth is being a record nobody could have edited once
the outcome was known. It imports no broker, and a test enforces it.

**Costs are the real ones.** Financing per side per instrument, the spread
crossed on entry, the conversion fee on the result. A simulation ignoring
costs would show the strategy in profit and prove nothing — the backtest's
central finding was that costs were 10.9× the edge before they were fixed.
The demo account now settles this directly: real fills, real spreads, real
financing, confirmed by a counterparty with no stake in the answer.

**A win rate is withheld below twenty closed trades.** The arithmetic is
still shown, greyed, with how far there is to go: hiding it invites the same
sum done worse by hand. What is withheld is the *claim*. A −5.53% that
looked like a finding on fifteen ticker-months inverted to +2.37% on four
hundred and fifty-six.

**A close carries the state the position was opened on.** Otherwise the
outcome and the evidence it was predicted from sit in separate rows, and
every question needs a join to ask — which is exactly the step that quietly
drops whatever fails to match.

**ATR is recorded, not acted on.** The stake stays fixed so the only thing
varying between trades is whether the call was right. Recording volatility
means a weighted book can be reconstructed from the same file afterwards
without running the weeks again.

## What was measured, and what was not

Direction has proved unpredictable from every non-price source tried:
sentiment, EPS surprise, 8-K item codes, filing circumstance, sustained
narrative, continuation, contrarian extremes, and price-derived features.
Each measured as a **risk** signal or as nothing. Volume against its own
normal reached t=+39 against next-day range and said nothing about
direction.

The only directional predictor found is the trend engine's own confidence
(t=+7.43 across 2,018 trips).

Sentiment is the one candidate not made of price, and it could not be tested
historically — two years of verified headlines overlapped 37 round trips. It
is recorded here so that stops being true.

## Mutations checked

| | |
| --- | --- |
| M115 | The stake read as margin, so leverage sizes the position |
| M116 | A trade opened below the broker's minimum |
| M117 | The entry spread recorded and then forgotten at the close |
| M118 | Financing dropped on the close |
| M119 | Two opens read as two positions |
| M120 | A position limit nobody asked for |
| M121 | A second position stacked on the first |
| M126 | The decision recorded without the evidence around it |
| M127 | One cohort's position blocking another's |
| M128 | The stake sized as if the instrument were priced in sterling |
| M129 | An instrument with no published spread charged nothing |
| M131 | A part-finished session read as a quiet market |
| M132 | One financing rate used for both directions |
| M133 | Margin defaulted rather than read from the recorded table |
| M134 | A resize event, which Trading 212 cannot perform |
| M135 | Why it was opened and why it was closed sharing one field |
| M136 | The entry state dropped from the close |
| M141 | Three thresholds averaged into one verdict |
| M167 | An email about a decision the book never made |
| M168 | Meaning that lives only in the markup |
| M169 | Twenty-six performance emails a session |
| M191 | A forward record validating a strategy nobody trades |
| M227 | The recorded signal opened with no re-evaluation |
| M228 | A direction that flipped overnight still traded |
| M229 | A signal that no longer clears the bar still traded |
| M230 | A check that could not run treated as approval |
| M231 | The re-evaluation run once per instrument, not once per pass |
| M232 | The check paid for when nothing would open |
| M233 | A stale quote rewriting a real bar |
| M234 | The last look inheriting the session-open rule |

## What it writes

`<account root>/forward/observations.jsonl`, append-only. A prediction that
can be edited after the fact is not a prediction.

It used to write `<account root>/paper/book.jsonl` as well. Nothing has
written that file since 2026-08-30 and no module reads it.
