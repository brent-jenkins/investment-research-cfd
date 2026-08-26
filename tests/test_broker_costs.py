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
M59  financing accrued only on bars that carry a trade
     -> test_financing_accrues_on_bars_that_carry_no_trade
M60  attribution ignores the per-bar accrual and sums the trade log
     -> test_per_instrument_attribution_sums_to_the_runs_own_pnl
M61  GBPUSD used as quoted instead of inverted
     -> test_the_dollar_rate_is_the_reciprocal_of_the_published_pair
M62  a cross's legs collapse to a single direct leg
     -> test_rate_legs_accepts_a_plain_ticker_an_inversion_and_a_cross
M63  the confidence ceiling counts flat rows
     -> test_confidence_ceiling_reports_the_best_on_offer
M64  the environment multiplier folds into confidence regardless of the flag
     -> test_the_environment_multiplier_can_be_kept_out_of_confidence
M65  tilt mode reuses the gate's boolean permission
     -> test_tilt_mode_scales_where_the_gate_refuses
M66  a regime tilt above 1.0, levering the book
     -> test_the_tilt_never_levers_the_book_up
M67  an unknown mode falls through to permitting everything
     -> test_an_unknown_regime_mode_is_refused
M68  the value floor fills in for a missing unit floor
     -> test_the_value_floor_is_not_read_as_a_unit_count
M69  an absent position value is assumed to clear the floor
     -> test_an_unknown_position_value_is_not_assumed_to_clear_the_floor
M70  exit_on_flat ignored, so a flat signal always closes
     -> test_a_flat_signal_need_not_close_a_position
M71  the switch becomes "never close", losing the decay exit
     -> test_holding_through_flat_still_exits_a_decayed_signal
M72  closing a position gated by the relative drift threshold
     -> test_a_position_can_always_be_closed_however_high_the_threshold
M73  the endpoint fix becomes "resize always"
     -> test_the_middle_is_still_governed_by_the_threshold
M74  the calendar's time unit ignored converting to days
     -> test_the_cooldown_measures_days_not_raw_index_integers
M75  the cooldown comparison inverted
     -> test_a_longer_cooldown_blocks_a_re_entry_a_shorter_one_allows
M76  the cooldown computed and never applied
     -> test_a_longer_cooldown_blocks_a_re_entry_a_shorter_one_allows
M77  the time stop never fires
     -> test_the_time_stop_closes_a_position_that_is_not_working
M78  the time stop cuts winners as well as losers
     -> test_the_time_stop_leaves_a_working_position_alone
M79  the time stop measures the price move, ignoring financing
     -> test_the_time_stop_counts_financing_against_the_position
M80  the blackout window stops on the reporting date, missing the reaction
     -> test_the_blackout_covers_the_session_after_the_announcement
M81  an instrument with no recorded dates is blacked out anyway
     -> test_an_instrument_with_no_recorded_dates_is_not_blacked_out
M82  the blackout blocks entry but carries an open position through
     -> test_the_blackout_closes_an_open_position_not_only_new_ones
M83  a failed earnings fetch recorded as "has no earnings"
     -> test_fetching_omits_rather_than_blanks_a_ticker_it_cannot_read
M85  the strategy section is read and then ignored
     -> test_the_strategy_section_reaches_the_execution_config
M86  a strategy default silently changes recorded behaviour
     -> test_the_defaults_reproduce_the_behaviour_recorded_before_the_section
M87  a strategy field exists that config.toml never mentions
     -> test_every_strategy_field_is_present_in_the_shipped_config
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


# --------------------------------------------------------------------------- #
# The accounting identity that was quietly broken
# --------------------------------------------------------------------------- #
def _held_market():
    """One instrument, held long throughout, so financing accrues on bars
    that carry no trade at all."""
    from cfdinvest.preprocessing import PreprocessConfig
    from cfdinvest.preprocessing.frame import build_analysis_frame
    from cfdinvest.signals.frame import SignalFrame

    calendar = pd.bdate_range("2021-01-04", periods=180, tz="UTC", name="timestamp")
    rng = np.random.default_rng(11)
    close = 100 * np.exp(np.cumsum(rng.normal(0.0004, 0.01, len(calendar))))
    tidy = pd.DataFrame(
        {
            "open": close * 0.999, "high": close * 1.01, "low": close * 0.99,
            "close": close, "volume": 1e6, "ticker": "SPX",
        },
        index=calendar,
    ).set_index("ticker", append=True).sort_index()

    analysis = build_analysis_frame(
        tidy, interval="1d", config=PreprocessConfig(correlation_window=63)
    )
    columns = analysis.close.columns
    signals = SignalFrame(
        direction=pd.DataFrame("long", index=analysis.calendar, columns=columns),
        size=pd.DataFrame(0.5, index=analysis.calendar, columns=columns),
        confidence=pd.DataFrame(0.9, index=analysis.calendar, columns=columns),
        reason=pd.DataFrame("test", index=analysis.calendar, columns=columns),
    )
    return analysis, signals


def test_financing_accrues_on_bars_that_carry_no_trade():
    """M59: financing attributed only where a fill happened.

    A position held for a hundred bars and traded on three of them pays
    financing on all hundred. Summing the trade log finds the three.
    """
    from cfdinvest.execution import ExecutionConfig, run_backtest

    analysis, signals = _held_market()
    result = run_backtest(signals, analysis, ExecutionConfig())

    booked_on_trades = result.trades["financing"].sum()
    accrued = result.financing_by_asset.sum()

    assert accrued > 0.0
    assert accrued > booked_on_trades * 1.5, (
        "the per-bar accrual must exceed what the trade log booked, or the "
        "fixture is not holding anything between trades"
    )


