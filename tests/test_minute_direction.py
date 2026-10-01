"""The minute model must read only minutes stamped before the decision.

Mutations checked:

  M1960 the decision minute itself read as the last close
  M1961 a lookback return reaching forward across a missing minute
  M1962 a 60-minute window ending at the decision rather than before it
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from cfdinvest.research import minute_direction as md

DAYS = pd.bdate_range("2026-06-01", "2026-07-17")


def _minutes(seed=0):
    rng = np.random.default_rng(seed)
    stamps = pd.date_range("2026-06-01 13:00", "2026-07-17 21:00", freq="min", tz="UTC")
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.0005, len(stamps))))
    open_ = np.r_[close[0], close[:-1]]
    return pd.DataFrame({
        "open": open_, "close": close,
        "high": np.maximum(open_, close) * 1.0002, "low": np.minimum(open_, close) * 0.9998,
        "volume": rng.integers(5, 50, len(stamps)).astype(float),
        "spread_bp": rng.uniform(3, 8, len(stamps)),
    }, index=stamps)


def _decision(day, hour):
    return (pd.Timestamp(day) + pd.Timedelta(hours=hour)).tz_localize(md.NEW_YORK).tz_convert("UTC")


def _row(rows, day, hour):
    return rows[(rows["day"] == day) & (rows["hour"] == hour)].iloc[0]


def test_nothing_from_the_decision_minute_onwards_reaches_a_feature():
    """M1960, M1962: rewrite every minute from 12:00 New York on the last
    day; the outcome must move and not one feature may."""
    bars, day = _minutes(), DAYS[-1]
    decision = _decision(day, 12)
    shaken = bars.copy()
    later = shaken.index >= decision
    for column in ("open", "close", "high", "low"):
        shaken.loc[later, column] *= 1.03
    shaken.loc[later, "volume"] *= 9
    shaken.loc[later, "spread_bp"] *= 4

    before, after = _row(md.ticker_rows(bars, DAYS), day, 12), _row(md.ticker_rows(shaken, DAYS), day, 12)
    assert before["move_close"] != after["move_close"] or before["cost_close"] != after["cost_close"]
    for name in md.FEATURES:
        if name.startswith("mkt_") or name.endswith("_vs_mkt"):
            continue  # panel-level
        assert np.isclose(before[name], after[name], equal_nan=True), f"{name} read the decision minute or later"


def test_a_missing_minute_does_not_pull_a_lookback_forward():
    """M1961: with the five minutes before the decision missing, the
    5-minute return must not reach the decision minute's open."""
    bars, day = _minutes(), DAYS[-1]
    decision = _decision(day, 12)
    gap = (bars.index >= decision - pd.Timedelta(minutes=5)) & (bars.index < decision)
    holed = bars[~gap].copy()
    base = _row(md.ticker_rows(holed, DAYS), day, 12)["r5"]
    holed.loc[holed.index >= decision, ["open", "close", "high", "low"]] *= 1.05
    assert np.isclose(_row(md.ticker_rows(holed, DAYS), day, 12)["r5"], base, equal_nan=True)
