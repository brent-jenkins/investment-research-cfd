"""The universe screen — what the rule would pick, and what is traded.

The screen has run monthly since 2026-08-27 and been visible to nobody. It
appends a selection to a store and prints to a terminal, so a month where
three names drifted out of the rule's answer was recorded and then sat
there. An alarm that rings where no one is standing is not an alarm.

**Nothing here changes anything.** Editing the traded universe stays a human
act at the command line, and `config.toml` says so: the screen "reports the
difference and records it, and never edits this file". That is the right
arrangement and this page does not soften it -- there is no button, because
the moment there is one the question stops being "should this change?" and
becomes "why not, it is only a click".

The reason to be slow about it is measured. Consecutive screens correlate at
+0.97 to +0.99 and the selection moves by one or two names a month, so
almost every difference this page shows is noise wearing the clothes of a
signal. The pool is also drawn with hindsight -- US large caps that are
large *now* -- so the screen is reproducible while the thing it screens is
not. Drift is worth seeing and rarely worth acting on.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parents[3])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from cfdinvest.config import load_config  # noqa: E402
from cfdinvest.ui import components, theme  # noqa: E402

_data = components.page_setup(
    st,
    "Universe",
    icon="🔭",
    subtitle="What the screening rule picks, and the history of what it has "
    "picked. Read-only: the selection is recorded, never edited.",
)
mode = _data.get("mode", "dark")

# The config the rest of the app is reading, not a fresh load.
#
# This called `load_config()` directly until 2026-09-04, which bypassed the
# shared source every other page uses. Two consequences: with
# CFD_ACCOUNT_MODE set it could describe a different account from the one the
# page is showing, and in tests it read the developer's real data root -- so
# the Universe test was asserting against live selections rather than the
# synthetic lake, and passed only while the two happened to agree about how
# many names a selection holds.
settings = _data.get("config") or load_config()

# Read from the owner, not from the config. `market.tickers` is the FALLBACK
# and stopped being the answer on 2026-08-29, when `follow_screen` was turned
# on and the daemon began trading the screen's latest recorded selection.
# This page went on showing the fallback and describing it as the source, so
# it named eight instruments while the account held ten -- LLY and ISRG were
# being traded and did not appear here at all.
#
# `universe.traded` is the single owner of that question and reports which
# source answered, how old it is, and whether it is stale, which is exactly
# what a reader needs to tell "the screen chose this" from "the screen has
# not run and this is the fallback".
try:
    from cfdinvest.universe.traded import traded_universe

    _traded = traded_universe(settings)
    traded = list(_traded.names or ())
    traded_source = _traded.source
    traded_as_of = getattr(_traded, "as_of", None)
    traded_stale = bool(getattr(_traded, "stale", False))
except Exception as error:  # noqa: BLE001 - a view must not break the page
    traded = list(settings.market.tickers or ())
    traded_source, traded_as_of, traded_stale = "unreadable", None, False
    st.warning(f"The traded universe could not be resolved: {error}")

try:
    from cfdinvest.universe.screen import load_screens

    screens = load_screens(settings)
except Exception as error:  # noqa: BLE001
    screens = []
    st.warning(f"The selection store could not be read: {error}")

# --- how many, and why ------------------------------------------------------ #
#
# The count used to be however many names somebody had typed into
# `market.tickers`. It is computed now, and a computed number that nobody
# can see is worse than a typed one -- so this says what it is, what it was
# derived from, and what it declined to take.
st.subheader("How many it holds")
try:
    from cfdinvest.risk.capacity import NOISE_FLOOR_PP
    from cfdinvest.universe.traded import explain_target

    curve = {int(k): float(v)
             for k, v in (getattr(settings.universe, "size_curve", {}) or {}).items()}
    # ONE computation, read for both the number and the reason. These used to
    # be two: the metric came from resolve_target, which applies the viability
    # ceiling and max_target, while the caption came from operating_size with
    # NO ceiling -- so the page explained choosing 100 beside a metric reading
    # 25 and the reader could not tell which half was wrong.
    decision = explain_target(settings, len(traded))
    computed = decision.slots
    explicit = getattr(settings.universe, "target", None)
    preference = str(getattr(settings.universe, "size_preference", "largest"))

    row = st.columns(3)
    row[0].metric("Instruments held", computed)
    row[1].metric("Chosen by", decision.bound_by)
    row[2].metric("Noise floor", f"{NOISE_FLOOR_PP:g}pp")

    if explicit:
        st.caption(
            f"Pinned to {explicit} by `universe.target`. Unset it to let the "
            "measurement decide."
        )
    elif curve:
        peak = max(curve, key=lambda k: curve[k])
        st.caption(
            f"Derived from a measured curve of return against size, read "
            f"through a noise floor of {NOISE_FLOOR_PP:g}pp rather than by "
            f"taking its best point. {decision.curve_note}. The peak is "
            f"{peak} at {curve[peak]:.1f}%, and it is **not** taken on its "
            f"own: changing only the day of the month a screen runs on moves "
            f"the same strategy by {NOISE_FLOOR_PP:g}pp, so sizes within that "
            f"of each other are indistinguishable and `{preference}` decides "
            f"between them."
        )
        # What the curve wanted before anything bounded it. Shown only when it
        # differs, because the gap is the whole point: the measurement can
        # want a book the balance cannot fund or the cap will not permit, and
        # a page that hides that reads as though the curve chose.
        if decision.curve_slots is not None and decision.curve_slots != computed:
            held_back = []
            if decision.max_target is not None and computed >= decision.max_target:
                held_back.append(
                    f"`universe.max_target` stops it at {decision.max_target}"
                )
            if decision.ceiling is not None:
                held_back.append(
                    f"the viability ceiling allows {decision.ceiling} "
                    f"({decision.ceiling_why})"
                )
            st.warning(
                f"**The curve alone would take {decision.curve_slots}.** It "
                f"holds {computed} because "
                + ("; ".join(held_back) if held_back
                   else "something below the curve bound it")
                + ". The viability ceiling is computed from the recorded "
                "balance and the broker's minimum deal sizes; `max_target` is "
                "a deliberate stop that no measurement argues for."
            )
        st.caption(
            "This is also the open-position cap, deliberately the same "
            "number: every name eligible but unheld is a candidate to rotate "
            "into, and each rotation pays a round trip."
        )
        # Drawn rather than tabulated, because the decision is a shape: a
        # curve, a band of sizes indistinguishable from its best, and the one
        # taken. A table makes the reader recompute all three in their head.
        #
        # The band is the rule itself, not decoration -- every size whose
        # measured CAGR falls inside it is a size the noise floor says is
        # tied with the peak, and `size_preference` breaks the tie. Seeing
        # which points sit in the band IS the justification.
        sizes = sorted(curve)
        values = [curve[k] for k in sizes]
        best = max(values)
        line_colour = theme.colour_for("ok", mode)
        soft = theme.colour_for("muted", mode)

        width, height = 720.0, 260.0
        left, right, top, bottom = 44.0, 16.0, 18.0, 34.0
        lo = min(min(values), best - NOISE_FLOOR_PP)
        hi = max(values)
        span = max(hi - lo, 1e-9)
        pad = span * 0.12
        lo, hi = lo - pad, hi + pad
        span = hi - lo

        def px(size):
            first, last = sizes[0], sizes[-1]
            reach = max(last - first, 1e-9)
            return left + (size - first) / reach * (width - left - right)

        def py(value):
            return top + (hi - value) / span * (height - top - bottom)

        band_top, band_bottom = py(best), py(best - NOISE_FLOOR_PP)
        points = " ".join(f"{px(k):.1f},{py(curve[k]):.1f}" for k in sizes)

        parts = [
            f'<svg viewBox="0 0 {width:.0f} {height:.0f}" width="100%" '
            f'role="img" aria-label="Measured annual return against universe '
            f'size, with the band of sizes indistinguishable from the best." '
            f'style="max-width:100%;height:auto;font:400 11px/1.3 inherit;">',
            # The decision band, behind everything.
            f'<rect x="{left:.1f}" y="{band_top:.1f}" '
            f'width="{width-left-right:.1f}" height="{max(band_bottom-band_top,1):.1f}" '
            f'fill="{soft}" opacity="0.16"/>',
            f'<line x1="{left:.1f}" y1="{band_bottom:.1f}" x2="{width-right:.1f}" '
            f'y2="{band_bottom:.1f}" stroke="{soft}" stroke-width="1" '
            f'stroke-dasharray="3 3" opacity="0.55"/>',
            # Baseline.
            f'<line x1="{left:.1f}" y1="{height-bottom:.1f}" x2="{width-right:.1f}" '
            f'y2="{height-bottom:.1f}" stroke="{soft}" stroke-width="1" opacity="0.35"/>',
            f'<polyline points="{points}" fill="none" stroke="{line_colour}" '
            f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>',
        ]
        for k in sizes:
            inside = best - curve[k] <= NOISE_FLOOR_PP
            chosen = k == computed
            radius = 6.0 if chosen else 4.0
            parts.append(
                f'<circle cx="{px(k):.1f}" cy="{py(curve[k]):.1f}" r="{radius:.1f}" '
                f'fill="{line_colour if inside else soft}" '
                f'stroke="{"#0B0C10" if mode == "dark" else "#FFFFFF"}" '
                f'stroke-width="2">'
                f"<title>{k} instruments: {curve[k]:.1f}% a year"
                f'{" -- inside the noise floor of the best" if inside else ""}'
                f'{" -- CHOSEN" if chosen else ""}</title></circle>'
            )
            parts.append(
                f'<text x="{px(k):.1f}" y="{height-bottom+15:.1f}" fill="{soft}" '
                f'text-anchor="middle">{k}</text>'
            )
        # The chosen point is direct-labelled, so it is never identified by
        # colour alone.
        if computed in curve:
            parts.append(
                f'<text x="{px(computed):.1f}" y="{py(curve[computed])-13:.1f}" '
                f'fill="{line_colour}" text-anchor="middle" '
                f'style="font-weight:700;">holds {computed}</text>'
            )
        parts.append(
            f'<text x="4" y="{band_top-4:.1f}" fill="{soft}">{best:.0f}%</text>'
        )
        parts.append(
            f'<text x="4" y="{band_bottom+11:.1f}" fill="{soft}">'
            f'{best-NOISE_FLOOR_PP:.0f}%</text>'
        )
        parts.append("</svg>")
        st.markdown("".join(parts), unsafe_allow_html=True)
        st.caption(
            f"Measured annual return against universe size. The shaded band "
            f"is the top {NOISE_FLOOR_PP:g}pp — every size inside it is tied "
            f"with the best by the noise floor, and `{preference}` breaks the "
            f"tie. Hover a point for its figure."
        )
    else:
        st.warning(
            f"No size curve is configured, so the count falls back to the "
            f"traded list ({len(traded)}). That is a fallback, not a "
            "measurement -- run the size sweep and record it."
        )
except Exception as error:  # noqa: BLE001 - a view must not break the page
    st.warning(f"The traded count could not be explained: {error}")

# --- what is in force, and where it came from ------------------------------ #
#
# The list itself has moved into the history table below: a selection and the
# selections before it are the same kind of thing, and showing the current
# one twice invited them to disagree -- which they did, because this section
# read `market.tickers` while the daemon traded the screen.
#
# What stays here is the part the table cannot carry: which source answered,
# and whether it can be trusted.
st.subheader("In force")
if traded_source == "config":
    st.caption(
        "From `market.tickers`, the fallback — `follow_screen` is off, so "
        "this is a deliberate human choice rather than a failure."
    )
elif traded_source == "unreadable":
    st.error(
        "The traded universe could not be resolved. Treat the list below as "
        "unverified rather than as what is being traded."
    )
elif traded_source.startswith("config ("):
    st.error(
        f"**The screen could not answer** ({traded_source}). The daemon opens "
        "nothing while this is true, and closes held positions on their own "
        "terms. `market.tickers` is shown as a reference, not as what is "
        "traded."
    )
else:
    st.caption(
        "From the screen's daily selection"
        + (f", recorded {str(traded_as_of)[:19]}" if traded_as_of else "")
        + ". Configuration is never rewritten — the selection is recorded as "
        "an observation and read back."
    )
if traded_stale:
    st.warning(
        "That selection is stale. An old decision is still a decision; "
        "silence about its age is the part that is not acceptable."
    )

row = st.columns(3)
row[0].metric("Instruments", len(traded))
row[1].metric("Source", "screen" if traded_source == "screen" else "fallback")
row[2].metric(
    "Recorded", str(traded_as_of)[:10] if traded_as_of else "—"
)

# --- where the rule and the account disagree ------------------------------- #
#
# Only shown when they DO disagree. With the screen followed they are the
# same list by construction, so a section permanently reading "no drift" is
# a section that trains you to skip it -- and the history table below already
# marks which selection is in force.
latest = screens[-1] if screens else {}
selected = [str(t) for t in (latest.get("selected") or ()) if str(t)]
joined = [t for t in selected if t not in traded]
left = [t for t in traded if t not in selected]

if joined or left:
    st.subheader("Drift")
    st.caption(
        f"The latest screen ({str(latest.get('as_of') or 'undated')[:19]}) "
        "and what is being traded are not the same list. That is expected "
        "while a selection is being refused — for staleness or a size "
        "mismatch — and otherwise worth explaining."
    )
    st.dataframe(
        pd.DataFrame(
            [{"instrument": t, "difference": "would join"} for t in joined]
            + [{"instrument": t, "difference": "would leave"} for t in left]
        ),
        hide_index=True,
        width="stretch",
    )

capped = list(latest.get("sector_capped") or ())
if capped:
    st.caption(
        "Held back by the sector cap rather than by rank: "
        + ", ".join(capped)
    )

for note in latest.get("notes") or ():
    st.caption(str(note))

# --- the history ------------------------------------------------------------ #
st.subheader("Recorded screens")
st.caption(
    "Append-only. A selection is a decision taken on evidence that has since "
    "moved, and one that can be edited afterwards is evidence of nothing."
)

# Names rather than counts. A count tells you something changed; the point
# of an append-only record is being able to see WHAT changed, and "3 joined"
# sends the reader somewhere else to find out which three.
rows = []
previous: list[str] = []

# Identity, not equality. Marking every row whose names MATCH the traded list
# looked equivalent and is not: consecutive screens correlate +0.97 to +0.99,
# so picking the same ten names two days running is the normal case, and set
# comparison then marked both as in force. Only one is -- the one
# `traded_universe` actually read -- and it reports which by timestamp.
#
# And only when the selection is actually BEING FOLLOWED. A selection refused
# for the wrong size now carries its date -- so the halt mail can say the
# screen is running rather than reporting "none on record" -- but a refused
# selection is not in force, it is the thing that was declined. Marking it
# would tell the reader the book holds names it does not hold.
in_force_at = (
    str(traded_as_of)[:19]
    if traded_as_of and not getattr(_traded, "screen_failed", False)
    else None
)
for row in screens:
    names = [str(t) for t in (row.get("selected") or ()) if str(t)]
    held = set(names)
    before = set(previous)
    as_of = str(row.get("as_of") or "?")[:19]
    rows.append({
        "as of": as_of,
        "n": len(names),
        "instruments": ", ".join(names),
        "joined": ", ".join(sorted(held - before)) if previous else "",
        "left": ", ".join(sorted(before - held)) if previous else "",
        # Marks the selection the daemon is acting on, so the reader never
        # has to work out which row is live.
        "in force": "yes" if in_force_at and as_of == in_force_at else "",
    })
    previous = names

st.dataframe(
    pd.DataFrame(rows[::-1]), hide_index=True, width="stretch",
    column_config={
        "instruments": st.column_config.TextColumn(width="large"),
        "joined": st.column_config.TextColumn(width="medium"),
        "left": st.column_config.TextColumn(width="medium"),
    },
)
if rows and not any(r["in force"] for r in rows):
    st.caption(
        "No row is marked in force, which means the traded list did not come "
        "from a recorded screen — see **In force** above for what it came "
        "from instead."
    )

st.caption(
    "The pool is US large caps that are large *now*, so it is drawn with "
    "hindsight: screening within it is reproducible, the pool itself is not "
    "a measurement. See `cfdinvest/universe/README.md`."
)
