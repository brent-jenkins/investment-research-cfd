"""``market_filters = False`` lifts the regime, correlation and macro overlays.

Added 2026-10-01 for the trend test: on daily bars those overlays vetoed
index longs for most of a twenty-year bull market, and a rule that cannot
be switched off cannot be measured without it. The default must keep them.

Mutations checked:

  M1970 the switch ignored, so the overlays still veto
  M1971 the switch inverted, so the DEFAULT drops the overlays
"""
from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pandas as pd

from cfdinvest.signals import frame as signal_frame
from cfdinvest.signals.frame import SignalConfig

INDEX = pd.date_range("2026-01-01", periods=5, freq="D", tz="UTC")
COLUMNS = pd.Index(["AAA"])


def _hostile_regime():
    """Every overlay reads against an equity long."""
    label = lambda value: pd.Series(value, index=INDEX)
    return SimpleNamespace(
        index=INDEX,
        combined_regime=label("crisis"),
        volatility_regime=label("high"),
        macro_regime=label("contraction"),
        correlation_regime=label("breakdown"),
        components={"growth_regime": label("contraction"),
                    "inflation_regime": label("rising"),
                    "labor_regime": label("loosening")},
    )


def _outcome(**settings):
    config = dataclasses.replace(SignalConfig(), asset_classes={"AAA": "equity"}, **settings)
    analysis = SimpleNamespace(correlations={}, prices={})
    return signal_frame._build_filters(analysis, _hostile_regime(), config, INDEX, COLUMNS)


def test_the_overlays_veto_by_default():
    """M1971: the default is the recorded behaviour."""
    outcome = _outcome()
    vetoed = ~outcome.allow_long.to_numpy().all() or (outcome.long_multiplier.to_numpy() < 1).any()
    assert vetoed, "the default no longer applies the overlays"


def test_switching_them_off_permits_and_shrinks_nothing():
    """M1970."""
    outcome = _outcome(market_filters=False)
    assert outcome.allow_long.to_numpy().all() and outcome.allow_short.to_numpy().all()
    assert (outcome.long_multiplier.to_numpy() == 1.0).all()
    assert (outcome.short_multiplier.to_numpy() == 1.0).all()
