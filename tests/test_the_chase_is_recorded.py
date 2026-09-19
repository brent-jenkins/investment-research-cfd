"""A refusal records whether it was a fresh decision or a repeat of one.

Measuring the stale-intent loop on 2026-09-19 needed `entered_bars_ago`
joined to every last-look refusal. It could not be done from the order
store, because that number reached the LOG and not the RECORD -- so it was
done by parsing the journal and matching on timestamps, which is a scratch
join nobody can re-run in six months.

The measurement it made possible is the reason to keep it:

    last-look refusal events                     790
      age 0  a fresh signal the quote refused    233   29.5%
      age >0 the engine chasing an entry         557   70.5%

    instrument-days refused only while stale       0 of 30
    fresh refusal came first                      20 of 20

Seven in ten refusals are the same decision re-taken, and none of them
creates a blocked instrument-day. That retired a claim this file had carried
since 2026-09-10 -- that the elevated blocked-outright rate IS the loop --
and it is the kind of claim that only gets retired if the number is in the
record rather than in a log that rotates.

THE FIELD IS CARRIED, NEVER ACTED ON. `tests/test_stale_intent_is_visible.py`
still fails the build if a non-zero age starts refusing an open without
somebody saying so. This makes the loop countable from the store; it does
not enforce anything, and the 2026-09-19 measurement is why enforcement
would be pointless rather than dangerous -- it would remove 557 repeated
refusals and zero trades.

ABSENCE IS ITS OWN ANSWER, which is the one judgement here. A refusal row
written before this existed has no age, and counting it as fresh would read
a silent gap as a measurement. It is counted as `age_unknown_events`, so a
store that predates the field reports honestly rather than reporting zero
chases.

Mutations checked:

  M1750 the age dropped from the Intent, so nothing reaches the row
  M1751 the age dropped from the row `record_plan` writes
  M1752 a missing age counted as fresh rather than as unknown
  M1753 a chase counted as fresh (the > 0 test inverted or removed)
  M1754 the age read from the row but not from the engine's intent row
"""

from __future__ import annotations

import pandas as pd
import pytest

from cfdinvest.broker.capital.bridge import Intent
from cfdinvest.persist import orders as orders_store


class _Report:
    """The shape `record_plan` reads."""

    halted = False
    halt_reason = ""
    balance = 5000.0

    def __init__(self, intents):
        self.intents = tuple(intents)


@pytest.fixture
def store(tmp_path, monkeypatch):
    """An order store in a temporary tree."""
    path = tmp_path / "orders" / "orders.jsonl"
    path.parent.mkdir(parents=True)
    monkeypatch.setattr(orders_store, "_path", lambda config=None, account=None: path)
    return path


def refusal(ticker: str, age: int | None) -> Intent:
    return Intent(
        ticker=ticker, epic=ticker, direction="long",
        held_units=0.0, target_units=0.0, action="refused",
        reason="no longer clears the bar", price=10.0,
        entered_bars_ago=age,
    )


def test_the_age_reaches_the_record(store) -> None:
    """M1750, M1751: the field dropped from the Intent or from the row.

    The whole point is that the number survives into the store. A log line
    rotates; this does not.
    """
    orders_store.record_plan(
        _Report([refusal("AAA", 0), refusal("BBB", 3)]),
        now=pd.Timestamp("2026-09-19 14:00", tz="UTC"),
    )

    rows = orders_store.load_orders()
    ages = {row["ticker"]: row.get("entered_bars_ago") for row in rows}
    assert ages == {"AAA": 0, "BBB": 3}, (
        "the age did not reach the order store, so the loop can only be "
        "measured by parsing the journal again"
    )


def test_a_chase_is_counted_apart_from_a_fresh_refusal(store) -> None:
    """M1753: the chase counted as fresh.

    Two refusals on one day, one of each kind. The split is what says
    whether an alarming refusal count is the gate working or the same
    decision echoing.
    """
    orders_store.record_plan(
        _Report([refusal("AAA", 0), refusal("BBB", 2), refusal("CCC", 4)]),
        now=pd.Timestamp("2026-09-19 14:00", tz="UTC"),
    )

    summary = orders_store.refusal_summary(now=pd.Timestamp("2026-09-19 18:00", tz="UTC"))

    assert summary["fresh_events"] == 1, "a fresh refusal was not counted as fresh"
    assert summary["chase_events"] == 2, (
        f"chases counted as {summary['chase_events']}, expected 2 -- the "
        "split is what the field exists for"
    )
    assert summary["age_unknown_events"] == 0


def test_a_row_with_no_age_is_unknown_rather_than_fresh(store) -> None:
    """M1752: absence read as a measurement.

    Every refusal written before 2026-09-19 has no age. Counting those as
    fresh would report "no chases" about a store that simply cannot say,
    which is the reassuring half of a distinction this project keeps
    insisting on: "not measured" and "measured and fine" are different.
    """
    orders_store.record_plan(
        _Report([refusal("AAA", None), refusal("BBB", 1)]),
        now=pd.Timestamp("2026-09-19 14:00", tz="UTC"),
    )

    summary = orders_store.refusal_summary(now=pd.Timestamp("2026-09-19 18:00", tz="UTC"))

    assert summary["age_unknown_events"] == 1, (
        "a refusal with no recorded age was counted as something it cannot "
        "be known to be"
    )
    assert summary["fresh_events"] == 0
    assert summary["chase_events"] == 1


def test_the_split_does_not_disturb_the_instrument_day_counts(store) -> None:
    """The figures the panel already reports must not move.

    `blocked` is instrument-days and the 2026-09-19 measurement turns on it
    NOT being inflated by chases. Three refusal events on one name and day
    are one blocked instrument-day, however many of them were chases.
    """
    orders_store.record_plan(
        _Report([refusal("AAA", 0)]), now=pd.Timestamp("2026-09-19 14:00", tz="UTC"),
    )
    orders_store.record_plan(
        _Report([refusal("AAA", 1)]), now=pd.Timestamp("2026-09-19 15:00", tz="UTC"),
    )
    orders_store.record_plan(
        _Report([refusal("AAA", 2)]), now=pd.Timestamp("2026-09-19 16:00", tz="UTC"),
    )

    summary = orders_store.refusal_summary(now=pd.Timestamp("2026-09-19 18:00", tz="UTC"))

    assert summary["blocked"] == 1, (
        "three refusals of one name on one day became more than one blocked "
        "instrument-day, which would inflate the rate the loop was wrongly "
        "blamed for"
    )
    assert summary["considered"] == 1
    assert (summary["fresh_events"], summary["chase_events"]) == (1, 2)


def test_the_bridge_reads_the_age_off_the_engine_row() -> None:
    """M1754: the field exists and the bridge never populates it.

    Asserted against the module's source because the alternative is standing
    up a whole broker pass, and the failure being guarded is a missing
    assignment rather than a wrong value. A field that is always None is the
    same as no field, and this project has shipped that twice.
    """
    from pathlib import Path

    import cfdinvest.broker.capital.bridge as bridge_module

    source = Path(bridge_module.__file__).read_text(encoding="utf-8")
    assert 'row.get("entered_bars_ago")' in source, (
        "the bridge never reads the age off the engine's intent row, so "
        "every refusal records None and the split is always unknown"
    )
    assert "entered_bars_ago=age," in source, (
        "the age is read and not attached to the refusal Intent"
    )