def test_per_instrument_attribution_sums_to_the_runs_own_pnl():
    """M60: the identity the project claims and this artefact broke.

    Before this, a run that lost £57,772 produced a per-instrument table
    summing to £28,266 of loss. Both numbers were displayed; nothing said
    which was the P&L.
    """
    from cfdinvest.analytics.attribution import compute_instrument_attribution
    from cfdinvest.execution import ExecutionConfig, run_backtest

    analysis, signals = _held_market()
    result = run_backtest(signals, analysis, ExecutionConfig())

    table = compute_instrument_attribution(result)
    actual = float(result.equity_curve.iloc[-1] - result.equity_curve.iloc[0])

    # Realised only: an open position at the end has realised nothing, so
    # close the comparison against realised plus the unrealised remainder.
    unrealised = float(result.pnl["unrealized"].iloc[-1])
    assert table["net_pnl"].sum() == pytest.approx(actual - unrealised, rel=0.02)


# --------------------------------------------------------------------------- #
# Building a rate the lake does not publish
# --------------------------------------------------------------------------- #
def test_rate_legs_accepts_a_plain_ticker_an_inversion_and_a_cross():
    """The three shapes, and nothing invented from a bad one."""
    from cfdinvest.execution.fx import rate_legs

    assert rate_legs("EURGBP=X") == (("EURGBP=X", 1),)
    assert rate_legs(("GBPUSD=X", -1)) == (("GBPUSD=X", -1),)
    assert rate_legs([("USDJPY=X", -1), ("GBPUSD=X", -1)]) == (
        ("USDJPY=X", -1), ("GBPUSD=X", -1),
    )
    # Nothing usable must produce no legs, so the caller reports the rate
    # absent instead of falling through to parity.
    assert rate_legs(None) == ()
    assert rate_legs("") == ()
    assert rate_legs([("", 1)]) == ()


def test_the_dollar_rate_is_the_reciprocal_of_the_published_pair():
    """M61: GBPUSD used as quoted rather than inverted.

    GBPUSD prints about 1.27 -- dollars per pound. A sterling account
    holding a dollar instrument needs about 0.79. Using it the right way up
    overstates every foreign position by the square of the rate, and it
    still looks like a plausible number.
    """
    from cfdinvest.execution.fx import rate_legs
    from cfdinvest.schedule.app import LAKE_FX_PAIRS

    legs = rate_legs(LAKE_FX_PAIRS["USD"])
    assert legs == (("GBPUSD=X", -1),)
    ticker, exponent = legs[0]
    assert exponent == -1, "a GBP account needs GBP per USD, not USD per GBP"

    published = 1.27
    assert 1.0 / published == pytest.approx(0.787, abs=0.01)


def test_every_currency_in_the_map_crosses_through_a_pair_the_lake_holds():
    """A leg naming a series nobody ingests is a silent fallback to 1.0."""
    from cfdinvest.execution.fx import rate_legs
    from cfdinvest.schedule.app import LAKE_FX_PAIRS

    # The FX instruments the configured universe actually carries.
    available = {
        "EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X",
        "USDCAD=X", "USDCHF=X", "NZDUSD=X", "EURGBP=X",
    }
    for currency, spec in LAKE_FX_PAIRS.items():
        legs = rate_legs(spec)
        assert legs, f"{currency} has no legs"
        for ticker, _ in legs:
            assert ticker in available, f"{currency} needs {ticker}, not in the lake"


# --------------------------------------------------------------------------- #
# A gate that cannot be cleared is not a filter
# --------------------------------------------------------------------------- #
def test_confidence_ceiling_reports_the_best_on_offer():
    """M63: the ceiling ignores flat rows or reports None when it should not.

    The engine multiplies confidence by the environment multiplier, 0.5 in
    high volatility. Nothing can then score above 0.5, so a 60% gate rejects
    every signal by construction -- and the count of rejections alone cannot
    tell that apart from a genuinely quiet day.
    """
    from cfdinvest.advisor.actions import confidence_ceiling, suggested_trades

    rows = [
        {"ticker": "AAPL", "direction": "long", "confidence": 0.50, "size": 0.1},
        {"ticker": "MSFT", "direction": "long", "confidence": 0.42, "size": 0.1},
        {"ticker": "NKE", "direction": "flat", "confidence": 0.99, "size": 0.0},
    ]

    # The flat row scores highest and must not count: there is no confidence
    # in a position not taken.
    assert confidence_ceiling(rows) == pytest.approx(0.50)

    kept, below = suggested_trades(rows, balance=1000.0, threshold=0.60)
    assert kept == []
    assert below == 2
    # The pair together is what distinguishes the two silences.
    assert confidence_ceiling(rows) < 0.60

    assert confidence_ceiling([]) is None
    assert confidence_ceiling([{"direction": "flat", "confidence": 0.9}]) is None


def test_the_environment_multiplier_can_be_kept_out_of_confidence():
    """M64: the flag is ignored and the multiplier folds in regardless."""
    from cfdinvest.signals.combined import confidence_score

    index = pd.date_range("2024-01-01", periods=3, tz="UTC")
    columns = ["AAPL"]
    strength = pd.DataFrame(1.0, index=index, columns=columns)
    agreement = pd.DataFrame(1.0, index=index, columns=columns)
    halved = pd.DataFrame(0.5, index=index, columns=columns)
    direction = pd.DataFrame("long", index=index, columns=columns)

    folded = confidence_score(strength, agreement, halved, direction)
    kept_out = confidence_score(
        strength, agreement, halved, direction, include_environment=False
    )

    # With it folded in, a maximal signal in a volatile market caps at 0.5 --
    # unreachable for any gate above that.
    assert folded["AAPL"].max() == pytest.approx(0.5)
    assert kept_out["AAPL"].max() == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# Tilting instead of gating
