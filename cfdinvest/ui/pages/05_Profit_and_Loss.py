"""Profit & Loss — the equity curve and where the money went."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parents[3])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from cfdinvest.ui import components  # noqa: E402

data = components.page_setup(
    st,
    "Profit & Loss",
    icon="💷",
    subtitle="How the simulated account performed, and what it paid to do so. "
    "Money figures are scaled to your account balance.",
)
balance = float(
    st.session_state.get(components.BALANCE_KEY, components.DEFAULT_BALANCE)
)
factor = components.scale_factor(data, balance)
execution = data["execution"]
summary = data["summary"]

if components.render_absent(st, execution, "Execution output"):
    st.stop()

parts = execution.value or {}
equity_frame = parts.get("equity")
trades = parts.get("trades")
metrics = (summary.value or {}).get("metrics") or {}

components.render_note(
    st,
    "The pipeline persists the equity curve and the trade log, but **not** "
    "the per-bar P&L decomposition. The cost series below are therefore "
    "reconstructed by summing the trade log per day, and the run totals come "
    "from the run summary. Both are exact for costs, which are only ever "
    "charged on a trade; the gap and intraday legs are not recoverable here.",
)

# --------------------------------------------------------------------------- #
# Headline
# --------------------------------------------------------------------------- #
def scaled(key):
    value = metrics.get(key)
    return None if value is None else float(value) * factor


cells = st.columns(5)
cells[0].metric("Closing account value", components.money(scaled("final_equity")))
cells[1].metric("Total profit & loss", components.money(scaled("total_pnl")))
cells[2].metric("Return", components.percent(metrics.get("total_return")))
cells[3].metric(
    "Maximum drawdown", components.percent(metrics.get("max_drawdown_fraction"))
)
cells[4].metric(
    "Trades placed", len(trades) if isinstance(trades, pd.DataFrame) else 0
)

st.divider()

# --------------------------------------------------------------------------- #
# Equity curve
# --------------------------------------------------------------------------- #
st.subheader("Account value")

if isinstance(equity_frame, pd.DataFrame) and not equity_frame.empty:
    curve = equity_frame.copy()
    curve.index = pd.to_datetime(curve.index, utc=True, errors="coerce")
    curve = curve.select_dtypes("number")
    st.line_chart(curve, height=320)

    peak = curve.iloc[:, 0].cummax()
    drawdown = (curve.iloc[:, 0] - peak) / peak.replace(0, np.nan)
    st.caption("Drawdown")
    st.area_chart(drawdown.to_frame("drawdown"), height=180)
else:
    components.render_note(st, "No equity curve was written by the last run.")

st.divider()

# --------------------------------------------------------------------------- #
# Cost decomposition
# --------------------------------------------------------------------------- #
st.subheader("Where the money went")

TOTALS = (
    ("Realised profit & loss", "total_pnl", "realized_pnl"),
    ("Spread paid", "total_spread", "spread_cost"),
    ("Slippage", "total_slippage", "slippage"),
    ("Overnight financing", "total_financing", "financing"),
    ("Currency conversion", "total_fx", "fx_conversion_fee"),
)

cost_cells = st.columns(len(TOTALS))
for position, (label, metric_key, trade_column) in enumerate(TOTALS):
    value = metrics.get(metric_key)
    if value is None and isinstance(trades, pd.DataFrame) and trade_column in trades:
        value = float(pd.to_numeric(trades[trade_column], errors="coerce").sum())
    cost_cells[position].metric(
        label, components.money(None if value is None else float(value) * factor)
    )

components.render_note(
    st,
    "Costs are reported as **positive amounts taken out of the account**, "
    "which is how the run summary records them. The FX total is the "
    "conversion fee only when reconstructed from the trade log; the run "
    "summary's `total_fx` includes the FX spread and slippage as well.",
)

if isinstance(trades, pd.DataFrame) and not trades.empty and "timestamp" in trades:
    daily = trades.copy()
    daily["timestamp"] = pd.to_datetime(daily["timestamp"], utc=True, errors="coerce")
    daily["date"] = daily["timestamp"].dt.date

    available = [
        column
        for column in ("realized_pnl", "spread_cost", "slippage", "financing",
                       "fx_conversion_fee", "fx_spread", "fx_slippage")
        if column in daily.columns
    ]
    if available:
        by_day = daily.groupby("date")[available].sum()
        tabs = st.tabs(
            ["Costs by day", "Cumulative costs", "Daily summary", "Trade history"]
        )

        with tabs[0]:
            st.bar_chart(by_day, height=300)
        with tabs[1]:
            st.line_chart(by_day.cumsum(), height=300)
        with tabs[2]:
            enriched = by_day.copy()
            enriched["trades"] = daily.groupby("date").size()
            components.render_frame(st, enriched.sort_index(ascending=False), height=360)
        with tabs[3]:
            components.render_frame(
                st, daily.sort_values("timestamp", ascending=False), height=420
            )
    else:
        components.render_frame(st, trades, height=380)
else:
    components.render_note(st, "No trades were recorded, so there is nothing to split.")

st.divider()

# --------------------------------------------------------------------------- #
# Run metrics
# --------------------------------------------------------------------------- #
with st.expander("Every measure the run recorded", expanded=False):
    if metrics:
        flat = {
            key: value
            for key, value in metrics.items()
            if isinstance(value, (int, float, str, bool)) or value is None
        }
        components.render_frame(
            st,
            pd.DataFrame({"metric": list(flat), "value": list(flat.values())}),
            height=420,
        )
    else:
        components.render_note(st, "The run recorded no metrics.")
