"""The rolling direction model must not see past 10:00, nor train on today.

Mutations checked:

  M1950 the bar stamped 10:00 New York (still in progress) read as a feature
  M1951 today's own session volatility read unshifted
  M1952 the predicted session included in its own training window
  M1953 a logistic fit that cannot recover a planted signal
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from cfdinvest.research import rolling_direction as rd


def _hourly(seed=0):
    rng = np.random.default_rng(seed)
    stamps = pd.date_range("2026-06-01 08:00", "2026-08-14 23:00", freq="h", tz="UTC")
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.004, len(stamps))))
    open_ = np.r_[close[0], close[:-1]]
    return pd.DataFrame({
        "open": open_, "close": close,
        "high": np.maximum(open_, close) * 1.001, "low": np.minimum(open_, close) * 0.999,
        "spread_bp": 5.0,
    }, index=stamps)


DAYS = pd.bdate_range("2026-06-01", "2026-08-14")


def test_nothing_from_ten_onwards_on_the_day_reaches_a_feature():
    """M1950, M1951: rewrite every bar from 10:00 New York on the last day;
    the outcome must change and not one feature may."""
    bars = _hourly()
    before = rd.ticker_days(bars, None, DAYS).iloc[-1]
    ten = rd._clock(pd.DatetimeIndex([DAYS[-1]]), 10)[0]
    later = bars.index >= ten
    shaken = bars.copy()
    for column in ("open", "close", "high", "low"):
        shaken.loc[later, column] *= np.linspace(1.05, 0.9, later.sum())
    after = rd.ticker_days(shaken, None, DAYS).iloc[-1]

    assert before["move"] != after["move"], "the fixture did not move the outcome"
    for name in rd.FEATURES:
        if name.startswith("mkt_") or name.endswith("_vs_mkt") or name == "rel_volume":
            continue  # panel-level, or absent without daily bars
        assert before[name] == after[name] or (pd.isna(before[name]) and pd.isna(after[name])), (
            f"{name} read a bar from 10:00 onwards"
        )


def test_the_predicted_session_is_never_in_its_own_training_window():
    """M1952."""
    sessions = list(range(30))
    for i in range(5, 30):
        window = rd.training_days(sessions, i, 10)
        assert i not in window and max(window) == i - 1 and len(window) == min(10, i)


def test_the_logistic_fit_recovers_a_planted_sign():
    """M1953: a model that cannot find a real signal reports a null for free."""
    rng = np.random.default_rng(1)
    X = rng.normal(size=(2000, 3))
    y = (X[:, 1] + 0.5 * rng.normal(size=2000) > 0).astype(float)
    w = rd.fit_logistic(X, y)
    assert w[2] > 1.0 and abs(w[1]) < 0.3 and abs(w[3]) < 0.3
    assert ((rd.predict_logistic(w, X) > 0.5) == (y > 0)).mean() > 0.8
