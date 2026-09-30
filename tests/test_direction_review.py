"""The direction review: pre-registered, so its dossier must be cut at 10:00.

Mutations checked:

  M1930 an hourly bar still in progress at 10:00 shown to the model
  M1931 today's daily bar shown to the model
  M1932 the window starting before Sonnet 5's release
"""
from __future__ import annotations

import pandas as pd

from cfdinvest.research import direction_review as dr

TEN = dr.ten_of("2026-08-12")  # 14:00 UTC in summer


def _hourly():
    stamps = pd.date_range(TEN - pd.Timedelta(days=3), TEN + pd.Timedelta(hours=6), freq="h", tz="UTC")
    return pd.DataFrame({"open": 100.0, "high": 101.0, "low": 99.0,
                         "close": [100 + i / 100 for i in range(len(stamps))]}, index=stamps)


def _daily():
    days = pd.date_range(TEN.normalize() - pd.Timedelta(days=40), TEN.normalize(), freq="D", tz="UTC")
    return pd.DataFrame({"close": 100.0, "volume": 1e6}, index=days)


def _dossier():
    return dr.build_dossier(
        ticker="AAA", company="Aaa Inc", sector="tech", ten=TEN, hourly=_hourly(),
        daily=_daily(), engine={"direction": "long"}, market={"today": 0.0, "five_days": 0.0},
        filings=[],
    )


def test_no_bar_the_model_sees_was_incomplete_at_ten():
    """M1930: the bar stamped 10:00 New York is still in progress then,
    and so is every later one."""
    rows = [line for line in _dossier().splitlines() if line[:4].isdigit()]
    assert any(r.startswith("2026-08-12 09:00,") for r in rows), "the last complete bar is missing"
    for later in ("2026-08-12 10:00,", "2026-08-12 11:00,", "2026-08-12 13:00,"):
        assert not any(r.startswith(later) for r in rows), f"the model was shown the bar starting {later}"


def test_todays_daily_bar_is_not_shown():
    """M1931: a daily bar dated today includes the close being predicted."""
    daily_section = _dossier().split("Daily bars")[1].split("Trend engine")[0]
    dated = [line.split(",")[0] for line in daily_section.splitlines() if line[:4].isdigit()]
    assert dated[-1] == "2026-08-11", f"the last daily bar shown is {dated[-1]}, not yesterday's"
    assert "2026-08-12" not in dated


def test_the_window_starts_after_the_model_was_released():
    """M1932: the Models API gives Sonnet 5's release as 2026-06-29."""
    assert dr.START >= "2026-06-29"
    assert dr.MODEL == "claude-sonnet-5"
