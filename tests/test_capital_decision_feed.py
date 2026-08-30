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

import dataclasses

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


# --------------------------------------------------------------------------- #
# Currency pairs are vendor symbols, and the broker does not quote them
# --------------------------------------------------------------------------- #
def test_reference_series_fall_back_to_a_vendor_interval():
    """The third separate way these pairs have failed to reach the engine.

    They were named in the rate map but not fetched, then fetched but not
    loaded, and then -- when the decision feed moved to capital_1h -- looked
    for at an interval where they cannot exist, because Capital quotes
    shares and not GBPUSD=X. Every USD instrument silently converted at a
    rate of 1.0, which for a GBP account holding US shares drops the
    currency move rather than relabelling it: measured, 27.0% CAGR at -34.6%
    became 23.7% at -45.7% once rates applied.
    """
    from cfdinvest.backtest import runner

    assert not capital_prices.is_capital_interval(
        runner.REFERENCE_FALLBACK_INTERVAL
    ), "the fallback points at an interval the broker does not serve either"

    asked = []

    class Repo:
        config = type("C", (), {"market": type("M", (), {"interval": "capital_1h"})()})()

        def load_market(self, tickers=None, interval=None, **kwargs):
            asked.append(interval)
            if capital_prices.is_capital_interval(interval):
                return pd.DataFrame()          # the broker has no FX bars
            index = pd.date_range(
                "2026-01-05 14:00", periods=4, freq="1h", tz="UTC"
            )
            return pd.DataFrame(
                {"timestamp": index, "ticker": "GBPUSD=X", "close": 1.35},
            ).set_index(["timestamp", "ticker"])

    index = pd.date_range("2026-01-05 14:00", periods=4, freq="1h", tz="UTC")
    close = pd.DataFrame({"AMD": 100.0}, index=index)

    # A dataclass, because attach_reference_prices rebuilds the frame with
    # dataclasses.replace -- it never mutates the one it was handed.
    @dataclasses.dataclass
    class Frame:
        prices: dict

    out = runner.attach_reference_prices(
        Frame({"close": close}), Repo(), ["GBPUSD=X"], interval="capital_1h"
    )
    assert "capital_1h" in asked, "the decision feed was never tried"
    assert runner.REFERENCE_FALLBACK_INTERVAL in asked, (
        "no fallback was attempted, so every rate silently stays at its "
        "configured value"
    )
    assert "GBPUSD=X" in out.prices["close"].columns, (
        "the pair was fetched but never reached the close panel, which is "
        "where the execution layer reads its rates"
    )
    assert float(out.prices["close"]["GBPUSD=X"].iloc[-1]) == pytest.approx(1.35)


def test_no_fallback_is_attempted_for_a_vendor_feed():
    """A yfinance run must not gain a second lookup it never had: the pairs
    are already there, and a spurious retry would mask a genuine absence."""
    from cfdinvest.backtest import runner

    asked = []

    class Repo:
        config = type("C", (), {"market": type("M", (), {"interval": "1h"})()})()

        def load_market(self, tickers=None, interval=None, **kwargs):
            asked.append(interval)
            return pd.DataFrame()

    index = pd.date_range("2026-01-05 14:00", periods=3, freq="1h", tz="UTC")

    @dataclasses.dataclass
    class Frame:
        prices: dict

    runner.attach_reference_prices(
        Frame({"close": pd.DataFrame({"AMD": 100.0}, index=index)}),
        Repo(), ["GBPUSD=X"], interval="1h",
    )
    assert asked == ["1h"], f"expected exactly one lookup, got {asked}"


# --------------------------------------------------------------------------- #
# Once a series is being collected, it is never dropped
# --------------------------------------------------------------------------- #
def test_ingestion_covers_every_candidate_not_only_the_traded_list(tmp_path):
    """A candidate the screen picks tomorrow needs history today.

    Without this its first intraday tick fetches years of bars inline on a
    fifteen-minute job -- and, less obviously, seeding that history flips
    ``universe.screen._spread_series`` from the live observation store to the
    capital_1h archive, because it prefers the archive *if it exists at all*.
    An archive kept for only the traded names would then freeze for everyone
    else, which is the mirror image of the failure that function's docstring
    records.
    """
    from cfdinvest.schedule import app

    class Cfg:
        universe = type("U", (), {"candidates": ("AAA", "BBB", "CCC")})()
        market = type("M", (), {"interval": "capital_1h"})()
        paths = type("P", (), {"data_root": str(tmp_path)})()

    covered = app._screen_candidates(Cfg())
    assert set(covered) >= {"AAA", "BBB", "CCC"}


