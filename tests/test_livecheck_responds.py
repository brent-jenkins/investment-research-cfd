"""The last look must respond to the quote it is given.

On 2026-09-10 it did not. Moving every price in the 50-name universe by 5%
moved **0 of 50** readings, because `ProvisionalRepository.load_market`
updated the bar in progress inside `working` and then returned the original
`frame`:

    if not rows:
        return frame

Once the update branch was added, every name took it and `continue`d, so
`rows` was empty on every pass and the whole check returned the lake
untouched. The readings therefore agreed with the engine exactly -- which
looked like a fix working perfectly, and was the check not running.

That is the failure mode this project keeps meeting from new directions: an
inert gate reporting a clean result. The earlier version of this same bug
was the opposite sign -- an appended zero-range bar collapsed strong
signals by 0.32 so nothing could clear 0.75 -- and both were invisible
because the number that came back was plausible.

So the property asserted here is not "the close equals the quote" but
**the reading moves when the quote moves**. A constant cannot satisfy it,
and neither can a discarded frame.

Mutations checked, each reintroduced as a bug and confirmed to fail:
  M600 the shipped bug -- `if not rows: return frame`, discarding the update
  M610 the early return kept but `working` swapped back for `frame`
  M620 `touched` never set, so the guard cannot see the update
  M630 the close write landing on a throwaway copy
  M631 the update targeting the instrument's first row, not its last
  M640 the high not extended when the quote prints above it
  M650 the append path removed, so a stale lake silently checks nothing
  M660 the appended bar given zero range again, refusing every signal
  M670 the appended bar opening at the quote rather than the prior close

A seventh was tried and is recorded because it says something about the
code rather than about the test: rebinding `working = working.copy()`
inside the loop SURVIVED, and correctly so -- the copy is what the function
then returns, so the behaviour is unchanged. It was a bad mutation rather
than a gap in the suite. The genuine form of that bug is M630, which the
same test catches.
"""

from __future__ import annotations

import pandas as pd
import pytest

from cfdinvest.advisor.livecheck import ProvisionalRepository

BARS = 6
START = pd.Timestamp("2026-09-10 09:00", tz="UTC")
NAMES = ("AAA", "BBB")


class FakeLake:
    """Just enough repository to hand a panel back."""

    def __init__(self, frame):
        self._frame = frame
        self.config = object()

    def load_market(self, *args, **kwargs):
        return self._frame


def panel():
    rows = []
    for step in range(BARS):
        stamp = START + pd.Timedelta(hours=step)
        for name in NAMES:
            base = 100.0 + step
            rows.append(
                {
                    "timestamp": stamp,
                    "ticker": name,
                    "open": base,
                    "high": base + 1.0,
                    "low": base - 1.0,
                    "close": base + 0.5,
                    "volume": 1000.0,
                }
            )
    frame = pd.DataFrame(rows).set_index(["timestamp", "ticker"])
    return frame.sort_index()


def newest():
    return START + pd.Timedelta(hours=BARS - 1)


def check(prices, at):
    return ProvisionalRepository(FakeLake(panel()), prices, at).load_market()


def test_the_reading_moves_when_the_quote_moves():
    """M600/M610/M620/M630: the whole point, stated as a response.

    Two quotes that differ must produce panels that differ. Every way of
    losing the update -- returning `frame`, writing to a copy, failing to
    record that anything happened -- makes these two identical, and a test
    asserting a single expected close would not have caught the third.
    """
    at = newest() + pd.Timedelta(minutes=20)

    low = check({"AAA": 90.0}, at)
    high = check({"AAA": 120.0}, at)

    got_low = float(low.loc[(newest(), "AAA"), "close"])
    got_high = float(high.loc[(newest(), "AAA"), "close"])

    assert got_low != got_high, (
        "the panel is identical for quotes 30 apart -- the check is not "
        "reading the quote at all"
    )
    assert got_low == pytest.approx(90.0)
    assert got_high == pytest.approx(120.0)


def test_the_bar_in_progress_is_updated_rather_than_appended():
    """The bar the lake is already building is the bar in progress.

    Appending a second bar at the same session point gives it zero range by
    construction, which is what collapsed strong signals before 6a81863.
    """
    at = newest() + pd.Timedelta(minutes=20)
    before = panel()
    after = check({"AAA": 120.0}, at)

    assert len(after) == len(before), (
        "a bar was appended rather than the one in progress being updated"
    )
    assert after.index.get_level_values(0).max() == newest()


