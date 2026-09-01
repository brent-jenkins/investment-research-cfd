"""The universe screen — what the rule would pick, and what is traded.

The screen has run monthly since 2026-08-27 and been visible to nobody. It
appends a selection to a store and prints to a terminal, so a month where
three names drifted out of the rule's answer was recorded and then sat
there. An alarm that rings where no one is standing is not an alarm.

**Nothing here changes anything.** Editing the traded universe stays a human
act at the command line, and `config.toml` says so: the screen "reports the
difference and records it, and never edits this file". That is the right
arrangement and this page does not soften it -- there is no button, because
the moment there is one the question stops being "should this change?" and
becomes "why not, it is only a click".

The reason to be slow about it is measured. Consecutive screens correlate at
+0.97 to +0.99 and the selection moves by one or two names a month, so
almost every difference this page shows is noise wearing the clothes of a
signal. The pool is also drawn with hindsight -- US large caps that are
large *now* -- so the screen is reproducible while the thing it screens is
not. Drift is worth seeing and rarely worth acting on.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parents[3])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from cfdinvest.config import load_config  # noqa: E402
from cfdinvest.ui import components  # noqa: E402

components.page_setup(
    st,
    "Universe",
    icon="🔭",
    subtitle="What the screening rule would pick, beside what is actually "
    "traded. Read-only: changing the universe stays a human act.",
)

settings = load_config()

# Read from the owner, not from the config. `market.tickers` is the FALLBACK
# and stopped being the answer on 2026-08-29, when `follow_screen` was turned
# on and the daemon began trading the screen's latest recorded selection.
# This page went on showing the fallback and describing it as the source, so
# it named eight instruments while the account held ten -- LLY and ISRG were
# being traded and did not appear here at all.
#
# `universe.traded` is the single owner of that question and reports which
# source answered, how old it is, and whether it is stale, which is exactly
# what a reader needs to tell "the screen chose this" from "the screen has
# not run and this is the fallback".
try:
    from cfdinvest.universe.traded import traded_universe

    _traded = traded_universe(settings)
    traded = list(_traded.names or ())
    traded_source = _traded.source
    traded_as_of = getattr(_traded, "as_of", None)
    traded_stale = bool(getattr(_traded, "stale", False))
except Exception as error:  # noqa: BLE001 - a view must not break the page
    traded = list(settings.market.tickers or ())
    traded_source, traded_as_of, traded_stale = "unreadable", None, False
    st.warning(f"The traded universe could not be resolved: {error}")

try:
    from cfdinvest.universe.screen import load_screens

    screens = load_screens(settings)
except Exception as error:  # noqa: BLE001
    screens = []
    st.warning(f"The selection store could not be read: {error}")

# --- how many, and why ------------------------------------------------------ #
#
# The count used to be however many names somebody had typed into
# `market.tickers`. It is computed now, and a computed number that nobody
# can see is worse than a typed one -- so this says what it is, what it was
# derived from, and what it declined to take.
st.subheader("How many it holds")
try:
    from cfdinvest.risk.capacity import NOISE_FLOOR_PP, operating_size
    from cfdinvest.universe.traded import resolve_target

    curve = {int(k): float(v)
             for k, v in (getattr(settings.universe, "size_curve", {}) or {}).items()}
    computed = resolve_target(settings, len(traded))
    explicit = getattr(settings.universe, "target", None)
    preference = str(getattr(settings.universe, "size_preference", "largest"))

    row = st.columns(3)
    row[0].metric("Instruments held", computed)
    row[1].metric("Chosen by", "a person" if explicit else f"the curve ({preference})")
    row[2].metric("Noise floor", f"{NOISE_FLOOR_PP:g}pp")

    if explicit:
        st.caption(
            f"Pinned to {explicit} by `universe.target`. Unset it to let the "
            "measurement decide."
        )
    elif curve:
        answer = operating_size(curve, prefer=preference)
        peak = max(curve, key=lambda k: curve[k])
        st.caption(
            f"Derived from a measured curve of return against size, read "
            f"through a noise floor of {NOISE_FLOOR_PP:g}pp rather than by "
            f"taking its best point. {answer.note}. The peak is {peak} at "
            f"{curve[peak]:.1f}%, and it is **not** taken on its own: "
            f"changing only the day of the month a screen runs on moves the "
            f"same strategy by {NOISE_FLOOR_PP:g}pp, so sizes within that of "
            f"each other are indistinguishable and `{preference}` decides "
            f"between them."
        )
        st.caption(
            "This is also the open-position cap, deliberately the same "
            "number: every name eligible but unheld is a candidate to rotate "
            "into, and each rotation pays a round trip."
        )
        import pandas as _pd

        frame = _pd.DataFrame(
            {"universe size": list(curve), "measured CAGR %": list(curve.values())}
        ).set_index("universe size")
        frame["within the floor of the best"] = [
            "yes" if max(curve.values()) - v <= NOISE_FLOOR_PP else ""
            for v in curve.values()
        ]
        frame["chosen"] = ["<-" if k == computed else "" for k in curve]
        st.dataframe(frame, width="stretch")
    else:
        st.warning(
            f"No size curve is configured, so the count falls back to the "
            f"traded list ({len(traded)}). That is a fallback, not a "
            "measurement -- run the size sweep and record it."
        )
except Exception as error:  # noqa: BLE001 - a view must not break the page
    st.warning(f"The traded count could not be explained: {error}")

# --- what is traded now ---------------------------------------------------- #
st.subheader("Traded")
if traded_source == "config":
    st.caption(
        "From `market.tickers`, the fallback — either `follow_screen` is off "
        "or the screen has recorded nothing usable. Changing it is a human "
        "act at the command line."
    )
elif traded_source == "unreadable":
    st.caption(
        "The source could not be read, so this is `market.tickers` shown as "
        "a last resort. Treat it as unverified rather than as what is traded."
    )
else:
    st.caption(
        "From the **screen's daily selection**"
        + (f", recorded {traded_as_of}" if traded_as_of else "")
        + ". Configuration is never rewritten — the selection is recorded as "
        "an observation and read back, so `market.tickers` stays exactly as a "
        "person left it and remains the fallback."
    )
if traded_stale:
    st.warning(
        "That selection is stale. The daemon refuses a selection it cannot "
        "trust rather than trading a list chosen weeks ago."
    )
st.write(", ".join(traded) if traded else "—")

# Shown together deliberately: the gap between them is the thing worth
# seeing, and this page previously showed only the second while calling it
# the first.
configured = list(settings.market.tickers or ())
if traded_source not in ("config", "unreadable") and set(configured) != set(traded):
    st.caption(
        f"`market.tickers` currently holds {len(configured)} name(s) — "
        + (", ".join(configured) if configured else "none")
        + " — and is not what is being traded."
    )

if not screens:
    # Absent is not the same as agreeing. A page that rendered an empty
    # drift table would read as "nothing has moved", which is a claim
    # nobody has made.
    st.info(
        "No screen has been recorded yet, so there is nothing to compare "
        "against. The monthly job runs on the first at 03:10 UTC, or "
        "`cfdinvest-universe screen` runs it now."
    )
    st.stop()

latest = screens[-1]
selected = list(latest.get("selected") or ())

# --- the rule's answer against the traded list ----------------------------- #
st.subheader("Drift")
joined = [t for t in selected if t not in traded]
left = [t for t in traded if t not in selected]

columns = st.columns(3)
columns[0].metric("Screened", len(selected))
columns[1].metric("Would join", len(joined))
columns[2].metric("Would leave", len(left))

if not joined and not left:
    st.success("The rule's answer matches what is traded.")
else:
    st.caption(
        f"As of {str(latest.get('as_of') or 'an unrecorded date')[:19]}. "
        "One or two names a month is the normal amount of movement; "
        "consecutive screens correlate at +0.97 to +0.99, so a small "
        "difference is usually noise rather than news."
    )
    st.dataframe(
        pd.DataFrame(
            [{"instrument": t, "difference": "would join"} for t in joined]
            + [{"instrument": t, "difference": "would leave"} for t in left]
        ),
        hide_index=True,
        width="stretch",
    )

capped = list(latest.get("sector_capped") or ())
if capped:
    st.caption(
        "Held back by the sector cap rather than by rank: "
        + ", ".join(capped)
    )

for note in latest.get("notes") or ():
    st.caption(str(note))

# --- the history ------------------------------------------------------------ #
st.subheader("Recorded screens")
st.caption(
    "Append-only. A selection is a decision taken on evidence that has since "
    "moved, and one that can be edited afterwards is evidence of nothing."
)

rows = []
previous: set[str] = set()
for row in screens:
    names = set(row.get("selected") or ())
    rows.append({
        "as of": str(row.get("as_of") or "?")[:19],
        "selected": len(names),
        "joined": len(names - previous) if previous else 0,
        "left": len(previous - names) if previous else 0,
        "vs traded": len(names.symmetric_difference(traded)),
    })
    previous = names

st.dataframe(
    pd.DataFrame(rows[::-1]), hide_index=True, width="stretch"
)

st.caption(
    "The pool is US large caps that are large *now*, so it is drawn with "
    "hindsight: screening within it is reproducible, the pool itself is not "
    "a measurement. See `cfdinvest/universe/README.md`."
)
