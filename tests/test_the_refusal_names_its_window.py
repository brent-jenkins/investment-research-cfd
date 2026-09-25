"""A refused open says which window refused it.

`open_allowed` is False for two unrelated reasons -- too soon after the open,
or too near the close -- and until 2026-09-25 both wrote "within the opening
window". Friday's store:

    'within the opening window'   167 rows, by hour (UTC)
        13: 64   14: 33          the opening window
        17: 8   18: 21   19: 41  the CLOSING window, wearing its name

Seventy rows a day labelled as something they were not, in the store this
project reads its refusal rates out of. It changed no behaviour. It would
have been misread the first time anybody counted opening-window refusals,
which is the shape of half the retractions in CLAUDE.md.

WHY THE CALENDAR AND NOT THE HOUR. The hour separates the two today and
stops doing so on 1 November, when US clocks change and the session moves an
hour in UTC. A heuristic keyed on 13:00-14:00 would mis-assign silently
within five weeks of being written. The bridge is handed `session_ending`,
the same calendar answer that closed the window, rather than re-deriving it.

NEITHER REASON IS THE LAST LOOK. Both are limits. `LAST_LOOK_REASONS` is the
single owner of which refusals belong to the gate, and a test in
`test_orders.py` already fails the build if a limit is counted as one.

Mutations checked:

  M1810 the split reverted, so both windows write the opening reason again
  M1811 the choice inverted, so each window carries the other's name
  M1812 the new reason added to LAST_LOOK_REASONS, so a clock limit is
        counted as the gate refusing
"""

from __future__ import annotations

from tests.test_broker_capital_bridge import only, plan, signal

from cfdinvest.persist.orders import LAST_LOOK_REASONS


def refused(**kwargs):
    report = plan([signal("AAPL")], open_allowed={"AAPL": False}, **kwargs)
    intent = only(report, "AAPL")
    assert intent.action == "refused", intent.action
    return intent.reason


def test_the_opening_window_keeps_its_own_name() -> None:
    """M1811: the names swapped.

    Nothing says the session is ending, so the only reason left is the
    opening one.
    """
    assert refused() == "within the opening window"
    assert refused(session_ending={"AAPL": False}) == "within the opening window"


def test_the_closing_window_says_so() -> None:
    """M1810: the split reverted, and the defect with it."""
    reason = refused(session_ending={"AAPL": True})

    assert reason != "within the opening window", (
        "a name refused because the session is ending was recorded as "
        "being inside the OPENING window -- seventy rows a day of that on "
        "2026-09-25, in the store refusal rates are read from"
    )
    assert reason == "too near the session close to open"


def test_a_name_the_window_allows_is_not_refused_either_way() -> None:
    """The reason is decided only when the open is refused.

    `session_ending` chooses a label. It must never itself refuse an open
    that `open_allowed` permitted, or a reporting fix becomes a gate nobody
    measured.
    """
    report = plan(
        [signal("AAPL")], open_allowed={"AAPL": True},
        session_ending={"AAPL": True},
    )
    assert only(report, "AAPL").action == "open", (
        "session_ending refused an open on its own -- a label has become a "
        "rule, which is how an unmeasured gate gets into the dealing path"
    )


def test_neither_window_is_the_last_look() -> None:
    """M1812: a clock limit counted as the gate refusing.

    The refusal rate this project reads is the last look's. Folding the
    session windows into it would add seventy-plus rows a day that say
    nothing about the check.
    """
    for reason in ("within the opening window", "too near the session close to open"):
        assert reason not in LAST_LOOK_REASONS, (
            f"{reason!r} is counted as the last look refusing, so the "
            f"clock would inflate the gate's refusal rate"
        )
