"""No one instrument may carry the book.

The bridge has capped a single instrument's share of gross since it was
written; the engine never has. So every backtest figure this project holds
was measured on a book that could put everything into one name, which is not
what the account does -- and it is why the question could not be answered
from a backtest at all.

It binds hardest exactly where the book is thinnest. Measured over 3.65
years the book holds a single name on 17.3% of bars and two or fewer on
30.3%, so the thin case is the common one rather than the tail.

M367 one instrument carrying the whole book
M368 the cap measured against gross held, so it tightens as the book empties
M369 the cap applied proportionally, which cannot stop one name dominating
M370 the gross cap applied first, shrinking the book for a position about to be cut
M371 a second copy of the threshold, free to drift from the bridge's
M372 the cap set but never reaching the engine
"""

from __future__ import annotations

import numpy as np
import pytest

from cfdinvest.execution.engine import (
    _apply_concentration_cap,
    _apply_leverage_cap,
)


class Config:
    def __init__(self, concentration=None, max_leverage=2.5):
        self.concentration = concentration
        self.max_leverage = max_leverage


def test_one_instrument_cannot_carry_the_whole_book():
    """M367: everything in one name.

    At 2.5x gross on £1,000 of equity the ceiling is £2,500, and a 0.40 cap
    allows £1,000 of it to any single instrument.
    """
    units = np.array([25.0, 0.0])          # 25 units at 100 = £2,500, the lot
    price = np.array([100.0, 100.0])

    capped = _apply_concentration_cap(units, price, 1000.0, Config(0.40))

    assert abs(capped[0]) * 100.0 == pytest.approx(1000.0)
    assert capped[0] < units[0], "the position was not trimmed at all"


def test_no_cap_leaves_every_recorded_run_untouched():
    """Absent means no limit, which is what every figure before this assumed."""
    units = np.array([25.0, 3.0])
    price = np.array([100.0, 50.0])

    for absent in (None, 0.0, 1.0, -0.2):
        assert np.array_equal(
            _apply_concentration_cap(units, price, 1000.0, Config(absent)), units
        ), f"concentration={absent!r} changed the book"


def test_the_ceiling_does_not_tighten_as_the_book_empties():
    """M368: measured against gross held rather than the ceiling.

    A one-name book would then cap that name at its share of *itself*, which
    is a share of a share and shrinks towards nothing. The limit is a
    fraction of what the account may carry, not of what it happens to hold.
    """
    price = np.array([100.0])
    config = Config(0.40, max_leverage=2.5)

    alone = _apply_concentration_cap(np.array([25.0]), price, 1000.0, config)
    assert abs(alone[0]) * 100.0 == pytest.approx(1000.0), (
        "a single position was measured against itself"
    )

    # And the same instrument beside others is allowed exactly the same.
    crowded = _apply_concentration_cap(
        np.array([25.0, 5.0, 5.0]), np.array([100.0, 100.0, 100.0]),
        1000.0, config,
    )
    assert abs(crowded[0]) * 100.0 == pytest.approx(1000.0)


def test_only_the_offending_position_is_trimmed():
    """M369: a proportional scaling, which cannot stop one name dominating.

    The gross cap already scales the whole book. If this did too, the ratio
    between the names would never change and the largest would stay the
    largest by the same margin -- which is the entire thing being prevented.
    """
    units = np.array([25.0, 2.0, 1.0])
    price = np.array([100.0, 100.0, 100.0])

    capped = _apply_concentration_cap(units, price, 1000.0, Config(0.40))

    assert capped[0] < units[0], "the oversized position survived"
    assert capped[1] == pytest.approx(units[1]), "an innocent position was scaled"
    assert capped[2] == pytest.approx(units[2])


def test_a_short_is_capped_on_magnitude_and_keeps_its_side():
    units = np.array([-25.0])
    capped = _apply_concentration_cap(units, np.array([100.0]), 1000.0, Config(0.40))

    assert capped[0] < 0, "the cap flipped a short into a long"
    assert abs(capped[0]) * 100.0 == pytest.approx(1000.0)


def test_the_caps_compose_in_the_order_that_frees_room():
    """M370: the gross cap first, shrinking the book for a position about to be cut.

    Trimming one name releases room under the ceiling for the others. Doing
    it the other way round shrinks everybody to make space for exposure that
    is then removed anyway, and the book ends up under-invested.
    """
    units = np.array([25.0, 10.0])         # £3,500 against a £2,500 ceiling
    price = np.array([100.0, 100.0])
    config = Config(0.40, max_leverage=2.5)

    right = _apply_leverage_cap(
        _apply_concentration_cap(units, price, 1000.0, config), price, 1000.0, config
    )
    wrong = _apply_concentration_cap(
        _apply_leverage_cap(units, price, 1000.0, config), price, 1000.0, config
    )

    assert float(np.abs(right * price).sum()) == pytest.approx(2000.0)
    assert float(np.abs(wrong * price).sum()) < float(np.abs(right * price).sum()), (
        "the orders compose the same either way, so the order does not matter"
    )
    # The smaller position is untouched by the right order and shrunk by the wrong.
    assert right[1] == pytest.approx(10.0)


