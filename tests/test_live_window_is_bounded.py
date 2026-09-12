"""The live decision reads recent bars, and research still reads everything.

This is not a long-term platform. It predicts the next few hours from recent
movement, so a price from 2023 cannot inform the decision -- the owner's
point, made three times before it was acted on. Until 2026-09-12 the live run
was bounded by nothing: ``[market] start = "2005-01-01"`` and no window, so
it read 6,467 bars over 3.69 years and grew by seven bars every session.
3.69 years was never chosen; it is what "no window" evaluates to against this
lake.

Six months was measured before it was taken. On five recent session bars,
comparing the final bar's target as a SHARE OF EQUITY -- what
`persist.intent` actually carries -- against the whole-lake run on the same
bar and the same pinned universe, every window from twelve months down to two
reproduced the decision exactly and one month did not.

**Two months reproduced and is still wrong**, which is the reason the shipped
number comes from the warm-up rather than from the sweep's edge:
``breakdown_baseline_window = 252`` is computed over ``correlation_window =
63``, so 315 bars must exist before the longest input does, and two months is
308. It passed five dates from below its own floor. Six months is 890 bars,
2.8x the warm-up, and buys all of the saving -- 17.4s/867MB becomes
4.9s/565MB, while three months saves a further 0.7s for half the margin.

Two properties are asserted, and they pull in opposite directions on purpose.

  THE LIVE RUN IS BOUNDED, and bounded from the bar being decided on rather
  than from the wall clock, or a run with an explicit ``--end`` would read
  the six months before today instead of before that bar.

  RESEARCH IS NOT. A backtest needs 3.65 years to MEASURE across many
  periods; the dealer needs recent bars to PREDICT the next few hours. Same
  engine, two uses. `_backtest_config` must stay unbounded or every recorded
  figure moves as a side effect of a live change -- which is the shared-config
  failure that has already cost three sessions here.

And one that is easy to miss: the window is only safe BECAUSE the macro
calendar fix ships with it. `_macro_lookback` resolves to `periods_per_year`
= 1,764, so the growth and inflation classifiers do a genuine year-over-year
change counted in PRICE BARS and need about 12.4 months of them. Below that
every macro label reads `unknown`, and `allow_short` permits a short only on
`contraction` or `crisis` with `allow_when_unknown` False. Shortening the
window alone would have silently stopped the book shorting.

Mutations checked:

  M1680 the window dropped, so the live run reads the whole lake again
  M1681 the window measured from the wall clock instead of the decision bar
  M1682 an explicit --start silently overridden by the window
  M1683 `macro_on_own_calendar` left False, so six months blocks every short
  M1684 the window applied to `_backtest_config`, moving every research figure
  M1685 the window set below the 315-bar warm-up
  M1686 the window applied as a flat six months at every interval, which
        starves a daily run -- the suite reported it as "no data to trade"
  M1687 an unparseable interval windowed anyway rather than left unbounded
"""
from __future__ import annotations

import dataclasses

import pandas as pd
import pytest

from cfdinvest.schedule import app as A

#: The longest input the pipeline builds, in bars, and where it comes from.
#: Asserted against the real config objects rather than restated, so a
#: lengthened lookback fails the build instead of silently outgrowing the
#: window that feeds it.
WARM_UP_BARS = 315

#: Session bars in six months of this lake, measured on ABT at the 50-name
#: universe. Used only to check the window clears the warm-up, so a rough
#: figure is the right kind of figure.
BARS_PER_MONTH = 148


class _Args:
    interval = None
    start = end = universe = account = tickers = None


def _app_config():
    from cfdinvest.config import load_config

    return load_config()


def _live(end=None, start=None):
    config = _app_config()
    backtest = A._backtest_config(_Args(), config)
    if end is not None or start is not None:
        backtest = dataclasses.replace(backtest, end=end, start=start)
    return A.for_live_intent(backtest, config), backtest


def test_the_live_run_is_bounded_at_all() -> None:
    """M1680: the run reads the whole lake, growing every session."""
    live, _ = _live()
    assert live.start is not None, (
        "the live decision reads everything the lake holds. That is 6,467 "
        "bars over 3.69 years and grows daily -- and a price from 2023 "
        "cannot inform the next few hours"
    )


def test_the_window_is_measured_from_the_decision_bar() -> None:
    """M1681: six months before TODAY, not before the bar being decided on.

    A research run asking for an old bar would read the six months before
    now -- a window that does not contain its own decision bar, which
    produces an empty book rather than an error.
    """
    end = pd.Timestamp("2026-06-11 19:00", tz="UTC")
    live, _ = _live(end=end)

    span_months = (end.year - live.start.year) * 12 + end.month - live.start.month
    assert span_months == pytest.approx(A.INTENT_WINDOW_MONTHS, abs=1), (
        f"the window runs {live.start} to {end}, which is {span_months} "
        f"month(s), not {A.INTENT_WINDOW_MONTHS}. It must be measured back "
        "from the decision bar, not from the wall clock."
    )
    assert live.start < end, "the window does not contain its own decision bar"


def test_a_stated_research_window_is_never_moved() -> None:
    """M1682: --start silently overridden, so a measurement is not what was asked."""
    asked = pd.Timestamp("2024-01-01", tz="UTC")
    live, _ = _live(end=pd.Timestamp("2026-09-11 19:00", tz="UTC"), start=asked)
    assert pd.Timestamp(live.start) == asked, (
        f"--start was given as {asked} and the run used {live.start}. A "
        "stated window must never be silently moved -- the same contract "
        "--end already has."
    )


