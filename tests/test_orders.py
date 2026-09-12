"""The record reconciliation compares the account against.

Without it there is no way to tell a position this app opened from one that
arrived some other way, and "there is a position in NFLX" says nothing about
whether it is expected.

M316 the plan and the result merged into one row
M317 a refusal not recorded, so the limits leave no evidence
M318 an unconfirmed order counted as a position
M319 open positions tracked separately rather than replayed
M320 the trading job running outside the session, or before the run
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
import pytest

from cfdinvest.config import AppConfig, PathConfig
from cfdinvest.persist import orders


@pytest.fixture
def lake(tmp_path) -> AppConfig:
    return AppConfig(paths=PathConfig(data_root=tmp_path / "data"))


@dataclass(frozen=True)
class Intent:
    ticker: str
    epic: str
    action: str
    direction: str = "long"
    held_units: float = 0.0
    target_units: float = 10.0
    price: float | None = 100.0
    notional: float | None = 1000.0
    reason: str = ""
    deal_id: str = ""


@dataclass(frozen=True)
class Report:
    intents: tuple
    halted: bool = False
    halt_reason: str = ""
    balance: float | None = 5000.0
    unexplained: tuple = ()
    missing: tuple = ()


@dataclass(frozen=True)
class Result:
    ok: bool = True
    deal_id: str = "D1"
    size: float | None = 10.0
    level: float | None = 100.0
    status: str = "OPEN"
    reference: str = "R1"
    error: str = ""
    reason: str = ""
    direction: str = "BUY"


def test_a_refusal_is_recorded_alongside_the_orders(lake):
    """M317: the limits leaving no evidence they did anything.

    A store that kept only the orders would show a system that never
    declined, and the refusals are the only record of a limit working.
    """
    report = Report(intents=(
        Intent("AAPL", "AAPL", "open"),
        Intent("NKE", "NKE", "refused", reason="below the minimum deal size"),
    ))
    assert orders.record_plan(report, lake, now=pd.Timestamp("2026-08-28", tz="UTC")) == 2

    rows = orders.load_orders(lake)
    actions = {r["ticker"]: r["action"] for r in rows}
    assert actions == {"AAPL": "open", "NKE": "refused"}
    assert any("minimum" in (r.get("reason") or "") for r in rows)


def test_the_plan_and_the_result_are_separate_rows(lake):
    """M316: the one state that needs a person, merged away.

    They differ exactly when something went wrong -- an order placed and not
    confirmed -- and a merged row would lose the case the store exists for.
    """
    intent = Intent("AAPL", "AAPL", "open")
    orders.record_plan(Report(intents=(intent,)), lake)
    orders.record_result(
        intent, Result(ok=False, error="PLACED BUT UNCONFIRMED"), lake
    )

    rows = orders.load_orders(lake)
    assert [r["kind"] for r in rows] == ["plan", "result"]
    assert rows[1]["ok"] is False
    assert "UNCONFIRMED" in rows[1]["error"]


def test_an_unconfirmed_order_is_not_a_position(lake):
    """M318: the record running ahead of the account.

    Reconciliation would then report the account as missing something that
    was never there, and halt on the app's own optimism.
    """
    intent = Intent("AAPL", "AAPL", "open")
    orders.record_result(intent, Result(ok=False, error="unconfirmed"), lake)

    assert orders.open_orders(lake) == {}


def test_open_positions_are_replayed_from_the_rows(lake):
    """M319: a second copy of "what is open" to keep true.

    The day the two disagreed neither would be trusted, so there is one --
    replayed the way the confirm layer replays confirmations.
    """
    opened = Intent("AAPL", "AAPL", "open")
    orders.record_result(opened, Result(deal_id="D1"), lake)
    assert set(orders.open_orders(lake)) == {"AAPL"}

    closed = Intent("AAPL", "AAPL", "close")
    orders.record_result(closed, Result(deal_id="D1", status="CLOSED"), lake)
    assert orders.open_orders(lake) == {}


def test_a_resize_leaves_the_position_open(lake):
    intent = Intent("AAPL", "AAPL", "resize", target_units=20.0)
    orders.record_result(intent, Result(deal_id="D1", size=20.0), lake)

    live = orders.open_orders(lake)
    assert set(live) == {"AAPL"}
    assert live["AAPL"]["size"] == pytest.approx(20.0)


def test_nothing_recorded_is_an_empty_book_not_an_error(lake):
    assert orders.load_orders(lake) == []
    assert orders.open_orders(lake) == {}


def test_a_truncated_row_loses_one_record_not_the_history(lake):
    orders.record_result(Intent("AAPL", "AAPL", "open"), Result(), lake)
    path = orders._path(lake)
    path.write_text(path.read_text(encoding="utf-8") + "{broken\n", encoding="utf-8")

    assert len(orders.load_orders(lake)) == 1


def test_the_trading_job_runs_after_the_signals_and_only_in_session():
    """M320: acting on signals that have not been written yet.

    It reads the run's output, so it must follow it; and it places orders,
    so the session gate that governs everything else governs it.
    """
    from cfdinvest.schedule import daemon

    history = {
        "poll-live-quotes": "2026-08-28T14:59:00Z",
        # Ingestion has just written, which is what makes the pipeline owed.
        # Since the jobs moved into separate units the analysis is gated on
        # a refreshed lake rather than on a clock it shared with ingestion.
        "ingest-intraday": "2026-08-28T14:59:00Z",
        "ingest-daily": "2026-08-28T03:00:00Z",
        "monitoring-snapshot": "2026-08-28T03:05:00Z",
        "screen-universe": "2026-08-01T03:10:00Z",
    }
    due = daemon.jobs_due(
        pd.Timestamp("2026-08-28T15:00:00Z"), history, market_open=True
    )
    # **The property survives; the mechanism that enforces it changed.**
    #
    # Dealing used to be a job in this list, ordered after the run so it
    # could not act on signals that had not been written. That ordering is
    # also why the account could not be CLOSED for an entire session on
    # 2026-09-11: the jobs run sequentially, so dealing queued behind the
    # pipeline and a wedged pipeline stranded the book.
    #
    # Dealing now runs as `cfdinvest-dealer.service` on its own timer, and
    # "must follow the run" is enforced by `decision_is_stale` instead --
    # the dealer refuses to OPEN on a decision bar older than two bar
    # spacings, whatever order anything ran in. `tests/test_stale_run_guard.py`
    # owns that, and `tests/test_dealer_is_separate.py` owns the requirement
    # that exactly one thing deals.
    assert "trade-capital" not in due, (
        "the daemon deals again, so it and the dealer unit would both deal "
        "against one account"
    )
    assert due.index("run-intraday") < due.index("record-spreads"), (
        "the cost of a trade is read after the signals that decide it"
    )

    from cfdinvest.persist import intent as intent_store

    assert hasattr(intent_store, "decision_is_stale"), (
        "dealing left the job ordering and nothing replaced the guarantee "
        "that it acts on a run that exists"
    )
    # The book decides and the account mirrors it, so the book must have
    # run. Reversed, the account would mirror the previous pass and act one
    # cycle late on every entry and exit.
    assert "paper-book" not in due, (
        "the paper book was retired on 2026-08-30; the account now takes its "
        "positions from the engine's own run rather than from a second book"
    )

    shut = daemon.jobs_due(
        pd.Timestamp("2026-08-28T22:00:00Z"), history, market_open=False
    )
    assert "trade-capital" not in shut


# --------------------------------------------------------------------------- #
# Why a position was closed, recorded rather than lost
# --------------------------------------------------------------------------- #
# M-live: every close in the order store read "reason=(none recorded)", so
# "did the time stop fire or did the signal reverse" was unanswerable from
# the log that exists to answer it. The engine writes one of stop_out,
# take_profit, stop_loss, time_stop or signal on every trade row; nothing
# carried it to the broker record.


def test_the_intents_reason_survives_when_the_broker_gives_none():
    """record_result wrote the BROKER's reason, which is empty on success.

    That single fallback is why every row said nothing: the bridge's own
    wording never reached the store either.
    """
    import inspect

    from cfdinvest.persist import orders

    source = inspect.getsource(orders.record_result)
    assert 'getattr(intent, "reason"' in source, (
        "the intent's reason is discarded, so a close explains nothing"
    )


def test_completed_trips_pairs_opens_with_closes(tmp_path, monkeypatch):
    """Trips, not orders. The store records orders and "how is it doing" is
    asked of trips, so the pairing has one owner rather than one per caller.
    """
    import json

    from cfdinvest.persist import orders as store

    path = tmp_path / "orders" / "orders.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(store, "_path", lambda config=None, account=None: path)

    rows = [
        # A short that made money: sold at 100, bought back at 90.
        {"kind": "result", "action": "open", "epic": "AAA", "ok": True,
         "status": "OPEN", "error": "", "level": 100.0, "units": 2.0,
         "direction": "SELL", "at": "2026-09-01T14:00:00+00:00"},
        {"kind": "result", "action": "close", "epic": "AAA", "ok": True,
         "status": "CLOSED", "error": "", "level": 90.0, "units": 2.0,
         "direction": "BUY", "at": "2026-09-01T18:00:00+00:00",
         "reason": "time_stop"},
        # A long that lost: bought at 50, sold at 45.
        {"kind": "result", "action": "open", "epic": "BBB", "ok": True,
         "status": "OPEN", "error": "", "level": 50.0, "units": 3.0,
         "direction": "BUY", "at": "2026-09-01T15:00:00+00:00"},
        {"kind": "result", "action": "close", "epic": "BBB", "ok": True,
         "status": "CLOSED", "error": "", "level": 45.0, "units": 3.0,
         "direction": "SELL", "at": "2026-09-02T14:00:00+00:00",
         "reason": "signal"},
        # Still open: must not appear as a trip.
        {"kind": "result", "action": "open", "epic": "CCC", "ok": True,
         "status": "OPEN", "error": "", "level": 10.0, "units": 1.0,
         "direction": "BUY", "at": "2026-09-02T15:00:00+00:00"},
    ]
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")

    trips = store.completed_trips()

    assert len(trips) == 2, "an unclosed position was counted as a trip"
    by_name = {t["ticker"]: t for t in trips}
    # The SHORT made money as the price fell -- sign handled from the open's
    # own direction, not the close's, which is the opposite by construction.
    assert by_name["AAA"]["pnl"] == pytest.approx(20.0)
    assert by_name["BBB"]["pnl"] == pytest.approx(-15.0)
    assert by_name["AAA"]["reason"] == "time_stop"
    assert by_name["BBB"]["reason"] == "signal"


def test_an_unconfirmed_order_is_not_a_trip(tmp_path, monkeypatch):
    """A deal reference the broker never turned into a position is not a
    trade, and counting it would put a phantom in the win rate."""
    import json

    from cfdinvest.persist import orders as store

    path = tmp_path / "orders" / "orders.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(store, "_path", lambda config=None, account=None: path)

    rows = [
        {"kind": "result", "action": "open", "epic": "AAA", "ok": False,
         "status": "REJECTED", "error": "no", "level": 100.0, "units": 2.0,
         "direction": "SELL", "at": "2026-09-01T14:00:00+00:00"},
        {"kind": "result", "action": "close", "epic": "AAA", "ok": False,
         "status": "CLOSED", "error": "POSITION STILL OPEN -- HTTP 403",
         "level": 90.0, "units": 2.0, "direction": "BUY",
         "at": "2026-09-01T18:00:00+00:00"},
    ]
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")

    assert store.completed_trips() == []


# --------------------------------------------------------------------------- #
# The last look, surfaced.
#
# `advisor/livecheck.py` refuses an open when the signal no longer stands at
# the moment of dealing. It blocked roughly a fifth of candidate opens over
# the account's first week and nothing reported it: the dealing mail is
# suppressed when a pass places nothing, so a pass where the gate refused
# everything sent no mail at all.
#
# M321 refusals counted per row, so a name refused twice reads as two
# M322 no record at all reported as a gate that refused nothing
# M323 the same refusal mailed every fifteen minutes until it clears
# M324 a limit's refusal counted as the last look's
# --------------------------------------------------------------------------- #


def _plan(rows):
    """A report-shaped object carrying intents, as record_plan expects."""
    @dataclass(frozen=True)
    class Report:
        intents: tuple
        halted: bool = False
        halt_reason: str = ""
        balance: float | None = 5_000.0

    return Report(intents=tuple(rows))


def test_refusals_are_counted_per_instrument_day_not_per_pass(lake):
    """The dealing pass runs every fifteen minutes and records every intent.

    A name refused at 14:00 and again at 14:15 is one decision described
    twice. Counting rows roughly triples the rate, which would make a gate
    that fired once look like a gate firing constantly.
    """
    refusal = Intent("AAPL", "AAPL", "refused", reason="no longer a signal")
    for stamp in ("2026-09-01T14:00:00+00:00", "2026-09-01T14:15:00+00:00",
                  "2026-09-01T14:30:00+00:00"):
        orders.record_plan(_plan([refusal]), lake, now=stamp)

    summary = orders.refusal_summary(lake, days=3650)
    assert summary["checked"] is True
    assert summary["blocked"] == 1, (
        f"three passes describing one refusal counted as {summary['blocked']}"
    )
    assert summary["considered"] == 1
    assert summary["reasons"] == {"no longer a signal": 1}


def test_a_refusal_that_later_opens_is_not_counted_as_blocked(lake):
    """Refused at 14:00, cleared at 14:30: the gate delayed it, not blocked it."""
    orders.record_plan(
        _plan([Intent("AAPL", "AAPL", "refused",
                      reason="no longer clears the bar")]),
        lake, now="2026-09-01T14:00:00+00:00",
    )
    orders.record_plan(
        _plan([Intent("AAPL", "AAPL", "open")]),
        lake, now="2026-09-01T14:30:00+00:00",
    )

    summary = orders.refusal_summary(lake, days=3650)
    assert summary["blocked"] == 0
    assert summary["opened_anyway"] == 1
    assert summary["considered"] == 1


def test_no_order_record_is_unchecked_rather_than_a_quiet_gate(lake):
    """Absence named. "Never dealt" and "refused nothing" are different."""
    summary = orders.refusal_summary(lake)
    assert summary["checked"] is False
    assert summary["blocked"] == 0
    # And the moment there is a record, the same reading becomes a real zero.
    orders.record_plan(_plan([Intent("AAPL", "AAPL", "open")]), lake,
                       now="2026-09-01T14:00:00+00:00")
    after = orders.refusal_summary(lake, days=3650)
    assert after["checked"] is True
    assert after["blocked"] == 0


def test_a_limit_refusal_is_not_the_last_look(lake):
    """Only the gate's own reasons count, or the panel measures the caps."""
    for reason in ("outside the exchange session", "within the opening window",
                   "no price", "capped by the per-instrument limit"):
        orders.record_plan(
            _plan([Intent("AAPL", "AAPL", "refused", reason=reason)]),
            lake, now="2026-09-01T14:00:00+00:00",
        )
    summary = orders.refusal_summary(lake, days=3650)
    assert summary["blocked"] == 0, (
        f"a limit's refusal was attributed to the last look: {summary['reasons']}"
    )
    assert summary["reasons"] == {}


