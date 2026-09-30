"""News-driven trades: fresh company news sets the side, through the engine.

Switched on by the owner on 2026-09-30, unmeasured, as a live trial. What
must hold is that it is the engine doing it -- the run the dealer reads, the
last look, and any backtest all apply one rule -- and that the rule sees
only what was knowable when the engine decided.

Mutations checked:

  M1910 a story counted from the bar's START rather than its END
  M1911 the 24-hour window before 10:00 New York not applied
  M1912 commentary, price moves, neutral or other-company stories counted
  M1913 the breakout gate still blocking a news entry (the exemption removed)
  M1914 the exemption leaking past the breakout mask to the screen
  M1915 research runs changed by the rule's existence
  M1916 the live run not given the events when news trading is on
  M1917 the veto not refusing a trend open the news contradicts
  M1918 the veto opening a trade of its own
  M1919 an unknown news mode run as one of the others
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from cfdinvest.backtest.runner import BacktestConfig, run_from_analysis
from cfdinvest.execution.frame import ExecutionConfig
from cfdinvest.preprocessing.frame import AnalysisFrame
from cfdinvest.signals import news

DAY = pd.Timestamp("2026-09-02", tz="UTC")  # a Wednesday; 10:00 New York = 14:00 UTC


def _index(start="2026-08-03", days=40):
    stamps = []
    for d in range(days):
        base = pd.Timestamp(start, tz="UTC") + pd.Timedelta(days=d)
        if base.dayofweek < 5:
            stamps += [base + pd.Timedelta(hours=13 + n) for n in range(7)]
    return pd.DatetimeIndex(stamps)


def _falling():
    """A steady decline: the trend engine is short, and no long can break out."""
    index = _index()
    rng = np.random.default_rng(1)
    close = pd.DataFrame(
        {"AAA": 100 * np.exp(np.cumsum(-0.002 + rng.normal(0, 0.0005, len(index))))},
        index=index,
    )
    return AnalysisFrame(
        prices={"close": close, "open": close, "high": close * 1.001, "low": close * 0.999},
        returns={"simple": close.pct_change(), "log": np.log(close).diff()},
    )


def _trendless():
    """A price that alternates about 100: the trend engine is flat, and no
    breakout ever clears -- so the gate refuses every NEW entry."""
    index = _index()
    close = pd.DataFrame(
        {"AAA": 100 * (1 + 0.001 * np.where(np.arange(len(index)) % 2, 1, -1))}, index=index
    )
    return AnalysisFrame(
        prices={"close": close, "open": close, "high": close * 1.0005, "low": close * 0.9995},
        returns={"simple": close.pct_change(), "log": np.log(close).diff()},
    )


def _event(first_seen, *, impact="good", event="guidance_up", about="company", ticker="AAA"):
    return {"key": "|".join((str(first_seen), ticker, impact, event, about)),
            "prompt_version": "v", "ticker": ticker,
            "about": about, "event": event, "impact": impact,
            "first_seen": pd.Timestamp(first_seen).isoformat()}


def _run(events, **execution):
    config = BacktestConfig(
        use_regimes=False,
        execution=ExecutionConfig(breakout_lookback=12, **execution),
        news_events=None if events is None else news.events_from_labels(events, prompt_version="v"),
    )
    return run_from_analysis(_falling(), config).execution.positions["AAA"].fillna(0.0)


def test_good_news_turns_a_falling_name_long_for_the_session():
    """M1913: the breakout gate would refuse this long -- the price never
    clears a high -- so only the news exemption lets it open."""
    base = _run(None)
    held = _run([_event(DAY + pd.Timedelta(hours=12))])

    assert (base.loc[DAY + pd.Timedelta(hours=15):DAY + pd.Timedelta(hours=19)] < 0).all(), (
        "precondition: the trend engine should be short here"
    )
    session = held.loc[DAY + pd.Timedelta(hours=14):DAY + pd.Timedelta(hours=19)]
    assert (session > 0).all(), f"the news day was not held long:\n{session}"
    after = held.loc[DAY + pd.Timedelta(days=1, hours=15):DAY + pd.Timedelta(days=1, hours=19)]
    assert (after < 0).all(), "the news outlived its window and kept trading the next day"


def test_news_opens_what_the_breakout_gate_would_refuse():
    """M1913: from FLAT the gate demands a breakout, and this price never
    makes one. The first test cannot show this -- there the engine is
    already short, and the gate treats a flip as a continuing signal."""
    def held(events):
        config = BacktestConfig(
            use_regimes=False, execution=ExecutionConfig(breakout_lookback=12),
            news_events=None if events is None else news.events_from_labels(events, prompt_version="v"),
        )
        return run_from_analysis(_trendless(), config).execution.positions["AAA"].fillna(0.0)

    assert (held(None) == 0).all(), "precondition: the engine alone should never trade this"
    session = held([_event(DAY + pd.Timedelta(hours=12))]).loc[
        DAY + pd.Timedelta(hours=14):DAY + pd.Timedelta(hours=19)
    ]
    assert (session > 0).all(), f"the breakout gate refused a news entry:\n{session}"


def test_a_story_counts_from_the_end_of_the_bar_it_arrived_in():
    """M1910: seen at 15:30, it is knowable when the 15:00 bar completes,
    so the engine decides on it there and holds it from the 16:00 bar."""
    held = _run([_event(DAY + pd.Timedelta(hours=15, minutes=30))])
    assert held.loc[DAY + pd.Timedelta(hours=15)] <= 0, "traded before the story existed"
    assert held.loc[DAY + pd.Timedelta(hours=16)] > 0, "not traded once it was knowable"


def test_yesterdays_morning_news_is_not_todays():
    """M1911: first seen at 13:00 UTC the day before is outside the 24 hours
    before today's 10:00 New York (14:00 UTC)."""
    panel = news.news_sign_panel(
        news.events_from_labels([_event(DAY - pd.Timedelta(hours=11))], prompt_version="v"),
        _index(), ["AAA"],
    )
    assert panel.loc[DAY - pd.Timedelta(hours=11), "AAA"] == 1, "not even traded on its own day"
    assert (panel.loc[DAY:DAY + pd.Timedelta(hours=19), "AAA"] == 0).all()