def test_the_macro_calendar_fix_ships_with_the_window() -> None:
    """M1683: six months without it reads `unknown` and blocks every short.

    The two are one change. `_macro_lookback` is `periods_per_year` = 1,764
    bars, about 12.4 months, so a six-month panel cannot resolve a
    year-over-year macro label at all -- and `allow_short` reads that label.
    """
    live, _ = _live()
    assert live.regime.macro_on_own_calendar is True, (
        "the live run is bounded at six months with macro still classified "
        "in price bars, so every macro label resolves to `unknown` and "
        "`allow_short` refuses every short. The window and this flag are one "
        "change and neither is safe alone."
    )


def test_research_still_reads_the_whole_span() -> None:
    """M1684: bounding research too, which moves every recorded figure.

    A backtest MEASURES across many periods and needs the span; the dealer
    PREDICTS the next few hours and does not. Only the live path is bounded,
    which is the same split `INTENT_EXECUTION_LAG` already makes.
    """
    config = _app_config()
    research = A._backtest_config(_Args(), config)

    assert research.start is None or pd.Timestamp(
        research.start, tz="UTC"
    ) < pd.Timestamp("2020-01-01", tz="UTC"), (
        f"research now starts at {research.start}, so every sweep, battery "
        "and recorded figure is measured over a different span than the one "
        "that produced them"
    )
    assert research.regime.macro_on_own_calendar is False, (
        "flipping the regime default moves the macro labels across 3.65 "
        "years -- unknown 1,827 bars to 0, stagflation 84 to 586 -- so every "
        "recorded research figure changes as a side effect of a live fix"
    )


def test_the_window_clears_the_longest_lookback() -> None:
    """M1685: a window below the warm-up, which two months is.

    Asserted against the real config objects, so lengthening a lookback past
    the window fails the build rather than quietly starving it.
    """
    from cfdinvest.preprocessing import PreprocessConfig

    settings = PreprocessConfig()
    longest = settings.breakdown_baseline_window + settings.correlation_window
    assert longest == WARM_UP_BARS, (
        f"the longest input is now {longest} bars, not {WARM_UP_BARS}. "
        "Re-measure the window against it before changing this number."
    )

    available = A.INTENT_WINDOW_MONTHS * BARS_PER_MONTH
    assert available >= longest, (
        f"{A.INTENT_WINDOW_MONTHS} months is about {available} bars against "
        f"a {longest}-bar warm-up, so the longest input never exists. Two "
        "months is 308 bars and reproduced five dates anyway -- passing from "
        "below the floor is not evidence of headroom."
    )
    assert available >= 2 * longest, (
        f"{A.INTENT_WINDOW_MONTHS} months clears the warm-up by only "
        f"{available / longest:.1f}x. Six months is 2.8x and costs nothing "
        "more than three, so a thinner margin should be argued for."
    )


def test_the_window_is_six_months_at_the_interval_it_was_measured_on() -> None:
    """The shipped figure is the measured one, not an approximation of it.

    The floor exists for coarser resolutions and must not quietly override
    the number the sweep actually established. It did at first: estimating
    bars-per-session by rounding gave 6 where the lake holds 7.06, which
    stretched the production window to 6.8 months.
    """
    end = pd.Timestamp("2026-09-11 19:00", tz="UTC")
    start = A._intent_window_start(end, "capital_1h")

    months = (end - start).days / 30.44
    assert months == pytest.approx(A.INTENT_WINDOW_MONTHS, abs=0.3), (
        f"the production window is {months:.1f} months, not "
        f"{A.INTENT_WINDOW_MONTHS}. The warm-up floor is overriding the "
        "figure that was measured, which means the shipped behaviour is not "
        "the behaviour the sweep tested."
    )


def test_a_coarser_interval_still_gets_its_warm_up() -> None:
    """M1686: six months of DAILY bars is ~126, well under the warm-up.

    Found by the suite rather than by reasoning: adding a flat six-month
    window made `test_run_once_through_the_cli` report "no data to trade",
    because its fixture runs at ``1d``. An empty book from a starved panel
    is indistinguishable from an empty book from a quiet market.
    """
    end = pd.Timestamp("2026-09-11 19:00", tz="UTC")
    start = A._intent_window_start(end, "1d")

    sessions = (end - start).days * 5.0 / 7.0
    needed = A._warm_up_bars() * A.INTENT_WINDOW_WARM_UP_MARGIN
    assert sessions >= needed, (
        f"a daily run gets about {sessions:.0f} bars against a warm-up of "
        f"{needed:.0f}. The longest input never exists, so the run produces "
        "an empty book and calls it a quiet market."
    )


def test_an_unreadable_interval_leaves_the_run_unbounded() -> None:
    """M1687: guessing a grid, rather than declining to window.

    The same safe direction `_completed_bar_end` takes. An unbounded run is
    what shipped before the window existed, so falling back to it cannot be
    a regression -- whereas windowing on a guessed bar size can starve the
    panel silently.
    """
    end = pd.Timestamp("2026-09-11 19:00", tz="UTC")
    for interval in (None, "", "nonsense", "capital_7q"):
        assert A._intent_window_start(end, interval) is None, (
            f"{interval!r} is not a readable bar size and the run was "
            "windowed against a guess at one anyway"
        )
