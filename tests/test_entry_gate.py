"""The breakout entry gate: it refuses openings and closes nothing.

Validated across four lookbacks -- trending-only beats always-on at every
one, by 1.1 to 1.5 points -- which is what separates it from the several
ideas that won at a single parameter and were fitted.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from cfdinvest.execution.engine import _apply_entry_gate
from cfdinvest.signals.breakout import DEFAULT_LOOKBACK, entry_mask


def test_a_shut_gate_refuses_new_positions():
    target = np.array([5.0, 3.0])
    held = np.array([1, 1], dtype=np.int8)
    units = np.array([0.0, 0.0])          # both would be opening

    kept, directions = _apply_entry_gate(
        target, held, units, np.array([True, False])
    )
    assert kept[0] == 5.0, "an allowed entry was refused"
    assert kept[1] == 0.0, "a refused entry opened anyway"
    assert directions[1] == 0, (
        "a refused newcomer kept its direction, so the position frame "
        "records a holding the book never took"
    )


def test_a_shut_gate_never_closes_an_incumbent():
    """The distinction that has been got wrong twice in the engine: a
    condition on *entering* must not be applied to something already in."""
    target = np.array([5.0, 3.0])
    held = np.array([1, 1], dtype=np.int8)
    units = np.array([5.0, 3.0])          # both already open

    kept, directions = _apply_entry_gate(
        target, held, units, np.array([False, False])
    )
    assert list(kept) == [5.0, 3.0], (
        "the gate closed open positions -- it governs opening only"
    )
    assert list(directions) == [1, 1]


def test_an_absent_gate_permits_everything():
    target = np.array([5.0, 3.0])
    kept, _ = _apply_entry_gate(
        target, np.array([1, 1], dtype=np.int8), np.array([0.0, 0.0]), None
    )
    assert list(kept) == [5.0, 3.0]


def test_the_range_excludes_the_bar_being_traded():
    """Without the shift every new high trivially clears a window that
    contains it -- lookahead by arithmetic, and it would make the filter
    look spectacular."""
    index = pd.date_range("2026-01-01", periods=40, freq="h", tz="UTC")
    close = pd.DataFrame({"A": np.arange(100.0, 140.0)}, index=index)
    direction = pd.DataFrame("long", index=index, columns=["A"])

    mask = entry_mask(close, direction, lookback=5)
    # On a monotone rise every bar is a new high against the PRIOR window,
    # so all are allowed -- but the value being compared must be the prior
    # window's max, not this bar's.
    highs = close.rolling(5).max().shift(1)
    assert bool((close["A"] > highs["A"]).iloc[10]), "the shift is missing"
    assert bool(mask["A"].iloc[10])


def _stop_start_signal(index):
    """A signal that repeatedly turns on and off.

    Needed because the gate now only binds where a signal *starts*. A
    continuously-held direction has exactly one start, so a fixture built
    from one cannot tell a working gate from a broken one -- which is what
    the first version of this test did.
    """
    pattern = ["long" if (i // 3) % 2 == 0 else "flat" for i in range(len(index))]
    return pd.DataFrame({"A": pattern}, index=index)


def test_the_filter_is_off_outside_the_named_regimes():
    """The requirement is added where it helps rather than removed where it
    hurts -- measured: applied in sideways markets it returns 73.0% against
    74.4% for no filter at all."""
    index = pd.date_range("2026-01-01", periods=60, freq="h", tz="UTC")
    close = pd.DataFrame({"A": 100 + np.sin(np.arange(60))}, index=index)
    direction = _stop_start_signal(index)

    trending = entry_mask(close, direction,
                          pd.Series(["uptrend"] * 60, index=index), lookback=10)
    sideways = entry_mask(close, direction,
                          pd.Series(["sideways"] * 60, index=index), lookback=10)

    assert int(sideways["A"].sum()) == 60, "the filter bound in a sideways market"
    assert int(trending["A"].sum()) < 60, "the filter did not bind in a trend"


def test_an_unknown_regime_leaves_the_filter_off():
    """Absence is not evidence a trend is running. Refusing entries because
    the label is missing would be inferring from silence."""
    index = pd.date_range("2026-01-01", periods=60, freq="h", tz="UTC")
    close = pd.DataFrame({"A": 100 + np.sin(np.arange(60))}, index=index)

    mask = entry_mask(close, _stop_start_signal(index),
                      pd.Series(["unknown"] * 60, index=index), lookback=10)
    assert int(mask["A"].sum()) == 60


def test_too_little_history_is_permitted_not_refused():
    """"Not enough history to test" is not "failed the test"."""
    index = pd.date_range("2026-01-01", periods=30, freq="h", tz="UTC")
    close = pd.DataFrame({"A": np.arange(100.0, 130.0)}, index=index)
    direction = pd.DataFrame("long", index=index, columns=["A"])

    mask = entry_mask(close, direction, lookback=DEFAULT_LOOKBACK)
    assert bool(mask["A"].iloc[0]), "the leading window refused entries"


# --------------------------------------------------------------------------- #
# Trimming positions whose conviction has decayed below the entry bar.
# --------------------------------------------------------------------------- #

from cfdinvest.execution.engine import _trim_decayed


class _Thresholds:
    entry_confidence = 0.35
    exit_confidence = 0.20


class _Config:
    thresholds = _Thresholds()
    decayed_position_multiplier = 0.5


def test_a_decayed_position_is_trimmed_not_closed():
    """The gap the hysteresis band leaves open: entry needs 0.35, exit
    triggers at 0.20, so a position at 0.30 is too weak to open and too
    strong to close -- and was held at full size."""
    target = np.array([10.0])
    units = np.array([10.0])            # already held
    trimmed = _trim_decayed(target, units, np.array([0.30]), _Config())

    assert trimmed[0] == 5.0, "a decayed position was not trimmed"
    assert trimmed[0] != 0.0, (
        "trimming became closing -- the position must run on and exit on "
        "its own terms"
    )


def test_a_position_still_worth_opening_is_left_alone():
    trimmed = _trim_decayed(
        np.array([10.0]), np.array([10.0]), np.array([0.50]), _Config()
    )
    assert trimmed[0] == 10.0


def test_entries_are_untouched():
    """A new position cannot be below the entry bar -- the band refuses it --
    so this must never fire at entry, whatever the confidence says."""
    trimmed = _trim_decayed(
        np.array([10.0]), np.array([0.0]), np.array([0.10]), _Config()
    )
    assert trimmed[0] == 10.0, "the trim fired on an opening position"


def test_the_threshold_is_the_entry_bar_not_a_number_of_its_own():
    """The swept peak coincided with entry_confidence, which is what the
    rule means: halve a position you would no longer open. Moving the entry
    bar must move this with it."""
    class Raised(_Config):
        thresholds = type("T", (), {"entry_confidence": 0.60})()

    at_045 = _trim_decayed(
        np.array([10.0]), np.array([10.0]), np.array([0.45]), _Config()
    )
    assert at_045[0] == 10.0, "0.45 was trimmed against a 0.35 bar"

    raised = _trim_decayed(
        np.array([10.0]), np.array([10.0]), np.array([0.45]), Raised()
    )
    assert raised[0] == 5.0, (
        "raising entry_confidence did not raise the trim threshold with it"
    )


def test_disabled_by_default_and_when_unset():
    class Off(_Config):
        decayed_position_multiplier = None

    trimmed = _trim_decayed(
        np.array([10.0]), np.array([10.0]), np.array([0.10]), Off()
    )
    assert trimmed[0] == 10.0


def test_a_continuing_signal_is_not_re_gated():
    """The rule that was measured: confirm where a move STARTS, not on every
    re-entry into it.

    With a 0.29-day time stop, positions close constantly while signals
    persist. Demanding a fresh breakout for each re-entry gates a move
    already confirmed once, and measured 63.0% against 78.1% for confirming
    only at the start -- fifteen points, and the difference between the rule
    that was validated and the rule first implemented.
    """
    index = pd.date_range("2026-01-01", periods=60, freq="h", tz="UTC")
    close = pd.DataFrame({"A": 100 + np.sin(np.arange(60))}, index=index)
    direction = pd.DataFrame("long", index=index, columns=["A"])

    lenient = entry_mask(close, direction, lookback=10)
    strict = entry_mask(close, direction, lookback=10, on_new_signal_only=False)

    assert int(lenient["A"].sum()) > int(strict["A"].sum()), (
        "a continuing signal is being re-gated on every bar, which is the "
        "stricter rule that measured fifteen points worse"
    )

    # Confirmation is required once, not never: a signal that stops and
    # starts is gated at each start. (A single unbroken run cannot show
    # this -- its only start falls in the leading window, which is
    # permitted by design.)
    restarting = _stop_start_signal(index)
    gated = entry_mask(close, restarting, lookback=10)
    active = int((restarting["A"] != "flat").sum())
    permitted_while_active = int(gated["A"][restarting["A"] != "flat"].sum())
    assert permitted_while_active < active, (
        "every start was permitted -- the gate requires confirmation nowhere"
    )


def test_the_trim_threshold_can_be_swept_without_moving_the_entry_bar():
    """Sweeping a rule must not require editing the signal frame.

    The first threshold for this rule was chosen in a research harness that
    multiplied signal size before the engine saw it -- which can push a
    signal under ``entry_size`` and suppress an entry outright, something
    the engine rule never does. The harness measured 77.4%; the shipped rule
    measures 73.6%. Same name, different rule, for the third time in a
    weekend.

    This field exists so the sweep can run through ExecutionConfig against
    the code that ships.
    """
    class Swept(_Config):
        decayed_threshold = 0.60

    # A position at 0.45 clears the 0.35 entry bar but not a swept 0.60.
    assert _trim_decayed(
        np.array([10.0]), np.array([10.0]), np.array([0.45]), _Config()
    )[0] == 10.0
    assert _trim_decayed(
        np.array([10.0]), np.array([10.0]), np.array([0.45]), Swept()
    )[0] == 5.0

    # And the entry bar itself is untouched by the sweep.
    assert Swept.thresholds.entry_confidence == 0.35


def test_absent_the_override_the_entry_bar_still_governs():
    """The shipped behaviour, and what the rule means."""
    class Raised(_Config):
        thresholds = type("T", (), {"entry_confidence": 0.60})()
        decayed_threshold = None

    assert _trim_decayed(
        np.array([10.0]), np.array([10.0]), np.array([0.45]), Raised()
    )[0] == 5.0


def test_the_pipeline_builds_the_gate_when_configured():
    """Structural: the mask needs the price panel, the direction frame and
    the regime, so it is built in the runner rather than in the execution
    config. The realistic regression is somebody moving the switch and
    leaving the build behind, which would disable the gate silently while
    the setting still reads 12."""
    import inspect

    from cfdinvest.backtest import runner

    source = inspect.getsource(runner)
    assert "breakout_lookback" in source and "entry_mask(" in source, (
        "the runner no longer builds the entry mask, so a configured gate "
        "would do nothing"
    )
    assert "entry_gate=mask" in source


def test_a_gate_that_cannot_be_built_warns_rather_than_silently_passing():
    """Absence named. A configured gate that produces no mask must say so --
    'ungated because it could not be built' and 'ungated because nobody
    asked' are different states."""
    import inspect

    from cfdinvest.backtest import runner

    source = inspect.getsource(runner)
    assert "entries are ungated this run" in source