# --------------------------------------------------------------------------- #
def _regime_series(labels):
    index = pd.date_range("2024-01-01", periods=len(labels), tz="UTC")
    return pd.Series(labels, index=index), index, pd.Index(["AAPL"])


def test_tilt_mode_scales_where_the_gate_refuses():
    """M65: tilt mode falls back to the gate's boolean permission.

    A gate discards the engine's ranking at a boundary: a strong trend in a
    middling regime and a weak one are refused alike. Tilting keeps the
    ordering and scales it, and only a zero tilt forbids outright.
    """
    from cfdinvest.signals.filters import regime_filter

    regimes, index, columns = _regime_series(
        ["risk_on", "neutral", "risk_off", "crisis"]
    )
    calm = pd.Series(["low"] * 4, index=index)
    macro = pd.Series(["expansion"] * 4, index=index)

    gated = regime_filter(regimes, calm, macro, index, columns, mode="gate")
    tilted = regime_filter(regimes, calm, macro, index, columns, mode="tilt")

    # The gate permits only risk_on; everything else is refused outright.
    assert list(gated.allow_long["AAPL"]) == [True, False, False, False]
    assert list(gated.long_multiplier["AAPL"]) == [1.0, 1.0, 1.0, 1.0]

    # The tilt permits three of the four, at descending size, and still
    # forbids a new long in a crisis.
    assert list(tilted.allow_long["AAPL"]) == [True, True, True, False]
    sizes = list(tilted.long_multiplier["AAPL"])
    assert sizes[0] > sizes[1] > sizes[2] > 0.0
    assert sizes[3] == pytest.approx(0.0)


def test_the_tilt_never_levers_the_book_up():
    """No filter may return a multiplier above 1.0, tilt mode included.

    The module's opening promise. A tilt that could exceed 1.0 would turn a
    filter into a position-sizer with leverage, which is the one thing the
    asymmetry here exists to prevent.
    """
    from cfdinvest.signals.filters import (
        LONG_REGIME_TILTS, SHORT_REGIME_TILTS, regime_filter,
    )

    labels = list(LONG_REGIME_TILTS)
    regimes, index, columns = _regime_series(labels)
    volatile = pd.Series(["high"] * len(labels), index=index)
    macro = pd.Series(["contraction"] * len(labels), index=index)

    outcome = regime_filter(regimes, volatile, macro, index, columns, mode="tilt")

    assert outcome.long_multiplier.to_numpy().max() <= 1.0
    assert outcome.short_multiplier.to_numpy().max() <= 1.0
    assert outcome.long_multiplier.to_numpy().min() >= 0.0
    assert all(v <= 1.0 for v in LONG_REGIME_TILTS.values())
    assert all(v <= 1.0 for v in SHORT_REGIME_TILTS.values())


def test_an_unknown_regime_mode_is_refused():
    """A typo must not silently fall through to permitting everything."""
    from cfdinvest.signals.filters import regime_filter

    regimes, index, columns = _regime_series(["risk_on"])
    calm = pd.Series(["low"], index=index)
    macro = pd.Series(["expansion"], index=index)

    with pytest.raises(ValueError, match="unknown regime filter mode"):
        regime_filter(regimes, calm, macro, index, columns, mode="tlit")


# --------------------------------------------------------------------------- #
# A minimum in pounds is not a minimum in units
# --------------------------------------------------------------------------- #
def test_the_value_floor_is_not_read_as_a_unit_count():
    """M68: min_trade_value conflated with min_quantity.

    Trading 212's floor on Lululemon is £5, which at a three-figure share
    price is about 0.04 of a share. Recording that as a unit minimum would
    demand five shares -- roughly £600 -- and refuse almost every trade the
    broker would actually accept.
    """
    from cfdinvest.livehelper.t212_rules import rules_from_mapping, validate_quantity

    rules = rules_from_mapping(
        {"LULU": {"t212_name": "Lululemon", "min_trade_value": 5.0}}, "test"
    ).get("LULU")

    assert rules.min_trade_value == pytest.approx(5.0)
    assert rules.min_quantity is None, "a value floor must not become a unit floor"

    # 0.04 shares worth £12 is fine; the same size worth £4 is not.
    assert not [
        i for i in validate_quantity(0.04, rules, value=12.0)
        if i.check == "min_trade_value"
    ]
    blocked = [
        i for i in validate_quantity(0.04, rules, value=4.0)
        if i.check == "min_trade_value"
    ]
    assert len(blocked) == 1 and blocked[0].blocking


def test_an_unknown_position_value_is_not_assumed_to_clear_the_floor():
    """Absence is named: no value supplied means the check did not run."""
    from cfdinvest.livehelper.t212_rules import rules_from_mapping, validate_quantity

    rules = rules_from_mapping(
        {"LULU": {"t212_name": "Lululemon", "min_trade_value": 5.0}}, "test"
    ).get("LULU")

    issues = validate_quantity(0.04, rules, value=None)
    assert not [i for i in issues if i.check == "min_trade_value"]


