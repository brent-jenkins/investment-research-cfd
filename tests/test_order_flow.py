"""The order-flow test reads only 09:30-10:00, and reads the side the right way.

Pre-registered in CLAUDE.md on 2026-11-03. Each property below is one a
wrong implementation would get wrong while still producing a number:

  M2130 B and A swapped, inverting every call
  M2131 the window's end inclusive, so a 10:00:00 trade is read
  M2132 the shuffle across all sessions, so a day's mix is not kept
  M2133 the paired comparison over all days, not only shared ones
  M2134 the spending cap checked after the spend, crossing it by a day
  M2135 an unsigned window read as perfectly balanced (0) rather than absent
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from cfdinvest.research import order_flow as of


def _trades(rows):
    frame = pd.DataFrame(rows, columns=["ts_recv", "side", "size", "price", "symbol"])
    frame["ts_recv"] = pd.to_datetime(frame["ts_recv"], utc=True)
    return frame


def test_the_window_is_new_york_time_on_both_sides_of_the_clock_change():
    winter_start, winter_end = of.window("2026-03-06")    # EST
    summer_start, summer_end = of.window("2026-07-15")    # EDT
    assert (winter_start.hour, winter_start.minute) == (14, 30)
    assert (summer_start.hour, summer_start.minute) == (13, 30)
    assert winter_end - winter_start == pd.Timedelta(minutes=30)


def test_a_buyer_initiated_trade_is_buying_pressure():
    """M2130: B is the buy aggressor in Databento's trades schema."""
    start, end = of.window("2026-07-15")
    flows = of.aggregate(_trades([
        ("2026-07-15T13:31:00Z", "B", 300, 100.0, "AAA"),
        ("2026-07-15T13:32:00Z", "A", 100, 100.1, "AAA"),
    ]), start, end)
    assert of.imbalance(flows["buy"], flows["sell"])[0] == pytest.approx(0.5)


def test_nothing_at_or_after_ten_and_nothing_before_half_past_nine():
    """M2131: the bound is half-open, on receipt time."""
    start, end = of.window("2026-07-15")
    flows = of.aggregate(_trades([
        ("2026-07-15T13:29:59Z", "B", 999, 99.0, "AAA"),
        ("2026-07-15T13:30:00Z", "A", 10, 100.0, "AAA"),
        ("2026-07-15T13:59:59Z", "B", 30, 101.0, "AAA"),
        ("2026-07-15T14:00:00Z", "B", 999, 120.0, "AAA"),
    ]), start, end)
    row = flows.loc["AAA"]
    assert (row["buy"], row["sell"], row["trades"]) == (30, 10, 2)
    assert (row["first_px"], row["last_px"]) == (100.0, 101.0)


def test_unsigned_volume_is_counted_but_never_read_as_balance():
    """M2135: an absent reading must not look like a balanced one."""
    start, end = of.window("2026-07-15")
    flows = of.aggregate(_trades([("2026-07-15T13:40:00Z", "N", 500, 100.0, "AAA")]), start, end)
    assert flows.loc["AAA", "unsigned"] == 500
    assert np.isnan(of.imbalance(flows["buy"], flows["sell"])[0])


def _frame(days=6, names=4, seed=1):
    rng = np.random.default_rng(seed)
    rows = []
    for d in pd.bdate_range("2026-01-05", periods=days):
        for i in range(names):
            buy, sell = rng.integers(1, 100, 2)
            rows.append({"day": d, "ticker": f"N{i}", "move": rng.normal(0, 0.01),
                         "mkt_move": 0.0, "cost": 0.001, "buy": float(buy), "sell": float(sell),
                         "unsigned": 10.0, "trades": 5.0,
                         "first_px": 100.0, "last_px": 100.0 + rng.normal()})
    loud = pd.DataFrame(rows)[["day", "ticker", "move", "mkt_move", "cost"]]
    flows = pd.DataFrame(rows).drop(columns=["move", "mkt_move", "cost"])
    return of.calls(loud, flows)


def test_the_shuffle_keeps_each_sessions_mix():
    """M2132: permuted inside a session, so long/short counts per day hold."""
    frame = _frame()
    shuffled = of.shuffled_within_session(frame, "side_flow")
    before = frame.groupby("day")["side_flow"].apply(sorted)
    after = frame.assign(s=shuffled).groupby("day")["s"].apply(sorted)
    assert before.equals(after)
    assert not shuffled.equals(frame["side_flow"])


def test_flow_identical_to_price_beats_it_by_nothing():
    frame = _frame()
    part = of._with_side(frame, frame["side_flow"])
    result = of.paired(part, part)
    assert result["mean_pct"] == 0.0 and result["days"] == frame["day"].nunique()


def test_the_pairing_uses_only_sessions_both_traded():
    """M2133: a day the control sat out is not a free win for the flow."""
    frame = _frame()
    flow = of._with_side(frame, frame["side_flow"])
    first = flow["day"].min()
    price = flow[flow["day"] != first]
    assert of.paired(flow, price)["days"] == flow["day"].nunique() - 1


def test_the_cap_is_never_crossed(tmp_path):
    """M2134: priced first, stopped before the day that would cross it."""
    days = {pd.Timestamp(f"2026-01-0{i}"): ["AAA"] for i in range(5, 10)}
    fetched = []

    def fake_fetch(day, symbols, key):
        fetched.append(day)
        return of.aggregate(None, *of.window(day)), 0.4, 0

    ledger = of.run_fetch(days, tmp_path, "k", cap=1.0, fetch=fake_fetch,
                          quote=lambda day, symbols, key: 0.4)
    assert ledger["spent"] <= 1.0 and len(fetched) == 2
    assert "stopped_at" in ledger
    # resumable: what is on disk is not fetched again
    again = []
    of.run_fetch(days, tmp_path, "k", cap=1.0,
                 fetch=lambda d, s, k: again.append(d) or (of.aggregate(None, *of.window(d)), 0.4, 0),
                 quote=lambda d, s, k: 0.4)
    assert again == []
    assert json.loads((tmp_path / "spend.json").read_text())["days"] == 2


def test_the_verdict_needs_the_control_beaten_as_well():
    good = {"net": {"t": 2.5, "h1_pct": 0.1, "h2_pct": 0.1}, "gross": {"t": 3.0}}
    assert of.verdict(good, {"t": 2.1}).startswith("pass")
    assert not of.verdict(good, {"t": 1.0}).startswith("pass")
    assert of.verdict({"net": {"t": 0.1}, "gross": {"t": 0.2}}, {"t": 0.0}).startswith("otherwise")
