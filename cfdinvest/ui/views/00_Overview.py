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

# Held as whole percent, because that is what the slider shows. A slider
# spanning 0.0-1.0 with a "%.0f%%" format renders the raw fraction rounded,
# so every value from 0.5 up read "1%" -- the label was meaningless and the
# number it implied was a hundredth of the real bar.
percent = int(st.session_state.get(
    "cfd_confidence_pct", round(actions.DEFAULT_CONFIDENCE * 100)
))
threshold = percent / 100.0
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
# The market you are trading into
# --------------------------------------------------------------------------- #
# Above everything, because it is the context every line below is read in:
# a short suggestion in a risk-off market means something different from the
# same one in a rally, and you should know which before reading the list.
st.markdown("##### Market regime")
components.render_regime_summary(st, data)

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
                    f"#### {t212_instruments.display_name(watch.ticker)} &nbsp; "
                    f"{components.direction_badge('long' if watch.quantity > 0 else 'short', mode)}",
                    unsafe_allow_html=True,
                )
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
    min_value=0, max_value=100, step=5,
    key="cfd_confidence_pct",
    format="%d%%",
    help="Confidence is conviction x component agreement x environment "
    "support — three factors multiplied. It is how sure the engine is, "
    "NOT a probability of profit. Most days nothing clears 60%.",
)
st.caption(
    f"Signals as of **{components.moment(as_of)}**, from daily bars. These "
    "change once a day, after the 03:05 UTC run — not intraday."
)

if not suggestions:
    # Two different silences, and they used to read identically. The engine
    # multiplies confidence by the environment multiplier, which is 0.5 in
    # high volatility -- roughly one bar in three. On those days nothing can
    # score above 0.5 however strong the trend is, so a bar set above that
    # rejects every signal by construction. Telling you the engine was
    # "unconvinced" then is a reassurance about a question it never got to
    # answer.
    ceiling = actions.confidence_ceiling(latest_rows)
    unreachable = ceiling is not None and ceiling < threshold and below

    if unreachable:
        components.render_note(
            st,
            f"**Nothing can clear {threshold:.0%} today.** The strongest "
            f"signal available scores {ceiling:.0%}, so your bar rejects "
            f"all {below} of them before their merits are considered. "
            "Confidence is scaled by the market environment — in high "
            "volatility it is halved, capping it at 50% — so a bar above "
            "that number is a blackout rather than a filter. Lower it to "
            "see what is actually on offer.",
            tone="warning",
        )
    else:
        components.render_note(
            st,
            f"**Nothing clears {threshold:.0%} today.**"
            + (f" {below} signal(s) were below your bar and are not shown."
               if below else "")
            + " That is the system being unconvinced, not broken — most days "
            "a trend follower should do nothing.",
        )
else:
    if below:
        st.caption(f"{below} further signal(s) fell below your bar and are hidden.")

    # A table, not cards. These are compared against each other -- which is
    # the strongest, which needs the biggest stake -- and comparison down a
    # column is what a table is for. The reasons are long and go underneath.
    #
    # Direction carries an arrow as well as a colour: an arrow survives a
    # colourblind reader and a phone screen, and the colour is doing the
    # scanning work for everyone else.
    # Sentiment sits beside each suggestion and decides nothing. It cannot
    # be backtested -- the feed carries days, not years -- so it never
    # reaches sizing or the confidence gate. What it is for is the moment
    # before you place the trade: the engine has read the price and has no
    # way to know the company cut guidance last night.
    #
    # Read once per instrument and cached, because it is a network call per
    # ticker and this page reruns on every interaction.
    readings = components.sentiment_readings(
        st, [s.ticker for s in suggestions]
    )

    table = pd.DataFrame([
        {
            "": "📉" if suggestion.direction == "short" else "📈",
            # The Trading 212 name leads because it is what you type into
            # the app; the lake symbol follows because it is what every
            # other page here calls the same thing.
            "Instrument": t212_instruments.display_name(suggestion.ticker),
            "Direction": suggestion.direction,
            # Whole percent, not a fraction: a ProgressColumn formats the
            # raw value, so 0.80 with "%.0f%%" renders "1%" -- the same
            # trap the slider had.
            "Confidence": suggestion.confidence * 100.0,
            "News": components.sentiment_badge(readings.get(suggestion.ticker)),
            "Stake": suggestion.cash,
            "Quantity": suggestion.quantity,
            "Price": suggestion.price,
            "Priced from": (
                "live quote" if suggestion.price_source.startswith("Using live")
                else "daily bar"
            ),
        }
        for suggestion in suggestions
    ])

    chosen = st.dataframe(
        components.style_states(table, mode),
        width="stretch",
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key="cfd_suggestion_pick",
        column_config={
            "": st.column_config.TextColumn("", width="small"),
            "Confidence": st.column_config.ProgressColumn(
                "Confidence", min_value=0, max_value=100, format="%d%%",
                help="How sure the engine is. NOT a probability of profit.",
            ),
            "News": st.column_config.TextColumn(
                "News",
                width="small",
                help=(
                    "Recent headlines, scored by keyword. Headlines that "
                    "merely report the price move are excluded — they "
                    "describe the past and predict nothing. This has no "
                    "history to be tested against, so it decides nothing "
                    "and sizes nothing. It is here for you to read."
                ),
            ),
            "Stake": st.column_config.NumberColumn(
                "Stake", format=f"{components.CURRENCY}%.2f"
            ),
            "Quantity": st.column_config.NumberColumn("Quantity", format="%.4f"),
            "Price": st.column_config.NumberColumn("Price", format="%.4f"),
        },
    )

    picked = (chosen.selection.rows or [None])[0] if chosen else None
    if picked is not None and picked < len(suggestions):
        suggestion = suggestions[picked]
        st.markdown(
            f"**{suggestion.ticker}** selected — "
            f"{suggestion.direction} {components.number(suggestion.quantity, 4)} "
            f"at about {components.price(suggestion.price)}"
        )

        reading = readings.get(suggestion.ticker)
        if reading is not None and reading.score is not None:
            with st.expander(f"What is being said — {reading.describe()}"):
                components.render_note(
                    st,
                    "Scored by keyword, not judgement. It has no track "
                    "record here and is not part of any calculation — the "
                    "call is yours.",
                )
                for headline in reading.headlines:
                    if headline.price_report:
                        st.caption(f"↷ {headline.title}  *(reports the price)*")
                    else:
                        mark = {1: "▲", -1: "▼"}.get(headline.score, "·")
                        st.markdown(f"{mark} {headline.title}")
        if st.button("Log this trade", type="primary", key="cfd_log_pick"):
            # Hand the numbers over rather than making them be retyped. This
            # records what you did; it does not place anything.
            st.session_state[components.PREFILL_KEY] = {
                "ticker": suggestion.ticker,
                "direction": suggestion.direction,
                "quantity": suggestion.quantity,
                "price": suggestion.price,
            }
            st.switch_page("views/11_Confirm_Trades.py")

    with st.expander("Why each of these", expanded=False):
        for suggestion in suggestions:
            st.markdown(
                f"**{suggestion.ticker}** — {explain.reason(suggestion.reason)}"
            )

    components.render_note(
        st,
        "These are **sizes, not permissions**. Run the trade through the "
        "**Live-Execution Helper** before placing it: that is what checks it "
        "against your risk limits, margin and the broker rules, and it is the "
        "only thing here that can say no.",
    )

