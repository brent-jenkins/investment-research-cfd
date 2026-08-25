"""System status: is the machine doing its job?

Deliberately narrow. This page answers "is the system healthy" and nothing
else — no coverage tables, no vintage modes, no regime. Market data moved to
the Data page, because a status page that also carries market detail is one
where a red scheduler row sits below a table nobody was reading, and the two
questions are asked at different moments by a different sort of worry.

What each job last did is read from the **artefacts it writes**, not from the
daemon. The daemon's own record lives in memory and dies with the process,
and it is written whether a job succeeded or not; the artefact is only there
if the work actually happened.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parents[3])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from cfdinvest.ui import components, explain, vitals  # noqa: E402

components.render_crumb(st, "Status")
data = components.page_setup(
    st,
    "System status",
    icon="🩺",
    subtitle="Whether the machine is doing its job. Market data lives on the "
    "Data page; this one is vitals only.",
)
now = data["now"]
scheduler = data["scheduler"].value or {}
summary = data["summary"]
record = summary.value or {}
disk = data["disk"]

# --------------------------------------------------------------------------- #
# The header tiles
# --------------------------------------------------------------------------- #
health = data["health"]
if health.show_banner:
    overall, overall_level = "Critical", "critical"
elif health.triggered:
    overall, overall_level = "Warnings", "warning"
else:
    overall, overall_level = "Healthy", "pass"

usage = disk.value if not disk.failed else {}
free_gb = (usage or {}).get("free_gb")
free_share = (usage or {}).get("free_fraction")
# Below a tenth free, a run that writes artefacts is the thing that fills it.
disk_level = (
    "unchecked" if free_share is None
    else "critical" if free_share < 0.05
    else "warning" if free_share < 0.15
    else "pass"
)

last_run = components.clock(scheduler.get("last_run"))
last_success = components.clock(scheduler.get("last_success"))
outcome = explain.outcome(scheduler.get("last_status")) or components.DASH

components.render_tiles(st, [
    ("Overall", overall, f"{len(health.checks) - len(health.triggered)} of "
     f"{len(health.checks)} checks passing", overall_level),
    ("Last run", last_run, components.age(scheduler.get("last_run"), now),
     "pass" if scheduler.get("last_run") else "unchecked"),
    ("Last success", last_success, outcome,
     "pass" if scheduler.get("last_status") == "ok" else "warning"),
])

components.render_tiles(st, [
    ("Disk free", components.DASH if free_gb is None else f"{free_gb:.1f} GB",
     components.percent(free_share) + " free" if free_share is not None else "",
     disk_level),
    ("Bars processed", f"{record.get('bars', 0):,}",
     f"{len(record.get('universe') or [])} instruments", "pass"),
    ("Run duration", components.duration(record.get("duration_seconds")),
     f"run {record.get('run_id', '?')}", "pass"),
])

# --------------------------------------------------------------------------- #
# Scheduler health
# --------------------------------------------------------------------------- #
st.divider()
st.subheader("Scheduler health")

held = components.confirmed_open_count(data)
st.caption(
    "Each job's last run is taken from the artefact it wrote, not from the "
    "daemon's own memory — a record written whether or not the work "
    "succeeded is not evidence that it did."
)

rows = vitals.job_report(
    vitals.gather_evidence(data["config"], account=data.get("account")),
    now=now,
    quotes_expected=held > 0,
)

table = pd.DataFrame([
    {
        "Job": row.name,
        "Schedule": row.schedule,
        "Last run": components.clock(row.last_run),
        "Next due": components.clock(row.next_due),
        "Status": row.status,
        "Detail": row.detail,
    }
    for row in rows
])
components.render_frame(st, table)

overdue = [row for row in rows if row.overdue]
if overdue:
    st.warning(
        "**Behind schedule:** "
        + ", ".join(f"{row.name} — {row.detail}" for row in overdue)
        + "  \nCheck `systemctl status cfdinvest` on the host."
    )
else:
    components.render_note(st, "Every scheduled job is up to date.")

# --------------------------------------------------------------------------- #
# The checks themselves
# --------------------------------------------------------------------------- #
st.divider()
st.subheader("Health checks")

# A failing check is rendered in full, not as a row. What was found and what
# to do are the two things you need, and putting them in a table column means
# reading a truncated cell to discover the machine wants something.
failing = [check for check in health.checks if check.triggered]
for check in failing:
    st.markdown(
        f"**{check.title}** — {check.detail}  \n"
        + (f"*What to do:* {check.remedy}" if check.remedy else "")
    )

if not health.checks:
    components.render_note(st, "No checks ran.")
elif not failing:
    components.render_note(
        st, f"All {len(health.checks)} checks passed."
    )

with st.expander(f"Every check ({len(health.checks)})", expanded=not health.checks):
    components.render_frame(st, pd.DataFrame([
        {
            "Check": check.title,
            "Status": "warning" if check.triggered else "pass",
            "Detail": check.detail,
            "What to do": check.remedy if check.triggered else "",
        }
        for check in health.checks
    ]), empty="No checks ran.")

# --------------------------------------------------------------------------- #
# Recent log
# --------------------------------------------------------------------------- #
st.divider()
st.subheader("Recent log")
st.caption("The tail, newest last. The Logs page has filtering and history.")

errors = data["errors"]
if not components.render_absent(st, errors, "The log"):
    lines = list(errors.value or [])
    st.code("\n".join(lines[-40:]) or "(nothing recorded)", language="text")
