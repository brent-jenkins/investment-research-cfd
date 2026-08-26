"""The universe screen: mechanical, point-in-time, and blind to returns.

The universe was assembled by hand and its stated basis lived in a commit
message. That is unauditable, which was tolerable for a list set once and is
not for one refreshed quarterly. These tests pin the three properties that
make the replacement worth having.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from cfdinvest.universe import Candidate, ScreenConfig, rank_candidates


def series(price=100.0, vol_pct=30.0, volume=10_000_000, bars=200, drift=0.0,
           seed=1, end="2026-01-01"):
    """One instrument's bars, with a controllable drift and volatility."""
    rng = np.random.default_rng(seed)
    index = pd.date_range(end=pd.Timestamp(end, tz="UTC"), periods=bars, freq="B")
    daily = vol_pct / 100 / np.sqrt(252)
    steps = rng.normal(drift / bars, daily, bars)
    close = price * np.exp(np.cumsum(steps))
    return pd.DataFrame({"close": close, "volume": [volume] * bars}, index=index)


def test_the_screen_never_looks_past_the_decision_date():
    """M171: a screen that peeks.

    Worse than no screen, because it produces an audit trail that looks
    rigorous. Two instruments are identical up to the cutoff and wildly
    different after it; the ranking must not be able to tell them apart.
    """
    shared = series(bars=200, seed=11)
    cutoff = shared.index[120]

    calm_after = shared.copy()
    wild_after = shared.copy()
    # Everything after the cutoff diverges: one flat, one exploding.
    wild_after.loc[wild_after.index > cutoff, "close"] *= np.linspace(
        1.0, 8.0, int((wild_after.index > cutoff).sum())
    )
    wild_after.loc[wild_after.index > cutoff, "volume"] *= 50

    result = rank_candidates(
        {"CALM": calm_after, "WILD": wild_after},
        as_of=cutoff,
        config=ScreenConfig(target=2, min_history=60, lookback=60),
    )

    scores = {c.ticker: c.score for c in result.ranked}
    assert set(scores) == {"CALM", "WILD"}
    assert scores["CALM"] == pytest.approx(scores["WILD"], abs=1e-9), (
        "the future changed the ranking -- the screen is looking past as_of"
    )


def test_the_path_to_a_price_does_not_change_the_score():
    """M172: the rebalance quietly becoming 'hold whatever went up'.

    Two instruments that arrive at the same price with the same share volume
    and the same volatility -- one by rising to it, one by falling to it --
    must score identically. What they did on the way is a return, and a
    return must not reach the ranking.

    The first version of this screen failed exactly here. Liquidity was the
    median of the daily *turnover* series, so a stock that doubled through
    the window had doubling turnover through it and the path-median rewarded
    the rise: 0.92 against 0.07 with everything else equal. Turnover is now
    priced at the decision date, so the score depends on where an instrument
    is rather than on how it travelled there.

    What this does *not* claim is that liquidity is uncorrelated with having
    gone up. It plainly is -- large liquid companies became large by rising,
    and dollar turnover is the correct measure of whether a position can be
    exited. That correlation is the price of measuring tradability properly
    and is accepted. What is excluded is the avoidable part: rewarding the
    move itself.
    """
    rising = series(vol_pct=30, drift=0.9, seed=5, bars=200)
    falling = series(vol_pct=30, drift=-0.9, seed=5, bars=200)
    # Rebased so both end at the same price, isolating the path from the
    # level. Share volume and volatility are already identical by
    # construction, so liquidity is the only thing the path could move.
    rising["close"] *= 100.0 / float(rising["close"].iloc[-1])
    falling["close"] *= 100.0 / float(falling["close"].iloc[-1])

    result = rank_candidates(
        {"ROSE": rising, "FELL": falling},
        as_of="2026-01-01",
        config=ScreenConfig(target=2, min_history=60, lookback=120),
    )
    measured = {c.ticker: c for c in result.ranked}
    assert len(measured) == 2

    # Asserted on the metric rather than the score. The score is rank-based,
    # and with two candidates a rank turns any difference at all -- including
    # a 0.4% wobble in realised volatility -- into a full separation, so it
    # cannot express "these are the same".
    assert measured["ROSE"].liquidity == pytest.approx(
        measured["FELL"].liquidity, abs=1e-12
    ), (
        f"liquidity moved with the path: {measured['ROSE'].liquidity} against "
        f"{measured['FELL'].liquidity} -- a return is reaching the rank"
    )

    # And the surviving difference is volatility noise, not direction: a
    # tenth of a percent, against the 90% move that separates the two paths.
    assert abs(measured["ROSE"].volatility - measured["FELL"].volatility) < 0.01


