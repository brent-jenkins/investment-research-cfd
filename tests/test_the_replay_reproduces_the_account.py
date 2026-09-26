"""The harness is anchored to the account, and the anchor cannot be faked.

`research.replay` checks that the harness makes the live engine's own entry
decisions, bar for bar. That is only worth having if each part of the
comparison means what it says: an entry is read off the order store the way
the live engine meant it, the classification cannot turn a disagreement
into agreement, the universe on a day is the one the daemon traded rather
than one chosen later, and the research config stops the engine taking the
opening-bar trade the account never can.

Mutations checked:

  M1830 approved opens without an age dropped from the live entries
  M1831 clock refusals counted as live entries
  M1832 a chase recognised from a LATER harness entry, not an earlier one
  M1833 not-intended harness entries tolerated rather than refused
  M1834 an empty live side passes
  M1835 a selection recorded after the cut hour used that same day
  M1836 segments merged by equal universe rather than consecutive days
  M1837 the research config no longer refuses the opening bar
  M1838 the segment ends on its own last day, so that 19:00 bar is the
        frame's final bar and escapes the closing window
  M1839 an override the engine has no field for dropped silently
  M1843 the research config scaling the book at the gross ceiling
  M1844 the replay checking the engine under the account's selecting cap,
        which the live intent run does not use
  M1845 the history screening at a target other than the shipped one, or
        asking for a rebalance date past the window
"""
from __future__ import annotations

import dataclasses

import pandas as pd
import pytest

from cfdinvest.research import replay

T = lambda s: pd.Timestamp(s, tz="UTC")  # noqa: E731


def row(at, ticker="AAA", direction="BUY", action="refused", reason="",
        age=0.0, kind="plan"):
    return {"at": at, "ticker": ticker, "direction": direction, "action": action,
            "reason": reason, "entered_bars_ago": age, "kind": kind}


# -- reading the live side -------------------------------------------------- #

def test_the_decision_bar_is_the_hour_before_the_pass() -> None:
    entries, _ = replay.live_engine_entries([row("2026-09-24T14:05:00Z")])
    assert entries == {(T("2026-09-24 13:00"), "AAA", 1)}


def test_an_approved_open_is_an_entry_even_without_an_age() -> None:
    """M1830: approvals carried no age before 2026-09-23.

    Reading that absence as "not fresh" dropped every open before the date,
    which is how the first replay under-counted agreement.
    """
    entries, _ = replay.live_engine_entries(
        [row("2026-09-21T14:05:00Z", action="open", age=None),
         row("2026-09-21T15:05:00Z", ticker="BBB", action="open", age=float("nan"))]
    )
    assert {e[1] for e in entries} == {"AAA", "BBB"}


def test_a_chase_refusal_is_not_an_entry_but_is_an_intent() -> None:
    entries, intents = replay.live_engine_entries(
        [row("2026-09-24T16:05:00Z", age=3.0, reason="no longer clears the bar")]
    )
    assert entries == set()
    assert intents == {(T("2026-09-24 15:00"), "AAA", 1)}


def test_a_clock_refusal_is_not_an_entry() -> None:
    """M1831: the engine models both windows itself."""
    entries, intents = replay.live_engine_entries(
        [row("2026-09-24T19:05:00Z", reason="too near the session close to open"),
         row("2026-09-24T13:35:00Z", ticker="BBB", reason="within the opening window")]
    )
    assert entries == set()
    assert len(intents) == 2


# -- classifying ------------------------------------------------------------ #

def test_a_late_live_open_is_a_chase_only_if_the_harness_entered_earlier() -> None:
    """M1832: a harness entry AFTER the live one explains nothing."""
    live = {(T("2026-09-24 15:00"), "AAA", 1)}
    earlier = {(T("2026-09-24 13:00"), "AAA", 1)}
    later = {(T("2026-09-24 16:00"), "AAA", 1)}
    intents = live | earlier | later

    assert replay.agreement(live, earlier, intents).chases == frozenset(live)
    assert replay.agreement(live, later, intents).unexplained == frozenset(live)


def test_a_harness_entry_the_live_engine_never_intended_is_refused() -> None:
    """M1833: the one divergence with no tolerance at all."""
    live = {(T("2026-09-24 13:00"), "AAA", 1)}
    rogue = (T("2026-09-24 13:00"), "ZZZ", -1)
    report = replay.agreement(live, live | {rogue}, set(live))

    assert report.not_intended == frozenset({rogue})
    with pytest.raises(replay.ReplayError, match="never intended"):
        replay.assert_reproduces_account(report)


