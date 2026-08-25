"""FX conversion fees: the full test matrix.

The ten cases the specification names are here under those names, plus the
ones that decide whether the module is safe to switch on: that a constant
rate with no fees changes nothing, that the P&L identity survives a sixth
component, and that a rate read from the lake cannot reach backwards in
time.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from cfdinvest.execution import ExecutionConfig, run_backtest
from cfdinvest.execution import fills as fills_module
from cfdinvest.execution import pnl as pnl_module
from cfdinvest.execution.fx import (
    CENT,
    FX_FEE_BASES,
    FX_RATE_SOURCES,
    FX_ROUNDING_MODES,
    FX_TRADE_COLUMNS,
    PIP,
    FXConfig,
    apply_conversion_fee,
    apply_fx_conversion_close,
    apply_fx_conversion_open,
    apply_fx_slippage,
    apply_fx_spread,
    convert_from_account_currency,
    convert_to_account_currency,
    converting_assets,
    effective_fx_rate,
    recommended_fx_fill,
    resolve_fx_rates,
    round_fx_rate,
    split_open_close,
)
from cfdinvest.preprocessing import PreprocessConfig, build_analysis_frame
from cfdinvest.schema import TICKER, TIMESTAMP
from cfdinvest.signals import SignalFrame

#: A GBP account holding a USD instrument at 0.80 GBP/USD. Round numbers so
#: every expected value below can be checked by hand.
RATE = 0.80

# The basis is stated rather than inherited. These tests exercise the
# notional mechanism, which is still supported -- but the *default* is now
# "result", because that is what Trading 212 charges, and a fixture that
# silently follows the default tests whichever one happens to be set.
FEE_ONLY = FXConfig(
    enabled=True,
    instrument_currencies={"SPX": "USD"},
    fx_rate_source="static",
    static_fx_rate=RATE,
    fx_conversion_fee_pct=0.005,
    fx_fee_basis="notional",
)
BUY, SELL = 1.0, -1.0


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def test_fx_is_disabled_by_default():
    """Enabling it moves every foreign-currency number, so it is opt-in."""
    assert FXConfig().enabled is False
    assert ExecutionConfig().fx.enabled is False


def test_the_documented_defaults():
    config = FXConfig()
    assert config.fx_conversion_fee_pct == 0.005
    assert config.fx_spread_bps == 0.0
    assert config.fx_slippage_bps == 0.0
    assert config.fx_rate_source == "lake"
    assert config.fx_rounding_mode == "none"
    assert config.record_fx_details is True


@pytest.mark.parametrize("source", FX_RATE_SOURCES)
def test_every_rate_source_is_usable(source):
    assert FXConfig(fx_rate_source=source).fx_rate_source == source


@pytest.mark.parametrize("mode", FX_ROUNDING_MODES)
def test_every_rounding_mode_is_usable(mode):
    assert FXConfig(fx_rounding_mode=mode).fx_rounding_mode == mode


@pytest.mark.parametrize("basis", FX_FEE_BASES)
def test_every_fee_basis_is_usable(basis):
    assert FXConfig(fx_fee_basis=basis).fx_fee_basis == basis


def test_unknown_settings_are_refused():
    with pytest.raises(ValueError, match="fx_rate_source"):
        FXConfig(fx_rate_source="guess")
    with pytest.raises(ValueError, match="fx_rounding_mode"):
        FXConfig(fx_rounding_mode="guess")
    with pytest.raises(ValueError, match="fx_fee_basis"):
        FXConfig(fx_fee_basis="guess")


def test_negative_charges_are_refused():
    with pytest.raises(ValueError, match="fx_conversion_fee_pct"):
        FXConfig(fx_conversion_fee_pct=-0.01)
    with pytest.raises(ValueError, match="fx_spread_bps"):
        FXConfig(fx_spread_bps=-1)
    with pytest.raises(ValueError, match="fx_slippage_bps"):
        FXConfig(fx_slippage_bps=-1)


def test_a_percentage_mistaken_for_a_fraction_is_refused():
    """``0.5`` means 50%, and nobody means 50%."""
    with pytest.raises(ValueError, match="fraction, not a percentage"):
        FXConfig(fx_conversion_fee_pct=0.5)


def test_a_non_positive_static_rate_is_refused():
    with pytest.raises(ValueError, match="static_fx_rate"):
        FXConfig(static_fx_rate=0)


# --------------------------------------------------------------------------- #
# Matrix 9: the multi-currency primitives
# --------------------------------------------------------------------------- #
def test_9_convert_to_account_currency():
    assert convert_to_account_currency([100.0], [RATE])[0] == pytest.approx(80.0)


def test_9_convert_from_account_currency():
    assert convert_from_account_currency([80.0], [RATE])[0] == pytest.approx(100.0)


def test_9_the_two_conversions_are_exact_inverses():
    prices = np.array([1.0, 42.5, 1234.56])
    rates = np.array([0.79, 1.27, 0.0068])
    restored = convert_from_account_currency(
        convert_to_account_currency(prices, rates), rates
    )
    assert np.allclose(restored, prices)


def test_9_a_bad_rate_yields_nan_not_infinity():
    """An ``inf`` would poison the equity curve silently; a NaN shows up."""
    assert np.isnan(convert_from_account_currency([80.0], [0.0])[0])
    assert np.isnan(convert_from_account_currency([80.0], [-1.0])[0])


def test_9_the_primitives_are_vectorised():
    converted = convert_to_account_currency([100.0, 200.0], [0.5, 2.0])
    assert converted.tolist() == [50.0, 400.0]


def test_9_conversion_fee_is_adverse_in_both_directions():
    assert apply_conversion_fee(100.0, 0.005, side=BUY) == pytest.approx(100.5)
    assert apply_conversion_fee(100.0, 0.005, side=SELL) == pytest.approx(99.5)


def test_9_converting_assets_marks_only_the_foreign_ones():
    mask = converting_assets(
        pd.Index(["SPX", "UKX", "NKY"]),
        FXConfig(
            enabled=True,
            account_currency="GBP",
            instrument_currencies={"SPX": "USD", "UKX": "GBP"},
        ),
    )
    assert mask.tolist() == [True, False, False]  # NKY unmapped -> domestic


def test_9_nothing_converts_when_fx_is_disabled():
    assert not converting_assets(pd.Index(["SPX"]), FXConfig()).any()


# --------------------------------------------------------------------------- #
# Matrix 4 and 5: spread alone, fee alone
# --------------------------------------------------------------------------- #
def test_4_fx_spread_only():
    config = FXConfig(
        enabled=True, fx_conversion_fee_pct=0.0, fx_spread_bps=25.0,
        instrument_currencies={"SPX": "USD"}, fx_fee_basis="notional",
    )
    result = apply_fx_conversion_open(
        [100.0], [10.0], [RATE], config, converts=[True]
    )

    assert result["fx_conversion_fee"][0] == pytest.approx(0.0)
    assert result["fx_spread"][0] == pytest.approx(800.0 * 0.0025)
    assert result["total"][0] == pytest.approx(result["fx_spread"][0])


def test_4_the_spread_is_crossed_adversely_on_each_side():
    assert apply_fx_spread(RATE, 25.0, side=BUY) > RATE
    assert apply_fx_spread(RATE, 25.0, side=SELL) < RATE


def test_5_fx_fee_only():
    result = apply_fx_conversion_open(
        [100.0], [10.0], [RATE], FEE_ONLY, converts=[True]
    )

    assert result["fx_spread"][0] == pytest.approx(0.0)
    assert result["fx_slippage"][0] == pytest.approx(0.0)
    # 10 units at £80 = £800 converted, at 0.5%.
    assert result["fx_conversion_fee"][0] == pytest.approx(4.0)


def test_5_the_fee_scales_with_the_converted_notional():
    small = apply_fx_conversion_open([100.0], [1.0], [RATE], FEE_ONLY, converts=[True])
    large = apply_fx_conversion_open([100.0], [10.0], [RATE], FEE_ONLY, converts=[True])
    assert large["total"][0] == pytest.approx(10 * small["total"][0])


# --------------------------------------------------------------------------- #
# Matrix 1: opening
# --------------------------------------------------------------------------- #
def test_1_open_converts_account_into_instrument_currency():
    result = apply_fx_conversion_open(
        [100.0], [10.0], [RATE], FEE_ONLY, converts=[True]
    )
    assert result["fill_price"][0] == pytest.approx(80.0)
    assert result["fx_rate"][0] == pytest.approx(RATE)


def test_1_open_pays_up_for_instrument_currency():
    """Funding a position buys the foreign currency, so the rate is worse."""
    config = FXConfig(
        enabled=True, fx_spread_bps=25.0, instrument_currencies={"SPX": "USD"}
    )
    result = apply_fx_conversion_open(
        [100.0], [10.0], [RATE], config, converts=[True]
    )
    assert result["effective_rate"][0] > RATE
    assert result["fx_adjusted_fill_price"][0] > result["fill_price"][0]


def test_1_open_applies_all_three_charges():
    config = FXConfig(
        enabled=True, fx_conversion_fee_pct=0.005, fx_spread_bps=25.0,
        fx_slippage_bps=10.0, instrument_currencies={"SPX": "USD"},
        fx_fee_basis="notional",
    )
    result = apply_fx_conversion_open(
        [100.0], [10.0], [RATE], config, converts=[True]
    )
    assert result["fx_conversion_fee"][0] == pytest.approx(4.0)
    assert result["fx_spread"][0] == pytest.approx(2.0)     # 800 x 25bps
    assert result["fx_slippage"][0] == pytest.approx(0.8)  # 800 x 10bps
    assert result["total"][0] == pytest.approx(6.8)


def test_1_the_result_basis_converts_nothing_at_open():
    """A CFD never buys the underlying, so no notional crosses at open."""
    config = FXConfig(
        enabled=True, fx_fee_basis="result", fx_conversion_fee_pct=0.005,
        instrument_currencies={"SPX": "USD"},
    )
    result = apply_fx_conversion_open(
        [100.0], [10.0], [RATE], config, converts=[True]
    )
    assert result["total"][0] == pytest.approx(0.0)
    assert result["fill_price"][0] == pytest.approx(80.0)  # still marked


# --------------------------------------------------------------------------- #
# Matrix 2: closing
# --------------------------------------------------------------------------- #
def test_2_close_converts_instrument_into_account_currency():
    result = apply_fx_conversion_close(
        [100.0], [10.0], [RATE], FEE_ONLY, converts=[True]
    )
    assert result["fill_price"][0] == pytest.approx(80.0)
    assert result["fx_conversion_fee"][0] == pytest.approx(4.0)


def test_2_close_receives_less_for_instrument_currency():
    config = FXConfig(
        enabled=True, fx_spread_bps=25.0, instrument_currencies={"SPX": "USD"}
    )
    result = apply_fx_conversion_close(
        [100.0], [10.0], [RATE], config, converts=[True]
    )
    assert result["effective_rate"][0] < RATE
    assert result["fx_adjusted_fill_price"][0] < result["fill_price"][0]


def test_2_the_result_basis_converts_the_outcome_not_the_notional():
    config = FXConfig(
        enabled=True, fx_fee_basis="result", fx_conversion_fee_pct=0.005,
        instrument_currencies={"SPX": "USD"},
    )
    result = apply_fx_conversion_close(
        [100.0], [10.0], [RATE], config,
        realized_pnl=[200.0], financing=[-5.0], converts=[True],
    )
    assert result["converted_amount"][0] == pytest.approx(195.0)
    assert result["fx_conversion_fee"][0] == pytest.approx(195.0 * 0.005)


def test_2_the_two_bases_differ_by_orders_of_magnitude():
    """The reason the basis is documented as the setting that matters."""
    common = dict(
        enabled=True, fx_conversion_fee_pct=0.005,
        instrument_currencies={"SPX": "USD"},
    )
    notional = apply_fx_conversion_close(
        [100.0], [10.0], [RATE],
        FXConfig(**common, fx_fee_basis="notional"),
        realized_pnl=[8.0], converts=[True],
    )
    result = apply_fx_conversion_close(
        [100.0], [10.0], [RATE],
        FXConfig(**common, fx_fee_basis="result"),
        realized_pnl=[8.0], converts=[True],
    )
    assert notional["total"][0] > 50 * result["total"][0]


def test_2_nothing_is_repatriated_when_nothing_closed():
    config = FXConfig(
        enabled=True, fx_fee_basis="result", instrument_currencies={"SPX": "USD"}
    )
    result = apply_fx_conversion_close(
        [100.0], [0.0], [RATE], config, realized_pnl=[200.0], converts=[True]
    )
    assert result["total"][0] == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# Matrix 3: no conversion
# --------------------------------------------------------------------------- #
def test_3_a_domestic_instrument_is_charged_nothing():
    """Nothing crossed a currency boundary, so nothing can be charged."""
    config = FXConfig(
        enabled=True, fx_conversion_fee_pct=0.005, fx_spread_bps=25.0,
        fx_slippage_bps=10.0, instrument_currencies={"UKX": "GBP"},
    )
    opened = apply_fx_conversion_open([100.0], [10.0], [1.0], config, converts=[False])
    closed = apply_fx_conversion_close([100.0], [10.0], [1.0], config, converts=[False])

    assert opened["total"][0] == pytest.approx(0.0)
    assert closed["total"][0] == pytest.approx(0.0)
    assert opened["fill_price"][0] == pytest.approx(100.0)


def test_3_an_unmapped_ticker_is_treated_as_domestic():
    rates = resolve_fx_rates(
        pd.Index(["MYSTERY"]),
        FXConfig(enabled=True, fx_rate_source="config", fx_rates={"USD": RATE}),
    )
    assert rates.iloc[0] == pytest.approx(1.0)


def test_3_a_rate_of_one_is_the_identity():
    assert convert_to_account_currency([123.45], [1.0])[0] == pytest.approx(123.45)


# --------------------------------------------------------------------------- #
# Matrix 6: order of operations
# --------------------------------------------------------------------------- #
def test_6_spread_then_slippage_then_rounding():
    config = FXConfig(
        enabled=True, fx_spread_bps=100.0, fx_slippage_bps=50.0,
        instrument_currencies={"SPX": "USD"},
    )
    expected = RATE * 1.01 * 1.005
    assert effective_fx_rate(RATE, config, side=BUY) == pytest.approx(expected)


def test_6_the_order_is_not_commutative_with_rounding():
    """Rounding the mid first would quote a granularity the deal never had."""
    config = FXConfig(
        enabled=True, fx_spread_bps=100.0, fx_rounding_mode="nearest_cent",
        instrument_currencies={"SPX": "USD"},
    )
    rate = 0.7937
    correct = effective_fx_rate(rate, config, side=BUY)
    backwards = apply_fx_spread(round_fx_rate(rate, "nearest_cent"), 100.0, side=BUY)

    assert correct == pytest.approx(round(rate * 1.01, 2))
    assert correct != pytest.approx(backwards)


def test_6_slippage_is_applied_after_the_spread():
    once = apply_fx_spread(RATE, 100.0, side=BUY)
    twice = apply_fx_slippage(once, 50.0, side=BUY)
    assert twice == pytest.approx(RATE * 1.01 * 1.005)


def test_6_all_three_charges_are_reported_separately():
    config = FXConfig(
        enabled=True, fx_conversion_fee_pct=0.005, fx_spread_bps=25.0,
        fx_slippage_bps=10.0, instrument_currencies={"SPX": "USD"},
        fx_fee_basis="notional",
    )
    result = apply_fx_conversion_open(
        [100.0], [10.0], [RATE], config, converts=[True]
    )
    assert result["total"][0] == pytest.approx(
        result["fx_conversion_fee"][0]
        + result["fx_spread"][0]
        + result["fx_slippage"][0]
    )


# --------------------------------------------------------------------------- #
# Matrix 7: rounding
# --------------------------------------------------------------------------- #
def test_7_rounding_none_leaves_the_rate_alone():
    assert round_fx_rate(0.793749, "none") == pytest.approx(0.793749)


def test_7_rounding_to_the_nearest_pip():
    assert round_fx_rate(0.7937492, "nearest_pip") == pytest.approx(0.7937)
    assert round_fx_rate(0.79376, "nearest_pip") == pytest.approx(0.7938)


def test_7_rounding_to_the_nearest_cent():
    assert round_fx_rate(0.7937, "nearest_cent") == pytest.approx(0.79)
    assert round_fx_rate(1.2761, "nearest_cent") == pytest.approx(1.28)


def test_7_the_granularities_are_what_they_claim():
    assert PIP == pytest.approx(1e-4)
    assert CENT == pytest.approx(1e-2)


def test_7_rounding_is_vectorised():
    rounded = round_fx_rate([0.79371, 1.27614], "nearest_pip")
    assert np.allclose(rounded, [0.7937, 1.2761])


def test_7_an_unknown_rounding_mode_is_refused():
    with pytest.raises(ValueError, match="fx_rounding_mode"):
        round_fx_rate(0.79, "nearest_furlong")


# --------------------------------------------------------------------------- #
# Matrix 8: manual execution
# --------------------------------------------------------------------------- #
def test_8_a_recommended_fx_fill_is_returned():
    config = FXConfig(
        enabled=True, fx_conversion_fee_pct=0.005, fx_spread_bps=25.0,
        instrument_currencies={"SPX": "USD"},
    )
    recommended = recommended_fx_fill([100.0], [RATE], config, side=BUY)

    assert recommended["instrument_price"][0] == pytest.approx(100.0)
    assert recommended["fx_rate"][0] == pytest.approx(RATE)
    assert np.isfinite(recommended["recommended_fill"][0])


def test_8_the_recommendation_is_all_in_and_adverse():
    config = FXConfig(
        enabled=True, fx_conversion_fee_pct=0.005, fx_spread_bps=25.0,
        instrument_currencies={"SPX": "USD"},
    )
    buying = recommended_fx_fill([100.0], [RATE], config, side=BUY)
    selling = recommended_fx_fill([100.0], [RATE], config, side=SELL)

    assert buying["recommended_fill"][0] > 80.0   # pays more than the mid
    assert selling["recommended_fill"][0] < 80.0  # receives less
    assert buying["recommended_fill"][0] > selling["recommended_fill"][0]


def test_8_the_ticket_price_is_reported_unconverted():
    """What the broker's order ticket shows, beside what it will cost."""
    recommended = recommended_fx_fill([100.0], [RATE], FEE_ONLY, side=BUY)
    assert recommended["instrument_price"][0] == pytest.approx(100.0)


