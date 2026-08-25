# Trade confirmation

Tell the system what you actually did.

```bash
cfdinvest-confirm open SPX500 --price 4434.8 --quantity 0.684
cfdinvest-confirm close NAS100 --price 18923.4
cfdinvest-confirm ignore GLD --notes "spread too wide"
cfdinvest-confirm list
```

```python
from cfdinvest.confirm import Confirmation, write_confirmation

write_confirmation(
    Confirmation.of("SPX500", "open", direction="long",
                    quantity=0.684, price=4434.8),
    config,
)
```

Also a page in the dashboard: **Confirm Trades**.

---

## Why this exists

Every other layer assumes the recommendation was followed exactly. The engine
sizes a position, the analytics score that position, the risk dashboard
measures it. All of it is one assumption deep.

You trade manually, so the assumption is wrong roughly always — a fill is a
few points off, a size is rounded to what the ticket would take, a
recommendation is skipped because the spread looked wide. None of that is
visible to a system that only ever sees its own output.

A confirmation replaces the assumption with a fact.

---

## It places nothing

There is no broker client, no order endpoint and no automation of the Trading
212 interface. This records that *you* traded. The mail-reply parser reads
text and never fetches mail — reading a mailbox is not SMTP, and the project's
rule is no external API calls except SMTP, so something else has to hand the
parser a body.

---

## Append-only, always

A confirmation is an observation of a moment that does not come back. Nothing
here edits or deletes.

To correct a mistyped fill, record the correction. The original stays, because
it is what you believed at the time — and a system that quietly rewrote it
could not later show you that you had been wrong.

```
confirmations/
    20260825-090000-OPEN-SPX500.json
    20260825-091500-CLOSE-NAS100.json
    20260825-093000-OPEN-SPX500.json   <- the correction
```

The filename says what it is without being opened, and sorts in time order.
Two confirmations for the same instrument and action in the same second get a
suffix rather than one landing on the other.

---

## Four things it is careful about

**Quantity is signed.** Negative is short. `positions.parquet` holds signed
units, so an unsigned confirmed quantity would need a direction inferred from
somewhere — and the only place to infer it from is the simulated position the
confirmation exists to override. `Confirmation.of(...)` takes `direction=` and
applies the sign exactly once.

**Not confirming is not the same as declining.** Silence leaves the simulated
position standing, because nothing has contradicted it. Declining is the
explicit `ignore` action. Collapsing the two would make an unconfirmed week
look like a week of refused trades, and every page would quietly change its
numbers when you went on holiday.

**A bad confirmation is refused at the door.** A fill price of zero, a
negative price, an open with no quantity, a flip missing a leg — all rejected
on the way in. A confirmation overrides the engine, so a fat-fingered price
does not produce a suspicious number downstream. It produces a wrong one that
looks exactly as authoritative as a right one.

**Failing to write raises.** Unlike the rest of the persistence layer, which
logs and carries on. A snapshot that failed to record is a lost reading; a
confirmation that failed to record leaves the system believing the
recommendation was followed. Silence there is worse than an error.

---

## The merge only touches the latest bar

A confirmation says what you hold *now*. It says nothing about what you held
three weeks ago, and back-filling history from it would rewrite a simulation
the engine never ran.

```python
merged, report = merge_simulated_and_confirmed_positions(simulated, confirmations)
report["overridden"]    # {"GLD": {"simulated": 0.0, "confirmed": -1.5}}
report["added"]         # instruments the run never traded
report["unconfirmed"]   # instruments still on the simulation's word
```

The report is returned rather than logged because a silent override is
indistinguishable from a bug — the position changes and nothing says why.

---

## Two files, both true

The runner writes `positions_confirmed.parquet` **alongside**
`positions.parquet`, never over it.

Overwriting would leave the run unable to answer the one question worth asking
of it: how far reality drifted from the simulation. `positions.parquet` stays
the record of what the engine did; the overlay is what actually happened.

The overlay is written **only when something has been confirmed**. An empty
overlay file would be indistinguishable from "everything was confirmed as
simulated", and those are different claims.

Readers prefer the overlay and say which they used:

