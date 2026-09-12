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


def activity(epic, direction, level, status="ACCEPTED"):
    return {
        "epic": epic,
        "status": status,
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


def test_the_newest_matching_close_wins() -> None:
    """M1712: the log is newest first and this book holds the same name
    repeatedly within a week. An older close is a different trip."""
    log = [
        activity("MU", "BUY", 99.0),
        activity("MU", "BUY", 80.0),
    ]

    assert client.closing_level(log, "MU", direction="short") == 99.0


def test_a_rejected_activity_is_not_a_fill() -> None:
    """M1713: an order that did not execute has no price to report."""
    log = [
        activity("MU", "BUY", 99.0, status="REJECTED"),
        activity("MU", "BUY", 95.0, status="ACCEPTED"),
    ]

    assert client.closing_level(log, "MU", direction="short") == 95.0


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