def test_8_recording_can_be_suppressed():
    config = FXConfig(
        enabled=True, record_fx_details=False,
        instrument_currencies={"SPX": "USD"},
    )
    recommended = recommended_fx_fill([100.0], [RATE], config, side=BUY)
    assert np.isnan(recommended["recommended_fill"][0])


# --------------------------------------------------------------------------- #
# Matrix 10: determinism
# --------------------------------------------------------------------------- #
def test_10_conversion_is_deterministic():
    args = ([100.0], [10.0], [RATE], FEE_ONLY)
    first = apply_fx_conversion_open(*args, converts=[True])
    second = apply_fx_conversion_open(*args, converts=[True])
    assert first["total"] == second["total"]
    assert first["fill_price"] == second["fill_price"]


def test_10_the_rate_resolution_is_deterministic():
    config = FXConfig(
        enabled=True, fx_rate_source="config", fx_rates={"USD": RATE},
        instrument_currencies={"SPX": "USD"},
    )
    columns = pd.Index(["SPX", "UKX"])
    pd.testing.assert_series_equal(
        resolve_fx_rates(columns, config), resolve_fx_rates(columns, config)
    )


# --------------------------------------------------------------------------- #
# Rates and splitting
# --------------------------------------------------------------------------- #
def test_the_static_source_uses_one_rate_for_every_foreign_instrument():
    rates = resolve_fx_rates(
        pd.Index(["SPX", "NKY", "UKX"]),
        FXConfig(
            enabled=True, fx_rate_source="static", static_fx_rate=RATE,
            instrument_currencies={"SPX": "USD", "NKY": "JPY", "UKX": "GBP"},
        ),
    )
    assert rates.tolist() == [RATE, RATE, 1.0]