# --------------------------------------------------------------------------- #
# A flat signal is silence, not an instruction
# --------------------------------------------------------------------------- #
def test_a_flat_signal_need_not_close_a_position():
    """M70: exit_on_flat ignored, so flat always closes.

    A flat cell scores zero size and zero confidence by construction, so it
    fails the hold band as well as being flat. That is why widening
    exit_size and exit_confidence cannot lengthen a hold ending this way --
    measured down to 0.001 with no effect on mean hold at all. The
    behaviour needs its own switch or it cannot be reached.
    """
    import numpy as np

    from cfdinvest.execution.state import (
        FLAT, LONG, HysteresisThresholds, next_direction,
    )

    held = np.array([LONG, LONG])
    desired = np.array([FLAT, LONG])
    # Zero on both measures, as a flat cell always is.
    size = np.array([0.0, 0.5])
    confidence = np.array([0.0, 0.9])

    closes = next_direction(
        held, desired, size, confidence,
        HysteresisThresholds(exit_on_flat=True),
    )
    holds = next_direction(
        held, desired, size, confidence,
        HysteresisThresholds(exit_on_flat=False),
    )

    assert closes[0] == FLAT, "the recorded behaviour closes on a flat signal"
    assert holds[0] == LONG, "with the switch off, silence is not an exit"
    # The instrument still signalling is untouched either way.
    assert closes[1] == LONG and holds[1] == LONG


def test_holding_through_flat_still_exits_a_decayed_signal():
    """The switch must not become 'never close'.

    A signal that still points somewhere and has fallen out of the hold band
    is a real exit, and turning off the flat exit must leave that intact --
    otherwise the only way out is an opposing signal.
    """
    import numpy as np

    from cfdinvest.execution.state import (
        FLAT, LONG, HysteresisThresholds, next_direction,
    )

    held = np.array([LONG])
    desired = np.array([LONG])          # still pointing long...
    size = np.array([0.001])            # ...but decayed out of the band
    confidence = np.array([0.001])

    result = next_direction(
        held, desired, size, confidence,
        HysteresisThresholds(exit_on_flat=False),
    )
    assert result[0] == FLAT


# --------------------------------------------------------------------------- #
# Going flat is a change of state, not a drift
# --------------------------------------------------------------------------- #
def test_a_position_can_always_be_closed_however_high_the_threshold():
    """M72: closing gated by the relative drift threshold.

    Closing a position out is a relative drift of exactly 1.0, so with
    `drift > threshold` and any threshold at or above 1.0 the comparison is
    False and the position can never be reduced to nothing. Opening from
    nothing divides by the floor and always fires. Together that is a
    one-way ratchet -- positions grow and are never retired -- and a run at
    threshold 1.0 booked 45,398 trades and £320,712 of financing on its way
    to losing everything.
    """
    import numpy as np

    from cfdinvest.execution.state import needs_resize

    current = np.array([100.0, 0.0])
    target = np.array([0.0, 100.0])       # one closing, one opening

    for threshold in (0.25, 1.0, 5.0, 100.0):
        resize = needs_resize(current, target, threshold=threshold)
        assert resize[0], f"closing must fire at threshold {threshold}"
        assert resize[1], f"opening must fire at threshold {threshold}"


def test_the_middle_is_still_governed_by_the_threshold():
    """The endpoints are absolute; ordinary drift is not.

    Without this the fix would become "resize on every bar", which is the
    churn the threshold exists to prevent.
    """
    import numpy as np

    from cfdinvest.execution.state import needs_resize

    current = np.array([100.0, 100.0])
    target = np.array([150.0, 105.0])     # 50% drift and 5% drift

    loose = needs_resize(current, target, threshold=0.25)
    assert list(loose) == [True, False]

    tight = needs_resize(current, target, threshold=1.0)
    assert list(tight) == [False, False], "neither drift reaches 100%"


# --------------------------------------------------------------------------- #
# The re-entry cooldown
# --------------------------------------------------------------------------- #
def _two_bar_market(periods=60, unit="us"):
    """A single instrument on a tz-aware calendar in a chosen time unit."""
    from cfdinvest.preprocessing import PreprocessConfig
    from cfdinvest.preprocessing.frame import build_analysis_frame

    calendar = pd.bdate_range(
        "2021-01-04", periods=periods, tz="UTC", name="timestamp"
    ).as_unit(unit)
    rng = np.random.default_rng(5)
    close = 100 * np.exp(np.cumsum(rng.normal(0.0005, 0.01, len(calendar))))
    tidy = pd.DataFrame(
        {
            "open": close * 0.999, "high": close * 1.01, "low": close * 0.99,
            "close": close, "volume": 1e6, "ticker": "SPX",
        },
        index=calendar,
    ).set_index("ticker", append=True).sort_index()
    return build_analysis_frame(
        tidy, interval="1d", config=PreprocessConfig(correlation_window=20)
    )


def _on_off_signals(analysis, off_from, off_until):
    """Long, then flat for a stretch, then long again.

    A re-entry at a known distance, which is what the cooldown is about and
    what an always-long fixture can never produce.
    """
    from cfdinvest.signals.frame import SignalFrame

    calendar, columns = analysis.calendar, analysis.close.columns
    direction = pd.DataFrame("long", index=calendar, columns=columns)
    size = pd.DataFrame(0.5, index=calendar, columns=columns)
    confidence = pd.DataFrame(0.9, index=calendar, columns=columns)

    quiet = slice(off_from, off_until)
    direction.iloc[quiet] = "flat"
    size.iloc[quiet] = 0.0
    confidence.iloc[quiet] = 0.0

    return SignalFrame(
        direction=direction, size=size, confidence=confidence,
        reason=pd.DataFrame("test", index=calendar, columns=columns),
    )


