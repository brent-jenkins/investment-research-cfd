"""The forward experiment: many simulated arms, one that reaches an account.

The shadow book is how anything about this strategy gets verified, because
no backtest can settle a question the backtest's own window decides. Arms
are nearly free -- a pass is arithmetic over signals and prices already
computed, and the book stakes a fixed GBP 1,000 rather than a share of a
balance -- so the count is bounded by what can be interpreted rather than by
capital.

That separation is the safety property. The Capital bridge mirrors
``cohorts_for(...)[0]`` and nothing else, so exactly one arm can ever reach
a real account no matter how many are added. If that ordering broke, an
experimental setting would start dealing.

M422 an arm carrying overrides placed first, so a variant reaches the account
M423 overrides declared and never applied, so every arm silently runs the
     baseline and the experiment measures nothing
M424 the confidence arms renamed or dropped, throwing away a forward record
     running since 2026-08-26
M425 a variant arm differing in more than one setting, so a difference
     cannot be attributed
"""

from __future__ import annotations

import inspect

import pytest

from cfdinvest.advisor.papertrade_run import (
    VARIANT_ARMS,
    Cohort,
    cohorts_for,
    run_all_cohorts,
)


def test_the_baseline_is_first_and_runs_the_strategys_own_settings():
    """M422: an experimental setting reaching a funded account.

    The bridge mirrors the first arm. It must therefore be the one with no
    overrides -- the configuration the engine is actually running.
    """
    from cfdinvest.config import load_config

    arms = cohorts_for(load_config().strategy)
    assert arms[0].name.startswith("c")
    assert not arms[0].overrides, (
        f"the first arm carries {dict(arms[0].overrides)}; it is the one "
        "mirrored to Capital and must run the strategy's own settings"
    )
    assert arms[0].threshold == pytest.approx(
        load_config().strategy.entry_confidence
    )


def test_exactly_one_arm_can_reach_an_account():
    """The safety property, asserted at the seam rather than assumed.

    Adding arms must never add anything that deals. Both places that pick
    the mirrored arm take index zero.
    """
    from cfdinvest.risk import snapshot
    from cfdinvest.schedule import app

    for module, name in ((app, "app"), (snapshot, "risk.snapshot")):
        source = inspect.getsource(module)
        assert "cohorts_for(" in source
        assert "[0][0]" in source or "[0].name" in source, (
            f"{name} does not take the first arm; the mirrored arm is "
            "whichever one happens to come back"
        )


def test_an_arms_overrides_actually_reach_the_pass():
    """M423: a declared variation that never happens.

    Every arm would produce a book, they would all be identical, and the
    experiment would look like it was running while measuring nothing.
    """
    seen = []

    def fake_pass(latest, prices, **kwargs):
        seen.append((kwargs.get("cohort"), kwargs.get("time_stop_days"),
                     kwargs.get("cooldown_days"), kwargs.get("blackout_days")))
        return {"cohort": kwargs.get("cohort"), "opened": 0}

    import cfdinvest.advisor.papertrade_run as module

    original = module.run_paper_pass
    module.run_paper_pass = fake_pass
    try:
        run_all_cohorts([], {}, config=None)
    finally:
        module.run_paper_pass = original

    by_name = {name: rest for name, *rest in
               [(s[0], s[1], s[2], s[3]) for s in seen]}
    assert "ts14" in by_name, "the variant arms did not run"
    assert by_name["ts14"][0] == pytest.approx(0.14), (
        "the ts14 arm ran with the baseline time stop; its override was lost"
    )
    assert by_name["ts60"][0] == pytest.approx(0.60)
    assert by_name["cool0"][1] == pytest.approx(0.0)
    assert by_name["blk0"][2] == 0


def test_the_confidence_arms_keep_their_names():
    """M424: throwing away a forward record to tidy a list.

    c35/c50/c60/c70 have been accumulating since 2026-08-26. Renaming one
    starts its history again, and the loss is invisible -- the book simply
    has fewer rows under the new name.
    """
    from cfdinvest.config import load_config

    names = {a.name for a in cohorts_for(load_config().strategy)}
    assert {"c50", "c60", "c70"} <= names
    assert any(n.startswith("c") and n[1:].isdigit() for n in names)


def test_each_variant_changes_exactly_one_thing():
    """M425: a variant that cannot be attributed.

    An arm differing from the baseline in two settings answers no question:
    whichever way it goes, either change could be responsible.
    """
    for name, overrides in VARIANT_ARMS:
        assert len(overrides) == 1, (
            f"the {name} arm varies {len(overrides)} settings "
            f"({sorted(overrides)}); a difference could not be attributed"
        )


def test_variant_arms_sit_at_the_baseline_bar():
    """A variant that also moved the confidence bar would confound the two."""
    from cfdinvest.config import load_config

    strategy = load_config().strategy
    variants = {n for n, _ in VARIANT_ARMS}
    for arm in cohorts_for(strategy):
        if arm.name in variants:
            assert arm.threshold == pytest.approx(strategy.entry_confidence), (
                f"{arm.name} varies a setting AND the confidence bar"
            )


def test_arms_are_unique():
    from cfdinvest.config import load_config

    names = [a.name for a in cohorts_for(load_config().strategy)]
    assert len(names) == len(set(names)), f"duplicate arm names: {names}"