def test_the_config_source_uses_the_configured_rates():
    rates = resolve_fx_rates(
        pd.Index(["SPX", "NKY"]),
        FXConfig(
            enabled=True, fx_rate_source="config",
            fx_rates={"USD": 0.79, "JPY": 0.0052},
            instrument_currencies={"SPX": "USD", "NKY": "JPY"},
        ),
    )
    assert rates.tolist() == [0.79, 0.0052]


def test_an_unavailable_rate_falls_back_to_parity_with_a_warning(caplog):
    rates = resolve_fx_rates(
        pd.Index(["SPX"]),
        FXConfig(
            enabled=True, fx_rate_source="config",
            instrument_currencies={"SPX": "USD"},
        ),
    )
    assert rates.iloc[0] == pytest.approx(1.0)
    assert "understates costs" in caplog.text


def test_disabled_fx_resolves_everything_to_parity():
    rates = resolve_fx_rates(pd.Index(["SPX", "UKX"]), FXConfig())
    assert rates.tolist() == [1.0, 1.0]


def test_split_open_close_on_a_plain_open():
    opened, closed = split_open_close([0.0], [10.0])
    assert opened[0] == pytest.approx(10.0)
    assert closed[0] == pytest.approx(0.0)


def test_split_open_close_on_a_plain_close():
    opened, closed = split_open_close([10.0], [0.0])
    assert opened[0] == pytest.approx(0.0)
    assert closed[0] == pytest.approx(10.0)


