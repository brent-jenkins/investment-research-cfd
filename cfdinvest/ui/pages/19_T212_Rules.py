"""T212 Rules — what you have recorded, and what is still unverified.

The table is one you fill in by hand from the ticket. Nothing publishes these
numbers and this project makes no broker calls, so what is not here has not
been checked — and this page exists to make that visible rather than leaving
it as a silence.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parents[3])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from cfdinvest.livehelper.t212_checks import CHECKS, NEEDS  # noqa: E402
from cfdinvest.livehelper.t212_rules import (  # noqa: E402
    DEFAULT_RULES_PATH,
    load_rules,
)
from cfdinvest.ui import components  # noqa: E402

data = components.page_setup(
    st,
    "T212 Rules",
    icon="📐",
    subtitle="What Trading 212's ticket will accept — as far as you have "
    "recorded it. What is missing was not checked, not passed.",
)

#: The fields a rule table can carry, in the order the checks ask for them.
FIELDS: tuple[str, ...] = tuple(NEEDS[name] for name, _ in CHECKS)
FIELD_TITLES: dict[str, str] = {NEEDS[name]: title for name, title in CHECKS}

# --------------------------------------------------------------------------- #
# Where it is
# --------------------------------------------------------------------------- #
chosen_path = st.text_input(
    "Rules file",
    value=DEFAULT_RULES_PATH,
    help="TOML or JSON. Optional — without it every broker check reports "
    "itself not consulted.",
)

try:
    table = load_rules(chosen_path)
except Exception as error:  # noqa: BLE001 - the page must not go dark
    st.error(f"**The rules file could not be read.** {error}")
    st.stop()

resolved = Path(table.path or chosen_path).expanduser()
exists = resolved.is_file()

cells = st.columns(4)
cells[0].metric("Instruments recorded", len(table))
cells[1].metric("File present", "yes" if exists else "no")
cells[2].metric(
    "Verified sources",
    len([r for r in table.rules.values() if r.verified]),
)
cells[3].metric(
    "Last modified",
    components.clock(
        pd.Timestamp(resolved.stat().st_mtime, unit="s", tz="UTC")
    ) if exists else components.DASH,
)

st.caption(f"Reading `{resolved}`")

if not exists:
    st.info(
        "**No rules file.** That is the normal state, and it is not a fault — "
        "nothing publishes these numbers. Every broker check will report "
        "itself *not consulted*, which is deliberately different from "
        "reporting a pass."
    )
    components.render_note(
        st,
        "Start from `deployment/scripts/t212_rules.example.toml`. A partial "
        "entry is better than none: it makes the checks it covers real and "
        "leaves the rest honestly unchecked.",
    )
    st.stop()

if not len(table):
    st.warning(
        "**The file is there and holds no usable instruments.** Either it is "
        "empty, or every entry was rejected — check the logs for which."
    )

st.divider()

# --------------------------------------------------------------------------- #
# What is recorded
# --------------------------------------------------------------------------- #
st.subheader("What is recorded")

if len(table):
    rows = []
    for ticker in table.tickers:
        rules = table.get(ticker)
        row = {"instrument": ticker, "source": rules.source}
        for field in FIELDS:
            row[FIELD_TITLES[field]] = getattr(rules, field, None)
        row["fields recorded"] = f"{len(rules.known)} of {len(FIELDS)}"
        rows.append(row)

    components.render_frame(st, pd.DataFrame(rows))

    components.render_note(
        st,
        "A blank is **not checked**, never zero and never a default. There is "
        "no typical minimum size and no usual leverage — an invented value "
        "would be a number that looks measured, is not, and gets trusted "
        "because it sits beside numbers that are.",
    )

st.divider()

# --------------------------------------------------------------------------- #
# What is missing
# --------------------------------------------------------------------------- #
st.subheader("What is still unverified")

if len(table):
    gaps = []
    for ticker in table.tickers:
        rules = table.get(ticker)
        missing = [
            FIELD_TITLES[field]
            for field in FIELDS
            if getattr(rules, field, None) is None
        ]
        if missing:
            gaps.append(
                {
                    "instrument": ticker,
                    "recorded": f"{len(rules.known)} of {len(FIELDS)}",
                    "not checked": ", ".join(missing),
                }
            )

    if gaps:
        components.render_frame(st, pd.DataFrame(gaps))
    else:
        st.success("Every recorded instrument has every field.")

# The instruments the pipeline actually trades, against what is recorded.
universe = list(data.get("universe") or [])
if universe:
    unverified = [ticker for ticker in universe if ticker not in table]
    covered = len(universe) - len(unverified)

    cells = st.columns(2)
    cells[0].metric("Traded instruments recorded", f"{covered} of {len(universe)}")
    cells[1].metric("Unverified", len(unverified))

    if unverified:
        st.warning(
            f"**Not in the table at all:** {', '.join(unverified)}. These are "
            "*unverified* — the checks did not run, which is not the same as "
            "the trade passing them."
        )
    else:
        st.success("Every instrument in the universe has an entry.")

st.divider()

# --------------------------------------------------------------------------- #
# Which sources you can point at
# --------------------------------------------------------------------------- #
st.subheader("Where the numbers came from")

components.render_note(
    st,
    "`source` is whatever you wrote, and nothing checks it. That is the "
    "point: it is a note to your future self, so a transcribed ticket can be "
    "told from a guess made in a hurry.",
)

if len(table):
    unverified_source = [
        ticker for ticker in table.tickers if not table.get(ticker).verified
    ]
    if unverified_source:
        st.info(
            f"**No source recorded for:** {', '.join(unverified_source)}. The "
            "rules are still applied — this is about being able to check them "
            "later, not about trusting them now."
        )
    else:
        st.success("Every entry says where its numbers came from.")

with st.expander("The file, as read"):
    st.code(resolved.read_text(encoding="utf-8")[:8000], language="toml")
