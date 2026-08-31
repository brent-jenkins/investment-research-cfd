

# --------------------------------------------------------------------------- #
# M-live: the decision feed stopped advancing and nothing said so
# --------------------------------------------------------------------------- #
# _refresh routed on backtest_config.interval, which is None whenever
# nothing overrode it -- always, in the daemon. None means "whatever the
# config says", not "daily", so is_capital_interval(None) was False and every
# intraday top-up went down the yfinance branch. MarketDataIngestor then
# resolved the SAME None against the config, got "capital_1h", and asked
# yfinance for an interval it has never heard of.
#
# The vendor rejected all hundred names on every pass. On 2026-08-31 the book
# opened positions at 15:06 UTC on bars that ended the previous Friday at
# 19:00 -- three days stale, with the market open.
#
# Functional: these assert which vendor is reached, not which branch is
# written. A structural test would have passed through this, because the
# branch was correct and the value it read was not.


def _routed_vendor(monkeypatch, config_interval, override=None):
    """Which ingester a refresh reaches, for a given config and override."""
    import dataclasses

    from cfdinvest.backtest.runner import BacktestConfig
    from cfdinvest.ingestion import capital_prices
    from cfdinvest.schedule import runner as runner_module

    reached: list[str] = []

    monkeypatch.setattr(
        capital_prices, "backfill",
        lambda *a, **k: reached.append("capital") or None,
    )

    class _Ingestor:
        def __init__(self, config):
            pass

        def ingest(self, tickers=None, interval=None):
            reached.append(f"yfinance:{interval}")

    monkeypatch.setattr(runner_module, "_refresh_events", lambda *a, **k: None)

    import cfdinvest.ingestion as ingestion_pkg
    monkeypatch.setattr(ingestion_pkg, "MarketDataIngestor", _Ingestor)

    class _Capital:
        complete = True

        @classmethod
        def from_env(cls):
            return cls()

    from cfdinvest.broker.capital import client as capital_client
    monkeypatch.setattr(capital_client, "CapitalConfig", _Capital)

    class _Macro:
        api_key = ""

    class _Config:
        macro = _Macro()

        class market:
            interval = config_interval

        class paths:
            data_root = "."

    class _Repo:
        config = _Config()

    runner_module._refresh(
        _Repo(), ["AAPL"], BacktestConfig(tickers=["AAPL"], interval=override)
    )
    return reached


def test_the_decision_feed_is_fetched_from_the_broker_not_yfinance(monkeypatch):
    """The reported failure: no override, so the interval arrives as None."""
    reached = _routed_vendor(monkeypatch, "capital_1h", override=None)

    assert reached == ["capital"], (
        f"an unoverridden refresh reached {reached}; the decision feed must "
        "come from the venue that fills the orders, and yfinance has never "
        "heard of 'capital_1h'"
    )


def test_an_explicit_capital_override_still_reaches_the_broker(monkeypatch):
    """The case that always worked, so the fix is not a swap."""
    assert _routed_vendor(monkeypatch, "1d", override="capital_1h") == ["capital"]


def test_a_yfinance_interval_still_reaches_yfinance(monkeypatch):
    """The other half. Daily bars, volume and the screening series are still
    yfinance's, and routing everything to the broker would be the same bug
    facing the other way."""
    assert _routed_vendor(monkeypatch, "1d", override=None) == ["yfinance:1d"]
    assert _routed_vendor(monkeypatch, "capital_1h", override="1d") == ["yfinance:1d"]


def test_the_resolved_interval_is_what_reaches_the_vendor(monkeypatch):
    """The precise defect: routing read one value and fetching read another.

    Whatever interval the router decides on must be the interval the
    ingester is handed. Passing None through let the two disagree, which is
    how a branch that was written correctly still reached the wrong vendor.
    """
    reached = _routed_vendor(monkeypatch, "1d", override=None)

    assert reached == ["yfinance:1d"], (
        f"the ingester was handed {reached}, not the resolved interval the "
        "router decided on"
    )


# --------------------------------------------------------------------------- #
# M-live: the broker was asked for currency pairs it does not quote
# --------------------------------------------------------------------------- #
# The pairs ride along in the ticker list because the engine needs them to
# price a dollar instrument in sterling. They were being asked of Capital on
# every pass, which recorded five permanent "failed" entries in the lake
# manifest -- a critical banner that could never clear -- and left nobody
# fetching them at all once the decision feed moved off yfinance. Their 1h
# bars went two days stale and were getting staler, which is how every USD
# instrument ends up converting at 1.0.


