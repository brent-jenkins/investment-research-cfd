"""The forward experiments are read from what was known before 10:00.

Mutations checked:

  M2150 a story first seen AT 10:00 counted for that session
  M2151 the crowd's baseline including today's own reading
  M2152 the crowd signal followed rather than faded
  M2153 a null long share read as 0% long
"""
from __future__ import annotations

import pandas as pd

from cfdinvest.research import forward_reads as fr

DAY = pd.Timestamp("2026-10-15")   # EDT: 10:00 New York is 14:00 UTC


def test_news_counts_only_before_ten():
    """M2150."""
    events = pd.DataFrame([
        {"ticker": "AAA", "first_seen": "2026-10-15T13:59:59Z", "sign": 1},
        {"ticker": "BBB", "first_seen": "2026-10-15T14:00:00Z", "sign": 1},
        {"ticker": "CCC", "first_seen": "2026-10-14T13:59:59Z", "sign": 1},
    ])
    sides = fr.window_signs(events, [DAY])
    assert list(sides["ticker"]) == ["AAA"]


def _readings(values, today):
    rows = []
    for i, v in enumerate(values):
        day = pd.bdate_range(end="2026-10-14", periods=len(values))[i]
        rows.append({"at": f"{day:%Y-%m-%d}T13:00:00Z", "ticker": "AAA", "long_pct": v})
    rows.append({"at": "2026-10-15T13:00:00Z", "ticker": "AAA", "long_pct": today})
    return rows


def test_the_crowd_is_faded_against_its_own_past():
    """M2151, M2152: twenty days at 80% long, today 90% -> short."""
    days = list(pd.bdate_range(end="2026-10-15", periods=21))
    sides = fr.crowd_sides(_readings([80.0] * 20, 90.0), days)
    assert sides[sides["day"] == DAY]["side"].tolist() == [-1]
    # at the baseline exactly, no call: a baseline including today would
    # move and call anyway
    sides = fr.crowd_sides(_readings([80.0] * 10 + [85.0] * 10, 82.5), days)
    assert sides[sides["day"] == DAY].empty


def test_a_missing_reading_is_absent():
    """M2153."""
    days = list(pd.bdate_range(end="2026-10-15", periods=21))
    rows = _readings([80.0] * 20, 80.0)
    rows[-1]["long_pct"] = None
    sides = fr.crowd_sides(rows, days)
    assert sides[sides["day"] == DAY].empty
