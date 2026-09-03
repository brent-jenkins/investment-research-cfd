"""The margin series: what the broker requires, and how close the account is.

Prompted by a question the app could not answer: does Capital change its
margin factor on positions that are already open? It publishes one per
instrument and reserves the right to vary it -- tiered by size, raised in
volatile conditions and around events -- and the app had been fetching the
figure since the account opened and discarding it.

It cannot be answered retrospectively. The API serves a current value with no
history, so a change that happened yesterday is gone. The series has to run
before the question can be asked, which is why this store exists ahead of
anything that reads it.

The second question is the one that decides whether a drawdown is survivable:
``balance - available`` is the margin in use, and the engine has no margin
model at all, so every drawdown figure this project holds is an upper bound
on what the account would actually live through.

M334 factors written every pass, so the file grows without saying more
M335 an unchanged factor indistinguishable from an unobserved one
M336 a first sighting reported as a change
M337 utilisation reported as zero when nothing has been recorded
"""

from __future__ import annotations

import pytest

from cfdinvest.config import AppConfig, PathConfig
from cfdinvest.persist import margin


@pytest.fixture
def lake(tmp_path) -> AppConfig:
    return AppConfig(paths=PathConfig(data_root=tmp_path / "data"))


def test_an_unchanged_factor_is_not_written_every_pass():
    """The dealing pass runs every fifteen minutes; the factor rarely moves.

    Writing each observation would be 40MB a year to say the same thing.
    """
    observed = {"AAPL": 0.20, "TSLA": 0.20}
    known = {"AAPL": 0.20, "TSLA": 0.20}
    seen = {"AAPL": "2026-09-03T12:00:00+00:00",
            "TSLA": "2026-09-03T12:00:00+00:00"}

    rows = margin.changed_factors(
        observed, known, now="2026-09-03T12:15:00+00:00", last_seen=seen)
    assert rows == [], f"an unchanged factor was written again: {rows}"


def test_an_unchanged_factor_still_gets_a_daily_heartbeat():
    """Absence named: "unchanged" and "unobserved" must stay different.

    Without the heartbeat, a gap in the file could mean the factor held
    steady or that nothing looked, and only one of those is evidence.
    """
    rows = margin.changed_factors(
        {"AAPL": 0.20}, {"AAPL": 0.20},
        now="2026-09-04T12:00:00+00:00",
        last_seen={"AAPL": "2026-09-03T11:00:00+00:00"},
    )
    assert len(rows) == 1, "a day passed with no heartbeat recorded"
    assert rows[0]["changed"] is False, (
        "a heartbeat was recorded as a change, which would invent an event"
    )


def test_a_moved_factor_is_recorded_as_a_change():
    """The event the series exists to catch."""
    rows = margin.changed_factors(
        {"AAPL": 0.25}, {"AAPL": 0.20}, now="2026-09-03T12:15:00+00:00",
        last_seen={"AAPL": "2026-09-03T12:00:00+00:00"},
    )
    assert len(rows) == 1
    assert rows[0]["changed"] is True, "a real move was not flagged"
    assert rows[0]["previous"] == 0.20
    assert rows[0]["margin_factor"] == 0.25


def test_a_first_sighting_is_not_a_change():
    """Recording an instrument for the first time invents no event.

    A first sighting flagged as a change would make the answer to "do they
    move it" a function of how many instruments the screen has rotated
    through, which is a fact about us rather than about the broker.
    """
    rows = margin.changed_factors({"AAPL": 0.20}, {}, now="2026-09-03T12:00:00+00:00")
    assert len(rows) == 1, "a first sighting was not recorded at all"
    assert rows[0]["changed"] is False, (
        "a first sighting was reported as a change in the broker's terms"
    )


def test_changes_are_read_back_and_first_sightings_are_not(lake):
    """End to end: only genuine moves reach `factor_changes`."""
    margin.record_margin(
        margin.changed_factors({"AAPL": 0.20}, {}, now="2026-09-01T12:00:00+00:00"),
        lake,
    )
    margin.record_margin(
        margin.changed_factors(
            {"AAPL": 0.30}, margin.latest_factors(lake),
            now="2026-09-02T12:00:00+00:00",
        ),
        lake,
    )

    assert margin.latest_factors(lake) == {"AAPL": 0.30}
    changes = margin.factor_changes(lake)
    assert len(changes) == 1, f"expected one move, got {changes}"
    assert changes[0]["previous"] == 0.20 and changes[0]["margin_factor"] == 0.30


def test_utilisation_with_no_record_is_unchecked_not_zero(lake):
    """A system that never looked is not an account using no margin."""
    summary = margin.usage_summary(lake)
    assert summary["checked"] is False
    assert summary["latest"] is None and summary["peak"] is None


def test_utilisation_reports_the_latest_and_the_worst(lake):
    """The peak is the number that matters; the latest is the one people read."""
    margin.record_margin([
        {"at": "2026-09-01T12:00:00+00:00", "kind": "usage",
         "balance": 5000.0, "available": 2500.0, "used": 2500.0},
        {"at": "2026-09-02T12:00:00+00:00", "kind": "usage",
         "balance": 5000.0, "available": 1500.0, "used": 3500.0},
        {"at": "2026-09-03T12:00:00+00:00", "kind": "usage",
         "balance": 5000.0, "available": 3000.0, "used": 2000.0},
    ], lake)

    summary = margin.usage_summary(lake)
    assert summary["checked"] is True
    assert summary["observations"] == 3
    assert summary["latest"] == pytest.approx(0.40), (
        "the latest reading is not the last row"
    )
    assert summary["peak"] == pytest.approx(0.70), (
        "the peak is not the worst reading, so a near-miss would go unseen"
    )


def test_a_zero_balance_does_not_fabricate_a_utilisation(lake):
    """Absence again: an unusable denominator reports nothing, not zero."""
    margin.record_margin([
        {"at": "2026-09-01T12:00:00+00:00", "kind": "usage",
         "balance": 0.0, "available": 0.0, "used": 0.0},
    ], lake)
    summary = margin.usage_summary(lake)
    assert summary["checked"] is True
    assert summary["latest"] is None, (
        "a zero balance produced a utilisation figure rather than an absence"
    )
