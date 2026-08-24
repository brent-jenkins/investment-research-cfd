"""Return calculations on wide ``timestamp x ticker`` panels.

Every function is pure, vectorised across the whole universe, and NaN-safe:
a gap in one asset's history never contaminates another's, and a gap never
silently becomes a zero return.

Convention: a return at timestamp ``t`` describes the move *into* ``t`` from
the previous bar. The first row of any return panel is therefore NaN, which
is what stops a backtest trading a bar it has no return for.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = [
    "simple_returns",
    "log_returns",
    "rolling_returns",
    "overnight_returns",
    "intraday_returns",
    "cumulative_returns",
]


def _positive(prices: pd.DataFrame) -> pd.DataFrame:
    """Mask non-positive prices as NaN.

    A zero or negative price is a vendor defect, not a tradable level.
    Feeding one to a logarithm yields ``-inf``/``NaN`` and poisons every
    rolling window that touches it, so it is excluded up front. Ingestion
    already flags these bars; this is the numerical guard.
    """
    return prices.where(prices > 0)


def simple_returns(prices: pd.DataFrame, periods: int = 1) -> pd.DataFrame:
    """Return arithmetic returns ``P_t / P_{t-n} - 1``.

    Parameters
    ----------
    prices:
        ``timestamp x ticker`` panel, typically closes.
    periods:
        Lookback in bars.

    Returns
    -------
    pandas.DataFrame
        Same shape as ``prices``; the first ``periods`` rows are NaN.

    Notes
    -----
    ``fill_method=None`` is explicit: pandas' historical default
    forward-filled prices before differencing, which manufactures a 0%
    return across a gap that the asset never actually traded through.
    """
    if prices.empty:
        return prices.copy()
    return _positive(prices).pct_change(periods=periods, fill_method=None)


def log_returns(prices: pd.DataFrame, periods: int = 1) -> pd.DataFrame:
    """Return continuously compounded returns ``ln(P_t / P_{t-n})``.

    Log returns add across time, which makes multi-period aggregation a sum
    rather than a product -- the reason they are preferred for volatility
    and horizon scaling.
    """
    if prices.empty:
        return prices.copy()
    clean = _positive(prices)
    return np.log(clean / clean.shift(periods))


def rolling_returns(prices: pd.DataFrame, window: int) -> pd.DataFrame:
    """Return the total return over a trailing ``window`` of bars.

    Equivalent to ``simple_returns(prices, periods=window)`` but named for
    intent: this is the momentum-style "how much has it moved lately"
    measure, not a bar-to-bar return.

    Raises
    ------
    ValueError
        If ``window`` is not a positive integer.
    """
    if window < 1:
        raise ValueError(f"window must be >= 1, got {window}")
    return simple_returns(prices, periods=window)


def overnight_returns(
    open_prices: pd.DataFrame, close_prices: pd.DataFrame
) -> pd.DataFrame:
    """Return the gap between one close and the next open.

    ``Open_t / Close_{t-1} - 1``: the move that happens while the market is
    shut, and therefore the part of a CFD position's P&L that cannot be
    stopped out. Sizing that risk separately is why it is computed at all.

    Parameters
    ----------
    open_prices, close_prices:
        Panels sharing an index and columns. Mismatched columns are aligned
        on their intersection.

    Returns
    -------
    pandas.DataFrame
        First row NaN (no prior close to gap from).
    """
    if open_prices.empty or close_prices.empty:
        return pd.DataFrame(index=open_prices.index)

    opens, closes = open_prices.align(close_prices, join="inner")
    return _positive(opens) / _positive(closes).shift(1) - 1.0


def intraday_returns(
    open_prices: pd.DataFrame, close_prices: pd.DataFrame
) -> pd.DataFrame:
    """Return the session move ``Close_t / Open_t - 1``.

    The complement of :func:`overnight_returns`: compounding the two
    reproduces the close-to-close return exactly, which is the invariant
    worth testing them against.
    """
    if open_prices.empty or close_prices.empty:
        return pd.DataFrame(index=open_prices.index)

    opens, closes = open_prices.align(close_prices, join="inner")
    return _positive(closes) / _positive(opens) - 1.0


def cumulative_returns(
    returns: pd.DataFrame, *, log: bool = False, skipna: bool = True
) -> pd.DataFrame:
    """Compound a return panel into a growth path starting at 0.

    Parameters
    ----------
    returns:
        Panel of per-bar returns.
    log:
        ``True`` when ``returns`` are log returns, in which case compounding
        is a cumulative sum and the result is converted back to simple terms.
    skipna:
        Treat gaps as flat rather than truncating the path at the first NaN.

    Returns
    -------
    pandas.DataFrame
        Cumulative simple return, i.e. ``0.05`` means +5% since the start.
    """
    if returns.empty:
        return returns.copy()

    if log:
        total = returns.fillna(0.0).cumsum() if skipna else returns.cumsum()
        return np.expm1(total)

    growth = (1.0 + returns.fillna(0.0)) if skipna else (1.0 + returns)
    return growth.cumprod() - 1.0
