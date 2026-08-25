"""Meta-Strategy — how hard to push, and every reason for it.

This is the one page in the dashboard whose subject is *increasing* risk, so
it says what a posture can and cannot do above the numbers rather than below
them: it may use headroom the safety engine already granted, and can never
create any.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parents[3])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from cfdinvest.bias import run_bias_detection  # noqa: E402
from cfdinvest.insights import run_insights  # noqa: E402
from cfdinvest.metastrategy import (  # noqa: E402
    FORWARD_REQUIREMENTS,
    POSTURES,
    compute_meta_strategy,
)
from cfdinvest.persist import metastrategy as store  # noqa: E402
from cfdinvest.ui import components  # noqa: E402

data = components.page_setup(
    st,
    "Meta-Strategy",
    icon="🎚",
    subtitle="How hard to push right now — decided from the record, and "
    "bounded by the safety engine.",
)
config = data["config"]

POSTURE_COLOUR = {
    "conservative": "#2E7D32",
    "neutral": "#455A64",
    "assertive": "#B26A00",
    "opportunistic": "#C62828",
}

components.render_note(
    st,
    "A posture may **ask** for more size and can never create the room for "
    "it — the refinement layer caps the result at what the safety engine "
    "already approved, and reports when a request is overruled. The two "
    "tolerances are clamped at your configured limits and can only ever hold "
    "you further inside them.",
)

# --------------------------------------------------------------------------- #
# Work it out
# --------------------------------------------------------------------------- #
try:
    from cfdinvest.livehelper.context import load_context

    context = load_context(config)
    insights = run_insights(config, store=False)
    bias = run_bias_detection(config, store=False).to_profile()
    profile = compute_meta_strategy(
        insights=insights,
        bias_profile=bias,
        regime_profile=context.regime,
        pnl_history={"current_drawdown": context.performance.current_drawdown},
        account_state=context.run_id and data.get("account"),
    )
except Exception as error:  # noqa: BLE001 - the page must not go dark
    st.error(f"**The posture could not be computed.** {error}")
    st.stop()

st.markdown(
    f'<div style="background:{POSTURE_COLOUR[profile.posture]};color:#fff;'
    f'padding:12px 18px;border-radius:8px;font-weight:700;font-size:1.1rem;">'
    f"{profile.posture.upper()} &nbsp;·&nbsp; score {profile.score:+.2f} "
    f"&nbsp;·&nbsp; {profile.confidence} confidence</div>",
    unsafe_allow_html=True,
)

cells = st.columns(4)
cells[0].metric("Risk budget", components.percent(profile.risk_budget))
cells[1].metric(
    "Sizing",
    components.percent(profile.sizing_multiplier),
    delta="a request" if profile.raises_size else None,
    delta_color="off",
)
cells[2].metric("Exit", components.percent(profile.exit_multiplier))
cells[3].metric("Account", profile.account or components.DASH)

cells = st.columns(5)
cells[0].metric("Safety tolerance", components.percent(profile.safety_tolerance))
cells[1].metric("Margin tolerance", components.percent(profile.margin_tolerance))
cells[2].metric(
    "Bias correction", components.percent(profile.bias_correction_strength)
)
cells[3].metric(
    "Regime alignment", components.percent(profile.regime_alignment_strength)
)
cells[4].metric(
    "Performance alignment",
    components.percent(profile.performance_alignment_strength),
)

st.divider()

# --------------------------------------------------------------------------- #
# Why
# --------------------------------------------------------------------------- #
st.subheader("Why this posture")

for note in profile.notes:
    st.markdown(f"- {note}")

if profile.contributions:
    st.markdown("**What each input contributed**")
    frame = pd.DataFrame(
        [
            {"input": name, "contribution": value}
            for name, value in sorted(
                profile.contributions.items(), key=lambda pair: -abs(pair[1])
            )
        ]
    )
    components.render_frame(st, frame)
    st.caption(
        f"These sum to {profile.score:+.2f}, which is the score the posture "
        "band was read from. Adding them up by hand is the point."
    )

st.divider()

# --------------------------------------------------------------------------- #
# What a forward posture needs
# --------------------------------------------------------------------------- #
st.subheader("What a forward posture requires")

components.render_note(
    st,
    "**Cutting requires one reason. Raising requires all of them.** Any "
    "single adverse finding is enough to hold back. Cutting size wrongly "
    "costs money; raising size wrongly can remove you from the game, and the "
    "evidence here is far better at telling you when to hold back.",
)

unmet = set(profile.details.get("unmet_requirements", ()) or ())
allowed = profile.details.get("forward_allowed", False)

for requirement in FORWARD_REQUIREMENTS:
    st.markdown(f"- {requirement}")

if unmet:
    st.warning(
        "**Not clear:** " + "; ".join(sorted(unmet)) + "."
    )
elif allowed:
    st.success("Every requirement is clear, so a forward posture is available.")

if profile.missing:
    components.render_note(
        st,
        f"Could not be read at all: {', '.join(profile.missing)}. A posture "
        "resting on less of the record is held at neutral rather than "
        "guessing.",
    )

st.divider()

# --------------------------------------------------------------------------- #
# History
# --------------------------------------------------------------------------- #
st.subheader("How the posture has moved")

components.render_note(
    st,
    "A run of steadily more forward postures is a thing to notice, and no "
    "single profile shows it. A posture that only ratchets one way is "
    "following the record rather than responding to it.",
)

try:
    history = store.load_meta_strategy_history(config)
except Exception as error:  # noqa: BLE001
    st.warning(f"Could not read the posture history: {error}")
    history = pd.DataFrame()

if history.empty:
    components.render_note(
        st,
        "Nothing recorded yet. Opening this page does not file a profile — a "
        "dashboard left open would otherwise fill the history with identical "
        "readings.",
    )
else:
    cells = st.columns(3)
    cells[0].metric("Recorded", len(history))
    cells[1].metric("Distinct postures", history["posture"].nunique())
    cells[2].metric("Latest", history["posture"].iloc[-1])

    if history["score"].notna().any():
        st.line_chart(history.set_index("timestamp")["score"], height=200)

    components.render_frame(
        st,
        history[
            [
                column
                for column in (
                    "timestamp", "posture", "score", "sizing_multiplier",
                    "risk_budget", "confidence", "account",
                )
                if column in history.columns
            ]
        ].iloc[::-1],
    )

with st.expander("Every posture, and what it means numerically"):
    from cfdinvest.metastrategy import POSTURE_SETTINGS

    components.render_frame(
        st,
        pd.DataFrame(
            [{"posture": name, **POSTURE_SETTINGS[name]} for name in POSTURES]
        ),
    )
    components.render_note(
        st,
        "Written down rather than computed, so the mapping can be argued "
        "with and a posture is a name for a set of numbers rather than a "
        "mood.",
    )
