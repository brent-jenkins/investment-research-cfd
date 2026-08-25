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

# Which book this is. Silence here would be the worst of the options: the
# numbers change when you confirm a fill, and nothing on the page would say
# why they had.
source = parts.get("positions_source", "simulated")
if source == "confirmed":
    simulated = parts.get("positions_simulated")
    st.success(
        "Showing **your confirmed fills**, not the simulation. Recorded on "
        "the Confirm Trades page."
    )
    if isinstance(simulated, pd.DataFrame) and not simulated.empty:
        differences = {}
        for name in held.index:
            if name not in simulated.columns:
                continue
            was = float(pd.to_numeric(
                pd.Series([simulated.iloc[-1].get(name)]), errors="coerce"
            ).fillna(0.0).iloc[0])
            if was != float(held[name]):
                differences[name] = {"simulated": was, "confirmed": float(held[name])}
        if differences:
            with st.expander(
                f"{len(differences)} position(s) differ from the simulation"
            ):
                components.render_frame(
                    st, pd.DataFrame(differences).T.rename_axis("instrument")
                )
else:
    components.render_note(
        st,
        "Showing the **simulated** positions. Nothing has been confirmed for "
        "this run, so the system is still assuming its own recommendations "
        "were followed exactly.",
    )

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
        name, levels, money = st.columns([1.3, 1.2, 1.3])
        with name:
            st.markdown(
                f"#### {ticker} &nbsp; "
                f"{components.direction_badge(row['direction'], mode)}",
                unsafe_allow_html=True,
            )
            st.caption(
                f"{row['units'] * factor:,.4f} units "
                f"(simulated {row['units']:,.4f})"
            )
            if not np.isfinite(row["stop_level"]):
                st.caption(
                    "⚠ No stop-loss level — either stop-losses are disabled, "
                    "or the average true range had not warmed up when this "
                    "position opened."
                )
        with levels:
            components.render_kv(
                st,
                {
                    "Entry price": components.number(row["entry_price"], 4),
                    "Latest price": components.number(row["last_price"], 4),
                    "Stop-loss level": components.number(row["stop_level"], 4),
                    "Take-profit level": components.number(
                        row["take_profit_level"], 4
                    ),
                },
            )
        with money:
            components.render_kv(
                st,
                {
                    "Unrealised profit & loss": components.money(
                        row["unrealized_pnl"] * factor
                    ),
                    "Exposure": components.money(row["notional"] * factor),
                    "Leverage": components.number(row["leverage"], 2),
                    "Financing and currency": components.money(
                        (row["financing_accrued"] + row["fx_costs"]) * factor
                    ),
                },
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
