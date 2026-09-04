"""What the daemon trades, and whether it can be talked into trading nothing.

The screen has ranked the pool and recorded the result for a long time
without being allowed to act on it, because acting meant rewriting the
configuration and this project forbids that. The resolution is to read the
append-only selection record instead and leave `market.tickers` untouched.

These pin the properties that make that safe rather than merely working.
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from cfdinvest.universe import traded as traded_module
from cfdinvest.universe.traded import STALE_AFTER_DAYS, traded_universe


class Market:
    def __init__(self, tickers):
        self.tickers = tuple(tickers)


class Universe:
    def __init__(self, follow):
        self.follow_screen = follow


class Config:
    def __init__(self, tickers=("MU", "AMD"), follow=False):
        self.market = Market(tickers)
        self.universe = Universe(follow)


def _record(monkeypatch, rows):
    monkeypatch.setattr(traded_module, "load_screens",
                        lambda config=None, account=None: rows, raising=False)
    import cfdinvest.universe.screen as screen
    monkeypatch.setattr(screen, "load_screens",
                        lambda config=None, account=None: rows)


def test_off_by_default_is_the_configured_list():
    """The setting exists to be switched on deliberately, and until then
    nothing about the traded universe changes."""
    result = traded_universe(Config())
    assert result.names == ("MU", "AMD")
    assert result.source == "config"


def test_following_the_screen_uses_the_latest_recorded_selection(monkeypatch):
    _record(monkeypatch, [
        {"as_of": "2026-06-01T00:00:00Z", "selected": ["OLD1", "OLD2"]},
        {"as_of": str(pd.Timestamp.now(tz="UTC")), "selected": ["NEW1", "NEW2"]},
    ])
    result = traded_universe(Config(follow=True))
    assert result.names == ("NEW1", "NEW2"), "an older selection won"
    assert result.source == "screen"
    assert not result.stale


def test_the_configured_list_is_never_rewritten(monkeypatch):
    """The hard constraint. Following the screen must leave the config
    exactly as a person left it -- the record is what moves."""
    config = Config(follow=True)
    _record(monkeypatch, [{"as_of": str(pd.Timestamp.now(tz="UTC")),
                           "selected": ["NEW1"]}])
    traded_universe(config)
    assert config.market.tickers == ("MU", "AMD"), (
        "the resolver wrote to the configuration"
    )


def test_nothing_recorded_falls_back_rather_than_trading_nothing(monkeypatch):
    """A deployment that has never screened must trade its configured list,
    not an empty book. 'Trade nothing' and 'there is no record' are
    different statements and only one is an instruction."""
    _record(monkeypatch, [])
    result = traded_universe(Config(follow=True))
    assert result.names == ("MU", "AMD")
    assert "no selection recorded" in result.source


def test_an_unreadable_store_falls_back_rather_than_trading_nothing(monkeypatch):
    def explode(config=None, account=None):
        raise OSError("disk gone")

    import cfdinvest.universe.screen as screen
    monkeypatch.setattr(screen, "load_screens", explode)
    result = traded_universe(Config(follow=True))
    assert result.names == ("MU", "AMD"), (
        "an unreadable record emptied the book"
    )


def test_an_empty_selection_row_is_skipped_not_obeyed(monkeypatch):
    """A screen that selected nothing is a failed screen, not an
    instruction to hold nothing."""
    _record(monkeypatch, [
        {"as_of": "2026-07-01T00:00:00Z", "selected": ["REAL1", "REAL2"]},
        {"as_of": str(pd.Timestamp.now(tz="UTC")), "selected": []},
    ])
    result = traded_universe(Config(follow=True))
    assert result.names == ("REAL1", "REAL2")


def test_a_stale_selection_is_used_and_named(monkeypatch):
    """Deliberately not a fallback to the configured list.

    A scheduler outage would otherwise swap the whole book back to
    `market.tickers`, closing and opening eight positions for a reason
    unrelated to any instrument. An old decision is still a decision; only
    silence about its age is unacceptable.
    """
    old = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=STALE_AFTER_DAYS + 10)
    _record(monkeypatch, [{"as_of": str(old), "selected": ["OLD1", "OLD2"]}])
    result = traded_universe(Config(follow=True))
    assert result.names == ("OLD1", "OLD2"), "a stale selection was discarded"
    assert result.stale is True
    assert "STALE" in result.describe()


def test_an_unparseable_date_is_not_called_fresh(monkeypatch):
    # Two names, matching the configured list, so the provenance guard
    # passes and the date logic is what is actually under test.
    _record(monkeypatch, [{"as_of": "not a date", "selected": ["X", "Y"]}])
    assert traded_universe(Config(follow=True)).stale is True


def test_the_daemon_has_one_owner_for_the_traded_list():
    """Seven call sites read `market.tickers` directly. Following the screen
    in six of them and missing the seventh is the realistic regression."""
    import inspect

    from cfdinvest.schedule import app

    source = inspect.getsource(app)
    assert "app_config.market.tickers" not in source, (
        "a daemon path reads the configured list directly and would ignore "
        "the screen's selection"
    )


def test_a_selection_made_for_a_different_universe_size_is_refused(monkeypatch):
    """M-live: stale by provenance rather than by age.

    The screen's target derives from ``len(market.tickers)`` when it runs.
    Narrowing the traded list from twenty names to eight left a two-day-old
    record holding twenty, ten of them instruments the current strategy has
    never been measured on. Every age-based check called it fresh, and
    following it would have tripled the book.
    """
    _record(monkeypatch, [{
        "as_of": str(pd.Timestamp.now(tz="UTC")),
        "selected": [f"N{i}" for i in range(20)],
    }])

    result = traded_universe(Config(tickers=("MU", "AMD"), follow=True))

    assert result.names == ("MU", "AMD"), (
        "a selection made for a twenty-name book was followed by an "
        "eight-name one"
    )
    assert "20 instruments" in result.source


def test_a_matching_selection_is_still_followed(monkeypatch):
    """The guard must not refuse the normal case -- the screen asks for as
    many as are traded, so sizes match on every ordinary run."""
    _record(monkeypatch, [{
        "as_of": str(pd.Timestamp.now(tz="UTC")),
        "selected": ["NEW1", "NEW2"],
    }])
    result = traded_universe(Config(tickers=("MU", "AMD"), follow=True))
    assert result.names == ("NEW1", "NEW2")
    assert result.source == "screen"


def test_the_selection_is_refused_whole_not_truncated(monkeypatch):
    """Taking the first N of a longer ranking is a different selection from
    the one the screen would make asking for N: the sector cap and the
    profile demotion both bind differently at different sizes."""
    _record(monkeypatch, [{
        "as_of": str(pd.Timestamp.now(tz="UTC")),
        "selected": ["A", "B", "C", "D"],
    }])
    result = traded_universe(Config(tickers=("MU", "AMD"), follow=True))
    assert result.names != ("A", "B"), "the selection was truncated to fit"


def test_the_universe_is_announced_on_change_not_on_every_call(monkeypatch, caplog):
    """M-live: the same line twice a minute.

    ``_traded`` is called by every job on the fifteen-minute cadence and
    several times within each. Announcing unconditionally wrote "Traded
    universe: 8 instruments from screen" every thirty seconds, which buries
    the one message that matters -- the day the list moves.
    """
    import logging

    from cfdinvest.schedule import app

    monkeypatch.setattr(app, "_LAST_UNIVERSE", None)
    _record(monkeypatch, [{"as_of": str(pd.Timestamp.now(tz="UTC")),
                           "selected": ["AAA", "BBB"]}])
    config = Config(follow=True)

    with caplog.at_level(logging.INFO, logger="cfdinvest.schedule.app"):
        for _ in range(5):
            assert app._traded(config) == ["AAA", "BBB"]
        announcements = [r for r in caplog.records if "Traded universe" in r.message]
        assert len(announcements) == 1, (
            f"announced {len(announcements)} times for one unchanged universe"
        )

        # A real change is announced, which is the point of keeping it at all.
        _record(monkeypatch, [{"as_of": str(pd.Timestamp.now(tz="UTC")),
                               "selected": ["AAA", "CCC"]}])
        app._traded(config)
        assert len([r for r in caplog.records if "Traded universe" in r.message]) == 2

    # And the names are in the line -- "8 instruments" alone cannot be
    # checked against anything later.
    assert "AAA" in " ".join(r.getMessage() for r in caplog.records
                             if "Traded universe" in r.getMessage())


def test_the_guard_compares_against_the_intended_size_not_the_ticker_count(monkeypatch):
    """M-live: the guard refusing the configuration working as designed.

    ``universe.target`` sets how many the screen selects, and defaults to
    the ticker count. Setting it explicitly -- ten held against an
    eight-name fallback list -- made every selection the wrong size by the
    guard's reckoning, so it would have refused each one and traded the
    fallback forever while warning about drift that was not happening.
    """
    config = Config(tickers=("MU", "AMD"), follow=True)
    config.universe.target = 3

    _record(monkeypatch, [{"as_of": str(pd.Timestamp.now(tz="UTC")),
                           "selected": ["A", "B", "C"]}])
    result = traded_universe(config)
    assert result.names == ("A", "B", "C"), (
        "a selection matching the configured target was refused for not "
        "matching the ticker count"
    )
    assert result.source == "screen"

    # And the guard still fires on a genuine mismatch against the target.
    _record(monkeypatch, [{"as_of": str(pd.Timestamp.now(tz="UTC")),
                           "selected": ["A", "B", "C", "D", "E"]}])
    stale = traded_universe(config)
    assert stale.names == ("MU", "AMD")
    assert "5 instruments" in stale.source


def test_the_traded_count_is_computed_from_the_curve_not_asserted():
    """The owner asked for a calculated number, not a chosen one.

    Read through the noise floor rather than by taking the curve's maximum:
    several sizes usually sit within it of each other, so the peak is not
    distinguishable from its neighbours and picking it would be a coin-toss
    dressed as an optimisation.

    Which sizes qualify depends on the floor, which is re-derived whenever
    the feed or the cost model changes -- it went from 14.6pp to 8.0pp on
    2026-08-30. So this asserts the rule, not its arithmetic. Hard-coding
    the answers made this test fail when the floor moved and the behaviour
    had not, which is the "test the property, not the constant" rule
    catching its own violation.
    """
    from cfdinvest.universe.traded import resolve_target

    class Uni:
        follow_screen = True
        target = None
        size_preference = "largest"
        size_curve = {4: 52.5, 6: 61.7, 8: 74.4, 10: 86.4,
                      12: 83.7, 16: 62.5, 20: 40.5, 24: 26.0}

    config = Config(follow=True)
    config.universe = Uni()

    from cfdinvest.risk.capacity import NOISE_FLOOR_PP

    peak = max(Uni.size_curve.values())
    band = sorted(
        size for size, value in Uni.size_curve.items()
        if peak - value <= NOISE_FLOOR_PP
    )
    assert len(band) > 1, (
        "the fixture no longer has several sizes within the floor, so it "
        "cannot exercise the choice this test exists for"
    )

    # A fallback above the band, so the VIABILITY CEILING does not bind and
    # this tests the curve alone. With no balance recorded the ceiling falls
    # back to the held count, which is correct -- an uncomputable input must
    # not expand the book -- and would otherwise mask what is under test.
    held = max(band) + 10
    assert resolve_target(config, held) == max(band), (
        f"returns-first did not take the band's top; band was {band}"
    )

    Uni.size_preference = "smallest"
    assert resolve_target(config, held) == min(band), (
        f"risk-first did not take the band's bottom; band was {band}"
    )

    # And the ceiling DOES bind when it is below the band: an account holding
    # four names is not expanded to ten because a curve says ten measured
    # well, when nothing is known about what it can size.
    Uni.size_preference = "largest"
    assert resolve_target(config, 4) == 4, (
        "the curve raised the target above the held count while the "
        "viability ceiling was uncomputable, which is absence read as "
        "permission"
    )

    # An explicit target overrides the computation, and no curve falls back
    # to the traded count rather than inventing one.
    Uni.size_curve = {}
    assert resolve_target(config, 9) == 9




# --------------------------------------------------------------------------- #
# A refused selection is not an absent one.
#
# The halt mail reported "Last usable selection: none on record" for a
# selection recorded minutes earlier, because the refusal path built a
# TradedUniverse without `as_of`. That conflates two states which need
# different actions: a screen that has never run needs investigating, and a
# selection made for a different size resolves itself on the next run.
#
# M359 the refusal dropping the date, so a fresh selection reads as absent
# M360 the two states sharing a message, so the remedy is wrong for one
# --------------------------------------------------------------------------- #


def test_a_selection_refused_for_size_keeps_its_date():
    """The operator must be able to see the screen IS running."""
    from cfdinvest.universe.traded import traded_universe

    class Uni:
        follow_screen = True
        target = 25
        size_preference = "largest"
        size_curve = {}
        max_target = 25

    config = Config(follow=True)
    config.universe = Uni()

    rows = [{"as_of": "2026-09-04T16:00:00+00:00",
             "selected": [f"T{n}" for n in range(10)]}]
    import cfdinvest.universe.screen as screen
    original = screen.load_screens
    screen.load_screens = lambda *a, **k: rows
    try:
        universe = traded_universe(config)
    finally:
        screen.load_screens = original

    assert universe.screen_failed, "a size mismatch should refuse the selection"
    assert universe.as_of, (
        "the refusal dropped the selection's date, so the halt mail reports "
        "'none on record' for a selection that exists and is recent"
    )
    assert "intended" in universe.source, (
        "the source does not say what was intended, so the reader cannot "
        "tell a size mismatch from a screen that never ran"
    )


def test_the_halt_mail_gives_a_different_remedy_for_each_state():
    """A working job must not be the thing the reader is sent to check."""
    import inspect

    from cfdinvest.schedule import app

    source = inspect.getsource(app._send_screen_halt)
    assert "The next screen run resolves this" in source, (
        "the mail has no branch for a selection refused on size, so it tells "
        "the reader to check a job that is working"
    )
    assert "screen-universe is running" in source, (
        "the genuine-failure remedy has been lost"
    )
    assert 'if "instruments," in universe.source' in source, (
        "the two states are no longer distinguished"
    )
