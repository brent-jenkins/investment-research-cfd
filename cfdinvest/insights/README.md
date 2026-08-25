# Insights

What the record says once it is sliced — and how much of each slice is worth
believing.

```python
from cfdinvest.insights import run_insights

report = run_insights(config)
print(report.headline)                  # "4 of 9 insight(s) have enough record"
for insight in report.usable:
    print(insight.title, "—", insight.finding)
```

Also a page in the dashboard: **Insights**.

---

## The problem with all nine questions

**Slicing a small record makes it smaller.**

Two hundred trades is a reasonable sample. The same two hundred split across
four regimes and eight instruments is thirty-two buckets averaging six trades
each, and six trades say nothing at all.

The most dangerous thing this layer could do is print a number from six trades
in the same typeface as one from six hundred, because the reader would act on
both. So:

**Every insight carries its sample size, and one below the threshold reports
no finding at all.**

```
● confident      ≥ 30 trades
◐ provisional    10–30 trades
○ insufficient   fewer than 10 — no claim is made
```

The figures stay in `buckets` and `details` for anyone who wants them. What is
withheld is the *claim*, because a claim is what gets acted on.

A withheld claim always says why. "We did not look" and "we looked and found
nothing" are different, and only one of them means carry on.

---

## The second problem, which is subtler

Nine questions asked of the same trades will turn up a striking answer or two
by chance. Nothing here corrects for that arithmetically — the honest
correction is to be conservative and to say so.

Two things follow. **A "best" bucket is never named alone:**

```
Best instrument: GLD (7.49 per trade over 35).
Worst: SPY (-14.00 over 38). Between them: TLT 2.73.
  ⚠ With 3 instruments compared, one of them is the best by construction.
    Treat the ordering as a question rather than a result.
```

With several buckets one of them is the best whatever the data says; printing
only the winner turns an ordering into a discovery. The example above is from
*pure noise* — random P&L with no instrument effect at all — and it still
looks like a finding. That is what the caveat is for.

And where a comparison is confounded, the confound is stated rather than
implied:

> Spread is charged on entry and exit, so a wide-spread third is also a
> more-traded third. The two are hard to separate here.

---

## Nothing is recomputed

Expectancy comes from `cfdinvest.analytics`, round trips from its pairing,
regimes from `cfdinvest.risk`. This layer **groups**: it decides which trades
belong together and asks the existing functions about each group.

A test asserts more than the import — it checks that the expectancy this layer
reports for a frame equals the one `compute_expectancy` gives for the same
frame. A second formula would be a second answer.

---

## The nine

| Insight | What it needs | The trap it avoids |
| --- | --- | --- |
| Expectancy over time | 10 trades each side | A rolling curve invites reading a shape into noise |
| Win/loss distribution | 10 trades | A win rate alone is close to meaningless |
| By regime | Regime **history** | Today's label says nothing about a trade from March |
| By instrument | 10 per instrument | The best of eight is the best by construction |
| Volatility sensitivity | A recorded volatility | Reported absent rather than skipped |
| Spread sensitivity | 30 trades | Wide-spread thirds are also more-traded thirds |
| FX sensitivity | Recorded FX detail | Reported absent rather than skipped |
| Execution quality | 10 confirmations | It measures the fills you *confirmed* |
| Margin behaviour | 3 snapshots | Snapshots miss the moment margin peaked |

Two of these are worth expanding.

**Win rate is never reported alone.** 30% winners at a 4:1 payoff is a good
strategy; 70% at 1:4 is not. Both numbers appear, and the shape gets named:

> Winning often and small while losing rarely and large is the shape that
> looks best and survives worst.

**One dominating loss is called out.** When a single trade is more than 30% of
all losses, the average is describing that trade rather than the strategy, and
the report says so.

---

## Sensitivities are terciles, not correlations

A correlation coefficient between spread and P&L is a number almost nobody can
act on. "The third with the widest spreads made half as much" is.

---

## What the headline counts

The number of insights that could be **supported**, not the number asked:

```
"Nothing can be said yet — the record is too short to slice."
```

Reporting "9 insights" over a record too thin to support one would be the
layer flattering itself. For a new install that sentence is the most useful
thing here.

---

## History

Reports are append-only, one file per pass. A finding that looked strong in
June and has quietly reversed is invisible in the current report and obvious
in the sequence — which is the only reason to keep them.

---

## Layout

| Module | What it does |
| --- | --- |
| `model.py` | The nine, and the sample gate. Pure. |
| `controller.py` | Reads the artefacts, runs them all, assembles the report. |
| `digest.py` | The report as an email, supported findings first. |
| `cfdinvest/persist/insights.py` | Append-only reports on disk. |

One failing insight does not stop the pass, and a crashed one is reported as
"could not be computed" rather than vanishing — one that disappeared would
read as one with nothing to say.

---

## Tests

```bash
python -m pytest tests/test_insights.py -q
```

53 tests. They pin:

- **A thin record makes no claim**, and says why.
- **The numbers survive even when the claim does not.**
- **Naming a best always names the others**, with the by-construction caveat.
- **Trades are judged by the regime they were taken in.**
- **A constant cost has nothing to compare** — reported, not silently skipped.
- **"Nothing confirmed" is not "fills matched the plan".**
- **The headline counts what can be said**, not what was asked.
- **This layer's expectancy equals the analytics layer's** on the same frame.

Mutation-checked: dropping the sample floor, comparing a single bucket, naming
a best without the others, removing the multiple-comparisons caveat, letting a
crashed insight vanish, and counting unsupported insights in the headline each
make the suite fail.
