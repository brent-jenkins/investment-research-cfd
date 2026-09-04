


# --------------------------------------------------------------------------- #
# Overnight profit-taking.
#
# The owner's observation, 2026-09-04: a short in AMAT stood at +141.77 on the
# last bar of Thursday and +2.43 on Friday morning. The question is whether
# banking a gain that has already appeared beats carrying it overnight.
#
# The one member of this family never measured. Every previous test was on the
# LOSING side and all failed, because losers mean-revert favourably and the
# premium is paid on every night to catch a rare one.
#
# M352 the rule firing on every bar rather than only at a session close
# M353 the threshold ignored, so any winner is banked
# M354 the stall condition inverted, banking what is still running
# M355 the rule closing losers as well as winners
# --------------------------------------------------------------------------- #


def _overnight_frames(prices, bars_per_day=4):
    """Two sessions of hourly bars with a persistent long signal."""
    import numpy as np
    import pandas as pd

    from cfdinvest.preprocessing.frame import AnalysisFrame
    from cfdinvest.signals.frame import SignalFrame

    stamps = []
    for day in range(len(prices) // bars_per_day):
        base = pd.Timestamp("2026-01-05", tz="UTC") + pd.Timedelta(days=day)
        stamps += [base + pd.Timedelta(hours=13 + n) for n in range(bars_per_day)]
    index = pd.DatetimeIndex(stamps[:len(prices)])
    close = pd.DataFrame({"AAA": list(prices)}, index=index)
    frame = lambda v: pd.DataFrame({"AAA": v}, index=index)
    signals = SignalFrame(
        direction=frame(["long"] * len(index)),
        size=frame([0.1] * len(index)),
        confidence=frame([0.9] * len(index)),
        reason=frame([""] * len(index)),
    )
    analysis = AnalysisFrame(
        prices={"close": close, "open": close, "high": close, "low": close},
        returns=close.pct_change().fillna(0.0),
    )
    return index, signals, analysis


def test_a_winner_is_banked_on_the_last_bar_of_a_session():
    from cfdinvest.execution import run_backtest
    from cfdinvest.execution.frame import ExecutionConfig

    # Entered at 100, worth 130 by the session's last bar: a 30% gain.
    index, signals, analysis = _overnight_frames(
        [100.0, 110.0, 120.0, 130.0, 131.0, 132.0, 133.0, 134.0])

    carried = run_backtest(signals, analysis, ExecutionConfig(execution_lag=0))
    banked = run_backtest(signals, analysis, ExecutionConfig(
        execution_lag=0, overnight_profit_take=10.0))

    session_close = index[3]
    assert float(carried.positions["AAA"].fillna(0.0).loc[session_close]) != 0.0, (
        "the control was already flat at the session close, so this proves nothing"
    )
    assert float(banked.positions["AAA"].fillna(0.0).loc[session_close]) == 0.0, (
        "a 30% winner was carried overnight against a 10% take-profit"
    )


def test_a_gain_below_the_threshold_is_carried():
    from cfdinvest.execution import run_backtest
    from cfdinvest.execution.frame import ExecutionConfig

    # Entered at 100, worth 103 at the close: 3%, under a 10% bar.
    index, signals, analysis = _overnight_frames(
        [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 107.0])
    result = run_backtest(signals, analysis, ExecutionConfig(
        execution_lag=0, overnight_profit_take=10.0))
    assert float(result.positions["AAA"].fillna(0.0).loc[index[3]]) != 0.0, (
        "a 3% gain was banked against a 10% threshold"
    )


def test_a_loser_is_never_banked_by_the_profit_take():
    """It is a profit rule. Cutting losses overnight was measured and failed."""
    from cfdinvest.execution import run_backtest
    from cfdinvest.execution.frame import ExecutionConfig

    index, signals, analysis = _overnight_frames(
        [100.0, 95.0, 90.0, 85.0, 84.0, 83.0, 82.0, 81.0])
    result = run_backtest(signals, analysis, ExecutionConfig(
        execution_lag=0, overnight_profit_take=10.0))
    assert float(result.positions["AAA"].fillna(0.0).loc[index[3]]) != 0.0, (
        "a losing position was closed by a rule that only banks gains"
    )


def test_a_position_still_running_is_not_banked_when_a_stall_is_required():
    """The owner's condition: bank it only once it has stopped paying."""
    from cfdinvest.execution import run_backtest
    from cfdinvest.execution.frame import ExecutionConfig

    # Six-bar sessions, because the decision is taken on the PREVIOUS
    # completed bar and the stall window reaches back from there: a four-bar
    # session cannot hold both "up 10%" and "flat for two bars" at once.
    #
    # Entered at 100 and still climbing: the decision bar is close[4] = 140,
    # against close[2] = 120 two bars earlier, so it is up 16.7% and running.
    index, signals, analysis = _overnight_frames(
        [100.0, 110.0, 120.0, 130.0, 140.0, 150.0,
         151.0, 152.0, 153.0, 154.0, 155.0, 156.0], bars_per_day=6)
    result = run_backtest(signals, analysis, ExecutionConfig(
        execution_lag=0, overnight_profit_take=10.0,
        overnight_profit_stall_bars=2))
    assert float(result.positions["AAA"].fillna(0.0).loc[index[5]]) != 0.0, (
        "a position still running strongly was banked despite the stall "
        "condition requiring it to have stopped paying"
    )


def test_a_stalled_winner_is_banked():
    """The AMAT shape: a big gain that has gone flat for the last few bars."""
    from cfdinvest.execution import run_backtest
    from cfdinvest.execution.frame import ExecutionConfig

    # Entered at 100, ran to 130, then sat there. The decision bar is
    # close[4] = 130 against close[2] = 130: up 30% and flat for two bars,
    # which is the AMAT shape.
    index, signals, analysis = _overnight_frames(
        [100.0, 130.0, 130.0, 130.0, 130.0, 130.0,
         131.0, 132.0, 133.0, 134.0, 135.0, 136.0], bars_per_day=6)
    result = run_backtest(signals, analysis, ExecutionConfig(
        execution_lag=0, overnight_profit_take=10.0,
        overnight_profit_stall_bars=2))
    assert float(result.positions["AAA"].fillna(0.0).loc[index[5]]) == 0.0, (
        "a 30% winner that had gone flat for two bars was carried overnight"
    )


def test_the_profit_take_only_fires_at_a_session_close():
    """Mid-session it must do nothing, or it is a take-profit and not this rule."""
    from cfdinvest.execution import run_backtest
    from cfdinvest.execution.frame import ExecutionConfig

    index, signals, analysis = _overnight_frames(
        [100.0, 130.0, 131.0, 132.0, 133.0, 134.0, 135.0, 136.0])
    result = run_backtest(signals, analysis, ExecutionConfig(
        execution_lag=0, overnight_profit_take=10.0))
    held = result.positions["AAA"].fillna(0.0)
    assert float(held.loc[index[1]]) != 0.0, (
        "a 30% gain was banked on the second bar of the session; the rule is "
        "firing on every bar rather than only at the close"
    )
    assert float(held.loc[index[3]]) == 0.0, "it never fired at the close either"


def test_overnight_profit_taking_is_off_by_default():
    from cfdinvest.execution import run_backtest
    from cfdinvest.execution.frame import ExecutionConfig

    assert ExecutionConfig().overnight_profit_take is None
    assert ExecutionConfig().overnight_profit_stall_bars == 0
    _, signals, analysis = _overnight_frames(
        [100.0, 110.0, 120.0, 130.0, 131.0, 132.0, 133.0, 134.0])
    a = run_backtest(signals, analysis, ExecutionConfig(execution_lag=0))
    b = run_backtest(signals, analysis, ExecutionConfig(
        execution_lag=0, overnight_profit_take=None))
    assert a.positions.fillna(0.0).equals(b.positions.fillna(0.0))
