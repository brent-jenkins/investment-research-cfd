"""EDGAR data for the screen's candidates, not only the traded list.

Filings and reporting dates covered nineteen names of a forty-nine name
pool. That is the same blind spot the spread readings had: a selection rule
cannot weigh a filing rate for an instrument whose filings were never
fetched, and the project's own record says non-price evidence is the only
untried direction -- "every price-derived feature tested against 2,018
historical round trips collapsed to |t|<1".

M389 the candidates never fetched, so a rule can only ever see the incumbents
M390 two writers taking turns deleting each other's half of the store
M391 a traded instrument fetched twice in one pass
M392 the traded names losing their place, so a partial fetch drops them first
"""

from __future__ import annotations

import pytest

from cfdinvest.schedule.runner import _with_candidates


class Universe:
    def __init__(self, candidates):
        self.candidates = tuple(candidates)


class Config:
    def __init__(self, candidates):
        self.universe = Universe(candidates)


def test_the_candidates_are_fetched_as_well_as_the_traded_list():
    """M389: a rule that can only ever see the instruments already held."""
    names = _with_candidates(["AAA", "BBB"], Config(["CCC", "DDD"]))
    assert set(names) == {"AAA", "BBB", "CCC", "DDD"}


def test_an_instrument_in_both_lists_is_fetched_once():
    """M391: a duplicate request, and a duplicated row downstream."""
    names = _with_candidates(["AAA", "BBB"], Config(["BBB", "CCC"]))
    assert names.count("BBB") == 1
    assert len(names) == len(set(names))


def test_the_traded_names_keep_their_place_at_the_front():
    """M392: order lost, so a truncated fetch drops the traded names first.

    The reference data feeds the earnings blackout, which only matters for
    instruments actually held. If the fetch is cut short -- a rate limit, a
    timeout -- the names that must not be missed should already be done.
    """
    names = _with_candidates(["AAA", "BBB"], Config(["ZZZ", "AAA", "YYY"]))
    assert names[:2] == ["AAA", "BBB"]


def test_an_empty_or_absent_pool_changes_nothing():
    """A configuration with no candidates must behave as it always did."""
    assert _with_candidates(["AAA"], Config([])) == ["AAA"]

    class Bare:
        pass

    assert _with_candidates(["AAA"], Bare()) == ["AAA"]
    assert _with_candidates([], Config(["BBB"])) == ["BBB"]


def test_one_writer_covers_the_union(monkeypatch):
    """M390: two fetches taking turns deleting each other.

    `store_filings` and `store_earnings_dates` *rewrite* -- deliberately,
    because EDGAR restates and amends. So a second writer covering only the
    candidates would drop the traded names on every pass, and a traded-only
    writer would drop the candidates. There is one fetch of the union.
    """
    import inspect

    from cfdinvest.schedule import runner

    source = inspect.getsource(runner._refresh)
    assert "_with_candidates(tickers, config)" in source, (
        "the refresh still fetches the traded list alone"
    )
    # And exactly one call site, so the halves cannot diverge.
    assert source.count("_refresh_events(") == 1


def test_the_shipped_pool_is_covered_end_to_end():
    """The real configuration, not a fixture."""
    from cfdinvest.config import load_config

    settings = load_config()
    names = _with_candidates(list(settings.market.tickers), settings)

    for ticker in settings.market.tickers:
        assert ticker in names, f"{ticker} is traded and would not be fetched"
    for candidate in settings.universe.candidates:
        assert candidate in names, f"{candidate} can be screened and is never fetched"
    assert len(names) == len(set(names))
