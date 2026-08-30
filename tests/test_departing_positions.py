"""A position in a name that has left the universe runs out; it does not die.

The traded list went from nineteen names to eight on 2026-08-28. Eleven
instruments left it while holding open positions, and what happens to those
positions is a decision rather than a detail. The decision: a departed name
keeps its signals so it can exit on its own reversal or time stop, and stops
being eligible to open. Leaving the list stops re-entry and nothing else.

Getting only half of that wrong is expensive and was: nine positions sat on
the demo account for want of the *pricing* half, refused every pass for "no
price" while financing accrued on all nine.

Rewritten on 2026-08-30 when the shadow book was removed. The rule is
unchanged; both halves moved. "What is held" now comes from
``persist.intent`` -- the engine's own final bar -- rather than from a book
that applied a different rule set, and opening is bounded by the traded
universe rather than by the book's own copy of it.
"""

from __future__ import annotations

import pytest

from cfdinvest.schedule import app


class Config:
    """Just enough app config for the universe helpers."""

    def __init__(self, tickers):
        self.market = type("M", (), {"tickers": list(tickers)})()
        self.strategy = type("S", (), {"exposure_per_unit_size": 4.0})()
        self.universe = type("U", (), {"follow_screen": False, "target": None,
                                       "size_curve": {}, "candidates": ()})()


def _intent(monkeypatch, tickers):
    """Make the engine appear to hold exactly ``tickers``."""
    from cfdinvest.persist import intent

    monkeypatch.setattr(
        intent, "held_tickers", lambda config=None, account=None: list(tickers)
    )
    monkeypatch.setattr(
        intent, "intended_positions",
        lambda config=None, account=None: [
            {"ticker": t, "direction": "long", "units": 1.0,
             "equity": 100_000.0, "exposure_per_unit_size": 4.0,
             "size": None, "confidence": 0.5}
            for t in tickers
        ],
    )


def test_a_held_name_outside_the_universe_still_gets_signals(monkeypatch):
    """The exit decision has to be taken on bars that keep arriving.

    A stale signal is confidently wrong where a missing one is merely
    absent, which is why this is worse than the failure it replaced.
    """
    _intent(monkeypatch, ["NKE"])
    monkeypatch.setattr(app, "_confirmed_instruments", lambda *a, **k: [])

    wanted = app._with_held(["AMD", "TSLA"], Config(["AMD", "TSLA"]))
    assert "NKE" in wanted, (
        "a name held by the engine but off the traded list lost its signals, "
        "so it could never decide to exit"
    )
    assert {"AMD", "TSLA"} <= set(wanted)


def test_a_name_held_only_on_the_account_still_gets_signals(monkeypatch):
    """Held is the broker's fact, not a configuration value."""
    _intent(monkeypatch, [])
    monkeypatch.setattr(app, "_confirmed_instruments", lambda *a, **k: ["COST"])

    wanted = app._with_held(["AMD"], Config(["AMD"]))
    assert "COST" in wanted, "a position open at the broker lost its signals"


def test_the_traded_names_keep_their_place(monkeypatch):
    """Order matters downstream: the traded list is fetched first so a
    partial failure degrades the departed names rather than the live ones."""
    _intent(monkeypatch, ["NKE"])
    monkeypatch.setattr(app, "_confirmed_instruments", lambda *a, **k: [])

    wanted = app._with_held(["AMD", "TSLA"], Config(["AMD", "TSLA"]))
    assert wanted[:2] == ["AMD", "TSLA"]


def test_nothing_is_added_twice(monkeypatch):
    """A name both held and traded is fetched once."""
    _intent(monkeypatch, ["AMD"])
    monkeypatch.setattr(app, "_confirmed_instruments", lambda *a, **k: ["AMD"])

    wanted = app._with_held(["AMD", "TSLA"], Config(["AMD", "TSLA"]))
    assert wanted.count("AMD") == 1


def test_an_unreadable_source_does_not_take_the_run_with_it(monkeypatch):
    """Signals for the traded list still matter when the held set cannot be
    read. Losing the whole pass would be a worse failure than losing the
    exits."""
    from cfdinvest.persist import intent

    def explode(*a, **k):
        raise OSError("disk gone")

    monkeypatch.setattr(intent, "held_tickers", explode)
    monkeypatch.setattr(app, "_confirmed_instruments", lambda *a, **k: [])

    wanted = app._with_held(["AMD", "TSLA"], Config(["AMD", "TSLA"]))
    assert {"AMD", "TSLA"} <= set(wanted)


def test_an_empty_traded_list_is_not_silently_everything(monkeypatch):
    """An empty universe permits nothing to open. Reading it as "no
    restriction" would open the whole candidate pool."""
    from cfdinvest.universe.traded import traded_universe

    resolved = traded_universe(Config([]))
    assert not resolved.names, (
        "an empty traded list resolved to something tradeable"
    )


def test_the_engine_is_the_source_of_what_is_held(monkeypatch):
    """Functional, not structural.

    The previous version of this asserted a string appeared in
    ``papertrade_run``'s source. It passed for four days while the account
    traded a different strategy, because the string was still there and
    still wrong.
    """
    _intent(monkeypatch, ["LRCX", "MU"])
    monkeypatch.setattr(app, "_confirmed_instruments", lambda *a, **k: [])

    assert set(app._book_instruments(Config(["AMD"]))) == {"LRCX", "MU"}