def test_only_a_real_event_about_the_company_carries_a_side():
    """M1912."""
    seen = DAY + pd.Timedelta(hours=12)
    ignored = [
        _event(seen, event="commentary"), _event(seen, event="price_move"),
        _event(seen, event="other"), _event(seen, impact="neutral"),
        _event(seen, about="other"),
    ]
    assert news.events_from_labels(ignored, prompt_version="v").empty
    assert len(news.events_from_labels(ignored + [_event(seen)], prompt_version="v")) == 1
    assert news.events_from_labels([_event(seen)], prompt_version="other").empty, (
        "another prompt's labels were read"
    )


def test_good_and_bad_news_on_the_same_name_cancel():
    seen = DAY + pd.Timedelta(hours=12)
    panel = news.news_sign_panel(
        news.events_from_labels([_event(seen), _event(seen, impact="bad", event="legal_regulatory")],
                                prompt_version="v"),
        _index(), ["AAA"],
    )
    assert (panel.loc[DAY:DAY + pd.Timedelta(hours=19), "AAA"] == 0).all()


def test_the_exemption_relaxes_the_breakout_mask_only():
    """M1914: the screen's membership is composed AFTER the exemption, so a
    name outside the screen stays closed whatever its news."""
    held = _run(
        [_event(DAY + pd.Timedelta(hours=12))],
        entry_gate=pd.DataFrame(False, index=_index(), columns=["AAA"]),
    )
    assert (held.loc[DAY:DAY + pd.Timedelta(hours=19)] <= 0).all(), (
        "news opened a name the screen had closed"
    )


def test_research_runs_are_unchanged():
    """M1915: no events given, no difference -- to the unit."""
    assert BacktestConfig().news_events is None
    base = _run(None)
    empty = _run([])
    pd.testing.assert_series_equal(base, empty)


