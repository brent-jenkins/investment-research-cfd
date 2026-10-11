"""A badly disagreeing last look leaves enough behind to replay it.

Mutations checked:

  M2030 a chase (entered on an earlier bar) kept as a disagreement
  M2031 a small, marginal shortfall kept every pass
  M2032 a flat live reading ("no longer a signal") not kept because it
        carries no number -- the 2026-10-08 case exactly
  M2033 the snapshot losing the prices, so nothing can be replayed
  M2034 the dealing pass never calling it
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace

from cfdinvest.persist import last_look


def _refused(ticker, engine, live, *, age=0, reason="no longer clears the bar"):
    return SimpleNamespace(ticker=ticker, direction="long", action="refused",
                           reason=reason, entered_bars_ago=age,
                           engine_confidence=engine, live_confidence=live)


def test_only_fresh_and_far_apart_is_kept():
    """M2030, M2031, M2032."""
    intents = [
        _refused("FAR", 0.84, 0.10),
        _refused("FLAT", 0.84, None, reason="no longer a signal"),
        _refused("CHASE", 0.84, 0.10, age=3),
        _refused("NEAR", 0.78, 0.72),
        _refused("UNCHECKED", 0.84, None, reason="not re-checked"),
        SimpleNamespace(ticker="OPEN", action="open"),
    ]
    assert [d["ticker"] for d in last_look.disagreements(intents)] == ["FAR", "FLAT"]


def test_a_snapshot_carries_what_a_replay_needs(tmp_path, monkeypatch):
    """M2033: the moment, the candidates, every price, every reading."""
    monkeypatch.setattr(last_look, "_path", lambda config=None, account=None: tmp_path / "s.jsonl")
    assert last_look.record_snapshot(
        at="2026-10-08 14:15:40+00:00", candidates=["COP", "XOM"],
        prices={"COP": 133.22, "XOM": 118.5},
        readings={"COP": {"direction": "flat", "confidence": 0.0,
                          "reason": "below_min_size|correlation_breakdown", "as_of": "x"}},
        disagreeing=[{"ticker": "COP"}],
    )
    (row,) = last_look.load_snapshots()
    assert row["at"].startswith("2026-10-08 14:15") and row["candidates"] == ["COP", "XOM"]
    assert row["prices"] == {"COP": 133.22, "XOM": 118.5}
    assert "correlation_breakdown" in row["readings"]["COP"]["reason"]


def test_the_dealing_pass_keeps_it():
    """M2034: on an uncommented line -- the dealer has no dry run."""
    from cfdinvest.schedule import app

    live = [line.split("#", 1)[0] for line in inspect.getsource(app._command_trade_capital).splitlines()]
    assert any("last_look_store.record_snapshot(" in line for line in live)
    assert any("last_look_store.disagreements(report.intents)" in line for line in live)
