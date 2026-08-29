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
    _record(monkeypatch, [{"as_of": "not a date", "selected": ["X"]}])
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


def test_the_book_opens_in_the_same_universe_the_daemon_trades(monkeypatch):
    """The book is what the demo account mirrors. If it opened in the old
    list while the daemon followed the screen, the divergence would appear
    as trades nobody chose.

    Asserted by calling it rather than by reading the source: a check for
    the word ``traded_universe`` in the function is satisfied by the import
    line alone, and passed a mutation that reverted the assignment under it.
    """
    from cfdinvest.advisor import papertrade_run

    _record(monkeypatch, [{"as_of": str(pd.Timestamp.now(tz="UTC")),
                           "selected": ["NEW1", "NEW2"]}])

    assert papertrade_run._open_universe(Config(follow=True)) == frozenset(
        {"NEW1", "NEW2"}
    ), "the book would open in the configured list, not the screen's choice"

    # And with the setting off it is exactly the configured list, unchanged.
    assert papertrade_run._open_universe(Config(follow=False)) == frozenset(
        {"MU", "AMD"}
    )


def test_unconfigured_is_not_the_same_as_configured_empty(monkeypatch):
    """M-live: the resolver flattened absence into emptiness.

    ``_open_universe`` answers ``None`` when nothing is configured, which
    leaves the pass as permissive as it was before the restriction existed,
    and ``frozenset()`` when a list is configured but empty, which permits
    nothing. The resolver returns a tuple either way, so consulting it
    first turned the permissive case into a total block on new positions --
    a config with no ticker list would have stopped the book opening
    anything, silently.
    """
    from cfdinvest.advisor import papertrade_run

    absent = Config(follow=False)
    absent.market.tickers = None
    assert papertrade_run._open_universe(absent) is None, (
        "an unconfigured list became a block on all new positions"
    )

    empty = Config(tickers=(), follow=False)
    assert papertrade_run._open_universe(empty) == frozenset(), (
        "an empty list must permit nothing, not everything"
    )

    # The case that actually exercises the guard: following the screen with
    # nothing configured and nothing recorded. The resolver answers with a
    # tuple, and taking it unconditionally is what destroys the
    # distinction.
    monkeypatch.setattr(traded_module, "load_screens",
                        lambda config=None, account=None: [], raising=False)
    import cfdinvest.universe.screen as screen
    monkeypatch.setattr(screen, "load_screens", lambda config=None, account=None: [])
    following_absent = Config(follow=True)
    following_absent.market.tickers = None
    assert papertrade_run._open_universe(following_absent) is None, (
        "following the screen with nothing configured and nothing recorded "
        "became a block on all new positions"
    )