def test_zero_equity_holds_nothing_rather_than_dividing_by_it():
    assert np.array_equal(
        _apply_concentration_cap(np.array([25.0]), np.array([100.0]), 0.0, Config(0.40)),
        np.array([0.0]),
    )


def test_a_missing_price_does_not_produce_a_nonsense_size():
    units = np.array([25.0, 5.0])
    price = np.array([np.nan, 100.0])

    capped = _apply_concentration_cap(units, price, 1000.0, Config(0.40))
    assert np.isfinite(capped).all(), "an unpriced instrument produced a non-finite size"


def test_the_threshold_has_one_owner():
    """M371: the engine and the bridge capping at different numbers.

    It used to be a default argument in two places. Two copies of a limit
    drift, and the day they drift the backtest measures a book the account
    would not hold -- which is exactly the state this test was written to
    end.
    """
    from cfdinvest.config import load_config
    from cfdinvest.risk import RiskLimits
    from cfdinvest.schedule.app import _execution_config

    import dataclasses

    # A value nothing would arrive at by accident. Reading the config's own
    # number proves nothing while a hardcoded copy happens to match it --
    # which is exactly how a second owner hides.
    settings = load_config()
    owner = 0.31
    strategy = dataclasses.replace(
        settings.strategy, max_instrument_concentration=owner
    )

    engine = _execution_config(list(settings.market.tickers), strategy)
    assert engine.concentration == pytest.approx(owner), (
        "the engine caps at a different share from the strategy config"
    )

    limits = RiskLimits.for_balance(
        5000.0,
        max_leverage=strategy.max_leverage,
        concentration=owner,
    )
    assert limits.max_instrument_concentration_pct == pytest.approx(owner)
    # And the money figure the bridge actually enforces agrees with it.
    assert limits.max_exposure_per_instrument_gbp == pytest.approx(
        5000.0 * strategy.max_leverage * owner
    )


def test_the_cap_reaches_a_real_run():
    """M372: the setting present, wired, and then never consulted.

    Everything above tests the function. This drives the engine and asserts
    the book that comes out respects the limit.
    """
    import pandas as pd

    from cfdinvest.execution import ExecutionConfig, run_backtest
    from cfdinvest.preprocessing.frame import AnalysisFrame
    from cfdinvest.signals.frame import SignalFrame

    index = pd.date_range("2026-08-10", periods=40, freq="h", tz="UTC")
    columns = ["BIG", "SMALL"]
    price = pd.DataFrame(
        np.column_stack([np.full(len(index), 100.0), np.full(len(index), 100.0)]),
        index=index, columns=columns,
    )
    signals = SignalFrame(
        direction=pd.DataFrame("long", index=index, columns=columns),
        # BIG asks for the whole book; SMALL asks for very little.
        size=pd.DataFrame(
            np.column_stack([np.full(len(index), 5.0), np.full(len(index), 0.5)]),
            index=index, columns=columns,
        ),
        confidence=pd.DataFrame(0.9, index=index, columns=columns),
        reason=pd.DataFrame("", index=index, columns=columns),
    )
    analysis = AnalysisFrame(
        prices={"open": price, "high": price, "low": price, "close": price}
    )

    uncapped = run_backtest(signals, analysis, ExecutionConfig(
        initial_equity=1000.0, max_leverage=2.5, concentration=None,
        rebalance_threshold=0.01))
    capped = run_backtest(signals, analysis, ExecutionConfig(
        initial_equity=1000.0, max_leverage=2.5, concentration=0.40,
        rebalance_threshold=0.01))

    def top_share(result):
        notional = result.notionals.abs()
        gross = notional.sum(axis=1)
        live = gross > 0
        return float((notional.max(axis=1)[live] / gross[live]).max())

    assert top_share(uncapped) > 0.8, "the fixture did not concentrate at all"
    assert top_share(capped) < top_share(uncapped), (
        "the cap was configured and changed nothing"
    )
    # And the trimmed name sits at its share of the ceiling, not of gross.
    biggest = capped.notionals.abs().max(axis=1).max()
    assert biggest == pytest.approx(1000.0, rel=1e-3), (
        f"the largest position reached {biggest:.0f}, not 0.40 x 2.5 x 1000"
    )

    # M370, asserted against the engine rather than against the two
    # functions composed by hand. Trimming BIG first frees room, so SMALL
    # keeps everything it asked for (£500). Capping gross first shrinks the
    # whole book to fit an exposure that is then cut anyway, and SMALL comes
    # out at about £417 -- which is the mutation this discriminates.
    small = capped.notionals.abs()["SMALL"].iloc[-1]
    assert small == pytest.approx(500.0, rel=1e-3), (
        f"SMALL ended at {small:.0f}; the caps ran in the order that "
        "shrinks the book before trimming the position causing it"
    )
