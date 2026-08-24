"""Alignment and publication-aware forward-fill.

The look-ahead guarantee lives here, so these tests are the ones that matter
most: every other panel is derived from what this module aligns.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from cfdinvest.preprocessing import alignment
from cfdinvest.preprocessing.alignment import (
    AlignmentError,
    align_macro,
    align_market,
    align_panels,
    diagnose_publication_dates,
    infer_bar_interval,
    periods_per_year,
    publication_aware_ffill,
    resample_panels,
    resolve_publication_dates,
    to_panels,
    trading_calendar,
    value_available_at,
)
from cfdinvest.schema import TICKER, TIMESTAMP


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #
def tidy_market(sessions: pd.DatetimeIndex, tickers=("AAA", "BBB")) -> pd.DataFrame:
    """Deterministic tidy OHLCV, one ramp per ticker."""
    blocks = []
    for offset, ticker in enumerate(tickers):
        close = np.arange(len(sessions), dtype="float64") + 100.0 + 10 * offset
        blocks.append(
            pd.DataFrame(
                {
                    "open": close - 0.5,
                    "high": close + 1.0,
                    "low": close - 1.0,
                    "close": close,
                    "volume": np.full(len(sessions), 1_000.0),
                    TICKER: ticker,
                },
                index=sessions,
            )
        )
    return pd.concat(blocks).set_index(TICKER, append=True).sort_index()


def macro_long(
    observations, values, published, series_id="UNRATE"
) -> pd.DataFrame:
    """Stacked macro frame in the repository's shape."""
    index = pd.DatetimeIndex(observations, tz="UTC", name=TIMESTAMP)
    return pd.DataFrame(
        {
            "value": np.asarray(values, dtype="float64"),
            "publication_date": pd.DatetimeIndex(published, tz="UTC"),
            "series_id": series_id,
        },
        index=index,
    )


@pytest.fixture
def sessions() -> pd.DatetimeIndex:
    return pd.bdate_range("2024-01-01", periods=40, tz="UTC", name=TIMESTAMP)


#: UNRATE 2020, with its real release calendar.
UNRATE_OBS = ["2020-01-01", "2020-02-01", "2020-03-01", "2020-04-01"]
UNRATE_VALUES = [3.6, 3.5, 4.4, 14.7]
UNRATE_PUBLISHED = ["2020-02-07", "2020-03-06", "2020-04-03", "2020-05-08"]


# --------------------------------------------------------------------------- #
# Frequency inference
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("freq", "expected"),
    [("h", "1h"), ("D", "1d"), ("B", "1d"), ("W-FRI", "1wk"), ("MS", "1mo")],
)
def test_infer_bar_interval(freq, expected):
    index = pd.date_range("2024-01-01", periods=20, freq=freq, tz="UTC")
    assert infer_bar_interval(index) == expected


def test_infer_survives_holidays_and_weekends():
    """A real trading calendar has irregular spacing; the median absorbs it."""
    index = pd.bdate_range("2024-01-01", periods=60, tz="UTC")
    assert infer_bar_interval(index) == "1d"


def test_infer_single_bar_defaults_to_daily():
    assert infer_bar_interval(pd.DatetimeIndex(["2024-01-01"], tz="UTC")) == "1d"


def test_periods_per_year_accepts_label_or_index():
    assert periods_per_year("1d") == 252
    assert periods_per_year(pd.bdate_range("2024-01-01", periods=30, tz="UTC")) == 252


# --------------------------------------------------------------------------- #
# Market alignment
# --------------------------------------------------------------------------- #
def test_to_panels_splits_by_field(sessions):
    panels = to_panels(tidy_market(sessions))

    assert set(panels) == {"open", "high", "low", "close", "volume"}
    assert list(panels["close"].columns) == ["AAA", "BBB"]
    assert panels["close"].index.equals(sessions)


def test_panels_are_sorted_on_both_axes(sessions):
    panels = to_panels(tidy_market(sessions, tickers=("ZZZ", "AAA")))
    assert list(panels["close"].columns) == ["AAA", "ZZZ"]
    assert panels["close"].index.is_monotonic_increasing


def test_to_panels_of_empty_frame():
    assert to_panels(pd.DataFrame()) == {}


def test_align_panels_does_not_fill(sessions):
    panels = to_panels(tidy_market(sessions))
    extended = sessions.append(pd.DatetimeIndex(["2024-06-01"], tz="UTC"))

    aligned = align_panels(panels, extended)

    assert len(aligned["close"]) == len(sessions) + 1
    # The added session has no data and must stay empty, not inherit a price.
    assert aligned["close"].iloc[-1].isna().all()


