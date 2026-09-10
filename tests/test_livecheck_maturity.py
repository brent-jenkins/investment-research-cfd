"""The last look must decline while the session is too young to check.

It extends the lake with a PROVISIONAL bar built from one quote -- open,
high, low and close all the same number, so its range is zero by
construction. Mid-session that sits behind well-formed bars and costs
little: measured on 2026-09-09, 52 minutes into a bar, the live reading and
the engine's agreed to three decimals on five of six names.

At the open it inverts. On 2026-09-10, six minutes after the bell:

    name   engine's 13:00 bar   live last look   outcome
    TGT           0.463             >= 0.75      OPENED, lost money
    BLK           0.806              < 0.75      refused

Wrong in both directions on one pass. The cause is not the provisional bar
alone -- the bar BEHIND it held six minutes of trading, so every range-based
input was reading noise.

That matters more than it did, because the entry being adjudicated is
eighteen hours old. `execution_lag` is a POSITIONAL shift, so across the
overnight gap the position on today's opening bar comes from yesterday's
19:00 signal. The last look is the only thing between that and the account,
and at the bell it cannot do the job.

So it declines instead of answering. `holds` already refuses an instrument
it has no reading for, so this fails closed with no new vocabulary.

Mutations checked, each reintroduced as a bug and confirmed to fail:
  M500 the maturity guard removed, so the bell is checked as usual
  M510 an unreadable calendar treated as mature rather than declining
  M520 the guard declining a mature session too, refusing everything always
  M530 declined instruments still returned with a reading
"""

from __future__ import annotations

import pandas as pd
import pytest

from cfdinvest.advisor import livecheck
from cfdinvest.advisor.livecheck import MIN_SESSION_MINUTES, _too_early, holds

NAMES = ["TGT", "BLK"]
BELL = pd.Timestamp("2026-09-10 13:30:00+00:00")      # the US open


def test_the_case_that_opened_TGT_is_declined():
    """M500: six minutes after the bell, which is when it went wrong."""
    declined = _too_early(NAMES, BELL + pd.Timedelta(minutes=6), None)

    assert set(declined) == set(NAMES), declined


def test_a_mature_session_is_checked_normally():
    """M520: a guard that declines everything is a gate that never opens.

    Without this the test above passes on `return set(names)`, and the
    account would stop trading entirely while looking well protected.
    """
    declined = _too_early(NAMES, BELL + pd.Timedelta(minutes=142), None)

    assert declined == set(), declined


def test_the_boundary_is_the_configured_threshold():
    """Read from the constant, not restated, so moving it moves the test."""
    just_before = _too_early(
        NAMES, BELL + pd.Timedelta(minutes=MIN_SESSION_MINUTES - 5), None)
    just_after = _too_early(
        NAMES, BELL + pd.Timedelta(minutes=MIN_SESSION_MINUTES + 5), None)

    assert set(just_before) == set(NAMES), "inside the window and not declined"
    assert just_after == set(), "past the window and still declined"


def test_an_unreadable_calendar_declines_everything(monkeypatch):
    """M510: absence named, in the direction that costs money to get wrong.

    Not being able to tell how long a market has been open is not a reason
    to trust a reading that depends on it. Assuming mature is the inert-gate
    failure this project keeps finding.
    """
    from cfdinvest.schedule import market_calendar

    def broken(*_args, **_kwargs):
        raise RuntimeError("calendar unavailable, for the test")

    monkeypatch.setattr(market_calendar, "describe", broken)
    declined = _too_early(NAMES, BELL + pd.Timedelta(hours=3), None)

    assert set(declined) == set(NAMES), (
        "an unreadable calendar must decline, not wave through"
    )


def test_a_closed_market_is_not_reported_as_immature():
    """Overnight the market is shut, which is a different statement.

    `open_allowed` and the session calendar already refuse out-of-session
    dealing. Reporting a shut market as "too early" would put the wrong
    reason in the record, and the refusal reasons are the only evidence of
    the limits working.
    """
    overnight = pd.Timestamp("2026-09-10 03:00:00+00:00")
    assert _too_early(NAMES, overnight, None) == set()


def test_a_declined_instrument_is_refused_by_holds():
    """M530: the whole chain, end to end.

    Declining is only useful if the caller refuses. `holds` turns a missing
    reading into "not re-checked", which the bridge records by name -- so a
    trade blocked this way is visible rather than silent.
    """
    ok, why = holds({}, "TGT", "short", 0.75)

    assert ok is False
    assert why == "not re-checked"


def test_reevaluate_consults_the_guard_rather_than_only_defining_it():
    """An unused guard is the inert-gate failure with extra steps.

    STRUCTURAL, and said so plainly. reevaluate builds a repository, an
    analysis frame, regimes and signals before it reaches the filter, so
    exercising it end to end needs a lake. Reading the source is the weaker
    check; it is here because the two mutations it catches -- the call
    commented out, and the filter dropped from the loop -- both survived
    every behavioural test in this file.
    """
    import inspect

    source = inspect.getsource(livecheck.reevaluate)

    # Asserted on the exact call and the exact use, not on the identifier
    # appearing somewhere. Both looser forms survived their mutations: a
    # commented-out call still contains the name, and dropping the filter
    # from the loop leaves the call above it untouched.
    assert "immature = _too_early(" in source, (
        "reevaluate does not call the maturity guard, so it is defined and "
        "never applied"
    )
    assert "ticker in immature" in source, (
        "reevaluate computes which instruments to decline and then returns "
        "readings for them anyway"
    )
    assert source.index("immature = _too_early(") < source.index("out[ticker]"), (
        "the guard runs after the readings are built, which is too late to "
        "withhold them"
    )
