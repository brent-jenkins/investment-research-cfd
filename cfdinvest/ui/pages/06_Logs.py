"""Logs — scheduler, pipeline, ingestion, execution and errors."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parents[3])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from cfdinvest.ui import components, sources  # noqa: E402

data = components.page_setup(
    st,
    "Logs",
    icon="📜",
    subtitle="Tailed from disk, never streamed from a running process.",
)
config = data["config"]

files = sources.load_log_files(config)

components.render_note(
    st,
    "Logs are tailed from disk, never streamed from a running process. The "
    "pipeline writes at most one file — whatever `logging.log_file` (or "
    "`CFD_LOG_FILE`) points at. The other roles are found by convention "
    "beside it or under `<data_root>/logs`.",
)

lines = st.slider("Lines to show", min_value=50, max_value=2000, value=300, step=50)
needle = st.text_input("Filter (case-insensitive substring)", value="")


def show(source, label: str) -> None:
    """Render one log tail, filtered."""
    if components.render_absent(st, source, label):
        return
    found = list(source.value)
    if needle:
        lowered = needle.lower()
        found = [line for line in found if lowered in line.lower()]
    if not found:
        components.render_note(st, f"No lines match `{needle}`.")
        return
    st.caption(f"{len(found)} line(s) from `{source.path}`")
    st.code("\n".join(found[-lines:]), language="text")


tabs = st.tabs(
    ["Errors", "Scheduler", "Application", "Data ingestion", "Execution",
     "Run history"]
)

with tabs[0]:
    show(sources.load_error_log(config, lines), "The error log")

with tabs[1]:
    show(sources.load_scheduler_log(config, lines), "The scheduler log")

with tabs[2]:
    available = files.value or {}
    if "application" in available:
        show(sources._tail(available["application"], lines), "The application log")
    else:
        components.render_absent(st, files, "The application log")

with tabs[3]:
    available = files.value or {}
    if "ingestion" in available:
        show(sources._tail(available["ingestion"], lines), "The ingestion log")
    else:
        source = sources.load_error_log(config, lines * 3)
        if source.ok:
            filtered = [
                line for line in source.value if "ingestion" in line.lower()
            ]
            st.caption("Filtered from the application log — no dedicated file.")
            st.code("\n".join(filtered[-lines:]) or "(nothing)", language="text")
        else:
            components.render_absent(st, source, "Ingestion logs")

with tabs[4]:
    available = files.value or {}
    if "execution" in available:
        show(sources._tail(available["execution"], lines), "The execution log")
    else:
        source = sources.load_error_log(config, lines * 3)
        if source.ok:
            filtered = [
                line for line in source.value
                if "execution" in line.lower() or "backtest" in line.lower()
            ]
            st.caption("Filtered from the application log — no dedicated file.")
            st.code("\n".join(filtered[-lines:]) or "(nothing)", language="text")
        else:
            components.render_absent(st, source, "Execution logs")

with tabs[5]:
    recorded_warnings = (data["persisted"].value or {}).get("warnings") or []
    if recorded_warnings:
        st.subheader("Warnings the last run recorded")
        for warning in recorded_warnings:
            st.warning(warning)

    # Every run writes a summary.json carrying its own status and error.
    # With no log file configured, this is the only durable record of what
    # happened, which makes it worth its own tab.
    scheduler_state = (data["scheduler"].value or {})
    history = scheduler_state.get("history")
    if isinstance(history, pd.DataFrame) and not history.empty:
        components.render_frame(st, history, height=420)
        failures = history[history["status"].astype(str) != "ok"]
        if not failures.empty:
            st.error(f"{len(failures)} of the last {len(history)} runs did not succeed.")
    else:
        components.render_note(st, "No run summaries on disk yet.")

st.divider()

with st.expander("Log files the dashboard can see", expanded=False):
    available = files.value or {}
    if available:
        components.render_frame(
            st,
            pd.DataFrame(
                [
                    {
                        "role": role,
                        "path": str(path),
                        "size_kb": round(Path(path).stat().st_size / 1024, 1),
                        "modified": components.moment(
                            pd.Timestamp(Path(path).stat().st_mtime, unit="s", tz="UTC")
                        ),
                    }
                    for role, path in sorted(available.items())
                ]
            ),
        )
    else:
        components.render_absent(st, files, "Log files")
