"""Volatility estimators on wide ``timestamp x ticker`` panels.

Three complementary measures, because they answer different questions:

``rolling_volatility``
    Standard deviation of returns. The textbook measure; subtracts the mean,
    so it describes dispersion *around the drift*.
``realized_volatility``
    Root-mean-square of returns, no mean subtraction. Closer to what an
    options desk means by realised vol, and the right input to a
    variance-targeting position sizer.
``average_true_range``
    Uses the high/low range, so it sees intrabar movement a close-to-close
    estimator is blind to. This is the one a CFD stop-loss should be sized
    against, because a stop is triggered by the low, not by the close.

All functions are pure and vectorised across the universe.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from cfdinvest.preprocessing.alignment import periods_per_year

__all__ = [
    "rolling_volatility",
    "realized_volatility",
    "true_range",
    "average_true_range",
    "annualize",
]


def _resolve_annualisation(
    index: pd.DatetimeIndex, periods: int | None
) -> int:
    """Return the periods-per-year factor, inferring it when not supplied."""
    return periods_per_year(index) if periods is None else periods


def annualize(volatility: pd.DataFrame, periods: int) -> pd.DataFrame:
    """Scale a per-bar volatility to annual terms by ``sqrt(periods)``.

    Square-root-of-time assumes returns are serially uncorrelated. That is
    close enough for daily equity and FX data, and materially wrong for
    trending or mean-reverting series -- worth remembering before comparing
    an annualised number across bar frequencies.
    """
    return volatility * np.sqrt(periods)


def rolling_volatility(
    returns: pd.DataFrame,
    window: int = 21,
    *,
    min_periods: int | None = None,
    annualized: bool = True,
    periods: int | None = None,
    ddof: int = 1,
) -> pd.DataFrame:
    """Rolling standard deviation of returns.

    Parameters
    ----------
    returns:
        Per-bar return panel.
    window:
        Lookback in bars.
    min_periods:
        Minimum observations before a value is emitted. Defaults to
        ``window``, so a partially filled window never yields a confident
        looking number computed from three points.
    annualized:
        Scale by ``sqrt(periods_per_year)``.
    periods:
        Bars per year; inferred from the index spacing when omitted.
    ddof:
        Delta degrees of freedom; 1 (sample) by default.

    Returns
    -------
    pandas.DataFrame
        Same shape as ``returns``.
    """
    if returns.empty:
        return returns.copy()
    if window < 2:
        raise ValueError(f"window must be >= 2 for a standard deviation, got {window}")

    sigma = returns.rolling(
        window, min_periods=window if min_periods is None else min_periods
    ).std(ddof=ddof)

    if not annualized:
        return sigma
    return annualize(sigma, _resolve_annualisation(returns.index, periods))


def realized_volatility(
    returns: pd.DataFrame,
    window: int = 21,
    *,
    min_periods: int | None = None,
    annualized: bool = True,
    periods: int | None = None,
) -> pd.DataFrame:
    """Rolling root-mean-square of returns (no mean subtraction).

    Realised volatility treats every move as risk, including the drift. Over
    a short window on a strongly trending asset it therefore reads higher
    than :func:`rolling_volatility`; over a noisy one the two converge.
    """
    if returns.empty:
        return returns.copy()
    if window < 1:
        raise ValueError(f"window must be >= 1, got {window}")

    effective = window if min_periods is None else min_periods
    mean_square = (returns**2).rolling(window, min_periods=effective).mean()
    sigma = np.sqrt(mean_square)

    if not annualized:
        return sigma
    return annualize(sigma, _resolve_annualisation(returns.index, periods))


def true_range(
    high: pd.DataFrame, low: pd.DataFrame, close: pd.DataFrame
) -> pd.DataFrame:
    """Return Wilder's true range.

    ``max(high - low, |high - close_prev|, |low - close_prev|)`` -- the
    widest of the intrabar range and the two gap-inclusive ranges, so an
    overnight jump is counted as movement rather than ignored.

    Pure and vectorised: the three candidates are computed as whole panels
    and reduced element-wise.
    """
    if high.empty or low.empty or close.empty:
        return pd.DataFrame(index=high.index)

    high, low = high.align(low, join="inner")
    high, close = high.align(close, join="inner")
    low = low.reindex_like(high)

    previous_close = close.shift(1)
    candidates = np.stack(
        [
            (high - low).to_numpy(dtype="float64"),
            (high - previous_close).abs().to_numpy(dtype="float64"),
            (low - previous_close).abs().to_numpy(dtype="float64"),
        ]
    )
    # nanmax so the first bar, which has no previous close, still yields the
    # plain high-low range instead of NaN.
    with np.errstate(invalid="ignore"):
        widest = np.nanmax(candidates, axis=0)

    result = pd.DataFrame(widest, index=high.index, columns=high.columns)
    # A bar with no high/low at all is genuinely unknown, not zero.
    return result.where(high.notna() & low.notna())


def average_true_range(
    high: pd.DataFrame,
    low: pd.DataFrame,
    close: pd.DataFrame,
    window: int = 14,
    *,
    method: str = "wilder",
    min_periods: int | None = None,
) -> pd.DataFrame:
    """Average true range, in price units.

    Parameters
    ----------
    method:
        ``"wilder"`` (default) applies Wilder's smoothing, an EWMA with
        ``alpha = 1 / window``; ``"sma"`` uses a simple moving average.
        Wilder's is the conventional definition and reacts to a shock more
        gradually, which makes a stop distance less jumpy.
    window:
        Smoothing length in bars.

    Returns
    -------
    pandas.DataFrame
        ATR in the same units as price -- not a percentage. Divide by close
        for a comparable cross-asset measure.

    Raises
    ------
    ValueError
        On an unknown ``method`` or a non-positive ``window``.
    """
    if window < 1:
        raise ValueError(f"window must be >= 1, got {window}")

    ranges = true_range(high, low, close)
    if ranges.empty:
        return ranges

    effective = window if min_periods is None else min_periods
    if method == "wilder":
        return ranges.ewm(alpha=1.0 / window, adjust=False, min_periods=effective).mean()
    if method == "sma":
        return ranges.rolling(window, min_periods=effective).mean()
    raise ValueError(f"unknown ATR method {method!r}; expected 'wilder' or 'sma'")
