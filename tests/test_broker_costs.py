"""The broker's recorded terms, as the backtest's cost model.

These cover the join between two layers that used to disagree: the rules
table quotes what Trading 212 charges, and the engine used to charge a flat
benchmark-plus-markup to everything. The properties worth protecting are the
ones that were wrong before, so each test below names the bug it re-catches.

Mutations checked (each reintroduced, each failed):

M52  fee mask follows the currency instead of the recorded flag
     -> test_a_recorded_waiver_beats_the_currency
M53  fee mask follows the recorded flag but ignores an unrecorded ticker's
     currency -> test_an_unrecorded_instrument_falls_back_to_the_currency
M54  the fee mask also gates spread and slippage
     -> test_a_waived_fee_does_not_waive_the_spread
M55  a half-recorded instrument is financed on the side it has
     -> test_an_instrument_missing_one_side_is_left_out_entirely
M56  unpriced() reports only unknown tickers, not half-recorded ones
     -> test_unpriced_names_what_will_fall_back
M57  shorts keep the earning sign of the benchmark model
     -> test_every_recorded_short_is_a_charge_not_an_income
M58  _execution_config leaves FX disabled
     -> test_the_runner_enables_fx_and_charges_on_the_result
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from cfdinvest.execution.fx import (
    FXConfig,
    converting_assets,
    fee_charging_assets,
)
from cfdinvest.livehelper import broker_costs
from cfdinvest.livehelper.t212_rules import rules_from_mapping


# --------------------------------------------------------------------------- #
# A table standing in for the real one, with the awkward cases in it
# --------------------------------------------------------------------------- #
@pytest.fixture
def table():
    """Four instruments covering every combination that has bitten us."""
    return rules_from_mapping(
        {
            # Quoted abroad, charges the fee. The ordinary case.
            "^GSPC": {
                "t212_name": "USA 500", "leverage": 20.0, "currency": "USD",
                "financing_daily_pct_long": 0.0083,
                "financing_daily_pct_short": 0.0139,
                "fx_fee_applies": True,
            },
            # Quoted abroad and charges NO fee. The case the currency rule
            # got backwards.
            "^GDAXI": {
                "t212_name": "Germany 40", "leverage": 20.0, "currency": "EUR",
                "financing_daily_pct_long": 0.0083,
                "financing_daily_pct_short": 0.0139,
                "fx_fee_applies": False,
            },
            # Only one side recorded.
            "HALF": {
                "t212_name": "Half", "leverage": 5.0, "currency": "USD",
                "financing_daily_pct_long": 0.0694,
                "fx_fee_applies": True,
            },
            # Nothing but a name.
            "BARE": {"t212_name": "Bare"},
        },
        "test",
    )


# --------------------------------------------------------------------------- #
# Which instruments pay the fee
# --------------------------------------------------------------------------- #
def test_a_recorded_waiver_beats_the_currency(table):
    """Germany 40 is priced in euros and charges nothing. M52."""
    columns = pd.Index(["^GSPC", "^GDAXI"])
    config = FXConfig(
        enabled=True,
        account_currency="GBP",
        instrument_currencies=broker_costs.instrument_currencies(columns, table),
        fx_fee_applies=broker_costs.fx_fee_flags(columns, table),
    )

    # Both cross a currency boundary, so both need a rate...
    assert list(converting_assets(columns, config)) == [True, True]
    # ...but only one of them pays for it.
    assert list(fee_charging_assets(columns, config)) == [True, False]


def test_an_unrecorded_instrument_falls_back_to_the_currency(table):
    """An absent flag is not a waiver. M53."""
    columns = pd.Index(["^GSPC", "UNKNOWN", "DOMESTIC"])
    config = FXConfig(
        enabled=True,
        account_currency="GBP",
        instrument_currencies={"^GSPC": "USD", "UNKNOWN": "USD"},
        # UNKNOWN and DOMESTIC have no recorded flag.
        fx_fee_applies={"^GSPC": True},
    )

    mask = fee_charging_assets(columns, config)
    assert mask[1], "a foreign instrument with no recorded flag still pays"
    assert not mask[2], "a domestic instrument converts nothing, so pays nothing"


def test_a_waived_fee_does_not_waive_the_spread(table):
    """The waiver is on the fee alone. M54.

    Germany 40 still converts, so the FX spread on it is real. Masking the
    whole conversion out would understate the cost of every European index.
    """
    from cfdinvest.execution.fx import _charges

    config = FXConfig(
        enabled=True, fx_conversion_fee_pct=0.005, fx_spread_bps=25.0,
    )
    amount = np.array([1_000.0, 1_000.0])
    charges = _charges(
        amount, config, with_fee=True,
        converts=np.array([True, True]),
        charges_fee=np.array([True, False]),
    )

    assert charges["fx_conversion_fee"][0] == pytest.approx(5.0)
    assert charges["fx_conversion_fee"][1] == pytest.approx(0.0)
    # The spread is charged on both, waiver or not.
    assert charges["fx_spread"][0] == pytest.approx(charges["fx_spread"][1])
    assert charges["fx_spread"][1] > 0.0


# --------------------------------------------------------------------------- #
# Financing
# --------------------------------------------------------------------------- #
def test_an_instrument_missing_one_side_is_left_out_entirely(table):
    """Half a rate is not a conservative estimate of a rate. M55."""
    overrides = broker_costs.financing_overrides(["^GSPC", "HALF", "BARE"], table)

    assert "^GSPC" in overrides
    assert "HALF" not in overrides, "a long-only rate must not be used"
    assert "BARE" not in overrides


def test_unpriced_names_what_will_fall_back(table):
    """Both the half-recorded and the unknown are reported. M56."""
    missing = broker_costs.unpriced(["^GSPC", "HALF", "BARE", "NEVER_HEARD_OF"], table)

    assert set(missing) == {"HALF", "BARE", "NEVER_HEARD_OF"}
    assert "^GSPC" not in missing


def test_every_recorded_short_is_a_charge_not_an_income(table):
    """The sign error that paid 451 shorts to exist. M57.

    The benchmark-and-markup model returns a negative short rate whenever the
    benchmark exceeds the markup -- a short that earns. Trading 212 charges
    both sides. Nothing read from the table may come back negative.
    """
    overrides = broker_costs.financing_overrides(["^GSPC", "^GDAXI"], table)

    assert overrides, "the fixture must produce rates for this to mean anything"
    for ticker, (long_rate, short_rate) in overrides.items():
        assert long_rate > 0.0, f"{ticker} long"
        assert short_rate > 0.0, f"{ticker} short is an income, not a charge"


def test_the_rates_are_annual_fractions_not_daily_percents(table):
    """A daily percent used as an annual fraction is wrong by 4000x."""
    long_rate, short_rate = broker_costs.financing_overrides(["^GSPC"], table)["^GSPC"]

    # 0.0083% a day compounds to a little over 3% a year.
    assert long_rate == pytest.approx(0.0083 / 100.0 * 365.0)
    assert 0.02 < long_rate < 0.05
    assert 0.03 < short_rate < 0.07


# --------------------------------------------------------------------------- #
# The runner's wiring
# --------------------------------------------------------------------------- #
def test_the_runner_enables_fx_and_charges_on_the_result(monkeypatch, table):
    """The three things the nightly run was not doing. M58."""
    import cfdinvest.schedule.app as app

    monkeypatch.setattr(
        "cfdinvest.livehelper.t212_rules.load_rules", lambda *a, **k: table
    )
    config = app._execution_config(["^GSPC", "^GDAXI"])

    assert config.fx.enabled, "FX was disabled, so every fx figure was zero"
    assert config.fx.fx_fee_basis == "result"
    assert config.financing_rates, "per-asset rates were never populated"
    assert config.fx.fx_fee_applies == {"^GSPC": True, "^GDAXI": False}
    assert config.fx.instrument_currencies == {"^GSPC": "USD", "^GDAXI": "EUR"}


def test_an_unreadable_table_does_not_stop_the_run(monkeypatch):
    """A missing rules file is the normal state, not an outage."""
    import cfdinvest.schedule.app as app

    def explode(*args, **kwargs):
        raise OSError("no such file")

    monkeypatch.setattr("cfdinvest.livehelper.t212_rules.load_rules", explode)
    config = app._execution_config(["^GSPC"])

    assert config.financing_rates is None
    assert not config.fx.enabled, "unknown currencies must not be converted"
