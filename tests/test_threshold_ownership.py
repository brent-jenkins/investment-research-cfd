"""One owner per threshold, across the engine and the book.

The house rule says a limit lives in exactly one config object and is read
from there, because two copies drift and the day they drift one of them is
wrong. Three had drifted, all in the same direction and all invisibly.

`run_paper_pass` carried its own literal defaults -- a 1-day time stop, a
14-day re-entry cooldown and a 2-day earnings blackout -- and nothing ever
passed it the configured values. The engine meanwhile read 0.29, 0.083 and
0 from `StrategyConfig`. So the shadow book, which is the forward test and
which the demo account mirrors trade for trade, was running a materially
different strategy from the backtest that justifies it: holding roughly
three times as long, refusing re-entry for a fortnight rather than two
hours, and sitting out earnings the engine traded straight through.

The engine's own copy of the blackout was worse than wrong, it was inert.
`_execution_config` called `load_earnings_dates()` with no config, which
resolves to the OS application-data directory rather than the configured
lake, found nothing, and returned an empty mapping -- which the engine
reads as "no instrument is blacked out". The rule had never once bitten,
whatever the configured value said, and it failed open in silence because
the absence is logged at debug.

M398 the book taking its own literal defaults instead of the config
M399 the engine's blackout reading a directory nothing writes to
M400 an absent config resurrecting the old literals as fallbacks
M401 a configured blackout that cannot bite, passing without complaint
"""

from __future__ import annotations

import inspect

import pytest


def test_the_book_takes_its_thresholds_from_the_configured_strategy():
    """M398: the divergence itself, asserted at the seam.

    Checked on the source rather than by running a pass, because the
    property is *where the number comes from*. A pass that happened to
    agree with the config would prove nothing about the next one.
    """
    from cfdinvest.advisor import papertrade_run

    source = inspect.getsource(papertrade_run.run_paper_pass)
    for name, field in (
        ("time_stop_days", "time_stop_days"),
        ("cooldown_days", "reentry_cooldown_days"),
        ("blackout_days", "earnings_blackout_days"),
    ):
        assert f'getattr(strategy, "{field}"' in source, (
            f"{name} does not come from the strategy config"
        )


def test_no_threshold_is_declared_twice_as_a_literal():
    """M400: the fallbacks must not be the old literals.

    A fallback of 1 / 14 / 2 would restore the divergence whenever the
    config was absent, which is exactly the case that is hardest to notice.
    """
    from cfdinvest.advisor import papertrade_run

    signature = inspect.signature(papertrade_run.run_paper_pass)
    for name in ("time_stop_days", "cooldown_days", "blackout_days"):
        default = signature.parameters[name].default
        assert default is None, (
            f"{name} still carries its own default of {default!r}; the "
            "config is meant to be the only owner"
        )


def test_the_engine_is_given_the_configured_lake_for_earnings():
    """M399: the loader pointed at a directory nothing writes to."""
    from cfdinvest.schedule import app

    source = inspect.getsource(app._execution_config)
    assert "load_earnings_dates(config, account)" in source, (
        "the engine still reads earnings dates from the default path"
    )


def test_the_earnings_path_actually_differs_between_the_two_readings():
    """The bug only exists because these resolve differently. If a refactor
    made them the same, the test above would be guarding nothing."""
    from cfdinvest.config import load_config
    from cfdinvest.ingestion.earnings import _path

    settings = load_config()
    assert _path(None, None) != _path(settings, None), (
        "the default path and the configured path now agree, so the "
        "regression this guards against can no longer happen -- check "
        "whether this test still earns its place"
    )


def test_the_engine_and_the_book_agree_on_every_shared_threshold():
    """The property, end to end, on the shipped configuration.

    Not asserting the values -- those are the config's to choose -- but
    that both paths resolve to the same ones.
    """
    from cfdinvest.config import load_config
    from cfdinvest.schedule.app import _execution_config

    settings = load_config()
    strategy = settings.strategy
    execution = _execution_config(list(settings.market.tickers), strategy, settings)

    assert execution.time_stop_days == strategy.time_stop_days
    assert execution.reentry_cooldown_days == strategy.reentry_cooldown_days
    assert execution.earnings_blackout_days == strategy.earnings_blackout_days


def test_a_configured_blackout_has_dates_to_work_with():
    """M401: a rule configured on and unable to bite.

    "Not measured" and "measured and fine" are different statements. If the
    blackout is switched on, every traded instrument needs reporting dates,
    or the rule silently covers only some of the book.
    """
    from cfdinvest.config import load_config
    from cfdinvest.ingestion.earnings import load_earnings_dates

    settings = load_config()
    if not settings.strategy.earnings_blackout_days:
        pytest.skip("blackout is off; nothing to cover")

    dates = load_earnings_dates(settings)
    missing = [t for t in settings.market.tickers if t not in dates]
    assert not missing, (
        f"the earnings blackout is on but has no dates for {missing}, so it "
        "protects part of the book and silently not the rest"
    )
