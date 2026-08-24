"""Fill prices and transaction costs.

**The one-bar lag is the point.** A signal computed from bar ``t``'s close
cannot be acted on until bar ``t+1``. The engine therefore shifts the whole
SignalFrame forward one bar before the state machine sees it, so a position
established on bar ``t`` is driven by information available at ``t-1``.
Without that shift the equity curve silently books a bar of impossible
profit on every trade, and the backtest looks brilliant for reasons that
have nothing to do with the strategy.

Costs are charged on the *change* in position, not on the position, so
holding is free and only trading pays:

``spread``
    Half the quoted spread is crossed on each transaction. A round trip
    therefore pays the full spread, which is how a CFD actually charges.
``slippage``
    Everything the spread does not capture — market impact, a fast tape, a
    gap through the level. Defaults to zero so a run's costs are exactly
    the spread until you deliberately say otherwise.

All functions are pure and vectorised.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = [
    "FILL_MODES",
    "DEFAULT_SPREAD_BPS",
    "shift_for_execution",
    "fill_prices",
    "resolve_spreads",
    "transaction_costs",
    "effective_fill_price",
]

#: Where in the bar a fill is assumed to happen.
FILL_MODES: tuple[str, ...] = ("open", "mid", "close")

#: Indicative half-spreads in basis points of price, by asset class. CFD
#: spreads vary by broker and time of day; these are conservative round
#: numbers, not quotes. Override per asset for anything that matters.
DEFAULT_SPREAD_BPS: dict[str, float] = {
    "equity": 2.0,
    "bond": 2.0,
    "commodity": 4.0,
    "fx": 1.0,
    "volatility": 10.0,
    "other": 5.0,
}


def shift_for_execution(frame: pd.DataFrame, lag: int = 1) -> pd.DataFrame:
    """Delay a signal panel so it can only be acted on after it was known.

    Pure. The first ``lag`` bars become NaN — there was nothing to act on
    yet — which the engine reads as flat.

    Raises
    ------
    ValueError
        If ``lag`` is negative. A negative lag would trade on future
        information, which is the exact failure this function exists to
        prevent, so it is refused rather than accommodated.
    """
    if lag < 0:
        raise ValueError(
            f"lag must be >= 0, got {lag}; a negative lag trades on the future"
        )
    if frame is None or frame.empty or lag == 0:
        return frame
    return frame.shift(lag)


def fill_prices(
    prices: dict[str, pd.DataFrame], mode: str = "open"
) -> pd.DataFrame:
    """Return the reference price each fill is struck against.

    Parameters
    ----------
    prices:
        OHLCV panels from ``AnalysisFrame.prices``.
    mode:
        ``"open"`` (default) fills at the bar's open — the honest choice,
        since the decision was made before the bar began.
        ``"mid"`` uses ``(high + low) / 2``, a rough proxy for an average
        execution across the session. Note this is **not knowable at the
        open**: it is a mild optimism, acceptable for comparing strategies
        but not for claiming an achievable fill.
        ``"close"`` fills at the close, which is more optimistic still.

    Returns
    -------
    pandas.DataFrame
        Fill reference price per bar per asset.

    Raises
    ------
    ValueError
        On an unknown mode, or when the required panels are absent.
    """
    if mode not in FILL_MODES:
        raise ValueError(f"unknown fill mode {mode!r}; expected one of {FILL_MODES}")

    if mode == "mid":
        high, low = prices.get("high"), prices.get("low")
        if high is None or low is None or high.empty or low.empty:
            raise ValueError("mid fills need high and low panels")
        return (high + low) / 2.0

    panel = prices.get(mode)
    if panel is None or panel.empty:
        # A close-only lake has no opens; fall back rather than fail, and be
        # explicit that the fill is then struck at the close.
        panel = prices.get("close")
        if panel is None or panel.empty:
            raise ValueError(f"no {mode!r} or 'close' panel available for fills")
    return panel


def resolve_spreads(
    columns: pd.Index,
    *,
    asset_classes: dict[str, str] | None = None,
    per_asset_bps: dict[str, float] | None = None,
    default_bps: float = 5.0,
) -> pd.Series:
    """Resolve a half-spread in fractional terms for every asset.

    Resolution order: an explicit per-asset value, then the asset class
    default, then ``default_bps``.

    Returns
    -------
    pandas.Series
        Spread as a fraction of price (not basis points), indexed by ticker.
    """
    explicit = per_asset_bps or {}
    classes = asset_classes or {}

    values = []
    for ticker in columns:
        name = str(ticker)
        if name in explicit:
            bps = explicit[name]
        elif name in classes:
            bps = DEFAULT_SPREAD_BPS.get(classes[name], default_bps)
        else:
            # No class known: use the caller's default. Falling through to
            # the "other" table entry here would make default_bps dead config.
            bps = default_bps
        values.append(float(bps) / 10_000.0)

    return pd.Series(values, index=columns, name="spread_fraction")


def effective_fill_price(
    reference: np.ndarray,
    traded_units: np.ndarray,
    spread_fraction: np.ndarray,
    slippage_fraction: float,
) -> np.ndarray:
    """Return the price actually paid, after crossing the spread.

    Buys fill above the reference, sells below — always adverse, never in
    the trader's favour. Pure and vectorised across assets.

    Parameters
    ----------
    traded_units:
        Signed change in position. Its sign decides which side is crossed.
    """
    side = np.sign(traded_units)
    adverse = (spread_fraction / 2.0) + slippage_fraction
    return reference * (1.0 + side * adverse)


def transaction_costs(
    traded_units: np.ndarray,
    reference_price: np.ndarray,
    spread_fraction: np.ndarray,
    slippage_fraction: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Split the cost of a trade into spread and slippage.

    Charged on ``|traded_units| * price``, so holding costs nothing and only
    the change in position pays. Both components are returned separately
    because they answer different questions: the spread is what the venue
    takes, slippage is what the strategy's own impatience costs.

    Returns
    -------
    tuple
        ``(spread_cost, slippage_cost)``, both non-negative.
    """
    notional = np.abs(traded_units) * np.abs(reference_price)
    spread_cost = notional * (spread_fraction / 2.0)
    slippage_cost = notional * slippage_fraction
    return spread_cost, slippage_cost
