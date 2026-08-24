"""FX conversion fees for instruments quoted outside the account currency.

A CFD on a US equity held in a GBP account earns and loses in dollars, and
every one of those dollars has to become pounds before it is worth anything.
The broker performs that conversion and charges for it. Ignoring the charge
makes a foreign-currency strategy look better than it is, by an amount that
scales with turnover rather than with skill — so it never shows up as a
suspicious single number, only as a slow overstatement of every result.

**Rate convention, fixed once so it cannot be read backwards.** ``fx_rate``
is always *units of account currency per one unit of instrument currency*.
A GBP account trading a USD instrument at 0.79 GBP/USD has ``fx_rate=0.79``,
and a $100 price is £79. That makes conversion a multiplication in one
direction and a division in the other, with no room for the sign of the
exponent to be wrong.

**Three separate charges, deliberately not merged.** They answer different
questions and a real statement lists them separately:

``fx_spread_bps``
    The two-sided quote. You buy instrument currency at a worse rate than
    you sell it, and both sides are adverse.
``fx_slippage_bps``
    Everything the quoted spread does not capture, applied after it.
``fx_conversion_fee_pct``
    The broker's explicit percentage — Trading212's is 0.5%. This is the
    dominant term by an order of magnitude at retail size.

**Prices convert at the mid rate; the three charges are separate cost
lines.** This is the same split the price spread already uses: P&L books at
the mid and the costs sit beside it, while the trade log's
``fx_adjusted_fill_price`` shows the all-in rate a manual execution would
actually see. Same total, two views, no double counting.

**What the fee is charged on is a policy, not a fact** — see
:data:`FX_FEE_BASES`. It is the single most consequential setting here and it
is worth an order of magnitude, so it is explicit rather than assumed.

Everything here is pure. The engine calls it; it calls nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

__all__ = [
    "FX_RATE_SOURCES",
    "FX_ROUNDING_MODES",
    "FX_FEE_BASES",
    "FX_TRADE_COLUMNS",
    "PIP",
    "CENT",
    "FXConfig",
    "convert_to_account_currency",
    "convert_from_account_currency",
    "apply_conversion_fee",
    "apply_fx_spread",
    "apply_fx_slippage",
    "round_fx_rate",
    "effective_fx_rate",
    "resolve_fx_rates",
    "converting_assets",
    "split_open_close",
    "apply_fx_conversion_open",
    "apply_fx_conversion_close",
    "recommended_fx_fill",
]

#: Where the rate comes from.
#:
#: ``"lake"`` (preferred)
#:     Read from an FX series in the data lake, via
#:     :attr:`FXConfig.fx_pair_tickers`. Falls back to ``"config"`` with a
#:     warning when the series is absent, rather than silently assuming 1.0.
#: ``"config"``
#:     Fixed rates from :attr:`FXConfig.fx_rates`. Honest for a short
#:     backtest; increasingly wrong over a decade.
#: ``"static"``
#:     One rate for everything, from :attr:`FXConfig.static_fx_rate`. For
#:     tests and for isolating the fee model from rate movement.
FX_RATE_SOURCES: tuple[str, ...] = ("lake", "config", "static")

#: Granularity the executed rate is rounded to.
#:
#: ``"none"``          no rounding — the default, and the only one that does
#:                     not invent precision the broker did not quote
#: ``"nearest_pip"``   1e-4, the conventional quote granularity for most pairs
#: ``"nearest_cent"``  1e-2, coarse; useful for a legible test, not for JPY
FX_ROUNDING_MODES: tuple[str, ...] = ("none", "nearest_pip", "nearest_cent")

#: What :attr:`FXConfig.fx_conversion_fee_pct` is charged on.
#:
#: ``"notional"`` (default)
#:     The converted position value, at open **and** at close. A 0.5% fee is
#:     then 1% of notional per round trip.
#: ``"result"``
#:     The realised P&L and financing being converted back, at close only.
#:
#: **These differ by roughly two orders of magnitude** on a position that
#: moves 1%, so the choice matters far more than the fee percentage itself.
#: ``"notional"`` is the default because it is what the specification for
#: this module described, and because it is the conservative reading — it
#: cannot flatter a result. ``"result"`` is closer to how Trading212 actually
#: charges CFD conversion: a CFD never buys the underlying, so no position
#: notional is converted at open; what crosses currencies is the outcome.
#: Run both if the strategy trades foreign instruments at any size.
FX_FEE_BASES: tuple[str, ...] = ("notional", "result")

#: FX columns added to the trade log.
FX_TRADE_COLUMNS: tuple[str, ...] = (
    "fx_rate",
    "fx_spread",
    "fx_conversion_fee",
    "fx_slippage",
    "fx_adjusted_fill_price",
)

#: Rounding granularities.
PIP: float = 1e-4
CENT: float = 1e-2

#: Which side of the two-sided quote a conversion sits on. Buying instrument
#: currency (funding a position) pays up; selling it (repatriating) receives
#: less. Both adverse, which is the point.
_BUY, _SELL = 1.0, -1.0


@dataclass(frozen=True, slots=True)
class FXConfig:
    """FX conversion policy.

    Attributes
    ----------
    enabled:
        ``False`` by default. With it off every instrument is assumed to be
        denominated in the account currency and no number moves, which keeps
        every previously recorded backtest reproducible.
    account_currency:
        What the equity curve is denominated in.
    instrument_currencies:
        ``{ticker: currency}``. Anything unlisted is assumed to already be in
        the account currency and converts at 1.0 with no fee — the safe
        default, because inventing a currency for an unmapped ticker would
        charge fees that were never paid.
    fx_conversion_fee_pct:
        The broker's percentage, as a fraction. ``0.005`` is Trading212's
        0.5%. Charged per :attr:`fx_fee_basis`.
    fx_spread_bps:
        The two-sided quote, in basis points of the rate, applied to the rate
        *before* conversion. Charged in full on each side, not halved: the
        quoted figure here is what one conversion costs.
    fx_slippage_bps:
        Applied to the rate after the spread.
    fx_rate_source:
        See :data:`FX_RATE_SOURCES`.
    fx_rates:
        ``{currency: rate}`` in account currency per unit, for the
        ``"config"`` source and as the ``"lake"`` fallback.
    fx_pair_tickers:
        ``{currency: ticker}`` naming the lake series that carries each rate.
    static_fx_rate:
        The single rate used by the ``"static"`` source.
    fx_rounding_mode:
        See :data:`FX_ROUNDING_MODES`.
    fx_fee_basis:
        See :data:`FX_FEE_BASES`. Worth an order of magnitude; read it.
    record_fx_details:
        Whether every trade carries its rate and cost breakdown. ``True``
        always, for this deployment: the trades are placed by hand and the
        rate is what the person placing them needs to see.
    """

    enabled: bool = False
    account_currency: str = "GBP"
    instrument_currencies: dict[str, str] | None = None

    fx_conversion_fee_pct: float = 0.005
    fx_spread_bps: float = 0.0
    fx_slippage_bps: float = 0.0

    fx_rate_source: str = "lake"
    fx_rates: dict[str, float] | None = None
    fx_pair_tickers: dict[str, str] | None = None
    static_fx_rate: float = 1.0

    fx_rounding_mode: str = "none"
    fx_fee_basis: str = "notional"
    record_fx_details: bool = True

    def __post_init__(self) -> None:
        if self.fx_rate_source not in FX_RATE_SOURCES:
            raise ValueError(
                f"unknown fx_rate_source {self.fx_rate_source!r}; "
                f"expected one of {FX_RATE_SOURCES}"
            )
        if self.fx_rounding_mode not in FX_ROUNDING_MODES:
            raise ValueError(
                f"unknown fx_rounding_mode {self.fx_rounding_mode!r}; "
                f"expected one of {FX_ROUNDING_MODES}"
            )
        if self.fx_fee_basis not in FX_FEE_BASES:
            raise ValueError(
                f"unknown fx_fee_basis {self.fx_fee_basis!r}; "
                f"expected one of {FX_FEE_BASES}"
            )
        for name, value in (
            ("fx_conversion_fee_pct", self.fx_conversion_fee_pct),
            ("fx_spread_bps", self.fx_spread_bps),
            ("fx_slippage_bps", self.fx_slippage_bps),
        ):
            if value < 0:
                raise ValueError(f"{name} must be >= 0, got {value}")
        # 0.5 is exactly what "0.5%" becomes when it is typed as a number
        # rather than a fraction, so the guard has to bite well below 1.0.
        # No broker charges a tenth of the converted amount.
        if self.fx_conversion_fee_pct >= 0.1:
            raise ValueError(
                f"fx_conversion_fee_pct is a fraction, not a percentage; "
                f"got {self.fx_conversion_fee_pct}, did you mean "
                f"{self.fx_conversion_fee_pct / 100}?"
            )
        if self.static_fx_rate <= 0:
            raise ValueError(
                f"static_fx_rate must be > 0, got {self.static_fx_rate}"
            )

    @property
    def spread_fraction(self) -> float:
        """FX spread as a fraction of the rate."""
        return self.fx_spread_bps / 10_000.0

    @property
    def slippage_fraction(self) -> float:
        """FX slippage as a fraction of the rate."""
        return self.fx_slippage_bps / 10_000.0

    @property
    def charges_on_open(self) -> bool:
        """Whether opening a position converts anything.

        Only under the ``"notional"`` basis. A CFD opened under the
        ``"result"`` basis converts nothing until it is closed.
        """
        return self.fx_fee_basis == "notional"


# --------------------------------------------------------------------------- #
# The four primitives a future accounting layer replaces
# --------------------------------------------------------------------------- #
def convert_to_account_currency(price, fx_rate):
    """Instrument currency -> account currency.

    Pure and vectorised. ``fx_rate`` is account currency per unit of
    instrument currency, so this is a multiplication.
    """
    return np.asarray(price, dtype="float64") * np.asarray(fx_rate, dtype="float64")


def convert_from_account_currency(price, fx_rate):
    """Account currency -> instrument currency.

    Pure and vectorised, and the exact inverse of
    :func:`convert_to_account_currency`. A non-positive rate yields NaN
    rather than an infinity, so a bad rate surfaces as missing data instead
    of poisoning an equity curve with ``inf``.
    """
    rate = np.asarray(fx_rate, dtype="float64")
    safe = np.where(rate > 0, rate, np.nan)
    return np.asarray(price, dtype="float64") / safe


def apply_conversion_fee(price, fee_pct: float, *, side: float = _BUY):
    """Adjust a price for the broker's conversion percentage.

    Pure and vectorised. ``side=+1`` is paying (the price rises), ``side=-1``
    is receiving (it falls). Always adverse in the direction given.
    """
    return np.asarray(price, dtype="float64") * (1.0 + side * float(fee_pct))


def apply_fx_spread(rate, spread_bps: float, *, side: float = _BUY):
    """Cross the two-sided FX quote.

    Pure and vectorised. Buying instrument currency pays above the mid,
    selling receives below it.
    """
    return np.asarray(rate, dtype="float64") * (
        1.0 + side * float(spread_bps) / 10_000.0
    )


def apply_fx_slippage(rate, slippage_bps: float, *, side: float = _BUY):
    """Adverse movement beyond the quoted spread. Pure and vectorised."""
    return np.asarray(rate, dtype="float64") * (
        1.0 + side * float(slippage_bps) / 10_000.0
    )


def round_fx_rate(rate, mode: str = "none"):
    """Round an executed rate to the quote granularity.

    Pure and vectorised. Rounding applies to the **rate**, not to a money
    amount: ``"nearest_pip"`` and ``"nearest_cent"`` are both quote
    granularities, and rounding the resulting price instead would make the
    effect depend on the size of the position.

    Raises
    ------
    ValueError
        On an unknown mode.
    """
    if mode not in FX_ROUNDING_MODES:
        raise ValueError(
            f"unknown fx_rounding_mode {mode!r}; expected one of {FX_ROUNDING_MODES}"
        )
    values = np.asarray(rate, dtype="float64")
    if mode == "none":
        return values
    granularity = PIP if mode == "nearest_pip" else CENT
    return np.round(values / granularity) * granularity


def effective_fx_rate(rate, config: FXConfig, *, side: float = _BUY):
    """The rate actually dealt at, after spread, slippage and rounding.

    Pure and vectorised. The order is fixed and is the order a broker
    applies it in: **spread, then slippage, then rounding**. Rounding last
    matters — rounding the mid first and then widening it would quote a
    granularity the deal never had.
    """
    adjusted = apply_fx_spread(rate, config.fx_spread_bps, side=side)
    adjusted = apply_fx_slippage(adjusted, config.fx_slippage_bps, side=side)
    return round_fx_rate(adjusted, config.fx_rounding_mode)


# --------------------------------------------------------------------------- #
# Rates
# --------------------------------------------------------------------------- #
def converting_assets(columns: pd.Index, config: FXConfig) -> np.ndarray:
    """Which instruments actually cross a currency boundary.

    Pure. An instrument already denominated in the account currency converts
    nothing, so it can be charged nothing — a fee on it would be the cost
    model applied to a transaction that never happened. Anything unmapped is
    assumed domestic, which is the safe direction: it understates costs
    visibly rather than inventing them silently.
    """
    if not config.enabled:
        return np.zeros(len(columns), dtype=bool)

    currencies = config.instrument_currencies or {}
    return np.array(
        [
            currencies.get(str(ticker), config.account_currency)
            != config.account_currency
            for ticker in columns
        ],
        dtype=bool,
    )


def resolve_fx_rates(
    columns: pd.Index,
    config: FXConfig,
    *,
    lake_rates: pd.Series | None = None,
    warn: bool = True,
) -> pd.Series:
    """The mid rate to use for every instrument, as a Series of floats.

    Pure. An instrument whose currency is unmapped, or which is already
    denominated in the account currency, resolves to ``1.0`` — no conversion
    and no fee. That is the safe default: inventing a currency for an
    unmapped ticker would charge fees that were never paid.

    Parameters
    ----------
    lake_rates:
        ``{currency: rate}`` read from the data lake by the caller. Used only
        by the ``"lake"`` source; a missing entry falls back to
        :attr:`FXConfig.fx_rates` with a warning rather than to 1.0, because
        silently assuming parity is the failure this module exists to stop.

    Returns
    -------
    pandas.Series
        Account currency per unit of instrument currency, indexed by ticker.
    """
    if not config.enabled:
        return pd.Series(1.0, index=columns, name="fx_rate")

    currencies = config.instrument_currencies or {}
    configured = config.fx_rates or {}
    from_lake = {} if lake_rates is None else dict(lake_rates)

    values = []
    for ticker in columns:
        currency = currencies.get(str(ticker), config.account_currency)
        if currency == config.account_currency:
            values.append(1.0)
            continue

        if config.fx_rate_source == "static":
            values.append(float(config.static_fx_rate))
            continue

        rate = None
        if config.fx_rate_source == "lake":
            rate = from_lake.get(currency)
            if rate is None or not np.isfinite(rate) or rate <= 0:
                rate = configured.get(currency)
                if rate is not None and warn:
                    logger.warning(
                        "No usable %s rate in the lake for %s; falling back to "
                        "the configured rate %s.", currency, ticker, rate
                    )
        else:
            rate = configured.get(currency)

        if rate is None or not np.isfinite(rate) or rate <= 0:
            if warn:
                logger.warning(
                    "No %s/%s rate available for %s; treating it as already "
                    "denominated in %s, so no conversion fee is charged. This "
                    "understates costs -- configure fx_rates.",
                    config.account_currency, currency, ticker,
                    config.account_currency,
                )
            values.append(1.0)
            continue

        values.append(float(rate))

    return pd.Series(values, index=columns, dtype="float64", name="fx_rate")


# --------------------------------------------------------------------------- #
# Open and close
# --------------------------------------------------------------------------- #
def split_open_close(
    previous_units: np.ndarray, new_units: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Split a change in position into the part opened and the part closed.

    Pure and vectorised. A flip does both: the whole old position closes and
    the whole new one opens, which is exactly what the broker converts.

    Returns
    -------
    tuple
        ``(opened_units, closed_units)``, both non-negative magnitudes.
    """
    previous = np.asarray(previous_units, dtype="float64")
    new = np.asarray(new_units, dtype="float64")

    old_size, new_size = np.abs(previous), np.abs(new)
    flipped = (np.sign(previous) != np.sign(new)) & (previous != 0) & (new != 0)

    opened = np.where(flipped, new_size, np.maximum(new_size - old_size, 0.0))
    closed = np.where(flipped, old_size, np.maximum(old_size - new_size, 0.0))
    return opened, closed


