# Accounts and routing

What accounts exist, what each is for, and which one a trade belongs in.

```python
from cfdinvest.accounts import AccountProfile, load_registry, route_trade

registry = load_registry(config).register(
    AccountProfile(name="momentum", type="strategy", strategy="trend",
                   instruments=("SPX500", "NAS100"))
)
decision = route_trade(intent, registry, strategy="trend")
decision.account       # "momentum"
decision.reasons       # every rule that matched
```

Two pages in the dashboard: **Accounts** and **Routing**.

---

## The registry does not invent a second storage layout

A registered account maps to a directory under the same data root, through the
same `account_root`, with the isolation the virtual/live separation already has
and already tests.

**Registering an account is what makes its directory legal** — nothing else
does. A typo is refused rather than quietly creating a tree nobody will look
in.

### The built-in three are load-bearing

`virtual`, `live` and `simulation` always exist. They can be relabelled,
described, given tighter limits and disabled. They **cannot** be removed or
repointed:

```python
registry.remove("live")
# ValueError: 'live' is a built-in account and cannot be removed

registry.register(AccountProfile(name="live", tree="elsewhere"))
# ValueError: repointing it would make everything written so far unreachable
```

Every path, test and migration assumes them. A registry that could delete
`live` could strand a real trading record.

### A name becomes a path segment

So it is checked at registration — `../escape`, `with space`, an absolute
path. Refusing at write time is too late, because by then something has
already decided where a file goes.

---

## Profiles reference, they do not redefine

An account's risk, sizing and exit settings are **overrides on the existing
config objects** — `RiskLimits`, `LiveHelperConfig`, `ExitRefinementConfig`.
The registry does not become a fourth place a limit can live.

```python
profile = AccountProfile(name="momentum", type="strategy",
                         risk_overrides={"max_total_exposure_gbp": 500.0})
profile.risk_limits().max_total_exposure_gbp        # 500.0
profile.risk_limits().max_effective_leverage       # the shared default
```

An unknown override is dropped with a warning rather than raising: a registry
written against a newer version should not stop an older one starting. An
invalid one falls back to the defaults for the same reason.

---

## The registry is the one store that is rewritten

Everything else in this project is append-only, because it records
observations — a reading, a verdict, a fill — and those describe moments that
do not come back.

The registry is *configuration*. It says what exists now, and a history of
what it used to say would be a second source of truth about which accounts are
legal.

It is written atomically, and **an unreadable registry falls back to the
built-in three** rather than to nothing. A corrupt file must not be able to
make `live` unreachable. One malformed entry is skipped; the others load.

It lives at the data root, not inside an account — one filed inside an account
it describes would be unreadable until you already knew which account to look
in.

---

## Routing: nothing reaches real money by accident

This is the property the router exists to guarantee.

```
Virtual account (virtual)  [low confidence]
  Nothing about this trade matched any account's rules.
  Fell back to Virtual account, which is the safest enabled account.
  Nothing was routed to real money by default.
```

Every other routing mistake is a bookkeeping error you can correct — a trade
filed against the wrong sleeve is fixable. A trade routed to the real-money
account because nothing matched is not, so an unmatched trade goes to the
safest enabled candidate and says that is what happened.

Where `live` is the *only* enabled account it is used, and said loudly:

```
Live account (live)  [low confidence]
  REAL MONEY
  Fell back to Live account, which is the only enabled account and holds
  real money. Confirm before you place anything.
```

---

## The reasons are as much the point as the answer

A router that returned only a name would be impossible to argue with. Six
months later, looking at a trade in the wrong sleeve, you need to know whether
the rule was wrong or the inputs were — and only the reasons distinguish those.

So a decision carries **every rule that matched and every rule that rejected a
candidate**:

```
Momentum (momentum)  [high confidence]
  instrument: SPX500 is in SPX500, NAS100
  strategy: strategy 'trend' matches 'trend'
  Ruled out:
    - calm: no regime is not in risk_on, low
```

### Three details

**A named instrument list is a restriction, not a preference.** An account
that lists `SPX500` does not take `GLD` at a lower score; it does not take it.

**A tie is not a decision.** Where two accounts fit equally the safer one is
taken, the confidence drops to low, and the decision says two fitted. Picking
one alphabetically and presenting it as an answer would be worse than saying
so.

**An explicit choice wins over every rule.** A router that could overrule the
person operating it is a router that gets switched off.

`decision.needs_a_decision` is true for a fall-back, a tie, or real money on a
match that was not clear-cut — not "is this wrong", which the router cannot
know, but "is this one where the machine should not be the last word".

---

## It places nothing

The router reads a trade and a registry and returns a suggestion. It does not
place trades, move money between accounts, or change an account's settings. It
does not even import the persistence layer — a test checks that.

---

## Layout

| Module | What it does |
| --- | --- |
| `registry.py` | Profiles, the registry, and what is legal. Pure. |
| `router.py` | Which account a trade belongs in, and why. Pure. |
| `cfdinvest/persist/accounts.py` | The registry document, rewritten atomically. |

---

## Tests

```bash
python -m pytest tests/test_accounts.py -q
```

50 tests. They pin:

- **The built-ins cannot be removed or repointed**, and can be relabelled.
- **A name that is not a directory name is refused** at registration.
- **Registering is what makes a tree legal** — and only registering.
- **An unreadable registry never hides the built-ins.**
- **An unmatched trade never goes to live.**
- **Live as the only account is used and flagged loudly.**
- **A tie takes the safer account and reports low confidence.**
- **Rejections are recorded, not just the winner.**
- **An override applies to the existing config**, leaving other fields alone.

Mutation-checked: reordering the safety ranking so `live` comes first, calling
a tie high-confidence, allowing a built-in to be removed or repointed,
loosening the name pattern, and dropping the built-ins from a partially-read
registry each make the suite fail.