def test_the_range_extends_to_contain_the_quote():
    """M640: a bar in progress whose high excludes its own last trade.

    Both directions, because extending only one is the kind of half-fix that
    passes a test written around a single case.
    """
    at = newest() + pd.Timedelta(minutes=20)
    row = panel().loc[(newest(), "AAA")]

    above = check({"AAA": float(row["high"]) + 5.0}, at).loc[(newest(), "AAA")]
    below = check({"AAA": float(row["low"]) - 5.0}, at).loc[(newest(), "AAA")]

    assert float(above["high"]) == pytest.approx(float(row["high"]) + 5.0)
    assert float(above["low"]) == pytest.approx(float(row["low"]))
    assert float(below["low"]) == pytest.approx(float(row["low"]) - 5.0)
    assert float(below["high"]) == pytest.approx(float(row["high"]))


def test_a_name_without_a_quote_is_left_alone():
    """The update must not leak across instruments.

    BBB is quoted nowhere in this call, so its bar must be exactly the
    lake's -- otherwise a single quote would rewrite the book.
    """
    at = newest() + pd.Timedelta(minutes=20)
    after = check({"AAA": 120.0}, at)

    pd.testing.assert_series_equal(
        after.loc[(newest(), "BBB")],
        panel().loc[(newest(), "BBB")],
        check_names=False,
    )


def test_the_appended_bar_carries_a_real_range():
    """The fallback must not be a motionless bar.

    It appended ``open = high = low = close = quote`` -- zero range, the
    same flat bar that collapsed strong readings by 0.32 before the update
    path existed. It fires whenever ingestion is more than one bar behind,
    which on an hourly grid is roughly the first twenty minutes of every
    hour.

    Measured live on 2026-09-11 at 16:04, lake newest 15:00, engine
    deciding on 14:00:

        name   on the bar   live (appended flat)
        ABT        0.789          0.457
        LMT        0.774          0.415
        REGN       0.777          0.438

    Every intended name refused, uniformly ~0.34 below its own reading. The
    quote WAS reaching the computation -- moving it 5% moved the reading
    from 0.000 to 0.95 -- so this was not the earlier fault returning. It
    was the fallback doing what it was written to do.

    A bar in progress opens near the previous close and has moved to the
    quote since, so the range must span that move.
    """
    at = newest() + pd.Timedelta(hours=4)
    prior_close = float(panel().loc[(newest(), "AAA"), "close"])

    above = check({"AAA": prior_close + 5.0}, at).loc[(at, "AAA")]

    assert float(above["open"]) == pytest.approx(prior_close), (
        "the bar does not open at the last completed close"
    )
    assert float(above["close"]) == pytest.approx(prior_close + 5.0)
    assert float(above["high"]) == pytest.approx(prior_close + 5.0)
    assert float(above["low"]) == pytest.approx(prior_close)
    assert float(above["high"]) > float(above["low"]), (
        "the appended bar is motionless, which is what refused every "
        "intended name for a third of each hour"
    )

    below = check({"AAA": prior_close - 5.0}, at).loc[(at, "AAA")]
    assert float(below["high"]) == pytest.approx(prior_close)
    assert float(below["low"]) == pytest.approx(prior_close - 5.0)


def test_a_stale_lake_still_gets_a_provisional_bar():
    """M650: ingestion behind is exactly when the check matters most.

    Past one bar's spacing the newest stored bar is no longer the one in
    progress, so there is nothing to update and appending is all that is
    left. Losing that path would make the check inert precisely when the
    lake has stopped keeping up -- the failure that cost eleven days in
    August.
    """
    at = newest() + pd.Timedelta(hours=4)
    after = check({"AAA": 120.0}, at)

    assert len(after) == len(panel()) + 1
    assert float(after.loc[(at, "AAA"), "close"]) == pytest.approx(120.0)


def test_a_quote_behind_the_lake_is_refused():
    """A stale vendor price must never rewrite a completed bar."""
    after = check({"AAA": 120.0}, newest() - pd.Timedelta(hours=1))

    pd.testing.assert_frame_equal(after, panel())
