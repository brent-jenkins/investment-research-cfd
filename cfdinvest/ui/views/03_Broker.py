"""Capital.com — the demo account, kept apart from the paper book.

Two books run side by side and they must never be read as one. The paper
book is a simulation at a fixed £1,000 a position, four arms, its fills
modelled. This account holds real orders confirmed by somebody with no stake
in the answer, sized from the balance.

The point of showing them separately is that the *difference* between them
is a measurement — how wrong the simulation's fills and costs are — and a
page that pooled them would destroy the only thing they jointly measure.

Everything here is read live from the broker at page load. That is
deliberate: a position is a fact about now, and a cached one is a claim
about a moment that has passed. It also means this page is the only one that
can be slow, and the only one that fails when the network does — so every
failure is named rather than rendered as an empty table, because an account
holding nothing and an account that could not be read look identical and
only one of them is calm.
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
    "Broker",
    icon="🏦",
    subtitle="The Capital.com demo account: what it holds, what it is worth, "
    "and what a trade costs there. Separate from the paper book, and "
    "deliberately so.",
)

components.render_note(
    st,
    "This is a **demo** account. Nothing here is real money, and the app "
    "refuses to reach a live one without a second explicit setting. The "
    "paper book runs alongside it and is not the same thing — the gap "
    "between them is what says how good the simulation is.",
)


@st.cache_data(ttl=30, show_spinner=False)
def _read_broker() -> dict:
    """Balance, positions and costs, read live. Never raises.

    Thirty seconds of caching so a page refresh does not hammer the API, and
    no longer, because the whole point is that it says what is true now.
    Returns a plain dictionary — nothing callable, which is what breaks the
    cache and takes the rest of the page down with it.
    """
    out: dict = {
        "configured": False, "error": "", "account": None,
        "positions": [], "positions_error": "", "markets": [],
    }
    try:
        from cfdinvest.broker.capital import client as capital
        from cfdinvest.broker.capital import dealing
        from cfdinvest.config import load_config

        settings = capital.CapitalConfig.from_env()
        out["environment"] = settings.environment
        if not settings.complete:
            out["error"] = (
                "No Capital.com credentials in the environment, so nothing "
                "can be read. This is not a failure — the account is "
                "optional — but it is also not an empty account."
            )
            return out
        out["configured"] = True

        session = capital.open_session(settings)
        if not session.ok:
            out["error"] = f"Could not authenticate: {session.error}"
            return out

        account = capital.fetch_account(session, settings)
        out["account"] = {
            "id": account.account_id, "currency": account.currency,
            "balance": account.balance, "available": account.available,
            "profit_loss": account.profit_loss,
        }

        found, error = dealing.list_positions(session, settings)
        out["positions_error"] = error
        out["positions"] = [
            {
                "Instrument": p.epic, "Side": p.direction, "Size": p.size,
                "Opened at": p.level, "Unrealised": p.upl, "Ccy": p.currency,
                "Deal id": p.deal_id,
            }
            for p in found
        ]

        tickers = list(load_config().market.tickers or ())
        markets = capital.fetch_market_details(tickers, session, settings)
        out["markets"] = [
            {
                "Instrument": m.ticker,
                "Bid": m.bid, "Offer": m.offer,
                "Spread bp": None if m.spread_pct is None else m.spread_pct * 100,
                "Min size": m.min_deal_size,
                "Status": "tradeable" if m.tradable else "closed",
            }
            for m in markets
        ]
    except Exception as error:  # noqa: BLE001 - a page must render
        out["error"] = f"{type(error).__name__}: {error}"
    return out


broker = _read_broker()

if not broker.get("configured"):
    components.render_note(st, broker.get("error") or "Not configured.", tone="warn")
    st.stop()
if broker.get("error"):
    components.render_note(st, broker["error"], tone="warn")
    st.stop()

# --------------------------------------------------------------------------- #
# The account
# --------------------------------------------------------------------------- #
components.render_section(
    st, "The account",
    "Read live. Every limit in the app is a ratio applied to this balance, "
    "not a figure written down — so this number is what sizes positions.",
)

account = broker["account"] or {}
currency = account.get("currency") or ""


def _money(value) -> str:
    return "—" if value is None else f"{currency} {value:,.2f}"


committed = None
if account.get("balance") is not None and account.get("available") is not None:
    committed = account["balance"] - account["available"]

components.render_tiles(st, [
    ("Balance", _money(account.get("balance")), "what sizing is a fraction of"),
    ("Available", _money(account.get("available")), "after committed margin"),
    ("Margin in use", _money(committed), "across every open position"),
    ("Profit and loss", _money(account.get("profit_loss")), "since the account opened"),
])

# --------------------------------------------------------------------------- #
# Positions
# --------------------------------------------------------------------------- #
components.render_section(
    st, "Open positions",
    "What the broker says is open, not what the app believes it opened. "
    "Those differing is the thing worth noticing.",
)

if broker.get("positions_error"):
    components.render_note(
        st,
        f"The position list could not be read: {broker['positions_error']}. "
        "That is not an empty account — it is an unanswered question, and "
        "only one of those means it is safe to open more.",
        tone="warn",
    )
elif not broker["positions"]:
    components.render_note(
        st, "The account holds nothing. Read successfully, and empty."
    )
else:
    st.dataframe(
        pd.DataFrame(broker["positions"]), width="stretch", hide_index=True
    )

# --------------------------------------------------------------------------- #
# What a trade costs here
# --------------------------------------------------------------------------- #
components.render_section(
    st, "What a trade costs, right now",
    "The spread is the largest line in the cost model. It was one constant "
    "of 10bp for every instrument until it was measured; the median is "
    "about half that, and it varies by a factor of ten across the universe.",
)

markets = pd.DataFrame(broker["markets"])
if markets.empty:
    components.render_note(st, "No instrument could be priced.", tone="warn")
else:
    priced = markets[markets["Spread bp"].notna()]
    if not priced.empty:
        components.render_tiles(st, [
            ("Median spread", f"{priced['Spread bp'].median():.1f} bp", "across the universe"),
            ("Cheapest", f"{priced.loc[priced['Spread bp'].idxmin(), 'Instrument']}"
                         f" {priced['Spread bp'].min():.1f} bp", ""),
            ("Dearest", f"{priced.loc[priced['Spread bp'].idxmax(), 'Instrument']}"
                        f" {priced['Spread bp'].max():.1f} bp", ""),
            ("Assumed by the model", "10.0 bp", "round trip, every instrument"),
        ])
    st.dataframe(
        markets.sort_values("Spread bp", na_position="last"),
        width="stretch", hide_index=True,
    )
    components.render_note(
        st,
        "Outside the exchange session these widen by a median of 1.85× — so "
        "a reading taken now, with the market shut, describes trading the "
        "app deliberately does not do.",
    )
