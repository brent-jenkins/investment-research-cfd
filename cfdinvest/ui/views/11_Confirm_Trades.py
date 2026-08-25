"""Confirm Trades — tell the system what you actually did.

Everything else in this dashboard assumes the recommendation was followed
exactly. This is the page where that assumption is replaced by a fact.

Two rules shape the layout. Every recommended number is on screen *before*
the form, so no arithmetic happens in your head at the moment you commit to a
figure. And nothing here places an order — it writes down that you did.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parents[3])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from cfdinvest.confirm import Confirmation, write_confirmation  # noqa: E402
from cfdinvest.confirm.recommendation import (  # noqa: E402
    recommendation_for,
    recommended_tickers,
)
from cfdinvest.persist import confirm as confirm_store  # noqa: E402
from cfdinvest.livehelper import ticket as ticket_module
from cfdinvest.ui import components  # noqa: E402

data = components.page_setup(
    st,
    "Confirm Trades",
    icon="✍",
    subtitle="Record the fills you actually got. Everything else assumes the "
    "recommendation was followed exactly — this is where you say otherwise.",
)
config = data["config"]
balance = float(
    st.session_state.get(components.BALANCE_KEY, components.DEFAULT_BALANCE)
)

ACTION_HELP = {
    "open": "You opened a position.",
    "close": "You closed one. The position goes to zero.",
    "flip": "You closed one and opened the other way, in one decision.",
    "ignore": "You saw the recommendation and passed. Not the same as saying "
    "nothing — silence leaves the simulated position standing.",
}

# --------------------------------------------------------------------------- #
# Which instrument
# --------------------------------------------------------------------------- #
try:
    tickers = recommended_tickers(config)
except Exception as error:  # noqa: BLE001 - the page must still let you type
    st.warning(f"Could not read the last run's instruments: {error}")
    tickers = []

# A suggestion picked on the Overview arrives here rather than being
# retyped. It pre-fills the form and nothing more -- what gets recorded is
# still what you tell this page you actually did.
prefill = st.session_state.get(components.PREFILL_KEY) or {}
if prefill:
    st.info(
        f"Pre-filled from the **{prefill.get('ticker')}** suggestion. "
        "Change anything that differs from your actual fill — a partial "
        "fill or a worse price is the normal case, not an error."
    )

# --------------------------------------------------------------------------- #
# Correcting something already recorded
# --------------------------------------------------------------------------- #
# Confirmations are append-only, so nothing here edits history. A correction
# is a *new* record, and the replay is last-write-wins per instrument -- so
# recording the same instrument again with the right numbers fixes the book
# while leaving the trail of what you originally entered intact. That trail
# is the point: it is how you later find out you keep mistyping the size.
open_now = components.confirmed_book({"config": config, "account": None})
if open_now:
    st.markdown("**Open now** — pick one to correct it")
    for name, entry in sorted(open_now.items()):
        held = float(entry.get("quantity") or 0.0)
        row = st.columns([2, 2, 1])
        row[0].markdown(
            f"**{name}** &nbsp; {'short' if held < 0 else 'long'} "
            f"{abs(held):,.4f}",
            unsafe_allow_html=True,
        )
        row[1].caption(
            f"at {components.price(entry.get('price'))} · "
            f"recorded {components.short_moment(entry.get('at'))}"
        )
        if row[2].button("Correct", key=f"cfd_fix_{name}"):
            st.session_state[components.PREFILL_KEY] = {
                "ticker": name,
                "direction": "short" if held < 0 else "long",
                "quantity": abs(held),
                "price": entry.get("price"),
                "currency": entry.get("currency"),
                "fx_rate": entry.get("fx_rate"),
                "leverage": entry.get("leverage"),
                "financing_rate": entry.get("financing_rate"),
            }
            st.rerun()
    st.caption(
        "Correcting records a new confirmation rather than editing the old "
        "one. The book takes the latest; the history keeps both."
    )
    st.divider()

left, right = st.columns([2, 1])
with left:
    if tickers:
        wanted = str(prefill.get("ticker") or "")
        ticker = st.selectbox(
            "Instrument",
            options=tickers or ["—"],
            index=tickers.index(wanted) if wanted in tickers else 0,
            help="Instruments the latest run traded or holds.",
        )
    else:
        ticker = st.text_input("Instrument", value=str(prefill.get("ticker") or ""))
with right:
    action = st.radio("What happened", tuple(ACTION_HELP), horizontal=True)

st.caption(ACTION_HELP[action])

# --------------------------------------------------------------------------- #
# What was recommended
# --------------------------------------------------------------------------- #
st.divider()
st.subheader("What the system recommended")

recommendation = {}
if ticker and ticker != "—":
    try:
        recommendation = recommendation_for(
            ticker, config, account_balance=balance
        )
    except Exception as error:  # noqa: BLE001
        st.warning(f"Could not read the recommendation: {error}")

if not recommendation.get("available"):
    components.render_note(
        st,
        "No run has recommended this instrument. You can still record what "
        "you did — the confirmation simply is not measured against anything.",
    )
else:
    if recommendation["flat"]:
        st.info(
            "The latest run holds **no position** in this instrument. Any "
            "direction shown below is the last signal, not an instruction."
        )

    cells = st.columns(4)
    cells[0].metric(
        "Recommended size",
        components.number(recommendation["scaled_quantity"], 4)
        if recommendation["scale"] is not None
        else components.number(recommendation["quantity"], 4),
        delta=(
            f"simulated {components.number(recommendation['quantity'], 2)}"
            if recommendation["scale"] is not None
            else None
        ),
        delta_color="off",
        help="Scaled to your account balance. The simulation runs at a "
        "different equity, and dividing that in your head at the moment of "
        "trading is where the mistake happens.",
    )
    cells[1].metric(
        "Recommended entry", components.number(recommendation["entry_price"], 4)
    )
    cells[2].metric("Stop", components.number(recommendation["stop_level"], 4))
    cells[3].metric(
        "Take-profit", components.number(recommendation["take_profit_level"], 4)
    )

    cells = st.columns(4)
    cells[0].metric(
        "Exposure",
        components.money(
            recommendation["scaled_exposure"]
            if recommendation["scale"] is not None
            else recommendation["exposure"]
        ),
    )
    cells[1].metric("Leverage", components.number(recommendation["leverage"], 1))
    cells[2].metric("Exchange rate", components.number(recommendation["fx_rate"], 4))
    cells[3].metric(
        "Confidence", components.percent(recommendation["confidence"])
    )

    if recommendation["costs"]:
        with st.expander("Costs the simulation charged", expanded=False):
            components.render_kv(
                st,
                {
                    name.replace("_", " ").capitalize(): components.money(value)
                    for name, value in recommendation["costs"].items()
                },
            )

    if recommendation["absent"]:
        components.render_note(
            st,
            "This run did not record: "
            f"**{', '.join(recommendation['absent'])}**. Blank above means "
            "not recorded, not zero.",
        )

# --------------------------------------------------------------------------- #
# What actually happened
# --------------------------------------------------------------------------- #
st.divider()
st.subheader("What actually happened")

with st.form("confirmation"):
    if action == "ignore":
        price = quantity = close_price = None
        direction = None
    elif action == "close":
        price = st.number_input(
            "Fill price you got", min_value=0.0, value=0.0, format="%.4f"
        )
        quantity = close_price = None
        direction = None
    else:
        cells = st.columns(3)
        default_direction = (
            0 if (prefill.get("direction")
                  or recommendation.get("direction") or "long") == "long" else 1
        )
        direction = cells[0].radio(
            "Direction", ("long", "short"), index=default_direction,
            horizontal=True,
        )
        quantity = cells[1].number_input(
            "Quantity you took", min_value=0.0,
            value=float(
                prefill.get("quantity")
                or recommendation.get("scaled_quantity") or 0.0
            ),
            format="%.4f",
            help="Prefilled with the recommendation, scaled to your balance. "
            "Change it to whatever you actually got — a partial fill is a "
            "normal thing to record.",
        )
        price = cells[2].number_input(
            "Fill price you got", min_value=0.0,
            value=float(
                prefill.get("price") or recommendation.get("entry_price") or 0.0
            ),
            format="%.4f",
        )
        close_price = (
            st.number_input(
                "Price the closing leg filled at", min_value=0.0, value=0.0,
                format="%.4f",
            )
            if action == "flip"
            else None
        )

    # --- the ticket's own terms ------------------------------------------
    # Recorded because they are true of *this fill*. An instrument's
    # leverage can change, and a position opened last month was opened on
    # last month's terms -- storing them beside the fill is what lets the
    # GBP figures be recomputed later without guessing.
    st.markdown("**Ticket terms** — from the Trading 212 order screen")
    terms = st.columns(4)
    currency = terms[0].text_input(
        "Priced in", value=str(prefill.get("currency") or "USD"),
        help="The instrument's currency. A US share CFD is priced in dollars "
        "even though your account is sterling.",
    ).strip().upper() or "GBP"
    fx_rate = terms[1].number_input(
        "GBP per 1 unit", min_value=0.0,
        value=float(prefill.get("fx_rate") or 0.0), format="%.4f",
        help="GBP per one unit of the instrument's currency — 0.79 GBP/USD, "
        "not 1.27 USD/GBP. Upside down misstates every GBP figure.",
    )
    leverage = terms[2].number_input(
        "Leverage", min_value=0.0,
        value=float(prefill.get("leverage") or 0.0), format="%.1f",
        help="What the ticket shows for this instrument. Left at zero it is "
        "recorded as unknown rather than assumed — margin would be wrong "
        "by a multiple.",
    )
    financing_rate = terms[3].number_input(
        "Overnight rate (annual)", min_value=0.0,
        value=float(prefill.get("financing_rate") or 0.0), format="%.4f",
        help="As a fraction: 0.0295 is 2.95% a year on the notional.",
    )

    notes = st.text_input("Notes", value="", placeholder="optional")
    submitted = st.form_submit_button("Record this", type="primary")

# The ticket as it will read, worked out from what is typed above.
if quantity and price:
    worked = ticket_module.build_ticket(
        ticker if ticker != "—" else "?", direction, quantity, price,
        currency=currency, fx_rate=fx_rate or None,
        leverage=leverage or None, financing_rate=financing_rate or None,
    )
    components.render_kv(st, worked.describe())
    if worked.missing:
        st.caption(
            ":orange[Not computed: " + ", ".join(worked.missing) + ". "
            "They are recorded as unknown rather than assumed.]"
        )
    else:
        example = ticket_module.close_out(worked, price, days_held=30)
        st.caption(
            f"Held 30 days at this price, financing would cost "
            f"{components.money(example.financing_gbp)}. A 0.5% FX fee "
            "applies to the profit or loss when you close."
        )

if submitted:
    if not ticker or ticker == "—":
        st.error("Choose an instrument first.")
    else:
        try:
            confirmation = Confirmation.of(
                ticker,
                action,
                direction=direction,
                quantity=quantity or 0.0,
                price=price or None,
                close_price=close_price or None,
                notes=notes or None,
                run_id=recommendation.get("run_id"),
                recommended_price=recommendation.get("entry_price"),
                recommended_quantity=(
                    recommendation.get("scaled_quantity")
                    if recommendation.get("scale") is not None
                    else recommendation.get("quantity")
                ),
                source="dashboard",
                currency=currency or None,
                fx_rate=fx_rate or None,
                leverage=leverage or None,
                financing_rate=financing_rate or None,
            )
            path = write_confirmation(confirmation, config)
        except (ValueError, OSError) as error:
            # Refused at the door on purpose: a wrong fill price does not make
            # a suspicious number downstream, it makes a wrong one that looks
            # exactly as authoritative as a right one.
            st.error(f"**Not recorded.** {error}")
        else:
            # Consumed. Left in place it would re-fill the form on the next
            # trade, quietly offering yesterday's numbers for today's fill.
            st.session_state.pop(components.PREFILL_KEY, None)
            st.success(
                f"Recorded: {confirmation.action} {confirmation.ticker}"
                + (
                    f" — {confirmation.direction} "
                    f"{abs(confirmation.quantity):,.4f} at "
                    f"{confirmation.price:,.4f}"
                    if confirmation.quantity
                    else ""
                )
            )
            if confirmation.slippage is not None:
                worse = "worse than" if confirmation.slippage > 0 else "better than"
                st.caption(
                    f"{abs(confirmation.slippage):,.4f} {worse} the recommended "
                    "fill."
                )
            if confirmation.quantity_deviation is not None:
                st.caption(
                    f"{confirmation.quantity_deviation:.1%} of the recommended "
                    "size."
                )
            st.caption(f"Written to `{path}`")

# --------------------------------------------------------------------------- #
# What has been confirmed
# --------------------------------------------------------------------------- #
st.divider()
st.subheader("Confirmed so far")

try:
    confirmations = confirm_store.load_confirmations(config)
except Exception as error:  # noqa: BLE001
    st.warning(f"Could not read the confirmations: {error}")
    confirmations = []

if not confirmations:
    components.render_note(
        st,
        "Nothing has been confirmed. That is not the same as no trades — "
        "every page is still showing the **simulated** positions, because "
        "nothing has contradicted them.",
    )
else:
    book = confirm_store.confirmed_positions(confirmations)
    open_now = {k: v for k, v in book.items() if v["quantity"]}

    cells = st.columns(3)
    cells[0].metric("Confirmations recorded", len(confirmations))
    cells[1].metric("Instruments held", len(open_now))
    cells[2].metric(
        "Last confirmed",
        components.clock(confirmations[-1].timestamp),
    )

    frame = confirm_store.to_frame(confirmations)
    components.render_frame(
        st,
        frame[
            [
                c
                for c in (
                    "timestamp", "ticker", "action", "direction", "quantity",
                    "price", "exposure", "slippage", "quantity_deviation",
                    "source", "notes",
                )
                if c in frame.columns
            ]
        ].iloc[::-1],
    )

    drift = confirm_store.deviations(confirmations)
    if not drift.empty:
        with st.expander("How far the fills drifted from the recommendations"):
            components.render_note(
                st,
                "Slippage is signed against the position: **positive is worse "
                "than recommended** for a long and a short alike.",
            )
            components.render_frame(st, drift.iloc[::-1])

components.render_note(
    st,
    "Confirmations are append-only. To correct one, record the correction — "
    "the original stays, because it is what you believed at the time.",
)