def test_the_same_refusal_is_reported_once_a_day_not_once_a_pass(lake):
    """The mail must not repeat a name every fifteen minutes until it clears."""
    refusal = Intent("AAPL", "AAPL", "refused", reason="no longer a signal")

    first = orders.last_look_refusals_today(
        [refusal], lake, now="2026-09-01T14:00:00+00:00")
    assert [i.ticker for i in first] == ["AAPL"], (
        "the first refusal of the day was not reported"
    )

    orders.record_plan(_plan([refusal]), lake, now="2026-09-01T14:00:00+00:00")
    again = orders.last_look_refusals_today(
        [refusal], lake, now="2026-09-01T14:15:00+00:00")
    assert again == [], "the same refusal was reported twice in one day"

    # A new day is news again -- the position it would have opened is a
    # different position.
    tomorrow = orders.last_look_refusals_today(
        [refusal], lake, now="2026-09-02T14:00:00+00:00")
    assert [i.ticker for i in tomorrow] == ["AAPL"]


def test_only_the_last_looks_reasons_reach_the_mail(lake):
    """A cap doing its job is not news; the gate changing its mind is."""
    capped = Intent("MSFT", "MSFT", "refused", reason="no price")
    faded = Intent("AAPL", "AAPL", "refused", reason="no longer clears the bar")
    reported = orders.last_look_refusals_today(
        [capped, faded], lake, now="2026-09-01T14:00:00+00:00")
    assert [i.ticker for i in reported] == ["AAPL"]


