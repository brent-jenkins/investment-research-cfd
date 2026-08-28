"""A position in a name that has left the universe runs out; it does not die.

The traded list went from nineteen names to eight on 2026-08-28. Eleven
instruments left it while holding open positions, and what happens to those
positions is a decision rather than a detail.

Left alone the answer was liquidation, and quietly. A departed name gets no
signal row, the paper pass reads its direction as ``flat``, and
``time_stop_days`` is 0.29 -- one US session -- so ``signal gone`` closes it
at the next open. Every position in a departing instrument would have been
closed within a session, attributed to a time stop, and nothing in the book
would have recorded that a config change was the cause.

The decision (owner, 2026-08-28) is that they run as normal: out on their
own reversal or their own time stop, the same exit they would have had if
the universe had never moved. That needs two things, and one without the
other is worse than neither.

M393 a departed name loses its signal and is liquidated within a session
M394 a departed name keeps its signal and is re-entered, so the universe
     never actually changes
M395 an empty traded list read as "everything permitted" rather than as a
     broken config
M396 the traded names losing their place at the front of the universe
M397 held instruments unreadable, taking the whole run down with them
"""

from __future__ import annotations

import inspect

import pytest

from cfdinvest.advisor.papertrade_run import _open_universe
from cfdinvest.schedule.app import _with_held


class Market:
    def __init__(self, tickers):
        self.tickers = tuple(tickers)


class Config:
    def __init__(self, tickers):
        self.market = Market(tickers)


def test_a_held_name_outside_the_universe_still_gets_signals(monkeypatch):
    """M393: the liquidation nobody asked for.

    Without this the position has no signal row, reads as flat, and the
    0.29-day time stop closes it at the next open.
    """
    monkeypatch.setattr(
        "cfdinvest.livequotes.poller.held_instruments",
        lambda config, account=None: ["AAPL", "NKE"],
    )
    names = _with_held(["AMD", "TSLA"], Config(["AMD", "TSLA"]))
    assert set(names) == {"AMD", "TSLA", "AAPL", "NKE"}


def test_the_traded_names_keep_their_place(monkeypatch):
    """M396: order lost, so a truncated universe drops the traded names.

    The traded names are deliberately not in alphabetical order and the
    held ones deliberately sort before them. A fixture that happens to be
    sorted already cannot tell "order preserved" from "order rebuilt", and
    the first version of this test could not -- sorting it passed.
    """
    monkeypatch.setattr(
        "cfdinvest.livequotes.poller.held_instruments",
        lambda config, account=None: ["AAPL", "TSLA", "ABNB"],
    )
    names = _with_held(["TSLA", "NVDA"], Config(["TSLA", "NVDA"]))
    assert names[:2] == ["TSLA", "NVDA"], "the traded names lost their place"
    assert names != sorted(names), "the order is merely alphabetical"
    assert len(names) == len(set(names)), "a held name that is also traded appears twice"


def test_an_unreadable_book_does_not_take_the_run_with_it(monkeypatch):
    """M397: no confirmations is not a reason to stop trading."""
    def boom(config, account=None):
        raise RuntimeError("no confirmations")

    monkeypatch.setattr("cfdinvest.livequotes.poller.held_instruments", boom)
    assert _with_held(["AMD"], Config(["AMD"])) == ["AMD"]


def test_no_universe_stays_no_universe(monkeypatch):
    """``None`` means "every instrument" downstream and must survive."""
    monkeypatch.setattr(
        "cfdinvest.livequotes.poller.held_instruments",
        lambda config, account=None: ["AAPL"],
    )
    assert _with_held(None, Config([])) is None


def test_a_departed_name_may_be_marked_but_not_re_entered():
    """M394: running a position out and taking a new one are different acts.

    If the open side used the signal universe, a departed name would simply
    be bought back and the universe would have changed on paper only.
    """
    universe = _open_universe(Config(["AMD", "TSLA"]))
    assert "AMD" in universe
    assert "AAPL" not in universe, "a departed name could be opened again"


def test_an_empty_traded_list_permits_nothing():
    """M395: the reading that turns a broken config into an unrestricted book.

    Absent configuration and empty configuration are different statements.
    Absent leaves the pass as permissive as it was; empty says so.
    """
    assert _open_universe(Config([])) == frozenset()

    class Bare:
        pass

    assert _open_universe(Bare()) is None, (
        "no market config should not silently forbid every trade"
    )


def test_the_open_side_actually_consults_the_universe():
    """The seam. A helper nothing calls is a decision that never happens."""
    from cfdinvest.advisor import papertrade_run

    source = inspect.getsource(papertrade_run.run_paper_pass)
    assert "_open_universe(config)" in source, (
        "the pass never asks which instruments may be opened"
    )
    assert "may_open is None or ticker in may_open" in source


def test_the_signal_universe_actually_includes_held_names():
    """The other seam, on the config the run is built from."""
    from cfdinvest.schedule import app

    source = inspect.getsource(app._backtest_config)
    assert "_with_held(" in source, (
        "the backtest universe is still the traded list alone"
    )


def test_the_two_universes_are_not_the_same_object():
    """The whole design is that opening is narrower than signalling.

    A refactor that unified them would restore the liquidation this exists
    to prevent, and every other test here would still pass.
    """
    from cfdinvest.config import load_config

    settings = load_config()
    traded = set(settings.market.tickers)
    opening = _open_universe(settings)
    assert opening == traded

    # The signal universe is a superset by construction: it is the traded
    # list plus whatever is held. It may equal it -- nothing held outside
    # the list -- but it may never be smaller.
    signalled = _with_held(list(settings.market.tickers), settings)
    assert traded.issubset(set(signalled))