def _charges(
    amount: np.ndarray,
    config: FXConfig,
    *,
    with_fee: bool,
    converts: np.ndarray | None = None,
) -> dict:
    """The three charges on one conversion of ``amount`` account currency.

    ``converts`` masks out instruments that never cross a currency boundary:
    a GBP instrument in a GBP account is converted zero times and charged
    zero times.
    """
    magnitude = np.abs(np.asarray(amount, dtype="float64"))
    if converts is not None:
        magnitude = np.where(np.asarray(converts, dtype=bool), magnitude, 0.0)
    spread = magnitude * config.spread_fraction
    slippage = magnitude * config.slippage_fraction
    fee = (
        magnitude * config.fx_conversion_fee_pct
        if with_fee
        else np.zeros_like(magnitude)
    )
    return {
        "fx_spread": spread,
        "fx_slippage": slippage,
        "fx_conversion_fee": fee,
        "total": spread + slippage + fee,
    }


def apply_fx_conversion_open(
    base_fill_price,
    opened_units,
    fx_rate,
    config: FXConfig,
    *,
    converts=None,
) -> dict[str, np.ndarray]:
    """Convert account currency into instrument currency to fund an open.

    Pure and vectorised across assets. Called by the engine after the base
    fill price is known, which is why it takes a price rather than computing
    one: the fill model, the stop model and the FX model each own one step
    and none of them second-guesses another.

    Under the ``"result"`` fee basis nothing is charged here — a CFD never
    buys the underlying, so no notional crosses currencies at open — but the
    converted price is still returned, because the position still has to be
    marked in the account currency.

    Returns
    -------
    dict
        ``fx_rate`` (mid), ``effective_rate`` (all-in),
        ``fill_price`` (converted at the mid), ``fx_adjusted_fill_price``
        (converted at the all-in rate), the three charges, and ``total``.
    """
    price = np.asarray(base_fill_price, dtype="float64")
    rate = np.asarray(fx_rate, dtype="float64")
    units = np.abs(np.asarray(opened_units, dtype="float64"))

    dealt = effective_fx_rate(rate, config, side=_BUY)
    converted = convert_to_account_currency(price, rate)
    notional = np.nan_to_num(units * converted)

    if config.charges_on_open:
        charges = _charges(notional, config, with_fee=True, converts=converts)
    else:
        # No conversion happens at open under this basis, so the spread and
        # slippage on it do not happen either.
        charges = _charges(np.zeros_like(notional), config, with_fee=False)

    return {
        "fx_rate": rate,
        "effective_rate": dealt,
        "fill_price": converted,
        "fx_adjusted_fill_price": convert_to_account_currency(price, dealt),
        **charges,
    }