def test_the_screen_reads_no_return_series_at_all():
    """The same property, asserted structurally.

    Volatility is computed from returns, which is legitimate -- it is a
    measure of spread, not of direction. What must never appear is a
    *cumulative* or *total* return, which is the thing that would encode
    which way an instrument went.
    """
    import inspect

    from cfdinvest.universe import screen

    source = inspect.getsource(screen)
    for forbidden in ("cumprod", "cumsum", "total_return", "iloc[-1] / ", "pct_change(len"):
        assert forbidden not in source, (
            f"{forbidden!r} appears in the screen -- that is a directional "
            "measure, not a dispersion one"
        )
    # And volatility must be a standard deviation, i.e. sign-blind.
    assert ".std()" in source


def test_too_little_history_is_unranked_rather_than_scored_zero():
    """M173: absence sorting itself last as though it had been measured.

    A zero score puts a new listing at the bottom of the ranking, which
    reads as "measured and found wanting" rather than "not measured".
    """
    result = rank_candidates(
        {
            "ESTABLISHED": series(bars=300, seed=2),
            "NEWLY_LISTED": series(bars=30, seed=3),
        },
        as_of="2026-01-01",
        config=ScreenConfig(target=2, min_history=120, lookback=120),
    )

    unranked = {c.ticker: c for c in result.unranked}
    assert "NEWLY_LISTED" in unranked, "a 30-bar instrument was ranked anyway"
    assert unranked["NEWLY_LISTED"].score is None, "absence was scored"
    assert "30 bars" in unranked["NEWLY_LISTED"].unranked
    assert "NEWLY_LISTED" not in result.selected


def test_an_illiquid_instrument_loses_to_a_liquid_one():
    """Liquidity is weighted first, and for a reason.

    An instrument you cannot get out of at a sane price is not tradable
    whatever else is true of it.
    """
    result = rank_candidates(
        {
            "LIQUID": series(volume=50_000_000, vol_pct=30, seed=4),
            "THIN": series(volume=30_000, vol_pct=30, seed=4),
        },
        as_of="2026-01-01",
        config=ScreenConfig(target=1, min_history=60, lookback=120),
    )
    assert result.selected == ("LIQUID",)


def test_the_sector_cap_applies_or_says_it_did_not():
    """M174: a cap that silently does nothing.

    Without a sector map the cap cannot be applied, and a result that looked
    identical either way would let a single-sector universe pass as a
    diversified one.
    """
    prices = {
        f"TECH{i}": series(volume=40_000_000 - i * 1000, seed=10 + i)
        for i in range(4)
    }
    prices["BANK"] = series(volume=1_000_000, seed=99)
    sectors = {**{f"TECH{i}": "technology" for i in range(4)}, "BANK": "financials"}

    capped = rank_candidates(
        prices, as_of="2026-01-01", sectors=sectors,
        config=ScreenConfig(target=4, min_history=60, lookback=120, max_per_sector=2),
    )
    chosen_sectors = [sectors[t] for t in capped.selected]
    assert chosen_sectors.count("technology") <= 2, (
        f"the cap did not bind: {capped.selected}"
    )
    assert capped.sector_capped, "nothing was recorded as capped"

    # And with no map, the result says the cap was not applied rather than
    # pretending it was.
    uncapped = rank_candidates(
        prices, as_of="2026-01-01",
        config=ScreenConfig(target=4, min_history=60, lookback=120, max_per_sector=2),
    )
    assert any("sector cap was not applied" in note for note in uncapped.notes)


def test_the_result_records_every_candidate_not_only_the_winners():
    """A selection you cannot audit is the thing this replaces.

    The rejected names and their scores are the evidence that the rule was
    followed, so they travel with the choice rather than being recomputed
    later from data that will have moved.
    """
    prices = {f"T{i}": series(volume=10_000_000 * (i + 1), seed=20 + i) for i in range(6)}
    result = rank_candidates(
        prices, as_of="2026-01-01",
        config=ScreenConfig(target=2, min_history=60, lookback=120),
    )

    assert len(result.selected) == 2
    assert len(result.candidates) == 6, "rejected candidates were discarded"
    for candidate in result.ranked:
        assert candidate.score is not None
        assert candidate.liquidity is not None
    assert result.as_of, "the decision date is not recorded"


