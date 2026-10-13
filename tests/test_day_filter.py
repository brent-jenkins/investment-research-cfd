"""The weather-vane test may not see the day it decides.

Every reading in ``research.day_filter`` decides whether to trade a session,
so a reading that includes that session's own outcome scores a perfect
filter out of nothing. Each test below changes the decided day's data and
asserts the decision does not move -- the property, not a value.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from cfdinvest.research import day_filter as df

DAYS = pd.bdate_range("2026-03-02", periods=90, tz="UTC")


def _hourly(seed: int = 1) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Two names, hourly bars 13:00-19:00 UTC, New York summer-time hours."""
    rng = np.random.default_rng(seed)
    stamps = [d + pd.Timedelta(hours=h) for d in DAYS for h in range(13, 20)]
    index = pd.DatetimeIndex(stamps)
    close = pd.DataFrame(100 * np.exp(np.cumsum(rng.normal(0, 0.004, (len(index), 2)), axis=0)),
                         index=index, columns=["AAA", "BBB"])
    return close, close.shift(1).bfill()


def test_the_primary_reading_never_includes_its_own_day():
    gross = pd.Series(np.linspace(-0.01, 0.01, len(DAYS)), index=DAYS)
    day = DAYS[40]
    before = df.own_recent_gross(gross)[day]
    changed = gross.copy()
    changed[day] = 5.0
    assert df.own_recent_gross(changed)[day] == before


def test_a_reading_is_compared_only_with_readings_before_it():
    reading = pd.Series(np.arange(len(DAYS), dtype=float), index=DAYS)
    day = DAYS[70]
    first = df.above_trailing_median(reading)[day]
    later = reading.copy()
    later[DAYS[71]:] = -1e9
    assert df.above_trailing_median(later)[day] == first
    # the day's own value is not in its own median: a spike reads "above"
    spiked = reading.copy()
    spiked[:] = 1.0
    spiked[day] = 2.0
    assert df.above_trailing_median(spiked)[day] == 1.0


def test_the_warm_up_decides_nothing():
    reading = pd.Series(1.0, index=DAYS)
    out = df.above_trailing_median(reading)
    assert out.iloc[: df.MEDIAN_LOOKBACK].isna().all()


def test_vix_is_the_previous_close_not_the_day_itself():
    vix = pd.Series(np.arange(len(DAYS), dtype=float), index=DAYS.tz_localize(None))
    got = df.prior_close_value(vix, DAYS[1:])
    assert (got.to_numpy() == np.arange(len(DAYS) - 1)).all()


def test_trendiness_does_not_read_the_day_it_decides():
    close, _ = _hourly()
    day = DAYS[30]
    before = df.efficiency_ratio(close)[day]
    changed = close.copy()
    changed.loc[close.index.normalize() == day] *= 1.5
    assert np.isfinite(before)
    assert df.efficiency_ratio(changed)[day] == before


def test_dispersion_does_not_read_the_day_it_decides():
    close, _ = _hourly()
    day = DAYS[30]
    before = df.dispersion(close)[day]
    changed = close.copy()
    changed.loc[close.index.normalize() == day, "AAA"] *= 1.5
    assert df.dispersion(changed)[day] == before


def test_the_opening_move_reads_only_the_ten_oclock_open():
    close, open_ = _hourly()
    day = DAYS[30]                      # March 2026, after the US clock change
    before = df.opening_move(close, open_)[day]
    later = open_.copy()
    later.loc[(open_.index.normalize() == day) & (open_.index.hour > 14)] *= 2.0
    changed_close = close.copy()
    changed_close.loc[close.index.normalize() == day] *= 2.0
    assert np.isfinite(before)
    assert df.opening_move(changed_close, later)[day] == before


def test_a_day_is_measured_on_the_equity_it_started_with():
    index = pd.DatetimeIndex(["2026-03-02 19:00", "2026-03-03 14:00",
                              "2026-03-03 19:00"], tz="UTC")
    equity = pd.Series([1000.0, 1010.0, 1020.0], index=index)
    pnl = pd.DataFrame({"intraday": [0.0, 15.0, 15.0], "gap": [0.0, 0.0, 0.0],
                        "spread": [0.0, -5.0, -5.0]}, index=index)
    row = df.day_rows(pnl, equity, [pd.Timestamp("2026-03-03", tz="UTC")]).iloc[0]
    assert np.isclose(row["net"], 0.02)
    assert np.isclose(row["gross"], 0.03)
    assert np.isclose(row["cost"], -0.01)


def test_undecided_days_are_on_neither_side():
    book = pd.DataFrame({"net": [0.01, -0.01, 0.5], "gross": [0.0, 0.0, 0.0]},
                        index=DAYS[:3])
    out = df.score(book, pd.Series([1.0, 0.0, np.nan], index=DAYS[:3]))
    assert out["days"] == 2 and out["traded"] == 1
    assert out["net"] == 0.01 and out["skipped_net"] == -0.01


def test_the_random_control_separates_skill_from_luck():
    net = pd.Series(np.random.default_rng(5).normal(0, 0.01, 400))
    best = net.sort_values().iloc[-100:].mean()
    assert df.random_control(net, 100, best, draws=2000) > 0.99
    assert df.random_control(net, 100, net.mean(), draws=2000) < 0.7
