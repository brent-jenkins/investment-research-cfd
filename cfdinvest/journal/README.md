# Journal

What you did, and what you knew when you did it.

```python
from cfdinvest.journal import write_journal, load_entries, render_journal

write_journal(config)                       # composes whatever is new
print(render_journal(load_entries(config)))
```

Also a page in the dashboard: **Journal**.

---

## The rule this layer is built on

**An entry is written from what was known at the time, and never rewritten.**

Regenerating a narrative later, with the outcome in hand, turns it into a
story about the result rather than the decision. "I was right to take it
because it won" is exactly the lesson a journal must not teach — a good
decision that lost and a bad one that won look identical in hindsight, and
telling them apart is the only reason to keep one.

So an entry is composed once and stored. When the outcome arrives it is
**appended as a separate entry**:

```
2026-08-24  trade    Opened long SPX500, 0.6800 units at 4,436.0000.
2026-08-29  outcome  Closed SPX500 at 4,480.0000. Realised 142.50.
```

The narrative keeps saying what you believed. The outcome says what happened.
The reader puts them side by side and does the work.

Nothing in the journal is editable, including your own notes. A diary that
agrees with whatever you now think is not evidence.

---

## It recomputes nothing

No P&L is calculated, no size is checked, no verdict is reached. Every figure
is read from an artefact another layer wrote, and the only thing added is
English.

That is enforced: a test parses every module here and fails the build if one
imports `cfdinvest.analytics`, `cfdinvest.backtest`, the safety engine, the
sizing engine or `cfdinvest.execution`. What the market was doing is asked of
`cfdinvest.risk`, not decided again.

---

## Three lenses, kept apart

```
Execution: Filled 1.2000 worse than planned — 3% of the stop distance.
           Took 97% of the recommended size.
Market:    Volatility high, combined crisis. Elevated: volatility.
You:       Took it with a BORDERLINE verdict standing.
```

Evidence about **you**, about the **fill**, and about the **market** lead to
different corrections. Run together in one paragraph, they invite attributing
the first to the second — which is the most comfortable mistake available and
the one a journal exists to prevent.

The narrative itself passes no judgement. Calling a trade good or bad at the
moment of writing would be doing the reader's job, with less information than
the reader will have.

---

## What changed since the last entry

The one thing a journal can say that no single artefact can:

```
Since the last entry:
  - Volatility regime moved from low to high.
  - Verdict moved from SAFE to UNSAFE.
  - 9 day(s) since the last entry.
```

A regime that turned last Tuesday is invisible in today's snapshot and obvious
in the difference between two. A quiet period says so explicitly rather than
printing nothing — an empty section reads as "not checked".

---

## Two details that matter

**An assessment made *after* the fill is never attached.** A verdict reached
later is a different thought, and putting it in the entry would credit the
decision with information it did not have.

**The writer is idempotent.** One entry per confirmation, keyed on the
instrument and the moment. Without that the journal would grow by the size of
your history on every run, and the entry written a month ago would be replaced
by one written with a month of hindsight.

---

## Trades you declined

`cfdinvest-confirm ignore GLD --notes "spread too wide"` becomes an entry of
kind `considered`. The trades you did not take are half the record, and they
are the half that is otherwise lost completely.

---

## Layout

| Module | What it does |
| --- | --- |
| `entry.py` | The entry, and every sentence in it. Pure. |
| `writer.py` | Reads the artefacts, composes what is new, appends it. |
| `reader.py` | Filtering and rendering. No statistics — that is the insights layer. |
| `digest.py` | The journal as an email. |
| `cfdinvest/persist/journal.py` | Append-only JSON Lines. |

---

## Tests

```bash
python -m pytest tests/test_journal.py -q
```

55 tests. They pin:

- **Writing an outcome does not touch the entry that opened the position.**
- **An assessment after the fill is not attached** to the decision.
- **A confirmation already journalled is skipped.**
- **A truncated line loses one entry, not the journal.**
- **An absent behavioural layer is named**, never assumed clean.
- **The narrative passes no judgement** — checked word by word.
- **The journal imports nothing that could compute.**

Mutation-checked: re-journalling every confirmation, attaching a later
assessment, filing an outcome as a trade, letting a bad line raise, and
dropping the "no behavioural profile" line each make the suite fail.
