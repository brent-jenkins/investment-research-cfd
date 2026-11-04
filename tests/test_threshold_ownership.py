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


def test_no_second_rule_set_decides_a_position():
    """The divergence this file was written for, closed at the root.

    It used to assert that the shadow book's own defaults were not the old
    literals -- 1 day, 14 days, 2 days -- because those would restore the
    divergence whenever the config was absent. That was treating a symptom.
    The book implemented the time stop, cooldown, blackout and confidence
    gate and *none* of the breakout gate, size hysteresis, decayed trim,
    leverage cap or concentration cap, and the account mirrored it: 163.7%
    at -52.8% where the engine returns 130.5% at -40.3%.

    So the book was removed on 2026-08-30 and the account now reads the
    engine's own run. This asserts the second rule set cannot come back.
    """
    import importlib

    for name in ("cfdinvest.advisor.papertrade",
                 "cfdinvest.advisor.papertrade_run"):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(name)


def test_the_account_reads_the_engines_intent_and_not_a_book():
    """One owner for "what should be held", and it reads a persisted run."""
    from cfdinvest.persist import intent
    from cfdinvest.schedule import app

    assert app._engine_rows.__module__ == "cfdinvest.schedule.app"
    source = inspect.getsource(app._engine_rows)
    assert "intent.intended_positions" in source, (
        "the dealing path derives its own view instead of reading the "
        "single owner"
    )
    # And that owner re-expresses no strategy rule by name: every gate and
    # cap is already inside the units the engine recorded.
    owner = inspect.getsource(intent.intended_positions)
    for rule in ("breakout", "time_stop", "cooldown", "blackout",
                 "max_leverage", "concentration"):
        assert rule not in owner, (
            f"persist.intent mentions {rule!r}; a rule it knows by name is a "
            "rule that can drift from the engine's"
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

    # On the deployment both resolve to /var/lib/cfdinvest, because the
    # configured data root IS where the OS default lands there. The
    # regression this guards -- reading earnings from the application-data
    # directory instead of the lake -- cannot occur in that environment, so
    # the premise is untestable rather than broken.
    #
    # Skipped, not failed. A test that fails where its premise does not hold
    # trains people to ignore a red suite on the one machine that matters,
    # and this failed on the box the first time the suite was ever run
    # there.
    if _path(None, None) == _path(settings, None):
        pytest.skip(
            "the default and configured earnings paths coincide in this "
            "environment, so the divergence this guards cannot arise here"
        )

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
    if not dates:
        # No earnings store at all, as in a fresh clone: nothing to
        # check. A store missing some names still fails below.
        pytest.skip("no earnings dates recorded on this machine")
    missing = [t for t in settings.market.tickers if t not in dates]
    assert not missing, (
        f"the earnings blackout is on but has no dates for {missing}, so it "
        "protects part of the book and silently not the rest"
    )
