"""Evaluation metrics over an execution result.

Read-only over an :class:`~cfdinvest.execution.ExecutionFrame`. Nothing here
changes a simulator parameter, and nothing here is allowed to: analytics
that can reach back into the simulator stop being analytics and become
curve-fitting with extra steps.

Every definition below is one of several defensible ones. Where a choice was
made, it is stated in the docstring rather than left for a reader to infer
from the arithmetic — a Sharpe ratio computed on a different convention is
not comparable to one computed on this one, and the difference is usually
larger than the effect being measured.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from cfdinvest.preprocessing.alignment import periods_per_year

__all__ = [
    "MetricsConfig",
    "equity_returns",
    "total_return",
    "cagr",
    "max_drawdown",
    "annualised_volatility",
    "sharpe_ratio",
    "sortino_ratio",
    "calmar_ratio",
    "round_trips",
    "attribute_financing",
    "win_rate",
    "expectancy",
    "average_r_multiple",
    "holding_time_distribution",
    "equity_metrics",
    "trade_metrics",
    "compute_metrics",
]

#: How ``1R`` is reconstructed. Execution does not model per-trade stops, so
#: the risk a trade was *intended* to carry has to be inferred.
R_BASES: tuple[str, ...] = ("equity_fraction", "average_loss", "notional")


@dataclass(frozen=True, slots=True)
class MetricsConfig:
    """Conventions the metric definitions depend on."""

    #: Annual risk-free rate, subtracted before Sharpe and Sortino.
    risk_free_rate: float = 0.0
    #: Minimum acceptable return for Sortino's downside deviation, annual.
    target_return: float = 0.0
    #: Bars per year. ``None`` infers from the equity index spacing.
    periods_per_year: int | None = None
    #: How ``1R`` is reconstructed; see :data:`R_BASES`.
    r_basis: str = "equity_fraction"
    #: Fraction of equity a trade was intended to risk, for
    #: ``r_basis="equity_fraction"``. Should match the signal layer's
    #: ``risk_fraction``, or the R multiples describe a different strategy
    #: from the one that ran.
    risk_fraction: float = 0.01
    #: Assumed stop distance as a fraction of notional, for
    #: ``r_basis="notional"``.
    stop_fraction: float = 0.02


def _annualisation(equity: pd.Series, config: MetricsConfig) -> int:
    """Bars per year, from config or inferred from the calendar."""
    if config.periods_per_year is not None:
        return config.periods_per_year
    if len(equity) > 1 and isinstance(equity.index, pd.DatetimeIndex):
        return periods_per_year(equity.index)
    return 252


def equity_returns(equity: pd.Series) -> pd.Series:
    """Per-bar simple returns of the equity curve.

    Pure. Non-positive equity yields NaN rather than a nonsense percentage:
    once an account is wiped out, a "return" on it has no meaning, and
    letting it through would poison every ratio downstream.
    """
    if equity is None or len(equity) < 2:
        return pd.Series(dtype="float64")

    previous = equity.shift(1)
    returns = equity / previous.where(previous > 0) - 1.0
    return returns.iloc[1:]


def total_return(equity: pd.Series) -> float:
    """Cumulative return from first to last bar."""
    if equity is None or len(equity) < 2 or equity.iloc[0] <= 0:
        return float("nan")
    return float(equity.iloc[-1] / equity.iloc[0] - 1.0)


def cagr(equity: pd.Series, config: MetricsConfig | None = None) -> float:
    """Compound annual growth rate.

    Uses the **calendar** span between the first and last bar, not a bar
    count, so a strategy that trades a partial year is not annualised as if
    the year were complete.

    Returns NaN for a period shorter than a day, or an account that reached
    zero — a negative base raised to a fractional power is not a return.
    """
    settings = config or MetricsConfig()
    if equity is None or len(equity) < 2 or equity.iloc[0] <= 0 or equity.iloc[-1] <= 0:
        return float("nan")

    if isinstance(equity.index, pd.DatetimeIndex):
        span_days = (equity.index[-1] - equity.index[0]).total_seconds() / 86_400.0
    else:
        span_days = len(equity) * 365.0 / _annualisation(equity, settings)

    if span_days < 1.0:
        return float("nan")

    years = span_days / 365.25
    return float((equity.iloc[-1] / equity.iloc[0]) ** (1.0 / years) - 1.0)


def max_drawdown(equity: pd.Series) -> dict[str, float]:
    """Deepest peak-to-trough decline, absolute and fractional.

    The peak is a running maximum, so the answer is the worst decline that
    was actually experienced, not the distance from the final high.

    Returns
    -------
    dict
        ``max_drawdown``, ``max_drawdown_fraction`` and ``drawdown_bars``
        (the longest stretch spent below a prior peak).
    """
    if equity is None or len(equity) == 0:
        return {
            "max_drawdown": float("nan"),
            "max_drawdown_fraction": float("nan"),
            "drawdown_bars": 0,
        }

    peak = equity.cummax()
    absolute = equity - peak
    fraction = absolute / peak.where(peak > 0)

    under_water = equity < peak
    if under_water.any():
        groups = (~under_water).cumsum()
        longest = int(under_water.groupby(groups).sum().max())
    else:
        longest = 0

    return {
        "max_drawdown": float(absolute.min()),
        "max_drawdown_fraction": float(fraction.min()) if fraction.notna().any() else float("nan"),
        "drawdown_bars": longest,
    }


def annualised_volatility(equity: pd.Series, config: MetricsConfig | None = None) -> float:
    """Standard deviation of bar returns, scaled by the square root of time."""
    settings = config or MetricsConfig()
    returns = equity_returns(equity).dropna()
    if len(returns) < 2:
        return float("nan")
    return float(returns.std(ddof=1) * np.sqrt(_annualisation(equity, settings)))


def sharpe_ratio(equity: pd.Series, config: MetricsConfig | None = None) -> float:
    """Excess return per unit of total volatility, annualised.

    Convention: arithmetic bar returns, sample standard deviation
    (``ddof=1``), the risk-free rate divided evenly across bars, and
    square-root-of-time scaling. That last assumption is the weak one — it
    treats returns as serially uncorrelated, which flatters a trending
    strategy and penalises a mean-reverting one.

    Returns NaN when volatility is zero: a flat account has no risk-adjusted
    return, and dividing by zero would report infinity instead.
    """
    settings = config or MetricsConfig()
    returns = equity_returns(equity).dropna()
    if len(returns) < 2:
        return float("nan")

    scale = _annualisation(equity, settings)
    excess = returns - settings.risk_free_rate / scale
    deviation = excess.std(ddof=1)
    if deviation == 0 or not np.isfinite(deviation):
        return float("nan")

    return float(excess.mean() / deviation * np.sqrt(scale))


def sortino_ratio(equity: pd.Series, config: MetricsConfig | None = None) -> float:
    """Excess return per unit of *downside* deviation, annualised.

    Convention: the downside deviation divides by the count of **all**
    observations, not only the losing ones. Dividing by the losing count
    instead — a common variant — inflates the ratio for a strategy that
    rarely loses, and the two numbers are not comparable.

    Returns NaN when there is no downside at all, since the ratio is
    unbounded there rather than infinitely good.
    """
    settings = config or MetricsConfig()
    returns = equity_returns(equity).dropna()
    if len(returns) < 2:
        return float("nan")

    scale = _annualisation(equity, settings)
    target = settings.target_return / scale
    excess = returns - settings.risk_free_rate / scale

    shortfall = np.minimum(returns - target, 0.0)
    downside = np.sqrt(np.mean(np.square(shortfall)))
    if downside == 0 or not np.isfinite(downside):
        return float("nan")

    return float(excess.mean() / downside * np.sqrt(scale))


def calmar_ratio(equity: pd.Series, config: MetricsConfig | None = None) -> float:
    """CAGR divided by the depth of the worst drawdown.

    A blunt but useful pairing: it asks what the strategy earned per unit of
    the worst pain it inflicted, which is the number an allocator tends to
    care about more than volatility.
    """
    growth = cagr(equity, config)
    depth = max_drawdown(equity)["max_drawdown_fraction"]
    if not np.isfinite(growth) or not np.isfinite(depth) or depth == 0:
        return float("nan")
    return float(growth / abs(depth))


# --------------------------------------------------------------------------- #
# Trade level
# --------------------------------------------------------------------------- #
def round_trips(trades: pd.DataFrame, index: pd.Index | None = None) -> pd.DataFrame:
    """Pair the trade log into completed round trips.

    Walks each asset's trades in order, opening a trip when the position
    leaves flat and closing it when it returns. A flip closes one trip and
    opens another on the same bar, which is what makes a reversal count as
    two decisions rather than one.

    Pure.

    Parameters
    ----------
    trades:
        The execution trade log.
    index:
        The run's calendar, used to measure holding time in bars. Without
        it, only the wall-clock duration is reported.

    Returns
    -------
    pandas.DataFrame
        One row per completed trip: ``asset``, ``direction``, ``entry_time``,
        ``exit_time``, ``bars_held``, ``days_held``, ``realized_pnl``,
        ``costs``, ``net_pnl``, ``entry_equity``, ``entry_notional``.

    Notes
    -----
    ``net_pnl`` here is **gross of financing** -- the trade log carries no
    per-asset carry. Pass the result through :func:`attribute_financing` to
    apportion the portfolio total, which is what :func:`trade_metrics` does
    automatically. Without that step, trade metrics and the equity curve
    tell different stories.
    """
    columns = [
        "asset", "direction", "entry_time", "exit_time", "bars_held",
        "days_held", "realized_pnl", "costs", "net_pnl", "entry_equity",
        "entry_notional",
    ]
    if trades is None or trades.empty:
        return pd.DataFrame(columns=columns)

    positions = (
        {stamp: number for number, stamp in enumerate(index)} if index is not None else {}
    )

    completed = []
    for asset, block in trades.sort_values("timestamp").groupby("asset", sort=True):
        open_trip: dict | None = None

        for _, trade in block.iterrows():
            cost = float(trade["spread_cost"]) + float(trade["slippage"])
            realized = float(trade["realized_pnl"])
            units_after = float(trade["units"])
            closes = open_trip is not None and (
                units_after == 0
                or np.sign(units_after) != np.sign(open_trip["direction_sign"])
            )

            if closes:
                open_trip["realized_pnl"] += realized
                open_trip["costs"] += cost
                completed.append(
                    _finish_trip(open_trip, trade, positions, asset)
                )
                open_trip = None

            if units_after != 0 and open_trip is None:
                open_trip = {
                    "direction_sign": np.sign(units_after),
                    "entry_time": trade["timestamp"],
                    "entry_equity": float(trade["equity"]),
                    "entry_notional": float(trade["notional"]),
                    "realized_pnl": 0.0,
                    "costs": cost if not closes else 0.0,
                }
            elif open_trip is not None:
                open_trip["realized_pnl"] += realized
                open_trip["costs"] += cost

    if not completed:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame(completed, columns=columns).sort_values(
        ["entry_time", "asset"]
    ).reset_index(drop=True)


def _finish_trip(trip: dict, closing_trade, positions: dict, asset: str) -> dict:
    """Close an open trip into a completed record."""
    entry_time = trip["entry_time"]
    exit_time = closing_trade["timestamp"]

    bars = (
        positions.get(exit_time, np.nan) - positions.get(entry_time, np.nan)
        if positions
        else np.nan
    )
    days = (
        (exit_time - entry_time).total_seconds() / 86_400.0
        if hasattr(exit_time, "to_pydatetime")
        else np.nan
    )

    return {
        "asset": asset,
        "direction": "long" if trip["direction_sign"] > 0 else "short",
        "entry_time": entry_time,
        "exit_time": exit_time,
        "bars_held": bars,
        "days_held": days,
        "realized_pnl": trip["realized_pnl"],
        "costs": trip["costs"],
        "net_pnl": trip["realized_pnl"] - trip["costs"],
        "entry_equity": trip["entry_equity"],
        "entry_notional": trip["entry_notional"],
    }


def attribute_financing(trips: pd.DataFrame, total_financing: float) -> pd.DataFrame:
    """Distribute the portfolio financing line across completed trips.

    Execution charges financing per bar on the whole book and does not
    record it per asset, so a per-trade figure cannot be read off the trade
    log. It can, however, be *apportioned* exactly: financing accrues in
    proportion to notional times nights held, which is precisely
    ``|entry_notional| x days_held``. Weighting by that and scaling to the
    known total gives per-trip figures that sum to the real number.

    Without this, trade-level P&L is gross of financing while the equity
    curve is net of it, and the two tell different stories: a run whose
    trades average a healthy profit can still lose money once carry is paid.

    Pure. Returns a copy with ``financing`` added and ``net_pnl`` reduced
    by it.

    Parameters
    ----------
    total_financing:
        The run's total financing **cost** (positive means paid).
    """
    if trips is None or trips.empty:
        return trips if trips is not None else pd.DataFrame()

    result = trips.copy()
    weight = result["entry_notional"].abs() * result["days_held"].clip(lower=0.0)
    total_weight = float(weight.sum())

    if total_weight <= 0 or not np.isfinite(total_financing):
        result["financing"] = 0.0
    else:
        result["financing"] = total_financing * (weight / total_weight)

    result["net_pnl"] = result["realized_pnl"] - result["costs"] - result["financing"]
    return result


def win_rate(trips: pd.DataFrame, column: str = "net_pnl") -> float:
    """Fraction of completed trips that made money.

    Scratches (exactly zero) count as losses, on the grounds that a trade
    that paid the spread and returned nothing was not a win.
    """
    if trips is None or trips.empty:
        return float("nan")
    return float((trips[column] > 0).mean())


def expectancy(trips: pd.DataFrame, column: str = "net_pnl") -> dict[str, float]:
    """Average outcome per trade, and the pieces it decomposes into.

    Returns
    -------
    dict
        ``expectancy`` (mean P&L per trade), ``average_win``,
        ``average_loss`` (positive), ``payoff_ratio`` and ``profit_factor``.
    """
    if trips is None or trips.empty:
        return {
            "expectancy": float("nan"),
            "average_win": float("nan"),
            "average_loss": float("nan"),
            "payoff_ratio": float("nan"),
            "profit_factor": float("nan"),
        }

    values = trips[column]
    wins = values[values > 0]
    losses = values[values <= 0]

    average_win = float(wins.mean()) if len(wins) else 0.0
    average_loss = float(-losses.mean()) if len(losses) else 0.0
    gross_profit = float(wins.sum())
    gross_loss = float(-losses.sum())

    return {
        "expectancy": float(values.mean()),
        "average_win": average_win,
        "average_loss": average_loss,
        "payoff_ratio": average_win / average_loss if average_loss > 0 else float("nan"),
        "profit_factor": gross_profit / gross_loss if gross_loss > 0 else float("nan"),
    }


def average_r_multiple(
    trips: pd.DataFrame, config: MetricsConfig | None = None
) -> dict[str, float]:
    """Average outcome expressed in units of the risk taken.

    **Execution does not model per-trade stops**, so the risk a trade was
    intended to carry is not recorded anywhere and has to be reconstructed.
    Three bases are offered, and which one is used changes the answer:

    ``equity_fraction`` (default)
        ``1R = risk_fraction x equity at entry``. Matches the signal layer's
        sizing intent, provided ``risk_fraction`` here equals the one used
        there.
    ``average_loss``
        ``1R = the mean loss of this run's losing trades``. Self-referential
        — it cannot be known in advance — but it is the convention most
        trading literature uses.
    ``notional``
        ``1R = stop_fraction x notional at entry``.

    Returns
    -------
    dict
        ``average_r``, ``median_r``, ``r_basis`` and the ``risk_per_trade``
        that was used.

    Raises
    ------
    ValueError
        On an unrecognised basis.
    """
    settings = config or MetricsConfig()
    if settings.r_basis not in R_BASES:
        raise ValueError(f"unknown r_basis {settings.r_basis!r}; expected {R_BASES}")

    empty = {
        "average_r": float("nan"),
        "median_r": float("nan"),
        "r_basis": settings.r_basis,
        "risk_per_trade": float("nan"),
    }
    if trips is None or trips.empty:
        return empty

    if settings.r_basis == "equity_fraction":
        risk = trips["entry_equity"] * settings.risk_fraction
    elif settings.r_basis == "notional":
        risk = trips["entry_notional"].abs() * settings.stop_fraction
    else:
        losses = trips.loc[trips["net_pnl"] <= 0, "net_pnl"]
        mean_loss = float(-losses.mean()) if len(losses) else float("nan")
        risk = pd.Series(mean_loss, index=trips.index)

    usable = risk.where(risk > 0)
    multiples = trips["net_pnl"] / usable
    if not multiples.notna().any():
        return empty

    return {
        "average_r": float(multiples.mean()),
        "median_r": float(multiples.median()),
        "r_basis": settings.r_basis,
        "risk_per_trade": float(usable.mean()),
    }


def holding_time_distribution(trips: pd.DataFrame) -> dict[str, float]:
    """Where holding times sit, as a churn diagnostic.

    A median of one or two bars means the hysteresis band is too narrow and
    the strategy is paying the spread to stand still.
    """
    empty = {
        "trades": 0,
        "median_bars_held": float("nan"),
        "mean_bars_held": float("nan"),
        "min_bars_held": float("nan"),
        "max_bars_held": float("nan"),
        "p25_bars_held": float("nan"),
        "p75_bars_held": float("nan"),
        "median_days_held": float("nan"),
    }
    if trips is None or trips.empty:
        return empty

    bars = trips["bars_held"].dropna()
    if bars.empty:
        empty["trades"] = int(len(trips))
        empty["median_days_held"] = float(trips["days_held"].median())
        return empty

    return {
        "trades": int(len(trips)),
        "median_bars_held": float(bars.median()),
        "mean_bars_held": float(bars.mean()),
        "min_bars_held": float(bars.min()),
        "max_bars_held": float(bars.max()),
        "p25_bars_held": float(bars.quantile(0.25)),
        "p75_bars_held": float(bars.quantile(0.75)),
        "median_days_held": float(trips["days_held"].median()),
    }


# --------------------------------------------------------------------------- #
# Aggregates
# --------------------------------------------------------------------------- #
def equity_metrics(equity: pd.Series, config: MetricsConfig | None = None) -> dict:
    """Every equity-curve and risk-adjusted figure in one call."""
    settings = config or MetricsConfig()
    result = {
        "bars": int(len(equity)),
        "initial_equity": float(equity.iloc[0]) if len(equity) else float("nan"),
        "final_equity": float(equity.iloc[-1]) if len(equity) else float("nan"),
        "total_return": total_return(equity),
        "cagr": cagr(equity, settings),
        "annualised_volatility": annualised_volatility(equity, settings),
        "sharpe": sharpe_ratio(equity, settings),
        "sortino": sortino_ratio(equity, settings),
        "calmar": calmar_ratio(equity, settings),
        "periods_per_year": _annualisation(equity, settings),
    }
    result.update(max_drawdown(equity))
    return result


def trade_metrics(
    trades: pd.DataFrame,
    index: pd.Index | None = None,
    config: MetricsConfig | None = None,
    total_financing: float | None = None,
) -> dict:
    """Every trade-level figure in one call.

    Parameters
    ----------
    total_financing:
        The run's financing cost. Supplied, it is apportioned across trips
        so trade metrics are net of carry and comparable with the equity
        curve. Omitted, they are gross of it.
    """
    settings = config or MetricsConfig()
    trips = round_trips(trades, index)
    if total_financing is not None:
        trips = attribute_financing(trips, total_financing)

    result = {"round_trips": int(len(trips)), "win_rate": win_rate(trips)}
    result.update(expectancy(trips))
    result.update(average_r_multiple(trips, settings))
    result.update(holding_time_distribution(trips))
    return result


def compute_metrics(execution_frame, config: MetricsConfig | None = None) -> dict:
    """Evaluate an execution result end to end.

    Read-only: the frame is not modified, and no simulator parameter is
    consulted or changed.
    """
    settings = config or MetricsConfig()
    equity = execution_frame.equity_curve

    if equity is None or equity.empty:
        return {"bars": 0, "round_trips": 0}

    pnl = execution_frame.pnl
    financing_total = float(-pnl["financing"].sum()) if not pnl.empty else None

    metrics = equity_metrics(equity, settings)
    metrics.update(
        trade_metrics(
            execution_frame.trades, equity.index, settings, financing_total
        )
    )

    if not pnl.empty:
        metrics["total_spread"] = float(-pnl["spread"].sum())
        metrics["total_slippage"] = float(-pnl["slippage"].sum())
        metrics["total_financing"] = float(-pnl["financing"].sum())
        metrics["gross_pnl"] = float(pnl["gap"].sum() + pnl["intraday"].sum())
        metrics["cost_drag"] = (
            metrics["total_spread"] + metrics["total_slippage"] + metrics["total_financing"]
        )

    exposure = getattr(execution_frame, "exposure", None)
    if exposure is not None and len(exposure):
        metrics["mean_gross_leverage"] = float(exposure.mean())
        metrics["max_gross_leverage"] = float(exposure.max())

    return metrics
