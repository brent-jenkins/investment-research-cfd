"""cfdinvest dashboard — the landing page.

Run it with::

    streamlit run cfdinvest/ui/app.py

**This app is read-only.** It opens files the pipeline already wrote and
renders them. It places no orders, refreshes no data, writes no config and
contacts no external service. That is enforced by every read going through
:mod:`cfdinvest.ui.sources`, which has no write path at all.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Streamlit runs each page as a script, so the import root is this file's
# directory rather than the project. Must happen before any cfdinvest import.
_ROOT = str(Path(__file__).resolve().parents[2])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import streamlit as st  # noqa: E402

from cfdinvest.ui import components  # noqa: E402

data = components.page_setup(st, "cfdinvest", icon="📈")

health = data["health"]
summary = data["summary"]
lake = data["lake"]
scheduler = data["scheduler"]

components.render_note(
    st,
    "A read-only view of the trading system. Every trade is placed by hand; "
    "nothing on any page is sent anywhere.",
)

# -- the four numbers worth seeing before anything else ---------------------- #
columns = st.columns(4)
columns[0].metric(
    "Health",
    "critical" if health.show_banner else ("warnings" if health.triggered else "ok"),
    delta=f"{len(health.triggered)} of {len(health.checks)} checks firing",
    delta_color="inverse" if health.triggered else "normal",
)
columns[1].metric(
    "Lake freshness",
    components.age((lake.value or {}).get("last_updated"), data["now"]),
    delta=f"{(lake.value or {}).get('series_count', 0)} series",
    delta_color="off",
)
columns[2].metric(
    "Last run",
    components.age((scheduler.value or {}).get("last_run"), data["now"]),
    delta=str((scheduler.value or {}).get("last_status") or components.DASH),
    delta_color="off",
)
metrics = (summary.value or {}).get("metrics") or {}
columns[3].metric(
    "Equity",
    components.money(metrics.get("final_equity")),
    delta=components.percent(metrics.get("total_return")),
)

st.divider()

# -- what is on the other pages ---------------------------------------------- #
left, right = st.columns([3, 2])

with left:
    st.subheader("Pages")
    for name, description in components.PAGES:
        st.markdown(f"**{name}** — {description}")

with right:
    st.subheader("Last run")
    if not components.render_absent(st, summary, "The run summary"):
        record = summary.value
        st.markdown(
            f"**`{record.get('run_id', '?')}`** — status "
            f"`{record.get('status', '?')}`"
        )
        st.caption(
            f"{record.get('bars', 0)} bars over "
            f"{len(record.get('universe') or [])} instruments, "
            f"finished {components.moment(record.get('finished_at'))}"
        )
        if record.get("error"):
            st.error(record["error"])

st.divider()
components.render_note(
    st,
    "Data source: the most recent run directory under "
    f"`{(lake.value or {}).get('data_root', 'the lake')}`. "
    "Nothing is recomputed here — if a number looks stale, the pipeline has "
    "not run, and the Status page will say so.",
)
