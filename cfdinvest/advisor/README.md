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

`papertrade.py` / `papertrade_run.py` — the forward book. Four arms, real
costs, append-only.

`forward.py` — the prediction and the state it was made in, recorded before
the outcome exists.

`sentiment.py` — a news reading that decides nothing, recorded so the
question can be answered later.

## The paper book

Four arms run side by side on the same signals and prices, each taking
**£1,000 of exposure** — notional, not margin.

Fixed notional deliberately. With a fixed *margin* instead, leverage would
vary the stake without anyone choosing it: a 30× FX pair would carry £30,000
of exposure against a 5× share's £5,000, and the currency trades would
dominate the result on position size alone. What it does not equalise is
volatility, so the percentage move is recorded beside the pounds.

The baseline arm is **derived from `strategy.entry_confidence`**, not
hardcoded. It used to be hardcoded, and the arms sat at 0.50/0.60/0.70 while
the engine entered at 0.35 — so every signal between the two was traded by
the backtested strategy and by no arm at all, and the one measurement that
cannot be overfitted was validating a configuration returning 86% a year
against the shipped 127%.

Each arm keeps its own one-position-per-instrument rule and its own
cooldown. The arms of an experiment must not see each other: a position held
in the 0.50 book says nothing about whether the 0.70 book should hold one.

**Only `open` and `close` events exist.** Trading 212 cannot resize an open
CFD position, so neither can the simulation — a book that trimmed a winner
would be recording a trade the account could not have made, and every cost
after that point would belong to a position that never existed.

## Judgement calls

**The book is a simulation and must never become the thing that places
orders.** Its entire worth is being a record nobody could have edited once
the outcome was known. It imports no broker, and a test enforces it.

**Costs are the real ones.** Financing per side per instrument from the
recorded Trading 212 table, the spread crossed on entry, the 0.5% conversion
fee on the result. A paper book ignoring costs would show the strategy in
profit and prove nothing — the backtest's central finding was that costs
were 10.9× the edge before they were fixed.

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
| M122 | The cooldown ignored on the paper book |
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

## What it writes

`<account root>/paper/book.jsonl` and `<account root>/forward/observations.jsonl`,
both append-only. A prediction that can be edited after the fact is not a
prediction.