def test_trading_calendar_uses_sessions_with_quotes(sessions):
    panels = to_panels(tidy_market(sessions))
    panels["close"].iloc[3] = np.nan          # nobody traded

    calendar = trading_calendar(panels)

    assert len(calendar) == len(sessions) - 1
    assert sessions[3] not in calendar


def test_resample_aggregates_each_field_correctly(sessions):
    panels = to_panels(tidy_market(sessions))
    weekly = resample_panels(panels, "W-MON")

    first_week = panels["close"].index < weekly["close"].index[1]
    assert weekly["open"].iloc[0]["AAA"] == panels["open"]["AAA"][first_week].iloc[0]
    assert weekly["close"].iloc[0]["AAA"] == panels["close"]["AAA"][first_week].iloc[-1]
    assert weekly["high"].iloc[0]["AAA"] == panels["high"]["AAA"][first_week].max()
    assert weekly["low"].iloc[0]["AAA"] == panels["low"]["AAA"][first_week].min()
    assert weekly["volume"].iloc[0]["AAA"] == panels["volume"]["AAA"][first_week].sum()


def test_resample_drops_empty_buckets(sessions):
    """A gap in the calendar must not become a zero-volume phantom bar."""
    sparse = sessions[:5].append(sessions[30:])
    panels = to_panels(tidy_market(sparse))

    weekly = resample_panels(panels, "W-MON")

    assert weekly["close"].notna().any(axis=1).all()
    assert (weekly["volume"].fillna(0) > 0).any(axis=1).all()


def test_align_market_end_to_end(sessions):
    panels, calendar = align_market(tidy_market(sessions))

    assert calendar.equals(sessions)
    for panel in panels.values():
        assert panel.index.equals(calendar)


def test_align_market_with_resampling(sessions):
    panels, calendar = align_market(tidy_market(sessions), rule="W-MON")

    assert len(calendar) < len(sessions)
    assert panels["close"].index.equals(calendar)


def test_disjoint_calendars_union_without_filling():
    """Two exchanges with different holidays: gaps stay gaps."""
    us = pd.DatetimeIndex(["2024-07-03", "2024-07-04", "2024-07-05"], tz="UTC")
    uk = pd.DatetimeIndex(["2024-07-03", "2024-07-05", "2024-07-08"], tz="UTC")
    market = pd.concat([tidy_market(us, ("US",)), tidy_market(uk, ("UK",))]).sort_index()

    panels, calendar = align_market(market)

    assert len(calendar) == 4
    assert pd.isna(panels["close"].loc[pd.Timestamp("2024-07-04", tz="UTC"), "UK"])
    assert pd.isna(panels["close"].loc[pd.Timestamp("2024-07-08", tz="UTC"), "US"])


# --------------------------------------------------------------------------- #
# value_available_at
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("as_of", "expected"),
    [
        ("2020-01-31", None),   # nothing published yet
        ("2020-02-06", None),   # day before the first release
        ("2020-02-07", 3.6),    # release day is inclusive
        ("2020-03-05", 3.6),    # still January until February prints
        ("2020-03-06", 3.5),
        ("2020-04-03", 4.4),
        ("2020-05-07", 4.4),    # April data not public yet
        ("2020-05-08", 14.7),   # the COVID print
        ("2021-01-01", 14.7),   # carries forward indefinitely
    ],
)
def test_value_available_at_boundaries(as_of, expected):
    frame = macro_long(UNRATE_OBS, UNRATE_VALUES, UNRATE_PUBLISHED)
    result = value_available_at(
        frame["value"], frame["publication_date"], as_of
    )
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected)


def test_value_available_at_accepts_naive_timestamps():
    frame = macro_long(UNRATE_OBS, UNRATE_VALUES, UNRATE_PUBLISHED)
    assert value_available_at(
        frame["value"], frame["publication_date"], pd.Timestamp("2020-03-06")
    ) == pytest.approx(3.5)


def test_value_available_at_skips_missing_values():
    frame = macro_long(
        ["2020-01-01", "2020-02-01"], [1.5, np.nan], ["2020-02-07", "2020-03-06"]
    )
    # February published but empty: the January value remains current.
    assert value_available_at(
        frame["value"], frame["publication_date"], "2020-03-10"
    ) == pytest.approx(1.5)


def test_value_available_at_can_report_missing_instead():
    frame = macro_long(
        ["2020-01-01", "2020-02-01"], [1.5, np.nan], ["2020-02-07", "2020-03-06"]
    )
    result = value_available_at(
        frame["value"], frame["publication_date"], "2020-03-10", skip_missing=False
    )
    assert result is None or np.isnan(result)


def test_value_available_at_on_empty_input():
    empty = macro_long([], [], [])
    assert value_available_at(empty["value"], empty["publication_date"], "2020-01-01") is None


