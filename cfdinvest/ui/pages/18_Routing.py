"""Routing — which account a trade belongs in, and why.

A suggestion with reasons attached. Nothing here places a trade or moves
money, and nothing routes to the real-money account by default: every other
routing mistake is a bookkeeping error you can correct, and that one is not.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parents[3])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from cfdinvest.accounts import explain_routing, load_registry, route_trade  # noqa: E402
from cfdinvest.livehelper import parse_trade_intent  # noqa: E402
from cfdinvest.ui import components  # noqa: E402

data = components.page_setup(
    st,
    "Routing",
    icon="🔀",
    subtitle="Which account a trade belongs in, and every reason behind it.",
)
config = data["config"]

try:
    registry = load_registry(config)
except Exception as error:  # noqa: BLE001
    st.error(f"**The account registry could not be read.** {error}")
    st.stop()

# --------------------------------------------------------------------------- #
# The trade
# --------------------------------------------------------------------------- #
st.subheader("The trade to route")

cells = st.columns([3, 1])
raw = cells[0].text_input(
    "Trade",
    value="long SPX500 0.5 entry=4434.8 stop=4390",
    help="The same shorthand the live-execution helper takes.",
)
strategy = cells[1].text_input("Strategy", value="", placeholder="optional")

cells = st.columns(3)
regime = cells[0].text_input(
    "Regime",
    value="",
    placeholder="risk_on",
    help="Leave blank to route without a regime — accounts restricted to one "
    "will not match.",
)
prefer = cells[1].selectbox(
    "Force an account", options=["(decide for me)", *registry.names]
)
cells[2].markdown("")

intent = None
if raw.strip():
    try:
        intent = parse_trade_intent(raw)
    except Exception as error:  # noqa: BLE001
        st.warning(f"Could not read that trade: {error}")

if intent is None:
    components.render_note(st, "Enter a trade to route.")
    st.stop()

decision = route_trade(
    intent,
    registry,
    strategy=strategy.strip() or None,
    regime=regime.strip() or None,
    prefer=None if prefer == "(decide for me)" else prefer,
)

st.divider()

# --------------------------------------------------------------------------- #
# Where it goes
# --------------------------------------------------------------------------- #
st.subheader("Where it goes")

if not decision.routed:
    st.error("**Not routed.**")
    for reason in decision.reasons:
        st.markdown(f"- {reason}")
    st.stop()

if decision.is_real_money:
    st.markdown(
        '<div style="background:#C62828;color:#fff;padding:10px 16px;'
        'border-radius:8px;font-weight:700;">'
        f"{decision.label} &nbsp;·&nbsp; REAL MONEY</div>",
        unsafe_allow_html=True,
    )
elif decision.fell_back:
    st.warning(f"**{decision.label}** — by fall-back, not by a match.")
else:
    st.success(f"**{decision.label}**")

cells = st.columns(3)
cells[0].metric("Account", decision.account)
cells[1].metric("Confidence", decision.confidence)
cells[2].metric("Candidates", len(decision.candidates))

for reason in decision.reasons:
    st.markdown(f"- {reason}")

if decision.needs_a_decision:
    st.info(
        "**Worth confirming before you act on this.** Either nothing matched, "
        "several accounts fit equally, or it is the real-money account on a "
        "match that was not clear-cut."
    )

st.divider()

# --------------------------------------------------------------------------- #
# Why
# --------------------------------------------------------------------------- #
st.subheader("Every rule that was applied")

components.render_note(
    st,
    "A router that returned only a name would be impossible to argue with. "
    "Six months from now, looking at a trade in the wrong sleeve, the reasons "
    "are what tell you whether the rule was wrong or the inputs were.",
)

if decision.rules:
    components.render_frame(
        st,
        pd.DataFrame(
            [
                {
                    "account": rule.account,
                    "rule": rule.rule,
                    "matched": rule.matched,
                    "detail": rule.detail,
                }
                for rule in decision.rules
            ]
        ),
    )
else:
    components.render_note(
        st, "No account defines a rule, so there was nothing to apply."
    )

with st.expander("As text"):
    st.code("\n".join(explain_routing(decision)))
