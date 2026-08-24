"""Execution — recommended trades, fills, levels and costs.

Every number here is a **recommendation**. Nothing on this page has been
sent anywhere, and nothing on it will be.
"""

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
    "Execution",
    icon="🎯",
    subtitle="The trades the simulator made on the most recent bar, sized for your account. Every one of them is placed by hand.",
)
mode = data["mode"]
balance = float(
    st.session_state.get(components.BALANCE_KEY, components.DEFAULT_BALANCE)
)
factor = components.scale_factor(data, balance)
execution = data["execution"]
summary = data["summary"]

components.render_note(
    st,
    "The gap between what is listed here and what is in the account is the "
    "difference between the backtest and reality — including any resting "
    "stop-loss order, which only exists if you placed it.",
)

if components.render_absent(st, execution, "Execution output"):
    st.stop()

trades = (execution.value or {}).get("trades")
if not isinstance(trades, pd.DataFrame) or trades.empty:
    components.render_note(st, "The last run executed no trades.")
    st.stop()

trades = trades.copy()
if "timestamp" in trades.columns:
    trades["timestamp"] = pd.to_datetime(trades["timestamp"], utc=True, errors="coerce")
    as_of = trades["timestamp"].max()
    latest = trades[trades["timestamp"] == as_of]
else:
    as_of, latest = None, trades

st.caption(f"Most recent bar: {components.moment(as_of)}")


def column(frame: pd.DataFrame, name: str, default=np.nan):
    """A column if the log has it, NaN otherwise.

    The trade log grew stop and FX columns over time; a log written before
    those existed must still render rather than raise.
    """
    return frame[name] if name in frame.columns else pd.Series(default, index=frame.index)


# --------------------------------------------------------------------------- #
# Per instrument
# --------------------------------------------------------------------------- #
st.subheader("Recommended trades on the latest bar")
components.render_note(
    st,
    f"Sized for an account of {components.money(balance)} "
    f"(×{factor:,.4g} the simulated size). Change the balance in the sidebar.",
)

if latest.empty:
    components.render_note(st, "No trade on the most recent bar.")
else:
    for _, row in latest.iterrows():
        traded = float(row.get("traded_units", 0.0) or 0.0)
        side = "Buy" if traded > 0 else "Sell"
        with st.container(border=True):
            head, body = st.columns([1, 4])
            with head:
                st.markdown(f"### {row.get('asset', '?')}")
                st.markdown(
                    f"**{side} {abs(traded) * factor:,.4f} units**  \n"
                    f"{str(row.get('action', '?')).replace('_', ' ').capitalize()}"
                )
                st.caption(
                    f"Simulated size {abs(traded):,.4f} · "
                    f"{str(row.get('reason', '?')).replace('_', ' ')}"
                )
            with body:
                cells = st.columns(4)
                cells[0].metric(
                    "Recommended fill price",
                    components.number(row.get("fill_price"), 4),
                )
                cells[1].metric(
                    "Stop-loss level", components.number(row.get("stop_level"), 4)
                )
                cells[2].metric(
                    "Take-profit level",
                    components.number(row.get("take_profit_level"), 4),
                )
                cells[3].metric(
                    "Exposure for your account",
                    components.money(float(row.get("notional") or 0.0) * factor),
                )

                costs = st.columns(4)
                costs[0].metric(
                    "Spread cost",
                    components.money(float(row.get("spread_cost") or 0.0) * factor),
                )
                costs[1].metric(
                    "Slippage",
                    components.money(float(row.get("slippage") or 0.0) * factor),
                )
                costs[2].metric(
                    "Currency conversion fee",
                    components.money(
                        float(row.get("fx_conversion_fee") or 0.0) * factor
                    ),
                )
                costs[3].metric(
                    "Exchange rate", components.number(row.get("fx_rate"), 4)
                )

            flags = []
            if bool(row.get("gap_through", False)):
                flags.append("⚠ **gap-through** — the level never traded; the "
                             "fill is the bar's open")
            if str(row.get("breach_type") or ""):
                flags.append(f"breach: `{row['breach_type']}`")
            if pd.notna(row.get("fx_adjusted_fill_price")):
                flags.append(
                    "All-in fill price after currency conversion: "
                    f"**{components.number(row.get('fx_adjusted_fill_price'), 4)}**"
                    " — what the order ticket will show"
                )
            if flags:
                st.caption(" · ".join(flags))