def test_split_open_close_on_a_flip_does_both():
    """The old position is repatriated in full and the new one funded."""
    opened, closed = split_open_close([10.0], [-4.0])
    assert opened[0] == pytest.approx(4.0)
    assert closed[0] == pytest.approx(10.0)


def test_split_open_close_on_a_partial_reduction():
    opened, closed = split_open_close([10.0], [4.0])
    assert opened[0] == pytest.approx(0.0)
    assert closed[0] == pytest.approx(6.0)


def test_split_open_close_on_an_addition():
    opened, closed = split_open_close([4.0], [10.0])
    assert opened[0] == pytest.approx(6.0)
    assert closed[0] == pytest.approx(0.0)


def test_split_open_close_is_vectorised():
    opened, closed = split_open_close([0.0, 10.0, 10.0], [10.0, 0.0, -4.0])
    assert opened.tolist() == [10.0, 0.0, 4.0]
    assert closed.tolist() == [0.0, 10.0, 10.0]


# --------------------------------------------------------------------------- #
# The conversion helpers used by the engine
# --------------------------------------------------------------------------- #
def test_convert_prices_on_a_panel():
    panel = pd.DataFrame({"SPX": [100.0, 110.0], "UKX": [50.0, 55.0]})
    converted = fills_module.convert_prices(
        panel, pd.Series({"SPX": RATE, "UKX": 1.0})
    )
    assert converted["SPX"].tolist() == [80.0, 88.0]
    assert converted["UKX"].tolist() == [50.0, 55.0]