# --------------------------------------------------------------------------- #
# A resize must leave the RECORD describing the position, not the order.
#
# The account is in netting mode -- "trades in different direction will close
# existing trade first", confirmed 2026-09-03 -- so a reduction goes as an
# opposing order. Trimming a long from 100 to 60 sends SELL 40. Recorded as
# the order, the record said the position was 40 units SHORT where the
# account held 60 LONG: wrong in size and inverted in side.
#
# reconcile() could not catch it. It compares which epics are held against
# which are recorded, not how much of each, so the divergence would have been
# silent.
#
# M348 the record taking its size from the order rather than the position
# M349 the record taking its side from the order, inverting a trim
# M350 an unreadable fill recorded as a flat position
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Filled:
    size: float
    direction: str = "BUY"
    level: float = 100.0
    deal_id: str = "d1"
    reference: str = "r1"
    status: str = "OPEN"
    ok: bool = True
    error: str = ""
    reason: str = ""


def test_trimming_a_long_records_the_remaining_position(lake):
    """SELL 40 against 100 long leaves 60 long, not 40 short."""
    trim = Intent("AAPL", "AAPL", "resize", direction="long",
                  held_units=100.0, target_units=60.0)
    orders.record_result(trim, Filled(size=40.0, direction="SELL"), lake,
                         now="2026-09-03T14:00:00+00:00")

    held = orders.open_orders(lake)["AAPL"]
    assert held["size"] == pytest.approx(60.0), (
        f"the record says {held['size']} units where the account holds 60; "
        "it took the size from the order rather than the position"
    )
    assert str(held["direction"]).upper() not in ("SELL", "SHORT"), (
        f"trimming a long recorded the position as {held['direction']}; the "
        "opposing order's side was mistaken for the position's"
    )


