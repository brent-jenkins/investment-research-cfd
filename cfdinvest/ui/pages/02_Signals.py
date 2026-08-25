"""Signals — what the model wants, per instrument."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parents[3])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from cfdinvest.schema import TICKER, TIMESTAMP  # noqa: E402
from cfdinvest.ui import components, explain  # noqa: E402

data = components.page_setup(
    st,
    "Signals",
    icon="🧭",
    subtitle="What the model wants to hold, and why. A signal is not yet "
    "a position — see Execution for the trades it implies.",
)
mode = data["mode"]
signals = data["signals"]
summary = data["summary"]

components.render_note(
    st,
    "Only **active** signals are stored — flat cells are the vast majority "
    "and are dropped before writing, so an instrument missing here is flat, "
    "not missing.",
)

if components.render_absent(st, signals, "Signals"):
    st.stop()

frame = signals.value.copy()
if isinstance(frame.index, pd.MultiIndex):
    frame = frame.reset_index()

if TIMESTAMP in frame.columns:
    frame[TIMESTAMP] = pd.to_datetime(frame[TIMESTAMP], utc=True, errors="coerce")
    as_of = frame[TIMESTAMP].max()
    latest = frame[frame[TIMESTAMP] == as_of].copy()
else:
    as_of, latest = None, frame.copy()

st.caption(f"As of {components.moment(as_of)}")

# --------------------------------------------------------------------------- #
# Levels, joined from the trade log
# --------------------------------------------------------------------------- #
# Stop and take-profit levels live on the trade log, not on the signal frame:
# a level belongs to a position, and a signal is not yet a position. Joining
# the most recent trade per instrument is what connects the two.
execution = data["execution"]
trades = (execution.value or {}).get("trades")
levels = pd.DataFrame()

if isinstance(trades, pd.DataFrame) and not trades.empty and "asset" in trades:
    wanted = [
        column
        for column in ("stop_level", "take_profit_level", "fill_price", "price")
        if column in trades.columns
    ]
    if wanted:
        levels = (
            trades.sort_values("timestamp").groupby("asset")[wanted].last()
        )

if not levels.empty and TICKER in latest.columns:
    latest = latest.merge(
        levels, left_on=TICKER, right_index=True, how="left", suffixes=("", "_trade")
    )

reference = None
for column in ("price", "fill_price"):
    if column in latest.columns:
        reference = pd.to_numeric(latest[column], errors="coerce")
        break

for level, label in (("stop_level", "stop_distance"), ("take_profit_level", "tp_distance")):
    if level in latest.columns and reference is not None:
        distance = (pd.to_numeric(latest[level], errors="coerce") - reference).abs()
        latest[label] = distance
        latest[f"{label}_pct"] = distance / reference.replace(0, np.nan)

# --------------------------------------------------------------------------- #
# Per instrument
# --------------------------------------------------------------------------- #
st.subheader("Current signals")

if latest.empty:
    components.render_note(st, "No active signal on the most recent bar.")
else:
    for _, row in latest.sort_values(TICKER if TICKER in latest else latest.columns[0]).iterrows():
        ticker = row.get(TICKER, "?")
        with st.container(border=True):
            name, strength, levels = st.columns([1.5, 1, 1.2])
            with name:
                st.markdown(
                    f"#### {ticker} &nbsp; "
                    f"{components.direction_badge(row.get('direction'), mode)}",
                    unsafe_allow_html=True,
                )
                st.caption(explain.reason(row.get("reason")))
                st.caption(components.short_moment(row.get(TIMESTAMP)))
            with strength:
                components.render_kv(
                    st,
                    {
                        "Size": components.number(row.get("size"), 3),
                        "Confidence": components.percent(
                            row.get("confidence"), 0
                        ),
                    },
                )
            with levels:
                components.render_kv(
                    st,
                    {
                        "Stop-loss distance": components.percent(
                            row.get("stop_distance_pct")
                        ),
                        "Take-profit distance": components.percent(
                            row.get("tp_distance_pct")
                        ),
                    },
                )

st.divider()

# --------------------------------------------------------------------------- #
# Regime context
# --------------------------------------------------------------------------- #
st.subheader("Regime context")

persisted = data["persisted"]
regimes = (persisted.value or {}).get("regimes")

if regimes is None or getattr(regimes, "empty", True):
    components.render_absent(st, persisted, "The regime classifications")
    components.render_note(
        st,
        "The signal `reason` string above names the regime that produced each "
        "decision, so the context is not lost even when the full "
        "classification is absent.",
    )
else:
    frame_regimes = regimes.copy()
    if TIMESTAMP in frame_regimes.columns:
        frame_regimes[TIMESTAMP] = pd.to_datetime(
            frame_regimes[TIMESTAMP], utc=True, errors="coerce"
        )
        latest_regime = frame_regimes.sort_values(TIMESTAMP).iloc[-1]
    else:
        latest_regime = frame_regimes.iloc[-1]

    labels = {
        "volatility_regime": "Volatility",
        "trend_regime": "Trend",
        "correlation_regime": "Correlation",
        "macro_regime": "Macro",
        "combined_regime": "Overall",
    }
    present = [c for c in labels if c in frame_regimes.columns]
    cells = st.columns(max(len(present), 1))
    for cell, column in zip(cells, present):
        cell.metric(
            labels[column],
            str(latest_regime[column]).replace("_", " ").capitalize(),
        )

    st.caption(
        f"As at {components.short_moment(latest_regime.get(TIMESTAMP))} · "
        "one classification per bar, market-wide rather than per instrument."
    )
    with st.expander("Regime history", expanded=False):
        components.render_frame(
            st,
            frame_regimes.sort_values(TIMESTAMP, ascending=False)
            if TIMESTAMP in frame_regimes.columns else frame_regimes,
            height=320,
        )

st.divider()

# --------------------------------------------------------------------------- #
# History
# --------------------------------------------------------------------------- #
st.subheader("Signal history")
components.render_frame(st, frame.sort_values(TIMESTAMP, ascending=False)
                        if TIMESTAMP in frame.columns else frame, height=420)

if summary.ok and summary.value.get("latest_signals"):
    with st.expander("As recorded in the run summary", expanded=False):
        components.render_frame(
            st, pd.DataFrame(summary.value["latest_signals"])
        )
