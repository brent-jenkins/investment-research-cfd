"""The lake: what data is stored, how fresh it is, and how it was fetched.

Split out of the status page. Status answers "is the machine healthy" and
this answers "is the data any good" -- two questions asked at different
moments, and a status page carrying coverage tables is one where a red
scheduler row sits underneath a table nobody was reading.

Read-only. Nothing here refreshes the lake; ingestion is the scheduler's job.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parents[3])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from cfdinvest.ui import components, explain  # noqa: E402

components.render_crumb(st, "Data")
data = components.page_setup(
    st,
    "Data",
    icon="🗃",
    subtitle="What is in the lake, how fresh it is, and how the macro series "
    "were fetched.",
)

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