def test_too_many_unexplained_live_entries_are_refused() -> None:
    live = {(T("2026-09-24 13:00"), f"N{i}", 1) for i in range(20)}
    harness_side = set(sorted(live)[:18])
    report = replay.agreement(live, harness_side, live)

    assert report.unexplained_share == pytest.approx(0.10)
    with pytest.raises(replay.ReplayError, match="unexplained"):
        replay.assert_reproduces_account(report)
    replay.assert_reproduces_account(report, max_unexplained_share=0.10)


def test_agreement_with_nothing_is_not_agreement() -> None:
    """M1834: a window with no sessions in it would otherwise pass."""
    with pytest.raises(replay.ReplayError, match="nothing was compared"):
        replay.assert_reproduces_account(replay.agreement(set(), set(), set()))


# -- the universe in force -------------------------------------------------- #

SELECTIONS = [
    ("2026-09-21T03:12:00Z", ["AAA", "BBB"]),
    ("2026-09-22T03:12:00Z", ["AAA", "CCC"]),
    ("2026-09-22T15:00:00Z", ["ZZZ"]),          # after the cut: tomorrow's
    ("2026-09-24T03:12:00Z", ["AAA", "BBB"]),
]


def test_a_selection_recorded_after_the_cut_belongs_to_a_later_day() -> None:
    """M1835: using it the same day trades names chosen with hindsight."""
    assert replay.selection_in_force(SELECTIONS, T("2026-09-22")) == ("AAA", "CCC")
    assert replay.selection_in_force(SELECTIONS, T("2026-09-23")) == ("ZZZ",)
    assert replay.selection_in_force(SELECTIONS, T("2026-09-20")) is None


def test_segments_are_consecutive_days_not_equal_universes() -> None:
    """M1836: a list that returns after a change is a new segment.

    The regime between is computed on another list, so merging the two
    would run the middle days' state through the wrong universe.
    """
    days = pd.date_range("2026-09-21", "2026-09-24", freq="B", tz="UTC")
    segments = replay.selection_segments(SELECTIONS, days)

    assert [u for u, _ in segments] == [("AAA", "BBB"), ("AAA", "CCC"), ("ZZZ",),
                                        ("AAA", "BBB")]


# -- the research config ---------------------------------------------------- #

@pytest.fixture(scope="module")
def shipped():
    from cfdinvest.config import load_config

    return load_config()


def test_the_research_config_refuses_the_opening_bar(shipped) -> None:
    """M1837: the live dealer never fills on a session's first bar.

    Asserted on the config the function returns, not on its source text --
    a source check passed twice on 2026-09-19 against code that did not run.
    """
    from cfdinvest.research import harness

    config = harness.research_backtest(shipped, tickers=["AAA"])
    assert config.execution.open_delay_minutes >= 1
    assert config.execution.open_delay_minutes <= 60, (
        "a delay past one hour refuses the second bar as well, which live takes"
    )

    explicit = harness.research_backtest(shipped, tickers=["AAA"], open_delay_minutes=0)
    assert explicit.execution.open_delay_minutes == 0, "an explicit override must win"


def test_the_research_config_selects_at_the_gross_ceiling(shipped) -> None:
    """M1843: scaling rescaled every held position on every bar the cap bound.

    The dealer refuses the weakest new opens at the ceiling and never trims
    a held position to fund one. On 2026-09-22 the scaling engine made 61
    resizes against 4 once it selected, and the account made none.
    """
    from cfdinvest.research import harness

    assert harness.research_backtest(shipped, tickers=["AAA"]).execution.leverage_cap_selects is True
    assert harness.research_backtest(
        shipped, tickers=["AAA"], leverage_cap_selects=False
    ).execution.leverage_cap_selects is False, "an explicit override must win"


