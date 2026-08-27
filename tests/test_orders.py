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
        "ingest-daily": "2026-08-28T03:00:00Z",
        "monitoring-snapshot": "2026-08-28T03:05:00Z",
        "screen-universe": "2026-08-01T03:10:00Z",
    }
    due = daemon.jobs_due(
        pd.Timestamp("2026-08-28T15:00:00Z"), history, market_open=True
    )
    assert "trade-capital" in due
    assert due.index("run-intraday") < due.index("trade-capital")
    assert due.index("record-spreads") < due.index("trade-capital"), (
        "the cost of a trade is read after the orders that pay it"
    )

    shut = daemon.jobs_due(
        pd.Timestamp("2026-08-28T22:00:00Z"), history, market_open=False
    )
    assert "trade-capital" not in shut
