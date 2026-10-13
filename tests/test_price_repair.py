"""Capital's history is repaired where its split adjustment is wrong, and only there.

Found 2026-10-13 by a data audit: PANW at twice its price for 456 sessions,
AVGO with one bar at a tenth, NOW with one session at 0.6x, and the MMM, GE
and DHR spin-offs left unadjusted. A first version also rescaled the whole of
four high-dividend names, because the daily store's dividend adjustment
drifts -- so drift has a test of its own, and so does the rule that the
newest bars, which live quotes are compared with, never move.

Every case is synthetic with the answer known by construction: a true price
path, the daily store's close taken from it, and Capital's bars made wrong in
one stated way.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from cfdinvest.ingestion import price_repair as pr

DAYS = pd.bdate_range("2026-01-05", "2026-03-06", tz="UTC")   # EST: 14:00-20:00 UTC
HOURS = range(14, 21)


def _truth(seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    stamps = pd.DatetimeIndex([d + pd.Timedelta(hours=h) for d in DAYS for h in HOURS])
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.003, len(stamps))))
    open_ = np.r_[close[0], close[:-1]]
    return pd.DataFrame({"open": open_, "high": np.maximum(open_, close) * 1.001,
                         "low": np.minimum(open_, close) * 0.999, "close": close},
                        index=stamps)


def _tidy(frame: pd.DataFrame, name: str = "AAA") -> pd.DataFrame:
    out = frame.copy()
    out.index = pd.MultiIndex.from_arrays(
        [out.index, pd.Index([name] * len(out))], names=["timestamp", "ticker"])
    return out


def _daily(truth: pd.DataFrame, name: str = "AAA", drop_last: bool = False) -> pd.Series:
    close = truth["close"].groupby(truth.index.normalize()).last()
    if drop_last:
        close = close.iloc[:-1]
    return _tidy(close.to_frame("close"), name)["close"]


def _repaired(bars: pd.DataFrame, daily: pd.Series):
    factors, repairs = pr.repair_factors(bars, daily)
    fixed = bars.copy()
    for column in ("open", "high", "low", "close"):
        fixed[column] = fixed[column] * factors
    return fixed, repairs


def test_an_unadjusted_split_is_put_back_on_the_true_level():
    truth = _truth()
    capital = truth.copy()
    before = capital.index.normalize() < DAYS[20]
    capital.loc[before] *= 2.0                      # history left at the old price
    fixed, repairs = _repaired(_tidy(capital), _daily(truth))
    assert np.allclose(fixed["close"].to_numpy(), truth["close"].to_numpy())
    assert [r.kind for r in repairs] == ["level"]
    assert repairs[0].factor == pytest.approx(0.5)


def test_one_bad_session_moves_that_session_and_nothing_else():
    truth = _truth()
    capital = truth.copy()
    bad = capital.index.normalize() == DAYS[25]
    capital.loc[bad] *= 0.6
    fixed, repairs = _repaired(_tidy(capital), _daily(truth))
    assert np.allclose(fixed["close"].to_numpy(), truth["close"].to_numpy())
    assert [(r.kind, r.start) for r in repairs] == [("level", DAYS[25])]


def test_a_single_broken_bar_is_rescaled_onto_its_neighbours():
    truth = _truth()
    capital = truth.copy()
    capital.iloc[100] *= 0.1
    fixed, repairs = _repaired(_tidy(capital), _daily(truth))
    assert [r.kind for r in repairs] == ["bar"]
    assert fixed["close"].iloc[100] == pytest.approx(truth["close"].iloc[100], rel=0.01)
    others = np.r_[0:100, 101:len(truth)]
    assert np.allclose(fixed["close"].to_numpy()[others], truth["close"].to_numpy()[others])


def test_dividend_drift_in_the_daily_store_is_not_a_fault():
    """The first version's error: a slow drift rescaled the whole history."""
    truth = _truth()
    daily = _daily(truth)
    daily = daily * np.linspace(0.88, 1.0, len(daily))   # adjusted further back
    factors, repairs = pr.repair_factors(_tidy(truth), daily)
    assert repairs == []
    assert (factors == 1.0).all()


def test_a_move_both_feeds_agree_on_is_left_alone():
    truth = _truth()
    truth.loc[truth.index.normalize() >= DAYS[30]] *= 2.2   # a real re-rating
    factors, repairs = pr.repair_factors(_tidy(truth), _daily(truth))
    assert repairs == [] and (factors == 1.0).all()