# --------------------------------------------------------------------------- #
# Two gates, one field
# --------------------------------------------------------------------------- #
def test_two_gates_compose_rather_than_the_last_one_winning():
    """``entry_gate`` is a public field and the breakout is not its only user.

    ``run_from_analysis`` used to *assign* the breakout mask, so with
    ``breakout_lookback`` set -- it is, at 12 -- any caller-supplied gate was
    silently discarded and its arm measured the baseline under another name.
    A universe screen expressed as a gate returns an identical number at
    every target size that way, which reads as "size does not matter" rather
    than as a bug. Verified live before the fix: 1,412 trades gated and
    ungated alike; after it, 561 against 1,412.
    """
    import pandas as pd

    from cfdinvest.backtest.runner import compose_entry_gates

    index = pd.date_range("2026-01-05 14:00", periods=4, freq="1h", tz="UTC")
    columns = pd.Index(["AAA", "BBB"])
    breakout = pd.DataFrame(True, index=index, columns=columns)
    screen = pd.DataFrame(True, index=index, columns=columns)
    screen["BBB"] = False                      # never selected by the screen
    breakout.iloc[0] = False                   # no breakout on the first bar

    combined = compose_entry_gates(screen, breakout)
    assert not combined["BBB"].any(), (
        "a name the screen refused was permitted anyway; the masks were not "
        "combined"
    )
    assert not combined["AAA"].iloc[0], "the breakout refusal was lost"
    assert combined["AAA"].iloc[1:].all(), (
        "a bar both gates permitted was refused"
    )