def _trades(analysis, signals, cooldown):
    import dataclasses

    from cfdinvest.execution import ExecutionConfig, run_backtest

    return run_backtest(
        signals, analysis,
        dataclasses.replace(ExecutionConfig(), reentry_cooldown_days=cooldown),
    ).trades


def _reentry_gaps(trades):
    """Days between each close and the next open on the same instrument.

    The count of entries is the wrong measure and was the first thing tried:
    a cooldown delays a re-entry rather than cancelling it, so a signal that
    stays on produces the same number of opens either way, just later.
    """
    gaps, closed = [], {}
    for row in trades.sort_values("timestamp").itertuples():
        if row.action.startswith("close"):
            closed[row.asset] = row.timestamp
        elif row.action.startswith("open") and row.asset in closed:
            gaps.append((row.timestamp - closed.pop(row.asset)).days)
    return gaps


def test_the_cooldown_measures_days_not_raw_index_integers():
    """M74: the calendar's time unit ignored when converting to days.

    The lake's calendar arrives here as datetime64[us] and elsewhere as
    datetime64[ns]. Reading the raw integers without pinning the unit makes
    one day measure 0.001 days, which does not raise -- it silently puts
    every instrument permanently inside its cooldown. On the real run that
    cut the book from 3,383 trades to 171 and made every cooldown from 14
    days to 730 produce a byte-identical result, which is the only reason
    it was ever noticed.

    Two calendars differing only in their unit must produce the same book.
    """
    micro = _two_bar_market(periods=90, unit="us")
    nano = _two_bar_market(periods=90, unit="ns")

    same = _trades(micro, _on_off_signals(micro, 20, 30), cooldown=5)
    other = _trades(nano, _on_off_signals(nano, 20, 30), cooldown=5)

    assert list(same["timestamp"]) == list(other["timestamp"]), (
        "a microsecond calendar and a nanosecond one must produce the same "
        "book; the unit is not part of the strategy"
    )
    # And the cooldown must not have swallowed the re-entry entirely, which
    # is the shape the bug took.
    assert (same["action"].str.startswith("open")).sum() >= 2


def test_a_longer_cooldown_blocks_a_re_entry_a_shorter_one_allows():
    """M75: the comparison inverted, or the cooldown not applied.

    The signal goes flat for about two weeks and then returns. A cooldown
    shorter than the gap must let the second position open; one much longer
    must not.
    """
    analysis = _two_bar_market(periods=90)
    signals = _on_off_signals(analysis, 20, 30)   # ~10 business days quiet

    short_gaps = _reentry_gaps(_trades(analysis, signals, cooldown=2))
    long_gaps = _reentry_gaps(_trades(analysis, signals, cooldown=60))

    assert short_gaps, "the fixture must produce a re-entry to measure"
    assert min(short_gaps) < 60, "a 2-day cooldown must not delay it 60 days"
    assert min(long_gaps) >= 60, (
        f"a 60-day cooldown must hold every re-entry off that long, "
        f"smallest gap was {min(long_gaps)} days"
    )


def test_the_cooldown_does_not_close_anything_it_only_blocks_entry():
    """It is a rule about re-entering, never about holding."""
    import dataclasses

    from cfdinvest.execution import ExecutionConfig, run_backtest
    from cfdinvest.signals.frame import SignalFrame

    analysis = _two_bar_market(periods=90)
    columns = analysis.close.columns
    signals = SignalFrame(
        direction=pd.DataFrame("long", index=analysis.calendar, columns=columns),
        size=pd.DataFrame(0.5, index=analysis.calendar, columns=columns),
        confidence=pd.DataFrame(0.9, index=analysis.calendar, columns=columns),
        reason=pd.DataFrame("test", index=analysis.calendar, columns=columns),
    )
    result = run_backtest(
        signals, analysis,
        dataclasses.replace(ExecutionConfig(), reentry_cooldown_days=365),
    )
    # A position opened before any close happened is still opened, and a
    # 365-day cooldown on a 90-bar run must not have produced a close of
    # its own.
    actions = result.trades["action"]
    assert (actions.str.startswith("open")).any(), "the first entry is never blocked"


# --------------------------------------------------------------------------- #
# The time stop: how long it has failed to work, not how far it has fallen
# --------------------------------------------------------------------------- #
def _drifting_market(daily_return, periods=40):
    """One instrument moving a fixed amount a day."""
    from cfdinvest.preprocessing import PreprocessConfig
    from cfdinvest.preprocessing.frame import build_analysis_frame

    calendar = pd.bdate_range(
        "2021-01-04", periods=periods, tz="UTC", name="timestamp"
    )
    close = 100.0 * (1.0 + daily_return) ** np.arange(len(calendar))
    tidy = pd.DataFrame(
        {
            "open": close, "high": close * 1.001, "low": close * 0.999,
            "close": close, "volume": 1e6, "ticker": "SPX",
        },
        index=calendar,
    ).set_index("ticker", append=True).sort_index()
    return build_analysis_frame(
        tidy, interval="1d", config=PreprocessConfig(correlation_window=10)
    )


def _always_long(analysis):
    from cfdinvest.signals.frame import SignalFrame

    columns = analysis.close.columns
    return SignalFrame(
        direction=pd.DataFrame("long", index=analysis.calendar, columns=columns),
        size=pd.DataFrame(0.5, index=analysis.calendar, columns=columns),
        confidence=pd.DataFrame(0.9, index=analysis.calendar, columns=columns),
        reason=pd.DataFrame("test", index=analysis.calendar, columns=columns),
    )


