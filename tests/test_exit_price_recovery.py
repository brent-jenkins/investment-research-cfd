"""An out-of-band close costs the forward record a trip, and need not.

A position closed outside the dealing path leaves no fill in this app's
record, so `record_departure` writes the close with no level -- absent rather
than invented, because a price that never traded is worse than no price.

That is right and it has a cost. `trips()` skips a close it cannot pair, so
the trip is dropped from realised P&L. Measured on 2026-09-12: realised read
-209.62 against a balance change of -246.04, and the gap is those dropped
trips. The forward record is the only trustworthy evidence this project
holds and it was quietly understating losses.

The broker knows the price -- TGT's close on 2026-09-10 is in the activity
log at 158.43 against a 156.10 open, read by hand at the time. Now it is
read by code.

The properties worth testing are not "does it find the price" but "does it
refuse to invent one", and "does it find the CLOSE rather than the open".

Mutations checked:

  M1710 an unrecoverable price recorded as zero rather than absent
  M1711 the opening fill returned as the close on a netting account
  M1712 the oldest matching activity taken instead of the newest
  M1713 a rejected or cancelled activity read as a fill
  M1714 the activity reader given a body or a non-GET method
"""
from __future__ import annotations

import pytest

from cfdinvest.broker.capital import client
from cfdinvest.persist import orders


def activity(epic, direction, level, status="ACCEPTED", when="2026-09-10T12:00:00"):
    """One activity row, shaped like the ones the account actually returns.

    Verified against the live account on 2026-09-12: rows carry `dateUTC`,
    `epic`, `status` and a `details` block, and an EXECUTED row reports
    `level=0` while the ACCEPTED row beside it carries the real fill. The
    zero-guard is not hypothetical.
    """
    return {
        "epic": epic,
        "status": status,
        "dateUTC": when,
        "details": {"direction": direction, "level": level},
    }


def test_the_closing_leg_is_taken_not_the_opening_one() -> None:
    """M1711: on a netting account an opposing order closes.

    A name opened and closed inside the same window has BOTH fills in the
    log. Taking the first match returns the OPEN, which would pair the trip
    against its own entry and report a P&L of roughly zero -- worse than
    dropping it, because it looks like a real answer.
    """
    log = [
        activity("TGT", "BUY", 158.43),    # the close of a short
        activity("TGT", "SELL", 156.10),   # the open
    ]

    assert client.closing_level(log, "TGT", direction="short") == 158.43


def test_a_long_is_closed_by_a_sell() -> None:
    log = [activity("ABC", "BUY", 10.0), activity("ABC", "SELL", 12.0)]

    assert client.closing_level(log, "ABC", direction="long") == 12.0


def test_the_newest_matching_close_wins_whatever_order_it_arrives_in() -> None:
    """M1712: this book holds the same name repeatedly within a week, so an
    older close is a different trip at a different price.

    **The fixture is OLDEST FIRST, because that is what the account
    returns.** Read live on 2026-09-12 the activity log came back
    oldest-first, not newest-first as the first version of this code
    documented -- and its fixture had been written newest-first to match the
    assumption, so the mutation passed against a shape the broker never
    sends. Sorting is what removes the assumption; this fixture is what
    stops it coming back.
    """
    oldest_first = [
        activity("MU", "BUY", 80.0, when="2026-09-08T10:00:00"),
        activity("MU", "BUY", 99.0, when="2026-09-11T10:00:00"),
    ]

    assert client.closing_level(oldest_first, "MU", direction="short") == 99.0

    # And the other order, so neither is privileged.
    assert client.closing_level(
        list(reversed(oldest_first)), "MU", direction="short"
    ) == 99.0


def test_a_rejected_activity_is_not_a_fill() -> None:
    """M1713: an order that did not execute has no price to report."""
    log = [
        activity("MU", "BUY", 99.0, status="REJECTED",
                 when="2026-09-11T12:00:00"),
        activity("MU", "BUY", 95.0, status="ACCEPTED",
                 when="2026-09-11T10:00:00"),
    ]

    # The rejected row is NEWER, so this also proves the sort does not simply
    # win over the status filter.
    assert client.closing_level(log, "MU", direction="short") == 95.0


def test_an_executed_row_reporting_zero_is_skipped_for_the_accepted_one() -> None:
    """Observed on the live account, not imagined.

    Capital returns a WORKING_ORDER row with `level=0` beside the POSITION
    row carrying the real fill. Without the zero-guard the recovery would
    write 0 as the exit price -- the precise thing leaving the field absent
    exists to prevent.
    """
    log = [
        activity("INTC", "BUY", 0, status="EXECUTED",
                 when="2026-09-08T20:20:10"),
        activity("INTC", "BUY", 104.57, status="ACCEPTED",
                 when="2026-09-08T20:20:10"),
    ]

    assert client.closing_level(log, "INTC", direction="short") == 104.57


def test_an_unreadable_price_stays_absent() -> None:
    """M1710: absence is never a zero.

    A level that cannot be parsed, or is zero, must leave the trip unpaired
    rather than pricing it at something that never traded.
    """
    assert client.closing_level([], "MU") is None
    assert client.closing_level(None, "MU") is None
    assert client.closing_level([activity("MU", "BUY", 0.0)], "MU") is None
    assert client.closing_level([activity("MU", "BUY", "junk")], "MU") is None
    assert client.closing_level([activity("OTHER", "BUY", 9.0)], "MU") is None


def test_the_recorded_departure_refuses_a_zero() -> None:
    """M1710, at the other end: the store must not accept one either."""
    assert orders._number(0) is None
    assert orders._number(-5) is None
    assert orders._number(None) is None
    assert orders._number("junk") is None
    assert orders._number(158.43) == pytest.approx(158.43)


def test_the_activity_reader_places_nothing() -> None:
    """M1714: `client.py` guarantees it cannot deal, and reading history
    must not become the exception that breaks it.

    No body and no method override -- a GET, on a module whose tests assert
    no dealing endpoint appears in it and that the only POST is the session.
    """
    import inspect
    import re

    source = inspect.getsource(client.fetch_activity)
    assert "/history/activity" in source

    # Inspect the CALL, not the whole function. Asserting `"data=" not in
    # source` passed against a mutation that added `data = b""` with spaces,
    # which is the substring trap this project keeps hitting -- the check
    # has to look at the thing that would actually make it a write.
    call = re.search(r"read\((?P<args>[^)]*)\)", source)
    assert call, "no read() call found"

    args = call.group("args")
    assert "data" not in args, f"a body would make this a write: read({args})"
    assert "method" not in args, (
        f"a method override would make this a write: read({args})"
    )


def test_the_reader_returns_empty_rather_than_raising() -> None:
    """A departure must still record when the log cannot be read.

    Empty means UNKNOWN, not "nothing happened", and the caller keeps
    level=None on the strength of it.
    """
    class _Session:
        ok = True
        host = "https://example.invalid"
        error = None

        @staticmethod
        def headers(_key):
            return {}

    def explode(*_args, **_kwargs):
        raise RuntimeError("network down")

    assert client.fetch_activity(_Session(), None, opener=explode) == []


def test_no_session_reads_nothing() -> None:
    class _Session:
        ok = False
        host = ""
        error = "no session"

        @staticmethod
        def headers(_key):
            return {}

    assert client.fetch_activity(_Session(), None) == []