def test_the_replay_checks_the_engine_as_the_live_run_configures_it(shipped) -> None:
    """M1844: the replay run with the harness's selecting cap.

    The live intent run scales at the gross ceiling; replaying with the
    account's selecting rule dropped 33 of 88 live decisions on 2026-09-26.
    The replay checks the ENGINE, so it must configure the cap the engine's
    live run uses -- which is whatever `_execution_config` ships.
    """
    from cfdinvest.schedule import app

    class Args:
        interval = start = end = universe = account = tickers = None

    live = app._backtest_config(Args(), shipped).execution
    for field, value in replay.DECISION_OVERRIDES.items():
        assert getattr(live, field) == value, (
            f"the replay sets {field}={value!r} but the live run ships "
            f"{getattr(live, field)!r}; it would be checking a different engine"
        )
    config = replay.segment_config(shipped, ["AAA"], [T("2026-09-22")],
                                   **replay.DECISION_OVERRIDES)
    assert config.execution.leverage_cap_selects is False


def test_an_override_the_engine_does_not_have_is_refused(shipped) -> None:
    """M1839: a misspelled override made an arm identical to its baseline.

    On 2026-09-26 `decayed_multiplier` was passed for
    `decayed_position_multiplier`, silently dropped, and the "trim off" arm
    matched the shipped one to the trade -- the inert-arm failure, which
    reads as a finding that the rule does nothing.
    """
    from cfdinvest.research import harness

    with pytest.raises(TypeError, match="decayed_multiplier"):
        harness.research_backtest(shipped, tickers=["AAA"], decayed_multiplier=1.0)
    config = harness.research_backtest(shipped, tickers=["AAA"],
                                       decayed_position_multiplier=None)
    assert config.execution.decayed_position_multiplier is None


def test_a_segment_runs_past_its_own_last_day(shipped) -> None:
    """M1838: the engine exempts a frame's final bar from the closing window.

    Ending a segment on its own last day made that day's 19:00 bar the final
    bar, and three entries the account could never take appeared in the
    first replay.
    """
    days = [T("2026-09-22"), T("2026-09-23")]
    config = replay.segment_config(shipped, ["AAA"], days)

    assert pd.Timestamp(config.end) > days[-1] + pd.Timedelta(hours=19)
    assert pd.Timestamp(config.start) <= days[0] - pd.DateOffset(months=6)
    assert config.regime.macro_on_own_calendar is True, (
        "the live window needs the monthly macro calendar, or every macro "
        "label reads unknown"
    )


def test_history_uses_the_shipped_screen_and_only_past_selections(shipped, monkeypatch) -> None:
    """M1845: the history run must screen as the daemon does, and never ahead.

    The screen is stubbed so the test needs no lake; what is asserted is what
    the history asks it for -- the shipped cap and target -- and that no
    rebalance date lies after the window, which would hand an early segment a
    selection made later.
    """
    from cfdinvest.research import harness
    from cfdinvest.universe.screen import effective_sector_cap

    asked = {}

    def fake(repository, app_config, dates, *, cap, target, account=None):
        asked.update(cap=cap, target=target, dates=list(dates))
        return {d: ["AAA", "BBB"] for d in asked["dates"]}

    monkeypatch.setattr(harness, "screen_selections", fake)
    segments = replay.history_segments(None, shipped, "2026-09-07", "2026-09-18")

    assert asked["cap"] == effective_sector_cap(shipped)
    assert asked["target"] == int(shipped.universe.max_target)
    assert max(asked["dates"]) <= T("2026-09-18")
    assert sum(len(days) for _, days in segments) == 10


def test_a_summary_of_nothing_is_absent_not_zero() -> None:
    """Absence is named: no returns is not a flat record."""
    assert replay.summarise(pd.Series(dtype="float64")) == {}
    figures = replay.summarise(pd.Series(
        [0.01, -0.02, 0.01], index=pd.date_range("2026-09-21", periods=3, tz="UTC")))
    assert figures["days"] == 3 and figures["maxdd"] < 0


def test_entries_are_read_at_the_decision_bar() -> None:
    """At the research lag of 1 the fill is the bar after the decision."""
    index = pd.DatetimeIndex([T("2026-09-24 13:00"), T("2026-09-24 14:00"),
                              T("2026-09-24 15:00"), T("2026-09-24 16:00")])
    positions = pd.DataFrame({"AAA": [0.0, 5.0, 5.0, -2.0]}, index=index)

    found = replay.entries_from_positions(positions, [T("2026-09-24")])
    assert found == {(T("2026-09-24 13:00"), "AAA", 1),
                     (T("2026-09-24 15:00"), "AAA", -1)}
