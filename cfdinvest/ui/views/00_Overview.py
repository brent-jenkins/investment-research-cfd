"""What to do now.

The first page, and the only one that tries to answer a question rather than
present a subject. Three things, in the order they matter:

1. **Anything wrong** — a system fault means every number below it is
   suspect, so it goes first.
2. **Open positions** — what you are already carrying, priced now. This is
   the live half: the quote poller runs every five minutes, so a position
   walking towards its stop shows it here today.
3. **Trades to consider** — the suggestions clearing your confidence bar,
   with the amount to place.

The two clocks are labelled on the page on purpose. Suggestions come from
daily bars and change once a day; positions are priced every five minutes.
A suggestion list that *looks* live but refreshes daily is worse than one
labelled daily, because an absence would read as an all-clear.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parents[3])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from cfdinvest.livehelper import t212_instruments  # noqa: E402
from cfdinvest.schema import TIMESTAMP  # noqa: E402
from cfdinvest.advisor import actions
from cfdinvest.ui import components, explain  # noqa: E402

components.render_crumb(st, "Overview")
data = components.page_setup(
    st,
    "What to do now",
    icon="📈",
    subtitle="Suggestions come from daily bars and change once a day. Open "
    "positions are priced every five minutes.",
)
now = data["now"]
mode = data["mode"]
health = data["health"]
balance = float(st.session_state.get(components.BALANCE_KEY, components.DEFAULT_BALANCE))

# --------------------------------------------------------------------------- #
# What is being carried, and what is being offered
# --------------------------------------------------------------------------- #
book = components.confirmed_book(data)
prices = components.live_prices(data, list(book) or None)
watches = actions.positions_to_watch(book, prices=prices)

threshold = st.session_state.get("cfd_confidence", actions.DEFAULT_CONFIDENCE)
latest_rows, as_of = components.latest_signal_rows(data)
suggestions, below = actions.suggested_trades(
    latest_rows,
    balance=balance,
    threshold=threshold,
    prices=components.live_prices(data, [r.get("ticker") for r in latest_rows]),
    mapping=t212_instruments.mapping_for,
)

attention = [watch for watch in watches if watch.needs_attention]
failing = list(health.triggered)

components.render_tiles(st, [
    ("Needs attention", len(attention),
     "open positions" if attention else "nothing to deal with",
     "critical" if attention else "pass"),
    ("Trades to consider", len(suggestions),
     f"at {threshold:.0%} confidence or better",
     "pass" if suggestions else "muted"),
    ("Open positions", len(watches),
     "confirmed by you" if watches else "none confirmed",
     "pass"),
    ("System", "Problem" if failing else "Healthy",
     f"{len(failing)} check(s) failing" if failing else "all checks passing",
     "critical" if health.show_banner else "warning" if failing else "pass"),
])

# --------------------------------------------------------------------------- #
# 1. Anything wrong
# --------------------------------------------------------------------------- #
if failing:
    st.divider()
    st.subheader("Before you act")
    st.caption(
        "A system fault makes every number below it suspect. Deal with this "
        "first, or treat the rest as unverified."
    )
    for check in failing:
        st.markdown(
            f"**{check.title}** — {check.detail}  \n"
            + (f"*What to do:* {check.remedy}" if check.remedy else "")
        )

# --------------------------------------------------------------------------- #
# 2. Open positions, priced now
# --------------------------------------------------------------------------- #
st.divider()
st.subheader("Your open positions")

if not watches:
    components.render_note(
        st,
        "Nothing is confirmed open. Record a trade on **Confirm Trades** "
        "after you place it and it will be watched here — priced every five "
        "minutes, with a warning as it approaches its stop.",
    )
else:
    quoted = [w for w in watches if w.price_source.startswith("live quote")]
    st.caption(
        f"{len(quoted)} of {len(watches)} priced from a live quote; the rest "
        "from the most recent daily close."
    )
    for watch in watches:
        with st.container(border=True):
            left, middle, right = st.columns([1.4, 1, 1.2])
            with left:
                name = t212_instruments.mapping_for(watch.ticker)
                st.markdown(
                    f"#### {watch.ticker} &nbsp; "
                    f"{components.direction_badge('long' if watch.quantity > 0 else 'short', mode)}",
                    unsafe_allow_html=True,
                )
                if name.tradable:
                    st.caption(f"Trading 212: {name.name}")
                if watch.note:
                    st.caption(watch.note)
            with middle:
                components.render_kv(st, {
                    "Quantity": components.number(watch.quantity, 4),
                    "Entry": components.price(watch.entry),
                    "Now": components.price(watch.price),
                })
            with right:
                components.render_kv(st, {
                    "Unrealised": components.money(watch.pnl),
                    "Move": components.percent(watch.pnl_fraction),
                    "Priced from": watch.price_source or components.DASH,
                })

# --------------------------------------------------------------------------- #
# 3. Trades to consider
# --------------------------------------------------------------------------- #
st.divider()
st.subheader("Trades to consider")

st.slider(
    "Minimum confidence",
    min_value=0.0, max_value=1.0, step=0.05,
    key="cfd_confidence",
    value=threshold,
    format="%.0f%%",
    help="Confidence is conviction x component agreement x environment "
    "support — three factors multiplied. It is how sure the engine is, "
    "NOT a probability of profit. Most days nothing clears 60%.",
)
st.caption(
    f"Signals as of **{components.moment(as_of)}**, from daily bars. These "
    "change once a day, after the 03:05 UTC run — not intraday."
)

if not suggestions:
    components.render_note(
        st,
        f"**Nothing clears {threshold:.0%} today.**"
        + (f" {below} signal(s) were below your bar and are not shown."
           if below else "")
        + " That is the system being unconvinced, not broken — most days a "
        "trend follower should do nothing.",
    )
else:
    if below:
        st.caption(f"{below} further signal(s) fell below your bar and are hidden.")
    for suggestion in suggestions:
        with st.container(border=True):
            left, middle, right = st.columns([1.4, 1, 1.2])
            with left:
                st.markdown(
                    f"#### {suggestion.ticker} &nbsp; "
                    f"{components.direction_badge(suggestion.direction, mode)}",
                    unsafe_allow_html=True,
                )
                if suggestion.tradable:
                    st.caption(f"**Trading 212:** {suggestion.t212_name}")
                else:
                    st.caption(":orange[Not tradable on Trading 212.]")
                st.caption(explain.reason(suggestion.reason))
            with middle:
                components.render_kv(st, {
                    "Confidence": components.percent(suggestion.confidence, 0),
                    "Of your balance": components.percent(suggestion.size_fraction),
                    "Price": components.price(suggestion.price),
                })
            with right:
                components.render_kv(st, {
                    "Stake": components.money(suggestion.cash),
                    "Quantity": components.number(suggestion.quantity, 4),
                    "Priced from": suggestion.price_source or components.DASH,
                })

    components.render_note(
        st,
        "These are **sizes, not permissions**. Run the trade through the "
        "**Live-Execution Helper** before placing it: that is what checks it "
        "against your risk limits, margin and the broker rules, and it is the "
        "only thing here that can say no.",
    )

# --------------------------------------------------------------------------- #
# Market regime
# --------------------------------------------------------------------------- #
st.divider()
st.subheader("Market regime")
components.render_regime_summary(st, data)
