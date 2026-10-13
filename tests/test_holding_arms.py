"""The holding arms are what they are named for.

An arm whose settings the engine ignored would be the baseline under
another label -- which happened on 2026-09-26, when a misspelled override
returned the shipped book and read as "the rule does nothing".
"""
import numpy as np
import pandas as pd

from cfdinvest.config import load_config
from cfdinvest.research import harness, holding


def _config(name):
    return harness.research_backtest(load_config(), tickers=["AAA"], **holding.ARMS[name]).execution


def test_the_baseline_flattens_and_the_carry_arms_do_not():
    assert _config("A").overnight_profit_take is not None
    for name in ("B", "B3", "B5"):
        assert _config(name).overnight_profit_take is None, name


def test_the_primary_has_no_time_stop_and_the_secondaries_have_theirs():
    assert _config("B").time_stop_days is None
    assert _config("B3").time_stop_days == 3.0
    assert _config("B5").time_stop_days == 5.0
    assert _config("A").time_stop_days == load_config().strategy.time_stop_days


def test_the_comparison_is_paired_by_session():
    days = pd.bdate_range("2026-01-05", periods=6, tz="UTC")
    base = pd.DataFrame({"net": [0.01, -0.01, 0.0, 0.02, 0.0, 0.0],
                         "gross": 0.0, "cost": 0.0, "financing": 0.0}, index=days)
    arm = base.iloc[1:].copy()
    arm["net"] += 0.005
    out = holding.compare(arm, base)
    assert out["days"] == 5
    assert np.isclose(out["vs_base"], 0.005)
