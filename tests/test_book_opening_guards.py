"""The shadow book opens on the same rules as the account.

The guards went into the bridge first, on the reasoning that the book is
only a simulation so entering badly costs nothing. The live session of
2026-08-28 showed why that is wrong: the book opened at 13:31 and the
account at 14:03, and reconciliation reported a median fill difference of
28.9bp. Thirty-two minutes of price drift was being read as simulation
error, which is the single thing that comparison exists to measure.

The book decides and the account mirrors, so the guards belong on the book.

M378 the book opening at the bell while the account waits
M379 a close delayed by the opening window
M380 the book opening its whole list in one pass
M381 the deferred opens chosen by dictionary order rather than conviction
M382 an unreadable calendar treated as permission to open
"""

from __future__ import annotations

import pandas as pd
import pytest

from cfdinvest.advisor import papertrade, papertrade_run


@pytest.fixture
def book(tmp_path, monkeypatch):
    path = tmp_path / "book.jsonl"
    monkeypatch.setattr(
        papertrade, "_path", lambda config=None, account=None: path
    )
    monkeypatch.setattr(papertrade_run, "_market_open", lambda t, n: True)
    return path


def signal(ticker, confidence=0.9):
    return {"ticker": ticker, "direction": "long", "confidence": confidence,
            "size": 0.5}


def test_the_book_does_not_open_inside_the_opening_window(book, monkeypatch):
    """M378: the book entering at the bell while the account waits.

    Guarding only the follower makes the two enter at different prices, and
    the gap between them then measures the timing difference rather than how
    wrong the simulation is.
    """
    monkeypatch.setattr(papertrade_run, "_past_the_open", lambda t, n, d: False)

    result = papertrade_run.run_paper_pass(
        [signal("AAA")], {"AAA": 100.0}, now="2026-08-28T13:31:00Z",
        blackout_days=0, cooldown_days=0, recheck=False,
        open_delay_minutes=30,
    )

    assert result["opened"] == 0, result
    assert result["skipped"].get("within the opening window") == 1, result["skipped"]


def test_the_book_opens_once_the_window_has_passed(book, monkeypatch):
    monkeypatch.setattr(papertrade_run, "_past_the_open", lambda t, n, d: True)

    result = papertrade_run.run_paper_pass(
        [signal("AAA")], {"AAA": 100.0}, now="2026-08-28T14:01:00Z",
        blackout_days=0, cooldown_days=0, recheck=False,
        open_delay_minutes=30,
    )
    assert result["opened"] == 1, result


def test_a_close_is_never_delayed_by_the_window(book, monkeypatch):
    """M379: unable to leave because the bell rang recently.

    Being stuck in a position is a worse failure than entering badly, and
    the closes are handled before the guard is reached.
    """
    monkeypatch.setattr(papertrade_run, "_past_the_open", lambda t, n, d: True)
    papertrade_run.run_paper_pass(
        [signal("AAA")], {"AAA": 100.0}, now="2026-08-28T14:01:00Z",
        blackout_days=0, cooldown_days=0, recheck=False, open_delay_minutes=30,
    )
    assert len(papertrade.open_positions(papertrade.load_book())) == 1

    # Next day, inside the window, signal reversed: it must still close.
    monkeypatch.setattr(papertrade_run, "_past_the_open", lambda t, n, d: False)
    result = papertrade_run.run_paper_pass(
        [{"ticker": "AAA", "direction": "short", "confidence": 0.9, "size": 0.5}],
        {"AAA": 100.0}, now="2026-08-29T13:31:00Z",
        blackout_days=0, cooldown_days=0, recheck=False, open_delay_minutes=30,
    )

    assert result["closed"] == 1, (
        f"a close was blocked by the opening window: {result}"
    )


def test_a_cold_start_is_staggered(book, monkeypatch):
    """M380: the whole list in one pass.

    Measured live on 2026-08-28: a book restarted from flat opened twelve
    positions between 13:31 and 13:47, at the widest spreads of the day.
    """
    monkeypatch.setattr(papertrade_run, "_past_the_open", lambda t, n, d: True)

    result = papertrade_run.run_paper_pass(
        [signal(f"T{n}", confidence=0.5 + n / 100) for n in range(12)],
        {f"T{n}": 100.0 for n in range(12)},
        now="2026-08-28T14:01:00Z", blackout_days=0, cooldown_days=0,
        recheck=False, open_delay_minutes=30, max_opens_per_pass=3,
        threshold=0.35,
    )

    assert result["opened"] == 3, result
    assert result["skipped"].get("deferred to a later pass") == 9, result["skipped"]
    assert len(papertrade.load_book()) == 3, "more rows were written than opened"


def test_the_opens_that_go_through_are_the_highest_conviction(book, monkeypatch):
    """M381: which three, chosen by whatever order the dict happened to hold."""
    monkeypatch.setattr(papertrade_run, "_past_the_open", lambda t, n, d: True)

    rows = [signal("LOW", 0.36), signal("MID", 0.55),
            signal("HIGH", 0.95), signal("ALSO_LOW", 0.40)]
    papertrade_run.run_paper_pass(
        rows, {r["ticker"]: 100.0 for r in rows},
        now="2026-08-28T14:01:00Z", blackout_days=0, cooldown_days=0,
        recheck=False, open_delay_minutes=30, max_opens_per_pass=2,
        threshold=0.35,
    )

    opened = set(papertrade.open_positions(papertrade.load_book()))
    assert opened == {"HIGH", "MID"}, opened


def test_an_unreadable_calendar_does_not_permit_an_open(monkeypatch):
    """M382: not knowing treated as permission.

    The whole job of this guard is to refuse, so it fails closed. A
    calendar that cannot be read means the answer is unknown, and unknown
    is not a yes.
    """
    def explode(*args, **kwargs):
        raise OSError("no calendar")

    monkeypatch.setattr(
        "cfdinvest.schedule.scheduler.any_open", explode
    )
    assert papertrade_run._past_the_open(
        "AAA", pd.Timestamp("2026-08-28T14:01:00Z"), 30
    ) is False


def test_no_delay_configured_opens_immediately():
    """Absent must mean no guard, so every recorded run is unchanged."""
    for absent in (0, None):
        assert papertrade_run._past_the_open(
            "AAA", pd.Timestamp("2026-08-28T13:31:00Z"), absent
        ) is True


def test_the_book_and_the_account_read_the_same_settings():
    """One rule, two books. The point of the whole arrangement.

    They are separate code paths -- the book decides and the bridge mirrors
    -- so nothing but a test stops them drifting apart on the settings that
    decide *when* each of them may act.
    """
    import inspect

    from cfdinvest.config import load_config
    from cfdinvest.schedule import app

    settings = load_config()
    assert settings.strategy.open_delay_minutes > 0
    assert settings.strategy.max_opens_per_pass is not None

    book_call = inspect.getsource(app._command_paper_book)
    assert "open_delay_minutes=strategy.open_delay_minutes" in book_call
    assert "max_opens_per_pass=strategy.max_opens_per_pass" in book_call

    account_call = inspect.getsource(app._command_trade_capital)
    assert "open_allowed=open_allowed" in account_call
    assert "max_opens_per_pass=app_config.strategy.max_opens_per_pass" in account_call
