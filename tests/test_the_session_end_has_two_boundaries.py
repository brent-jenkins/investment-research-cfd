"""Opening stops and flattening happens at DIFFERENT times, from 2026-09-23.

They were one constant. `close_delay_minutes` documents itself as "refuse to
OPEN within this many minutes of the session close" and has its measurement
in that role -- 7.6pp of return, inside the noise floor, for drawdown 10.5%
to 7.3%, and a control separating it from the flattening. The flatten was
derived from the SAME flag, so the last permitted open and the flatten landed
on one instant.

What that cost, read off the account:

    18:30:21  WFC  open   82.39
    19:00:31  WFC  close  82.26   flattened before the session close

Thirty minutes of exposure for a full round trip, which is precisely the
waste the sixty minutes was measured to prevent. An open permitted at 18:59
would have been flattened in seconds.

It also made the project's own description false. "Stops opening 60 minutes
before the close, flattens at the bell" -- it flattened 60 minutes before the
bell, and the book sat flat for the last hour of every session. Verified
against the calendar the dealing pass itself uses: `next_close` is 20:00 UTC
and the market was still open at 19:50.

THE OWNER'S FIGURES, 2026-09-23, and neither is measured at its new value:

    flatten_minutes_before_close = 30   flatten at 19:30
    close_delay_minutes          = 90   stop opening at 18:30

It CANNOT be measured here, which is the honest state rather than an excuse:
the decision lake is hourly, the 19:00 bar spans 19:00-20:00, so 19:30 sits
inside a bar and is invisible, and the engine models the flatten as an exit
at the open of the session's last bar. `livequotes` holds only a snapshot. A
5-minute series would settle it.

What supports widening rather than narrowing, on the account's own fills:
trips opened more than two hours into the session returned +117.42 at a 58.3%
win rate against +7.92 at 29.0% for earlier ones, and the later cohort
survives dropping its best two where the earlier one reverses to -174.66.
Late entries are the better cohort and the old boundary truncated them. 55
trips, so a screen.

Mutations checked:

  M1790 both windows derived from one constant again, so they coincide
  M1791 the fallback dropped, so a config with no flatten time stops
        flattening entirely rather than behaving as it did before
  M1792 the flatten window used for the opening block, inverting the two

**M1791 SURVIVED FIRST TIME, and the test was wrong rather than the
mutation.** It emptied the flatten window, and `all({}.values())` is True --
so every assertion here passed against a window that enforced nothing. That
is the inert gate in its vacuous-truth form: a check reporting success
because it has nothing to check. The assertions now require the window to
be non-empty as well as correct, which is the same lesson as M1762 passing
against `true # market-open` and `entered_bars_ago` passing against
`entered_bars_ago_renamed`.
"""

from __future__ import annotations

import pandas as pd
import pytest

from cfdinvest.schedule.app import session_end_windows

TICKERS = ("AAPL", "MSFT")
OPEN_NOW = {t: True for t in TICKERS}
CLOSE = pd.Timestamp("2026-09-23 20:00", tz="UTC")


def calendar(tickers, moment):
    """A market that shuts at 20:00 UTC, as US equities do."""
    return pd.DataFrame(
        {"is_open": {t: moment < CLOSE for t in tickers}}
    )


def fires_for_every_name(window, why):
    """Assert a window is ON for every name -- and that it HAS names.

    `all({}.values())` is True. M1791 emptied the flatten window and this
    file's assertions passed against it, which is the vacuous-truth version
    of the inert gate this project keeps finding: a check that reports
    success because it has nothing to check.
    """
    assert window, f"{why}: the window is EMPTY, so nothing is enforced"
    assert all(window.values()), f"{why}: {window!r}"


def fires_for_nobody(window, why):
    """Assert a window is OFF -- distinguishing 'off' from 'absent'."""
    assert window, f"{why}: the window is EMPTY rather than off"
    assert not any(window.values()), f"{why}: {window!r}"


def windows(now, *, open_block=90, flatten=30):
    moment = pd.Timestamp(now)
    moment = moment.tz_localize("UTC") if moment.tzinfo is None else moment
    return session_end_windows(
        calendar, TICKERS, OPEN_NOW, moment,
        open_block_minutes=open_block, flatten_minutes=flatten,
    )