def test_composition_leaves_a_lone_gate_untouched():
    """A run configuring only the breakout must behave exactly as it did, and
    "no reading" is permitted rather than refused."""
    import pandas as pd

    from cfdinvest.backtest.runner import compose_entry_gates

    index = pd.date_range("2026-01-05 14:00", periods=3, freq="1h", tz="UTC")
    mask = pd.DataFrame([[True], [False], [True]], index=index, columns=["AAA"])
    assert compose_entry_gates(None, mask) is mask
    assert compose_entry_gates(mask, None) is mask

    # A supplied gate that does not cover every column or bar must not
    # refuse what it never spoke about.
    wide = pd.DataFrame(True, index=index, columns=pd.Index(["AAA", "BBB"]))
    partial = pd.DataFrame({"AAA": [True, True, True]}, index=index)
    combined = compose_entry_gates(partial, wide)
    assert combined["BBB"].all(), "an unmentioned name was refused"


# --------------------------------------------------------------------------- #
# Which regime the confirmation is required in
# --------------------------------------------------------------------------- #
def _regimes(trend_labels, vol_labels, index, columns=None):
    """Book-level regime Series, one label per bar.

    Series, not DataFrame. The pipeline produces one label for the whole
    book per bar and ``entry_mask`` expects that -- an earlier version of
    these tests passed DataFrames, which the production code never sees, so
    they exercised a shape that does not occur and missed a real crash.
    """
    import pandas as pd
    import types

    flat = lambda rows: [r[0] if isinstance(r, list) else r for r in rows]
    return types.SimpleNamespace(
        trend_regime=pd.Series(flat(trend_labels), index=index),
        volatility_regime=pd.Series(flat(vol_labels), index=index),
    )


