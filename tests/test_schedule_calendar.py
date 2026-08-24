"""Market calendar: sessions, holidays, DST and the weekly FX session.

Holidays are derived from rules rather than listed, so these tests check the
rules against known-correct dates across several years — including years
past the one this was written in, which is exactly what a hardcoded table
would get wrong.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from cfdinvest.schedule.market_calendar import (
    SESSIONS,
    asset_class_of,
    describe,
    easter_sunday,
    is_holiday,
    is_open,
    next_close,
    next_open,
    session_bounds,
    session_for,
    us_market_holidays,
)


# --------------------------------------------------------------------------- #
# Holiday rules
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("year", "expected"),
    [
        (2023, dt.date(2023, 4, 9)),
        (2024, dt.date(2024, 3, 31)),
        (2025, dt.date(2025, 4, 20)),
        (2026, dt.date(2026, 4, 5)),
        (2030, dt.date(2030, 4, 21)),
    ],
)
def test_easter_is_computed_correctly(year, expected):
    assert easter_sunday(year) == expected


@pytest.mark.parametrize(
    ("year", "date"),
    [
        (2024, dt.date(2024, 1, 1)),    # New Year's Day
        (2024, dt.date(2024, 1, 15)),   # MLK, third Monday
        (2024, dt.date(2024, 2, 19)),   # Washington's Birthday
        (2024, dt.date(2024, 3, 29)),   # Good Friday
        (2024, dt.date(2024, 5, 27)),   # Memorial Day, last Monday
        (2024, dt.date(2024, 6, 19)),   # Juneteenth
        (2024, dt.date(2024, 7, 4)),    # Independence Day
        (2024, dt.date(2024, 9, 2)),    # Labor Day
        (2024, dt.date(2024, 11, 28)),  # Thanksgiving, fourth Thursday
        (2024, dt.date(2024, 12, 25)),  # Christmas
        (2025, dt.date(2025, 4, 18)),   # Good Friday
        (2026, dt.date(2026, 1, 19)),   # MLK
        (2027, dt.date(2027, 5, 31)),   # Memorial Day
    ],
)
def test_known_holidays_are_recognised(year, date):
    assert date in us_market_holidays(year)


def test_holiday_rules_keep_working_in_future_years():
    """The reason holidays are computed and not listed."""
    for year in range(2024, 2036):
        holidays = us_market_holidays(year)
        assert 9 <= len(holidays) <= 11
        assert _observed_new_year(year) in holidays


def _observed_new_year(year: int) -> dt.date:
    date = dt.date(year, 1, 1)
    if date.weekday() == 5:
        return date - dt.timedelta(days=1)
    if date.weekday() == 6:
        return date + dt.timedelta(days=1)
    return date


def test_weekend_holidays_are_observed_on_a_weekday():
    # 2021-07-04 was a Sunday; the NYSE closed Monday the 5th.
    assert dt.date(2021, 7, 5) in us_market_holidays(2021)
    assert dt.date(2021, 7, 4) not in us_market_holidays(2021)


def test_juneteenth_only_from_2022():
    assert dt.date(2021, 6, 18) not in us_market_holidays(2021)
    assert dt.date(2022, 6, 20) in us_market_holidays(2022)


def test_non_us_sessions_ignore_us_holidays():
    japan = SESSIONS["equity_jp"]
    assert not is_holiday(dt.date(2024, 7, 4), japan)
    assert is_holiday(dt.date(2024, 7, 4), SESSIONS["equity"])


# --------------------------------------------------------------------------- #
# Asset classification
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("ticker", "asset_class"),
    [
        ("SPY", "equity"),
        ("TLT", "bond"),
        ("GLD", "commodity"),
        ("EURUSD=X", "fx"),
        ("^VIX", "volatility"),
        ("EWJ", "equity_jp"),
        ("EFA", "equity_eu"),
        ("BTC-USD", "crypto"),
        ("WHO_KNOWS", "other"),
    ],
)
def test_asset_classification(ticker, asset_class):
    assert asset_class_of(ticker) == asset_class


def test_a_custom_mapping_overrides_the_default():
    assert asset_class_of("SPY", {"SPY": "crypto"}) == "crypto"


# --------------------------------------------------------------------------- #
# Daily sessions
# --------------------------------------------------------------------------- #
def test_us_equities_open_and_close_at_the_right_local_times():
    # Wednesday 2025-06-11. 09:30 ET is 13:30 UTC in summer.
    assert not is_open("SPY", "2025-06-11 13:29")
    assert is_open("SPY", "2025-06-11 13:30")
    assert is_open("SPY", "2025-06-11 19:59")
    assert not is_open("SPY", "2025-06-11 20:00")


def test_the_session_survives_daylight_saving():
    """09:30 local is 14:30 UTC in winter and 13:30 UTC in summer."""
    assert next_open("SPY", "2025-01-15 00:00") == pd.Timestamp(
        "2025-01-15 14:30", tz="UTC"
    )
    assert next_open("SPY", "2025-07-15 00:00") == pd.Timestamp(
        "2025-07-15 13:30", tz="UTC"
    )


def test_markets_are_shut_at_the_weekend():
    assert not is_open("SPY", "2025-06-14 15:00")   # Saturday
    assert not is_open("SPY", "2025-06-15 15:00")   # Sunday


def test_markets_are_shut_on_a_holiday():
    assert not is_open("SPY", "2025-07-04 15:00")


def test_next_open_skips_a_holiday_weekend():
    # Friday 2025-07-04 is a holiday, so the next open is Monday the 7th.
    assert next_open("SPY", "2025-07-03 21:00") == pd.Timestamp(
        "2025-07-07 13:30", tz="UTC"
    )


def test_next_open_returns_now_when_already_open():
    moment = pd.Timestamp("2025-06-11 15:00", tz="UTC")
    assert next_open("SPY", moment) == moment


def test_next_close_is_the_current_session_close_when_open():
    assert next_close("SPY", "2025-06-11 15:00") == pd.Timestamp(
        "2025-06-11 20:00", tz="UTC"
    )


def test_next_close_is_the_following_session_when_shut():
    assert next_close("SPY", "2025-06-14 12:00") == pd.Timestamp(
        "2025-06-16 20:00", tz="UTC"
    )


def test_session_bounds_are_none_on_a_non_trading_day():
    assert session_bounds(dt.date(2025, 7, 4), SESSIONS["equity"]) is None
    assert session_bounds(dt.date(2025, 6, 14), SESSIONS["equity"]) is None


def test_different_asset_classes_close_at_different_times():
    moment = "2025-06-11 15:00"
    assert next_close("SPY", moment) < next_close("TLT", moment)


def test_tokyo_trades_while_new_york_sleeps():
    # 2025-06-12 01:00 UTC is 10:00 in Tokyo, 21:00 the previous day in NY.
    assert is_open("EWJ", "2025-06-12 01:00")
    assert not is_open("SPY", "2025-06-12 01:00")


# --------------------------------------------------------------------------- #
# FX: the weekly session
# --------------------------------------------------------------------------- #
def test_fx_runs_through_the_week():
    assert is_open("EURUSD=X", "2025-06-11 03:00")   # Wednesday, small hours
    assert is_open("EURUSD=X", "2025-06-11 23:00")


def test_fx_is_shut_at_the_weekend():
    assert not is_open("EURUSD=X", "2025-06-14 12:00")   # Saturday
    assert not is_open("EURUSD=X", "2025-06-15 12:00")   # Sunday morning


def test_fx_opens_sunday_evening_new_york_time():
    # 17:00 ET on Sunday 2025-06-15 is 21:00 UTC.
    assert not is_open("EURUSD=X", "2025-06-15 20:59")
    assert is_open("EURUSD=X", "2025-06-15 21:01")


def test_fx_closes_friday_evening():
    assert is_open("EURUSD=X", "2025-06-13 20:59")
    assert not is_open("EURUSD=X", "2025-06-13 21:01")


def test_fx_next_open_from_the_weekend():
    assert next_open("EURUSD=X", "2025-06-14 12:00") == pd.Timestamp(
        "2025-06-15 21:00", tz="UTC"
    )


def test_fx_trades_through_a_us_holiday():
    """Thin, but open — which is why FX observes no US holidays."""
    assert is_open("EURUSD=X", "2025-07-04 15:00")


# --------------------------------------------------------------------------- #
# Always-open
# --------------------------------------------------------------------------- #
def test_crypto_never_closes():
    for moment in ("2025-06-14 03:00", "2025-07-04 15:00", "2025-12-25 12:00"):
        assert is_open("BTC-USD", moment)


def test_crypto_has_no_next_close():
    assert next_close("BTC-USD", "2025-06-11 15:00") is None


def test_crypto_next_open_is_now():
    moment = pd.Timestamp("2025-06-14 03:00", tz="UTC")
    assert next_open("BTC-USD", moment) == moment


# --------------------------------------------------------------------------- #
# Purity and shape
# --------------------------------------------------------------------------- #
def test_naive_timestamps_are_read_as_utc():
    assert is_open("SPY", "2025-06-11 15:00") == is_open(
        "SPY", pd.Timestamp("2025-06-11 15:00", tz="UTC")
    )


def test_results_are_deterministic():
    moment = "2025-06-11 15:00"
    assert [is_open("SPY", moment) for _ in range(5)] == [True] * 5
    assert next_open("SPY", moment) == next_open("SPY", moment)


def test_describe_tabulates_the_universe():
    table = describe(["SPY", "EURUSD=X", "BTC-USD"], "2025-06-11 15:00")

    assert list(table.index) == ["SPY", "EURUSD=X", "BTC-USD"]
    assert set(table.columns) == {
        "asset_class", "session", "is_open", "next_open", "next_close"
    }
    assert table.loc["BTC-USD", "is_open"]


def test_describe_of_an_empty_universe():
    assert describe([], "2025-06-11 15:00").empty


def test_unknown_tickers_fall_back_to_a_session():
    assert session_for("MYSTERY").name == "Unclassified"
    assert is_open("MYSTERY", "2025-06-11 15:00")