def test_convert_prices_on_a_matrix():
    matrix = np.array([[100.0, 50.0], [110.0, 55.0]])
    rates = np.array([[RATE, 1.0], [RATE, 1.0]])
    assert np.allclose(
        fills_module.convert_prices(matrix, rates), [[80.0, 50.0], [88.0, 55.0]]
    )


def test_convert_prices_with_parity_is_the_identity():
    panel = pd.DataFrame({"UKX": [100.0]})
    assert fills_module.convert_prices(panel, pd.Series({"UKX": 1.0})) is panel


def test_fx_is_a_pnl_component():
    assert "fx" in pnl_module.PNL_COMPONENTS


def test_bar_pnl_defaults_fx_to_zero():
    """Every pre-FX caller keeps producing exactly what it produced before."""
    zeros = np.zeros(2)
    result = pnl_module.bar_pnl(
        zeros, zeros, np.full(2, 100.0), np.full(2, 100.0), np.full(2, 100.0),
        zeros, zeros, zeros,
    )
    assert np.allclose(result["fx"], 0.0)


def test_bar_pnl_charges_fx_against_the_total():
    zeros = np.zeros(2)
    result = pnl_module.bar_pnl(
        zeros, zeros, np.full(2, 100.0), np.full(2, 100.0), np.full(2, 100.0),
        zeros, zeros, zeros, np.array([3.0, 4.0]),
    )
    assert np.allclose(result["fx"], [-3.0, -4.0])
    assert np.allclose(result["total"], [-3.0, -4.0])


