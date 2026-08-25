# Simulation

What would have happened had you followed the advice — including every time it
said to trade smaller.

```python
from cfdinvest.simulate import run_simulation

run = run_simulation(config, helper_config=limits)
print(run.headline)
# "Following the advice would have ended with £1,142.25 less than the run
#  as executed."
```

Also a page in the dashboard: **Simulation**.

---

## It is not a second backtester

`cfdinvest.execution` already fills orders, charges spread and slippage,
converts currencies, applies financing and resolves stops intrabar. Writing
any of that again would give the project two fill models and two answers, and
the day they disagreed neither would be believable.

So this calls `run_backtest` unchanged. What it adds is the layer above: **the
decisions**. The pipeline produces a signal and a size; in reality you then run
the helper over it, and the helper often says trade smaller. This replays the
record with those decisions applied.

**The comparison is the output.** A single simulated equity curve is another
backtest. The same run with and without the advice measures the advice.

---

## What it will not simulate

**How your psychology would have evolved.** A trade record cannot support a
model of a person changing over time, and anything produced would be a
plausible-looking invention.

What is done instead is narrower and real: the **existing** bias detectors run
over the simulated record, answering "would this strategy, executed this way,
have triggered them?" That is a fact about the strategy, not a forecast about
you, and it is said in those words everywhere it is reported.

---

## The honest limit

Some execution settings — stops, hysteresis, FX — are written to the run
metadata flattened, and cannot be rebuilt into the nested config the engine
wants. They fall back to defaults.

Both curves share whatever settings *were* rebuilt, so comparing them with each
other is sound. Comparing either with the original run is not, and the
rehearsal says so when the gap is material:

> The rebuilt baseline ends −2,395.39 from what the run recorded
> (99,023.39). Some execution settings are not recoverable from the run
> metadata, so the two curves below are comparable with each other but not
> with the original run.

Without that line you could not tell how much of a difference was the advice
and how much was the rebuild.

---

## One conversion that decides whether this means anything

The signal `size` is a **share of equity**; the helper's limits are in units
and money. The engine turns size into notional with `exposure_per_unit_size`
and then into units with the price, and the rehearsal has to do the same
before asking the helper anything.

Skipping it was the difference between a rehearsal that adjusted four
positions by 0.1% and one that adjusted 217 of them meaningfully.

---

## Forced closures are reported, not applied

The execution engine caps leverage; it does not model a margin call. This finds
the bars where usage would have crossed the platform's own ratio — the point
at which Trading 212 closes positions without warning and picks which.

They are **reported rather than applied**. Rewinding the simulation to close a
position mid-path would change every subsequent bar, and a rehearsal that
quietly diverged from the run it is being compared against measures nothing.
What you get is the list of moments the comparison stopped being valid.

Without a recorded instrument leverage there is no margin figure, and an
assumed one would produce a list of margin calls that never happened — so the
list comes back empty instead.

---

## A rehearsal must never read as a real result

This is the one failure here that nobody recovers from: you do not find out
you were wrong about it. Three defences, and the redundancy is deliberate.

1. **Its own account tree.** `simulation` was added to `ACCOUNTS` so simulated
   artefacts get the isolation the virtual/live separation already provides
   and already tests. It is *not* an `ACCOUNT_MODE` — nothing runs "as
   simulation", so `CFD_ACCOUNT_MODE=simulation` is refused.
2. **Every document is marked.** `simulated: true` is the first key, set by
   the controller and set again by the store, with a disclaimer beside it.
3. **The store cannot be pointed elsewhere.** `write_simulation` takes no
   `account` parameter. A caller that could choose would eventually choose
   wrong.

The dashboard page says it above the numbers rather than below them — a banner
underneath is one people scroll past on the way to the figure they came for.
The email says it in the subject, the first line and the sign-off.

---

## What "skip UNSAFE" is and why it is off

`skip_unsafe` drops any trade the helper would have refused. It is the most
aggressive intervention available and the one most likely to flatter the
advice, because skipping trades during a losing period improves any curve.

`follow_sizing` and `follow_refinement` are separate switches for the same
reason: running both makes it impossible to say which did the work.

---

## Reading the result

On a winning run, following the advice usually ends **lower**. That is not a
failure of the advice — capping exposure reduces the return of a winning run
and the loss of a losing one, which is what it is for. The page says so, and
adds that one run is not evidence about the next.

---

## Layout

| Module | What it does |
| --- | --- |
| `engine.py` | Rewrites the sizes, runs both curves, compares. Pure. |
| `controller.py` | Reads a stored run, rehearses it, files the result. |
| `digest.py` | The rehearsal as an email, marked in three places. |
| `cfdinvest/persist/simulation.py` | Append-only, simulation tree only. |

`rebuild_signal_frame` and `rebuild_analysis_frame` live in
`cfdinvest.persist.readers` — they are the inverse of what the writers do, and
belong with them rather than here.

---

## Tests

```bash
python -m pytest tests/test_simulation.py -q
```

47 tests. They pin:

- **Nothing here prices a fill** — the module is parsed for it.
- **An unstored signal cell rebuilds as flat**, not NaN. NaN would carry a
  position through a bar the strategy had exited: a different strategy, with
  better-looking results.
- **Sizes are converted to units** before being judged.
- **An adjustment only ever reduces.**
- **A material reconstruction gap is always reported** — asserted with a
  figure that guarantees one, rather than hoping the fixture drifts.
- **A simulation never lands where a real run goes**, and the store cannot be
  pointed at one.
- **Forced closures are reported, not applied.**
- **The source run is left untouched.**

Mutation-checked: unmarking a document, removing the store's second marking,
pointing the store at the shared root, dropping the units conversion, letting
flat cells rebuild as NaN, and hiding the reconstruction gap each make the
suite fail.
