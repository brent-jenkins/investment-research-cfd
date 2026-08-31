

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


def test_the_broker_is_not_asked_for_pairs_it_does_not_quote(monkeypatch):
    """The reported failure: five permanent manifest failures."""
    seen = _split_refresh(monkeypatch, ["AAPL", "MU", "GBPUSD=X", "EURGBP=X"])

    assert seen["capital"] == ["AAPL", "MU"], (
        f"the broker was asked for {seen['capital']}; it does not quote the "
        "currency pairs and never will, so every pass records a failure"
    )


def test_the_pairs_are_still_fetched_from_a_vendor_that_has_them(monkeypatch):
    """The half that matters more.

    Not asking Capital is worthless if it means nobody asks anyone. Their
    bars are what convert a dollar instrument into sterling, and stale ones
    are how the rate silently becomes 1.0.
    """
    from cfdinvest.backtest.runner import REFERENCE_FALLBACK_INTERVAL

    seen = _split_refresh(monkeypatch, ["AAPL", "GBPUSD=X", "USDJPY=X"])

    assert seen["yfinance"] == ["GBPUSD=X", "USDJPY=X"]
    assert seen["yfinance_interval"] == REFERENCE_FALLBACK_INTERVAL


def test_a_run_with_no_pairs_asks_the_second_vendor_nothing(monkeypatch):
    """No empty fetches. An ingest of nothing is a vendor call that can fail
    and a manifest entry that can mislead."""
    seen = _split_refresh(monkeypatch, ["AAPL", "MU"])

    assert seen["capital"] == ["AAPL", "MU"]
    assert seen["yfinance"] is None


def test_a_yfinance_feed_is_not_split_at_all(monkeypatch):
    """The other branch is unchanged: when yfinance is the decision feed it
    serves the pairs too, and splitting would be two calls for one job."""
    seen = _split_refresh(
        monkeypatch, ["AAPL", "GBPUSD=X"], config_interval="1d"
    )

    assert seen["capital"] is None
    assert seen["yfinance"] == ["AAPL", "GBPUSD=X"]
    assert seen["yfinance_interval"] == "1d"


def test_the_pair_list_has_one_owner(monkeypatch):
    """M: a second hard-coded list of currency pairs drifts from the first.

    Read from the conversion map that already names them, so a pair added
    there is covered without anybody remembering this file.
    """
    from cfdinvest.schedule.app import fx_support_tickers
    from cfdinvest.schedule.runner import _reference_pairs

    assert set(_reference_pairs(None)) == set(fx_support_tickers())
    assert _reference_pairs(None), "the pair list came back empty"