def apply_fx_conversion_close(
    base_fill_price,
    closed_units,
    fx_rate,
    config: FXConfig,
    *,
    realized_pnl=None,
    financing=None,
    converts=None,
) -> dict[str, np.ndarray]:
    """Convert instrument currency back into account currency on a close.

    Pure and vectorised across assets. What gets converted depends on
    :attr:`FXConfig.fx_fee_basis`:

    ``"notional"``
        The closed position's converted value.
    ``"result"``
        The realised P&L plus the financing accrued on it — the amount that
        actually crosses currencies on a contract for difference.

    Parameters
    ----------
    realized_pnl, financing:
        Already in account currency, and required by the ``"result"`` basis.
        Ignored by ``"notional"``.
    """
    price = np.asarray(base_fill_price, dtype="float64")
    rate = np.asarray(fx_rate, dtype="float64")
    units = np.abs(np.asarray(closed_units, dtype="float64"))

    dealt = effective_fx_rate(rate, config, side=_SELL)
    converted = convert_to_account_currency(price, rate)

    if config.fx_fee_basis == "result":
        result = np.zeros_like(converted) if realized_pnl is None else np.asarray(
            realized_pnl, dtype="float64"
        )
        carry = np.zeros_like(converted) if financing is None else np.asarray(
            financing, dtype="float64"
        )
        # Only a position that actually closed repatriates anything.
        crossing = np.where(units > 0, np.nan_to_num(result) + np.nan_to_num(carry), 0.0)
    else:
        crossing = np.nan_to_num(units * converted)

    charges = _charges(crossing, config, with_fee=True, converts=converts)

    return {
        "fx_rate": rate,
        "effective_rate": dealt,
        "fill_price": converted,
        "fx_adjusted_fill_price": convert_to_account_currency(price, dealt),
        "converted_amount": crossing,
        **charges,
    }


