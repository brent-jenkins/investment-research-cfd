"""One source of truth for intraday, and it is the venue that fills.

Until 2026-08-30 the book decided on yfinance hourly bars and placed every
order at Capital's prices. That is two feeds answering the same question,
and only one of them is the one a fill happens against.

Two changes are tested here, and they are related: the decision feed moved
to Capital, and cost is now charged as the broker quoted it. The second is
only possible because of the first -- yfinance serves no spread at all.

Why the cost change matters more than it looks. Measured over Capital's
full history on the eight-name book, the quoted spread runs a median of
11.32bp against the 5.0bp constant the model charged, 2.26x, with only TSLA
cheaper than the assumption. Because the median position lives about four
hours, spread is nearly the whole cost and financing barely registers -- so
that gap is an error in the cost model rather than in a corner of it. It is
worth 37.7pp of CAGR on the eight-name book.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from cfdinvest.ingestion import capital_prices


# --------------------------------------------------------------------------- #
# The interval label and the resolution behind it
# --------------------------------------------------------------------------- #
def test_a_capital_interval_names_its_own_resolution():
    """``capital_1h`` is stored under its own name so it never collides with
    yfinance's ``1h`` in the lake, but the API is asked for ``1h``."""
    assert capital_prices.resolution_for("capital_1h") == "1h"
    assert capital_prices.resolution_for("capital_1m") == "1m"
    assert capital_prices.resolution_for("capital_5m") == "5m"


def test_an_unknown_interval_is_refused_rather_than_guessed():
    """Guessing would fill the right directory with bars of the wrong size,
    which surfaces as a strategy behaving oddly rather than as a failure."""
    with pytest.raises(ValueError):
        capital_prices.resolution_for("1h")          # yfinance, not Capital
    with pytest.raises(ValueError):
        capital_prices.resolution_for("capital_3y")  # no such resolution
    with pytest.raises(ValueError):
        capital_prices.resolution_for(None)


# --------------------------------------------------------------------------- #
# The refresh routes to the venue, not the vendor
# --------------------------------------------------------------------------- #
def test_capital_bars_are_refreshed_from_capital_not_yfinance(monkeypatch):
    """``MarketDataIngestor`` is yfinance by definition. Handing it
    ``capital_1h`` would send a label it cannot parse to a vendor that never
    had the data."""
    from cfdinvest.schedule import runner

    called = {}

    class Repo:
        config = type("C", (), {"macro": type("M", (), {"api_key": None})()})()

    def fake_backfill(tickers, config=None, app_config=None, **kwargs):
        called["backfill"] = {"tickers": list(tickers or ()), **kwargs}
        return []

    def fake_ingestor(*args, **kwargs):
        called["yfinance"] = True
        raise AssertionError("yfinance was asked for Capital bars")

    monkeypatch.setattr(capital_prices, "backfill", fake_backfill)
    monkeypatch.setattr(
        "cfdinvest.ingestion.MarketDataIngestor", fake_ingestor, raising=False
    )
    monkeypatch.setattr(runner, "_refresh_events", lambda *a, **k: None)
    monkeypatch.setattr(
        "cfdinvest.broker.capital.client.CapitalConfig.from_env",
        staticmethod(lambda: type("S", (), {"complete": True})()),
    )

    config = type("B", (), {"interval": "capital_1h", "series_ids": ()})()
    runner._refresh(Repo(), ["AMD", "CAT"], config)

    assert "yfinance" not in called
    assert called["backfill"]["interval"] == "capital_1h"
    assert called["backfill"]["resolution"] == "1h"


def test_a_missing_credential_fails_the_refresh_rather_than_running_stale(
    monkeypatch,
):
    """This is the trading path's own data. Continuing would run the
    strategy on whatever bars happened to be on disk without ever saying the
    refresh did not run."""
    from cfdinvest.schedule import runner

    class Repo:
        config = type("C", (), {"macro": type("M", (), {"api_key": None})()})()

    monkeypatch.setattr(
        "cfdinvest.broker.capital.client.CapitalConfig.from_env",
        staticmethod(lambda: type("S", (), {"complete": False})()),
    )
    config = type("B", (), {"interval": "capital_1h", "series_ids": ()})()

    with pytest.raises(RuntimeError, match="credentials"):
        runner._refresh(Repo(), ["AMD"], config)


