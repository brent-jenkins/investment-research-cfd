"""Signals — what the model wants, per instrument."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parents[3])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from cfdinvest.schema import TICKER, TIMESTAMP  # noqa: E402
from cfdinvest.ui import components, explain, theme  # noqa: E402

data = components.page_setup(
    st,
    "Signals",
    icon="🧭",
    subtitle="What the model wants to hold, and why. A signal is not yet "
    "a position — see Execution for the trades it implies.",
)
mode = data["mode"]
signals = data["signals"]
summary = data["summary"]

components.render_note(
    st,
    "Only **active** signals are stored — flat cells are the vast majority "
    "and are dropped before writing, so an instrument missing here is flat, "
    "not missing. A signal below the entry bar is shown for information and "
    "marked as untradeable: it is what the model sees, not what it would do.",
)

if components.render_absent(st, signals, "Signals"):
    st.stop()

frame = signals.value.copy()
if isinstance(frame.index, pd.MultiIndex):
    frame = frame.reset_index()

if TIMESTAMP in frame.columns:
    frame[TIMESTAMP] = pd.to_datetime(frame[TIMESTAMP], utc=True, errors="coerce")
    as_of = frame[TIMESTAMP].max()
    latest = frame[frame[TIMESTAMP] == as_of].copy()
else:
    as_of, latest = None, frame.copy()

st.caption(f"As of {components.moment(as_of)}")

# --------------------------------------------------------------------------- #
# Levels, joined from the trade log
# --------------------------------------------------------------------------- #
# Stop and take-profit levels live on the trade log, not on the signal frame:
# a level belongs to a position, and a signal is not yet a position. Joining
# the most recent trade per instrument is what connects the two.
execution = data["execution"]
trades = (execution.value or {}).get("trades")
levels = pd.DataFrame()

if isinstance(trades, pd.DataFrame) and not trades.empty and "asset" in trades:
    wanted = [
        column
        for column in ("stop_level", "take_profit_level", "fill_price", "price")
        if column in trades.columns
    ]
    if wanted:
        levels = (
            trades.sort_values("timestamp").groupby("asset")[wanted].last()
        )

if not levels.empty and TICKER in latest.columns:
    latest = latest.merge(
        levels, left_on=TICKER, right_index=True, how="left", suffixes=("", "_trade")
    )

reference = None
for column in ("price", "fill_price"):
    if column in latest.columns:
        reference = pd.to_numeric(latest[column], errors="coerce")
        break

for level, label in (("stop_level", "stop_distance"), ("take_profit_level", "tp_distance")):
    if level in latest.columns and reference is not None:
        distance = (pd.to_numeric(latest[level], errors="coerce") - reference).abs()
        latest[label] = distance
        latest[f"{label}_pct"] = distance / reference.replace(0, np.nan)

# --------------------------------------------------------------------------- #
# Per instrument
# --------------------------------------------------------------------------- #
# The state of play, in a sentence, before any table.
#
# The page used to open with fifteen bordered cards two hundred pixels tall,
# in ticker order, so the one signal that cleared the bar was as prominent
# as the fourteen that did not and you had to scroll to find out whether
# anything was actionable at all.
# The bar the engine actually enters on, not the one the advisor suggests
# at. Those were different numbers for a while -- the engine entered at 0.35
# while the paper book's lowest arm sat at 0.50 -- so a signal at 0.42 was
# traded by the strategy and shown here as though nothing would happen.
bar = getattr(data["config"].strategy, "entry_confidence", 0.35)
suggest_bar = getattr(data["config"].strategy, "suggestion_confidence", 0.60)
confidences = pd.to_numeric(latest.get("confidence"), errors="coerce")     if "confidence" in latest else pd.Series(dtype=float)
clearing = int((confidences >= bar).sum())
longs = int((latest.get("direction", pd.Series(dtype=str)) == "long").sum())
shorts = int((latest.get("direction", pd.Series(dtype=str)) == "short").sum())

if latest.empty:
    headline = "**Nothing is signalling.** The model wants no position on this bar."
elif clearing:
    headline = (
        f"**{clearing} of {len(latest)} signals clear the {bar:.2f} entry bar** "
        f"and would be taken. {longs} long, {shorts} short."
    )
else:
    best = confidences.max() if len(confidences) else float("nan")
    headline = (
        f"**Nothing clears the {bar:.2f} entry bar.** {len(latest)} instruments "
        f"are signalling — {longs} long, {shorts} short — but the strongest is "
        f"{best:.2f}, so nothing would be taken."
    )
components.render_note(st, headline)

st.subheader("Current signals")

if latest.empty:
    components.render_note(st, "No active signal on the most recent bar.")
else:
    # Highest conviction first. Ticker order treats a 90% signal and a 9%
    # one as equally worth reading, which is the opposite of true.
    ordered = latest.copy()
    ordered["_confidence"] = confidences
    ordered = ordered.sort_values("_confidence", ascending=False, na_position="last")

    # Resolved once for the table rather than per row: it reads the traded
    # universe, and asking it forty times a render is forty reads of the same
    # selections file. None means "could not tell", which is reported by
    # saying nothing rather than by marking every row.
    try:
        from cfdinvest.universe.traded import traded_universe

        openable = set(traded_universe(data["config"]).names or ())
    except Exception:  # noqa: BLE001 - a view must not break on this
        openable = None

    header = st.columns([1.1, 0.9, 1.5, 0.7, 3.2])
    for cell, label in zip(header, ("", "", "Trend strength", "Size", "Why")):
        cell.caption(f"**{label}**" if label else "")

    for _, row in ordered.iterrows():
        ticker = row.get(TICKER, "?")
        confidence = row.get("_confidence")
        takeable = pd.notna(confidence) and confidence >= bar

        name, side, conf, size, why = st.columns([1.1, 0.9, 1.5, 0.7, 3.2])

        name.markdown(f"**{ticker}**")
        side.markdown(
            components.direction_badge(row.get("direction"), mode),
            unsafe_allow_html=True,
        )

        # The bar is the point of the column: a number you have to compare
        # against a threshold in your head is a number you will misread.
        if pd.notna(confidence):
            # Drawn rather than using st.progress, which takes no colour. The
            # bar carries the verdict: red below the entry bar, accent above
            # it. A reader should not have to compare a percentage against a
            # threshold they are holding in their head.
            if not takeable:
                colour = theme.colour_for("critical", mode)
                mark = f" ✕ below {bar:.2f}"
            else:
                colour = theme.colour_for("ok", mode)
                mark = " ✓✓" if confidence >= suggest_bar else " ✓"
            width = min(max(float(confidence), 0.0), 1.0) * 100
            conf.markdown(
                f'<div style="font:600 13px/1.4 inherit;color:{colour};">'
                f'{confidence:.2f}{mark}</div>'
                f'<div style="background:rgba(128,128,128,0.22);border-radius:3px;'
                f'height:6px;width:100%;margin-top:2px;">'
                f'<div style="background:{colour};width:{width:.1f}%;height:6px;'
                f'border-radius:3px;"></div></div>',
                unsafe_allow_html=True,
            )
        else:
            conf.markdown("—")

        size.markdown(components.number(row.get("size"), 3))
        # No second line explaining the cross. The red bar and the "✕ below
        # 35%" already say it, and repeating it on every untradeable row was
        # more words than the rows they were describing.
        why.caption(
            explain.reason(row.get("reason")) if takeable
            else f":grey[{explain.reason(row.get('reason'))}]"
        )

        # Whether this instrument is one the daemon may actually OPEN.
        #
        # This used to report Trading 212 tradability -- "Not tradable on
        # Trading 212" -- from the days when a person placed these by hand
        # there. Orders have gone to Capital automatically since 2026-08-27,
        # so the line named a broker this page has nothing to do with.
        #
        # It is NOT replaced with a Capital equivalent, because there is no
        # honest one to give: `capital_venue` returns available=True for a
        # ticker that does not exist, so the field is a default rather than a
        # verified fact and a green "tradable at Capital" on every row would
        # be a confident claim about nothing.
        #
        # What IS checkable is whether the screen has selected the name. A
        # signal on an unselected instrument will not be opened however good
        # it looks, and that is the thing that would stop you acting.
        if openable is not None and str(ticker) not in openable:
            why.caption(
                ":orange[Not in the traded selection — the screen has not "
                "picked it, so it will not be opened.]"
            )

st.divider()

# --------------------------------------------------------------------------- #
# Regime context
# --------------------------------------------------------------------------- #
st.subheader("Regime context")

persisted = data["persisted"]
regimes = (persisted.value or {}).get("regimes")

if regimes is None or getattr(regimes, "empty", True):
    components.render_absent(st, persisted, "The regime classifications")
    components.render_note(
        st,
        "The signal `reason` string above names the regime that produced each "
        "decision, so the context is not lost even when the full "
        "classification is absent.",
    )
else:
    frame_regimes = regimes.copy()
    if TIMESTAMP in frame_regimes.columns:
        frame_regimes[TIMESTAMP] = pd.to_datetime(
            frame_regimes[TIMESTAMP], utc=True, errors="coerce"
        )
        latest_regime = frame_regimes.sort_values(TIMESTAMP).iloc[-1]
    else:
        latest_regime = frame_regimes.iloc[-1]

    labels = {
        "volatility_regime": "Volatility",
        "trend_regime": "Trend",
        "correlation_regime": "Correlation",
        "macro_regime": "Macro",
        "combined_regime": "Overall",
    }
    present = [c for c in labels if c in frame_regimes.columns]
    cells = st.columns(max(len(present), 1))
    for cell, column in zip(cells, present):
        cell.metric(
            labels[column],
            str(latest_regime[column]).replace("_", " ").capitalize(),
        )

    st.caption(
        f"As at {components.short_moment(latest_regime.get(TIMESTAMP))} · "
        "one classification per bar, market-wide rather than per instrument."
    )
    with st.expander("Regime history", expanded=False):
        components.render_frame(
            st,
            frame_regimes.sort_values(TIMESTAMP, ascending=False)
            if TIMESTAMP in frame_regimes.columns else frame_regimes,
            height=320,
        )

st.divider()

# --------------------------------------------------------------------------- #
# History
# --------------------------------------------------------------------------- #
st.subheader("Signal history")
components.render_frame(st, frame.sort_values(TIMESTAMP, ascending=False)
                        if TIMESTAMP in frame.columns else frame, height=420)

if summary.ok and summary.value.get("latest_signals"):
    with st.expander("As recorded in the run summary", expanded=False):
        components.render_frame(
            st, pd.DataFrame(summary.value["latest_signals"])
        )
