"""Config — every setting, read-only.

Nothing on this page is editable, and there is no code path in this package
that writes a config file.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parents[3])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from cfdinvest.config import AppConfig, load_toml  # noqa: E402
from cfdinvest.execution.frame import ExecutionConfig  # noqa: E402
from cfdinvest.execution.fx import FXConfig  # noqa: E402
from cfdinvest.execution.stops import StopConfig  # noqa: E402
from cfdinvest.schedule.runner import RunConfig  # noqa: E402
from cfdinvest.schedule.scheduler import SchedulerConfig  # noqa: E402
from cfdinvest.ui import components, configview  # noqa: E402

data = components.page_setup(st, "Config", icon="⚙")
config = data["config"]
config_source = data["config_source"]

st.markdown('<span class="cfd-readonly">read-only</span>', unsafe_allow_html=True)

if config_source.failed:
    st.error(
        f"**The config could not be read.** {config_source.error}  \n"
        "Everything below is the built-in default, not what the pipeline is "
        "actually running under."
    )

# --------------------------------------------------------------------------- #
# What is actually loaded versus what is a default
# --------------------------------------------------------------------------- #
components.render_note(
    st,
    "**Only the global config is read from disk.** The per-layer settings — "
    "stops, FX, execution, scheduler — are constructed in code by whatever "
    "invokes the pipeline and are *not* recorded in the run summary, so what "
    "is shown for them is the **built-in default**. If a run used different "
    "values, this page cannot know. Closing that gap means persisting "
    "`execution.metadata` alongside each run.",
)


def render(title: str, obj, default, *, payload=None, prefix: str = "") -> None:
    """One config section, with overrides and problems called out."""
    frame = configview.to_frame(obj, default)
    overridden = frame[frame["overridden"] == True]  # noqa: E712
    missing = frame[frame["missing"] == True]  # noqa: E712
    deprecated = frame[frame["deprecated"] == True]  # noqa: E712

    cells = st.columns(4)
    cells[0].metric("Settings", len(frame))
    cells[1].metric("Overridden", len(overridden))
    cells[2].metric("Unset", len(missing))
    cells[3].metric("Deprecated", len(deprecated))

    if not deprecated.empty:
        for _, row in deprecated.iterrows():
            st.warning(f"**{row['setting']}** is deprecated. {row['note']}")

    unknown = configview.unknown_keys(payload, obj, prefix=prefix)
    if unknown:
        st.warning(
            "**Unrecognised keys in the config file — these are silently "
            "ignored by the loader:** " + ", ".join(f"`{key}`" for key in unknown)
        )

    tabs = st.tabs(["Overridden", "All settings", "Unset"])
    with tabs[0]:
        if overridden.empty:
            components.render_note(st, "Everything is at its default.")
        else:
            components.render_frame(
                st, overridden[["setting", "value", "default"]], height=280
            )
    with tabs[1]:
        components.render_frame(
            st,
            frame[["setting", "value", "default", "overridden", "missing"]],
            height=420,
        )
    with tabs[2]:
        if missing.empty:
            components.render_note(st, "Every setting has a value.")
        else:
            components.render_note(
                st,
                "Most of these are legitimately optional. The ones worth "
                "checking are paths, credentials and anything a scheduled "
                "run depends on.",
            )
            components.render_frame(st, missing[["setting", "value"]], height=280)


# --------------------------------------------------------------------------- #
# Global
# --------------------------------------------------------------------------- #
st.subheader("Global config")

payload = {}
try:
    payload = load_toml("config.toml") or {}
except Exception as error:  # noqa: BLE001
    st.caption(f"No readable config.toml alongside the app ({error}).")

st.caption(
    "Loaded from config.toml and `CFD_*` environment variables. "
    "Credentials are redacted — this page renders into a browser."
)
render("Global", config, AppConfig(), payload=payload)

st.divider()

# --------------------------------------------------------------------------- #
# Per instrument
# --------------------------------------------------------------------------- #
st.subheader("Per-instrument config")

universe = list(data["universe"] or [])
if not universe:
    components.render_note(st, "No universe configured.")
else:
    rows = []
    execution_default = ExecutionConfig()
    fx_default = FXConfig()
    spreads = execution_default.spread_bps or {}
    classes = execution_default.asset_classes or {}
    rates = execution_default.financing_rates or {}
    currencies = fx_default.instrument_currencies or {}

    market = (data["market"].value or {}).get("sessions")
    for ticker in universe:
        row = {
            "ticker": ticker,
            "asset_class": classes.get(ticker, "(default)"),
            "spread_bps": spreads.get(ticker, "(class default)"),
            "financing_rates": rates.get(ticker, "(global)"),
            "currency": currencies.get(ticker, fx_default.account_currency),
        }
        if isinstance(market, pd.DataFrame) and ticker in market.index:
            row["session"] = market.loc[ticker, "session"]
            row["is_open"] = bool(market.loc[ticker, "is_open"])
        rows.append(row)

    components.render_frame(st, pd.DataFrame(rows).set_index("ticker"))
    components.render_note(
        st,
        "Per-instrument overrides live on `ExecutionConfig` and `FXConfig` "
        "in code, not in the config file, so `(default)` here means "
        "\"nothing overridden in the defaults\" — not \"nothing overridden "
        "in the run\".",
    )

st.divider()

# --------------------------------------------------------------------------- #
# Layer configs
# --------------------------------------------------------------------------- #
sections = (
    ("Stop / take-profit", StopConfig(), StopConfig()),
    ("FX", FXConfig(), FXConfig()),
    ("Execution", ExecutionConfig(), ExecutionConfig()),
    ("Scheduler", SchedulerConfig(), SchedulerConfig()),
    ("Run", RunConfig(), RunConfig()),
)

for title, obj, default in sections:
    st.subheader(f"{title} config")
    st.caption("Built-in defaults — see the note at the top of this page.")
    render(title, obj, default)
    st.divider()

# --------------------------------------------------------------------------- #
# Environment
# --------------------------------------------------------------------------- #
st.subheader("Environment")

import os  # noqa: E402

env = {
    key: configview.redact(key, value)
    for key, value in sorted(os.environ.items())
    if key.startswith("CFD_")
}
if env:
    components.render_frame(
        st, pd.DataFrame({"variable": list(env), "value": list(env.values())})
    )
    components.render_note(
        st, "Environment variables override config.toml, so anything listed "
            "here is what the pipeline actually used."
    )
else:
    components.render_note(st, "No `CFD_*` environment variables are set.")
