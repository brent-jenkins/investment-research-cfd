"""UTC normalisation edge cases: DST, holidays, mixed exchanges/frequencies.

The engine's core invariant is that every stored index is tz-aware UTC. These
tests pin the behaviour at the awkward boundaries, because a silent tz slip
misaligns a whole backtest rather than raising.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from cfdinvest.ingestion.market import normalize_yfinance_frame
from cfdinvest.repository import to_panel, to_utc
from cfdinvest.schema import TICKER, TIMESTAMP, ensure_utc_index
from tests.conftest import make_yf_frame


def ohlcv(index: pd.DatetimeIndex) -> pd.DataFrame:
    """A raw yfinance-shaped frame on an arbitrary index."""
    n = len(index)
    close = np.arange(n, dtype="float64") + 100.0
    return pd.DataFrame(
        {
            "Open": close - 0.5,
            "High": close + 1.0,
            "Low": close - 1.0,
            "Close": close,
            "Volume": np.full(n, 1e6),
        },
        index=index,
    )


# --------------------------------------------------------------------------- #
# Naive daily bars (yfinance's daily shape)
# --------------------------------------------------------------------------- #
def test_naive_daily_bars_become_utc_midnight():
    index = pd.date_range("2024-03-08", periods=5, freq="D", name="Date")
    frame = normalize_yfinance_frame(ohlcv(index), "SPY")

    assert str(frame.index.tz) == "UTC"
    assert (frame.index.hour == 0).all()
    assert frame.index[0] == pd.Timestamp("2024-03-08", tz="UTC")


def test_naive_bars_are_not_shifted_by_localisation():
    """Localising must relabel the tz, never move the clock."""
    index = pd.DatetimeIndex(["2024-01-15", "2024-01-16"], name="Date")
    frame = normalize_yfinance_frame(ohlcv(index), "SPY")

    assert frame.index[0].strftime("%Y-%m-%d %H:%M") == "2024-01-15 00:00"


# --------------------------------------------------------------------------- #
# DST transitions on intraday bars
# --------------------------------------------------------------------------- #
def test_us_spring_forward_converts_without_gaps_or_dupes():
    """2024-03-10 02:00 America/New_York does not exist; hourly bars skip it."""
    index = pd.date_range(
        "2024-03-08 09:30", periods=48, freq="h", tz="America/New_York", name="Datetime"
    )
    frame = normalize_yfinance_frame(ohlcv(index), "SPY")

    assert str(frame.index.tz) == "UTC"
    assert len(frame) == 48
    assert frame.index.is_unique
    assert frame.index.is_monotonic_increasing


def test_us_fall_back_keeps_both_repeated_local_hours():
    """01:00 America/New_York occurs twice on 2024-11-03; UTC disambiguates."""
    index = pd.date_range(
        "2024-11-02 20:00", periods=12, freq="h", tz="America/New_York", name="Datetime"
    )
    frame = normalize_yfinance_frame(ohlcv(index), "SPY")

    # Both ambiguous local hours survive as distinct UTC instants -- if the
    # code localised instead of converting, one would be dropped as a dupe.
    assert len(frame) == 12
    assert frame.index.is_unique
    assert (frame.index.to_series().diff().dropna() == pd.Timedelta(hours=1)).all()


def test_utc_offset_changes_across_a_dst_boundary():
    index = pd.DatetimeIndex(
        [
            pd.Timestamp("2024-03-08 09:30", tz="America/New_York"),  # EST, UTC-5
            pd.Timestamp("2024-03-15 09:30", tz="America/New_York"),  # EDT, UTC-4
        ],
        name="Datetime",
    )
    frame = normalize_yfinance_frame(ohlcv(index), "SPY")

    assert frame.index[0] == pd.Timestamp("2024-03-08 14:30", tz="UTC")
    assert frame.index[1] == pd.Timestamp("2024-03-15 13:30", tz="UTC")


def test_european_dst_boundary_differs_from_us():
    """EU and US switch on different dates; UTC is the only common clock."""
    london = pd.DatetimeIndex(
        [pd.Timestamp("2024-03-20 09:00", tz="Europe/London")], name="Datetime"
    )
    new_york = pd.DatetimeIndex(
        [pd.Timestamp("2024-03-20 09:00", tz="America/New_York")], name="Datetime"
    )

    lhs = normalize_yfinance_frame(ohlcv(london), "EWU")
    rhs = normalize_yfinance_frame(ohlcv(new_york), "SPY")

    # 2024-03-20: US already on EDT, UK still on GMT.
    assert lhs.index[0] == pd.Timestamp("2024-03-20 09:00", tz="UTC")
    assert rhs.index[0] == pd.Timestamp("2024-03-20 13:00", tz="UTC")


# --------------------------------------------------------------------------- #
# Multi-exchange alignment
# --------------------------------------------------------------------------- #
def test_tokyo_and_new_york_sessions_align_on_one_clock():
    tokyo = pd.DatetimeIndex(
        [pd.Timestamp("2024-06-03 09:00", tz="Asia/Tokyo")], name="Datetime"
    )
    new_york = pd.DatetimeIndex(
        [pd.Timestamp("2024-06-03 09:30", tz="America/New_York")], name="Datetime"
    )

    jp = normalize_yfinance_frame(ohlcv(tokyo), "EWJ")
    us = normalize_yfinance_frame(ohlcv(new_york), "SPY")

    combined = pd.concat([jp, us]).sort_index()
    # Tokyo's open precedes New York's by 13.5h on the same calendar day.
    assert combined.index[0] == pd.Timestamp("2024-06-03 00:00", tz="UTC")
    assert combined.index[1] == pd.Timestamp("2024-06-03 13:30", tz="UTC")
    assert combined.index.is_monotonic_increasing


def test_mixed_offset_index_is_resolved_not_rejected():
    """A concat of two exchanges yields mixed offsets; must not degrade."""
    mixed = pd.Index(
        [
            pd.Timestamp("2024-06-03 09:00", tz="Asia/Tokyo"),
            pd.Timestamp("2024-06-03 09:30", tz="America/New_York"),
        ],
        dtype=object,
    )
    frame = ensure_utc_index(pd.DataFrame({"close": [1.0, 2.0]}, index=mixed))

    assert isinstance(frame.index, pd.DatetimeIndex)
    assert str(frame.index.tz) == "UTC"
    assert frame.index.is_monotonic_increasing


def test_exchange_holiday_leaves_a_hole_not_a_filled_bar():
    """A market closed for a holiday must be absent, never forward-filled."""
    sessions = pd.DatetimeIndex(
        ["2024-07-03", "2024-07-05", "2024-07-08"], name="Date"  # 4 July missing
    )
    frame = normalize_yfinance_frame(ohlcv(sessions), "SPY")

    assert len(frame) == 3
    assert pd.Timestamp("2024-07-04", tz="UTC") not in frame.index


def test_padded_holiday_rows_are_dropped_not_stored():
    """yfinance pads a mixed-exchange batch with all-NaN bars."""
    index = pd.date_range("2024-07-03", periods=3, freq="D", name="Date")
    raw = ohlcv(index)
    raw.loc[raw.index[1], ["Open", "High", "Low", "Close"]] = np.nan

    frame = normalize_yfinance_frame(raw, "EWU")

    assert len(frame) == 2
    assert pd.Timestamp("2024-07-04", tz="UTC") not in frame.index


def test_multi_exchange_panel_has_one_row_per_utc_instant():
    us = normalize_yfinance_frame(
        ohlcv(pd.DatetimeIndex(["2024-06-03", "2024-06-04"], name="Date")), "SPY"
    )
    uk = normalize_yfinance_frame(
        ohlcv(pd.DatetimeIndex(["2024-06-03", "2024-06-05"], name="Date")), "EWU"
    )

    panel = to_panel(
        pd.concat([us, uk]).set_index(TICKER, append=True).sort_index(), "close"
    )

    assert list(panel.columns) == ["EWU", "SPY"]
    assert len(panel) == 3                     # union of both calendars
    assert pd.isna(panel.loc[pd.Timestamp("2024-06-04", tz="UTC"), "EWU"])
    assert pd.isna(panel.loc[pd.Timestamp("2024-06-05", tz="UTC"), "SPY"])


# --------------------------------------------------------------------------- #
# Mixed-frequency joins
# --------------------------------------------------------------------------- #
def test_daily_and_hourly_bars_share_a_comparable_clock():
    daily = normalize_yfinance_frame(
        ohlcv(pd.DatetimeIndex(["2024-06-03"], name="Date")), "SPY"
    )
    hourly = normalize_yfinance_frame(
        ohlcv(
            pd.date_range(
                "2024-06-03 09:30", periods=3, freq="h",
                tz="America/New_York", name="Datetime",
            )
        ),
        "SPY",
    )

    combined = pd.concat([daily, hourly]).sort_index()

    assert str(combined.index.tz) == "UTC"
    assert combined.index.is_monotonic_increasing
    # The daily bar's midnight label sorts before the session's hourly bars.
    assert combined.index[0].hour == 0
    assert combined.index[-1] == pd.Timestamp("2024-06-03 15:30", tz="UTC")


def test_macro_daily_joins_onto_hourly_market_bars():
    macro = pd.DataFrame(
        {"value": [1.88]},
        index=pd.DatetimeIndex(["2024-06-03"], name=TIMESTAMP),
    )
    macro = ensure_utc_index(macro)

    hourly = normalize_yfinance_frame(
        ohlcv(
            pd.date_range(
                "2024-06-03 09:30", periods=4, freq="h",
                tz="America/New_York", name="Datetime",
            )
        ),
        "SPY",
    )

    joined = hourly.join(macro, how="left")
    # Midnight-stamped macro does not land on an intraday bar; that alignment
    # is preprocessing's job. What matters here is that the join is legal --
    # same tz, same dtype -- rather than raising.
    assert len(joined) == len(hourly)
    assert "value" in joined.columns


# --------------------------------------------------------------------------- #
# Degenerate inputs
# --------------------------------------------------------------------------- #
def test_unparseable_timestamps_are_dropped_not_propagated():
    frame = pd.DataFrame(
        {"close": [1.0, 2.0, 3.0]},
        index=pd.Index(["2024-06-03", "not-a-date", "2024-06-05"]),
    )
    result = ensure_utc_index(frame)

    assert len(result) == 2
    assert result.index.notna().all()


def test_duplicate_timestamps_keep_the_last_observation():
    index = pd.DatetimeIndex(["2024-06-03", "2024-06-03"], name="Date")
    frame = ensure_utc_index(pd.DataFrame({"close": [1.0, 2.0]}, index=index))

    assert len(frame) == 1
    assert frame["close"].iloc[0] == 2.0


def test_unsorted_input_is_sorted():
    index = pd.DatetimeIndex(["2024-06-05", "2024-06-03", "2024-06-04"], name="Date")
    frame = ensure_utc_index(pd.DataFrame({"close": [3.0, 1.0, 2.0]}, index=index))

    assert list(frame["close"]) == [1.0, 2.0, 3.0]


def test_empty_frame_still_carries_a_utc_index():
    result = ensure_utc_index(pd.DataFrame({"close": pd.Series(dtype="float64")}))
    assert str(result.index.tz) == "UTC"
    assert result.index.name == TIMESTAMP


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2024-06-03", "2024-06-03 00:00"),
        (pd.Timestamp("2024-06-03 12:00", tz="Asia/Tokyo"), "2024-06-03 03:00"),
        (pd.Timestamp("2024-06-03 09:30", tz="America/New_York"), "2024-06-03 13:30"),
    ],
)
def test_to_utc_boundary_values(value, expected):
    assert to_utc(value).strftime("%Y-%m-%d %H:%M") == expected


def test_normalisation_is_idempotent():
    frame = normalize_yfinance_frame(make_yf_frame(pd.bdate_range("2024-01-01", periods=5)), "SPY")
    again = ensure_utc_index(frame)
    pd.testing.assert_index_equal(frame.index, again.index)