def test_both_is_the_intersection_not_the_union():
    """The whole reason this needed a derived frame.

    ``entry_mask`` returns True where the filter does NOT apply, so ANDing a
    trend-gated mask with a volatility-gated one yields "confirm if trending
    OR loud" -- the union of the conditions, which is the opposite of asking
    for both. Only the bar that is trending *and* loud may be gated.
    """
    import pandas as pd

    from cfdinvest.signals.breakout import gating_frame

    index = pd.date_range("2026-01-05", periods=4, freq="1D", tz="UTC")
    columns = pd.Index(["AAA"])
    regimes = _regimes(
        [["uptrend"], ["uptrend"], ["sideways"], ["sideways"]],
        [["high"], ["low"], ["high"], ["low"]],
        index, columns,
    )
    frame, labels = gating_frame(regimes, "both")
    assert labels == ("gate",)
    got = list(frame)
    assert got == ["gate", "off", "off", "off"], (
        f"expected only the trending-and-loud bar to be gated, got {got}"
    )


def test_each_mode_selects_the_frame_it_names():
    import pandas as pd

    from cfdinvest.signals.breakout import gating_frame

    index = pd.date_range("2026-01-05", periods=2, freq="1D", tz="UTC")
    columns = pd.Index(["AAA"])
    regimes = _regimes([["uptrend"], ["sideways"]], [["high"], ["low"]],
                       index, columns)

    frame, labels = gating_frame(regimes, "trend")
    assert labels == ("uptrend", "downtrend")
    assert list(frame) == ["uptrend", "sideways"]

    frame, labels = gating_frame(regimes, "volatility")
    assert labels == ("high",)
    assert list(frame) == ["high", "low"]

    # "any" means gate everywhere: no frame, which entry_mask already reads
    # as "the filter applies unconditionally".
    frame, labels = gating_frame(regimes, "any")
    assert frame is None and labels == ()