# --------------------------------------------------------------------------- #
# Engine integration
# --------------------------------------------------------------------------- #
BARS = 300


@pytest.fixture
def calendar() -> pd.DatetimeIndex:
    return pd.bdate_range("2021-01-04", periods=BARS, tz="UTC", name=TIMESTAMP)


def build_market(calendar, *, rate_series=None, seed: int = 7):
    """Two instruments, and optionally a GBP/USD series in the lake."""
    length = len(calendar)
    blocks = []
    for offset, ticker in enumerate(("UKX", "SPX")):
        # A separate stream per ticker, always drawn at full length and then
        # truncated. Sharing one stream would give a 200-bar run different
        # SPX prices from the first 200 bars of a 300-bar run, and the
        # truncation test would be measuring the fixture, not the engine.
        rng = np.random.default_rng(seed + offset)
        close = 100 * np.exp(
            np.cumsum(rng.normal(0.0006, 0.012, BARS))[:length]
        )
        blocks.append(
            pd.DataFrame(
                {
                    "open": close * 0.999,
                    "high": close * 1.01,
                    "low": close * 0.99,
                    "close": close,
                    "volume": 1e6,
                    TICKER: ticker,
                },
                index=calendar,
            )
        )
    if rate_series is not None:
        blocks.append(
            pd.DataFrame(
                {
                    "open": rate_series, "high": rate_series, "low": rate_series,
                    "close": rate_series, "volume": 0.0, TICKER: "GBPUSD",
                },
                index=calendar,
            )
        )
    tidy = pd.concat(blocks).set_index(TICKER, append=True).sort_index()
    return build_analysis_frame(
        tidy, interval="1d", config=PreprocessConfig(correlation_window=63)
    )


def signals_for(analysis) -> SignalFrame:
    calendar, columns = analysis.calendar, analysis.close.columns
    return SignalFrame(
        direction=pd.DataFrame("long", index=calendar, columns=columns),
        size=pd.DataFrame(0.3, index=calendar, columns=columns),
        confidence=pd.DataFrame(0.9, index=calendar, columns=columns),
        reason=pd.DataFrame("test", index=calendar, columns=columns),
    )


@pytest.fixture
def analysis(calendar):
    return build_market(calendar)


@pytest.fixture
def signals(analysis):
    return signals_for(analysis)


def static_fx(**changes) -> ExecutionConfig:
    settings = dict(
        enabled=True,
        account_currency="GBP",
        instrument_currencies={"SPX": "USD"},
        fx_rate_source="static",
        static_fx_rate=RATE,
        fx_conversion_fee_pct=0.0,
    )
    return ExecutionConfig(fx=FXConfig(**{**settings, **changes}))


