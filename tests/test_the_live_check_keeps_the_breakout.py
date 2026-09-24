"""The live re-check must not lose a breakout to its own extra bar.

WHAT WAS MEASURED, 2026-09-24, on the account's own rows with the last look
DISARMED so nothing selected which names appeared:

    ticker  engine   live    gap    3 x |gap|
    UPS     +0.754 +0.382 -0.372      1.116
    WFC     +0.861 +0.514 -0.347      1.042
    SCHW    +0.901 +0.556 -0.344      1.033
    GS      +0.777 +0.438 -0.340      1.019
    AXP     +0.791 +0.454 -0.337      1.012
    PGR     +0.842 +0.506 -0.336      1.009
    AMT     +0.769 +0.774 +0.004     -0.013
    TMO     +0.780 +0.789 +0.009     -0.026
    DHR     +0.831 +0.859 +0.029     -0.086

`trend_strength` divides by three whatever the components read, so losing
one costs exactly a third of its magnitude and `3 x |gap|` recovers it. Six
names imply a lost component of 1.009 to 1.116 against a saturation of
1.000; three imply nothing; NOTHING lies between. A noisy check spreads gaps
continuously. A check losing one saturated component gives two clusters and
a hole, which is what this is.

THE CAUSE IS AN OFF-BY-ONE IN WHICH BAR THE BAND EXCLUDES, and it is
deterministic rather than a market reading.

    engine   decides on completed bar N.  band = max(high[N-55 .. N-1])
    live     appends a provisional bar for the hour in progress, so its last
             row is N+1.                  band = max(high[N-54 .. N])

The live band therefore includes bar N's own high -- and for a name that
just broke out, bar N's high IS the new band top. `close[N] <= high[N]` by
definition, so a quote equal to the decision bar's close CANNOT exceed it.
**The component reads zero on an unchanged price.** It is not measuring that
the breakout failed; it is asking whether the bar in progress is itself a
NEW breakout, which is a different question and is almost always no.

At `band_lag=2` the live band ends where the engine's did, so the same price
gives the same answer -- and a price that has genuinely fallen back inside
the band still reads zero, which is the distinction the check exists for.

THE ENGINE IS UNCHANGED. `SignalConfig.breakout_band_lag` defaults to 1, so
no recorded figure moves. Whether the engine's own one-bar breakout EVENT
should instead be a persistent STATE is a separate question with its own
sweep, and nothing here answers it.

Mutations checked:

  M1800 the default changed to 2, moving every recorded engine figure
  M1801 the live check left at the engine's lag, restoring the artefact
  M1802 `lag` accepted and ignored, so the band never moves
  M1803 the fix made inert in the other direction -- a price that HAS
        fallen back inside the band still reading a breakout
  M1804 the band widened instead of shifted, which changes what a breakout
        means rather than which bars it is measured against
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from cfdinvest.signals.frame import SignalConfig
from cfdinvest.signals.trend import breakout_state, donchian_bounds

WINDOW = 5
QUIET = 100.0
BROKE = 110.0

#: The quiet band has WIDTH, deliberately. A flat history gives
#: ``upper == lower``, so every price breaks out one way or the other and a
#: "fell back inside the band" case cannot be expressed at all. The first
#: version of this fixture had that fault and its fall-back test read -1,
#: a breakDOWN, rather than the 0 it was asserting.
QUIET_HIGH = 101.0
QUIET_LOW = 99.0

#: Eight quiet bars before the breakout, so the rolling window is full at
#: BOTH lags. At lag 2 the band needs one more bar of history than at lag 1,
#: and a fixture that starves it would compare a number against NaN.
BEFORE = 8
DECISION = BEFORE       # index of the bar the engine decides on
PROVISIONAL = BEFORE + 1  # index of the hour in progress


def panel(closes, highs, lows):
    """One instrument, bars an hour apart, built so the answer is known."""
    index = pd.date_range("2026-09-24 06:00", periods=len(closes), freq="1h", tz="UTC")

    def frame(values):
        return pd.DataFrame({"AAA": list(values)}, index=index)

    return frame(closes), frame(highs), frame(lows)


def built_by_construction(last_close=BROKE, last_low=QUIET):
    """Quiet bars, a breakout bar, then the hour in progress.

    The decision bar closes at 110 against an upper band of 101, so it
    breaks out. The provisional bar carries ``last_close`` -- by default the
    SAME price, so nothing has moved and the check has no reason to withdraw
    the signal.
    """
    closes = [QUIET] * BEFORE + [BROKE, last_close]
    highs = [QUIET_HIGH] * BEFORE + [BROKE, max(BROKE, last_close)]
    lows = [QUIET_LOW] * BEFORE + [QUIET, min(last_low, last_close)]
    return panel(closes, highs, lows)


# --------------------------------------------------------------------- #
# The defect, stated as the thing that must not happen.
# --------------------------------------------------------------------- #
def test_the_engine_sees_the_breakout_on_its_decision_bar() -> None:
    """The baseline both arms have to agree with."""
    close, high, low = built_by_construction()

    engine = breakout_state(close, high, low, WINDOW, band_lag=1)

    assert engine.iloc[DECISION]["AAA"] == pytest.approx(1.0), (
        "the decision bar did not register a breakout, so this fixture no "
        "longer builds the case it is named for"
    )


def test_an_unchanged_price_loses_the_breakout_at_the_engines_lag() -> None:
    """M1802: the defect itself, pinned so it cannot be called a market move.

    This asserts the BROKEN behaviour on purpose, at lag 1, on a price that
    has not moved by a single tick. If this ever stops holding, the cause
    diagnosed on 2026-09-24 has changed and the fix below needs re-reading
    rather than trusting.
    """
    close, high, low = built_by_construction()

    at_engine_lag = breakout_state(close, high, low, WINDOW, band_lag=1)

    assert at_engine_lag.iloc[PROVISIONAL]["AAA"] == pytest.approx(0.0), (
        "the provisional bar kept its breakout at lag 1, so the artefact "
        "this fix exists for is not reproduced by this fixture"
    )


def test_the_live_lag_reproduces_the_engine_on_an_unchanged_price() -> None:
    """M1801: the fix, and the property that matters.

    Nothing has moved. The check must say what the engine said.
    """
    close, high, low = built_by_construction()

    engine = breakout_state(close, high, low, WINDOW, band_lag=1)
    live = breakout_state(close, high, low, WINDOW, band_lag=2)

    assert live.iloc[PROVISIONAL]["AAA"] == pytest.approx(
        engine.iloc[DECISION]["AAA"]
    ), (
        "on an unchanged price the live check still disagrees with the "
        "engine about the breakout, which costs a third of the score on "
        "exactly the names a 0.75 gate admits"
    )


def test_a_price_that_falls_back_inside_the_band_still_loses_it() -> None:
    """M1803: the fix made inert in the other direction.

    A check that keeps the breakout whatever the price does is not a check.
    Here the hour in progress has given the whole move back.
    """
    # Back to 100, which sits INSIDE the quiet band of 99-101. Falling
    # through the LOWER band would be a breakdown and would correctly read
    # -1, which is a different statement from "the breakout is gone".
    close, high, low = built_by_construction(last_close=QUIET)

    live = breakout_state(close, high, low, WINDOW, band_lag=2)

    assert live.iloc[PROVISIONAL]["AAA"] == pytest.approx(0.0), (
        "the price fell back through the band and the live check still "
        "reported a breakout, so the fix has disabled the component "
        "instead of correcting which bars it measures against"
    )


def test_the_band_is_shifted_not_widened() -> None:
    """M1804: a wider window is a different rule, not a different offset.

    Shifting asks the same question one bar earlier. Widening changes what
    counts as a breakout at all, and would move every recorded figure.
    """
    close, high, low = built_by_construction()

    one, _ = donchian_bounds(high, low, WINDOW, lag=1)
    two, _ = donchian_bounds(high, low, WINDOW, lag=2)

    # Aligned by position rather than by dropna: lag 2 carries one more
    # leading NaN, so dropping them compares arrays of different lengths --
    # which is how the first version of this test failed for a reason that
    # had nothing to do with the property.
    assert np.allclose(
        two["AAA"].to_numpy()[1:],
        one["AAA"].to_numpy()[:-1],
        equal_nan=True,
    ), "lag 2 is not lag 1 shifted by one bar, so the band was widened"


# --------------------------------------------------------------------- #
# The engine must not move.
# --------------------------------------------------------------------- #
def test_the_engine_default_is_unchanged() -> None:
    """M1800: the default moved to 2 and every recorded figure with it."""
    assert SignalConfig().breakout_band_lag == 1, (
        "the engine's breakout band moved. Every CAGR, sweep and control "
        "in this project was measured at a lag of 1, and changing the "
        "default silently invalidates all of them"
    )


def test_a_lag_below_one_is_refused() -> None:
    """Lag 0 puts the bar inside its own band, which is the classic bug.

    `donchian_bounds` documents that off-by-one as the whole reason the
    shift exists; accepting 0 would reintroduce it silently and no breakout
    could ever fire.
    """
    close, high, low = built_by_construction()

    with pytest.raises(ValueError):
        donchian_bounds(high, low, WINDOW, lag=0)


def test_the_live_check_is_the_caller_that_sets_it() -> None:
    """M1801: the setting exists and the live check does not use it.

    Anchored on an UNCOMMENTED assignment. `"breakout_band_lag" in source`
    would pass against the words sitting in a comment above a default --
    the trap that let M1762 survive against `true # market-open`.
    """
    import inspect

    from cfdinvest.advisor import livecheck

    live = [
        line for line in inspect.getsource(livecheck).splitlines()
        if "breakout_band_lag" in line and not line.lstrip().startswith("#")
    ]
    assert live, "the live check no longer sets the band lag at all"
    assert any("2" in line for line in live), (
        f"the live check sets the band lag to something other than 2: "
        f"{live!r}. At the engine's own lag the provisional bar's band "
        f"includes the decision bar and the breakout is lost on an "
        f"unchanged price"
    )
