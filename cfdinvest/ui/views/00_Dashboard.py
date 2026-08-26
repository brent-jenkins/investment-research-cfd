"""Dashboard — what the paper book has actually done.

The forward record is the point of this page. Every measurement before it
was made backwards over a universe chosen in 2026 for having gone up, and
the strategy trades a horizon of days; a backtest on that universe cannot
settle whether it works, and a few weeks of trades recorded before the
outcome was known can.

Three arms run side by side at confidence bars of 0.50, 0.60 and 0.70, on
the same signals and the same prices, each with £1,000 of exposure. They
are shown separately and never pooled into the headline, because the whole
reason for running three is to compare them.

Read-only. Nothing here places a trade, and the Trading 212 account this is
compared against is a virtual one.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parents[3])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from cfdinvest.ui import components  # noqa: E402

data = components.page_setup(
    st,
    "Dashboard",
    icon="📈",
    subtitle="The paper book: what the system committed to before the "
    "outcome was known. No trade here was ever placed.",
)
mode = data["mode"]
paper = data["paper"]

# --------------------------------------------------------------------------- #
# The book
# --------------------------------------------------------------------------- #
if components.render_absent(st, paper, "The paper book"):
    components.render_note(
        st,
        "Signals clear a confidence bar on a minority of days — 395 of 1,125 "
        "bars carried any signal above 0.50 in two years of history — so the "
        "expectation is a few trades a week across all three arms, not a few "
        "a day.",
    )
    st.stop()

value = paper.value or {}
cohorts = [row for row in value.get("cohorts", ()) if row.get("cohort")]
pooled = value.get("pooled") or {}
floor = value.get("min_closed", 20)
stake = value.get("stake", 1000.0)

# --------------------------------------------------------------------------- #
# Headline
# --------------------------------------------------------------------------- #
open_now = pooled.get("open") or 0
closed = pooled.get("closed") or 0

tiles = st.columns(4)
tiles[0].metric("Open positions", f"{open_now}")
tiles[1].metric("Closed trades", f"{closed}")
tiles[2].metric(
    "Net P&L", components.money(pooled.get("net_gbp"), "£")
    if pooled.get("net_gbp") is not None else "—"
)
# The rate is a claim, and a claim needs a sample. Below the floor the
# arithmetic is still shown, greyed, with how far there is to go -- hiding
# it entirely only invites the same sum done worse by hand.
if pooled.get("win_rate") is not None:
    tiles[3].metric("Win rate", components.percent(pooled["win_rate"] / 100, 1))
elif pooled.get("rate_so_far") is not None:
    tiles[3].metric(
        "Win rate",
        f"{pooled['rate_so_far']:.0f}%",
        delta=f"provisional · {pooled.get('needed', 0)} more needed",
        delta_color="off",
    )
else:
    tiles[3].metric("Win rate", "—", delta="nothing closed yet", delta_color="off")

if closed < floor:
    components.render_note(
        st,
        f"**{closed} of {floor} closed trades.** The win rate is shown but not "
        "yet claimed: a rate on a handful of trades is a number that gets "
        "quoted and should not exist. Every figure below carries its sample.",
    )

st.divider()

# --------------------------------------------------------------------------- #
# The three arms
# --------------------------------------------------------------------------- #
st.subheader("The three arms")
components.render_note(
    st,
    f"Each takes £{stake:,.0f} of exposure — notional, not margin — so "
    "leverage cannot silently vary the stake. They are never pooled into a "
    "verdict: the comparison *is* the experiment.",
)

BARS = {"c50": "0.50", "c60": "0.60", "c70": "0.70"}

if not cohorts:
    components.render_note(st, "No arm has opened a position yet.")
else:
    columns = st.columns(len(cohorts))
    for cell, arm in zip(columns, cohorts):
        name = str(arm.get("cohort") or "")
        with cell.container(border=True):
            st.markdown(f"#### Confidence ≥ {BARS.get(name, name)}")

            components.render_kv(
                st,
                {
                    "Open": arm.get("open", 0),
                    "Closed": arm.get("closed", 0),
                    "Won / lost": f"{arm.get('wins', 0)} / {arm.get('losses', 0)}",
                    "Win rate": (
                        components.percent(arm["win_rate"] / 100, 1)
                        if arm.get("win_rate") is not None
                        else (
                            f"{arm['rate_so_far']:.0f}% (provisional)"
                            if arm.get("rate_so_far") is not None
                            else "—"
                        )
                    ),
                    "Net P&L": (
                        components.money(arm.get("net_gbp"), "£")
                        if arm.get("net_gbp") is not None else "—"
                    ),
                    "Avg days held": components.number(arm.get("avg_days_held"), 1),
                },
            )

            if arm.get("open_tickers"):
                st.caption("Holding: " + ", ".join(arm["open_tickers"]))
            if arm.get("unpriced"):
                st.caption(
                    f":orange[{arm['unpriced']} closed trade(s) could not be "
                    "priced and are excluded from every total.]"
                )

st.divider()

# --------------------------------------------------------------------------- #
# Open positions
# --------------------------------------------------------------------------- #
st.subheader("Open positions")

book = value.get("book") or []

# Replayed per cohort, not filtered by ticker. The same instrument may be
# held in all three arms at once -- that is the point of running them
# together -- so matching on the ticker alone would collapse three distinct
# positions into whichever row happened to come last.
from cfdinvest.advisor import papertrade  # noqa: E402

open_rows = []
for arm in cohorts:
    name = str(arm.get("cohort") or "")
    open_rows.extend(papertrade.open_positions(book, name).values())

if not open_rows:
    components.render_note(st, "Nothing open.")
else:
    frame = pd.DataFrame(open_rows)
    wanted = [
        column
        for column in (
            "cohort", "ticker", "direction", "at", "price", "quantity",
            "confidence", "notional_gbp", "margin_gbp", "financing_rate",
            "spread_cost_gbp", "spread_source", "atr_pct", "regime",
        )
        if column in frame.columns
    ]
    components.render_frame(st, frame[wanted].sort_values(["cohort", "ticker"]))
    components.render_note(
        st,
        "`margin_gbp` is what the account would commit; `notional_gbp` is the "
        "£1,000 of exposure. `spread_source` says whether the spread was a "
        "figure Trading 212 published or one estimated from the instrument's "
        "class — an estimated cost and a measured one are different claims.",
    )

st.divider()

# --------------------------------------------------------------------------- #
# Where the money went
# --------------------------------------------------------------------------- #
st.subheader("Where the money went")
components.render_note(
    st,
    "The backtest's central finding was that costs were 10.9× the gross edge "
    "before they were fixed. These are the real ones: financing per side per "
    "instrument, the spread crossed on entry, and the 0.5% conversion fee on "
    "the result where it applies.",
)

if pooled.get("gross_gbp") is None:
    components.render_note(st, "Nothing has closed yet, so nothing has been paid.")
else:
    costs = st.columns(4)
    costs[0].metric("Gross P&L", components.money(pooled.get("gross_gbp"), "£"))
    costs[1].metric("Financing", components.money(pooled.get("financing_gbp"), "£"))
    costs[2].metric("Spread", components.money(pooled.get("spread_gbp"), "£"))
    costs[3].metric("Conversion fee", components.money(pooled.get("fx_fee_gbp"), "£"))

    # The identity, asserted on screen rather than assumed. If these ever
    # disagree the arithmetic is wrong and the page should say so rather
    # than quietly showing two numbers that do not reconcile.
    residual = (
        (pooled.get("gross_gbp") or 0.0)
        - (pooled.get("financing_gbp") or 0.0)
        - (pooled.get("spread_gbp") or 0.0)
        - (pooled.get("fx_fee_gbp") or 0.0)
    )
    net = pooled.get("net_gbp") or 0.0
    if abs(residual - net) > 0.01:
        st.error(
            f"Components do not sum to the total: {residual:+.2f} against a "
            f"recorded net of {net:+.2f}. Treat every figure on this page as "
            "suspect until that is explained."
        )

st.divider()

# --------------------------------------------------------------------------- #
# Closed trades
# --------------------------------------------------------------------------- #
st.subheader("Closed trades")

closed_rows = [row for row in book if row.get("event") == "close"]
if not closed_rows:
    components.render_note(st, "Nothing has closed yet.")
else:
    frame = pd.DataFrame(closed_rows)
    wanted = [
        column
        for column in (
            "cohort", "ticker", "direction", "opened_at", "at", "days_held",
            "confidence", "net_pnl_gbp", "net_pct", "gross_pnl_gbp",
            "financing_gbp", "spread_cost_gbp", "fx_fee_gbp", "reason",
            "signal_reason",
        )
        if column in frame.columns
    ]
    if "at" in frame.columns:
        frame = frame.sort_values("at", ascending=False)
    components.render_frame(st, frame[wanted], height=380)
