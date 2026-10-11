"""The last look computes the engine's strategy, plus a provisional bar.

Until 2026-10-11 it built its own configuration -- candidates instead of
the universe, no live macro dating, no six-month window -- and on
2026-10-08 read nine fresh engine longs at 0.84 as exactly flat all day.

Mutations checked:

  M2040 the check's regime settings no longer the engine's
  M2041 the check's window no longer the engine's
  M2042 the check's universe narrowed to the candidates
  M2043 a fourth, unstated difference in the signal settings
"""
from __future__ import annotations

import dataclasses

from cfdinvest.advisor import livecheck
from cfdinvest.config import load_config
from cfdinvest.schedule import app


def _engine(settings):
    class _Args:
        interval = start = end = universe = tickers = account = None

    return app.for_live_intent(app._backtest_config(_Args(), settings), settings)


def test_the_check_is_the_engine_but_for_three_stated_fields(monkeypatch):
    settings = load_config()
    monkeypatch.setattr(app, "_traded", lambda *_a, **_k: ["AAA", "BBB", "CCC"])
    engine, check = _engine(settings), livecheck.engine_check_config(settings)

    assert check.tickers == engine.tickers                    # M2042
    assert check.regime == engine.regime                      # M2040
    assert check.start == engine.start and check.interval == engine.interval  # M2041
    assert check.preprocess == engine.preprocess
    assert check.news_mode == engine.news_mode
    assert check.end is None
    allowed = {"hold_across_session_open": False, "breakout_band_lag": 2}
    assert dataclasses.replace(check.signal, **{
        k: getattr(engine.signal, k) for k in allowed
    }) == engine.signal                                       # M2043
    for name, value in allowed.items():
        assert getattr(check.signal, name) == value
