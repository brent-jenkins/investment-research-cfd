"""cfdinvest dashboard — the overview page.

Run it with::

    streamlit run cfdinvest/ui/app.py

**This app is read-only.** It opens files the pipeline already wrote and
renders them. It places no orders, refreshes no data, writes no config and
contacts no external service.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Streamlit runs each page as a script, so the import root is this file's
# directory rather than the project. Must happen before any cfdinvest import.
_ROOT = str(Path(__file__).resolve().parents[2])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from cfdinvest.ui import components  # noqa: E402

data = components.page_setup(st, "Overview", icon="📈")

now = data["now"]
health = data["health"]
summary = data["summary"]
lake = data["lake"]
scheduler = data["scheduler"].value or {}
execution = data["execution"]
metrics = (summary.value or {}).get("metrics") or {}
balance = float(st.session_state.get(components.BALANCE_KEY, components.DEFAULT_BALANCE))
factor = components.scale_factor(data, balance)

# --------------------------------------------------------------------------- #
# The account, scaled to the operator's real balance
# --------------------------------------------------------------------------- #
simulated = components.simulated_equity(data)
final = metrics.get("final_equity")
scaled_final = None if final is None or simulated is None else float(final) * factor

cells = st.columns(4)
cells[0].metric(
    "Account value",
    components.money(scaled_final if scaled_final is not None else balance),
    delta=components.percent(metrics.get("total_return")),
    help="Your balance grown by the simulated return, not a broker balance.",
)
cells[1].metric(
    "Simulated return",
    components.percent(metrics.get("total_return")),
    delta=components.money(
        None if final is None or simulated is None else (float(final) - simulated) * factor
    ),
    help="What the strategy returned over the backtested period.",
)
cells[2].metric(
    "Maximum drawdown",
    components.percent(metrics.get("max_drawdown_fraction")),
    help="The deepest peak-to-trough fall on the simulated equity curve.",
)
cells[3].metric(
    "Total costs",
    components.money(
        sum(
            float(metrics.get(key) or 0.0)
            for key in ("total_spread", "total_slippage", "total_financing", "total_fx")
        )
        * factor
    ),
    help="Spread, slippage, overnight financing and currency conversion.",
    delta_color="off",
)

st.divider()

# --------------------------------------------------------------------------- #
# System, last run, and the open book — three columns, no scrolling
# --------------------------------------------------------------------------- #
left, middle, right = st.columns(3)

with left:
    st.subheader("System")
    components.render_kv(
        st,
        {
            "Health": (
                "Critical"
                if health.show_banner
                else ("Warnings" if health.triggered else "Healthy")
            ),
            "Checks passing": f"{len(health.checks) - len(health.triggered)}"
            f" of {len(health.checks)}",
            "Data last updated": components.age(
                (lake.value or {}).get("last_updated"), now
            ),
            "Instruments stored": (lake.value or {}).get("series_count", 0),
            "Data quality flags": (lake.value or {}).get("anomalies_count", 0),
        },
    )

with middle:
    st.subheader("Last pipeline run")
    if not components.render_absent(st, summary, "The run summary"):
        record = summary.value
        components.render_kv(
            st,
            {
                "Run": record.get("run_id", "?"),
                "Outcome": str(record.get("status", "?")).capitalize(),
                "Finished": components.short_moment(record.get("finished_at")),
                "Took": components.duration(record.get("duration_seconds")),
                "Bars processed": f"{record.get('bars', 0):,}",
            },
        )
        if record.get("error"):
            st.error(record["error"])

with right:
    st.subheader("Next scheduled run")
    components.render_kv(
        st,
        {
            "Due": components.short_moment(scheduler.get("next_run")),
            "Trigger": " ".join(
                part
                for part in (
                    str(scheduler.get("next_run_kind") or "").capitalize(),
                    "of" if scheduler.get("next_run_asset") else "",
                    str(scheduler.get("next_run_asset") or ""),
                )
                if part
            )
            or components.DASH,
            "Runs on record": scheduler.get("run_count", 0),
            "Last outcome": str(scheduler.get("last_status") or components.DASH).capitalize(),
        },
    )
    components.render_note(
        st,
        "Derived from the run directory — the scheduler keeps no heartbeat, "
        "so this is when a run last finished, not whether one is running.",
    )

st.divider()

# --------------------------------------------------------------------------- #
# The open book, with sizes scaled to the real account
# --------------------------------------------------------------------------- #
st.subheader("Open positions")

positions = (execution.value or {}).get("positions")
trades = (execution.value or {}).get("trades")

if not components.render_absent(st, execution, "Execution output"):
    if isinstance(positions, pd.DataFrame) and not positions.empty:
        latest = positions.iloc[-1]
        held = latest[latest != 0]

        if held.empty:
            components.render_note(st, "The book is flat — nothing is open.")
        else:
            marks = pd.Series(dtype="float64")
            if isinstance(trades, pd.DataFrame) and "price" in trades.columns:
                marks = trades.sort_values("timestamp").groupby("asset")["price"].last()

            book = pd.DataFrame(
                {
                    "direction": ["Long" if units > 0 else "Short" for units in held],
                    "simulated_units": held.to_numpy(),
                    "scaled_units": held.to_numpy() * factor,
                    "last_price": [marks.get(name, float("nan")) for name in held.index],
                },
                index=held.index,
            )
            book["scaled_notional"] = (
                book["scaled_units"].abs() * book["last_price"]
            )
            book.index.name = "asset"
            components.render_frame(st, book)
            components.render_note(
                st,
                f"Sizes scaled to an account of {components.money(balance)} "
                f"(×{factor:,.4g}). Set the balance in the sidebar. "
                "These are recommendations — every order is placed by hand.",
            )
    else:
        components.render_note(st, "The last run held no positions.")