def _split_refresh(monkeypatch, tickers, config_interval="capital_1h"):
    """Which tickers reach which vendor, for one refresh."""
    from cfdinvest.backtest.runner import BacktestConfig
    from cfdinvest.ingestion import capital_prices
    from cfdinvest.schedule import runner as runner_module

    seen = {"capital": None, "yfinance": None, "yfinance_interval": None}

    def _backfill(names, *a, **k):
        seen["capital"] = list(names or ())

    monkeypatch.setattr(capital_prices, "backfill", _backfill)
    monkeypatch.setattr(runner_module, "_refresh_events", lambda *a, **k: None)

    class _Ingestor:
        def __init__(self, config):
            pass

        def ingest(self, names=None, interval=None):
            seen["yfinance"] = list(names or ())
            seen["yfinance_interval"] = interval

    import cfdinvest.ingestion as ingestion_pkg
    monkeypatch.setattr(ingestion_pkg, "MarketDataIngestor", _Ingestor)

    class _Capital:
        complete = True

        @classmethod
        def from_env(cls):
            return cls()

    from cfdinvest.broker.capital import client as capital_client
    monkeypatch.setattr(capital_client, "CapitalConfig", _Capital)

    class _Macro:
        api_key = ""

    class _Config:
        macro = _Macro()

        class market:
            interval = config_interval

    class _Repo:
        config = _Config()

    runner_module._refresh(
        _Repo(), tickers, BacktestConfig(tickers=tickers, interval=None)
    )
    return seen


def test_the_pairs_are_fetched_from_the_broker_by_its_own_epic():
    """The pairs were the last input still coming from yfinance intraday.

    They failed against Capital only because the lake spells them
    GBPUSD=X and the broker calls the same market GBPUSD. That read as a
    vendor limitation for a day. It is a naming mismatch, and the standing
    instruction is one source of truth with no intraday data from yfinance.
    """
    from cfdinvest.schedule.app import fx_support_tickers, reference_epics

    epics = reference_epics()

    assert set(epics) == set(fx_support_tickers()), (
        "a pair exists that has no epic, so the broker would be asked for a "
        "name it does not know and the fetch would silently return nothing"
    )
    assert epics["GBPUSD=X"] == "GBPUSD"
    for lake, epic in epics.items():
        assert "=X" in lake and "=X" not in epic
        assert epic == lake.replace("=X", "")


def test_the_refresh_hands_the_broker_the_epic_map(monkeypatch):
    """Functional: the map must actually reach the fetch.

    A correct mapping that nothing passes down is the shape of the bug this
    replaced -- the routing was right and the value it carried was not.
    """
    from cfdinvest.backtest.runner import BacktestConfig
    from cfdinvest.ingestion import capital_prices
    from cfdinvest.schedule import runner as runner_module

    seen = {}

    def _backfill(names, *a, **k):
        seen["names"] = list(names or ())
        seen["epics"] = k.get("epics")

    monkeypatch.setattr(capital_prices, "backfill", _backfill)
    monkeypatch.setattr(runner_module, "_refresh_events", lambda *a, **k: None)

    class _Capital:
        complete = True

        @classmethod
        def from_env(cls):
            return cls()

    from cfdinvest.broker.capital import client as capital_client
    monkeypatch.setattr(capital_client, "CapitalConfig", _Capital)

    class _Macro:
        api_key = ""

    class _Config:
        macro = _Macro()

        class market:
            interval = "capital_1h"

    class _Repo:
        config = _Config()

    runner_module._refresh(
        _Repo(), ["AAPL", "GBPUSD=X"],
        BacktestConfig(tickers=["AAPL", "GBPUSD=X"], interval=None),
    )

    assert seen["names"] == ["AAPL", "GBPUSD=X"], (
        "the pairs were dropped from the fetch, so nothing converts a dollar "
        "price into sterling"
    )
    assert (seen["epics"] or {}).get("GBPUSD=X") == "GBPUSD", (
        "the broker was handed the lake's spelling, which it does not know"
    )


def test_the_pair_list_has_one_owner(monkeypatch):
    """M: a second hard-coded list of currency pairs drifts from the first.

    Read from the conversion map that already names them, so a pair added
    there is covered without anybody remembering this file.
    """
    from cfdinvest.schedule.app import fx_support_tickers
    from cfdinvest.schedule.runner import _reference_pairs

    assert set(_reference_pairs(None)) == set(fx_support_tickers())
    assert _reference_pairs(None), "the pair list came back empty"
