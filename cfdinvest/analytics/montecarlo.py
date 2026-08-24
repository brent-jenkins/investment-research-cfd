"""Trade-level resampling: what else could have happened.

One backtest is one sample from a distribution of outcomes. A strategy that
returned 12% might sit comfortably in the middle of that distribution or right
out in its tail, and the equity curve alone cannot tell you which. Resampling
the completed trades in a different order draws more samples from the same
distribution and shows the spread.

**Time ordering is deliberately destroyed, and that has a cost.** Drawing
trades independently and with replacement preserves the distribution of
individual outcomes exactly, and discards every relationship *between* them:
the losing streak that came from one bad fortnight, the volatility clustering
that made three stops fire in a week. Real losing runs are longer than
independent draws produce, so **the drawdown distribution here is optimistic**
— it is a floor on how bad things get, not an estimate of it. For serial
structure, use the block bootstrap in
:mod:`cfdinvest.backtest.montecarlo`, which resamples bar returns in blocks.

Reproducible: the same trades, path count and seed always produce the same
paths, and a test pins it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Sequence

import numpy as np
import pandas as pd

from cfdinvest.analytics.metrics import as_round_trips

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_SEED",
    "PERCENTILES",
    "ResampledPath",
    "resample_trades",
    "compute_montecarlo_metrics",
    "paths_frame",
    "summary_frame",
]

#: Fixed so a run is reproducible without the caller having to think about it.
DEFAULT_SEED: int = 12_345

#: Reported for every resampled metric. The 5th percentile is the one that
#: matters: it is the bad-but-not-unthinkable outcome to plan around.
PERCENTILES: tuple[float, ...] = (1.0, 5.0, 25.0, 50.0, 75.0, 95.0, 99.0)


@dataclass(frozen=True, slots=True)
class ResampledPath:
    """One synthetic ordering of the same trades.

    Attributes
    ----------
    pnl:
        Per-trade profit and loss, in the resampled order.
    equity:
        The equity curve that sequence produces, starting at
        ``initial_equity``.
    metrics:
        What this path scored: drawdown, per-trade ratio, expectancy, final
        equity.
    """

    number: int
    pnl: np.ndarray
    equity: np.ndarray
    initial_equity: float
    metrics: dict[str, float] = field(default_factory=dict)

    @property
    def final_equity(self) -> float:
        return float(self.equity[-1]) if len(self.equity) else self.initial_equity

    def to_series(self) -> pd.Series:
        """The equity path as a Series, indexed by trade number."""
        return pd.Series(self.equity, name=f"path_{self.number}")


def _trade_pnl(trades, column: str = "net_pnl") -> np.ndarray:
    """Per-trade P&L from a trade log or a round-trip frame.

    Round trips, not raw fills: a fill is half a decision, and resampling
    fills would break every open position apart from its close.
    """
    trips = as_round_trips(trades)
    if trips is None or trips.empty or column not in trips.columns:
        return np.array([], dtype="float64")
    values = pd.to_numeric(trips[column], errors="coerce").dropna()
    return values.to_numpy(dtype="float64")


def _path_metrics(
    pnl: np.ndarray, equity: np.ndarray, initial: float, trades_per_year: float | None
) -> dict[str, float]:
    """Score one synthetic path."""
    if pnl.size == 0:
        return {
            "final_equity": initial,
            "total_return": 0.0,
            "max_drawdown": 0.0,
            "max_drawdown_fraction": 0.0,
            "expectancy": float("nan"),
            "trade_sharpe": float("nan"),
            "annualised_sharpe": float("nan"),
            "win_rate": float("nan"),
        }

    peak = np.maximum.accumulate(equity)
    drawdown = equity - peak
    with np.errstate(invalid="ignore", divide="ignore"):
        fraction = np.where(peak > 0, drawdown / peak, 0.0)

    spread = float(pnl.std(ddof=1)) if pnl.size > 1 else 0.0
    per_trade = float(pnl.mean() / spread) if spread > 0 else float("nan")
    annualised = (
        per_trade * float(np.sqrt(trades_per_year))
        if trades_per_year and np.isfinite(per_trade)
        else float("nan")
    )

    return {
        "final_equity": float(equity[-1]),
        "total_return": float(equity[-1] / initial - 1.0) if initial > 0 else float("nan"),
        "max_drawdown": float(drawdown.min()),
        "max_drawdown_fraction": float(fraction.min()),
        "expectancy": float(pnl.mean()),
        "trade_sharpe": per_trade,
        "annualised_sharpe": annualised,
        "win_rate": float((pnl > 0).mean()),
    }


def resample_trades(
    trades,
    n_paths: int = 1_000,
    seed: int = DEFAULT_SEED,
    *,
    initial_equity: float = 100_000.0,
    column: str = "net_pnl",
    trades_per_year: float | None = None,
    replace: bool = True,
) -> list[ResampledPath]:
    """Draw ``n_paths`` synthetic orderings of the same completed trades.

    Pure and reproducible: identical inputs and seed give identical paths.

    Parameters
    ----------
    replace:
        ``True`` (the default) samples with replacement, which varies the
        *composition* as well as the order and is the usual bootstrap.
        ``False`` shuffles the actual trades, which holds the total return
        fixed and varies only the path — useful for isolating "was this
        drawdown bad luck in the ordering?" from "was this return luck?".
    trades_per_year:
        Needed to annualise the per-trade ratio. Without it,
        ``annualised_sharpe`` is NaN rather than a number in unstated units.

    Raises
    ------
    ValueError
        On a non-positive path count.
    """
    if n_paths <= 0:
        raise ValueError(f"n_paths must be > 0, got {n_paths}")

    pnl = _trade_pnl(trades, column)
    if pnl.size == 0:
        logger.warning("No completed trades to resample; returning no paths")
        return []

    rng = np.random.default_rng(seed)
    paths: list[ResampledPath] = []

    for number in range(n_paths):
        if replace:
            drawn = rng.choice(pnl, size=pnl.size, replace=True)
        else:
            drawn = rng.permutation(pnl)
        equity = initial_equity + np.cumsum(drawn)
        paths.append(
            ResampledPath(
                number=number,
                pnl=drawn,
                equity=equity,
                initial_equity=initial_equity,
                metrics=_path_metrics(drawn, equity, initial_equity, trades_per_year),
            )
        )

    return paths


def compute_montecarlo_metrics(
    paths: Sequence[ResampledPath], *, percentiles: Sequence[float] = PERCENTILES
) -> dict[str, Any]:
    """The distribution of outcomes across resampled paths.

    ``tail_loss`` is the 5th percentile of final return: the outcome that a
    twentieth of paths did worse than. It is the number to size against, and
    it is optimistic for the reason in this module's docstring.

    ``probability_of_loss`` counts paths that finished below where they
    started — a more legible statement of the same distribution.
    """
    if not paths:
        return {
            "paths": 0,
            "distributions": {},
            "tail_loss": float("nan"),
            "median_return": float("nan"),
            "probability_of_loss": float("nan"),
            "worst_drawdown": float("nan"),
            "median_drawdown": float("nan"),
        }

    names = (
        "final_equity", "total_return", "max_drawdown", "max_drawdown_fraction",
        "expectancy", "trade_sharpe", "annualised_sharpe", "win_rate",
    )
    collected = {
        name: np.array([path.metrics.get(name, np.nan) for path in paths], dtype=float)
        for name in names
    }

    distributions = {
        name: {
            "mean": _safe(np.nanmean, values),
            "std": _safe(np.nanstd, values),
            **{
                f"p{_percentile_label(p)}": _safe(
                    lambda v, q=p: np.nanpercentile(v, q), values
                )
                for p in percentiles
            },
        }
        for name, values in collected.items()
    }

    returns = collected["total_return"]
    drawdowns = collected["max_drawdown_fraction"]

    return {
        "paths": len(paths),
        "trades_per_path": int(paths[0].pnl.size),
        "distributions": distributions,
        "tail_loss": _safe(lambda v: np.nanpercentile(v, 5.0), returns),
        "median_return": _safe(np.nanmedian, returns),
        "probability_of_loss": float(np.nanmean(returns < 0)) if returns.size else float("nan"),
        "worst_drawdown": _safe(np.nanmin, drawdowns),
        "median_drawdown": _safe(np.nanmedian, drawdowns),
    }


def _percentile_label(value: float) -> str:
    """``5.0`` -> ``5``; ``2.5`` -> ``2_5``."""
    return f"{value:g}".replace(".", "_")


def _safe(function, values: np.ndarray) -> float:
    """Apply a reducer, tolerating an all-NaN input."""
    if values.size == 0 or not np.isfinite(values).any():
        return float("nan")
    try:
        return float(function(values))
    except (ValueError, TypeError):
        return float("nan")


def paths_frame(paths: Sequence[ResampledPath]) -> pd.DataFrame:
    """One row per path, for a histogram or a scatter."""
    if not paths:
        return pd.DataFrame()
    return pd.DataFrame([{"path": p.number, **p.metrics} for p in paths]).set_index(
        "path"
    )


def summary_frame(summary: dict[str, Any]) -> pd.DataFrame:
    """The distribution table, one row per metric."""
    distributions = summary.get("distributions") or {}
    if not distributions:
        return pd.DataFrame()
    return pd.DataFrame(distributions).T