def test_daily_bars_still_come_from_yfinance(monkeypatch):
    """yfinance is unchanged as the source for daily bars, real share volume
    and the screening series. It is a good picture of the universe; it is
    simply not what we deal against."""
    from cfdinvest.schedule import runner

    seen = {}

    class Repo:
        config = type("C", (), {"macro": type("M", (), {"api_key": None})()})()

    class FakeIngestor:
        def __init__(self, config):
            pass

        def ingest(self, tickers, interval=None):
            seen["interval"] = interval

    monkeypatch.setattr(
        "cfdinvest.ingestion.MarketDataIngestor", FakeIngestor, raising=False
    )
    monkeypatch.setattr(runner, "_refresh_events", lambda *a, **k: None)
    monkeypatch.setattr(
        capital_prices, "backfill",
        lambda *a, **k: pytest.fail("Capital was asked for daily bars"),
    )

    config = type("B", (), {"interval": "1d", "series_ids": ()})()
    runner._refresh(Repo(), ["AMD"], config)
    assert seen["interval"] == "1d"


# --------------------------------------------------------------------------- #
# Cost charged as quoted
# --------------------------------------------------------------------------- #
def _frame_with_spread(spread_bp):
    """A two-bar, one-asset analysis frame carrying a measured spread."""
    index = pd.date_range("2026-01-05 14:00", periods=4, freq="1h", tz="UTC")
    columns = pd.Index(["AMD"])
    price = pd.DataFrame(100.0, index=index, columns=columns)
    panel = pd.DataFrame(float(spread_bp), index=index, columns=columns)
    return price, panel


def test_the_measured_spread_is_charged_when_it_is_switched_on():
    """The panel sits beside the prices on broker data and was simply not
    being read. A bar with a 40bp quoted spread must not be charged 5."""
    from cfdinvest.execution import fills

    index = pd.date_range("2026-01-05 14:00", periods=3, freq="1h", tz="UTC")
    columns = pd.Index(["AMD"])
    constant = np.array([5.0 / 10_000.0])
    panel = pd.DataFrame(40.0, index=index, columns=columns)

    matrix, measured, total = fills.resolve_spread_matrix(
        index, columns, constant, panel
    )
    assert measured == total == 3
    assert np.allclose(matrix, 40.0 / 10_000.0), (
        "the quoted spread was not charged; the constant was used instead"
    )

    # And with no panel the constant still stands, so a feed without a
    # spread column keeps behaving exactly as it did.
    matrix, measured, total = fills.resolve_spread_matrix(
        index, columns, constant, None
    )
    assert measured == 0
    assert np.allclose(matrix, 5.0 / 10_000.0)


def test_a_bad_spread_row_falls_back_rather_than_trading_free():
    """A zero or negative quote is a bad row, not a cheaper market. Charging
    it would make dealing free at exactly the bars where the data is worst."""
    from cfdinvest.execution import fills

    index = pd.date_range("2026-01-05 14:00", periods=3, freq="1h", tz="UTC")
    columns = pd.Index(["AMD"])
    constant = np.array([5.0 / 10_000.0])
    panel = pd.DataFrame([[0.0], [-3.0], [20.0]], index=index, columns=columns)

    matrix, measured, _ = fills.resolve_spread_matrix(
        index, columns, constant, panel
    )
    assert measured == 1, "a bad row was counted as a measurement"
    assert matrix[0, 0] == pytest.approx(5.0 / 10_000.0)
    assert matrix[1, 0] == pytest.approx(5.0 / 10_000.0)
    assert matrix[2, 0] == pytest.approx(20.0 / 10_000.0)


def test_measured_spreads_are_off_unless_asked_for():
    """Turning this on changes what every backtest charges, so it is a
    named switch rather than a silent improvement -- a recorded figure stays
    reproducible, and a run that charges measured spreads says so."""
    from cfdinvest.execution.frame import ExecutionConfig

    assert ExecutionConfig().use_measured_spreads is False


def test_production_asks_for_measured_spreads():
    """The default above is library safety. The deployed configuration is
    the place that decides, and it charges what Capital quoted."""
    from cfdinvest.config import load_config

    assert load_config().strategy.use_measured_spreads is True


def test_the_decision_feed_is_the_venue_that_fills():
    """A decision taken on one feed and executed on another is two sources
    of truth for the same question."""
    from cfdinvest.config import load_config

    interval = load_config().market.interval
    assert capital_prices.is_capital_interval(interval), (
        f"intraday decisions are being taken on {interval!r}, which is not "
        "the feed the orders fill against"
    )