st.divider()

# --------------------------------------------------------------------------- #
# Cost and FX detail
# --------------------------------------------------------------------------- #
st.subheader("Costs and conversion")

tabs = st.tabs(
    ["Trading costs", "Currency conversion", "Stop-loss exits", "Leverage"]
)

with tabs[0]:
    cost_columns = [
        c
        for c in ("timestamp", "asset", "action", "traded_units", "price",
                  "fill_price", "spread_cost", "slippage", "financing",
                  "realized_pnl")
        if c in trades.columns
    ]
    components.render_frame(st, trades[cost_columns].tail(200), height=360)
    components.render_note(
        st,
        "P&L books at the reference price with the spread as a separate "
        "line; `fill_price` shows the effective price including it. Same "
        "total, two views — the columns are not additive with each other.",
    )

with tabs[1]:
    fx_columns = [
        c
        for c in ("timestamp", "asset", "fx_rate", "fx_spread",
                  "fx_conversion_fee", "fx_slippage", "fx_adjusted_fill_price")
        if c in trades.columns
    ]
    if len(fx_columns) <= 2:
        components.render_note(
            st, "This trade log predates the FX module; no conversion detail "
                "was recorded."
        )
    else:
        converted = trades[fx_columns]
        active = converted[pd.to_numeric(column(converted, "fx_rate"),
                                         errors="coerce").fillna(1.0) != 1.0]
        if active.empty:
            components.render_note(
                st,
                "No instrument in this run crossed a currency boundary, so "
                "no conversion fee was charged.",
            )
        components.render_frame(st, converted.tail(200), height=360)

with tabs[2]:
    stop_columns = [
        c
        for c in ("timestamp", "asset", "reason", "stop_level",
                  "take_profit_level", "stop_fill_price",
                  "take_profit_fill_price", "gap_through", "slippage_applied",
                  "breach_type")
        if c in trades.columns
    ]
    if len(stop_columns) <= 3:
        components.render_note(
            st, "This trade log predates the stops module."
        )
    else:
        level_exits = trades[trades.get("reason", "").isin(
            ["stop_loss", "take_profit"]
        )] if "reason" in trades.columns else pd.DataFrame()
        gaps = int(pd.Series(column(trades, "gap_through")).fillna(False).astype(bool).sum())

        left, right = st.columns(2)
        left.metric("Exits at a stop-loss or take-profit level", len(level_exits))
        right.metric("Of which gapped through the level", gaps)
        if gaps:
            components.render_note(
                st,
                "A gap-through exit filled at the bar's open, not at the "
                "level. A strategy whose exits are mostly gaps is one whose "
                "backtested stop distances are fiction.",
            )
        components.render_frame(st, trades[stop_columns].tail(200), height=320)

with tabs[3]:
    if "notional" in trades.columns and "equity" in trades.columns:
        leverage = pd.DataFrame(
            {
                "timestamp": column(trades, "timestamp"),
                "gross_notional": pd.to_numeric(trades["notional"], errors="coerce"),
                "equity": pd.to_numeric(trades["equity"], errors="coerce"),
            }
        )
        by_bar = leverage.groupby("timestamp").agg(
            gross_notional=("gross_notional", "sum"), equity=("equity", "last")
        )
        by_bar["leverage"] = by_bar["gross_notional"] / by_bar["equity"].replace(
            0, np.nan
        )
        st.line_chart(by_bar[["leverage"]], height=260)
        components.render_note(
            st,
            "Leverage per bar **as traded**, summed across the trades on "
            "that bar. It is not the book's standing leverage — a bar with "
            "no trade contributes nothing here. The cap the engine enforced "
            "is `max_leverage` on the Config page.",
        )
        components.render_frame(st, by_bar.tail(60), height=280)
    else:
        components.render_note(st, "The trade log has no notional or equity column.")

st.divider()

if summary.ok and summary.value.get("latest_trades"):
    with st.expander("Latest trades as recorded in the run summary", expanded=False):
        components.render_frame(st, pd.DataFrame(summary.value["latest_trades"]))