# --------------------------------------------------------------------------- #
# publication_aware_ffill
# --------------------------------------------------------------------------- #
def test_fill_respects_the_publication_boundary():
    calendar = pd.bdate_range("2020-01-01", "2020-06-01", tz="UTC", name=TIMESTAMP)
    filled = publication_aware_ffill(
        macro_long(UNRATE_OBS, UNRATE_VALUES, UNRATE_PUBLISHED), calendar
    )["UNRATE"]

    assert pd.isna(filled.loc["2020-02-06"])
    assert filled.loc["2020-02-07"] == pytest.approx(3.6)
    assert filled.loc["2020-05-07"] == pytest.approx(4.4)
    assert filled.loc["2020-05-08"] == pytest.approx(14.7)


def test_nothing_is_known_before_the_first_release():
    calendar = pd.bdate_range("2020-01-01", "2020-02-06", tz="UTC")
    filled = publication_aware_ffill(
        macro_long(UNRATE_OBS, UNRATE_VALUES, UNRATE_PUBLISHED), calendar
    )
    assert filled["UNRATE"].isna().all()


def test_observation_date_fill_would_leak_but_this_does_not():
    """The exact bug this module exists to prevent."""
    calendar = pd.DatetimeIndex(["2020-04-05"], tz="UTC")
    frame = macro_long(UNRATE_OBS, UNRATE_VALUES, UNRATE_PUBLISHED)

    honest = publication_aware_ffill(frame, calendar)["UNRATE"].iloc[0]
    naive = frame["value"][frame.index <= calendar[0]].iloc[-1]

    assert honest == pytest.approx(4.4)     # March, published 2020-04-03
    assert naive == pytest.approx(14.7)     # April, not public until 05-08
    assert honest != naive


def test_fill_carries_the_last_value_forward_indefinitely():
    calendar = pd.bdate_range("2020-05-08", "2021-05-08", tz="UTC")
    filled = publication_aware_ffill(
        macro_long(UNRATE_OBS, UNRATE_VALUES, UNRATE_PUBLISHED), calendar
    )
    # np.allclose, not `Series == approx`: the latter compares the whole
    # series to the scalar and collapses to a single False.
    assert np.allclose(filled["UNRATE"], 14.7)


def test_fill_skips_missing_observations():
    """A holiday with no print carries the previous value, not a hole."""
    frame = macro_long(
        ["2020-01-01", "2020-01-02", "2020-01-03"],
        [1.88, np.nan, 1.80],
        ["2020-01-02", "2020-01-03", "2020-01-06"],
    )
    calendar = pd.DatetimeIndex(["2020-01-03", "2020-01-06"], tz="UTC")

    filled = publication_aware_ffill(frame, calendar)["UNRATE"]

    assert filled.loc["2020-01-03"] == pytest.approx(1.88)
    assert filled.loc["2020-01-06"] == pytest.approx(1.80)


def test_fill_handles_several_series_of_different_frequency():
    monthly = macro_long(UNRATE_OBS, UNRATE_VALUES, UNRATE_PUBLISHED, "UNRATE")
    daily = macro_long(
        ["2020-01-02", "2020-01-03"], [1.88, 1.80], ["2020-01-03", "2020-01-06"], "DGS10"
    )
    calendar = pd.bdate_range("2020-01-01", "2020-06-01", tz="UTC")

    filled = publication_aware_ffill(pd.concat([monthly, daily]).sort_index(), calendar)

    assert list(filled.columns) == ["DGS10", "UNRATE"]
    assert filled.loc["2020-01-06", "DGS10"] == pytest.approx(1.80)
    assert pd.isna(filled.loc["2020-01-06", "UNRATE"])


def test_fill_is_pure():
    frame = macro_long(UNRATE_OBS, UNRATE_VALUES, UNRATE_PUBLISHED)
    before = frame.copy()
    publication_aware_ffill(frame, pd.bdate_range("2020-01-01", periods=100, tz="UTC"))
    pd.testing.assert_frame_equal(frame, before)


def test_fill_is_deterministic():
    frame = macro_long(UNRATE_OBS, UNRATE_VALUES, UNRATE_PUBLISHED)
    calendar = pd.bdate_range("2020-01-01", periods=120, tz="UTC")
    pd.testing.assert_frame_equal(
        publication_aware_ffill(frame, calendar),
        publication_aware_ffill(frame, calendar),
    )


def test_out_of_order_input_is_handled():
    """Rows arriving unsorted must not change the answer."""
    frame = macro_long(UNRATE_OBS, UNRATE_VALUES, UNRATE_PUBLISHED)
    calendar = pd.bdate_range("2020-01-01", periods=120, tz="UTC")

    pd.testing.assert_frame_equal(
        publication_aware_ffill(frame.iloc[::-1], calendar),
        publication_aware_ffill(frame, calendar),
    )


