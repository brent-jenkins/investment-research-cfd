"""The last look records what it reads, whether or not it decides on it.

**RENAMED 2026-09-24.** This was `test_the_last_look_is_off_and_still_
watching.py`, which described a state that lasted one session. The property
it actually guards outlives that: the reading is taken and written to the
row in BOTH modes, so the check can always be judged from the record rather
than by re-running it. A filename describing a setting rather than a
property is one that goes stale the day the setting moves.

**The history, because the figures below are read in its light.** Switched
OFF 2026-09-23 on the evidence in the next paragraph; the cause was
diagnosed and fixed on 2026-09-24 (`SignalConfig.breakout_band_lag`); and
re-armed the same evening on the owner's decision, to get a session's data
before the weekend. The fix is NOT yet verified on real data, so Friday
tests the fix and the re-entry churn at once.

WHY IT WAS SWITCHED OFF. Over 727 refusals carrying a live reading, across
three sessions, the check agreed with the engine everywhere except where it
decides anything:

    engine band      n    live     gap
    <0.40           69   0.393  +0.006
    0.40-0.60      397   0.455  -0.000
    0.60-0.75       23   0.667   0.000
    >=0.75         180   0.448  -0.348

A third, at and above the gate, on n=180. That is the 2026-09-10
provisional-bar signature -- -0.324 that day on n=2, +0.001 after its fix --
and the check only ever runs on names the engine wants to OPEN, which are
>=0.75 by construction. So it is suspect across its whole domain of
application. Split by entry age the chases agree perfectly (live/engine
1.000) and fresh entries read 0.567 of the engine, which says the same thing
from a second direction.

THE MEASURED CASE FOR THE CHECK IS REAL AND IS NOT DISMISSED: refusing on a
live quote removed 197 of 2,139 round trips and those refused won 36.0%
against 50.4% for the ones kept, t=+4.18. That was 2026-08-27, on a 0.35
gate, before the feed switch and the corrected cost model -- and on a scale
the check was later shown unable to reach at 0.75.

DEFAULT TRUE. A safety check that is off unless somebody remembers to switch
it on is not a safety check. Turning it off is the deliberate act and it
lives in config.toml, the way live dealing needs its own variable.

REPORTED, NOT ENFORCED -- and this is the part that must not be lost. The
reading is still taken and still written to the row. Switching the check off
must not also switch off the evidence that says whether switching it off was
right; that is the trap this project declined when it refused to silence the
stale-intent loop, because "the noise it removes is the only thing making the
divergence visible".

ONE THING IT DOES FOR FREE: the stale-intent loop goes with it, without a
second rule. A chase exists BECAUSE the open was refused, so a name that
opens on its first pass is never chased. `tests/test_stale_intent_is_visible.py`
therefore still holds -- nothing started enforcing an age.

AND IT ALIGNS THE ENGINE WITH THE ACCOUNT. `ExecutionConfig.entry_recheck`
defaults False and is not wired into `_execution_config`, so with the live
check ON the backtest modelled a system one filter short of the one that
dealt. With it off, they agree for the first time.

Mutations checked:

  M1792 `recheck=True` hard-coded again, so the flag decides nothing
  M1793 the reading dropped when the check is off, coupling the measurement
        to the enforcement
  M1794 `last_look` defaulting to False, so a safety check is off unless
        somebody switches it on
  M1795 the reading taken but the bar not recorded with it, so a future
        gate change makes every row unreadable
"""

from __future__ import annotations

import inspect

import pytest

from cfdinvest.config import StrategyConfig, load_config
from cfdinvest.schedule import app as schedule_app

from tests.test_broker_capital_bridge import _recheck, only, plan, signal


# --------------------------------------------------------------------- #
# The default is safe; the deployment opts out.
# --------------------------------------------------------------------- #
def test_the_last_look_defaults_to_on() -> None:
    """M1794: a safety check that is off unless switched on.

    Every other gate in this project fails closed. This one is being
    switched off deliberately and on evidence, which is a different thing
    from defaulting off.
    """
    assert StrategyConfig().last_look is True, (
        "the last look now defaults OFF, so any configuration that does not "
        "mention it silently deals without a re-check"
    )


