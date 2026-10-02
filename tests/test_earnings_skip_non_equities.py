"""Earnings are asked only of shares.

Mutations checked:

  M2000 an index, future, pair or coin still sent to the vendor
  M2001 a share skipped as well
"""
from __future__ import annotations

import sys
import types

from cfdinvest.ingestion import earnings


def test_only_shares_reach_the_vendor(monkeypatch):
    asked = []

    class Ticker:
        def __init__(self, symbol):
            asked.append(symbol)
            self.earnings_dates = None

    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(Ticker=Ticker))
    earnings.fetch_earnings_dates(["^GSPC", "GC=F", "EURUSD=X", "BTC-USD", "MU", "BRK-B"])
    assert asked == ["MU", "BRK-B"], asked
