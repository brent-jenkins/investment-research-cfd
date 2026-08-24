"""Performance metrics for a completed run.

**These functions delegate rather than reimplement.** The maths for Sharpe,
Sortino, Calmar, drawdown, win rate, expectancy and R multiples already lives
in :mod:`cfdinvest.backtest.metrics`, and a second implementation is not a
second opinion — it is a second number that can disagree with the first while
both claim to be the Sharpe ratio. What this module adds is a consistent
surface that takes an :class:`~cfdinvest.execution.frame.ExecutionFrame`
directly, plus the metrics that only exist once stops and currency conversion
are in the picture: gap-through incidence, currency exposure and leverage.

Every function is pure, deterministic and offline. None of them writes.

**A trade argument is forgiving.** Some callers have the raw trade log, some
have completed round trips; a round trip carries ``net_pnl`` and a trade log
does not, so the difference is detectable and is handled here rather than at
every call site.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

from cfdinvest.backtest import metrics as base

logger = logging.getLogger(__name__)

__all__ = [
    "MetricsConfig",
    "as_round_trips",
    "compute_sharpe_ratio",
    "compute_sortino_ratio",
    "compute_max_drawdown",
    "compute_calmar_ratio",
    "compute_win_rate",
    "compute_expectancy",
    "compute_avg_r",
    "compute_r_distribution",
    "compute_trade_duration_stats",
    "compute_leverage_stats",
    "compute_gap_through_stats",
    "compute_fx_exposure_stats",
    "compute_all_metrics",
]

#: Re-exported so a caller configures the conventions in one place.
MetricsConfig = base.MetricsConfig

#: Percentiles reported for any distribution this module summarises.
PERCENTILES: tuple[float, ...] = (5.0, 25.0, 50.0, 75.0, 95.0)


def _equity(frame) -> pd.Series:
    """The equity curve off an ExecutionFrame, or a Series passed straight in."""
    if isinstance(frame, pd.Series):
        return frame
    return getattr(frame, "equity_curve", pd.Series(dtype="float64"))


def _trades(frame) -> pd.DataFrame:
    """The trade log off an ExecutionFrame, or a frame passed straight in."""
    if isinstance(frame, pd.DataFrame):
        return frame
    return getattr(frame, "trades", pd.DataFrame())


def _config(risk_free: float, config: MetricsConfig | None) -> MetricsConfig:
    """Honour an explicit config, or build one from the risk-free rate.

    The spec's signature takes ``risk_free`` directly, which is convenient
    and insufficient — annualisation and the R basis matter too. Both are
    accepted; an explicit config wins.
    """
    if config is not None:
        return config
    return MetricsConfig(risk_free_rate=risk_free)


def as_round_trips(trades, index: pd.Index | None = None) -> pd.DataFrame:
    """Completed round trips, whether given a trade log or trips already.

    Pure. A round-trip frame carries ``net_pnl``; a raw trade log does not,
    which is how the two are told apart without the caller having to say.
    """
    frame = _trades(trades)
    if frame is None or frame.empty:
        return pd.DataFrame()
    if "net_pnl" in frame.columns:
        return frame
    return base.round_trips(frame, index)


# --------------------------------------------------------------------------- #
# Equity-curve metrics
# --------------------------------------------------------------------------- #
def compute_sharpe_ratio(
    frame, risk_free: float = 0.0, *, config: MetricsConfig | None = None
) -> float:
    """Annualised excess return over annualised volatility."""
    return base.sharpe_ratio(_equity(frame), _config(risk_free, config))


def compute_sortino_ratio(
    frame, risk_free: float = 0.0, *, config: MetricsConfig | None = None
) -> float:
    """Sharpe, but penalising only downside deviation."""
    return base.sortino_ratio(_equity(frame), _config(risk_free, config))


def compute_max_drawdown(frame) -> dict[str, float]:
    """Deepest peak-to-trough fall, with when it happened and how long it lasted."""
    return base.max_drawdown(_equity(frame))


def compute_calmar_ratio(
    frame, *, config: MetricsConfig | None = None
) -> float:
    """Compound annual growth over the maximum drawdown."""
    return base.calmar_ratio(_equity(frame), config)


def compute_leverage_stats(frame) -> dict[str, float]:
    """Where gross leverage sat over the run.

    Reads ``exposure``, which the engine records **as the cap enforced it**:
    fill-price notional over the equity known at trade time. Close-marked
    leverage can drift above the cap after an adverse move — true of the
    resulting position, but not something a cap could have prevented.

    ``breaches`` counts bars above the configured cap, which should be zero;
    anything else means the cap is not doing what it claims.
    """
    exposure = getattr(frame, "exposure", pd.Series(dtype="float64"))
    cap = float((getattr(frame, "metadata", {}) or {}).get("max_leverage", np.nan))

    if exposure is None or exposure.empty:
        return {
            "mean_leverage": float("nan"),
            "median_leverage": float("nan"),
            "max_leverage": float("nan"),
            "p95_leverage": float("nan"),
            "bars_levered": 0,
            "leverage_cap": cap,
            "cap_breaches": 0,
        }

    values = pd.to_numeric(exposure, errors="coerce").dropna()
    breaches = int((values > cap + 1e-9).sum()) if np.isfinite(cap) else 0

    return {
        "mean_leverage": float(values.mean()),
        "median_leverage": float(values.median()),
        "max_leverage": float(values.max()),
        "p95_leverage": float(values.quantile(0.95)),
        "bars_levered": int((values > 0).sum()),
        "leverage_cap": cap,
        "cap_breaches": breaches,
    }


# --------------------------------------------------------------------------- #
# Trade metrics
# --------------------------------------------------------------------------- #
def compute_win_rate(trades, column: str = "net_pnl") -> float:
    """Fraction of completed round trips that made money."""
    return base.win_rate(as_round_trips(trades), column)


def compute_expectancy(trades, column: str = "net_pnl") -> dict[str, float]:
    """Average outcome per trade, and the pieces it decomposes into."""
    return base.expectancy(as_round_trips(trades), column)


def compute_avg_r(trades, *, config: MetricsConfig | None = None) -> dict[str, float]:
    """Average R multiple per trade.

    R is *reconstructed*, not recorded: execution does not store the risk a
    trade was intended to carry, so :class:`MetricsConfig` decides how 1R is
    inferred. Two runs with different ``r_basis`` values are not comparable
    on this number, which is why the basis is returned alongside it.

    Each basis needs a different column — ``entry_equity`` for the default,
    ``entry_notional`` for ``"notional"`` — and a caller holding a partial
    trip frame should get "not computable" rather than a ``KeyError``. R is
    the one metric here that depends on columns the trade log does not
    carry, so it is the one that has to tolerate their absence.
    """
    settings = config or MetricsConfig()
    trips = as_round_trips(trades)
    try:
        return base.average_r_multiple(trips, settings)
    except KeyError as missing:
        logger.debug("R multiples unavailable: no %s column", missing)
        return {
            "average_r": float("nan"),
            "median_r": float("nan"),
            "r_basis": settings.r_basis,
            "risk_per_trade": float("nan"),
        }


def compute_r_distribution(
    trades, *, config: MetricsConfig | None = None
) -> dict[str, Any]:
    """The shape of the R distribution, not only its mean.

    A strategy with a 0.2R average made of many small losses and rare large
    wins is a different thing to hold than one that grinds out 0.2R every
    time, and the average cannot tell them apart.
    """
    trips = as_round_trips(trades)
    summary = compute_avg_r(trips, config=config)

    empty = {
        **summary,
        "count": 0,
        "percentiles": {},
        "best": float("nan"),
        "worst": float("nan"),
        "positive": 0,
        "negative": 0,
    }
    if trips.empty or "r_multiple" not in trips.columns:
        return empty

    values = pd.to_numeric(trips["r_multiple"], errors="coerce").dropna()
    if values.empty:
        return empty

    return {
        **summary,
        "count": int(len(values)),
        "percentiles": {
            f"p{int(p)}": float(values.quantile(p / 100.0)) for p in PERCENTILES
        },
        "best": float(values.max()),
        "worst": float(values.min()),
        "positive": int((values > 0).sum()),
        "negative": int((values <= 0).sum()),
    }


def compute_trade_duration_stats(trades) -> dict[str, float]:
    """How long positions were held, as a churn diagnostic.

    A median of one or two bars means the hysteresis band is too narrow and
    the strategy is paying the spread to stand still.
    """
    return base.holding_time_distribution(as_round_trips(trades))


# --------------------------------------------------------------------------- #
# Stops and currency — only meaningful once those modules are enabled
# --------------------------------------------------------------------------- #
def compute_gap_through_stats(trades) -> dict[str, Any]:
    """How often a stop-loss or take-profit filled worse than its level.

    **The number that matters is ``gap_share``.** A stop bounds the loss at
    its level *only if the level trades*; a bar that gapped past it filled at
    the open instead. A strategy whose exits are mostly gaps is one whose
    backtested stop distances are fiction, and no amount of tightening them
    will help.
    """
    frame = _trades(trades)
    empty = {
        "level_exits": 0,
        "stop_losses": 0,
        "take_profits": 0,
        "gap_through_exits": 0,
        "gap_share": float("nan"),
        "total_slippage_applied": 0.0,
        "mean_slippage_applied": float("nan"),
        "available": False,
    }
    if frame is None or frame.empty or "reason" not in frame.columns:
        return empty
    if "gap_through" not in frame.columns:
        # A trade log written before the stops module existed. Reporting
        # zero gaps would read as "no gap risk", which is a different claim
        # from "not recorded".
        return empty

    reasons = frame["reason"].astype(str)
    stops = frame[reasons == "stop_loss"]
    targets = frame[reasons == "take_profit"]
    exits = frame[reasons.isin(("stop_loss", "take_profit"))]

    if exits.empty:
        return {**empty, "available": True}

    gapped = pd.Series(exits["gap_through"]).fillna(False).astype(bool)
    slippage = pd.to_numeric(
        exits.get("slippage_applied", pd.Series(0.0, index=exits.index)),
        errors="coerce",
    ).fillna(0.0)

    return {
        "level_exits": int(len(exits)),
        "stop_losses": int(len(stops)),
        "take_profits": int(len(targets)),
        "gap_through_exits": int(gapped.sum()),
        "gap_share": float(gapped.mean()),
        "total_slippage_applied": float(slippage.sum()),
        "mean_slippage_applied": float(slippage.mean()),
        "available": True,
    }


def compute_fx_exposure_stats(frame) -> dict[str, Any]:
    """How much of the book crossed a currency boundary, and what it cost.

    ``converted_notional_share`` is the fraction of traded notional in
    instruments quoted outside the account currency — the exposure the
    conversion fee is charged on, and the exposure that carries an unhedged
    currency risk the equity curve absorbs silently.
    """
    trades = _trades(frame)
    metadata = getattr(frame, "metadata", {}) or {}
    empty = {
        "enabled": bool(metadata.get("fx_enabled", False)),
        "account_currency": metadata.get("account_currency"),
        "converted_trades": 0,
        "converted_notional_share": float("nan"),
        "total_currency_cost": 0.0,
        "conversion_fees": 0.0,
        "currency_spread": 0.0,
        "currency_slippage": 0.0,
        "cost_share_of_gross": float("nan"),
        "rates_used": {},
        "available": False,
    }
    if trades is None or trades.empty or "fx_rate" not in trades.columns:
        return empty

    rates = pd.to_numeric(trades["fx_rate"], errors="coerce")
    converted = rates.notna() & (rates != 1.0)
    notional = pd.to_numeric(
        trades.get("notional", pd.Series(0.0, index=trades.index)), errors="coerce"
    ).fillna(0.0)

    def total(column: str) -> float:
        if column not in trades.columns:
            return 0.0
        return float(
            pd.to_numeric(trades[column], errors="coerce").fillna(0.0).sum()
        )

    fees = total("fx_conversion_fee")
    spread = total("fx_spread")
    slippage = total("fx_slippage")
    gross = float(notional.sum())

    rates_used = getattr(frame, "fx_rate_used", pd.Series(dtype="float64"))
    return {
        "enabled": bool(metadata.get("fx_enabled", False)),
        "account_currency": metadata.get("account_currency"),
        "converted_trades": int(converted.sum()),
        "converted_notional_share": (
            float(notional[converted].sum() / gross) if gross > 0 else float("nan")
        ),
        "total_currency_cost": fees + spread + slippage,
        "conversion_fees": fees,
        "currency_spread": spread,
        "currency_slippage": slippage,
        "cost_share_of_gross": (
            (fees + spread + slippage) / gross if gross > 0 else float("nan")
        ),
        # `series or {}` is ambiguous on a Series -- pandas refuses to say
        # whether one is truthy -- so emptiness is asked about explicitly.
        "rates_used": (
            {}
            if rates_used is None or len(rates_used) == 0
            else {
                str(k): float(v)
                for k, v in rates_used.items()
                if pd.notna(v)
            }
        ),
        "available": True,
    }


# --------------------------------------------------------------------------- #
# Everything at once
# --------------------------------------------------------------------------- #
def compute_all_metrics(
    execution_frame, *, config: MetricsConfig | None = None
) -> dict[str, Any]:
    """Every metric in this module, as one flat-ish dictionary.

    What a run's analytics record holds. Nested only where a metric is
    genuinely a group (drawdown, expectancy, the R distribution), so that a
    caller writing a table does not have to flatten by hand.
    """
    settings = config or MetricsConfig()
    equity = _equity(execution_frame)
    trips = as_round_trips(
        _trades(execution_frame),
        equity.index if isinstance(equity, pd.Series) and not equity.empty else None,
    )

    return {
        "sharpe_ratio": compute_sharpe_ratio(execution_frame, config=settings),
        "sortino_ratio": compute_sortino_ratio(execution_frame, config=settings),
        "calmar_ratio": compute_calmar_ratio(execution_frame, config=settings),
        "total_return": base.total_return(equity),
        "cagr": base.cagr(equity, settings),
        "annualised_volatility": base.annualised_volatility(equity, settings),
        "drawdown": compute_max_drawdown(execution_frame),
        "win_rate": compute_win_rate(trips),
        "expectancy": compute_expectancy(trips),
        "r_distribution": compute_r_distribution(trips, config=settings),
        "duration": compute_trade_duration_stats(trips),
        "leverage": compute_leverage_stats(execution_frame),
        "gap_through": compute_gap_through_stats(_trades(execution_frame)),
        "currency": compute_fx_exposure_stats(execution_frame),
        "trade_count": int(len(trips)),
        "r_basis": settings.r_basis,
    }
