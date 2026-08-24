# Live-execution helper

Check a Trading 212 CFD trade against your own limits **before** you type it
in. It reads what you intend to do, measures it, and tells you what to do
about it.

```bash
cfdinvest-helper "long SPX500 0.68 entry=4434.8 stop=4390 tp=4520 ccy=USD lev=20" \
    --fx-rate 0.79 --balance 2000
```

```python
from cfdinvest.livehelper import check
print(check("long UKX 0.5 entry=100 stop=96 tp=112 lev=20"))
```

Also a page in the dashboard: **Live-Execution Helper**.

---

## It cannot trade

There is no broker client in this package, no HTTP call, no credential, no
Trading 212 endpoint and no automation of its interface. It reads, measures
and prints.

That is enforced, not promised. `tests/test_livehelper.py` parses every
module here and fails if any of them imports `requests`, `urllib`, `httpx`,
`socket`, `selenium`, `playwright`, `pyautogui`, `webbrowser` or
`subprocess`, or if the string `https://` or `trading212.com` appears
anywhere in it.

---

## The one rule that matters

**Anything that cannot be computed caps the verdict at `BORDERLINE`.**

A missing stop, an unknown exchange rate, an unpublished leverage — none of
these are reasons to say `SAFE`. A helper that grades an unpriceable trade
green has done something worse than stay silent, because a green label
launders a decision nobody actually checked.

| Verdict | Meaning |
| --- | --- |
| `SAFE` | Inside every limit, and everything could be checked |
| `BORDERLINE` | Inside the hard limits, but something is close to one **or could not be checked** |
| `UNSAFE` | Outside at least one limit |

The command line returns the verdict as an exit code — `0`, `1`, `2` — so a
wrapper can act on it. A trade that could not be *parsed* exits `3`, which is
deliberately not one of the verdicts: it has not been judged.

---

## What it checks

| Check | Fires when |
| --- | --- |
| Stop-loss present | There is no stop — the loss is *unbounded*, not large |
| Stop on the correct side | Below a long, above a short. A stop on the wrong side fires immediately |
| Risk to stop | Above `max_risk_per_trade_gbp` |
| Exposure | Above `max_exposure_per_instrument_gbp` |
| Effective leverage | Above `max_leverage_per_trade` |
| Margin after the trade | Above `max_margin_usage_pct_after_trade` |
| Currency exposure | Above `max_fx_exposure_pct`, on a foreign instrument |
| Volatility | Above your threshold — a flag, never a block |
| Spread | Above your threshold **as a share of the stop distance** |
| Close-only | The instrument will reject an opening order |
| Financing rollover | Opening now pays a full night for minutes of exposure |

Two of those deserve their reasoning spelled out.

**Risk is not exposure.** Exposure is what you hold; risk is what you lose if
the stop is reached. A £2,400 position with a 1% stop risks £24. Sizing on
exposure alone is how people take a position that is fine on paper and
catastrophic when it moves.

**The spread is judged against the stop distance, not on its own.** A
two-point spread means nothing in isolation. Against a ten-point stop it is a
fifth of the risk budget spent before the trade has moved.

---

## Two things are called leverage

Conflating them makes the advice wrong, so they are kept apart:

- **Instrument leverage** is fixed by Trading 212 per instrument. No change of
  size touches it. It is what divides exposure into margin.
- **Effective leverage** is exposure over account balance — what this trade
  does to *your* account. A smaller position reduces it.

The limit is checked against effective leverage, because that is the one you
can act on. "Reduce quantity" is a real instruction for one and meaningless
for the other.

Trading 212 does not publish a machine-readable leverage table, so if you
don't pass one, margin is reported as **not computable** rather than guessed.

---

## Sizing

The safe size is a **minimum across ceilings**, not a formula. Each limit
implies a maximum quantity; the recommendation is the smallest — and the
useful output is *which limit produced it*, because that is the one to change
if the size is too small to be worth trading.

Two decisions worth knowing:

**Rounding is always down.** A size rounded up to the nearest tick can breach
the limit it was computed from, which would make the sizer the cause of the
problem it exists to prevent.

