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


def test_allow_short_false_leaves_no_short_and_keeps_the_longs():
    """M1972, M1973: through real signal generation, on a market that falls
    then rises, a long-only book holds no short -- with the overlays OFF,
    which the spot arm needs -- and still takes its longs."""
    import numpy as np

    from cfdinvest.preprocessing import PreprocessConfig, build_analysis_frame
    from cfdinvest.signals import generate_signals
    from cfdinvest.schema import TICKER, TIMESTAMP

    calendar = pd.bdate_range("2019-01-02", periods=500, tz="UTC", name=TIMESTAMP)
    path = np.r_[np.full(250, -0.004), np.full(250, 0.004)]
    close = 100 * np.exp(np.cumsum(path + np.random.default_rng(3).normal(0, 0.003, 500)))
    market = pd.DataFrame({"open": close, "high": close * 1.004, "low": close * 0.996,
                           "close": close, "volume": 1e6, TICKER: "AAA"},
                          index=calendar).set_index(TICKER, append=True)
    analysis = build_analysis_frame(market, None, interval="1d",
                                    config=PreprocessConfig(correlation_window=63))
    both = dataclasses.replace(SignalConfig(), market_filters=False)
    signals_both = generate_signals(analysis, None, both)
    signals_long = generate_signals(analysis, None, dataclasses.replace(both, allow_short=False))

    assert (signals_both.direction["AAA"] == "short").any(), "the fixture never goes short"
    assert not (signals_long.direction["AAA"] == "short").any(), "a long-only book went short"
    assert (signals_long.direction["AAA"] == "long").any(), "a long-only book lost its longs"


def test_switching_them_off_permits_and_shrinks_nothing():
    """M1970."""
    outcome = _outcome(market_filters=False)
    assert outcome.allow_long.to_numpy().all() and outcome.allow_short.to_numpy().all()
    assert (outcome.long_multiplier.to_numpy() == 1.0).all()
    assert (outcome.short_multiplier.to_numpy() == 1.0).all()
