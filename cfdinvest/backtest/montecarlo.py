"""Monte Carlo resampling: how much of the result was sequence luck?

A backtest produces one path. These functions ask what the *distribution* of
paths looks like if the same ingredients had arrived in a different order.

Two resamplers, answering different questions:

``resample_trades``
    Shuffles completed round trips. Holds the strategy's edge fixed and
    varies only the order it was realised in, which is what reveals how much
    of a drawdown was sequencing rather than the trades themselves.
``resample_returns``
    Block-bootstraps bar returns. Blocks preserve short-horizon
    autocorrelation — volatility clustering, momentum — that an i.i.d.
    shuffle destroys, which otherwise makes every strategy look far safer
    than it is.

**Every function is seeded and deterministic.** The same seed and inputs
give the same distribution, so a result can be reproduced and diffed.

**What this does not do.** Resampling assumes the trades or returns are
exchangeable draws from a stable process. They are not: a regime shift
changes the distribution, and no reshuffling of the past will contain a
future that has not happened yet. Treat the output as a sensitivity check on
sequencing, not as a forecast.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

__all__ = [
    "MonteCarloConfig",
    "resample_trades",
    "resample_returns",
    "summarise_simulations",
]


@dataclass(frozen=True, slots=True)
class MonteCarloConfig:
    """Settings for a resampling run."""

    simulations: int = 1_000
    #: Seeds the generator. Fixed by default so runs are reproducible.
    seed: int = 12_345
    #: Bars per block for the block bootstrap. ``1`` is an i.i.d. shuffle,
    #: which destroys volatility clustering and flatters the result.
    block_size: int = 5
    #: Percentiles reported in the summary.
    percentiles: tuple[float, ...] = (5.0, 25.0, 50.0, 75.0, 95.0)

    def __post_init__(self) -> None:
        if self.simulations <= 0:
            raise ValueError(f"simulations must be > 0, got {self.simulations}")
        if self.block_size <= 0:
            raise ValueError(f"block_size must be > 0, got {self.block_size}")


def _path_statistics(paths: np.ndarray, initial_equity: float) -> pd.DataFrame:
    """Final return and worst drawdown of every simulated path.

    Pure and fully vectorised across simulations: the running maximum and
    the drawdown are computed on the whole ``(simulations, steps)`` matrix
    at once rather than path by path.
    """
    equity = initial_equity + np.cumsum(paths, axis=1)
    equity = np.concatenate(
        [np.full((equity.shape[0], 1), initial_equity), equity], axis=1
    )

    running_peak = np.maximum.accumulate(equity, axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        drawdown = np.where(running_peak > 0, (equity - running_peak) / running_peak, np.nan)

    final = equity[:, -1]
    return pd.DataFrame(
        {
            "final_equity": final,
            "total_return": final / initial_equity - 1.0,
            "max_drawdown_fraction": np.nanmin(drawdown, axis=1),
        }
    )


def resample_trades(
    trips: pd.DataFrame,
    initial_equity: float,
    config: MonteCarloConfig | None = None,
    *,
    column: str = "net_pnl",
) -> pd.DataFrame:
    """Reorder completed trades and re-accumulate the equity path.

    Sampling is **with replacement**, so a simulation may repeat or omit
    trades — the standard bootstrap. Sampling without replacement would only
    permute the same fixed set and could not widen the distribution of
    outcomes, only of drawdowns.

    Parameters
    ----------
    trips:
        Completed round trips from
        :func:`~cfdinvest.backtest.metrics.round_trips`.
    initial_equity:
        Starting balance the paths accumulate from.

    Returns
    -------
    pandas.DataFrame
        One row per simulation: ``final_equity``, ``total_return``,
        ``max_drawdown_fraction``.
    """
    settings = config or MonteCarloConfig()
    if trips is None or trips.empty or initial_equity <= 0:
        return pd.DataFrame(
            columns=["final_equity", "total_return", "max_drawdown_fraction"]
        )

    outcomes = trips[column].to_numpy(dtype="float64")
    generator = np.random.default_rng(settings.seed)
    draws = generator.integers(
        0, len(outcomes), size=(settings.simulations, len(outcomes))
    )
    return _path_statistics(outcomes[draws], initial_equity)


def resample_returns(
    returns: pd.Series,
    initial_equity: float,
    config: MonteCarloConfig | None = None,
) -> pd.DataFrame:
    """Block-bootstrap bar returns into alternative equity paths.

    Blocks of ``block_size`` consecutive bars are drawn with replacement and
    concatenated. Keeping bars adjacent preserves the short-horizon
    dependence — a violent day tends to follow a violent day — that an
    i.i.d. shuffle removes, and removing it understates tail risk badly.

    Returns are compounded rather than summed, so the path is
    multiplicative like a real account.

    Returns
    -------
    pandas.DataFrame
        One row per simulation.
    """
    settings = config or MonteCarloConfig()
    clean = (
        returns.replace([np.inf, -np.inf], np.nan).dropna()
        if returns is not None
        else pd.Series(dtype="float64")
    )
    if clean.empty or initial_equity <= 0:
        return pd.DataFrame(
            columns=["final_equity", "total_return", "max_drawdown_fraction"]
        )

    values = clean.to_numpy(dtype="float64")
    n_bars = len(values)
    block = min(settings.block_size, n_bars)
    n_blocks = int(np.ceil(n_bars / block))

    generator = np.random.default_rng(settings.seed)
    starts = generator.integers(
        0, max(n_bars - block + 1, 1), size=(settings.simulations, n_blocks)
    )
    offsets = np.arange(block)
    sampled = values[np.clip(starts[:, :, None] + offsets, 0, n_bars - 1)]
    sampled = sampled.reshape(settings.simulations, -1)[:, :n_bars]

    equity = initial_equity * np.cumprod(1.0 + sampled, axis=1)
    equity = np.concatenate(
        [np.full((settings.simulations, 1), initial_equity), equity], axis=1
    )

    running_peak = np.maximum.accumulate(equity, axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        drawdown = np.where(running_peak > 0, (equity - running_peak) / running_peak, np.nan)

    final = equity[:, -1]
    return pd.DataFrame(
        {
            "final_equity": final,
            "total_return": final / initial_equity - 1.0,
            "max_drawdown_fraction": np.nanmin(drawdown, axis=1),
        }
    )


def summarise_simulations(
    simulations: pd.DataFrame, config: MonteCarloConfig | None = None
) -> pd.DataFrame:
    """Percentiles of each simulated statistic.

    Pure.

    Returns
    -------
    pandas.DataFrame
        Rows are statistics, columns are ``mean``, ``std`` and one column
        per requested percentile (``p5``, ``p50``, ...), plus
        ``probability_positive`` for the return column.
    """
    settings = config or MonteCarloConfig()
    if simulations is None or simulations.empty:
        return pd.DataFrame()

    rows = []
    for name, values in simulations.items():
        clean = values.replace([np.inf, -np.inf], np.nan).dropna()
        if clean.empty:
            continue
        row = {
            "statistic": name,
            "mean": float(clean.mean()),
            "std": float(clean.std(ddof=1)) if len(clean) > 1 else 0.0,
        }
        for percentile in settings.percentiles:
            row[f"p{percentile:g}"] = float(np.percentile(clean, percentile))
        row["probability_positive"] = float((clean > 0).mean())
        rows.append(row)

    return pd.DataFrame(rows).set_index("statistic")