def test_adding_to_a_long_records_the_larger_position(lake):
    add = Intent("AAPL", "AAPL", "resize", direction="long",
                 held_units=100.0, target_units=140.0)
    orders.record_result(add, Filled(size=40.0, direction="BUY"), lake,
                         now="2026-09-03T14:00:00+00:00")
    assert orders.open_orders(lake)["AAPL"]["size"] == pytest.approx(140.0)


def test_reducing_a_short_records_it_as_still_short(lake):
    """Held -100 to -60: a BUY of 40 that leaves a 60-unit short."""
    trim = Intent("AAPL", "AAPL", "resize", direction="short",
                  held_units=-100.0, target_units=-60.0)
    orders.record_result(trim, Filled(size=40.0, direction="BUY"), lake,
                         now="2026-09-03T14:00:00+00:00")

    held = orders.open_orders(lake)["AAPL"]
    assert held["size"] == pytest.approx(60.0)
    assert str(held["direction"]).upper() in ("SELL", "SHORT"), (
        f"reducing a short recorded it as {held['direction']}; buying back "
        "40 of a 100-unit short leaves a short, not a long"
    )


def test_a_partial_fill_records_what_actually_happened(lake):
    """Filled 25 of a 40-unit trim leaves 75, not the 60 that was asked for."""
    trim = Intent("AAPL", "AAPL", "resize", direction="long",
                  held_units=100.0, target_units=60.0)
    orders.record_result(trim, Filled(size=25.0, direction="SELL"), lake,
                         now="2026-09-03T14:00:00+00:00")
    assert orders.open_orders(lake)["AAPL"]["size"] == pytest.approx(75.0), (
        "the record took the intended target rather than the filled size"
    )


def test_an_unreadable_fill_is_not_recorded_as_flat(lake):
    """Unknown must not read as closed -- that is the dangerous way to be wrong."""
    add = Intent("AAPL", "AAPL", "open", direction="long",
                 held_units=0.0, target_units=40.0)
    orders.record_result(add, Filled(size=None, direction="BUY"), lake,
                         now="2026-09-03T14:00:00+00:00")
    held = orders.open_orders(lake).get("AAPL") or {}
    assert held.get("size") != 0.0, (
        "an unreadable fill was recorded as a flat position, which reads as "
        "closed and would hide a live position from reconciliation"
    )


def test_an_open_is_unchanged_by_the_position_fields(lake):
    """Held is zero on an open, so order and position agree -- as before."""
    add = Intent("AAPL", "AAPL", "open", direction="long",
                 held_units=0.0, target_units=40.0)
    orders.record_result(add, Filled(size=40.0, direction="BUY"), lake,
                         now="2026-09-03T14:00:00+00:00")
    held = orders.open_orders(lake)["AAPL"]
    assert held["size"] == pytest.approx(40.0)
    assert str(held["direction"]).upper() in ("BUY", "LONG")