**It targets the borderline threshold, not the hard limit.** A quantity
sitting exactly on a limit is flagged `BORDERLINE` by the safety check — so
sizing to the limit would mean the helper's own recommendation never came
back clean. Advice that fails the adviser's test is not advice. A test
re-runs the recommended size through the safety check and asserts it comes
back `SAFE`.

When no size clears the limits, it says so and returns zero rather than
quietly suggesting something tiny.

---

## Configuration

```python
LiveHelperConfig(
    account_balance_gbp=2_000.0,
    used_margin_gbp=0.0,              # already committed elsewhere

    max_risk_per_trade_gbp=100.0,
    max_exposure_per_instrument_gbp=2_000.0,
    max_leverage_per_trade=10.0,      # effective
    max_margin_usage_pct_after_trade=0.50,
    max_fx_exposure_pct=0.30,

    volatility_warning_threshold=0.25,
    spread_warning_threshold=0.20,    # as a share of the stop distance

    borderline_fraction=0.80,
)
```

Every field reads from the environment too — `CFD_ACCOUNT_BALANCE`,
`CFD_USED_MARGIN`, `CFD_MAX_RISK_PER_TRADE` and so on — because the balance
changes daily, and editing a file for it invites the number going stale,
which would make every margin figure wrong in the direction that flatters the
trade.

A fraction above 1 is refused with a suggestion: `0.50`, not `50`.

---

## What you type

The parser takes a mapping or a line of text, in the order a person types:

```
long SPX500 0.68 entry=4434.8 stop=4390 tp=4520 ccy=USD lev=20
buy UKX 1 sl=90 tp=110
sell NAS100 0.2 entry=market
```

Synonyms work (`sl`/`stop`/`stop_loss`, `tp`/`target`/`take_profit`,
`qty`/`size`/`quantity`), because a helper that refuses over a synonym is one
nobody uses. Currency symbols and thousands separators are tolerated.

**Anything it cannot read is refused, not guessed.** A misparsed field
silently changes the risk, and a confident wrong number is the worst thing
this tool could produce. One bug found in testing was exactly that: the value
pattern stopped at a comma, so `entry=£1,234.50` parsed as `1.0`.

---

## Where this sits

This package is the **consolidated Trading 212 safety core**. The execution
safety checks, the risk-limit enforcement, the margin warning, the sizing
assistant, the quality score and the go/no-go checklist all live here rather
than in six packages, because they are one operation: measure the trade,
compare each figure with a limit, say what to do.

Six packages would give every threshold several homes and let them disagree
— and a `max_leverage` that means one thing in the sizer and another in the
checker is exactly the failure a safety tool must not have.

`assessment.go` is the checklist's verdict. `assessment.findings` with their
severities are the quality score's breakdown. They are views of one
assessment, not separate computations.

---

## Testing

```bash
pytest tests/test_livehelper.py -q      # 58 tests, no network
```

The tests that matter are the ones about **refusing to say `SAFE`**. A sizing
bug produces a number that is visibly wrong; a verdict bug produces a green
label on a trade nobody checked.

Two real bugs the tests caught:

- **`entry=£1,234.50` parsed as `1.0`** — the value pattern ended at the
  thousands separator. A wrong price rather than a refusal, which is the
  worst outcome available to a parser in a safety tool.
- **The sizer's own recommendation came back `BORDERLINE`**, because it sized
  to the hard limit while the checker flags anything at a limit. Now it
  targets the borderline threshold, and a test re-runs its output through the
  checker.

---

## Known limitations

**No Trading 212 rules table.** Quantity precision, minimum and maximum sizes,
per-instrument leverage and trading hours are all set by the platform and not
published in machine-readable form. Precision defaults to four decimals and
rounds down; leverage and hours must be supplied. Nothing is invented — an
absent value is reported as absent.

**The market state is typed in.** Spread, volatility and the exchange rate
come from you, not from a feed. Every figure is only as good as those inputs,
which is why the summary prints them back and the disclaimer says to check
them against the platform.

**It does not know your open positions.** `used_margin_gbp` is a number you
supply. A trade that is fine on an empty account can be the one that tips a
loaded one over, so it is worth keeping current.
