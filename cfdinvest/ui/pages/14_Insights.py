"""Insights — what the record says, and how much of it is worth believing.

Every figure here is a slice of one record. The findings that have enough
trades behind them come first; the questions that could not be answered come
last, under a heading that says so. Burying the sample warnings would turn a
report of two findings and seven absences into what looks like nine findings.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parents[3])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from cfdinvest.insights import CONFIDENT_SAMPLE, MINIMUM_SAMPLE, run_insights  # noqa: E402
from cfdinvest.persist import insights as insight_store  # noqa: E402
from cfdinvest.ui import components  # noqa: E402

data = components.page_setup(
    st,
    "Insights",
    icon="🔎",
    subtitle="What the record says once it is sliced — and how much of each "
    "slice is worth believing.",
)
config = data["config"]

CONFIDENCE_ICON = {
    "confident": "●", "provisional": "◐", "insufficient": "○",
}
CONFIDENCE_HELP = {
    "confident": f"At least {CONFIDENT_SAMPLE} trades behind it.",
    "provisional": f"Between {MINIMUM_SAMPLE} and {CONFIDENT_SAMPLE} trades.",
    "insufficient": f"Fewer than {MINIMUM_SAMPLE} trades — no claim is made.",
}

# --------------------------------------------------------------------------- #
# Build the report
# --------------------------------------------------------------------------- #
try:
    # store=False: opening a page is not an observation worth filing, and a
    # dashboard left open would bury the reports the scheduler took.
    report = run_insights(config, store=False)
except Exception as error:  # noqa: BLE001 - the page must not go dark
    st.error(f"**The insights could not be built.** {error}")
    st.stop()

said = report.usable
unsaid = report.insufficient

if not said:
    st.info(f"**{report.headline}**")
else:
    st.success(f"**{report.headline}**")

cells = st.columns(4)
cells[0].metric("Supported", len(said))
cells[1].metric("Not enough record", len(unsaid))
cells[2].metric("Trades read", report.details.get("trades_available", 0))
cells[3].metric("Checked", components.clock(report.timestamp))

components.render_note(
    st,
    "Nine questions asked of the same trades will turn up a striking answer "
    "or two by chance. Where several groups are compared, one of them is the "
    "best by construction — treat an ordering as a question rather than a "
    "result.",
)

st.divider()

# --------------------------------------------------------------------------- #
# What the record supports
# --------------------------------------------------------------------------- #
st.subheader("What the record supports")

if not said:
    components.render_note(
        st,
        "Nothing yet. Every insight here needs a run of completed trades "
        f"behind it — at least {MINIMUM_SAMPLE} in any group before a claim "
        "is made about that group. That is not a fault; it is what a short "
        "record honestly supports.",
    )

for insight in said:
    with st.container(border=True):
        header, badge = st.columns([4, 1])
        header.markdown(f"**{insight.title}**")
        badge.markdown(
            f'<div style="text-align:right;" title="'
            f'{CONFIDENCE_HELP[insight.confidence]}">'
            f"{CONFIDENCE_ICON[insight.confidence]} {insight.confidence} "
            f"(n={insight.trades})</div>",
            unsafe_allow_html=True,
        )
        st.markdown(insight.finding)

        for caveat in insight.caveats:
            st.caption(f"⚠ {caveat}")

        if insight.buckets:
            frame = pd.DataFrame(
                [
                    {
                        "group": bucket.label,
                        "trades": bucket.trades,
                        "expectancy": bucket.expectancy,
                        "total": bucket.total,
                        "win rate": bucket.win_rate,
                        "enough to judge": "yes" if bucket.enough else "no",
                    }
                    for bucket in insight.buckets
                ]
            )
            with st.expander("The groups behind it"):
                components.render_frame(st, frame)

st.divider()

# --------------------------------------------------------------------------- #
# What could not be answered
# --------------------------------------------------------------------------- #
st.subheader("Not enough record yet")

components.render_note(
    st,
    "These were asked and could not be answered. That is different from being "
    "answered with nothing — and different again from there being no problem.",
)

for insight in unsaid:
    with st.container(border=True):
        st.markdown(f"○ **{insight.title}**")
        for caveat in insight.caveats:
            st.caption(caveat)

st.divider()

# --------------------------------------------------------------------------- #
# How a finding has moved
# --------------------------------------------------------------------------- #
st.subheader("How findings have moved")

try:
    history = insight_store.load_insight_history(config)
except Exception as error:  # noqa: BLE001
    st.warning(f"Could not read the insight history: {error}")
    history = pd.DataFrame()

if history.empty:
    components.render_note(
        st,
        "No reports have been recorded. A finding that looked strong in June "
        "and has since reversed is invisible in the current report and "
        "obvious in the sequence — which is what the history is for.",
    )
else:
    keys = sorted(history["key"].dropna().unique())
    chosen = st.selectbox("Insight", options=keys)
    shown = history[history["key"] == chosen]

    components.render_frame(
        st,
        shown[["timestamp", "confidence", "trades", "finding"]].iloc[::-1],
    )
