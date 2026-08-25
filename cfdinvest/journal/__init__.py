"""The trading journal: what happened, and what was known when it did.

::

    from cfdinvest.journal import write_journal, load_entries

    write_journal(config)                    # composes what is new
    print(render_journal(load_entries(config)))

Also a page in the dashboard: **Journal**.

**An entry is written from what was known at the time and never rewritten.**
Regenerating a narrative later, with the outcome in hand, turns it into a
story about the result rather than the decision — and a good decision that
lost looks identical to a bad one that won once the result is written over
the reasoning. When an outcome arrives it is appended as a separate entry.

Nothing here recomputes anything: no P&L, no sizing, no verdicts. It reads
artefacts other layers wrote and adds English.
"""

from __future__ import annotations

from cfdinvest.journal.entry import (
    KINDS,
    JournalEntry,
    behavioural_note,
    compose_entry,
    compose_outcome_entry,
    execution_note,
    narrate,
    regime_note,
    what_changed,
)
from cfdinvest.journal.reader import (
    filter_entries,
    group_by_day,
    render_entry,
    render_journal,
)
from cfdinvest.journal.writer import build_entries, entry_key, write_journal
from cfdinvest.persist.journal import (
    append_entries,
    append_entry,
    last_entry,
    load_entries,
    load_journal_frame,
)

__all__ = [
    "KINDS",
    "JournalEntry",
    "compose_entry",
    "compose_outcome_entry",
    "narrate",
    "behavioural_note",
    "execution_note",
    "regime_note",
    "what_changed",
    "build_entries",
    "entry_key",
    "write_journal",
    "filter_entries",
    "render_entry",
    "render_journal",
    "group_by_day",
    "append_entry",
    "append_entries",
    "load_entries",
    "load_journal_frame",
    "last_entry",
]
