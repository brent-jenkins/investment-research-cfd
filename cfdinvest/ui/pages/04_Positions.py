"""Positions — what is open, and what it is carrying."""

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
    "Positions",
    icon="📦",
    subtitle="What is open, what it is carrying, and what it would be at your account size.",
)
mode = data["mode"]
balance = float(
    st.session_state.get(components.BALANCE_KEY, components.DEFAULT_BALANCE)
)
factor = components.scale_factor(data, balance)
execution = data["execution"]

if components.render_absent(st, execution, "Execution output"):
    st.stop()

parts = execution.value or {}
positions = parts.get("positions")
trades = parts.get("trades")
equity_frame = parts.get("equity")

if not isinstance(positions, pd.DataFrame) or positions.empty:
    components.render_note(st, "The last run held no positions.")
    st.stop()

positions = positions.copy()
positions.index = pd.to_datetime(positions.index, utc=True, errors="coerce")
as_of = positions.index.max()
held = positions.loc[as_of]
held = held[held != 0]

equity = np.nan
if isinstance(equity_frame, pd.DataFrame) and not equity_frame.empty:
    equity = float(pd.to_numeric(equity_frame.iloc[-1], errors="coerce").iloc[0])

st.caption(f"As at {components.moment(as_of)}")

if held.empty:
    st.info("The book is flat. Nothing is open.")
    st.stop()

# --------------------------------------------------------------------------- #
# Per-instrument context, from the trade log
# --------------------------------------------------------------------------- #
# Entry price, levels, financing and FX are recorded per trade, not per bar,
# so the open book's context is the most recent trade on each instrument.
context = pd.DataFrame()
if isinstance(trades, pd.DataFrame) and not trades.empty and "asset" in trades:
    ordered = trades.sort_values("timestamp")
    wanted = [
        c
        for c in ("fill_price", "price", "stop_level", "take_profit_level",
                  "fx_rate", "timestamp")
        if c in ordered.columns
    ]
    context = ordered.groupby("asset")[wanted].last()

    accrued = {}
    for name in ("financing", "fx_conversion_fee", "fx_spread", "fx_slippage",
                 "spread_cost", "slippage"):
        if name in ordered.columns:
            accrued[name] = ordered.groupby("asset")[name].sum()
    if accrued:
        context = context.join(pd.DataFrame(accrued), how="left")

last_price = pd.Series(dtype="float64")
if isinstance(trades, pd.DataFrame) and "price" in trades.columns:
    last_price = trades.sort_values("timestamp").groupby("asset")["price"].last()

st.subheader(
    f"{len(held)} open position" + ("s" if len(held) != 1 else "")
)
components.render_note(
    st,
    f"Sizes and money figures are scaled to an account of "
    f"{components.money(balance)} (×{factor:,.4g}). Prices and levels are "
    "not scaled — a price is a price at any account size.",
)

rows = []
for ticker, units in held.items():
    entry = float(context.get("fill_price", pd.Series()).get(ticker, np.nan)) if not context.empty else np.nan
    mark = float(last_price.get(ticker, np.nan)) if len(last_price) else np.nan
    unrealized = units * (mark - entry) if np.isfinite(entry) and np.isfinite(mark) else np.nan
    notional = abs(units * mark) if np.isfinite(mark) else np.nan
    fx_costs = sum(
        float(context.get(name, pd.Series()).get(ticker, 0.0) or 0.0)
        for name in ("fx_conversion_fee", "fx_spread", "fx_slippage")
        if not context.empty
    )

    rows.append(
        {
            "asset": ticker,
            "units": units,
            "direction": "long" if units > 0 else "short",
            "entry_price": entry,
            "last_price": mark,
            "stop_level": float(context.get("stop_level", pd.Series()).get(ticker, np.nan)) if not context.empty else np.nan,
            "take_profit_level": float(context.get("take_profit_level", pd.Series()).get(ticker, np.nan)) if not context.empty else np.nan,
            "unrealized_pnl": unrealized,
            "financing_accrued": float(context.get("financing", pd.Series()).get(ticker, np.nan)) if not context.empty else np.nan,
            "fx_costs": fx_costs,
            "notional": notional,
            "leverage": notional / equity if np.isfinite(notional) and np.isfinite(equity) and equity else np.nan,
            "fx_rate": float(context.get("fx_rate", pd.Series()).get(ticker, np.nan)) if not context.empty else np.nan,
        }
    )

book = pd.DataFrame(rows).set_index("asset")

for ticker, row in book.iterrows():
    with st.container(border=True):
        head, body = st.columns([1, 4])
        with head:
            st.markdown(f"### {ticker}")
            st.markdown(
                components.direction_badge(row["direction"], mode),
                unsafe_allow_html=True,
            )
            st.caption(
                f"{row['units'] * factor:,.4f} units  \n"
                f"simulated {row['units']:,.4f}"
            )
        with body:
            top = st.columns(4)
            top[0].metric("Entry price", components.number(row["entry_price"], 4))
            top[1].metric("Stop-loss level", components.number(row["stop_level"], 4))
            top[2].metric(
                "Take-profit level", components.number(row["take_profit_level"], 4)
            )
            top[3].metric(
                "Unrealised profit & loss",
                components.money(row["unrealized_pnl"] * factor),
                delta_color="off",
            )
            bottom = st.columns(4)
            bottom[0].metric("Exposure", components.money(row["notional"] * factor))
            bottom[1].metric(
                "Leverage",
                components.number(row["leverage"], 2),
                help="A ratio, so it does not change with account size.",
            )
            bottom[2].metric(
                "Overnight financing",
                components.money(row["financing_accrued"] * factor),
            )
            bottom[3].metric(
                "Currency costs", components.money(row["fx_costs"] * factor)
            )

        if not np.isfinite(row["stop_level"]):
            st.caption(
                "⚠ No stop-loss level recorded for this position — either "
                "stop-losses are disabled, or the average true range had not "
                "warmed up when it opened."
            )

st.divider()

# --------------------------------------------------------------------------- #
# Book totals
# --------------------------------------------------------------------------- #
st.subheader("Whole book")
totals = st.columns(4)
totals[0].metric("Total exposure", components.money(book["notional"].sum() * factor))
totals[1].metric(
    "Gross leverage",
    components.number(book["notional"].sum() / equity if equity else np.nan, 2),
)
totals[2].metric(
    "Unrealised profit & loss",
    components.money(book["unrealized_pnl"].sum() * factor),
)
totals[3].metric("Account value", components.money(balance))

scaled = book.copy()
for name in ("units", "unrealized_pnl", "financing_accrued", "fx_costs", "notional"):
    if name in scaled.columns:
        scaled[name] = scaled[name] * factor
components.render_frame(st, scaled)

components.render_note(
    st,
    "Entry price, financing and FX are read from the **trade log**, which "
    "records them per trade rather than per bar. Financing and FX therefore "
    "show what has accrued across every trade on that instrument in this "
    "run, not only since the current position opened.",
)