def test_the_live_run_carries_the_news_when_switched_on(monkeypatch):
    """M1916: `for_live_intent` is what the dealer's run is built by."""
    import dataclasses

    from cfdinvest.config import AppConfig
    from cfdinvest.persist import headline_labels
    from cfdinvest.schedule import app

    events = news.events_from_labels([_event(DAY)], prompt_version="v")
    monkeypatch.setattr(headline_labels, "news_events", lambda config=None, account=None: events)

    base = AppConfig()
    mode = lambda m: dataclasses.replace(base, strategy=dataclasses.replace(base.strategy, news_mode=m))
    backtest = BacktestConfig(end=DAY, start=DAY - pd.Timedelta(days=200))

    for wanted in ("trade", "veto"):
        live = app.for_live_intent(backtest, mode(wanted))
        assert live.news_events is events and live.news_mode == wanted
    assert app.for_live_intent(backtest, mode("off")).news_events is None
    assert app.for_live_intent(backtest, mode("vetto")).news_events is None, "a misspelt mode acted"


# --------------------------------------------------------------------------- #
# Veto mode: the news may only refuse a trend open it contradicts
# --------------------------------------------------------------------------- #
def _run_mode(analysis, events, mode):
    from cfdinvest.schedule.app import FLATTEN_AT_SESSION_END

    config = BacktestConfig(
        use_regimes=False,
        # Flattened each evening, as the account is, so the engine OPENS
        # afresh every session -- a veto acts on opens, not on holdings.
        # And the session's opening bar refused, as live and the research
        # harness refuse it: otherwise the first open fills on the previous
        # evening's signal, before the news existed.
        execution=ExecutionConfig(breakout_lookback=12, overnight_profit_take=FLATTEN_AT_SESSION_END,
                                  open_delay_minutes=1),
        news_events=None if events is None else news.events_from_labels(events, prompt_version="v"),
        news_mode=mode,
    )
    return run_from_analysis(analysis, config).execution.positions["AAA"].fillna(0.0)


def test_the_veto_refuses_a_short_the_good_news_contradicts():
    """M1917: the falling name the engine shorts every day is left alone on
    the day good news arrives, and shorted again the day after."""
    day = slice(DAY + pd.Timedelta(hours=13), DAY + pd.Timedelta(hours=19))
    next_day = slice(DAY + pd.Timedelta(days=1, hours=15), DAY + pd.Timedelta(days=1, hours=18))
    plain = _run_mode(_falling(), None, "veto")
    vetoed = _run_mode(_falling(), [_event(DAY + pd.Timedelta(hours=12))], "veto")

    assert (plain.loc[day] < 0).any(), "precondition: the engine shorts on the news day"
    assert (vetoed.loc[day] == 0).all(), f"the veto let a contradicted short open:\n{vetoed.loc[day]}"
    assert (vetoed.loc[next_day] < 0).any(), "the veto outlived its news"


def test_agreeing_news_does_not_stop_the_engine():
    day = slice(DAY + pd.Timedelta(hours=13), DAY + pd.Timedelta(hours=19))
    plain = _run_mode(_falling(), None, "veto")
    agreed = _run_mode(_falling(), [_event(DAY + pd.Timedelta(hours=12), impact="bad",
                                           event="guidance_down")], "veto")
    pd.testing.assert_series_equal(plain.loc[day], agreed.loc[day])


def test_the_veto_never_opens_a_trade():
    """M1918: on a name the engine never trades, good news in veto mode
    leaves it flat -- where trade mode would buy it."""
    events = [_event(DAY + pd.Timedelta(hours=12))]
    assert (_run_mode(_trendless(), events, "veto") == 0).all()
    assert (_run_mode(_trendless(), events, "trade") > 0).any(), "the control did not trade"


def test_an_unknown_mode_is_refused():
    """M1919."""
    import pytest

    with pytest.raises(ValueError):
        _run_mode(_falling(), [_event(DAY)], "vetto")
