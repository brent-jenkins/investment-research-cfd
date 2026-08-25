# Behaviour

Patterns in how you have traded — described from the record, not diagnosed.

```python
from cfdinvest.bias import run_bias_detection

report = run_bias_detection(config)
print(report.headline)
profile = report.to_profile()      # what the refinement layer reads
```

Also a page in the dashboard: **Behaviour**.

---

## What this layer must not become

**It does not tell you what kind of person you are.**

"You are overconfident" is a claim about someone's character made from a few
dozen rows of a spreadsheet. It is the kind of claim people internalise and
then trade around. "Trades taken after a win averaged 2.0 units; after a loss,
0.5 — twice as large, over 59 trades" is the same evidence, stated as
evidence.

Every finding therefore:

- **names the pattern, never the person** — a test parses each one and fails
  the build on words like *impulsive*, *reckless* or *emotional*;
- **carries the innocent reading beside the evidence**, in the same block,
  because separated into an expander the caveat is the part that gets skipped;
- **proposes a change to a process**. "Be less emotional" is not an action.
  "Write the size down before you look at how the last trade ended" is.

---

## Why the sample floor is higher than anywhere else

Fifteen observations, against ten in the insights layer. Two reasons.

**Behavioural inference is weaker evidence than P&L arithmetic.** The same
sequence of sizes is consistent with a rule, a mood and a coincidence, and
nothing in a trade record separates them.

**These severities are acted on.** `cfdinvest.livehelper.refine_sizing` cuts
real position sizes when one passes 0.5. A false positive here does not merely
read badly — it shrinks your next trade. That consequence is what justifies
being slow to assert anything, and a test asserts the connection so it cannot
be forgotten.

An unmeasured pattern stays `None` all the way through to the profile. Never
rounded to zero, because zero reads as "no evidence of this" and that is a
reassurance the record has not earned.

---

## The nine patterns

| Pattern | What is counted | The innocent reading it always carries |
| --- | --- | --- |
| Sizing up after wins | Size after a win vs after a loss | Sizing up while an approach works is a strategy |
| Trading again after a loss | Both quick **and** larger | A loss and the next setup arrive together in a busy session |
| Taking less than the plan | Fills under ¾ of the recommendation | Trading smaller than a model says is often simply correct |
| Declining after losses | Decline rate after losses vs wins | Standing aside after a loss is a recognised discipline |
| Holding losers longer | Bars held, losers vs winners | A wide stop and a near target produce this mechanically |
| Closing winners early | Exit as a share of the target | A target you never reach may be too far away |
| Closing losers late | Exit as a share of the stop | A gap through the stop closes past it with nobody deciding |
| Leverage creep | Leverage across risk snapshots | A rising balance moves leverage the other way |
| Margin already high | Share of checks leaving usage >40% | Running margin high is a choice some strategies require |

Two are worth expanding.

**Trading again after a loss needs *both* conditions.** Trading again soon is
normal in a session; trading again soon *and* a quarter larger than your median
is the pattern. Either alone is not reported.

**Sizing up is measured against the previous outcome, not the average.** A
trader who always trades large is not reacting to anything. The pattern of
interest is a reaction, and only a comparison with the last result finds one.

---

## One finding that deliberately does nothing

Fear-driven under-exposure is detected, reported, and **never used to increase
a position**. The action it offers says so explicitly:

> Nothing here will ever tell you to trade larger. If the recommended size is
> one you would not take, the limit that produced it is the thing to change —
> deliberately, not in the moment.

The refusal is enforced in `refine_sizing` rather than trusted here. A system
that talked a frightened person into a bigger position would be the worst
thing in this repository.

---

## Some patterns are invisible unless you record what you did

Declining a trade leaves no trace at all unless you write it down —
`cfdinvest-confirm ignore GLD --notes "spread too wide"`. Where nothing has
been recorded, the finding says so rather than reporting no declines:

> No declined trades have been recorded. That is not evidence of not
> declining — an unrecorded pass leaves no trace.

---

## It fills a contract that already existed

The output is a `cfdinvest.livehelper.context.BiasProfile` — the shape the
refinement layer was written against before this layer existed. One shape,
filled here and read there, rather than a second profile to keep in step. A
test asserts every key in `BIASES` is a field on that contract.

---

## History matters more here than anywhere

A severity that spiked once and settled is **a week you had, not a trait you
have**. Only the sequence can tell those apart, and acting on a single reading
is how a bad fortnight becomes a permanently smaller position size.

Opening the dashboard page runs the detectors but records nothing — a
dashboard left open would otherwise file a report every rerun and make one bad
fortnight look like a hundred separate confirmations of it.

---

## Layout

| Module | What it does |
| --- | --- |
| `model.py` | The nine detectors, and the sample floor. Pure. |
| `controller.py` | Reads the artefacts, runs them, fills the profile. |
| `digest.py` | The report as an email, caveats beside the evidence. |
| `cfdinvest/persist/bias.py` | Append-only reports on disk. |

One failing detector does not stop the pass, and a crashed one is reported
rather than dropped — a pattern that vanished would read as one with no
evidence for it. Its message says "that is a bug here, not a finding about
you", which is a distinction worth making explicitly.

---

## Tests

```bash
python -m pytest tests/test_bias.py -q
```

68 tests. They pin:

- **No finding describes the person** — parsed word by word.
- **Every asserted finding offers the innocent reading and a process change.**
- **An unmeasured pattern is absent, not zero**, all the way to the profile.
- **The floor is higher than the insights layer's**, asserted against it.
- **A detected pattern reaches the sizing refinement** — the consequence that
  justifies the conservatism.
- **Steady sizing is not a pattern**; only a reaction is.
- **No recorded declines is not evidence of not declining.**

Mutation-checked: defaulting an unmeasured severity to zero, lowering the
floor, removing an innocent reading, suggesting a size increase on
under-exposure, rounding `None` to zero in the profile, and letting a crashed
detector vanish each make the suite fail.
