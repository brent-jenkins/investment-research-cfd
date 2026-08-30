"""Dashboard — what the account is doing, and whether the system is keeping up.

This page used to show the paper book's four confidence arms. The book was
removed on 2026-08-30: it implemented the time stop, the cooldown, the
earnings blackout and the confidence gate, and none of the breakout gate,
the size hysteresis, the decayed trim, the leverage cap or the
concentration cap. So it was not a simulation of this strategy but of a
different one, returning 163.7% at -52.8% where the engine returns 130.5%
at -40.3%. A forward record of the wrong strategy is worse than none,
because it invites being read as validation.

The demo account replaces it, and is strictly better evidence: real fills,
real spreads, real financing, confirmed by a counterparty with no stake in
the answer.

**Deliberately a summary, not a second broker page.** ``03_Broker`` already
shows the account in detail -- every position, the balance, what a trade
costs there. Repeating that here would be the duplication this project
keeps paying for. What belongs on a landing page is the shape of things:
is it running, what is it holding, is it up or down, and where to look
next.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parents[3])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from cfdinvest.ui import broker as broker_source  # noqa: E402
from cfdinvest.ui import components  # noqa: E402

data = components.page_setup(
    st,
    "Dashboard",
    icon="📊",
    subtitle="The demo account and the jobs that feed it. Detail lives on "
    "the Broker page; this is the shape of things.",
)


@st.cache_data(ttl=30, show_spinner=False)
def _read_broker() -> dict:
    """The live read, cached briefly so a reload does not re-poll."""
    return broker_source.read_broker()


broker = _read_broker()

components.render_note(
    st,
    "This is a **demo** account. Nothing here is real money, and the app "
    "refuses to reach a live one without a second explicit setting.",
)

# --------------------------------------------------------------------------- #
# The account
# --------------------------------------------------------------------------- #
st.subheader("The account")

account = (broker or {}).get("account") or {}
positions = (broker or {}).get("positions") or []

if not account:
    # Not render_absent: that takes a Source, and this is a live broker read.
    # Absence is still named rather than drawn as zero -- an unreadable
    # account and an empty one are different states.
    st.warning(
        "The demo account could not be read. Nothing is inferred from that: "
        "an unreadable account and an empty one are different states."
    )
else:
    row = st.columns(4)
    balance = account.get("balance")
    equity = account.get("equity")
    profit = account.get("profit")
    row[0].metric("Balance", components.money(balance))
    row[1].metric("Equity", components.money(equity))
    row[2].metric(
        "Open P&L", components.money(profit),
        delta=None if profit is None else f"{profit:+,.2f}",
    )
    row[3].metric("Open positions", len(positions))

# --------------------------------------------------------------------------- #
# What it holds
# --------------------------------------------------------------------------- #
st.subheader("What it holds")

if not positions:
    st.caption(
        "Nothing open. That is a position the system has taken, not a "
        "failure — it opens when a signal clears the gates and not otherwise."
    )
else:
    frame = pd.DataFrame([
        {
            "Instrument": p.get("ticker"),
            "Side": p.get("direction"),
            "Size": p.get("size"),
            "Open": p.get("open_price"),
            "Now": p.get("price"),
            "P&L": p.get("profit"),
        }
        for p in positions
    ])
    st.dataframe(frame, width="stretch", hide_index=True)
    st.caption(
        "Summary only. The Broker page carries the full detail, including "
        "what each instrument costs to trade."
    )

# --------------------------------------------------------------------------- #
# Is it running?
# --------------------------------------------------------------------------- #
st.subheader("Is it running?")

scheduler = (data or {}).get("scheduler")
if scheduler is None or getattr(scheduler, "missing", False):
    st.caption(
        "No scheduler record yet. The Status page reports each job against "
        "the artefact it writes, which survives a restart where a timestamp "
        "in memory would not."
    )
else:
    rows = getattr(scheduler, "value", None) or {}
    overdue = [name for name, ok in (rows.get("overdue") or {}).items() if ok]
    if overdue:
        st.warning(
            "Behind schedule: " + ", ".join(sorted(overdue))
            + ". The Status page says by how much."
        )
    else:
        st.success("Every scheduled job is keeping up.")

st.caption(
    "The paper book was removed on 2026-08-30. It ran a different rule set "
    "from the engine, so the gap between it and the account measured code "
    "divergence rather than simulation error, and could not be read either "
    "way. The demo account's own fills are the forward record now."
)
