"""Operational Monitoring — is the machine alright?

Not "are the numbers good" but "can the numbers be trusted at all". A stale
lake still serves prices, a dead scheduler still leaves yesterday's run
sitting there, and every other page in this dashboard will render perfectly
against data that is quietly wrong.

Nothing here runs the pipeline. It runs the checks and shows what they found.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parents[3])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from cfdinvest.monitoring import issues_frame, run_monitoring  # noqa: E402
from cfdinvest.monitoring.aggregate import TYPE_LABELS  # noqa: E402
from cfdinvest.persist import monitoring as monitoring_store  # noqa: E402
from cfdinvest.ui import components  # noqa: E402

data = components.page_setup(
    st,
    "Operational Monitoring",
    icon="🚨",
    subtitle="Whether the system can be trusted, as against whether it is "
    "making money. Checked against what is on disk.",
)
config = data["config"]

#: Deliberately off-brand, for the same reason the critical banner is: a
#: status that shares a hue with the page furniture is one nobody sees.
STATUS_COLOUR = {"green": "#2E7D32", "yellow": "#B26A00", "red": "#C62828"}
SEVERITY_ICON = {"critical": "⛔", "warning": "⚠", "info": "ℹ"}

# --------------------------------------------------------------------------- #
# Run the checks
# --------------------------------------------------------------------------- #
try:
    # store=False: opening a page is not an observation worth filing, and a
    # dashboard left open on a second monitor would otherwise write a snapshot
    # every rerun and bury the ones the scheduler took.
    snapshot = run_monitoring(config, store=False)
except Exception as error:  # noqa: BLE001 - a monitoring page must not go dark
    st.error(f"**The monitoring checks could not be run.** {error}")
    st.stop()

# --------------------------------------------------------------------------- #
# 1. The banner
# --------------------------------------------------------------------------- #
if snapshot.overall_status == "red":
    st.error(
        f"**{snapshot.headline}**\n\n"
        + "\n".join(
            f"- {issue.message}"
            for issue in snapshot.issues
            if issue.severity == "critical"
        )
    )
elif snapshot.overall_status == "yellow":
    st.warning(f"**{snapshot.headline}**")
else:
    st.success("Everything checked is clear.")

st.markdown(
    f'<div style="background:{STATUS_COLOUR[snapshot.overall_status]};color:#fff;'
    f'padding:10px 16px;border-radius:8px;font-weight:700;">'
    f"{snapshot.overall_status.upper()} &nbsp;·&nbsp; "
    f"{snapshot.critical_count} critical, {snapshot.warning_count} warning, "
    f"{snapshot.info_count} for information</div>",
    unsafe_allow_html=True,
)
st.caption(
    f"Checked {components.short_moment(snapshot.timestamp)} · "
    f"run `{snapshot.run_id or components.DASH}`"
)

st.divider()

# --------------------------------------------------------------------------- #
# 2. Every check, including the ones that passed
# --------------------------------------------------------------------------- #
st.subheader("Summary")

components.render_note(
    st,
    "A check that could not run is shown as a warning, not as a pass. "
    "\"No problem found\" and \"no problem could be looked for\" are "
    "different statements.",
)

worst = {}
for issue in snapshot.issues:
    current = worst.get(issue.type)
    if current is None or issue.rank < current.rank:
        worst[issue.type] = issue

cells = st.columns(4)
for index, (kind, label) in enumerate(TYPE_LABELS.items()):
    issue = worst.get(kind)
    cells[index % 4].metric(
        label,
        f"{SEVERITY_ICON[issue.severity]} {issue.severity}" if issue else "✓ clear",
        help=issue.message if issue else "Checked, nothing found.",
    )

st.divider()

# --------------------------------------------------------------------------- #
# 3. The issues
# --------------------------------------------------------------------------- #
st.subheader("What was found")

if not snapshot.issues:
    st.success("Nothing. Every check ran and every check passed.")
else:
    for kind in TYPE_LABELS:
        found = snapshot.of_type(kind)
        if not found:
            continue
        st.markdown(f"**{TYPE_LABELS[kind]}**")
        for issue in found:
            with st.container(border=True):
                st.markdown(
                    f"{SEVERITY_ICON[issue.severity]} "
                    f"**{issue.severity.upper()}** — {issue.message}"
                )
                if issue.remedy:
                    st.caption(issue.remedy)
                if issue.details:
                    with st.expander("What was measured", expanded=False):
                        components.render_kv(st, issue.details)

    with st.expander("As a table", expanded=False):
        components.render_frame(st, issues_frame(snapshot.issues))

st.divider()

# --------------------------------------------------------------------------- #
# 4. The machine
# --------------------------------------------------------------------------- #
st.subheader("The machine")

resources = snapshot.system_resources or {}
measured = tuple(resources.get("measured") or ())

cells = st.columns(4)
cells[0].metric("Disk free", components.number(resources.get("free_gb"), 1) + " GB")
cells[1].metric("Disk total", components.number(resources.get("total_gb"), 1) + " GB")
cells[2].metric(
    "CPU",
    components.percent(
        resources["cpu_percent"] / 100 if resources.get("cpu_percent") is not None
        else None
    ),
)
cells[3].metric(
    "Memory",
    components.percent(
        resources["memory_percent"] / 100
        if resources.get("memory_percent") is not None
        else None
    ),
)

if "cpu" not in measured:
    components.render_note(
        st,
        "CPU and memory were **not measured** — `psutil` is not installed. "
        "Absent is not the same as healthy.",
    )
st.caption(f"Volume: `{resources.get('path', components.DASH)}`")

st.divider()

# --------------------------------------------------------------------------- #
# 5. History
# --------------------------------------------------------------------------- #
st.subheader("Past checks")

try:
    history = monitoring_store.load_monitoring_history(config)
except Exception as error:  # noqa: BLE001
    st.warning(f"Could not read the monitoring history: {error}")
    history = pd.DataFrame()

if history.empty:
    components.render_note(
        st,
        "No monitoring pass has been recorded. The scheduler files one after "
        "every run; opening this page does not, so that a dashboard left open "
        "does not bury the ones that matter.",
    )
else:
    cells = st.columns(3)
    cells[0].metric("Passes recorded", len(history))
    red = int((history["overall_status"] == "red").sum())
    cells[1].metric("Red", red)
    cells[2].metric(
        "Last recorded", components.clock(history["timestamp"].iloc[-1])
    )

    if red and len(history) > 1:
        components.render_note(
            st,
            "A status that turned red an hour ago is a different problem from "
            "one that has been red for a week. That is what the history is for.",
        )

    components.render_frame(
        st,
        history[
            [c for c in ("timestamp", "overall_status", "critical", "warning",
                         "info", "headline", "run_id") if c in history.columns]
        ].iloc[::-1],
    )

    with st.expander("Open a past check", expanded=False):
        options = list(history["path"])[::-1]
        chosen = st.selectbox(
            "Snapshot",
            options=options,
            format_func=lambda p: Path(p).stem,
        )
        document = monitoring_store.load_monitoring_snapshot(chosen)
        if document is None:
            st.warning("That snapshot could not be read.")
        else:
            st.caption(document.get("headline", ""))
            components.render_frame(
                st, pd.DataFrame(document.get("issues") or [])
            )
