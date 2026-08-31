"""Capital.com — the demo account, which is the forward record.

This account holds real orders confirmed by a counterparty with no stake in
the answer, sized from the balance rather than from a constant. That is the
whole of the forward evidence now: the simulated parallel book this page
used to be contrasted against was removed on 2026-08-30, because it ran a
different rule set from the engine and a disagreement between the two
measured code divergence and fill error inseparably.

So there is nothing to hold this apart *from* any more, and the docstring
that said otherwise outlived the thing it described.

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

from cfdinvest.ui import broker as broker_source  # noqa: E402
from cfdinvest.ui import components  # noqa: E402

data = components.page_setup(
    st,
    "Broker",
    icon="🏦",
    subtitle="The Capital.com demo account: what it holds, what it is worth, "
    "and what a trade costs there. This is the forward record.",
)

components.render_note(
    st,
    "This is a **demo** account. Nothing here is real money, and the app "
    "refuses to reach a live one without a second explicit setting. What it "
    "shows is confirmed by the broker rather than simulated, which is why it "
    "replaced the parallel paper book rather than sitting beside it.",
)


@st.cache_data(ttl=30, show_spinner=False)
def _read_broker() -> dict:
    """The live read, cached for thirty seconds.

    Thirty and no longer: a position is a fact about now, and a cached one is
    a claim about a moment that has passed. Long enough that a refresh does
    not hammer the API, short enough that the page still means what it says.

    The reading itself lives in ``cfdinvest.ui.broker`` so a test can supply
    one. It used to live here, which made the whole body of this page
    unreachable in tests -- with no credentials the script stopped a few
    lines below and everything after went unexecuted, which is how a tile
    with three fields instead of four reached the deployed dashboard with a
    green suite behind it.
    """
    return broker_source.read_broker()


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

# The broker reports two different things and only one of them was shown.
#
# `profitLoss` is UNREALISED P&L on currently open positions. It was
# labelled "since the account opened", which is a different quantity and
# usually a different sign: on 2026-08-31 a 5,000 account stood at 4,890 --
# down 110 -- while this tile read "+26.12 since the account opened". A
# label that inverts a loss into a profit is worse than no tile.
#
# `deposit` is cash: the opening deposit plus everything realised since.
# Shown beside it so the identity is visible rather than asserted --
# cash plus unrealised equals balance -- which is what makes the two
# figures legible as different quantities instead of a contradiction.
#
# Profit since the account opened is NOT shown, because the broker does not
# report the opening deposit and nothing here records it. Inferring it from
# `deposit` would be wrong by exactly the realised P&L. "Not available" and
# "zero" are different statements, and this project does not guess between
# them.
components.render_tiles(st, [
    ("Balance", _money(account.get("balance")),
     "cash plus unrealised; what sizing is a fraction of", "pass"),
    ("Available", _money(account.get("available")),
     "after committed margin", "pass"),
    ("Margin in use", _money(committed),
     "across every open position", "pass"),
    ("Cash", _money(account.get("deposit")),
     "deposits plus everything realised", "pass"),
    ("Unrealised P&L", _money(account.get("profit_loss")),
     "on open positions only, not since opening",
     "pass" if (account.get("profit_loss") or 0) >= 0 else "warning"),
])

# --------------------------------------------------------------------------- #
# The question the account exists to answer
# --------------------------------------------------------------------------- #
# "If I gave you £5,000, would you earn or lose me money?" -- the owner's
# framing, and the reason the forward record is kept at all. It goes first
# and it is stated plainly, in money, with the two halves that make it up.
#
# The opening balance comes from CFD_CAPITAL_OPENING_BALANCE beside the
# credentials, because it is a fact about the account those credentials
# reach: point this at a live account and both change together.
since = account.get("since_opening")
if since is None:
    components.render_note(
        st,
        "**Profit since the account opened is not shown.** "
        f"{account.get('performance_note') or 'The opening balance is unset.'} "
        "Set `CFD_CAPITAL_OPENING_BALANCE` beside the Capital credentials to "
        "answer it. It is left absent rather than approximated: this figure "
        "once showed unrealised P&L instead, which read as a profit while "
        "the account was down.",
    )
else:
    opening = account.get("opening_balance")
    share = account.get("since_opening_pct")
    components.render_tiles(st, [
        ("Since the account opened", _money(since),
         f"against {_money(opening)} put in"
         + ("" if share is None else f" — {share:+.2%}"),
         "pass" if since >= 0 else "warning"),
        ("of which realised", _money(account.get("realised")),
         "closed positions, banked", "pass"),
        ("of which unrealised", _money(account.get("unrealised")),
         "open positions, not yet banked", "pass"),
    ])
    components.render_note(
        st,
        "The two halves sum to the whole: cash now minus what was put in, "
        "plus the open-position P&L. **Assumes no deposits or withdrawals "
        "after funding** — the broker folds contributions and realised P&L "
        "into one figure, so nothing here can detect one. And a few days of "
        "this is not evidence of anything: it is a record being kept, not a "
        "result.",
    )

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
        median = priced["Spread bp"].median()
        components.render_tiles(st, [
            ("Median spread", f"{median:.1f} bp", "across the universe",
             "pass" if median <= 10.0 else "warning"),
            ("Cheapest",
             f"{priced.loc[priced['Spread bp'].idxmin(), 'Instrument']}"
             f" {priced['Spread bp'].min():.1f} bp", "", "pass"),
            ("Dearest",
             f"{priced.loc[priced['Spread bp'].idxmax(), 'Instrument']}"
             f" {priced['Spread bp'].max():.1f} bp", "",
             "warning" if priced["Spread bp"].max() > 10.0 else "pass"),
            ("Assumed by the model", "10.0 bp", "round trip, every instrument",
             "unchecked"),
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
