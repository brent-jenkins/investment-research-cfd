

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