```python
positions, source = readers.load_positions(run_id, config)
source                                  # "confirmed" or "simulated"
frame.metadata["positions_source"]      # the same, on an analytics result
```

Every metric downstream depends on that choice, so a caller that cannot see it
cannot label its own output honestly. The Positions page prints it in as many
words, and shows what differs.

---

## Replying to an alert

Trade alerts now carry the line to send back:

```
If you took this trade, reply with your real fill so the system stops
assuming this one:
    CONFIRM OPEN SPX500 PRICE=<your fill> QTY=<your size>
```

```bash
cfdinvest-confirm from-reply --file reply.txt --apply
```

The parser is deliberately strict, because an email body is text that arrived
from outside and a reply chain carries the **original alert** — which is a
recommendation, and reads exactly like a confirmation to a relaxed parser.

- The command must start the line. The `^\s{0,3}CONFIRM` anchor is the whole
  defence against `> CONFIRM ...`: the prefix is not whitespace, so it cannot
  match. Loosening it to `\s*` would let a quoted recommendation through.
- **Parsing stops at the quote separator.** Outlook and Apple Mail drop the
  original in below `On … wrote:` or `-----Original Message-----` without
  prefixing a single line of it, so a prefix check alone leaves the hole open.
- Every field is named, unknown fields are refused, a repeated field is
  refused, and anything ambiguous is refused rather than guessed.
- One bad line means **nothing** is written. A body half applied leaves the
  sender believing both lines were recorded.

A refusal costs a retype. A wrong parse silently rewrites the book.

---

## No mental arithmetic

The confirmation page shows every recommended number before the form: size,
entry, stop, take-profit, exposure, leverage, exchange rate, and the costs the
simulation charged. The command line prints them before it writes — the check
is worthless after the write.

The size is scaled to your balance. **The run is not your account:** a
recommended 305 units at a simulated £100,000 is 6.1 units at a real £2,000,
and asking for that division at the moment you are typing a live order is
asking for the mistake. Both figures are shown, with the ratio between them.

Where the scale cannot be worked out it is reported absent, never 1.0 — a
default of 1.0 hands you the simulation's size at a hundredfold gap.

---

## Deviations

Recorded alongside each confirmation, so the drift is measurable later:

```python
confirmation.slippage             # signed against the position
confirmation.quantity_deviation   # 1.0 exactly filled, 0.5 a half fill
deviations(confirmations)         # the table
```

**Slippage is positive when it is worse than recommended**, for a long and a
short alike — a long that paid more and a short that sold lower both read as
positive. One convention, both directions.

The recommendation is stored *on the confirmation* rather than looked up
later, because runs are pruned (`keep_runs`) and a recommendation from five
weeks ago may no longer be on disk when you want to ask how your fills drifted.

---

## Layout

| Module | What it does |
| --- | --- |
| `cfdinvest/persist/confirm.py` | The model, the storage, the replay and the merge |
| `cfdinvest/confirm/cli.py` | `cfdinvest-confirm` |
| `cfdinvest/confirm/reply.py` | Reading `CONFIRM` lines out of a mail body |
| `cfdinvest/confirm/recommendation.py` | Every recommended number, gathered and scaled |

Storage sits with the rest of the persistence layer; this package is the
interfaces onto it.

---

## Tests

```bash
python -m pytest tests/test_confirm.py -q
```

86 tests. Beyond the arithmetic, they pin:

- **History is never rewritten** by a merge.
- **An unconfirmed instrument keeps its simulated position.**
- **The simulated artefact survives** a confirmed run, unchanged before the
  last bar.
- **A run with no confirmations writes no overlay.**
- **Nothing below a quote separator is read**, across six client styles.
- **One bad line in a reply writes none of them.**
- **A failed confirmation write raises** rather than returning `None`.
- **The CONFIRM line the alert prints parses back**, so the instruction the
  email gives is one the parser accepts.
- **A confirmation failure cannot fail a completed run.**

The suite was mutation-checked: rewriting all bars instead of the last,
writing an empty overlay, accepting a zero fill price, and loosening the
command anchor each make it fail.
