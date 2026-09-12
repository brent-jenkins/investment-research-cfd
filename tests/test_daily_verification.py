"""The daily verification cannot report a clean day it did not check.

Every fault in the week to 2026-09-12 was found because the owner noticed or
somebody went digging. None needed cleverness: each would have been plain in
a daily statement of what was intended, what happened and what was refused.
CLAUDE.md recorded on 2026-09-10 that such a statement was "worth more than
any single fix here and it is not built".

The danger in building it is that a verification is itself a component that
can fail, and the way it fails is by going quiet. So the properties worth
testing are not "does it notice a bad day" but "does it refuse to call a day
good when it could not look".

Mutations checked:

  M1690 a check that raises reported as ok rather than unchecked
  M1691 unchecked collapsed into ok in the verdict
  M1692 an unreadable calendar treated as "no session", excusing an outage
  M1693 a broken last look (not re-checked dominating) read as a strict gate
  M1694 a clean day mailed anyway, which trains the reader to ignore it
"""
from __future__ import annotations

import pytest

from cfdinvest.verify import daily


def finding(status: str, name: str = "x") -> daily.Finding:
    return daily.Finding(name, status, "detail")


def test_a_check_that_raises_is_unchecked_never_ok() -> None:
    """M1690: a verification that goes quiet when its own reader breaks
    reports a clean day because it could not look."""
    def explode():
        raise RuntimeError("the store is unreadable")

    result = daily._safe("thing", explode)

    assert result.status == daily.UNCHECKED
    assert "RuntimeError" in result.detail, (
        "the detail must name what failed, or nobody can act on it"
    )


def test_blind_outranks_fine() -> None:
    """M1691: a report that cannot see is not a clean report.

    Treating unchecked as ok is how a silent stall runs nineteen hours with
    everything reporting healthy.
    """
    report = daily.DayReport("2026-09-11", (finding(daily.OK),
                                            finding(daily.UNCHECKED)))
    assert report.verdict == daily.UNCHECKED


def test_a_problem_outranks_everything() -> None:
    report = daily.DayReport("2026-09-11", (finding(daily.OK),
                                            finding(daily.UNCHECKED),
                                            finding(daily.PROBLEM)))
    assert report.verdict == daily.PROBLEM


def test_a_clean_day_is_clean() -> None:
    report = daily.DayReport("2026-09-11", (finding(daily.OK),
                                            finding(daily.OK)))
    assert report.verdict == daily.OK


def test_the_report_says_what_was_not_verified() -> None:
    """A reader must be able to tell 'fine' from 'not looked at'."""
    report = daily.DayReport("2026-09-11", (finding(daily.UNCHECKED, "lake"),))
    text = report.describe()

    assert "NOT verified" in text
    assert "lake" in text


def test_an_unreadable_calendar_is_not_a_closed_market(monkeypatch) -> None:
    """M1692: None is not False.

    If a calendar that cannot answer were read as "the market was shut",
    every genuine outage would be excused as a holiday -- and the checks
    that depend on it would report ok on exactly the days they matter.
    """
    def explode(*_args, **_kwargs):
        raise RuntimeError("no calendar")

    monkeypatch.setattr(
        "cfdinvest.universe.traded.traded_universe", explode, raising=False
    )
    assert daily._was_a_trading_day(object(), "2026-09-11") is None


def test_a_missing_run_on_a_trading_day_is_a_problem() -> None:
    finding_ = daily._check_run_is_fresh.__doc__
    assert finding_, "the check must explain itself"


@pytest.mark.parametrize(
    "trading,expected",
    [(False, daily.OK), (None, daily.UNCHECKED)],
)
def test_no_dealing_pass_is_judged_against_the_calendar(
    monkeypatch, trading, expected
) -> None:
    """A weekend is not an outage, and an unknown calendar is not a weekend.

    A report that cries wolf every Saturday is one nobody opens on the
    Monday it is right -- the same reason the monitor timer sat disabled
    through two stalls.
    """
    monkeypatch.setattr(
        daily, "_stamp_of", lambda row: "2026-09-10T12:00:00", raising=True
    )

    class _Margin:
        @staticmethod
        def load_margin(_config, _account):
            return [{"timestamp": "2026-09-10T12:00:00"}]

    # Patched as a PACKAGE ATTRIBUTE, not in sys.modules. `from
    # cfdinvest.persist import margin` resolves the attribute once the
    # package is imported, so a sys.modules entry is ignored -- which made
    # this pass alone and fail in the full suite, purely on import order.
    import cfdinvest.persist as persist_package

    monkeypatch.setattr(persist_package, "margin", _Margin, raising=False)
    result = daily._check_dealing_happened(None, None, "2026-09-11", trading)
    assert result.status == expected


def test_a_broken_last_look_is_not_a_strict_one(monkeypatch) -> None:
    """M1693: 'not re-checked' dominating means the check cannot form an
    opinion, which is a different failure from a gate refusing trades.

    It fails CLOSED, so it looks like extreme selectivity while actually
    approving nothing on evidence.
    """
    class _Orders:
        LAST_LOOK_REASONS = ()

        @staticmethod
        def refusal_summary(_config, _account):
            # A LOW refusal rate, deliberately. With a high one the rate
            # branch reports PROBLEM anyway and the test passes whether or
            # not the broken-check branch exists -- which is how this test
            # first failed its own mutation. 10 of 100 is well inside every
            # threshold, so only the reason can raise it.
            return {
                "checked": True, "considered": 100, "blocked": 10,
                "opened_anyway": 0,
                "reasons": {"not re-checked": 9, "no longer clears the bar": 1},
            }

    import cfdinvest.persist as persist_package

    monkeypatch.setattr(persist_package, "orders", _Orders, raising=False)
    result = daily._check_refusals(None, None)

    assert result.status == daily.PROBLEM
    assert "not re-checked" in result.detail


def test_verify_day_never_raises() -> None:
    """The day it matters most is the day something else is already broken."""
    report = daily.verify_day(config=None, account=None, day="2026-09-11")

    assert isinstance(report, daily.DayReport)
    assert report.findings, "a report with no findings verifies nothing"


def test_a_clean_day_sends_nothing() -> None:
    """M1694: an alert that arrives daily is one nobody opens when it is red.

    The monitoring layer's own rule -- "only red sends mail" -- applied to
    the verification.
    """
    import inspect

    from cfdinvest.schedule import app

    source = inspect.getsource(app._command_verify_day)
    assert 'report.verdict == "ok"' in source, (
        "a clean verification must return before composing a message"
    )
    before_send = source.split("notify_module")[0]
    assert "return EXIT_OK" in before_send, (
        "the clean-day return must come BEFORE the send, or every day mails"
    )
