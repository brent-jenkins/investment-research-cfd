"""Live-Execution Helper — check a trade before you place it by hand.

Nothing on this page places an order. It takes what you intend to type into
Trading 212, measures it against your limits, and shows you the arithmetic so
you do not have to do it at the moment of trading.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parents[3])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from cfdinvest.livehelper import (  # noqa: E402
    LiveHelperConfig,
    MarketState,
    build_execution_helper_summary,
    compute_safe_sizing,
    evaluate_trade_safety,
    parse_trade_intent,
)
from cfdinvest.ui import components  # noqa: E402

data = components.page_setup(
    st,
    "Live-Execution Helper",
    icon="🧮",
    subtitle="Check an intended trade against your own limits before you type "
    "it into Trading 212. Nothing here is placed anywhere.",
)
mode = data["mode"]
balance = float(
    st.session_state.get(components.BALANCE_KEY, components.DEFAULT_BALANCE)
)

#: The verdict's colour. Deliberately not the brand palette — a verdict that
#: shares a hue with the page furniture is one nobody sees.
VERDICT_COLOUR = {
    "SAFE": "#2E7D32",
    "BORDERLINE": "#B26A00",
    "UNSAFE": "#C62828",
}

# --------------------------------------------------------------------------- #
# The trade
# --------------------------------------------------------------------------- #
st.subheader("The trade you intend to place")

left, right = st.columns(2)
with left:
    instrument = st.text_input("Instrument", value="SPX500")
    direction = st.radio("Direction", ("long", "short"), horizontal=True)
    quantity = st.number_input(
        "Quantity", min_value=0.0, value=0.68, step=0.01, format="%.4f"
    )
    entry = st.number_input(
        "Entry price (0 for market)", min_value=0.0, value=4434.8, format="%.4f"
    )
with right:
    stop = st.number_input(
        "Stop-loss (0 for none)", min_value=0.0, value=4390.0, format="%.4f"
    )
    target = st.number_input(
        "Take-profit (0 for none)", min_value=0.0, value=4520.0, format="%.4f"
    )
    currency = st.text_input("Instrument currency", value="USD").strip().upper()
    leverage = st.number_input(
        "Instrument leverage (0 if unknown)", min_value=0.0, value=20.0, step=1.0
    )

with st.expander("Market conditions and your limits", expanded=False):
    market_left, market_right = st.columns(2)
    with market_left:
        fx_rate = st.number_input(
            "Exchange rate (account per instrument currency)",
            min_value=0.0, value=0.79, format="%.6f",
            help="GBP per USD, for a sterling account trading a dollar "
            "instrument. Leave at 0 if the instrument is in your own currency.",
        )
        spread = st.number_input(
            "Spread (instrument currency)", min_value=0.0, value=0.0, format="%.4f"
        )
        volatility = st.number_input(
            "Volatility (annualised fraction)", min_value=0.0, value=0.0,
            format="%.3f",
        )
        last_price = st.number_input(
            "Last price (for a market order)", min_value=0.0, value=0.0,
            format="%.4f",
        )
    with market_right:
        used_margin = st.number_input(
            "Margin already committed", min_value=0.0, value=0.0, step=10.0
        )
        max_risk = st.number_input(
            "Most one trade may lose", min_value=1.0, value=100.0, step=10.0
        )
        max_exposure = st.number_input(
            "Exposure limit per instrument", min_value=1.0, value=2_000.0, step=100.0
        )
        max_leverage = st.number_input(
            "Effective leverage limit", min_value=0.1, value=10.0, step=0.5
        )
    flags = st.columns(2)
    near_rollover = flags[0].checkbox("Financing rolls over shortly")
    close_only = flags[1].checkbox("Instrument is close-only")

# --------------------------------------------------------------------------- #
# The assessment
# --------------------------------------------------------------------------- #
try:
    intent = parse_trade_intent(
        {
            "instrument": instrument,
            "direction": direction,
            "quantity": quantity,
            "entry_price": entry or None,
            "stop_loss": stop or None,
            "take_profit": target or None,
            "instrument_currency": currency or "GBP",
            "leverage": leverage or None,
        }
    )
except ValueError as error:
    st.error(f"**That trade cannot be read.** {error}")
    st.stop()

limits = LiveHelperConfig(
    account_balance_gbp=balance,
    used_margin_gbp=used_margin,
    max_risk_per_trade_gbp=max_risk,
    max_exposure_per_instrument_gbp=max_exposure,
    max_leverage_per_trade=max_leverage,
)
market = MarketState(
    last_price=last_price or None,
    spread=spread or None,
    volatility=volatility or None,
    fx_rate=fx_rate or None,
    leverage=leverage or None,
    near_financing_rollover=near_rollover,
    close_only=close_only,
)

assessment = evaluate_trade_safety(intent, limits, market)
sizing = compute_safe_sizing(intent, limits, market)

st.divider()

# --------------------------------------------------------------------------- #
# The verdict
# --------------------------------------------------------------------------- #
colour = VERDICT_COLOUR.get(assessment.status, components.DASH)
st.markdown(
    f'<div style="background:{colour};color:#fff;padding:14px 18px;'
    f'border-radius:8px;font-size:1.3rem;font-weight:700;letter-spacing:.04em;">'
    f"{assessment.status} &nbsp;·&nbsp; {intent.instrument} "
    f"{intent.direction.upper()}</div>",
    unsafe_allow_html=True,
)

actions = list(assessment.recommendations)
if sizing.reduces:
    actions = [a for a in actions if not a.startswith("Reduce quantity")]
    actions.insert(
        0,
        f"Reduce quantity to **{sizing.recommended_quantity:,.4f}** "
        f"(from {intent.quantity:,.4f}).",
    )
if actions:
    st.subheader("What to do")
    for action in actions:
        st.markdown(f"- {action}")

# --------------------------------------------------------------------------- #
# The numbers
# --------------------------------------------------------------------------- #
st.subheader("What it risks")

cells = st.columns(4)
cells[0].metric(
    "Risk to stop",
    components.money(assessment.risk_gbp),
    help=f"Limit {components.money(limits.max_risk_per_trade_gbp)}. This is "
    "what you lose if the stop is reached — the number that matters.",
)
cells[1].metric(
    "Exposure",
    components.money(assessment.exposure_gbp),
    help=f"Limit {components.money(limits.max_exposure_per_instrument_gbp)}",
)
cells[2].metric(
    "Effective leverage",
    components.number(assessment.leverage, 2),
    help="Exposure over account balance — the leverage a smaller position "
    "reduces. The instrument's own leverage is fixed by Trading 212.",
)
cells[3].metric(
    "Margin used after",
    components.percent(assessment.margin_usage_pct_after_trade),
    help=f"Limit {limits.max_margin_usage_pct_after_trade:.0%}",
)

more = st.columns(4)
more[0].metric("Margin required", components.money(assessment.margin_required_gbp))
more[1].metric(
    "Instrument leverage", components.number(assessment.instrument_leverage, 0)
)
more[2].metric(
    "Currency exposure",
    components.percent(assessment.fx_exposure_pct)
    if intent.is_foreign else "domestic",
)
more[3].metric(
    "Reward to risk", components.number(assessment.reward_risk_ratio, 2)
)

conditions = []
if assessment.volatility_flag:
    conditions.append("volatility above your threshold")
if assessment.spread_flag:
    conditions.append("spread wide against the stop distance")
components.render_note(
    st,
    f"**Conditions:** {'; '.join(conditions) if conditions else 'nothing flagged'}",
)

st.divider()

# --------------------------------------------------------------------------- #
# Safe size, reasons and the plain-text block
# --------------------------------------------------------------------------- #
left, right = st.columns(2)

with left:
    st.subheader("Safe size")
    if not sizing.tradeable:
        st.error("No size clears every limit.")
    else:
        components.render_kv(
            st,
            {
                "Recommended quantity": f"{sizing.recommended_quantity:,.4f}",
                "Its exposure": components.money(sizing.recommended_exposure_gbp),
                "Its risk": components.money(sizing.recommended_risk_gbp),
                "Its margin": components.money(sizing.margin_required_gbp),
                "Bound by": sizing.binding_constraint or components.DASH,
            },
        )
    if sizing.ceilings:
        components.render_frame(
            st,
            pd.DataFrame(
                {
                    "limit": list(sizing.ceilings),
                    "largest quantity it allows": list(sizing.ceilings.values()),
                }
            ).set_index("limit"),
        )
    for note in sizing.notes:
        components.render_note(st, note)

with right:
    st.subheader("Why")
    if assessment.reasons:
        for reason in assessment.reasons:
            st.markdown(f"- {reason}")
    else:
        st.success("Every check passed.")

    if assessment.unknowns:
        st.warning(
            f"**Not checked:** {', '.join(assessment.unknowns)}. Supply these "
            "for a complete assessment — until then this cannot be graded SAFE."
        )

st.divider()

with st.expander("The summary as plain text — for email or a terminal", expanded=False):
    st.code(
        build_execution_helper_summary(intent, assessment, sizing, config=limits),
        language="text",
    )

components.render_note(
    st,
    "This page places nothing. There is no broker client in this project, no "
    "Trading 212 endpoint, and no automation of its interface — every figure "
    "here rests on the values you entered above. Check them against the "
    "platform before you act.",
)
