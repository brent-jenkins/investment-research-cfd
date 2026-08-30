"""The universe screen's runner: a rule that can be re-run and is recorded.

The screen existed and had no runner. Every screen this project ran was a
script written for the occasion and deleted, so the rule was reproducible in
principle and, in practice, only by whoever still had the file -- the same
defect the screen exists to fix, one level up. And ``record_screen`` had been
written and never called, so the store held nothing to compare against.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from cfdinvest.universe import cli as universe_cli


# --------------------------------------------------------------------------- #
# M260 the runner editing the universe it is meant to report on
# M261 a screen that reports success without recording
# M262 the pool losing the instruments that are actually traded
# M263 the sector cap binding silently
# M264 the monthly job firing more than once a month
# --------------------------------------------------------------------------- #
@pytest.fixture
def screened_lake(tmp_path, monkeypatch):
    """A lake with daily bars for a small pool, and a config naming it."""
    from cfdinvest import storage
    from cfdinvest.config import (
        AppConfig, MarketDataConfig, PathConfig, UniverseConfig,
    )
    from cfdinvest.schema import TICKER, TIMESTAMP

    pool = ["AAA", "BBB", "CCC", "DDD", "EEE"]
    calendar = pd.bdate_range("2025-01-01", periods=300, tz="UTC", name=TIMESTAMP)
    config = AppConfig(
        paths=PathConfig(data_root=tmp_path / "data"),
        market=MarketDataConfig(tickers=("AAA", "BBB"), interval="1d"),
        universe=UniverseConfig(
            candidates=tuple(pool),
            sectors={t: ("tech" if t < "CCC" else "other") for t in pool},
        ),
    )
    rng = np.random.default_rng(5)
    for offset, ticker in enumerate(pool):
        close = 100 * np.exp(np.cumsum(rng.normal(0.0003, 0.01 + offset * 0.002, 300)))
        frame = pd.DataFrame(
            {"open": close, "high": close * 1.01, "low": close * 0.99,
             "close": close, "adj_close": close,
             "volume": float(1e6 * (offset + 1)), TICKER: ticker},
            index=calendar,
        )
        storage.write_parquet(
            frame, storage.market_path(config.paths, ticker, "1d")
        )
    return config


class _Args:
    as_of = None
    target = None
    no_record = False
    interval = "1d"
    account = None
    limit = 10


def test_the_runner_never_edits_the_universe(screened_lake, capsys):
    """M260: a scheduled job that rebalances on a ranking that predicts nothing.

    The screen ranks tradability, not expected return -- its rank does not
    correlate with the following month's contribution, its top N loses to
    keeping everything, and its selection does not beat a random draw from
    the same pool once volatility is controlled for. A runner that rewrote
    ``market.tickers`` on that would be acting with far more confidence than
    the evidence carries.

    Asserted structurally as well as behaviourally: nothing in the module
    writes a config at all.
    """
    before = tuple(screened_lake.market.tickers)
    universe_cli._command_screen(_Args(), screened_lake)
    assert tuple(screened_lake.market.tickers) == before

    source = Path("cfdinvest/universe/cli.py").read_text(encoding="utf-8")
    for forbidden in ("write_text", "save_config", "dump", "config.toml"):
        assert forbidden not in source, (
            f"the runner mentions {forbidden!r}; it must never write configuration"
        )


def test_a_screen_that_could_not_record_says_so(screened_lake, monkeypatch, capsys):
    """M261: a comparison that will quietly not exist next month.

    The whole point of recording is having something to diff against later.
    A run that believes it recorded and did not leaves no trace of the
    failure, and the absence only shows up as a gap in a history nobody is
    watching yet.
    """
    from cfdinvest.universe import screen as screen_module

    monkeypatch.setattr(screen_module, "record_screen", lambda *a, **k: 0)
    code = universe_cli._command_screen(_Args(), screened_lake)

    assert code == universe_cli.EXIT_FAILED
    assert "Could not record" in capsys.readouterr().out


def test_the_traded_names_are_always_ranked(screened_lake):
    """M262: a screen that cannot see what is held.

    A pool that omits a traded instrument cannot say whether the rule would
    keep it, so the difference it reports would be silently incomplete.
    """
    config = dataclasses.replace(
        screened_lake,
        universe=dataclasses.replace(
            screened_lake.universe, candidates=("CCC", "DDD")
        ),
    )
    pool = universe_cli._pool(config)

    assert set(config.market.tickers) <= set(pool)
    assert len(pool) == len(set(pool)), "an instrument is ranked twice"


def test_an_empty_pool_falls_back_to_what_is_traded(screened_lake):
    """No pool is a re-ranking of the book, not an error."""
    config = dataclasses.replace(
        screened_lake,
        universe=dataclasses.replace(screened_lake.universe, candidates=()),
    )
    assert universe_cli._pool(config) == list(config.market.tickers)


def test_the_sector_cap_says_when_it_bound(screened_lake, capsys):
    """M263: a limit that changes the answer and does not mention it.

    The cap was inert for the screen's whole life -- every run before a
    sector map existed reported "no sector map supplied, so the sector cap
    was not applied". The first run with one displaced sixteen instruments,
    nine of them held. A constraint that strong must not arrive silently.
    """
    args = _Args()
    args.target = 3
    config = dataclasses.replace(
        screened_lake,
        universe=dataclasses.replace(screened_lake.universe, max_per_sector=1),
    )
    universe_cli._command_screen(args, config)
    out = capsys.readouterr().out

    assert "sector cap" in out.lower()
    assert "displaced" in out.lower()


def test_the_screen_is_owed_once_a_day(screened_lake):
    """M264, restated: the drift record must still be readable.

    This was monthly, and the reasoning was sound while the selection was
    only a report -- consecutive rankings correlate +0.97 to +0.99, so daily
    meant twenty near-identical records a month with the drift buried among
    them. ``universe.follow_screen`` made the selection an instruction too,
    and an instruction a month behind the conditions it was chosen for is a
    different and worse problem.

    The legibility objection is answered at the read side instead: every
    screen is recorded and ``universe history`` collapses unchanged runs.
    Recording only the changes was the other option and the wrong one -- it
    cannot distinguish "the selection did not move" from "the screen did not
    run", and today has supplied three separate examples of why that matters.
    """
    from cfdinvest.schedule import daemon

    history = {
        "poll-live-quotes": "2026-08-27T12:00:00Z",
        "ingest-daily": "2026-08-27T03:00:00Z",
        "monitoring-snapshot": "2026-08-27T03:05:00Z",
    }
    assert "screen-universe" in daemon.jobs_due("2026-08-27T12:01:00Z", history)

    # Once a day, and not again the same day.
    ran = dict(history, **{"screen-universe": "2026-08-27T03:10:00Z"})
    assert "screen-universe" not in daemon.jobs_due("2026-08-27T12:01:00Z", ran)
    assert "screen-universe" not in daemon.jobs_due("2026-08-27T23:59:00Z", ran)

    # And it comes round tomorrow, once, however late the machine was.
    assert "screen-universe" in daemon.jobs_due("2026-08-28T03:11:00Z", ran)
    assert "screen-universe" in daemon.jobs_due("2026-09-20T09:00:00Z", ran)


def test_the_history_collapses_unchanged_runs_but_records_them_all(screened_lake, capsys):
    """The answer to what made the screen monthly.

    A daily screen produces long runs of identical selections. They are all
    recorded -- the record must be able to say the screen ran -- and the
    reading collapses them so that a change is visible rather than buried.
    """
    from cfdinvest.universe.screen import record_screen, ScreenResult, load_screens

    for day, names in (
        ("2026-08-01", ("AAA", "BBB")),
        ("2026-08-02", ("AAA", "BBB")),
        ("2026-08-03", ("AAA", "BBB")),
        ("2026-08-04", ("AAA", "CCC")),
    ):
        record_screen(
            ScreenResult(as_of=f"{day}T03:10:00Z", selected=names), screened_lake
        )

    assert len(load_screens(screened_lake)) == 4, "a screen went unrecorded"

    universe_cli._command_history(_Args(), screened_lake)
    out = " ".join(capsys.readouterr().out.split())

    assert "4 selection(s) recorded" in out
    assert "2 distinct" in out, "unchanged runs were not collapsed"
    assert "(3 screens)" in out, "the collapsed run does not say how many"
    assert "AAA, CCC" in out, "the change that matters is missing"


def test_a_screen_round_trips_into_the_history(screened_lake, capsys):
    """Recorded, and readable back -- the pair that did not exist before."""
    universe_cli._command_screen(_Args(), screened_lake)
    capsys.readouterr()

    universe_cli._command_history(_Args(), screened_lake)
    out = capsys.readouterr().out
    assert "1 selection(s) recorded" in out

    universe_cli._command_drift(_Args(), screened_lake)
    drift = capsys.readouterr().out
    assert "traded but not selected" in drift


def test_every_recorded_screen_carries_its_decision_date(screened_lake, capsys):
    """M265: a selection with no date.

    Without a cutoff the screen records an empty ``as_of``, and a selection
    that cannot be ordered against the others or replayed is most of the way
    to not being a record at all. It showed up as a "?" row in the drift
    table, next to a dated one.

    Stamped with now when no cutoff is given, so ``--as-of`` replays a past
    decision and its absence still dates the record.
    """
    from cfdinvest.universe.screen import load_screens

    universe_cli._command_screen(_Args(), screened_lake)
    capsys.readouterr()

    rows = load_screens(screened_lake, None)
    assert rows, "nothing was recorded"
    for row in rows:
        stamp = str(row.get("as_of") or "")
        assert stamp.strip(), "a selection was recorded with no decision date"
        assert pd.Timestamp(stamp) is not pd.NaT


def test_the_daily_ingest_fetches_every_candidate_the_screen_ranks(
    tmp_path, monkeypatch
):
    """M266: named in the configuration, never fetched.

    The screen ranks whatever the lake holds. With only the traded universe
    in it, a screen of forty-nine candidates silently becomes a re-ranking
    of the twenty held names and reports "it would add: nothing" -- the
    reassuring answer, reached by being unable to see the alternatives.

    This is the third thing today found named in the configuration and never
    fetched, after the currency pairs twice over. The pattern is the same
    each time: a list that is correct, and nothing that acts on it.
    """
    from cfdinvest.config import (
        AppConfig, MarketDataConfig, PathConfig, UniverseConfig,
    )
    from cfdinvest.schedule import app
    from cfdinvest.schedule import runner as runner_module

    config = AppConfig(
        paths=PathConfig(data_root=tmp_path),
        market=MarketDataConfig(tickers=("AAA", "BBB"), interval="1h"),
        universe=UniverseConfig(candidates=("AAA", "BBB", "CCC", "DDD")),
    )

    asked: list[list[str]] = []
    monkeypatch.setattr(
        runner_module, "_refresh",
        lambda repository, tickers, settings: asked.append(
            [str(t) for t in tickers]
        ),
    )

    app.run_daemon_job("ingest-daily", parsed_run_once(), config)

    assert asked, "the daily job fetched nothing"
    # The last call is the daily top-up; the first is the trading interval.
    daily = asked[-1]
    assert {"CCC", "DDD"} <= set(daily), (
        f"the daily top-up asked for {daily} -- the screen's candidates are "
        "missing, so it can only ever rank what is already traded"
    )
    assert {"AAA", "BBB"} <= set(daily), "the traded universe was dropped"

    # And the intraday job fetches them too, since 2026-08-30.
    #
    # This assertion used to be its inverse -- that the intraday job did NOT
    # ask for candidates, because they are not traded and the screen ranks on
    # daily bars. Both halves of that were true and the conclusion was still
    # wrong, for two reasons found on the same afternoon.
    #
    # A candidate the screen selects tomorrow has no intraday history, so its
    # first tick fetches years of bars inline on a fifteen-minute job. And
    # `universe.screen._spread_series` prefers the capital_1h archive over the
    # live observation store *if the archive exists at all* -- all or nothing,
    # not per instrument -- so an archive kept only for traded names freezes
    # the screen's cost term for everyone else. Seeding the candidates without
    # also keeping them current would have caused exactly the failure that
    # function's own docstring records, arrived at from the other side.
    asked.clear()
    app.run_daemon_job("ingest-intraday", parsed_run_once(), config)
    assert asked, "the intraday job fetched nothing"
    intraday = set(asked[-1])
    assert {"AAA", "BBB"} <= intraday, "the traded universe was dropped"
    assert {"CCC", "DDD"} <= intraday, (
        "the intraday job is not keeping the screen's candidates current; "
        "the first one selected will fetch its whole history inline, and the "
        "screen's spread term will read a frozen archive in the meantime"
    )
def parsed_run_once():
    """Real arguments from the real parser, as the daemon builds them."""
    from cfdinvest.schedule import app

    return app.build_parser().parse_args(["--no-email", "run-once"])