def test_the_time_stop_closes_a_position_that_is_not_working():
    """M77: the time stop never fires.

    Measured on index CFDs long, 73% of eventual losers were already under
    water on their first day against 29% of eventual winners. Losers do not
    plunge, they fail to start -- which a price stop cannot ask about and
    this can.
    """
    import dataclasses

    from cfdinvest.execution import ExecutionConfig, run_backtest

    falling = _drifting_market(-0.004)
    signals = _always_long(falling)

    without = run_backtest(signals, falling, ExecutionConfig()).positions
    with_stop = run_backtest(
        signals, falling,
        dataclasses.replace(ExecutionConfig(), time_stop_days=1),
    ).positions

    held_without = int((without.to_numpy() != 0).sum())
    held_with = int((with_stop.to_numpy() != 0).sum())
    assert held_with < held_without, (
        f"a losing position must be cut sooner: {held_with} bars held with "
        f"the stop against {held_without} without"
    )


def test_the_time_stop_leaves_a_working_position_alone():
    """It must cut losers, not everything.

    A stop that closed a winner would raise the win rate by throwing away
    the trades the whole thing exists to catch, and the summary statistics
    would look better for it.
    """
    import dataclasses

    from cfdinvest.execution import ExecutionConfig, run_backtest

    # Rising faster than the default financing costs to carry it.
    rising = _drifting_market(0.006)
    signals = _always_long(rising)

    without = run_backtest(signals, rising, ExecutionConfig()).positions
    with_stop = run_backtest(
        signals, rising,
        dataclasses.replace(ExecutionConfig(), time_stop_days=1),
    ).positions

    assert int((with_stop.to_numpy() != 0).sum()) == int(
        (without.to_numpy() != 0).sum()
    ), "a position in profit must not be touched by the time stop"


def test_the_time_stop_counts_financing_against_the_position():
    """Rising, but slower than it costs to carry, is not working.

    The distinction needs a market that is genuinely up on price and still
    behind after financing -- a flat one proves nothing, because a zero move
    trips a "no better than zero" test whether or not carry was counted.

    The default long rate is 8% a year. This drifts up at about 2.6% a year,
    so the price move is positive throughout and the position is only ever
    behind once its own carry is deducted.
    """
    import dataclasses

    from cfdinvest.execution import ExecutionConfig, run_backtest

    crawling = _drifting_market(0.0001)
    signals = _always_long(crawling)

    held = int((run_backtest(
        signals, crawling,
        dataclasses.replace(ExecutionConfig(), time_stop_days=3),
    ).positions.to_numpy() != 0).sum())
    unstopped = int((run_backtest(
        signals, crawling, ExecutionConfig()
    ).positions.to_numpy() != 0).sum())

    assert held < unstopped, (
        "a position up on price but behind on carry is not working, and the "
        "time stop must measure the result rather than the price move"
    )


# --------------------------------------------------------------------------- #
# The earnings blackout
# --------------------------------------------------------------------------- #
def test_the_blackout_covers_the_session_after_the_announcement():
    """M80: the window stops on the reporting date itself.

    Most companies report after the close, so the reaction lands on the
    *following* session -- every one of the ten worst days in Lululemon's
    decline was the day after a print. A window that reopened on the
    morning of the reaction would take the gap it exists to avoid.
    """
    from cfdinvest.execution.engine import _earnings_blackout
    from cfdinvest.execution import ExecutionConfig
    import dataclasses

    index = pd.date_range("2024-03-18", periods=8, tz="UTC")
    columns = pd.Index(["LULU", "^GSPC"])
    config = dataclasses.replace(
        ExecutionConfig(),
        earnings_dates={"LULU": ["2024-03-21"]},
        earnings_blackout_days=2,
    )
    mask = _earnings_blackout(index, columns, config)

    by_day = dict(zip([str(d.date()) for d in index], mask[:, 0]))
    assert by_day["2024-03-19"], "two days before is inside the window"
    assert by_day["2024-03-21"], "the reporting date itself is inside"
    assert by_day["2024-03-22"], "the session after carries the reaction"
    assert not by_day["2024-03-23"], "and then it reopens"
    assert not by_day["2024-03-18"], "three days before is outside"

    # An instrument with no recorded dates is never blacked out.
    assert not mask[:, 1].any(), "an index has no earnings and must be untouched"


def test_an_instrument_with_no_recorded_dates_is_not_blacked_out():
    """M81: a missing entry treated as "unknown, so block everything".

    Indices, commodities and FX pairs have no earnings, and a share whose
    fetch failed is a different case from one that genuinely has none --
    but neither may be blocked on a date nobody recorded.
    """
    from cfdinvest.execution.engine import _earnings_blackout
    from cfdinvest.execution import ExecutionConfig
    import dataclasses

    index = pd.date_range("2024-03-18", periods=5, tz="UTC")
    columns = pd.Index(["GC=F", "EURUSD=X"])
    config = dataclasses.replace(
        ExecutionConfig(),
        earnings_dates={"LULU": ["2024-03-21"]},
        earnings_blackout_days=3,
    )
    assert not _earnings_blackout(index, columns, config).any()


