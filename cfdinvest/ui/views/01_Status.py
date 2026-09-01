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

# The Data page is gone -- the dashboard was cut to three during the
# research period -- so this page carries the run's own figures too.
last_run_at = scheduler.get("last_run")
last_success_at = scheduler.get("last_success")
outcome = explain.outcome(scheduler.get("last_status")) or components.DASH
succeeded = scheduler.get("last_status") == "ok"

# Last run and last success were separate tiles showing, on a healthy
# system, the identical timestamp twice. The information was never the pair
# of values -- it is whether they *differ*, which says a run started and did
# not finish. So they are one tile until they disagree, and a second tile
# appears only when there is something to say.
run_note = " · ".join(part for part in (
    components.age(last_run_at, now),
    outcome if outcome != components.DASH else "",
    components.duration(record.get("duration_seconds")),
) if part and part != components.DASH)

tiles = [
    ("Overall", overall,
     f"{len(health.checks) - len(health.triggered)} of {len(health.checks)} "
     "checks passing", overall_level),
    ("Last run", components.clock(last_run_at), run_note,
     "pass" if succeeded else "warning" if last_run_at else "unchecked"),
]

if not succeeded and last_success_at:
    # Only when it means something: a successful run older than the last
    # attempt is the shape of "it started and died".
    tiles.append((
        "Last success", components.clock(last_success_at),
        components.age(last_success_at, now), "warning",
    ))

tiles.extend([
    ("Disk free", components.DASH if free_gb is None else f"{free_gb:.1f} GB",
     components.percent(free_share) + " free" if free_share is not None else "",
     disk_level),
    ("Bars processed", f"{record.get('bars', 0):,}",
     f"{len(record.get('universe') or [])} instruments", "pass"),
])

components.render_tiles(st, tiles)

# --------------------------------------------------------------------------- #
# Scheduler health
# --------------------------------------------------------------------------- #
st.divider()
st.subheader("Scheduler health")

# What the poller is actually asked to price -- which is the ENGINE's own
# intended positions, and nothing else.
#
# This counted the confirm layer plus the paper book. The confirm layer is
# empty on this deployment (nothing is confirmed by hand) and the paper book
# was deleted on 2026-08-30, so the count was 0 + 0 and the table reported
# "Nothing is open in either book, so there is nothing to price" while the
# broker held seven positions and the poller was pricing all seven.
#
# A row that says a working job has nothing to do is how a table teaches you
# to stop reading it -- which is the same sentence the previous version of
# this comment used about a different wrong source. Read what the poller
# reads instead of guessing at it.
try:
    from cfdinvest.persist import intent as _intent

    held = len(_intent.held_tickers(data.get("config")) or ())
except Exception:  # noqa: BLE001 - a view must not break on this
    # Absence named: fall back to the confirm layer, which is a real source
    # even if it is usually empty, rather than silently reporting zero.
    held = components.confirmed_open_count(data)
st.caption(
    "Each job's last run is taken from the artefact it wrote where it writes "
    "one, and otherwise from the daemon's own record — the paper book writes "
    "a file only when it opens something, and declining to trade is a run "
    "that happened."
)

# Whether a market this universe trades is open right now. The intraday
# jobs run only during a session, so an overnight gap is the system working
# rather than a fault.
# Unknown is treated as open. Guessing wrong that way shows "overdue" while
# the market is shut, which is visible and harmless; guessing the other way
# hides a genuinely stalled pipeline behind "markets closed".
session_open = True
try:
    market_source = data.get("market")
    market = getattr(market_source, "value", None)
    sessions = market.get("sessions") if isinstance(market, dict) else None
    # Explicit emptiness checks throughout: a DataFrame has no truth value,
    # and `or {}` on one raises rather than falling back.
    if sessions is not None and not sessions.empty and "is_open" in sessions:
        session_open = bool(sessions["is_open"].any())
except Exception:  # noqa: BLE001 - a calendar gap must not blank the page
    session_open = True

rows = vitals.job_report(
    vitals.gather_evidence(data["config"], account=data.get("account")),
    now=now,
    quotes_expected=held > 0,
    session_open=session_open,
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
# There is no Logs page. This caption promised "filtering and history" on one
# for as long as it has existed, and the app has five: Dashboard, Status,
# Signals, Broker, Universe. Sending a reader somewhere that does not exist
# is worse than saying plainly that the tail is all there is.
st.caption(
    "The tail of the application log, newest last. Reading is bounded at "
    "both ends: `sources._tail` seeks to the end of the file and never reads "
    "more than 4MB however large it grows, and the controls below decide how "
    "much of that is shown."
)

errors = data["errors"]
if not components.render_absent(st, errors, "The log"):
    lines = list(errors.value or [])
    if not lines:
        st.code("(nothing recorded)", language="text")
    else:
        # Paged rather than dumped. The reader already tails safely, but a
        # few hundred lines in one block is unreadable and the interesting
        # line is rarely the last one.
        controls = st.columns([1, 1, 2])
        window = controls[0].select_slider(
            "Lines", options=[20, 40, 80, 160, 320], value=40,
            key="status_log_window",
        )
        needle = controls[1].text_input(
            "Filter", value="", key="status_log_filter",
            placeholder="e.g. WARNING",
        )
        shown = (
            [line for line in lines if needle.lower() in line.lower()]
            if needle else lines
        )
        page_count = max((len(shown) + window - 1) // window, 1)
        page = 1
        if page_count > 1:
            page = controls[2].slider(
                "Page (1 is newest)", min_value=1, max_value=page_count,
                value=1, key="status_log_page",
            )
        end = len(shown) - (page - 1) * window
        block = shown[max(end - window, 0):end]
        st.code(chr(10).join(block) or "(nothing matched)", language="text")
        st.caption(
            f"Showing {len(block)} of {len(shown):,} line(s)"
            + (f" matching `{needle}`" if needle else "")
            + f", page {page} of {page_count}. The file holds more than is "
            "read; this is the tail."
        )
