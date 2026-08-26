

def test_an_intraday_start_is_pulled_inside_the_vendors_window():
    """M151: a configured start that returns nothing at all.

    Yahoo serves roughly 730 days of hourly bars and refuses a request
    reaching further rather than truncating it -- the whole response comes
    back empty. The configured start of 2005-01-01 is right for daily bars
    and fatal for hourly: the lake would stay empty with only a warning to
    show for it, which is exactly what happened the first time this was run.

    Daily is untouched, because the limit does not apply to it.
    """
    import pandas as pd

    from cfdinvest.config import AppConfig, MarketDataConfig
    from cfdinvest.ingestion.market import (
        INTRADAY_HISTORY_LIMIT_DAYS, MarketDataIngestor,
    )

    config = AppConfig(
        market=MarketDataConfig(tickers=("AAPL",), interval="1d", start="2005-01-01")
    )
    ingestor = MarketDataIngestor(config)

    daily = ingestor._effective_settings(interval="1d", start=None, end=None)
    assert daily.start == "2005-01-01", "the daily window must not be clamped"

    hourly = ingestor._effective_settings(interval="1h", start=None, end=None)
    assert hourly.start != "2005-01-01", "an unreachable start returns nothing"

    age = (pd.Timestamp.now(tz="UTC") - pd.Timestamp(hourly.start, tz="UTC")).days
    assert age < INTRADAY_HISTORY_LIMIT_DAYS, "still outside the vendor's window"
    # And not so cautious that it throws away usable history.
    assert age > INTRADAY_HISTORY_LIMIT_DAYS - 60

    # A start already inside the window is left exactly as given.
    recent = str((pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=30)).date())
    assert ingestor._effective_settings(
        interval="1h", start=recent, end=None
    ).start == recent