def test_the_blackout_is_skipped_entirely_when_off():
    """None rather than an all-false array, so the engine can skip it."""
    from cfdinvest.execution.engine import _earnings_blackout
    from cfdinvest.execution import ExecutionConfig
    import dataclasses

    index = pd.date_range("2024-03-18", periods=5, tz="UTC")
    columns = pd.Index(["LULU"])
    dates = {"LULU": ["2024-03-21"]}

    off = dataclasses.replace(
        ExecutionConfig(), earnings_dates=dates, earnings_blackout_days=0
    )
    nothing_recorded = dataclasses.replace(
        ExecutionConfig(), earnings_dates=None, earnings_blackout_days=3
    )
    assert _earnings_blackout(index, columns, off) is None
    assert _earnings_blackout(index, columns, nothing_recorded) is None


def test_fetching_omits_rather_than_blanks_a_ticker_it_cannot_read():
    """A failed lookup and a genuine absence must stay distinguishable.

    An index has no earnings; a share whose fetch failed might. Recording
    the second as an empty list would make it permanently untradeable by a
    rule that reads emptiness as "nothing scheduled".
    """
    from cfdinvest.ingestion import earnings as module

    class Boom:
        def __init__(self, symbol):
            self.symbol = symbol

        @property
        def earnings_dates(self):
            raise RuntimeError("no such symbol")

    import sys
    import types

    stub = types.ModuleType("yfinance")
    stub.Ticker = Boom
    saved = sys.modules.get("yfinance")
    sys.modules["yfinance"] = stub
    try:
        assert module.fetch_earnings_dates(["LULU", "NVDA"]) == {}
    finally:
        if saved is None:
            sys.modules.pop("yfinance", None)
        else:
            sys.modules["yfinance"] = saved


def test_the_blackout_closes_an_open_position_not_only_new_ones():
    """M82: the blackout blocks entry but carries an existing position in.

    A position opened a fortnight before a print is in exactly the same
    danger as one opened the day before, and only one of those is prevented
    by refusing new entries. Demonstrated on the real book -- Apple was held
    at 194.10 units on 28, 29 and 30 October 2025 without the rule and flat
    with it, having been opened before the window began -- and pinned here.
    """
    import dataclasses

    import numpy as np

    from cfdinvest.execution import ExecutionConfig, run_backtest
    from cfdinvest.signals.frame import SignalFrame

    analysis = _two_bar_market(periods=60)
    columns = analysis.close.columns
    calendar = analysis.calendar
    signals = SignalFrame(
        direction=pd.DataFrame("long", index=calendar, columns=columns),
        size=pd.DataFrame(0.5, index=calendar, columns=columns),
        confidence=pd.DataFrame(0.9, index=calendar, columns=columns),
        reason=pd.DataFrame("test", index=calendar, columns=columns),
    )

    # A print in the middle of a stretch the signal wants held throughout,
    # so any position in the window must have been opened before it.
    reporting = calendar[40].date().isoformat()
    config = dataclasses.replace(
        ExecutionConfig(),
        earnings_dates={str(columns[0]): [reporting]},
        earnings_blackout_days=3,
    )

    without = run_backtest(signals, analysis, ExecutionConfig()).positions
    with_rule = run_backtest(signals, analysis, config).positions

    # Only the bars the window actually spans -- three *calendar* days
    # before a Monday reaches back over a weekend, so the bar count varies.
    stamp = pd.Timestamp(reporting, tz="UTC")
    covered = calendar[
        (calendar >= stamp - pd.Timedelta(days=3))
        & (calendar <= stamp + pd.Timedelta(days=1))
    ]
    earlier = calendar[calendar < stamp - pd.Timedelta(days=3)]

    assert (without.loc[covered, columns[0]] != 0).any(), (
        "the fixture must hold through the window when the rule is off"
    )
    assert (with_rule.loc[earlier[-3:], columns[0]] != 0).any(), (
        "the position must already be open before the window opens, or "
        "this proves nothing about closing"
    )
    assert (with_rule.loc[covered, columns[0]] == 0).all(), (
        "every bar in the window must be flat, including ones whose "
        "position was opened before it"
    )


# --------------------------------------------------------------------------- #
# Strategy settings belong in a file the process reads, not in code
# --------------------------------------------------------------------------- #
def test_the_strategy_section_reaches_the_execution_config():
    """M85: the section is read and then ignored.

    The reason this matters is not tidiness. On 2026-08-25 a long-running
    daemon went on executing the modules it had imported at start-up while
    the files on disk said something else, so FX conversion was switched on
    in code and stayed off in production for eleven hours. A value read
    from a file on each run cannot fail that way.
    """
    import dataclasses

    import cfdinvest.schedule.app as app
    from cfdinvest.config import StrategyConfig

    tuned = StrategyConfig(
        regime_mode="tilt",
        exit_on_flat=False,
        exit_on_opposing_signal=False,
        time_stop_days=1,
        reentry_cooldown_days=14,
        rebalance_threshold=3.0,
        exposure_per_unit_size=4.0,
        earnings_blackout_days=2,
        fx_enabled=False,
        fx_conversion_fee_pct=0.004,
    )
    config = app._execution_config(["^GSPC"], tuned)

    assert config.time_stop_days == 1
    assert config.reentry_cooldown_days == 14
    assert config.rebalance_threshold == pytest.approx(3.0)
    assert config.exposure_per_unit_size == pytest.approx(4.0)
    assert config.earnings_blackout_days == 2
    assert config.thresholds.exit_on_flat is False
    assert config.thresholds.exit_on_opposing_signal is False
    assert config.fx.enabled is False
    assert config.fx.fx_conversion_fee_pct == pytest.approx(0.004)