def test_the_newest_bars_are_never_rescaled():
    """Live quotes are compared with them; moving them fakes a jump."""
    truth = _truth()
    capital = truth.copy()
    capital.loc[capital.index.normalize() < DAYS[-3]] *= 3.0
    factors, _ = pr.repair_factors(_tidy(capital), _daily(truth))
    newest = capital.index.normalize() >= DAYS[-3]
    assert (factors[newest] == 1.0).all()


def test_a_jump_on_a_day_without_a_reference_is_reported_not_repaired():
    truth = _truth()
    capital = truth.copy()
    capital.loc[capital.index.normalize() == DAYS[-1]] *= 2.0   # split today
    factors, repairs = pr.repair_factors(_tidy(capital), _daily(truth, drop_last=True))
    assert (factors == 1.0).all()
    assert [r.kind for r in repairs] == ["unverified"]
    assert repairs[0].start.normalize() == DAYS[-1]


def test_a_day_missing_its_reference_at_a_split_takes_the_later_level():
    """A holiday in one calendar and not the other, on the split's first day.

    Capital already shows the new price on that day, so it belongs with the
    days after the split, not before.
    """
    truth = _truth()
    capital = truth.copy()
    capital.loc[capital.index.normalize() < DAYS[20]] *= 2.0
    daily = _daily(truth)
    daily = daily[daily.index.get_level_values(0) != DAYS[20]]
    fixed, _ = _repaired(_tidy(capital), daily)
    assert np.allclose(fixed["close"].to_numpy(), truth["close"].to_numpy())


def test_no_daily_reference_repairs_nothing():
    truth = _truth()
    capital = truth.copy()
    capital.loc[capital.index.normalize() < DAYS[20]] *= 2.0
    factors, _ = pr.repair_factors(_tidy(capital), None)
    assert (factors == 1.0).all()


def test_the_lake_hands_out_repaired_bars_and_says_so(tmp_path):
    """Through ``DataRepository.load_market``, which every caller uses."""
    from cfdinvest import storage
    from cfdinvest.config import AppConfig, MarketDataConfig, PathConfig
    from cfdinvest.repository import DataRepository

    config = AppConfig(paths=PathConfig(data_root=tmp_path),
                       market=MarketDataConfig(interval="capital_1h"))
    truth = _truth()
    capital = truth.copy()
    capital.loc[capital.index.normalize() < DAYS[20]] *= 2.0
    for interval, frame in (("capital_1h", capital),
                            ("1d", truth["close"].groupby(truth.index.normalize()).last()
                             .to_frame("close").assign(open=np.nan, high=np.nan,
                                                       low=np.nan, volume=0.0))):
        out = frame.copy()
        out["volume"] = 0.0
        out["ticker"] = "AAA"
        out.index.name = "timestamp"
        path = storage.market_path(config.paths, "AAA", interval)
        path.parent.mkdir(parents=True, exist_ok=True)
        out.to_parquet(path)

    repo = DataRepository(config)
    bars = repo.load_market(["AAA"], interval="capital_1h")
    assert np.allclose(bars["close"].to_numpy(), truth["close"].to_numpy())
    assert [r.kind for r in repo.last_price_repairs] == ["level"]
    opens = repo.load_market(["AAA"], interval="capital_1h", columns=["open"])
    assert np.allclose(opens["open"].to_numpy(), truth["open"].to_numpy())
    raw = repo.load_market(["AAA"], interval="capital_1h", repair=False)
    assert np.allclose(raw["close"].to_numpy(), capital["close"].to_numpy())


def test_monitoring_warns_on_an_unverified_jump_and_names_an_unchecked_lake():
    from cfdinvest.monitoring import core

    now = pd.Timestamp("2026-03-06 20:00", tz="UTC")
    jump = pr.Repair("AAA", "unverified", now - pd.Timedelta(hours=3),
                     now - pd.Timedelta(hours=3), 1.0)
    old = pr.Repair("BBB", "level", pd.Timestamp("2025-01-02", tz="UTC"),
                    pd.Timestamp("2025-06-02", tz="UTC"), 0.5)
    issues = core.check_price_repairs([jump, old], now=now)
    assert [(i.severity, "AAA" in i.message) for i in issues] == [("warning", True)]
    assert core.check_price_repairs([old], now=now) == []
    unchecked = core.check_price_repairs(None, now=now)
    assert unchecked and unchecked[0].severity != "info"
