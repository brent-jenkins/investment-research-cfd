# Meta-strategy

The risk posture: how hard to push right now, and every reason for it.

```python
from cfdinvest.metastrategy import compute_meta_strategy

profile = compute_meta_strategy(insights, bias, regime, pnl, "live")
profile.posture          # "assertive"
profile.contributions    # {"expectancy": 1.0, "drawdown": 0.5, ...}
profile.score            # they sum to this
```

Also a page in the dashboard: **Meta-Strategy**.

---

## This is the first layer that tries to increase risk

Everything else in this project reduces, reports, or refuses. That difference
changes what counts as enough evidence, so it is worth saying out loud.

**Cutting requires one reason. Raising requires all of them.**

Any single adverse finding — a drawdown, a broken correlation, an asserted
behavioural pattern — holds the posture back. Moving forward needs *all* of:

- an established positive expectancy, not merely a positive one
- no asserted behavioural pattern
- no elevated market regime
- a drawdown under 5%

Miss one and the posture stays neutral, and the notes say which.

That is not caution for its own sake. **Cutting size wrongly costs money;
raising size wrongly can remove you from the game.** Those are different kinds
of error, and the evidence available here — expectancy over a few dozen
trades, a classifier's label — is far better at telling you when to hold back
than when to push.

---

## The ceiling

**A posture may ask for more size. It cannot create the room for it.**

`sizing_multiplier` above 1.0 is applied to the refinement's multiplier, and
the result is then capped at the quantity the safety engine already approved:

```
The opportunistic posture asked for more than the safe size of 0.3607 and
was held there. A posture can use headroom the safety engine granted; it
cannot create any.
```

The overrule is **reported**, not absorbed. A posture applied and refused
reads very differently from one that never asked, and silently absorbing it
would make the posture look like it had been applied.

Where no safety-approved maximum is available to cap against, that is said
too — silence there would read as safe.

---

## The tolerances can only tighten

`safety_tolerance` and `margin_tolerance` multiply your **configured** limits
and are clamped at `1.0` in the profile's own constructor. A posture can hold
you further inside a limit and can never move one outward.

"Relaxed" in a finding means *back toward the limit you set*:

> Meta-strategy relaxed margin tolerance to your configured limit. It cannot
> go past it.

A tolerance that could exceed a configured limit would be a way around it, and
the limits would be advisory.

---

## Exits widen, they do not tighten

A forward posture widens the stop. Pushing size and tightening stops at the
same time compounds two risks while reading like one decision — and the exit
layer re-checks the widened stop against the per-trade risk limit and reports
the size it fits at, which is the pairing that makes it safe.

---

## Real money counts against

A live account reaches a forward posture on strictly stronger evidence than a
simulation does. The inputs are identical; what differs is what a mistake
costs, and that belongs in the decision rather than in a disclaimer.

```
Held at assertive rather than opportunistic because this is the real-money
account. The evidence is identical; what a mistake costs is not.
```

---

## Deterministic and decomposable

The same inputs give the same posture. Every input's effect is a signed
contribution, and they sum to the score the posture band was read from:

```
expectancy +1.00 · drawdown +0.50 · regime +0.50 · behaviour +0.25
                                                    -> score +2.25
```

Adding them up by hand is the point. A score you cannot decompose is a number
nobody can argue with.

The posture settings are a written-down table rather than a formula, so a
posture is a name for a set of numbers rather than a mood — and the mapping
can be disagreed with.

---

## Two things it will not do

**Behaviour only ever counts against.** There is no pattern whose presence
argues for a larger position, and a system that found one would be telling
someone their mood was a reason to trade bigger.

**Nothing measurable gives neutral, not conservative.** With nothing to go on,
pushing and holding back are equally unjustified. Neutral is not a hedge — it
is the only claim the evidence supports.

---

## A note on dead code

An earlier version had a separate low-confidence guard that re-checked the
posture after the fact. Mutation testing showed it was unreachable: every path
producing low confidence also closes the gate, because an unestablished
expectancy is itself an unmet requirement.

It was removed rather than kept. Code that cannot run cannot be trusted to,
and the coupling it was defending is now asserted by a test — so if someone
loosens the expectancy gate, that is what notices.

---

## Layout

| Module | What it does |
| --- | --- |
| `profile.py` | The posture and its numbers, and the tolerance clamp. |
| `engine.py` | The decision, and every contribution to it. Pure. |
| `cfdinvest/livehelper/refine_meta.py` | Applying it — and the ceiling. |
| `cfdinvest/simulate/meta.py` | The posture journal for a rehearsal. |
| `cfdinvest/persist/metastrategy.py` | Append-only profiles on disk. |

The profile fills `cfdinvest.livehelper.context.MetaStrategyPlan`, the contract
the refinement layer was written against before this layer existed. One shape,
filled here and read there.

Expectancy comes from `cfdinvest.insights` — one expectancy in the project,
not two.

---

## The simulation journal

A single posture applied to a whole run tells you little; it is one number
multiplied through. What is worth having is the sequence — when the posture
moved and what moved it.

```
neutral -> assertive -> opportunistic -> assertive -> conservative
```

`drifted_forward` is called out on purpose: a posture that only ratchets one
way is following the record rather than responding to it.

---

## Tests

```bash
python -m pytest tests/metastrategy/ -q
```

59 tests. They pin:

- **A forward posture is capped at the safe size**, and the overrule reported.
- **A raise with no ceiling available says so.**
- **A tolerance above 1.0 is clamped**, and no posture ships one.
- **Any single adverse finding stops a forward posture** — four cases.
- **A high score is still gated**: the gate is not a modifier.
- **A positive but unestablished expectancy does not open the gate.**
- **Low confidence and an open gate cannot coexist** — the coupling that
  replaced the dead guard.
- **Behaviour only ever counts against.**
- **The contributions add up to the score.**
- **A forward posture widens the stop rather than tightening it.**

Mutation-checked: removing the safety cap, dropping the tolerance clamp,
turning the forward gate into an `or`, letting a live account be
opportunistic, making behaviour count in favour, tightening stops on a forward
posture, and loosening the expectancy gate each make the suite fail.