def test_the_defaults_reproduce_the_behaviour_recorded_before_the_section():
    """Adopting the section must not silently change a recorded number."""
    import cfdinvest.schedule.app as app
    from cfdinvest.config import StrategyConfig
    from cfdinvest.execution import ExecutionConfig

    stock = ExecutionConfig()
    built = app._execution_config(["^GSPC"], StrategyConfig())

    for field in (
        "rebalance_threshold", "exposure_per_unit_size",
        "reentry_cooldown_days", "time_stop_days", "earnings_blackout_days",
    ):
        assert getattr(built, field) == getattr(stock, field), field
    assert built.thresholds.exit_on_flat == stock.thresholds.exit_on_flat
    assert (
        built.thresholds.exit_on_opposing_signal
        == stock.thresholds.exit_on_opposing_signal
    )


def test_every_strategy_field_is_present_in_the_shipped_config():
    """A setting the file does not mention is one nobody knows they have.

    The section exists so the behaviour is visible in one place. A field
    added to StrategyConfig and not to config.toml defeats that, quietly.
    """
    import tomllib
    from pathlib import Path

    from cfdinvest.config import StrategyConfig

    text = Path("config/config.toml").read_text(encoding="utf-8")
    document = tomllib.loads(text)
    section = document.get("strategy") or {}

    for field in StrategyConfig.__dataclass_fields__:
        mentioned = field in section or f"# {field} =" in text
        assert mentioned, (
            f"{field} is a strategy setting that config.toml never mentions"
        )


# --------------------------------------------------------------------------- #
# Volume: conviction, which turns out to mean range rather than direction
# --------------------------------------------------------------------------- #
def test_heavy_volume_shrinks_a_position_and_never_grows_one():
    """M108: the volume filter able to size up.

    Volume against the next day's range measured r=+0.177 at t=+39.03 on
    shares -- the strongest thing in this project. Against the next
    *return* it measured r=-0.013. So a heavy session is a reason to hold
    less, never a reason to hold more or to pick a side, and the module's
    standing promise that no filter returns a multiplier above 1.0 is what
    keeps that honest.
    """
    from cfdinvest.signals.filters import volume_filter

    index = pd.date_range("2024-01-01", periods=30, tz="UTC")
    columns = pd.Index(["AAA"])
    # Twenty-five ordinary sessions, then one at three times normal.
    volume = pd.DataFrame(
        {"AAA": [1e6] * 25 + [3e6, 1e6, 1e6, 5e6, 1e6]}, index=index
    )
    outcome = volume_filter(volume, index, columns)
    multiplier = outcome.long_multiplier["AAA"]

    assert multiplier.iloc[24] == pytest.approx(1.0), "a quiet session is untouched"
    assert multiplier.iloc[25] == pytest.approx(0.6), "three times normal shrinks"
    assert multiplier.iloc[28] == pytest.approx(0.6), "so does five times"
    assert outcome.long_multiplier.to_numpy().max() <= 1.0
    assert outcome.short_multiplier.to_numpy().max() <= 1.0


def test_it_shrinks_both_directions_equally():
    """A wider range is a wider range whichever way the position points.

    Every other filter here treats the two sides differently, and for good
    reasons. This one must not: it is a statement about magnitude.
    """
    from cfdinvest.signals.filters import volume_filter

    index = pd.date_range("2024-01-01", periods=30, tz="UTC")
    columns = pd.Index(["AAA"])
    volume = pd.DataFrame({"AAA": [1e6] * 28 + [4e6, 1e6]}, index=index)
    outcome = volume_filter(volume, index, columns)

    assert outcome.long_multiplier.equals(outcome.short_multiplier)
    assert outcome.allow_long.to_numpy().all(), "it shrinks, it does not forbid"
    assert outcome.allow_short.to_numpy().all()


def test_missing_volume_is_not_read_as_a_quiet_market():
    """M109: an instrument with no volume data sized as though calm.

    Nine of the forty-one instruments report no volume -- index CFDs whose
    vendor does not supply it. Treating a gap in the feed as evidence of
    calm trading would size *up* into precisely the unknown.
    """
    from cfdinvest.signals.filters import volume_filter

    index = pd.date_range("2024-01-01", periods=30, tz="UTC")
    columns = pd.Index(["AAA"])

    assert volume_filter(None, index, columns).long_multiplier.to_numpy().min() == 1.0
    assert (
        volume_filter(pd.DataFrame(), index, columns)
        .long_multiplier.to_numpy().min() == 1.0
    )

    # And a column that is entirely NaN stays at full size rather than
    # being penalised for the vendor's silence.
    blank = pd.DataFrame({"AAA": [np.nan] * 30}, index=index)
    assert volume_filter(blank, index, columns).long_multiplier.to_numpy().min() == 1.0


def test_volume_is_measured_against_the_instrument_own_normal():
    """A company whose turnover doubled compares with itself, not with Apple."""
    from cfdinvest.signals.filters import volume_filter

    index = pd.date_range("2024-01-01", periods=30, tz="UTC")
    columns = pd.Index(["SMALL", "LARGE"])
    volume = pd.DataFrame(
        {
            "SMALL": [1_000] * 25 + [3_000, 1_000, 1_000, 1_000, 1_000],
            "LARGE": [50_000_000] * 30,
        },
        index=index,
    )
    outcome = volume_filter(volume, index, columns)

    # The small one traded three times its own normal and is shrunk; the
    # large one traded vastly more in absolute terms and is not.
    assert outcome.long_multiplier["SMALL"].iloc[25] == pytest.approx(0.6)
    assert outcome.long_multiplier["LARGE"].iloc[25] == pytest.approx(1.0)
