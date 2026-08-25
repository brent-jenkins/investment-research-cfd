"""Behaviour — patterns in how you have traded.

Called Behaviour rather than Bias on purpose. "Bias" names a conclusion about
a person; what is on this page is a set of counts from a trade record, and
every one of them is consistent with having meant to.

Nothing here diagnoses anyone. Each finding shows what was counted, the
innocent reading of the same numbers, and a change to a process.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parents[3])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from cfdinvest.bias import (  # noqa: E402
    ASSERT_THRESHOLD,
    MINIMUM_OBSERVATIONS,
    run_bias_detection,
)
from cfdinvest.persist import bias as bias_store  # noqa: E402
from cfdinvest.ui import components  # noqa: E402

data = components.page_setup(
    st,
    "Behaviour",
    icon="🧠",
    subtitle="Patterns in how you have traded — described from the record, "
    "not diagnosed. Every one has an innocent reading.",
)
config = data["config"]

# --------------------------------------------------------------------------- #
# Run the detectors
# --------------------------------------------------------------------------- #
try:
    # store=False: opening a page is not an observation. A dashboard left
    # open would otherwise file a report every rerun and make a bad fortnight
    # look like a hundred separate confirmations of it.
    report = run_bias_detection(config, store=False)
except Exception as error:  # noqa: BLE001 - the page must not go dark
    st.error(f"**The behavioural patterns could not be computed.** {error}")
    st.stop()

if not report.measured:
    st.info(f"**{report.headline}**")
elif report.asserted:
    st.warning(f"**{report.headline}**")
else:
    st.success(f"**{report.headline}**")

cells = st.columns(4)
cells[0].metric("Standing out", len(report.asserted))
cells[1].metric("Measured", len(report.measured))
cells[2].metric("Not enough record", len(report.unmeasured))
cells[3].metric("Checked", components.short_moment(report.timestamp))

components.render_note(
    st,
    "These severities are **acted on**: the refinement layer trades smaller "
    f"when one passes {ASSERT_THRESHOLD:.0%}. That is why nothing here is "
    f"asserted below {MINIMUM_OBSERVATIONS} observations — a false positive "
    "does not just read badly, it shrinks your next position.",
)

st.divider()

# --------------------------------------------------------------------------- #
# What stands out
# --------------------------------------------------------------------------- #
st.subheader("Patterns that stand out")

if not report.asserted:
    components.render_note(
        st,
        "Nothing is pronounced enough to act on. Where a pattern could be "
        "measured at all, its figure is below.",
    )

for finding in report.asserted:
    with st.container(border=True):
        header, badge = st.columns([4, 1])
        header.markdown(f"**{finding.title}**")
        badge.markdown(
            f'<div style="text-align:right;">{finding.severity:.0%} · '
            f"n={finding.observations}</div>",
            unsafe_allow_html=True,
        )
        st.progress(min(float(finding.severity), 1.0))

        st.markdown(f"**What was counted.** {finding.evidence}")
        # In the same block as the evidence, deliberately. Separated into an
        # expander, the caveat is the part that gets skipped.
        if finding.innocent_reading:
            st.markdown(f"**The other reading.** {finding.innocent_reading}")
        if finding.action:
            st.info(f"**One thing to change.** {finding.action}")
        st.caption(f"Confidence: {finding.confidence}")

st.divider()

# --------------------------------------------------------------------------- #
# Everything measured
# --------------------------------------------------------------------------- #
st.subheader("Everything that could be measured")

quiet = [f for f in report.measured if not f.asserted]
if quiet:
    components.render_frame(
        st,
        pd.DataFrame(
            [
                {
                    "pattern": finding.title,
                    "severity": finding.severity,
                    "observations": finding.observations,
                    "confidence": finding.confidence,
                    "what was counted": finding.evidence,
                }
                for finding in quiet
            ]
        ),
    )
else:
    components.render_note(st, "Nothing else could be measured.")

st.divider()

# --------------------------------------------------------------------------- #
# What could not be looked at
# --------------------------------------------------------------------------- #
st.subheader("Not enough record to look")

components.render_note(
    st,
    "These could not be measured. That is not the same as finding nothing, "
    "and it is certainly not a clean bill — several of them are only visible "
    "if you record what you did, including the trades you declined.",
)

for finding in report.unmeasured:
    st.markdown(
        f"○ **{finding.title}** — {finding.details.get('reason', 'not measured')}."
    )

st.divider()

# --------------------------------------------------------------------------- #
# How a pattern has moved
# --------------------------------------------------------------------------- #
st.subheader("How a pattern has moved")

components.render_note(
    st,
    "A severity that spiked once and settled is a week you had, not a trait "
    "you have. Only the sequence can tell those apart, and acting on a single "
    "reading is how a bad fortnight becomes a permanently smaller size.",
)

try:
    history = bias_store.load_bias_history(config)
except Exception as error:  # noqa: BLE001
    st.warning(f"Could not read the behavioural history: {error}")
    history = pd.DataFrame()

if history.empty:
    components.render_note(st, "No reports have been recorded yet.")
else:
    keys = sorted(history["key"].dropna().unique())
    chosen = st.selectbox("Pattern", options=keys)
    shown = history[history["key"] == chosen].copy()

    if not shown.empty and shown["severity"].notna().any():
        st.line_chart(
            shown.set_index("timestamp")["severity"], height=200
        )
    components.render_frame(
        st,
        shown[
            [c for c in ("timestamp", "severity", "observations", "confidence",
                         "evidence") if c in shown.columns]
        ].iloc[::-1],
    )
