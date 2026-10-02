"""A corrected open replaces its leg; a close takes the open it names.

Found 2026-10-02 in SPGI's record: on 2026-09-10 two legs were re-written
with their side corrected, repeating their deal ids. Appended rather than
replaced, they paired with later, unrelated closes -- one opened on
2026-09-10 and "closed" on 2026-09-29 for +176.43 that never happened.
The rows below are SPGI's own, trimmed.

Mutations checked:

  M1990 a repeated deal id appended as a new open
  M1991 a close paired oldest-first even when it names its open
"""
from __future__ import annotations

import json

import pytest

from cfdinvest.persist import orders as store


def _row(at, action, direction, units, level, deal):
    return {"kind": "result", "action": action, "epic": "SPGI", "ok": True, "error": "",
            "status": "OPEN" if action == "open" else "CLOSED", "direction": direction,
            "units": units, "level": level, "deal_id": deal, "at": at}


ROWS = [
    _row("2026-09-10T19:48:53", "open", "SELL", 8.64, 413.14, "be19"),
    _row("2026-09-10T19:48:53", "open", "SELL", 8.67, 412.13, "e7f7"),
    # The correction: the same two legs, re-written.
    _row("2026-09-10T20:04:51", "open", "SELL", 8.64, 413.14, "be19"),
    _row("2026-09-10T20:04:51", "open", "SELL", 8.67, 412.13, "e7f7"),
    # Closed by name, the second leg first. Both really closed at 411.76;
    # this one is moved to 411.50 so a close paired with the wrong open is
    # visible -- at one price the two pairings are indistinguishable, which
    # is how the first version of this test passed a FIFO mutation.
    _row("2026-09-11T14:36:23", "close", "BUY", 8.67, 411.50, "e7f7"),
    _row("2026-09-11T14:36:24", "close", "BUY", 8.64, 411.76, "be19"),
    _row("2026-09-11T17:18:45", "open", "SELL", 11.0, 407.82, "60fe"),
    _row("2026-09-11T19:19:14", "close", "BUY", 11.0, 409.71, "6101"),
    _row("2026-09-29T14:39:09", "open", "SELL", 10.75, 391.61, "6a69"),
    _row("2026-09-29T19:32:00", "close", "BUY", 10.75, 391.78, "6a6c"),
]


@pytest.fixture
def trips(tmp_path, monkeypatch):
    path = tmp_path / "orders.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in ROWS))
    monkeypatch.setattr(store, "_path", lambda config=None, account=None: path)
    return store.completed_trips()


def test_a_corrected_leg_is_one_position_not_two(trips):
    """M1990: four trips, one per real position, and none spanning weeks."""
    assert len(trips) == 4
    for trip in trips:
        assert str(trip["closed_at"])[:10] in (str(trip["opened_at"])[:10], "2026-09-11")
    assert sum(t["pnl"] for t in trips) == pytest.approx(
        (413.14 - 411.76) * 8.64 + (412.13 - 411.50) * 8.67
        + (407.82 - 409.71) * 11.0 + (391.61 - 391.78) * 10.75
    )


def test_a_close_takes_the_open_it_names(trips):
    """M1991: the e7f7 close comes first and must not take be19's entry."""
    by_entry = {round(t["entry"], 2): t for t in trips}
    assert by_entry[412.13]["exit"] == pytest.approx(411.50)
    assert by_entry[413.14]["exit"] == pytest.approx(411.76)