def test_late_revision_of_an_old_period_does_not_win():
    """Publication order and observation order can disagree."""
    frame = macro_long(
        ["2020-01-01", "2020-02-01", "2019-12-01"],
        [3.6, 3.5, 9.9],
        ["2020-02-07", "2020-03-06", "2020-03-20"],   # Dec-2019 revised last
        "UNRATE",
    )
    calendar = pd.DatetimeIndex(["2020-03-25"], tz="UTC")

    filled = publication_aware_ffill(frame, calendar)["UNRATE"]

    # The newest *observation period* wins, not the newest publication.
    assert filled.iloc[0] == pytest.approx(3.5)


def test_fill_onto_an_empty_calendar():
    frame = macro_long(UNRATE_OBS, UNRATE_VALUES, UNRATE_PUBLISHED)
    assert publication_aware_ffill(frame, pd.DatetimeIndex([], tz="UTC")).empty


def test_fill_of_empty_macro():
    calendar = pd.bdate_range("2020-01-01", periods=5, tz="UTC")
    result = publication_aware_ffill(pd.DataFrame(), calendar)
    assert result.empty
    assert result.index.equals(pd.DatetimeIndex(calendar, name=TIMESTAMP))


# --------------------------------------------------------------------------- #
# Backward compatibility with pre-publication_date files
# --------------------------------------------------------------------------- #
def test_missing_publication_dates_raise_by_default():
    legacy = pd.DataFrame(
        {"value": [1.0, 2.0], "series_id": "OLD"},
        index=pd.DatetimeIndex(["2020-01-01", "2020-02-01"], tz="UTC", name=TIMESTAMP),
    )
    with pytest.raises(AlignmentError, match="no publication_date"):
        resolve_publication_dates(legacy)


def test_missing_publication_dates_can_fall_back_to_a_lag(caplog):
    legacy = pd.DataFrame(
        {"value": [1.0, 2.0], "series_id": "OLD"},
        index=pd.DatetimeIndex(["2020-01-01", "2020-02-01"], tz="UTC", name=TIMESTAMP),
    )
    resolved = resolve_publication_dates(legacy, missing="lag", fallback_lag_days=5)

    assert (resolved == legacy.index + pd.Timedelta(days=5)).all()
    assert "ASSUMED, not vendor-reported" in caplog.text


def test_legacy_file_aligns_under_the_lag_policy():
    legacy = pd.DataFrame(
        {"value": [1.0, 2.0], "series_id": "OLD"},
        index=pd.DatetimeIndex(["2020-01-01", "2020-02-01"], tz="UTC", name=TIMESTAMP),
    )
    calendar = pd.bdate_range("2020-01-01", "2020-03-01", tz="UTC")

    filled = publication_aware_ffill(
        legacy, calendar, missing_publication="lag", fallback_lag_days=1
    )

    assert pd.isna(filled.loc["2020-01-01", "OLD"])
    assert filled.loc["2020-01-02", "OLD"] == pytest.approx(1.0)


def test_error_message_names_the_remedy():
    legacy = pd.DataFrame(
        {"value": [1.0], "series_id": "OLD"},
        index=pd.DatetimeIndex(["2020-01-01"], tz="UTC", name=TIMESTAMP),
    )
    with pytest.raises(AlignmentError, match="publication_lag"):
        resolve_publication_dates(legacy)


# --------------------------------------------------------------------------- #
# Diagnostics for latest-mode files
# --------------------------------------------------------------------------- #
def test_latest_mode_series_is_diagnosed():
    """Every row stamped with the ingest date -> no usable release calendar."""
    frame = macro_long(
        UNRATE_OBS, UNRATE_VALUES, ["2026-08-24"] * 4, "UNRATE"
    )
    flags = diagnose_publication_dates(frame)["UNRATE"]

    assert flags["constant_publication_date"] is True
    assert flags["published_after_last_observation"] is True
    assert flags["usable"] is False


def test_point_in_time_series_is_diagnosed_usable():
    frame = macro_long(UNRATE_OBS, UNRATE_VALUES, UNRATE_PUBLISHED)
    assert diagnose_publication_dates(frame)["UNRATE"]["usable"] is True


def test_align_macro_warns_about_an_unusable_series(caplog):
    frame = macro_long(UNRATE_OBS, UNRATE_VALUES, ["2026-08-24"] * 4, "UNRATE")
    calendar = pd.bdate_range("2020-01-01", "2020-06-01", tz="UTC")

    filled = align_macro(frame, calendar)

    assert "'latest' mode" in caplog.text
    # Correct, and now explained: nothing was public during the window.
    assert filled["UNRATE"].isna().all()
