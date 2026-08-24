"""Status — lake, scheduler, market, errors and every health check."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parents[3])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from cfdinvest.ui import components  # noqa: E402

data = components.page_setup(st, "Status", icon="🩺")
now = data["now"]

# --------------------------------------------------------------------------- #
# Critical warnings, expanded
# --------------------------------------------------------------------------- #
st.subheader("Health checks")
health = data["health"]

if health.triggered:
    for check in health.triggered:
        with st.expander(
            f"{'🔴' if check.severity == 'critical' else '🟠'}  {check.title}",
            expanded=check.severity == "critical",
        ):
            st.markdown(f"**What was found:** {check.detail}")
            if check.remedy:
                st.markdown(f"**What to do:** {check.remedy}")
else:
    st.success("Every check passing.")

with st.expander("All checks", expanded=not health.triggered):
    components.render_frame(st, health.to_frame())

st.divider()

# --------------------------------------------------------------------------- #
# Lake
# --------------------------------------------------------------------------- #
st.subheader("Lake")
lake = data["lake"]

if not components.render_absent(st, lake, "Lake metadata"):
    metadata = lake.value
    columns = st.columns(5)
    columns[0].metric("Last updated", components.age(metadata["last_updated"], now))
    columns[1].metric("Series", metadata["series_count"])
    columns[2].metric("Anomalies", metadata["anomalies_count"])
    columns[3].metric(
        "Pub. calendar",
        components.DASH
        if metadata["publication_calendar_freshness"] is None
        else f"{metadata['publication_calendar_freshness']:.0f} d",
    )
    columns[4].metric("Vintage mode", metadata["vintage_mode"] or components.DASH)

    st.caption(
        f"`{metadata['data_root']}` — {metadata['market_series']} market, "
        f"{metadata['macro_series']} macro. Freshness is the newest "
        "observation across every series, not a file timestamp: a lake "
        "rewritten today with data ending last month is stale."
    )

    tabs = st.tabs(["Market coverage", "Macro coverage", "Anomalies", "Disk"])
    with tabs[0]:
        components.render_frame(st, metadata["coverage"], height=320)
    with tabs[1]:
        components.render_frame(st, metadata["macro_coverage"], height=320)
    with tabs[2]:
        anomalies = metadata["anomalies"]
        if isinstance(anomalies, pd.DataFrame) and not anomalies.empty:
            components.render_note(
                st,
                "Anomalous bars are stored **unrepaired** by design. These "
                "are counts of flags raised at ingest, not corrections.",
            )
        components.render_frame(st, anomalies, height=320)
    with tabs[3]:
        disk = data["disk"]
        if not components.render_absent(st, disk, "Disk usage"):
            usage = disk.value
            free, total = st.columns(2)
            free.metric("Free", f"{usage['free_gb']:.1f} GB")
            total.metric("Free share", components.percent(usage["free_fraction"]))
            st.caption(f"Volume holding `{usage['path']}`")

st.divider()

# --------------------------------------------------------------------------- #
# Scheduler
# --------------------------------------------------------------------------- #
st.subheader("Scheduler")
scheduler = data["scheduler"]
state = scheduler.value or {}

components.render_note(
    st,
    "The scheduler keeps **no state file**, so these are read from the run "
    "directory rather than from the scheduler process. This says when a run "
    "last *completed*; it cannot say whether a scheduler is alive right now. "
    "A dead one shows up as a last run that keeps getting older.",
)

columns = st.columns(4)
columns[0].metric("Last run", components.age(state.get("last_run"), now))
columns[1].metric(
    "Next due",
    components.DASH
    if state.get("next_run") is None
    else components.moment(state["next_run"]),
)
columns[2].metric(
    "Last duration",
    components.DASH
    if state.get("last_duration") is None
    else f"{float(state['last_duration']):.1f} s",
)
columns[3].metric(
    "Exit code",
    components.DASH
    if state.get("last_exit_code") is None
    else str(state["last_exit_code"]),
)

if state.get("next_run_asset"):
    st.caption(
        f"Next event: {state['next_run_kind']} of "
        f"`{state['next_run_asset']}`. Exit code is derived from the run's "
        "recorded status — the process's real exit code is not stored."
    )

if state.get("last_error"):
    st.error(state["last_error"])

with st.expander("Run history", expanded=False):
    components.render_frame(st, state.get("history", pd.DataFrame()), height=320)

st.divider()

# --------------------------------------------------------------------------- #
# Market
# --------------------------------------------------------------------------- #
st.subheader("Market")
market = data["market"]

if not components.render_absent(st, market, "Market status"):
    sessions = market.value["sessions"]
    open_count = int(sessions["is_open"].sum()) if "is_open" in sessions else 0
    left, right = st.columns([1, 3])
    left.metric("Open now", f"{open_count} / {len(sessions)}")
    with right:
        components.render_frame(st, sessions, height=260)

    holidays = market.value.get("holidays") or []
    if holidays:
        with st.expander(f"US market holidays ({now.year})", expanded=False):
            st.write(", ".join(str(pd.Timestamp(d).date()) for d in holidays))

st.divider()

# --------------------------------------------------------------------------- #
# Recent errors
# --------------------------------------------------------------------------- #
st.subheader("Recent errors")
errors = data["errors"]

if not components.render_absent(st, errors, "The error log"):
    recent = list(errors.value)[-10:]
    st.code("\n".join(recent) or "(nothing)", language="text")
    st.caption(f"Last {len(recent)} lines. Full logs on the Logs page.")
