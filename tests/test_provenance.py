"""Every tuned parameter names its evidence, and stale evidence is declared.

The failure being prevented, stated once so a reader of this file knows why
it is fussy: on 2026-08-28 this project measured that its cost model
undercharged spread by 1.85x, wrote it down, and kept charging the old
number for two more days because nothing connected the finding to the code.
The same weekend produced a fill path believed to use broker prices and
never tested, a cost gate whose docstring records an unreproducible
measurement, and a breakout lookback whose docstring claims the opposite
sign to what the engine now measures.

Measuring and retracting were already disciplined here. Nothing made a
conclusion *binding*. These tests are that.

Mutations checked
-----------------
* renaming a justified field so the path resolves to nothing -- caught by
  ``test_every_justification_points_at_a_real_setting``;
* changing a configured value without revisiting its evidence -- caught by
  ``test_the_recorded_value_is_the_configured_value``;
* adding a parameter with no justification -- caught by
  ``test_no_tuned_parameter_is_unjustified``;
* letting a justification go stale silently -- caught by
  ``test_stale_evidence_is_declared_rather_than_forgotten``;
* leaving a re-measured entry on the queue -- caught by
  ``test_the_queue_holds_only_things_that_are_actually_stale``.
"""

from __future__ import annotations

import dataclasses

import pytest

from cfdinvest import provenance
from cfdinvest.config import load_config
from cfdinvest.schedule.app import _backtest_config


class _Args:
    interval = None
    start = end = tickers = universe = account = None


@pytest.fixture(scope="module")
def config():
    """The production BacktestConfig, as the daemon would build it."""
    return _backtest_config(_Args(), load_config())


def _resolve(config, path):
    """Follow a dotted path the way ``analytics.apply_overrides`` does."""
    node = config
    for part in path.split("."):
        if not hasattr(node, part):
            raise AttributeError(path)
        node = getattr(node, part)
    return node


def test_every_justification_points_at_a_real_setting(config):
    """A justification for a field that no longer exists is worse than none:
    it reads as evidence while describing nothing."""
    for justification in provenance.JUSTIFICATIONS:
        try:
            _resolve(config, justification.path)
        except AttributeError:
            pytest.fail(
                f"{justification.path} has a recorded justification but does "
                "not resolve in the production config; the field was renamed "
                "or removed and its evidence was left behind"
            )


def test_the_recorded_value_is_the_configured_value(config):
    """Changing a value without revisiting the evidence is how a parameter
    ends up justified by a measurement of a different parameter."""
    for justification in provenance.JUSTIFICATIONS:
        actual = _resolve(config, justification.path)
        assert actual == justification.value, (
            f"{justification.path} is configured as {actual!r} but its "
            f"justification describes {justification.value!r}. Either the "
            "change was made without re-measuring, or the evidence was not "
            "updated when it was."
        )


def test_no_tuned_parameter_is_unjustified():
    """Adding a knob without evidence must cost something at the time, or it
    costs far more later when nobody can say why it holds its value."""
    justified = {j.path for j in provenance.JUSTIFICATIONS}
    missing = [p for p in provenance.PENDING_REMEASUREMENT if p not in justified]
    assert not missing, (
        f"listed for re-measurement but carrying no justification: {missing}"
    )


def test_stale_evidence_is_declared_rather_than_forgotten():
    """The point of the whole file.

    A justification taken under a superseded feed or cost model is not
    evidence for the value that runs now. It may be declared as pending, but
    it may not pass silently -- that silence is exactly what let a known
    spread undercharge survive in the code for two days after it was
    measured.
    """
    undeclared = [
        j.path for j in provenance.stale()
        if j.path not in provenance.PENDING_REMEASUREMENT
    ]
    assert not undeclared, (
        "these parameters are justified by measurements taken under a "
        f"different feed or cost model and are not declared as pending: "
        f"{undeclared}. Either re-measure them under "
        f"{provenance.CURRENT_FEED} with the {provenance.CURRENT_COST_MODEL} "
        "cost model, or add them to PENDING_REMEASUREMENT."
    )


def test_the_queue_holds_only_things_that_are_actually_stale():
    """A queue that keeps entries after they are fixed stops being read."""
    stale_paths = {j.path for j in provenance.stale()}
    done = [p for p in provenance.PENDING_REMEASUREMENT if p not in stale_paths]
    assert not done, (
        f"{done} are on the re-measurement queue but their evidence is "
        "current; remove them from PENDING_REMEASUREMENT"
    )


def test_a_justification_names_something_that_can_be_run_again():
    """"It was measured" is not a justification anybody can check."""
    for justification in provenance.JUSTIFICATIONS:
        assert justification.source.strip(), f"{justification.path} names no source"
        assert justification.figure.strip(), f"{justification.path} states no figure"


def test_the_current_conditions_match_what_is_configured(config):
    """The registry's notion of "current" has to track the real config, or
    staleness is computed against a fiction."""
    # BacktestConfig.interval is None when the CLI did not override it: the
    # pipeline resolves it against market.interval at load time
    # (analysis_frame_from_repository), so the effective feed is the app
    # config's. Asserting the BacktestConfig field alone would compare
    # against None and pass or fail for the wrong reason.
    effective = config.interval or load_config().market.interval
    assert effective == provenance.CURRENT_FEED, (
        f"provenance believes decisions are taken on "
        f"{provenance.CURRENT_FEED!r} but the effective feed is "
        f"{effective!r}; every staleness verdict in this module is being "
        "computed against the wrong feed"
    )
    charging_measured = bool(config.execution.use_measured_spreads)
    expected = provenance.CURRENT_COST_MODEL == "measured"
    assert charging_measured == expected, (
        f"provenance believes the cost model is "
        f"{provenance.CURRENT_COST_MODEL!r} but use_measured_spreads is "
        f"{charging_measured}"
    )


def test_changing_the_cost_model_invalidates_everything_measured_under_it():
    """Not a test of today's state -- a test of the mechanism.

    The registry must react to a change in conditions rather than merely
    recording a date, because a date says when somebody looked and not
    whether what they looked at still resembles what runs.
    """
    measured_now = provenance.current()
    assert measured_now, "expected at least one justification under current conditions"

    # Pretend the cost model moved on. Everything justified under the old one
    # must go stale, including the entries that were current a moment ago.
    moved = tuple(
        dataclasses.replace(j, cost_model="something_else")
        for j in provenance.JUSTIFICATIONS
    )
    assert len(provenance.stale(moved)) == len(provenance.JUSTIFICATIONS)
    assert not provenance.current(moved)
