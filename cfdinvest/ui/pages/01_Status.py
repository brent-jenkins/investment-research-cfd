"""Status — lake, scheduler, market, errors and every health check."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parents[3])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from cfdinvest.ui import components, explain  # noqa: E402

data = components.page_setup(
    st,
    "System status",
    icon="🩺",
    subtitle="Whether the data, the scheduler and the market are in the "
    "state the rest of this dashboard assumes.",
)
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
    # Five cards, five different facts. This row used to lead with a
    # timestamp to the second -- the bars are daily, so the clock was always
    # midnight and carried no information -- and it collapsed the market and
    # macro counts into one number labelled as though it were all
    # instruments. Splitting them says more in the same space.
    columns = st.columns(5)
    columns[0].metric(
        "Newest bar",
        components.day(metadata["last_updated"]),
        help="The newest observation in the lake, not the time the files "
        "were written. A lake rewritten today with data ending last month "
        "is stale, and reads as stale here.",
    )
    columns[1].metric("Instruments", metadata["market_series"])
    columns[2].metric("Macro series", metadata["macro_series"])
    columns[3].metric(
        "Data quality flags",
        f"{metadata['anomalies_count']:,}",
        help="Bars flagged at ingest and stored **unrepaired** by design. "
        "These are counts of flags raised, not corrections made. The "
        "breakdown is in the Data quality flags tab below.",
    )
    columns[4].metric(
        "Vintage mode",
        explain.vintage(metadata["vintage_mode"]) or components.DASH,
        help="How the macro series were fetched. Publication lag is the "
        "look-ahead-safe choice: each figure is dated when it was published, "
        "not when it describes.",
    )

    calendar_age = metadata["publication_calendar_freshness"]
    st.caption(
        f"`{metadata['data_root']}` — publication calendar "
        + (
            "not built yet"
            if calendar_age is None
            else f"{components.plural(calendar_age, 'day')} old"
        )
        + f", {metadata['publication_calendar_rows']:,} rows."
    )

    tabs = st.tabs(
        ["Market data", "Economic data", "Data quality flags", "Disk space"]
    )
    with tabs[0]:
        components.render_frame(
            st, components.coverage_table(metadata["coverage"]), height=320
        )
    with tabs[1]:
        components.render_frame(
            st, components.coverage_table(metadata["macro_coverage"]), height=320
        )
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

if state.get("derived", True):
    components.render_note(
        st,
        "No scheduler state file yet, so these are **derived from the run "
        "directory**: they say when a run last *completed*, not whether one "
        "started and died. The next scheduled run will record its own state.",
    )
else:
    components.render_note(
        st,
        "Read from the scheduler's own state file. `Last started` is written "
        "before the pipeline runs, so a start with no matching success is a "
        "run that began and did not finish — something the run directory "
        "alone could never show.",
    )

if state.get("incomplete_run"):
    st.error(
        "**A run started and did not finish.** The last start is newer than "
        "the last success, so the pipeline died part-way. Check the error "
        "below and the Logs page."
    )

columns = st.columns(4)
columns[0].metric("Last started", components.clock(state.get("last_run")))
columns[1].metric(
    "Next due",
    components.clock(state.get("next_run")),
)
columns[2].metric(
    "Last succeeded",
    components.clock(state.get("last_success")),
)
with columns[3]:
    components.render_state_card(
        st,
        "Outcome",
        explain.outcome(state.get("last_status")) or components.DASH,
        note="the run's recorded status",
    )

extra = st.columns(3)
extra[0].metric("Time taken", components.duration(state.get("last_duration")))
extra[1].metric("Runs recorded", state.get("recorded_runs", state.get("run_count", 0)))
extra[2].metric("Failures recorded", state.get("recorded_failures", 0))

if state.get("next_run_asset"):
    st.caption(
        f"Next event: {state['next_run_kind']} of `{state['next_run_asset']}`."
    )

if state.get("last_error"):
    when = state.get("last_error_at")
    st.error(
        f"**Last recorded error**"
        f"{f' ({components.short_moment(when)})' if when else ''}: "
        f"{state['last_error']}"
    )
if state.get("state_file"):
    st.caption(f"State file: `{state['state_file']}`")

with st.expander("Run history", expanded=False):
    components.render_frame(st, state.get("history", pd.DataFrame()), height=320)

st.divider()

# --------------------------------------------------------------------------- #
# Operational monitoring
# --------------------------------------------------------------------------- #
st.subheader("Operational monitoring")

try:
    from cfdinvest.persist import monitoring as monitoring_store

    latest_monitor = monitoring_store.load_latest_monitoring_snapshot(data["config"])
except Exception as error:  # noqa: BLE001 - a status page must not go dark
    st.warning(f"Could not read the monitoring history: {error}")
    latest_monitor = None

if latest_monitor is None:
    components.render_note(
        st,
        "No monitoring pass has been recorded. The scheduler files one after "
        "every run — see the **Operational Monitoring** page to run the "
        "checks now.",
    )
else:
    colour = {"green": "#2E7D32", "yellow": "#B26A00", "red": "#C62828"}.get(
        str(latest_monitor.get("overall_status")), "#B26A00"
    )
    st.markdown(
        f'<div style="background:{colour};color:#fff;padding:10px 16px;'
        f'border-radius:8px;font-weight:700;">'
        f"{str(latest_monitor.get('overall_status', '?')).upper()} &nbsp;·&nbsp; "
        f"{latest_monitor.get('headline', '')}</div>",
        unsafe_allow_html=True,
    )
    cells = st.columns(4)
    cells[0].metric("Critical", latest_monitor.get("critical_count", 0))
    cells[1].metric("Warnings", latest_monitor.get("warning_count", 0))
    cells[2].metric("For information", latest_monitor.get("info_count", 0))
    cells[3].metric(
        "Checked", components.clock(latest_monitor.get("timestamp"))
    )

st.divider()

# --------------------------------------------------------------------------- #
# Confirmations
# --------------------------------------------------------------------------- #
st.subheader("Confirmations")

try:
    from cfdinvest.persist import confirm as confirm_store

    confirmations = confirm_store.load_confirmations(data["config"])
except Exception as error:  # noqa: BLE001 - a status page must not go dark
    st.warning(f"Could not read the confirmations: {error}")
    confirmations = []

if not confirmations:
    components.render_note(
        st,
        "Nothing has been confirmed. Every page is showing the **simulated** "
        "positions — which is correct until you say otherwise, but it does "
        "mean the numbers assume the recommendations were followed exactly.",
    )
else:
    latest = confirmations[-1]
    held = {
        name: entry
        for name, entry in confirm_store.confirmed_positions(confirmations).items()
        if entry["quantity"]
    }
    cells = st.columns(4)
    cells[0].metric("Recorded", len(confirmations))
    cells[1].metric("Last confirmed", components.clock(latest.timestamp))
    cells[2].metric("Age", components.age(latest.timestamp, now))
    cells[3].metric("Instruments held", len(held))
    st.caption(
        f"Most recent: {latest.action} {latest.ticker}"
        + (f" at {latest.price:,.4f}" if latest.price is not None else "")
    )

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
            st.write(", ".join(components.day(d) for d in holidays))

st.divider()

# --------------------------------------------------------------------------- #
# Recent errors
# --------------------------------------------------------------------------- #
st.subheader("Recent errors")
errors = data["errors"]

if not components.render_absent(st, errors, "The error log"):
    recent = list(errors.value)[-10:]
    st.code("\n".join(recent) or "(nothing)", language="text")
    st.caption(f"Last {len(recent)} lines. Complete logs are on the Logs page.")