def test_disabled_fx_reproduces_the_previous_behaviour(analysis, signals):
    execution = run_backtest(signals, analysis, ExecutionConfig())

    assert execution.metadata["fx_enabled"] is False
    assert execution.fx_total_cost == pytest.approx(0.0)
    assert np.allclose(execution.pnl["fx"], 0.0)


def test_a_constant_rate_with_no_fees_changes_nothing(analysis, signals):
    """Position sizing is notional-based, so a pure rescaling cancels.

    This is the test that proves the conversion is a change of units and not
    a change of model: any difference from here on is a *cost*, not an
    artefact of multiplying prices by a number.
    """
    plain = run_backtest(signals, analysis, ExecutionConfig())
    converted = run_backtest(signals, analysis, static_fx())

    assert np.allclose(plain.equity_curve, converted.equity_curve)


def test_the_reported_fx_cost_is_exactly_what_the_pnl_was_charged(analysis, signals):
    """The one exact statement available: the component and the total agree."""
    charged = run_backtest(signals, analysis, static_fx(fx_conversion_fee_pct=0.005))

    assert charged.fx_total_cost > 0
    assert -charged.pnl["fx"].sum() == pytest.approx(charged.fx_total_cost)
    assert charged.metadata["total_fx"] == pytest.approx(charged.fx_total_cost)


def test_the_fee_is_the_only_difference_a_constant_rate_makes(analysis, signals):
    """Approximately, and the gap between "approximately" and "exactly" is real.

    Charging the fee lowers equity, which lowers the notional the next
    position is sized against, which changes every number after it. The
    shortfall is therefore close to the fee but not equal to it, and the
    difference is compounding, not error.
    """
    plain = run_backtest(signals, analysis, ExecutionConfig())
    charged = run_backtest(signals, analysis, static_fx(fx_conversion_fee_pct=0.005))

    shortfall = plain.equity_curve.iloc[-1] - charged.equity_curve.iloc[-1]
    assert shortfall == pytest.approx(charged.fx_total_cost, rel=0.05)
    assert shortfall > 0


def test_the_pnl_identity_survives_fx(analysis, signals):
    execution = run_backtest(signals, analysis, static_fx(fx_conversion_fee_pct=0.005))
    parts = execution.pnl[list(pnl_module.PNL_COMPONENTS)].sum(axis=1)

    assert np.allclose(parts, execution.pnl["total"])
    assert np.allclose(
        execution.equity_curve,
        execution.metadata["initial_equity"] + execution.pnl["total"].cumsum(),
    )


def test_only_the_foreign_instrument_is_charged(analysis, signals):
    execution = run_backtest(signals, analysis, static_fx(fx_conversion_fee_pct=0.005))
    summary = execution.fx_summary()

    assert summary.loc["SPX", "fx_cost"] > 0
    assert summary.loc["UKX", "fx_cost"] == pytest.approx(0.0)


def test_the_trade_log_carries_the_fx_columns(analysis, signals):
    execution = run_backtest(signals, analysis, static_fx(fx_conversion_fee_pct=0.005))

    for column in FX_TRADE_COLUMNS:
        assert column in execution.trades.columns

    foreign = execution.trades[execution.trades["asset"] == "SPX"]
    assert np.allclose(foreign["fx_rate"], RATE)
    assert foreign["fx_adjusted_fill_price"].notna().all()


def test_the_frame_reports_the_cost_split(analysis, signals):
    execution = run_backtest(signals, analysis, static_fx(fx_conversion_fee_pct=0.005))

    assert len(execution.fx_open_cost) == len(execution.equity_curve)
    assert len(execution.fx_close_cost) == len(execution.equity_curve)
    assert execution.fx_total_cost == pytest.approx(
        execution.fx_open_cost.sum() + execution.fx_close_cost.sum()
    )
    assert execution.fx_rate_used["SPX"] == pytest.approx(RATE)
    assert execution.fx_rate_used["UKX"] == pytest.approx(1.0)


def test_metadata_records_the_whole_policy(analysis, signals):
    execution = run_backtest(
        signals, analysis,
        static_fx(fx_conversion_fee_pct=0.005, fx_spread_bps=25.0,
                  fx_rounding_mode="nearest_pip", fx_fee_basis="result"),
    )
    assert execution.metadata["fx_enabled"] is True
    assert execution.metadata["account_currency"] == "GBP"
    assert execution.metadata["fx_conversion_fee_pct"] == 0.005
    assert execution.metadata["fx_spread_bps"] == 25.0
    assert execution.metadata["fx_rounding_mode"] == "nearest_pip"
    assert execution.metadata["fx_fee_basis"] == "result"