def test_an_unknown_mode_is_refused_rather_than_defaulted():
    """Silently falling back to the trend regime would make a typo measure
    the baseline under another name -- the inert-arm failure this project
    has paid for repeatedly."""
    import pandas as pd
    import pytest as _pytest

    from cfdinvest.signals.breakout import gating_frame

    index = pd.date_range("2026-01-05", periods=1, freq="1D", tz="UTC")
    regimes = _regimes([["uptrend"]], [["high"]], index, pd.Index(["AAA"]))
    with _pytest.raises(ValueError, match="unknown breakout gating regime"):
        gating_frame(regimes, "voaltility")


def test_a_missing_regime_frame_leaves_the_filter_off():
    """Absence is not evidence. An unavailable regime must not be read as
    'not trending' and used to refuse entries."""
    from cfdinvest.signals.breakout import gating_frame
    import types

    frame, labels = gating_frame(types.SimpleNamespace(trend_regime=None,
                                                       volatility_regime=None),
                                 "both")
    assert frame is None and labels == ()


# --------------------------------------------------------------------------- #
# Sizing by conviction, not just gating on it
# --------------------------------------------------------------------------- #
def test_confidence_sizing_is_off_unless_asked_for():
    """Every accepted signal was sized identically until 2026-08-30. That
    stays the default, so a recorded run keeps reproducing."""
    import numpy as np

    from cfdinvest.execution.engine import _size_by_confidence
    from cfdinvest.execution.frame import ExecutionConfig

    target = np.array([10.0, -4.0])
    same = _size_by_confidence(target, np.array([0.9, 0.4]), ExecutionConfig())
    assert np.allclose(same, target)


def test_a_position_at_the_entry_bar_is_unchanged():
    """The multiplier is centred on the bar: sizing by conviction must not
    silently re-scale the whole book, or the arm measures leverage."""
    import dataclasses

    import numpy as np

    from cfdinvest.execution.engine import _size_by_confidence
    from cfdinvest.execution.frame import ExecutionConfig

    config = dataclasses.replace(ExecutionConfig(), confidence_sizing_strength=1.0)
    bar = config.thresholds.entry_confidence
    got = _size_by_confidence(np.array([10.0]), np.array([bar]), config)
    assert got[0] == pytest.approx(10.0)


def test_more_conviction_means_more_size_and_less_means_less():
    import dataclasses

    import numpy as np

    from cfdinvest.execution.engine import _size_by_confidence
    from cfdinvest.execution.frame import ExecutionConfig

    config = dataclasses.replace(ExecutionConfig(), confidence_sizing_strength=1.0)
    bar = config.thresholds.entry_confidence
    got = _size_by_confidence(
        np.array([10.0, 10.0, 10.0]),
        np.array([1.0, bar, bar / 2.0]),
        config,
    )
    assert got[0] > got[1] > got[2], f"not monotone in confidence: {got}"
    # Sign is preserved: a short sized up is still a short.
    short = _size_by_confidence(np.array([-10.0]), np.array([1.0]), config)
    assert short[0] < 0


def test_a_weak_signal_is_trimmed_rather_than_erased():
    """The gate already decided the signal is worth taking. Sizing it to
    nothing would be a second, unmeasured gate wearing a multiplier."""
    import dataclasses

    import numpy as np

    from cfdinvest.execution.engine import _size_by_confidence
    from cfdinvest.execution.frame import ExecutionConfig

    config = dataclasses.replace(
        ExecutionConfig(), confidence_sizing_strength=5.0,
        confidence_sizing_floor=0.25,
    )
    got = _size_by_confidence(np.array([10.0]), np.array([0.0]), config)
    assert got[0] == pytest.approx(2.5), (
        "a low-confidence position was erased rather than trimmed"
    )


def test_an_absent_confidence_reading_scales_nothing():
    """Absence named: "no confidence recorded" is not "no conviction", and
    scaling it to zero would silently drop positions."""
    import dataclasses

    import numpy as np

    from cfdinvest.execution.engine import _size_by_confidence
    from cfdinvest.execution.frame import ExecutionConfig

    config = dataclasses.replace(ExecutionConfig(), confidence_sizing_strength=1.0)
    got = _size_by_confidence(np.array([10.0]), np.array([np.nan]), config)
    assert got[0] == pytest.approx(10.0)
