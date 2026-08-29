"""Dashboard — what the paper book has actually done.

The forward record is the point of this page. Every measurement before it
was made backwards over a universe chosen in 2026 for having gone up, and
the strategy trades a horizon of days; a backtest on that universe cannot
settle whether it works, and a few weeks of trades recorded before the
outcome was known can.

Four arms run side by side, on the same signals and the same prices, each
with £1,000 of exposure. The first is the bar the engine actually enters on;
the rest are raised, to test whether raising it helps. They are shown
separately and never pooled into the headline, because the whole reason for
running several is to compare them.

The page reads the arms from the book rather than naming them. It used to
say "three at 0.50, 0.60 and 0.70", which stopped being true the day the
baseline arm was added and nobody noticed -- on the page a person reads to
reconcile against their broker.

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

from cfdinvest.advisor import papertrade  # noqa: E402
from cfdinvest.advisor.papertrade_run import cohorts_for  # noqa: E402
from cfdinvest.config import load_config  # noqa: E402
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
        "expectation is a few trades a week across the arms, not a few "
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
# The arms
# --------------------------------------------------------------------------- #
st.subheader("The arms")
components.render_note(
    st,
    f"Each takes £{stake:,.0f} of exposure — notional, not margin — so "
    "leverage cannot silently vary the stake. They are never pooled into a "
    "verdict: the comparison *is* the experiment.",
)

# Read from the arms the book actually runs, not written out here. The
# hardcoded version listed c50, c60 and c70 -- so the baseline arm, the one
# the engine enters on and the only one the trade mail carries, rendered
# with its bare cohort name and no bar at all. The arm being acted on was
# the one the page could not label.
BARS = {
    arm.name: f"≥ {arm.threshold:.2f}" for arm in cohorts_for(load_config().strategy)
}

if not cohorts:
    components.render_note(st, "No arm has opened a position yet.")
else:
    # One table, not three boxes of key-values. The arms exist to be read
    # against each other and a reader comparing three separate panels is
    # doing in their head what a row alignment does for free.
    rows = []
    for arm in cohorts:
        name = str(arm.get("cohort") or "")
        rate = arm.get("win_rate")
        provisional = rate is None and arm.get("rate_so_far") is not None
        rows.append({
            "Bar": BARS.get(name, name),
            "Open": arm.get("open", 0),
            "Closed": arm.get("closed", 0),
            "Won": arm.get("wins", 0),
            "Lost": arm.get("losses", 0),
            "Win rate": (
                f"{rate:.0f}%" if rate is not None
                else f"{arm['rate_so_far']:.0f}%*" if provisional
                else components.DASH
            ),
            "Net P&L": (
                components.money(arm.get("net_gbp"), "£")
                if arm.get("net_gbp") is not None else components.DASH
            ),
            "Avg hours": (
                f"{arm['avg_days_held'] * 24:.1f}"
                if arm.get("avg_days_held") is not None else components.DASH
            ),
            "Holding": ", ".join(arm.get("open_tickers") or []) or "—",
        })

    components.render_frame(st, pd.DataFrame(rows))

    if any(
        arm.get("win_rate") is None and arm.get("rate_so_far") is not None
        for arm in cohorts
    ):
        st.caption(
            f"\\* provisional — fewer than {floor} closed trades, so the rate "
            "is shown but not claimed."
        )

    unpriced = sum(int(arm.get("unpriced") or 0) for arm in cohorts)
    if unpriced:
        st.caption(
            f":orange[{unpriced} closed trade(s) could not be priced and are "
            "excluded from every total.]"
        )

st.divider()

# --------------------------------------------------------------------------- #
# Open positions
# --------------------------------------------------------------------------- #
st.subheader("Open positions")

book = value.get("book") or []
quotes_source = data.get("quotes")
quotes_value = getattr(quotes_source, "value", None) or {}
prices = quotes_value.get("quotes") or {}
age = quotes_value.get("age_minutes")
taken_at = quotes_value.get("taken_at")

# How old the marks are, said before the numbers rather than after. Quotes
# are polled every few minutes, so every value below is behind the market;
# a figure shown without its age invites being read as live, and on a
# leveraged book that is the reading that costs money.
if age is None:
    components.render_note(
        st,
        ":orange[**These marks are not live.** No quote timestamp is "
        "available, so how far behind the market they are is unknown — "
        "which is worse than knowing they are stale.]",
    )
elif quotes_value.get("stale"):
    components.render_note(
        st,
        f":red[**Quotes are {age:.0f} minutes old**, past the "
        f"{quotes_value.get('limit_minutes', 15):.0f}-minute limit. The "
        "values below are marks against a price the market has moved on "
        "from. Treat them as indicative only.]",
    )
else:
    st.caption(
        f"Marked at {components.clock(taken_at)} — **{age:.0f} minutes ago**. "
        "Quotes are polled on a cycle, so every figure here is that far "
        "behind the market."
    )

marks = []
for arm in cohorts:
    marks.extend(
        papertrade.mark_open_positions(
            book, prices, str(data.get("now") or ""), str(arm.get("cohort") or "")
        )
    )

if not marks:
    components.render_note(st, "Nothing open.")
else:
    frame = pd.DataFrame(marks)

    # The headline: what the open book is worth against what was staked.
    priced = frame[frame["priced"].fillna(False).astype(bool)]
    unpriced = len(frame) - len(priced)

    if len(priced):
        staked = priced["notional_gbp"].sum()
        worth = priced["value_gbp"].sum()
        open_pnl = priced["net_pnl_gbp"].sum()
        cells = st.columns(4)
        cells[0].metric("Staked", components.money(staked, "£"))
        cells[1].metric(
            "Current value", components.money(worth, "£"),
            delta=components.money(open_pnl, "£"),
        )
        cells[2].metric(
            "Open P&L", components.money(open_pnl, "£"),
            delta=f"{open_pnl / staked * 100:+.2f}%" if staked else None,
            delta_color="off",
        )
        cells[3].metric("Margin committed", components.money(
            priced["margin_gbp"].sum(), "£"
        ))

    if unpriced:
        components.render_note(
            st,
            f":orange[{unpriced} open position(s) could not be priced and are "
            "excluded from the totals above.] A position that exists and "
            "cannot be valued is the one most worth seeing, so it is still "
            "listed below.",
        )

    shown = frame.rename(columns={
        "entry_price": "entry", "price": "now", "net_pnl_gbp": "open P&L",
        "net_pct": "open %", "value_gbp": "value", "notional_gbp": "staked",
        "financing_gbp": "financing", "days_held": "days",
    })
    wanted = [c for c in (
        "cohort", "ticker", "direction", "confidence", "entry", "now",
        "staked", "value", "open P&L", "open %", "financing", "days",
        "margin_gbp",
    ) if c in shown.columns]
    components.render_frame(st, shown[wanted].sort_values(["cohort", "ticker"]))
    components.render_note(
        st,
        "`open P&L` is after everything the position has cost so far — the "
        "spread paid on entry, financing accrued to the mark, and the "
        "conversion fee on the result. It is the same calculation that will "
        "close the trade, so the figure will not jump when it does.",
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
