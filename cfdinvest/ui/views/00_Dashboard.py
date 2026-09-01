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
currency = account.get("currency") or ""


def _money(value) -> str:
    """Matches the Broker page: absent shows as a dash, never as zero."""
    return "—" if value is None else f"{currency} {value:,.2f}"


if (broker or {}).get("error") or not account:
    # Not render_absent: that takes a Source, and this is a live broker read.
    # Absence is named rather than drawn as zero -- an unreadable account and
    # an empty one are different states, and only one of them is reassuring.
    st.warning(
        "The demo account could not be read"
        + (f": {broker['error']}" if (broker or {}).get("error") else "")
        + ". That is an unanswered question, not an empty account."
    )
else:
    # Keys are the broker reader's own display labels -- balance, available,
    # profit_loss -- not invented ones. Guessing them showed a row of blanks
    # while looking like it worked.
    # "Since opening" leads, because it is the question the account exists
    # to answer and the front page is where it is asked. Open P&L sits
    # beside it rather than instead of it -- showing only the second under
    # the first's name is the bug this replaced.
    row = st.columns(4)
    profit = account.get("profit_loss")
    since = account.get("since_opening")
    row[0].metric(
        "Since opening",
        "—" if since is None else _money(since),
        delta=None if since is None else f"{since:+,.2f}",
        help=account.get("performance_note") or
        "Balance now against what was put in. Realised and unrealised "
        "together.",
    )
    row[1].metric("Balance", _money(account.get("balance")))
    row[2].metric(
        "Open P&L", _money(profit),
        delta=None if profit is None else f"{profit:+,.2f}",
        help="Unrealised, on open positions only.",
    )
    row[3].metric("Open positions", len(positions))

# --------------------------------------------------------------------------- #
# What it holds
# --------------------------------------------------------------------------- #
st.subheader("What it holds")

if (broker or {}).get("positions_error"):
    st.warning(
        f"The position list could not be read: {broker['positions_error']}. "
        "That is not an empty account, and only one of those means it is "
        "safe to open more."
    )
elif not positions:
    st.caption(
        "Nothing open. Read successfully, and empty — the book opens when a "
        "signal clears the gates and not otherwise."
    )
else:
    # Rendered as the reader returns them. The rows already carry display
    # labels, so re-mapping them was how every column came out blank.
    frame = pd.DataFrame(positions)
    st.dataframe(
        frame.drop(columns=[c for c in ("Deal id",) if c in frame.columns]),
        width="stretch", hide_index=True,
    )
    st.caption(
        "Summary only. The Broker page carries the full detail, including "
        "what each instrument costs to trade."
    )

# --------------------------------------------------------------------------- #
# Is it running?
# --------------------------------------------------------------------------- #
st.subheader("Is it running?")

scheduler = (data or {}).get("scheduler")
# `Source.missing` is a CLASSMETHOD CONSTRUCTOR, not a flag. So
# `getattr(source, "missing", False)` returns a bound method, which is
# always truthy -- this branch fired on every render since it was written,
# and everything below it has never once been shown. The dashboard has been
# reporting "no scheduler record yet" while thirty run directories sat on
# disk and the Status page listed each job's last run to the minute.
#
# `ok` is the property that answers the question: the read succeeded and
# produced something.
if scheduler is None or not getattr(scheduler, "ok", False):
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