def test_a_name_dropped_from_the_candidates_keeps_being_collected(tmp_path):
    """Owner's rule: once a series is being collected it is not stopped.

    A hole cannot be backfilled once the vendor's retention window passes,
    so a configuration edit must not be able to create one. The set is the
    configured candidates *union what is already on disk*, which makes it
    monotonic.
    """
    from cfdinvest.schedule import app

    folder = tmp_path / "raw" / "market" / "capital_1h"
    folder.mkdir(parents=True)
    for name in ("AAA", "DROPPED", "GBPUSD=X"):
        (folder / f"{name}.parquet").write_bytes(b"")

    class Cfg:
        universe = type("U", (), {"candidates": ("AAA", "BBB")})()
        market = type("M", (), {"interval": "capital_1h"})()
        paths = type("P", (), {"data_root": str(tmp_path)})()

    covered = app._screen_candidates(Cfg())
    assert "DROPPED" in covered, (
        "a name removed from the candidate list stopped being collected; its "
        "history will acquire a hole that cannot be backfilled later"
    )
    assert "BBB" in covered, "a newly configured candidate was not picked up"
    assert "GBPUSD=X" not in covered, (
        "a reference currency pair was added to the instrument fetch"
    )
    assert len(covered) == len(set(covered)), "an instrument is fetched twice"


def test_an_unreadable_lake_falls_back_to_the_configured_candidates(tmp_path):
    """Absence named: no directory means the configured list, which is the
    old behaviour, rather than an empty fetch."""
    from cfdinvest.schedule import app

    class Cfg:
        universe = type("U", (), {"candidates": ("AAA", "BBB")})()
        market = type("M", (), {"interval": "capital_1h"})()
        paths = type("P", (), {"data_root": str(tmp_path / "nothing-here")})()

    assert app._screen_candidates(Cfg()) == ["AAA", "BBB"]


def test_a_delisted_name_is_the_one_reason_to_stop_collecting(tmp_path):
    """The exception to "never stop": a series that has genuinely ended.

    Not the same as leaving the traded list -- there is simply nothing left
    to fetch. Recorded rather than inferred, because "no new bars today"
    looks identical for a holiday, an outage and a delisting, and guessing
    would stop collecting a live instrument.
    """
    import json

    from cfdinvest.schedule import app

    folder = tmp_path / "raw" / "market" / "capital_1h"
    folder.mkdir(parents=True)
    for name in ("ALIVE", "GONE"):
        (folder / f"{name}.parquet").write_bytes(b"")
    (tmp_path / "metadata").mkdir()
    (tmp_path / "metadata" / "delisted.json").write_text(
        json.dumps({"GONE": {"reason": "delisted", "last_bar": "2026-06-30"}}),
        encoding="utf-8",
    )

    class Cfg:
        universe = type("U", (), {"candidates": ()})()
        market = type("M", (), {"interval": "capital_1h"})()
        paths = type("P", (), {"data_root": str(tmp_path)})()

    covered = app._screen_candidates(Cfg())
    assert "ALIVE" in covered
    assert "GONE" not in covered, "a dead series is still being requested"


def test_an_unreadable_delisted_list_collects_everything(tmp_path):
    """Errs towards collecting too much: a missed bar cannot be recovered
    later, a wasted request can."""
    from cfdinvest.schedule import app

    folder = tmp_path / "raw" / "market" / "capital_1h"
    folder.mkdir(parents=True)
    (folder / "ALIVE.parquet").write_bytes(b"")
    (tmp_path / "metadata").mkdir()
    (tmp_path / "metadata" / "delisted.json").write_text("{ not json", encoding="utf-8")

    class Cfg:
        universe = type("U", (), {"candidates": ()})()
        market = type("M", (), {"interval": "capital_1h"})()
        paths = type("P", (), {"data_root": str(tmp_path)})()

    assert app._screen_candidates(Cfg()) == ["ALIVE"]
