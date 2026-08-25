"""Risk — what the book is carrying, read from what the pipeline wrote.

Nothing on this page drives the pipeline and nothing recomputes a position.
Every figure comes from a persisted artefact; anything that could not be read
is named rather than filled in.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parents[3])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from cfdinvest.risk import RiskLimits, snapshot_from_disk  # noqa: E402
from cfdinvest.ui import components  # noqa: E402

data = components.page_setup(
    st,
    "Risk",
    icon="⚖",
    subtitle="What the book is carrying, and what it is carrying it into. "
    "Read from persisted artefacts — nothing here re-runs the pipeline.",
)
config = data["config"]
balance = float(
    st.session_state.get(components.BALANCE_KEY, components.DEFAULT_BALANCE)
)

#: Deliberately off-brand, for the same reason the critical banner is.
STATUS_COLOUR = {"OK": "#2E7D32", "FAILED": "#C62828", "UNKNOWN": "#B26A00"}

with st.expander("Book limits", expanded=False):
    cells = st.columns(3)
    max_total = cells[0].number_input(
        "Total exposure limit", min_value=0.0, value=3_000.0, step=100.0
    )
    max_instrument = cells[0].number_input(
        "Per-instrument limit", min_value=0.0, value=1_500.0, step=100.0
    )
    max_leverage = cells[1].number_input(
        "Effective leverage limit", min_value=0.1, value=15.0, step=0.5
    )
    max_margin = cells[1].number_input(
        "Margin usage limit", min_value=0.01, max_value=1.0, value=0.50, step=0.05
    )
    max_fx = cells[2].number_input(
        "Currency exposure limit", min_value=0.01, max_value=1.0, value=0.30,
        step=0.05,
    )
    max_drawdown = cells[2].number_input(
        "Drawdown limit", min_value=0.01, max_value=1.0, value=0.20, step=0.05
    )
    components.render_note(
        st,
        "These bound the **book**. The per-trade bounds — what you may open — "
        "live on the Live-Execution Helper page, because \"may I open this?\" "
        "and \"how much am I carrying?\" are different questions.",
    )

limits = RiskLimits(
    account_balance_gbp=balance,
    max_total_exposure_gbp=max_total,
    max_exposure_per_instrument_gbp=max_instrument,
    max_effective_leverage=max_leverage,
    max_margin_usage_pct=max_margin,
    max_fx_exposure_pct=max_fx,
    max_drawdown_pct=max_drawdown,
)

try:
    snapshot = snapshot_from_disk(config, limits=limits)
except Exception as error:  # noqa: BLE001 - a risk page must not go dark
    st.error(f"**The risk snapshot could not be built.** {error}")
    st.stop()

# --------------------------------------------------------------------------- #
# The verdict
# --------------------------------------------------------------------------- #
if snapshot.flags:
    st.error(
        f"**{snapshot.message}**\n\n"
        + "\n".join(f"- {flag}" for flag in snapshot.flags)
    )
elif snapshot.unavailable:
    st.warning(f"**{snapshot.message}** Not checked: "
               f"{', '.join(snapshot.unavailable)}.")
else:
    st.success(snapshot.message)

st.caption(
    f"Run `{snapshot.run_id or components.DASH}` · "
    f"positions as at {components.short_moment(snapshot.as_of)}"
)

st.divider()

# --------------------------------------------------------------------------- #
# Scheduler
# --------------------------------------------------------------------------- #
st.subheader("Status")

status = snapshot.scheduler_status or {}
kind = str(status.get("run_status", "UNKNOWN"))
st.markdown(
    f'<div style="background:{STATUS_COLOUR.get(kind, "#B26A00")};color:#fff;'
    f'padding:10px 16px;border-radius:8px;font-weight:700;">'
    f"{kind} &nbsp;·&nbsp; {status.get('message', '')}</div>",
    unsafe_allow_html=True,
)

cells = st.columns(4)
cells[0].metric(
    "Last started", components.short_moment(status.get("last_run_started_at"))
)
cells[1].metric(
    "Last completed",
    components.short_moment(status.get("last_run_completed_at")),
    help="Written only when the pipeline finished. A start with no completion "
    "after it is a run that died part-way.",
)
cells[2].metric("Runs recorded", status.get("runs", 0))
cells[3].metric("Failures recorded", status.get("failures", 0))

if status.get("last_error"):
    st.error(f"Last recorded error: {status['last_error']}")

st.divider()

# --------------------------------------------------------------------------- #
# Exposure
# --------------------------------------------------------------------------- #
st.subheader("Exposure")

exposure = snapshot.exposure_metrics or {}
if exposure.get("flat"):
    st.info("The book is flat — nothing is open.")
elif exposure.get("gross_exposure_gbp") is None:
    components.render_note(
        st,
        "Positions are recorded but no prices were stored with this run, so "
        "exposure cannot be valued. Units are shown below.",
    )
    components.render_frame(
        st,
        pd.DataFrame(
            {"units": exposure.get("units", {})}
        ).rename_axis("instrument"),
    )
else:
    gross = exposure["gross_exposure_gbp"]
    cells = st.columns(4)
    cells[0].metric(
        "Gross exposure",
        components.money(gross),
        delta=f"limit {components.money(exposure.get('limit_total'))}",
        delta_color="off",
    )
    cells[1].metric("Net exposure", components.money(exposure.get("net_exposure_gbp")))
    cells[2].metric("Open positions", exposure.get("open_positions", 0))
    cells[3].metric(
        "Largest position",
        components.percent(exposure.get("largest_instrument_share")),
        delta=str(exposure.get("largest_instrument") or components.DASH),
        delta_color="off",
        help="Concentration: a book inside every absolute limit can still be "
        "one position.",
    )

    left, right = st.columns(2)
    with left:
        st.caption("By instrument")
        per_instrument = pd.DataFrame(
            {"exposure_gbp": exposure.get("exposure_by_instrument", {})}
        ).rename_axis("instrument").sort_values("exposure_gbp", ascending=False)
        components.render_frame(st, per_instrument)
        if not per_instrument.empty:
            st.bar_chart(per_instrument["exposure_gbp"], height=220)
    with right:
        st.caption("By asset class")
        per_class = pd.DataFrame(
            {"exposure_gbp": exposure.get("exposure_by_asset_class", {})}
        ).rename_axis("asset class").sort_values("exposure_gbp", ascending=False)
        components.render_frame(st, per_class)
        if not per_class.empty:
            st.bar_chart(per_class["exposure_gbp"], height=220)

st.divider()

# --------------------------------------------------------------------------- #
# Leverage, margin and currency
# --------------------------------------------------------------------------- #
left, middle, right = st.columns(3)

with left:
    st.subheader("Leverage")
    leverage = snapshot.leverage_metrics or {}
    st.metric(
        "Effective leverage",
        components.number(leverage.get("effective_leverage"), 2),
        delta=f"limit {leverage.get('limit', 0):,.0f}x",
        delta_color="off",
        help="Gross exposure over account balance — the leverage a smaller "
        "book reduces. Instrument leverage is fixed by Trading 212.",
    )

with middle:
    st.subheader("Margin")
    margin = snapshot.margin_metrics or {}
    st.metric(
        "Margin usage",
        components.percent(margin.get("margin_usage_pct")),
        delta=f"limit {margin.get('limit', 0):.0%}",
        delta_color="off",
    )
    if not margin.get("complete"):
        unknown = margin.get("unknown_leverage") or ()
        components.render_note(
            st,
            "Not computable"
            + (
                f" for {', '.join(unknown)}: Trading 212 sets leverage per "
                "instrument and does not publish a list, so margin is only "
                "known for instruments you have supplied it for."
                if unknown
                else "."
            ),
        )

with right:
    st.subheader("Currency")
    fx = snapshot.fx_metrics or {}
    st.metric(
        "Foreign exposure",
        components.percent(fx.get("foreign_share_of_account")),
        delta=f"limit {fx.get('limit', 0):.0%}",
        delta_color="off",
    )
    by_currency = fx.get("exposure_by_currency") or {}
    if by_currency:
        components.render_frame(
            st,
            pd.DataFrame({"exposure_gbp": by_currency}).rename_axis("currency"),
        )
    if fx.get("unmapped_instruments"):
        components.render_note(
            st,
            f"{len(fx['unmapped_instruments'])} instrument(s) have no recorded "
            "currency and are counted as domestic, which understates foreign "
            "exposure rather than inventing it.",
        )

st.divider()

# --------------------------------------------------------------------------- #
# Regime
# --------------------------------------------------------------------------- #
st.subheader("Market regime")

regimes = snapshot.regime_metrics or {}
if not regimes.get("available"):
    components.render_note(
        st, "This run did not store regime classifications."
    )
else:
    from cfdinvest.risk.model import REGIME_COLUMNS

    labels = {
        "volatility_regime": "Volatility",
        "trend_regime": "Trend",
        "correlation_regime": "Correlation",
        "macro_regime": "Macro",
        "combined_regime": "Overall",
    }
    present = [c for c in REGIME_COLUMNS if c in regimes["regimes"]]
    cells = st.columns(max(len(present), 1))
    for cell, column in zip(cells, present):
        label = str(regimes["regimes"][column]).replace("_", " ").capitalize()
        elevated = column in (regimes.get("elevated") or ())
        cell.metric(labels[column], f"{'⚠ ' if elevated else ''}{label}")

    if regimes.get("elevated"):
        components.render_note(
            st,
            "Flagged regimes are not a reason to stop — a crisis is also "
            "where the opportunity is. They are a reason to hold less.",
        )
    st.caption(f"As at {components.short_moment(regimes.get('as_of'))}")

st.divider()

# --------------------------------------------------------------------------- #
# Recent trades and profit and loss
# --------------------------------------------------------------------------- #
left, right = st.columns(2)

with left:
    st.subheader("Trades you considered")
    trades = snapshot.trade_risk_metrics or {}
    if not trades.get("available"):
        components.render_note(
            st,
            "No live-helper verdicts recorded. Run `cfdinvest-helper` with "
            "`--record` and they will be summarised here.",
        )
    else:
        counts = trades.get("counts", {})
        cells = st.columns(3)
        for cell, status_name in zip(cells, ("SAFE", "BORDERLINE", "UNSAFE")):
            cell.metric(status_name.capitalize(), counts.get(status_name, 0))

        components.render_kv(
            st,
            {
                "Assessed": trades.get("assessed", 0),
                "Average risk": components.money(trades.get("average_risk_gbp")),
                "Average leverage": components.number(
                    trades.get("average_effective_leverage"), 2
                ),
            },
        )
        reasons = trades.get("unsafe_reasons") or {}
        if reasons:
            st.caption("What kept making them unsafe")
            components.render_frame(
                st,
                pd.DataFrame({"times": reasons}).rename_axis("reason"),
            )

with right:
    st.subheader("Profit and loss")
    pnl = snapshot.pnl_risk_metrics or {}
    if not pnl.get("available"):
        components.render_note(
            st, "This run did not store a profit-and-loss decomposition."
        )
    else:
        cells = st.columns(2)
        cells[0].metric("Total", components.money(pnl.get("total_pnl")))
        cells[1].metric(
            f"Last {pnl.get('recent_bars', 0)} bars",
            components.money(pnl.get("recent_pnl")),
        )
        drawdowns = st.columns(2)
        drawdowns[0].metric(
            "Current drawdown", components.percent(pnl.get("current_drawdown"))
        )
        drawdowns[1].metric(
            "Maximum drawdown", components.percent(pnl.get("max_drawdown"))
        )

        components.render_kv(
            st,
            {
                "Overnight (gap)": components.money(pnl.get("gap_pnl")),
                "Intraday": components.money(pnl.get("intraday_pnl")),
                "Overnight share": components.percent(pnl.get("overnight_share")),
            },
        )
        components.render_note(
            st,
            "**The split is the finding.** Profit arriving overnight is gap "
            "risk a stop cannot bound; losses arriving overnight are moves "
            "you were never in the market for.",
        )

st.divider()

# --------------------------------------------------------------------------- #
# History
# --------------------------------------------------------------------------- #
st.subheader("Risk over time")

try:
    from cfdinvest.persist import risk as risk_store

    history = risk_store.load_risk_history(config)
except Exception as error:  # noqa: BLE001
    history = pd.DataFrame()
    components.render_note(st, f"Snapshot history unreadable: {error}")

if history.empty:
    components.render_note(
        st,
        "No risk snapshots stored yet. The scheduled run writes one each time "
        "it completes; until then this page computes the current figures live "
        "from the persisted artefacts above.",
    )
else:
    charted = history.set_index("timestamp")
    for column, label in (
        ("gross_exposure_gbp", "Gross exposure"),
        ("effective_leverage", "Effective leverage"),
        ("current_drawdown", "Drawdown"),
    ):
        if column in charted.columns and charted[column].notna().any():
            st.caption(label)
            st.line_chart(charted[[column]], height=180)
    components.render_frame(st, history.tail(50), height=280)

components.render_note(
    st,
    "Every figure on this page comes from a persisted artefact. Nothing here "
    "runs the pipeline, recomputes a position, or places an order.",
)