def recommended_fx_fill(
    base_fill_price,
    fx_rate,
    config: FXConfig,
    *,
    side: float = _BUY,
) -> dict[str, np.ndarray]:
    """The FX-adjusted price to expect when placing the trade by hand.

    **Nothing is sent anywhere.** The simulator converts at the mid and
    charges the costs separately; a person placing the order sees one
    all-in number instead, and this is that number.

    Latency between this being computed and the order being placed is
    accepted: the rate will have moved by a few pips, which is small beside
    the conversion fee and is not something a daily-bar backtest could model
    honestly anyway.

    Returns
    -------
    dict
        ``instrument_price`` (unconverted, what the ticket shows),
        ``fx_rate``, ``effective_rate`` and ``recommended_fill``.
    """
    price = np.asarray(base_fill_price, dtype="float64")
    rate = np.asarray(fx_rate, dtype="float64")
    dealt = effective_fx_rate(rate, config, side=side)
    converted = convert_to_account_currency(price, dealt)

    return {
        "instrument_price": price,
        "fx_rate": rate,
        "effective_rate": dealt,
        "recommended_fill": apply_conversion_fee(
            converted, config.fx_conversion_fee_pct, side=side
        )
        if config.record_fx_details
        else np.full_like(converted, np.nan),
    }