def test_the_shipped_value_is_stated_rather_than_drifting() -> None:
    """M1796: the shipped setting flipped without anybody saying so.

    This asserted False between 2026-09-23 and 2026-09-24, when the check
    was disarmed on measured evidence. It asserts True now: the cause of
    that evidence was found -- the live panel's Donchian band included the
    decision bar's own high, so a name that had just broken out read a
    breakout of zero on an UNCHANGED price -- and fixed by
    `SignalConfig.breakout_band_lag`.

    Pinned either way so a silent flip fails the build. What the account
    re-checks before placing an order is not something that should move in
    a diff nobody reads.
    """
    assert load_config().strategy.last_look is True, (
        "config.toml no longer arms the last look. It was re-armed on "
        "2026-09-24 once the breakout band lag was fixed; turning it off "
        "again is a decision about what the account trades and needs to be "
        "made deliberately, not inherited"
    )


def test_the_dealing_pass_reads_the_flag_rather_than_hard_coding_it() -> None:
    """M1792: `recheck=True` restored.

    Anchored on an UNCOMMENTED line that both names the setting and feeds
    `recheck`. `"last_look" in source` would pass against the words sitting
    in a comment above a hard-coded True -- which is exactly how M1762
    survived against `true # market-open`.
    """
    source = inspect.getsource(schedule_app._command_trade_capital)

    live = [
        line for line in source.splitlines()
        if "recheck=" in line and not line.lstrip().startswith("#")
    ]
    assert live, "the dealing pass no longer passes recheck at all"
    # The literal assignment, not "True appears somewhere on the line" --
    # `getattr(..., "last_look", True)` carries a legitimate True as its
    # fallback, and a test that cannot tell those apart fails on the fix.
    hard_coded = [
        line for line in live
        if line.replace(" ", "").startswith(("recheck=True", "recheck=False"))
    ]
    assert not hard_coded, (
        f"recheck is hard-coded: {hard_coded!r}. The configuration flag "
        f"then decides nothing, whatever config.toml says"
    )
    assert any("last_look" in line for line in live), (
        "recheck is passed from something other than the last_look setting"
    )


# --------------------------------------------------------------------- #
# Off means it decides nothing -- and still reports.
# --------------------------------------------------------------------- #
def test_with_the_check_off_a_refused_reading_no_longer_refuses() -> None:
    """The behaviour change itself, through the real `plan_orders`.

    A live reading of 0.1 against a 0.75 bar would have been refused. With
    the check off it opens.
    """
    blocked = plan(
        [signal("AAPL")], recheck=True, rechecked=_recheck(confidence=0.1),
        entry_confidence=0.75,
    )
    assert only(blocked, "AAPL").action == "refused"

    allowed = plan(
        [signal("AAPL")], recheck=False, rechecked=_recheck(confidence=0.1),
        entry_confidence=0.75,
    )
    assert only(allowed, "AAPL").action == "open", (
        "the last look is switched off and still refused the open"
    )


def test_the_reading_is_still_recorded_when_it_decides_nothing() -> None:
    """M1793: the measurement switched off with the enforcement.

    This is the whole reason the change is safe to make. Without the reading
    there is no way to tell later whether the check was broken, and the
    decision to switch it off becomes unfalsifiable -- which is the shape
    this project refused when it declined to silence the stale-intent loop.
    """
    report = plan(
        [signal("AAPL")], recheck=False, rechecked=_recheck(confidence=0.42),
        entry_confidence=0.75,
    )
    intent = only(report, "AAPL")

    assert intent.action == "open"
    assert intent.live_confidence == pytest.approx(0.42), (
        "the last look was switched off and its reading went with it, so "
        "nothing can say afterwards whether switching it off was right"
    )


def test_the_recorded_reading_still_carries_its_bar() -> None:
    """M1795: the reading kept and the threshold dropped.

    A reading without its threshold cannot be read later. That is how 18.7%
    and 20.7% came to be quoted against a 0.75 gate they predate, four times
    in one day, and it is why the bar is on the row at all.
    """
    report = plan(
        [signal("AAPL")], recheck=False, rechecked=_recheck(confidence=0.42),
        entry_confidence=0.75,
    )
    intent = only(report, "AAPL")

    assert intent.confidence_bar == pytest.approx(0.75)


def test_no_readings_supplied_is_not_an_invented_zero() -> None:
    """Absence named, with the check off as well as on."""
    report = plan([signal("AAPL")], recheck=False, rechecked=None)
    intent = only(report, "AAPL")

    assert intent.action == "open"
    assert intent.live_confidence is None, (
        "nothing re-checked this and a number was recorded anyway"
    )