def test_a_larger_fee_costs_more(analysis, signals):
    cheap = run_backtest(signals, analysis, static_fx(fx_conversion_fee_pct=0.001))
    dear = run_backtest(signals, analysis, static_fx(fx_conversion_fee_pct=0.005))
    assert dear.fx_total_cost > cheap.fx_total_cost


def test_the_result_basis_costs_far_less_than_the_notional_basis(analysis, signals):
    """A round trip on notional dwarfs a fee on the outcome."""
    notional = run_backtest(
        signals, analysis,
        static_fx(fx_conversion_fee_pct=0.005, fx_fee_basis="notional"),
    )
    result = run_backtest(
        signals, analysis,
        static_fx(fx_conversion_fee_pct=0.005, fx_fee_basis="result"),
    )
    assert notional.fx_total_cost > result.fx_total_cost


def test_execution_with_fx_is_deterministic(analysis, signals):
    config = static_fx(fx_conversion_fee_pct=0.005, fx_spread_bps=25.0)
    first = run_backtest(signals, analysis, config)
    second = run_backtest(signals, analysis, config)

    assert np.allclose(first.equity_curve, second.equity_curve)
    pd.testing.assert_frame_equal(first.trades, second.trades)


def test_an_unhedged_position_earns_the_currency_move(calendar):
    """Not a cost — a real exposure the account carries, and it must show.

    A GBP investor holding a USD instrument owns the dollar as well as the
    instrument. A rising dollar helps and a falling one hurts, before any
    fee is charged.
    """
    rising = 0.80 * np.linspace(1.0, 1.25, BARS)
    analysis = build_market(calendar, rate_series=rising)
    signals = signals_for(analysis)

    lake = ExecutionConfig(
        fx=FXConfig(
            enabled=True,
            instrument_currencies={"SPX": "USD"},
            fx_rate_source="lake",
            fx_pair_tickers={"USD": "GBPUSD"},
            fx_rates={"USD": 0.80},
            fx_conversion_fee_pct=0.0,
        )
    )
    flat = run_backtest(signals, analysis, static_fx())
    appreciating = run_backtest(signals, analysis, lake)

    assert appreciating.equity_curve.iloc[-1] > flat.equity_curve.iloc[-1]


def test_a_lake_rate_cannot_reach_backwards(calendar):
    """The look-ahead test: truncating the future must not change the past.

    Using one rate for a whole run — the last one, say — would put an
    end-of-run rate on a start-of-run trade. Forward-filling a per-bar
    series is what prevents it, and this is what pins it.
    """
    rate_series = 0.80 * np.linspace(1.0, 1.25, BARS)
    full = build_market(calendar, rate_series=rate_series)
    config = ExecutionConfig(
        fx=FXConfig(
            enabled=True,
            instrument_currencies={"SPX": "USD"},
            fx_rate_source="lake",
            fx_pair_tickers={"USD": "GBPUSD"},
            fx_rates={"USD": 0.80},
            fx_conversion_fee_pct=0.005,
        )
    )
    whole = run_backtest(signals_for(full), full, config)

    cut = 200
    truncated = build_market(calendar[:cut], rate_series=rate_series[:cut])
    partial = run_backtest(signals_for(truncated), truncated, config)

    overlap = partial.equity_curve.index
    assert np.allclose(
        whole.equity_curve.loc[overlap], partial.equity_curve, rtol=1e-9
    )


def test_a_missing_lake_series_warns_and_falls_back(analysis, signals, caplog):
    execution = run_backtest(
        signals, analysis,
        ExecutionConfig(
            fx=FXConfig(
                enabled=True,
                instrument_currencies={"SPX": "USD"},
                fx_rate_source="lake",
                fx_pair_tickers={"USD": "GBPUSD"},
                fx_rates={"USD": RATE},
            )
        ),
    )
    assert "no USD series" in caplog.text
    assert execution.fx_rate_used["SPX"] == pytest.approx(RATE)


def test_stops_still_work_under_conversion(calendar):
    """An ATR level is a price range, so it has to convert like a price."""
    from cfdinvest.execution import StopConfig

    analysis = build_market(calendar)
    config = static_fx(fx_conversion_fee_pct=0.005)
    config = ExecutionConfig(
        fx=config.fx, stops=StopConfig(enabled=True, stop_distance=1.0)
    )
    execution = run_backtest(signals_for(analysis), analysis, config)

    assert not execution.stop_events.empty
    parts = execution.pnl[list(pnl_module.PNL_COMPONENTS)].sum(axis=1)
    assert np.allclose(parts, execution.pnl["total"])


def test_empty_frame_fx_views_do_not_raise():
    from cfdinvest.execution import ExecutionFrame

    empty = ExecutionFrame()
    assert empty.fx_summary().empty
    assert empty.fx_total_cost == pytest.approx(0.0)
