"""A request the server refuses fetches nothing, forever.

``capital_1m`` holds 778 rows from January 2025 and collected nothing for
the eight months after. ``ingest-minute`` ran every night the whole time.

The cause is that ``backfill_ticker`` resumes from the tail on disk, or from
``DEFAULT_START`` of 2023-01-01 when there is no file, and Capital serves
fine resolutions for about 400 days. Probed on 2026-09-10: 1m, 5m and 15m
all returned bars from 400 days back and nothing from 980. So every request
asked for a window the server refuses, came back HTTP 404, and wrote
nothing -- one warning a night that nobody read.

A permission fault on 2026-08-30 later hid it further by producing a louder
error for the same silent outcome, which is why restoring writability
changed nothing on its own.

The clamp moves the start forward to the served window. It loses the bars
between a stale tail and that window -- but those are bars the server no
longer has, and the alternative is fetching nothing at all.

**These tests call the real ``backfill_ticker`` and capture the ``start`` it
hands the fetcher.** The first version computed the expected start with a
local helper and asserted against that, which is a second implementation of
the rule -- forbidden here, and it showed: three of four mutations survived
because the tests were checking the copy rather than the code. Only the
mutation run caught it.

Mutations checked, each reintroduced as a bug and confirmed to fail:
  M490 the clamp removed, so a stale tail asks beyond the window again
  M491 the clamp applied to 1h and 1d, truncating the backtest's history
  M492 the clamp moving the start BACKWARD, widening rather than narrowing
  M493 a recent tail clamped anyway, refetching the window every night
"""

from __future__ import annotations

import pandas as pd
import pytest

from cfdinvest.ingestion import capital_prices
from cfdinvest.ingestion.capital_prices import MAX_LOOKBACK_DAYS


def asked_for(tmp_path, resolution, tail=None, start=None):
    """Run the real backfill and return the ``start`` it requested.

    The fetcher records and returns nothing, so no network is touched and
    the answer is exactly what the code decided rather than what a test
    thinks it should have decided.
    """
    seen: dict[str, str] = {}

    def fake_fetch(epic, session, config, *, resolution, start, **kwargs):
        seen["start"] = start
        return []

    # The real PathConfig, not a stand-in: a fake here would be a second
    # implementation of the directory layout, which is the same mistake this
    # file's first draft made about the clamp itself.
    from cfdinvest.config import PathConfig

    paths = PathConfig(data_root=tmp_path)

    class AppConfig:
        pass

    AppConfig.paths = paths

    if tail is not None:
        from cfdinvest import storage

        index = pd.DatetimeIndex([pd.Timestamp(tail, tz="UTC")], name="timestamp")
        frame = pd.DataFrame(
            {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0],
             "volume": [1.0], "ticker": ["MU"]}, index=index)
        storage.write_parquet(frame, storage.market_path(
            paths, "MU", "capital_" + resolution))

    kwargs = {"resolution": resolution, "fetch": fake_fetch,
              "interval": "capital_" + resolution, "app_config": AppConfig()}
    if start is not None:
        kwargs["start"] = start
    capital_prices.backfill_ticker("MU", None, None, **kwargs)
    return pd.Timestamp(seen["start"], tz="UTC")


def days_back(stamp):
    return (pd.Timestamp.now(tz="UTC") - stamp).total_seconds() / 86400


def test_fine_resolutions_are_bounded_and_coarse_ones_are_not():
    """M491: clamping 1h or 1d would truncate the backtest's own history."""
    assert MAX_LOOKBACK_DAYS.get("1m") and MAX_LOOKBACK_DAYS.get("5m")
    assert MAX_LOOKBACK_DAYS.get("1h") is None, "clamping hourly cuts the backtest"
    assert MAX_LOOKBACK_DAYS.get("1d") is None


def test_the_default_start_is_clamped_for_a_fine_resolution(tmp_path):
    """M490: the actual bug. 2023-01-01 is ~980 days back and 404s."""
    asked = asked_for(tmp_path, "1m")

    assert days_back(asked) == pytest.approx(MAX_LOOKBACK_DAYS["1m"], abs=1), (
        f"asked from {asked}, which is {days_back(asked):.0f} days back"
    )


def test_an_hourly_request_still_reaches_2023(tmp_path):
    """The mirror: without it the test above passes on a clamp of everything."""
    asked = asked_for(tmp_path, "1h")

    assert asked.year == 2023, f"hourly history was truncated to {asked}"


def test_the_stale_tail_that_stalled_for_eight_months(tmp_path):
    """capital_1m's own tail, 2025-01-03, about 615 days back."""
    asked = asked_for(tmp_path, "1m", tail="2025-01-03T20:59:00")

    assert days_back(asked) == pytest.approx(MAX_LOOKBACK_DAYS["1m"], abs=1), (
        "a tail older than the served window must be moved forward, or the "
        "fetch returns nothing and the series never advances"
    )


def test_a_recent_tail_is_left_alone(tmp_path):
    """M493: clamping unconditionally turns an incremental resume into a
    full re-pull every night."""
    recent = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=2)
    asked = asked_for(tmp_path, "1m", tail=recent.strftime("%Y-%m-%dT%H:%M:%S"))

    assert days_back(asked) < 10, (
        f"a two-day-old tail was pushed back to {asked}; the resume should "
        "have been left where it was"
    )


def test_the_clamp_never_widens_the_request(tmp_path):
    """M492: a clamp that moved the start backward would cause the bug it
    fixes. Checked across the range, because the failure is directional."""
    for back in (1, 30, 179, 181, 400, 980):
        tail = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=back)
        asked = asked_for(
            tmp_path / str(back), "5m",
            tail=tail.strftime("%Y-%m-%dT%H:%M:%S"))
        # RESUME_OVERLAP_HOURS deliberately steps back a little from the
        # tail, so allow that and no more.
        assert days_back(asked) <= back + 2, (
            f"a {back}-day tail produced a request {days_back(asked):.0f} "
            "days back -- the clamp widened the window"
        )


def test_the_window_is_inside_what_the_server_serves():
    """Measured, not chosen: Capital returned bars from 400 days back and
    nothing from 980."""
    for resolution, days in MAX_LOOKBACK_DAYS.items():
        assert 30 <= days <= 365, (
            f"{resolution} at {days}d is outside the probed 400-day window "
            "or too short to be useful"
        )
