"""Cost is measured on every candidate, and no longer refuses any of them.

The screen ranks on liquidity, volatility and continuity, none of which know
what an instrument costs to deal. A gate was added on 2026-08-28 to refuse
anything above 2.5x the universe median, justified by a backtest of the
forty-nine name pool where exclusion was worth eleven points of return.

**It was switched off on 2026-08-29**, measured on a hundred-name pool over
Capital's full 3.65 years with every instrument traded and each charged its
own per-bar spread. Every threshold lost money -- 1,848 given up at 2.5x,
17,181 at 1.5x -- the refused instruments were profitable in all four years,
and net per trade *rises* with spread rather than falling (0.8 at or below
the median, 1.5 above 1.5x). Spread against net P&L correlates +0.069.

What the original measurement found was that INTC and SMCI had a bad
2024-25 and happened to be the dear names in a pool of forty-nine.
Generalising from two instruments was the error. A name is usually dear
*because* it moves, and movement is what the strategy is paid for.

The machinery is kept and still tested: off by default is not removed, and
if a venue or universe turns up where cost genuinely disqualifies, it is a
config value rather than a rewrite. What must not regress is that cost is
still *measured* on every candidate -- "not checked" and "checked and it
does not disqualify" are different claims.

M402 an unaffordable instrument ranked anyway, when the gate is asked for
M403 cost applied after the ranking, so refused names still move the
     z-scores of the ones that survive
M404 a missing spread reading treated as a cheap one
M405 a whole-history median instead of a trailing one, banning a name for a
     period it has already left
M406 a claim made on a handful of readings
M407 a fixed basis-point threshold rather than one relative to the day
M408 the spread measurement silently dropped when the score is attached
M409 the gate back on by default, or the measurement lost with it
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from cfdinvest.universe.screen import Candidate, ScreenConfig, rank_candidates


def bars(n=200, seed=0, start="2025-01-01"):
    rng = np.random.default_rng(seed)
    index = pd.date_range(start, periods=n, freq="B", tz="UTC")
    close = pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.02, n))), index=index)
    return pd.DataFrame({"close": close, "volume": 1_000_000.0}, index=index)


def spread(value, n=120, end="2025-10-01"):
    index = pd.date_range(end=end, periods=n, freq="D", tz="UTC")
    return pd.Series(float(value), index=index)


def test_an_unaffordable_instrument_is_refused_and_says_why():
    """M402: the whole point."""
    prices = {t: bars(seed=i) for i, t in enumerate(["AAA", "BBB", "CCC", "DEAR"])}
    spreads = {"AAA": spread(10), "BBB": spread(10), "CCC": spread(10),
               "DEAR": spread(60)}

    result = rank_candidates(
        prices, as_of="2025-10-01",
        config=ScreenConfig(target=4, min_history=50, lookback=120,
                            max_spread_multiple=2.5),
        spreads=spreads,
    )

    assert "DEAR" in result.spread_excluded
    assert "DEAR" not in result.selected
    refused = {c.ticker: c for c in result.candidates}["DEAR"]
    assert "spread" in refused.unranked and "x" in refused.unranked, (
        f"the refusal does not say what refused it: {refused.unranked!r}"
    )


def test_the_measurement_survives_the_scoring():
    """M408: the spread dropped on the way to the score.

    `Candidate` was rebuilt positionally when the score was attached, so
    adding a field before `unranked` silently shifted the trailing "" into
    `spread_bp`. Nothing raised until something tried to format it.
    """
    prices = {t: bars(seed=i) for i, t in enumerate(["AAA", "BBB", "CCC"])}
    result = rank_candidates(
        prices, as_of="2025-10-01",
        config=ScreenConfig(target=3, min_history=50, lookback=120),
        spreads={"AAA": spread(8), "BBB": spread(10), "CCC": spread(12)},
    )
    for candidate in result.ranked:
        assert isinstance(candidate.spread_bp, float), (
            f"{candidate.ticker} carries {candidate.spread_bp!r}, not a measurement"
        )
        assert isinstance(candidate.spread_multiple, float)
        assert candidate.score is not None


def test_a_refused_name_does_not_move_the_survivors_ranking():
    """M403: order of operations, which is invisible unless asserted.

    The z-scores are rank-based, so a name left in the pool shifts every
    other name's standard score even if it is dropped afterwards. Refusing
    first is what makes the survivors' ordering independent of who was
    refused.
    """
    names = ["AAA", "BBB", "CCC"]
    prices = {t: bars(seed=i) for i, t in enumerate(names)}
    config = ScreenConfig(target=3, min_history=50, lookback=120,
                          max_spread_multiple=2.5)
    cheap = {t: spread(10) for t in names}

    without = rank_candidates(prices, as_of="2025-10-01", config=config, spreads=cheap)

    # Same three, plus one that will be refused on cost.
    prices_plus = dict(prices, DEAR=bars(seed=99))
    spreads_plus = dict(cheap, DEAR=spread(80))
    with_dear = rank_candidates(
        prices_plus, as_of="2025-10-01", config=config, spreads=spreads_plus
    )

    # Scores, not order. The z-scores are percentile ranks, so adding a
    # fourth name shifts every score while leaving the three in the same
    # sequence -- an ordering assertion cannot see this and the first
    # version of this test could not.
    before = {c.ticker: c.score for c in without.ranked}
    after = {c.ticker: c.score for c in with_dear.ranked}
    assert set(before) == set(after) == {"AAA", "BBB", "CCC"}
    for ticker, score in before.items():
        assert after[ticker] == pytest.approx(score), (
            f"{ticker} scored {score:.4f} against three tradable names and "
            f"{after[ticker]:.4f} once an untradable fourth was in the pool; "
            "a score is meant to place an instrument against the universe "
            "it competes with, and a refused name is not in that universe"
        )


def test_an_unmeasured_spread_is_named_not_assumed_cheap():
    """M404: absence read as a pass.

    "No reading" and "read and fine" are different statements. The one
    without a reading is still ranked -- refusing on absence would empty
    the screen wherever the spread history does not reach -- but the result
    has to say so.
    """
    prices = {t: bars(seed=i) for i, t in enumerate(["AAA", "BBB", "QUIET"])}
    result = rank_candidates(
        prices, as_of="2025-10-01",
        config=ScreenConfig(target=3, min_history=50, lookback=120),
        spreads={"AAA": spread(10), "BBB": spread(10)},
    )

    quiet = {c.ticker: c for c in result.candidates}["QUIET"]
    assert quiet.spread_bp is None, "an unmeasured spread was given a number"
    assert quiet.unranked == "", "absence was treated as a refusal"
    assert any("unverified" in note for note in result.notes), (
        "nothing in the result says the spread could not be checked"
    )


def test_the_median_is_trailing_not_whole_history():
    """M405: a ban that outlives the period that earned it.

    INTC ran 0.85x the universe median in 2023, 3.63x in 2024, 5.02x in
    2025 and 1.45x in 2026. A whole-history median is 2.88x and would
    refuse it today for two years it has already left.
    """
    names = ["AAA", "BBB", "REFORMED"]
    prices = {t: bars(seed=i, n=400, start="2024-01-01") for i, t in enumerate(names)}

    # Dear for a year, ordinary for the last three months.
    old = pd.Series(80.0, index=pd.date_range("2024-06-01", periods=300, freq="D", tz="UTC"))
    new = pd.Series(10.0, index=pd.date_range("2025-07-05", periods=90, freq="D", tz="UTC"))
    spreads = {
        "AAA": spread(10, n=400, end="2025-10-01"),
        "BBB": spread(10, n=400, end="2025-10-01"),
        "REFORMED": pd.concat([old, new]),
    }
    config = ScreenConfig(target=3, min_history=50, lookback=120,
                          spread_lookback_days=90)
    result = rank_candidates(prices, as_of="2025-10-01", config=config, spreads=spreads)

    assert "REFORMED" not in result.spread_excluded, (
        "an instrument that is cheap now was refused on history it has left"
    )
    reformed = {c.ticker: c for c in result.candidates}["REFORMED"]
    assert reformed.spread_bp == pytest.approx(10.0, abs=1e-6)


def test_a_thin_sample_withholds_the_refusal():
    """M406: a claim on a handful of readings.

    The figure is still reported -- the arithmetic is available. What is
    withheld is acting on it.
    """
    prices = {t: bars(seed=i) for i, t in enumerate(["AAA", "BBB", "NEW"])}
    spreads = {
        "AAA": spread(10), "BBB": spread(10),
        "NEW": spread(90, n=4),          # dear, but barely measured
    }
    config = ScreenConfig(target=3, min_history=50, lookback=120,
                          min_spread_readings=25)
    result = rank_candidates(prices, as_of="2025-10-01", config=config, spreads=spreads)

    assert "NEW" not in result.spread_excluded, "refused on four readings"
    new = {c.ticker: c for c in result.candidates}["NEW"]
    assert new.spread_bp == pytest.approx(90.0), "the reading was withheld too"
    assert any("withheld" in note for note in result.notes)


def test_the_threshold_is_relative_to_the_day_not_a_fixed_figure():
    """M407: a fixed bp threshold tightens and loosens with the market.

    The universe median ran 13.94bp in 2023 and 9.63bp in 2026. The same
    instrument, priced identically against its peers, must be judged the
    same in both.
    """
    names = ["AAA", "BBB", "CCC", "DEAR"]
    prices = {t: bars(seed=i) for i, t in enumerate(names)}
    config = ScreenConfig(target=4, min_history=50, lookback=120,
                          max_spread_multiple=2.5)

    tight = rank_candidates(
        prices, as_of="2025-10-01", config=config,
        spreads={"AAA": spread(5), "BBB": spread(5), "CCC": spread(5),
                 "DEAR": spread(30)},
    )
    wide = rank_candidates(
        prices, as_of="2025-10-01", config=config,
        spreads={"AAA": spread(20), "BBB": spread(20), "CCC": spread(20),
                 "DEAR": spread(120)},
    )
    assert tight.spread_excluded == wide.spread_excluded == ("DEAR",), (
        "the same relative cost was judged differently at different "
        "absolute levels"
    )


def test_the_rule_can_be_switched_off():
    """``None`` disables it, and the measurements still get reported."""
    prices = {t: bars(seed=i) for i, t in enumerate(["AAA", "DEAR"])}
    result = rank_candidates(
        prices, as_of="2025-10-01",
        config=ScreenConfig(target=2, min_history=50, lookback=120,
                            max_spread_multiple=None),
        spreads={"AAA": spread(10), "DEAR": spread(200)},
    )
    assert result.spread_excluded == ()
    assert {c.ticker for c in result.ranked} == {"AAA", "DEAR"}
    dear = {c.ticker: c for c in result.candidates}["DEAR"]
    assert dear.spread_bp == pytest.approx(200.0)


def test_no_spread_data_at_all_leaves_the_screen_working():
    """A screen that stops running is worse than one that cannot check cost."""
    prices = {t: bars(seed=i) for i, t in enumerate(["AAA", "BBB"])}
    result = rank_candidates(
        prices, as_of="2025-10-01",
        config=ScreenConfig(target=2, min_history=50, lookback=120),
        spreads={},
    )
    assert len(result.selected) == 2
    assert result.spread_excluded == ()


def test_the_shipped_configuration_measures_cost_without_refusing_on_it():
    """The real configuration, not a fixture.

    The gate was switched off on 2026-08-29 after it measured as costing
    money on a hundred-name pool across all four years -- net per trade
    *rises* with spread, and the instruments it would have refused were
    profitable in every year. What must still hold is that cost is
    measured and reported on every candidate: "we did not check" and "we
    checked and it does not disqualify" are different statements, and only
    one of them is what this now does.
    """
    from cfdinvest.config import load_config
    from cfdinvest.repository import DataRepository

    settings = load_config()
    result = rank_candidates.__globals__["select_universe"](
        DataRepository(settings), list(settings.universe.candidates),
        as_of=str(pd.Timestamp.now(tz="UTC")),
        config=ScreenConfig(target=8), sectors=None,
    )
    if not any("universe median spread" in n for n in result.notes):
        pytest.skip("no spread bars in this lake")

    assert ScreenConfig().max_spread_multiple is None, (
        "the gate is on by default again; it measured as costing money"
    )
    assert result.spread_excluded == (), (
        "something was refused for cost while the gate is off"
    )
    # Measured, though: every ranked candidate carries its spread.
    priced = [c for c in result.ranked if c.spread_bp is not None]
    assert priced, "no candidate carries a spread measurement at all"
    for candidate in priced:
        assert candidate.spread_multiple is not None
        assert candidate.spread_bp > 0


def test_the_gate_still_works_when_asked_for():
    """Off by default is not the same as removed.

    The rule is kept because the measurement that retired it is one pool
    over one span. If a venue or a universe turns up where cost genuinely
    disqualifies, switching it back on is a config value rather than a
    rewrite.
    """
    prices = {t: bars(seed=i) for i, t in enumerate(["AAA", "BBB", "CCC", "DEAR"])}
    spreads = {"AAA": spread(10), "BBB": spread(10), "CCC": spread(10),
               "DEAR": spread(60)}
    on = rank_candidates(
        prices, as_of="2025-10-01",
        config=ScreenConfig(target=4, min_history=50, lookback=120,
                            max_spread_multiple=2.5),
        spreads=spreads,
    )
    assert "DEAR" in on.spread_excluded

    off = rank_candidates(
        prices, as_of="2025-10-01",
        config=ScreenConfig(target=4, min_history=50, lookback=120),
        spreads=spreads,
    )
    assert off.spread_excluded == ()
    assert "DEAR" in off.selected, "the default should rank it, not refuse it"