def test_a_short_universe_is_reported_rather_than_padded():
    """Filling fewer than asked for is a fact, not a failure to hide."""
    result = rank_candidates(
        {"ONLY": series(seed=31)},
        as_of="2026-01-01",
        config=ScreenConfig(target=5, min_history=60, lookback=120),
    )
    assert result.selected == ("ONLY",)
    assert any("only 1 of 5" in note for note in result.notes)


def test_a_selection_records_what_it_rejected_and_cannot_be_edited(tmp_path, monkeypatch):
    """M175: an audit trail that only holds the winners.

    Being able to re-run the rule is not the same as being able to see what
    it saw. Recomputing a ranking later reads a lake that has moved, and
    would quietly justify whatever was chosen -- which is the failure this
    whole module exists to prevent, reappearing one level up.

    Append-only for the same reason every other observation here is: a
    decision that can be edited after the outcome is known is not evidence.
    """
    import json

    from cfdinvest.persist import storage
    from cfdinvest.universe import screen

    monkeypatch.setattr(storage, "account_root", lambda *a, **k: tmp_path)

    first = rank_candidates(
        {f"T{i}": series(volume=10_000_000 * (i + 1), seed=40 + i) for i in range(5)},
        as_of="2026-08-26",
        config=ScreenConfig(target=2, min_history=60, lookback=120),
    )
    assert screen.record_screen(first) == 1

    stored = screen.load_screens()
    assert len(stored) == 1
    row = stored[0]
    assert row["as_of"].startswith("2026-08-26")
    assert len(row["selected"]) == 2
    assert len(row["candidates"]) == 5, "the rejected candidates were not kept"
    # Every candidate carries the numbers it was judged on.
    for candidate in row["candidates"]:
        assert "liquidity" in candidate and "score" in candidate

    # A second selection appends rather than replacing the first.
    second = rank_candidates(
        {f"T{i}": series(volume=5_000_000 * (i + 1), seed=50 + i) for i in range(5)},
        as_of="2026-09-26",
        config=ScreenConfig(target=2, min_history=60, lookback=120),
    )
    screen.record_screen(second)

    both = screen.load_screens()
    assert len(both) == 2, "the earlier selection was overwritten"
    assert both[0]["as_of"].startswith("2026-08-26"), "history was reordered or lost"


def test_the_daily_lake_keeps_being_refreshed_after_the_move_to_hourly():
    """M176: a lake that quietly stops updating.

    Every scheduled ingest follows ``config.market.interval``. The moment
    the engine moved to hourly bars, that became 1h -- and the daily lake
    stopped being refreshed by anything at all. Nothing broke, which is
    exactly the problem: the universe screen reads daily bars, and so does
    any analysis wanting more than the two years the vendor serves intraday.
    The failure would have surfaced as a screen ranking on months-old
    turnover, with no error anywhere.

    In the daily job rather than the intraday one, because a daily bar
    changes once a day and re-fetching it every fifteen minutes would ask
    the vendor for the same row ninety times a session.
    """
    import inspect

    from cfdinvest.schedule import app

    source = inspect.getsource(app)
    assert '_SCREENING_INTERVAL = "1d"' in source

    daily = source[source.index('elif name == "ingest-daily":"'.rstrip('"')):]
    daily = daily[: daily.index('elif name == "ingest-intraday":')]
    assert "_SCREENING_INTERVAL" in daily, (
        "the daily job does not top up the screening interval"
    )
    assert "_refresh" in daily

    intraday = source[source.index('elif name == "ingest-intraday":'):]
    intraday = intraday[: intraday.index("elif name ==", 10)]
    assert "_SCREENING_INTERVAL" not in intraday, (
        "the intraday job re-fetches daily bars every fifteen minutes"
    )


def test_the_screen_reads_daily_bars_whatever_the_engine_trades():
    """Liquidity is a property of the instrument, not of the bar size.

    Screening on the trading interval would tie the universe to a decision
    it has nothing to do with, and would throw away every year of history
    beyond the two the vendor serves intraday.
    """
    import inspect

    from cfdinvest.universe import screen

    signature = inspect.signature(screen.select_universe)
    assert signature.parameters["interval"].default == "1d"