# --------------------------------------------------------------------- #
# The two boundaries are genuinely two.
# --------------------------------------------------------------------- #
def test_mid_session_neither_window_fires() -> None:
    no_opens, flattening = windows("2026-09-23 17:00")
    fires_for_nobody(no_opens, "opening blocked mid-session")
    fires_for_nobody(flattening, "flattening mid-session")


def test_opening_stops_an_hour_before_the_flatten() -> None:
    """M1790, M1792: one constant driving both, or the two swapped.

    18:35 is inside the 90-minute opening block and outside the 30-minute
    flatten window. If these ever coincide again, this bar has to fail.
    """
    no_opens, flattening = windows("2026-09-23 18:35")

    fires_for_every_name(
        no_opens,
        "opening should already be blocked at 18:35, 85 minutes before a "
        "20:00 close, with close_delay_minutes at 90",
    )
    fires_for_nobody(
        flattening,
        "the book was flattened at the same moment opening stopped, which "
        "is the WFC case: thirty minutes of exposure for a round trip",
    )


def test_the_flatten_fires_in_the_last_half_hour() -> None:
    no_opens, flattening = windows("2026-09-23 19:35")

    fires_for_every_name(flattening, "the book was carried past 19:30")
    fires_for_every_name(no_opens, "opening must still be blocked by then")


def test_there_is_a_real_gap_between_the_last_open_and_the_flatten() -> None:
    """The property, stated as a duration rather than as two timestamps.

    A boundary test that only checks two chosen minutes would pass against
    windows one minute apart. This walks the session and asserts the gap is
    substantial, which is the thing that was actually wrong.
    """
    minutes_open_blocked = []
    minutes_flattening = []
    for step in range(0, 400, 5):
        moment = pd.Timestamp("2026-09-23 13:30", tz="UTC") + pd.Timedelta(minutes=step)
        no_opens, flattening = windows(moment)
        if any(no_opens.values()):
            minutes_open_blocked.append(moment)
        if any(flattening.values()):
            minutes_flattening.append(moment)

    first_block = min(minutes_open_blocked)
    first_flatten = min(minutes_flattening)
    gap = (first_flatten - first_block).total_seconds() / 60.0

    assert gap >= 45, (
        f"only {gap:.0f} minutes separate the last permitted open from the "
        f"flatten. They shared a constant until 2026-09-23 and the gap was "
        f"ZERO, which paid a full round trip for thirty minutes of exposure"
    )


# --------------------------------------------------------------------- #
# The fallback, so nothing moves for a configuration that has not chosen.
# --------------------------------------------------------------------- #
def test_an_unset_flatten_time_behaves_as_it_did_before() -> None:
    """M1791: the fallback dropped.

    Zero must mean "use the opening constant", not "never flatten". A book
    that stops flattening carries overnight, which is the exposure the
    2026-09-04 AMAT gap cost 131 on 7.3 units.
    """
    no_opens, flattening = windows("2026-09-23 18:35", open_block=90, flatten=0)

    fires_for_every_name(no_opens, "opening blocked at 18:35")
    fires_for_every_name(
        flattening,
        "with no flatten time configured the flatten stopped happening "
        "altogether, so the book would be carried overnight",
    )


def test_both_windows_empty_when_neither_is_configured() -> None:
    """Nothing configured means nothing enforced, not everything enforced."""
    no_opens, flattening = windows("2026-09-23 19:55", open_block=0, flatten=0)

    assert no_opens == {}
    assert flattening == {}


# --------------------------------------------------------------------- #
# The shipped configuration, read from where the live path reads it.
# --------------------------------------------------------------------- #
def test_the_shipped_configuration_separates_them() -> None:
    """Test the property at the values that actually run.

    "Test the property, not the constant" applies to the fixture too: the
    live path takes these from config.toml, so that is where they come from
    here. A config that quietly puts them back together must fail.
    """
    from cfdinvest.config import load_config

    strategy = load_config().strategy
    block = int(getattr(strategy, "close_delay_minutes", 0) or 0)
    flatten = int(getattr(strategy, "flatten_minutes_before_close", 0) or 0)

    assert flatten, "no flatten time is configured, so it falls back again"
    assert block > flatten, (
        f"opening stops {block} minutes before the close and the flatten "
        f"runs at {flatten} -- opening must stop FIRST, or a position is "
        f"opened into its own flatten"
    )
    assert block - flatten >= 45, (
        f"only {block - flatten} minutes between the last open and the "
        f"flatten; a trade needs room to be worth its spread"
    )
