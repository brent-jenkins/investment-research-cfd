"""Simulation — what would have happened had you followed the advice.

Every figure on this page is from a rehearsal. Nothing here is a real result,
and the page says so above the numbers rather than below them.

It is not a second backtester: the fills, spread, slippage, FX and stops all
come from the same execution engine the pipeline uses. What is rehearsed is
the *decisions* — the helper's sizing and the refinement layer's adjustments.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parents[3])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from cfdinvest.livehelper import LiveHelperConfig  # noqa: E402
from cfdinvest.persist import simulation as simulation_store  # noqa: E402
from cfdinvest.simulate import SimulationConfig, run_simulation  # noqa: E402
from cfdinvest.ui import components  # noqa: E402

data = components.page_setup(
    st,
    "Simulation",
    icon="🎬",
    subtitle="What would have happened had you followed the advice — "
    "including every time it said to trade smaller.",
)
config = data["config"]
balance = float(
    st.session_state.get(components.BALANCE_KEY, components.DEFAULT_BALANCE)
)

# Above the numbers, deliberately. A banner underneath is one people scroll
# past on the way to the figure they came for.
st.error(
    "**Everything on this page is a rehearsal.** No order was placed and no "
    "money moved. These are not your results, and they are stored separately "
    "from those that are."
)

# --------------------------------------------------------------------------- #
# What to rehearse
# --------------------------------------------------------------------------- #
with st.expander("What to follow", expanded=True):
    cells = st.columns(3)
    follow_sizing = cells[0].checkbox(
        "Follow the safe size", value=True,
        help="Apply the size the helper would have recommended.",
    )
    follow_refinement = cells[1].checkbox(
        "Follow the refinements", value=True,
        help="Also apply the drawdown, regime and behavioural adjustments.",
    )
    skip_unsafe = cells[2].checkbox(
        "Skip UNSAFE trades", value=False,
        help="The most aggressive intervention, and the one most likely to "
        "flatter the advice — skipping trades in a losing period improves any "
        "curve.",
    )

    components.render_note(
        st,
        "The two adjustments are separate because they answer different "
        "questions. Running both makes it impossible to say which did the work.",
    )

if st.button("Run the rehearsal", type="primary"):
    st.session_state["cfd_run_simulation"] = True

if not st.session_state.get("cfd_run_simulation"):
    components.render_note(
        st,
        "Nothing has been rehearsed yet. This replays the last recorded run "
        "twice — once as it was executed, once with the helper's advice "
        "applied — and compares them. The comparison is the output; a single "
        "simulated curve would just be another backtest.",
    )
    st.stop()

try:
    run = run_simulation(
        config,
        simulation=SimulationConfig(
            follow_sizing=follow_sizing,
            follow_refinement=follow_refinement,
            skip_unsafe=skip_unsafe,
        ),
        helper_config=LiveHelperConfig(account_balance_gbp=balance),
        store=False,
    )
except Exception as error:  # noqa: BLE001 - the page must not go dark
    st.error(f"**The rehearsal could not be run.** {error}")
    st.stop()

result = run.result

if result.error:
    st.warning(f"**{result.headline}**")
    st.stop()

st.subheader("The comparison")
st.markdown(f"**{result.headline}**")

# --------------------------------------------------------------------------- #
# Metrics side by side
# --------------------------------------------------------------------------- #
rows = []
for metric in ("final_equity", "sharpe_ratio", "win_rate", "sortino_ratio"):
    baseline = result.baseline_metrics.get(metric)
    advised = result.advised_metrics.get(metric)
    if baseline is None and advised is None:
        continue
    rows.append(
        {
            "metric": metric.replace("_", " "),
            "as executed": baseline,
            "as advised": advised,
            "difference": result.difference(metric),
        }
    )

if rows:
    components.render_frame(st, pd.DataFrame(rows))

cells = st.columns(3)
cells[0].metric("Sizes changed", len(result.adjustments))
cells[1].metric("Forced closures", len(result.forced_closures))
cells[2].metric(
    "Trades rehearsed", len(result.trades) if result.trades is not None else 0
)

if not result.baseline_equity.empty and not result.advised_equity.empty:
    curves = pd.DataFrame(
        {
            "as executed": result.baseline_equity,
            "as advised": result.advised_equity,
        }
    )
    st.line_chart(curves, height=280)

components.render_note(
    st,
    "Following the advice costing money on this run is not evidence that it "
    "would on the next. Capping exposure reduces the return of a winning run "
    "and the loss of a losing one — which is what it is for.",
)

st.divider()

# --------------------------------------------------------------------------- #
# What the helper would have changed
# --------------------------------------------------------------------------- #
st.subheader("What the advice would have changed")

if result.adjustments.empty:
    components.render_note(
        st,
        "Nothing. The helper would have left every size alone — which is "
        "itself an answer, and usually means the limits are set well above "
        "what the strategy asks for.",
    )
else:
    components.render_frame(st, result.adjustments.iloc[::-1])

if not result.forced_closures.empty:
    st.warning(
        f"**The book would have crossed the platform's margin ratio on "
        f"{len(result.forced_closures)} bar(s).** These are reported, not "
        "applied — closing a position mid-path would change every bar after "
        "it, and the two curves would stop being comparable."
    )
    components.render_frame(st, result.forced_closures)

for note in result.notes:
    st.caption(f"⚠ {note}")

st.divider()

# --------------------------------------------------------------------------- #
# What was derived from it
# --------------------------------------------------------------------------- #
st.subheader("What the simulated record shows")

components.render_note(
    st,
    "The insights and behavioural layers, run over the **simulated** trades. "
    "These are facts about the strategy executed this way — not forecasts "
    "about you.",
)

cells = st.columns(2)
with cells[0]:
    st.markdown("**Insights**")
    st.caption(run.insights.headline if run.insights else "Not derived.")
with cells[1]:
    st.markdown("**Behaviour**")
    st.caption(run.bias.headline if run.bias else "Not derived.")

st.divider()

# --------------------------------------------------------------------------- #
# Past rehearsals
# --------------------------------------------------------------------------- #
st.subheader("Past rehearsals")

try:
    history = simulation_store.load_simulation_history(config)
except Exception as error:  # noqa: BLE001
    st.warning(f"Could not read the simulation history: {error}")
    history = pd.DataFrame()

if history.empty:
    components.render_note(
        st,
        "None recorded. Opening this page does not file one — a dashboard "
        "left open would otherwise fill the history with identical rehearsals.",
    )
else:
    components.render_frame(
        st,
        history[
            [c for c in ("timestamp", "label", "equity_difference",
                         "adjustments", "forced_closures", "headline")
             if c in history.columns]
        ].iloc[::-1],
    )
