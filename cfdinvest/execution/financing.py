"""Overnight financing on CFD positions.

A CFD is a leveraged position financed by the broker, so holding one
overnight costs (or occasionally earns) interest. The broker quotes it as a
benchmark rate plus a markup, and the markup is charged in the same
direction regardless of which way you are positioned:

``long``   pays  ``benchmark + markup``
``short``  pays  ``markup - benchmark``

The short leg is the interesting one. With a 5% benchmark and a 3% markup, a
short *receives* 2% — the position is effectively lending. With rates near
zero the same short *pays* 3%. Getting this sign wrong flatters a short-heavy
backtest by several percent a year, which is why it is modelled as a single
signed rate rather than two independent inputs.

Financing accrues **per night held**, not per bar. A Friday position carried
into Monday pays three nights, not one. Intraday round trips pay nothing.
All functions are pure and vectorised.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = [
    "DAY_COUNT",
    "FinancingRates",
    "nights_between",
    "financing_rates",
    "financing_cost",
]

#: Days per year for the accrual. CFD brokers conventionally use 365 for
#: equity and commodity CFDs; 360 is common for FX. Configurable per run.
DAY_COUNT = 365.0


class FinancingRates:
    """Signed annual financing rates, resolved per asset.

    Attributes
    ----------
    long_rate:
        Annual cost of carrying a long, as a fraction. Positive is a charge.
    short_rate:
        Annual cost of carrying a short. Negative means the short earns.
    """

    __slots__ = ("long_rate", "short_rate")

    def __init__(self, long_rate: float, short_rate: float) -> None:
        self.long_rate = float(long_rate)
        self.short_rate = float(short_rate)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"FinancingRates(long_rate={self.long_rate:.4f}, "
            f"short_rate={self.short_rate:.4f})"
        )


def financing_rates(benchmark: float = 0.05, markup: float = 0.03) -> FinancingRates:
    """Build the signed rate pair from a benchmark and a broker markup.

    Pure.

    >>> rates = financing_rates(benchmark=0.05, markup=0.03)
    >>> round(rates.long_rate, 4), round(rates.short_rate, 4)
    (0.08, -0.02)

    The short rate comes out negative here: at a 5% benchmark, a short earns
    2% after the 3% markup. Drop the benchmark to 1% and the same short pays
    2% instead.

    Raises
    ------
    ValueError
        If the markup is negative, which would mean the broker pays you to
        trade in both directions.
    """
    if markup < 0:
        raise ValueError(f"markup must be >= 0, got {markup}")
    return FinancingRates(long_rate=benchmark + markup, short_rate=markup - benchmark)


def nights_between(index: pd.DatetimeIndex) -> np.ndarray:
    """Calendar nights elapsed before each bar.

    Not bars: calendar days. A Friday-to-Monday gap is three nights, and a
    holiday-extended weekend is four. Charging one night per bar would
    understate financing by roughly 40% on a daily strategy, which compounds
    into a materially wrong equity curve over a decade.

    Pure.

    Returns
    -------
    numpy.ndarray
        Non-negative float per bar; the first bar is ``0`` because nothing
        was held into it.
    """
    if len(index) == 0:
        return np.zeros(0)

    stamps = pd.DatetimeIndex(index)
    deltas = stamps.to_series().diff().dt.total_seconds().to_numpy() / 86_400.0
    deltas[0] = 0.0
    # An intraday bar spans no night; clip so sub-daily bars accrue nothing
    # rather than a fraction that would over-charge a fast strategy.
    return np.floor(np.maximum(deltas, 0.0))


def financing_cost(
    units: np.ndarray,
    price: np.ndarray,
    nights: float,
    long_rate: np.ndarray | float,
    short_rate: np.ndarray | float,
    *,
    day_count: float = DAY_COUNT,
) -> np.ndarray:
    """Cost of carrying a position for ``nights`` nights.

    Pure and vectorised across assets. Charged on the **absolute notional**,
    since a short borrows the instrument just as a long borrows the cash.

    Parameters
    ----------
    units:
        Signed position. The sign selects which rate applies.
    price:
        Mark price used for the notional.
    nights:
        Nights held. Zero returns zero, so an intraday round trip is free.

    Returns
    -------
    numpy.ndarray
        Positive values are costs; negative values are credits (a short
        earning carry).
    """
    if nights <= 0:
        return np.zeros_like(units, dtype="float64")
    if day_count <= 0:
        raise ValueError(f"day_count must be > 0, got {day_count}")

    notional = np.abs(units) * np.abs(price)
    rate = np.where(units > 0, long_rate, short_rate)
    # Flat positions finance nothing regardless of the rate.
    rate = np.where(units == 0, 0.0, rate)

    return notional * rate * (nights / day_count)


def resolve_rates(
    columns: pd.Index,
    *,
    benchmark: float = 0.05,
    markup: float = 0.03,
    per_asset: dict[str, tuple[float, float]] | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Resolve long and short rate vectors for the universe.

    Parameters
    ----------
    per_asset:
        Optional ``{ticker: (long_rate, short_rate)}`` overrides, bypassing
        the benchmark/markup model entirely for instruments the broker
        quotes differently.

    Returns
    -------
    tuple
        ``(long_rates, short_rates)`` aligned to ``columns``.
    """
    default = financing_rates(benchmark, markup)
    overrides = per_asset or {}

    long_rates = np.full(len(columns), default.long_rate, dtype="float64")
    short_rates = np.full(len(columns), default.short_rate, dtype="float64")

    for position, ticker in enumerate(columns):
        if str(ticker) in overrides:
            long_rates[position], short_rates[position] = overrides[str(ticker)]

    return long_rates, short_rates
