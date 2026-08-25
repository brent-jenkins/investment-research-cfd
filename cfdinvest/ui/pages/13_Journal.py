"""Journal — what you did, and what you knew when you did it.

Entries are written from the artefacts as they stood at the time and are
never rewritten. Where an outcome was recorded it sits as its own entry,
beside the decision rather than over it — a good decision that lost and a bad
one that won look identical once the result is written over the reasoning.

Nothing on this page recomputes anything.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parents[3])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from cfdinvest.journal import (  # noqa: E402
    JournalEntry,
    filter_entries,
    group_by_day,
    load_entries,
    write_journal,
)
from cfdinvest.persist import journal as journal_store  # noqa: E402
from cfdinvest.ui import components  # noqa: E402

data = components.page_setup(
    st,
    "Journal",
    icon="📓",
    subtitle="What you did, and what you knew when you did it. Written at the "
    "time and never rewritten.",
)
config = data["config"]

KIND_ICON = {
    "trade": "📈", "considered": "🤔", "outcome": "🏁", "note": "✍",
}

# --------------------------------------------------------------------------- #
# Compose whatever is new
# --------------------------------------------------------------------------- #
try:
    entries = load_entries(config)
except Exception as error:  # noqa: BLE001 - the journal must not go dark
    st.error(f"**The journal could not be read.** {error}")
    st.stop()

left, right = st.columns([3, 1])
with right:
    if st.button("Compose new entries", width="stretch"):
        try:
            written = write_journal(config)
        except Exception as error:  # noqa: BLE001
            st.error(f"Could not compose: {error}")
        else:
            if written:
                st.success(f"Wrote {len(written)} entr(ies).")
                entries = load_entries(config)
            else:
                st.info("Nothing new to write.")

with left:
    if not entries:
        components.render_note(
            st,
            "The journal is empty. Entries are composed from **confirmations** "
            "— what you actually did — so an unconfirmed trade leaves no "
            "entry. Record fills on the Confirm Trades page, then compose.",
        )

# --------------------------------------------------------------------------- #
# Filters
# --------------------------------------------------------------------------- #
if entries:
    instruments = sorted(
        {e.instrument for e in entries if e.instrument}
    )
    cells = st.columns(3)
    chosen_instrument = cells[0].selectbox(
        "Instrument", options=["All", *instruments]
    )
    chosen_kind = cells[1].selectbox(
        "Kind", options=["All", *sorted({e.kind for e in entries})]
    )
    days = cells[2].number_input(
        "Days back", min_value=1, max_value=3650, value=90, step=30
    )

    shown = filter_entries(
        entries,
        instrument=None if chosen_instrument == "All" else chosen_instrument,
        kind=None if chosen_kind == "All" else chosen_kind,
        since=pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=int(days)),
    )

    cells = st.columns(4)
    cells[0].metric("Entries shown", len(shown))
    cells[1].metric("In the journal", len(entries))
    cells[2].metric(
        "Instruments", len({e.instrument for e in shown if e.instrument})
    )
    cells[3].metric(
        "Last entry",
        components.short_moment(entries[-1].timestamp) if entries else components.DASH,
    )

    st.divider()

    # ----------------------------------------------------------------------- #
    # The diary
    # ----------------------------------------------------------------------- #
    for day, day_entries in sorted(
        group_by_day(shown).items(), key=lambda pair: (pair[0] is None, pair[0]),
        reverse=True,
    ):
        st.markdown(f"#### {components.day(day) if day else 'Undated'}")

        for entry in sorted(
            day_entries, key=lambda e: str(e.timestamp), reverse=True
        ):
            with st.container(border=True):
                header, badge = st.columns([4, 1])
                header.markdown(
                    f"{KIND_ICON.get(entry.kind, '•')} **{entry.narrative}**"
                )
                if entry.verdict:
                    badge.markdown(
                        f'<div style="text-align:right;">'
                        f'<span class="cfd-readonly">{entry.verdict}</span></div>',
                        unsafe_allow_html=True,
                    )
                st.caption(components.moment(entry.timestamp))

                # Three lenses, kept apart. Running them together mixes
                # evidence about you with evidence about the market, and the
                # two lead to different corrections.
                for label, text in (
                    ("Execution", entry.execution_note),
                    ("Market", entry.regime_note),
                    ("You", entry.behavioural_note),
                ):
                    if text:
                        st.markdown(f"**{label}.** {text}")

                if entry.changed:
                    with st.expander("What changed since the entry before"):
                        for change in entry.changed:
                            st.markdown(f"- {change}")

                if entry.notes:
                    st.info(f"Your note: {entry.notes}")

    st.divider()

# --------------------------------------------------------------------------- #
# Add a note of your own
# --------------------------------------------------------------------------- #
st.subheader("Add a note")

components.render_note(
    st,
    "Your own words, kept alongside the composed entries. Notes are appended "
    "like everything else — nothing in the journal is ever edited, because a "
    "diary that agrees with whatever you now think is not evidence.",
)

with st.form("journal_note"):
    cells = st.columns([1, 3])
    note_instrument = cells[0].text_input("Instrument", value="")
    note_text = cells[1].text_area("Note", value="", height=80)
    submitted = st.form_submit_button("Add to the journal", type="primary")

if submitted:
    if not note_text.strip():
        st.error("Nothing to add.")
    else:
        entry = JournalEntry(
            timestamp=pd.Timestamp.now(tz="UTC"),
            kind="note",
            instrument=note_instrument.strip().upper() or None,
            narrative=note_text.strip(),
            notes=note_text.strip(),
            source="dashboard",
        )
        path = journal_store.append_entry(entry, config)
        if path is None:
            st.error("**Not written.** The journal could not be appended to.")
        else:
            st.success("Added.")
            st.caption(f"Written to `{path}`")
