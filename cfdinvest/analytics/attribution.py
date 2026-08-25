"""Where the money came from, and where it went.

The execution engine already decomposes each bar into six components that
**must** sum to the change in equity. This module groups them into the terms
a person asks about — what the market gave, what the broker took — without
losing that identity. Every function here returns a decomposition whose parts
sum to the total, and the test suite asserts it rather than trusting it.

**One split is not free.** "How much came from the instrument and how much
from the currency" cannot be read off a completed run: the engine restates
every price into the account currency *before* the loop, so by the time P&L
exists the two are already added together. Recovering them needs the
instrument-currency prices and the per-bar exchange rate, which means handing
this module the analysis frame the run was built from. Without it,
:func:`compute_pnl_attribution` reports the combined market leg and says so —
a zero would read as "no currency effect", which is a different claim from
"not measured".

All functions are pure, deterministic and offline.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from cfdinvest.execution.pnl import PNL_COMPONENTS

__all__ = [
    "ATTRIBUTION_TERMS",
    "COST_TERMS",
    "compute_pnl_attribution",
    "compute_return_attribution",
    "compute_cost_breakdown",
    "compute_currency_attribution",
    "compute_instrument_attribution",
    "attribution_frame",
    "attribution_from_trades",
    "cost_breakdown_from_trades",
]

#: The terms every attribution reports, in the order they are presented.
#: ``instrument_pnl`` and ``fx_pnl`` together are the market leg; the rest
#: are what it cost to hold and trade.
ATTRIBUTION_TERMS: tuple[str, ...] = (
    "instrument_pnl",
    "fx_pnl",
    "spread_pnl",
    "slippage_pnl",
    "financing_pnl",
    "fees_pnl",
)

#: The subset that are costs. Reported as **negative** contributions, the
#: same sign convention the P&L frame uses, so that summing works without
#: anyone having to remember which way round a cost goes.
COST_TERMS: tuple[str, ...] = (
    "spread_pnl",
    "slippage_pnl",
    "financing_pnl",
    "fees_pnl",
)


def _pnl(execution_frame) -> pd.DataFrame:
    """The per-bar P&L frame, or an empty one."""
    if isinstance(execution_frame, pd.DataFrame):
        return execution_frame
    return getattr(execution_frame, "pnl", pd.DataFrame())


def _total(frame: pd.DataFrame, column: str) -> float:
    """One column's sum, tolerating its absence."""
    if frame is None or frame.empty or column not in frame.columns:
        return 0.0
    return float(pd.to_numeric(frame[column], errors="coerce").fillna(0.0).sum())


def compute_pnl_attribution(
    execution_frame,
    *,
    analysis_frame=None,
) -> dict[str, Any]:
    """Total P&L split into its sources, in account currency.

    The identity ``sum(ATTRIBUTION_TERMS) == total_pnl`` holds exactly, with
    stops, mid-bar exits and currency conversion all in play, because the
    terms are groupings of the engine's own components rather than a
    recalculation of them.

    Parameters
    ----------
    analysis_frame:
        Supply it to split the market leg into instrument and currency
        contributions. Without it ``instrument_pnl`` carries the whole market
        leg and ``currency_split`` reports why.

    Returns
    -------
    dict
        The six :data:`ATTRIBUTION_TERMS`, ``total_pnl``, the residual (zero
        by construction, reported so a caller can assert it), and a
        ``currency_split`` sub-dictionary.
    """
    pnl = _pnl(execution_frame)

    market = _total(pnl, "gap") + _total(pnl, "intraday")
    terms: dict[str, float] = {
        "instrument_pnl": market,
        "fx_pnl": 0.0,
        "spread_pnl": _total(pnl, "spread"),
        "slippage_pnl": _total(pnl, "slippage"),
        "financing_pnl": _total(pnl, "financing"),
        "fees_pnl": _total(pnl, "fx"),
    }

    split = compute_currency_attribution(execution_frame, analysis_frame)
    if split["available"]:
        # The reconstruction cannot be exact — it works close-to-close while
        # the engine splits each bar at the fill — so its residual stays with
        # the instrument leg. That keeps the headline identity exact and puts
        # the approximation where it is visible.
        terms["instrument_pnl"] = market - split["currency_pnl"]
        terms["fx_pnl"] = split["currency_pnl"]

    total = _total(pnl, "total")
    attributed = sum(terms.values())

    return {
        **terms,
        "market_pnl": market,
        "total_pnl": total,
        "residual": total - attributed,
        "currency_split": split,
    }


def compute_return_attribution(execution_frame, *, analysis_frame=None) -> dict:
    """The same split, as fractions of the equity the run started with.

    Returns rather than amounts, because an amount means nothing without the
    account size beside it and the two are rarely printed together.
    """
    attribution = compute_pnl_attribution(
        execution_frame, analysis_frame=analysis_frame
    )
    metadata = getattr(execution_frame, "metadata", {}) or {}

    try:
        initial = float(metadata.get("initial_equity"))
    except (TypeError, ValueError):
        initial = float("nan")
    if not np.isfinite(initial) or initial <= 0:
        equity = getattr(execution_frame, "equity_curve", pd.Series(dtype="float64"))
        initial = float(equity.iloc[0]) if len(equity) else float("nan")

    if not np.isfinite(initial) or initial <= 0:
        return {
            **{term: float("nan") for term in ATTRIBUTION_TERMS},
            "total_return": float("nan"),
            "initial_equity": float("nan"),
            "residual": float("nan"),
        }

    return {
        **{term: attribution[term] / initial for term in ATTRIBUTION_TERMS},
        "market_return": attribution["market_pnl"] / initial,
        "total_return": attribution["total_pnl"] / initial,
        "residual": attribution["residual"] / initial,
        "initial_equity": initial,
    }


def compute_cost_breakdown(execution_frame) -> dict[str, Any]:
    """Everything the run paid, and what share of the gross result it took.

    ``cost_share_of_gross`` is the number to look at: a strategy whose costs
    are a third of its gross profit is a different proposition from one where
    they are a twentieth, and both can show the same net figure.
    """
    pnl = _pnl(execution_frame)
    costs = {
        "spread": -_total(pnl, "spread"),
        "slippage": -_total(pnl, "slippage"),
        "financing": -_total(pnl, "financing"),
        "currency_conversion": -_total(pnl, "fx"),
    }
    total_cost = sum(costs.values())
    gross = _total(pnl, "gap") + _total(pnl, "intraday")
    net = _total(pnl, "total")

    return {
        **costs,
        "total_cost": total_cost,
        "gross_pnl": gross,
        "net_pnl": net,
        "cost_share_of_gross": (
            total_cost / gross if gross > 0 else float("nan")
        ),
        "shares": {
            name: (value / total_cost if total_cost > 0 else float("nan"))
            for name, value in costs.items()
        },
    }


# --------------------------------------------------------------------------- #
# Instrument versus currency
# --------------------------------------------------------------------------- #
def compute_currency_attribution(execution_frame, analysis_frame=None) -> dict:
    """How much of the market leg came from the exchange rate moving.

    For a position of ``u`` units held across a bar, with instrument price
    ``P`` and rate ``r`` (account currency per unit of instrument currency),
    the account-currency move decomposes exactly::

        u(P₁r₁ − P₀r₀) = u·r₀(P₁ − P₀)   ← the instrument moved
                       + u·P₀(r₁ − r₀)   ← the currency moved
                       + u(P₁−P₀)(r₁−r₀) ← both, at once

    The cross term is small but real, and is reported rather than quietly
    folded into one side. ``currency_pnl`` is the currency term plus the
    cross term: the cross term only exists because the currency moved, so
    attributing it to the instrument would flatter the instrument.

    **This is a reconstruction, not a reading.** It marks close-to-close
    while the engine splits each bar at the fill price, so it will not
    reproduce the engine's market leg to the penny; the difference is
    reported as ``reconstruction_error``. Returns ``available=False`` — with
    a reason — when currency conversion was off, or when the analysis frame
    is not supplied.
    """
    unavailable = {
        "available": False,
        "reason": "",
        "instrument_pnl": float("nan"),
        "currency_pnl": 0.0,
        "cross_term": float("nan"),
        "reconstruction_error": float("nan"),
    }

    metadata = getattr(execution_frame, "metadata", {}) or {}
    if not metadata.get("fx_enabled", False):
        return {
            **unavailable,
            "reason": "currency conversion was disabled for this run, so the "
            "whole market leg is the instrument",
        }
    if analysis_frame is None:
        return {
            **unavailable,
            "reason": "the analysis frame was not supplied; the instrument and "
            "currency legs are already combined in the stored P&L",
        }

    positions = getattr(execution_frame, "positions", pd.DataFrame())
    if positions is None or positions.empty:
        return {**unavailable, "reason": "the run held no positions"}

    try:
        rates = _rate_matrix(analysis_frame, positions, metadata, execution_frame)
        prices = _instrument_prices(analysis_frame, positions, rates)
    except Exception as error:  # noqa: BLE001 - a failed split is not a crash
        return {**unavailable, "reason": f"could not reconstruct rates: {error}"}

    if rates is None or prices is None:
        return {
            **unavailable,
            "reason": "no per-bar exchange rates are reconstructable for this run",
        }

    # The position held *into* each bar earns that bar's move, which is the
    # same convention the engine's gap leg uses.
    held = positions.shift(1).fillna(0.0).to_numpy(dtype="float64")
    price = prices.to_numpy(dtype="float64")
    rate = rates.to_numpy(dtype="float64")

    price_before, price_after = price[:-1], price[1:]
    rate_before, rate_after = rate[:-1], rate[1:]
    units = held[1:]

    price_move = np.nan_to_num(price_after - price_before)
    rate_move = np.nan_to_num(rate_after - rate_before)
    price_before = np.nan_to_num(price_before)
    rate_before = np.nan_to_num(rate_before)

    instrument = float(np.nansum(units * rate_before * price_move))
    currency = float(np.nansum(units * price_before * rate_move))
    cross = float(np.nansum(units * price_move * rate_move))

    pnl = _pnl(execution_frame)
    market = _total(pnl, "gap") + _total(pnl, "intraday")

    return {
        "available": True,
        "reason": "",
        "instrument_pnl": instrument,
        "currency_pnl": currency + cross,
        "currency_only": currency,
        "cross_term": cross,
        "reconstruction_error": market - (instrument + currency + cross),
        "market_pnl": market,
    }


def _rate_matrix(analysis_frame, positions, metadata, execution_frame):
    """Per-bar exchange rates, on the run's own grid.

    Built by the engine's own helper so that the forward-fill rule — and its
    refusal to look ahead — is the same one the run used. Importing it is a
    read of existing behaviour, not a change to it; reimplementing the rule
    here would be the riskier choice, because the two could drift apart
    without anything failing.
    """
    from cfdinvest.execution import engine as engine_module
    from cfdinvest.execution.fx import FXConfig

    fx = getattr(getattr(execution_frame, "config", None), "fx", None)
    if fx is None:
        fx = _fx_config_from_metadata(metadata, FXConfig)
    if fx is None or not fx.enabled:
        return None

    matrix = engine_module._fx_matrix(
        positions.index, positions.columns, analysis_frame, fx
    )
    return pd.DataFrame(matrix, index=positions.index, columns=positions.columns)


def _fx_config_from_metadata(metadata: dict, FXConfig):
    """Rebuild enough of the FX config from what the run recorded.

    The run summary stores the *policy* — fee, spread, source, basis — but
    not the per-instrument currency map, which is what decides which
    instruments convert at all. Without that map every rate resolves to 1.0
    and the split is trivially zero, so this returns ``None`` rather than a
    confidently wrong answer.
    """
    if not metadata.get("fx_enabled"):
        return None
    currencies = metadata.get("instrument_currencies")
    if not currencies:
        return None
    return FXConfig(
        enabled=True,
        account_currency=metadata.get("account_currency", "GBP"),
        instrument_currencies=dict(currencies),
        fx_rate_source=metadata.get("fx_rate_source", "config"),
        fx_rates=metadata.get("fx_rates") or {},
        fx_pair_tickers=metadata.get("fx_pair_tickers") or {},
    )


def _instrument_prices(analysis_frame, positions, rates):
    """Close prices in the instrument's own currency, on the run's grid."""
    close = (getattr(analysis_frame, "prices", {}) or {}).get("close")
    if close is None or close.empty or rates is None:
        return None
    aligned = close.reindex(index=positions.index, columns=positions.columns)
    return aligned


# --------------------------------------------------------------------------- #
# Presentation
# --------------------------------------------------------------------------- #
def compute_instrument_attribution(execution_frame) -> pd.DataFrame:
    """Realised P&L and costs per instrument, from the trade log.

    Per-instrument P&L is only available for *closed* trades: the trade log
    records what each fill realised, and an open position has realised
    nothing yet. The unrealised part of the book is therefore absent here by
    construction, not by omission.
    """
    trades = getattr(execution_frame, "trades", pd.DataFrame())
    columns = [
        "realized_pnl", "spread_cost", "slippage", "financing",
        "fx_conversion_fee", "fx_spread", "fx_slippage",
    ]
    if trades is None or trades.empty or "asset" not in trades.columns:
        return pd.DataFrame(columns=["trades", *columns, "net_pnl"])

    present = [column for column in columns if column in trades.columns]
    grouped = trades.groupby("asset")
    summary = grouped[present].sum() if present else pd.DataFrame(index=grouped.size().index)
    summary.insert(0, "trades", grouped.size())

    # Financing from the engine's per-bar accrual, where the frame carries
    # it. The trade log holds only the carry booked alongside a fill, so
    # summing it attributes nothing to the nights a position was merely
    # held -- and those are most of the nights. Left unfixed, the per-asset
    # net summed to £28,266 of loss on a run that actually lost £57,772,
    # and no column in the table said which of the two it was.
    accrued = getattr(execution_frame, "financing_by_asset", None)
    if accrued is not None and len(accrued) and "financing" in summary.columns:
        summary["financing"] = (
            pd.Series(accrued).reindex(summary.index).fillna(summary["financing"])
        )

    costs = sum(
        summary[column]
        for column in present
        if column != "realized_pnl"
    ) if len(present) > 1 else 0.0
    summary["net_pnl"] = summary.get("realized_pnl", 0.0) - costs
    summary.index.name = "asset"
    return summary.sort_values("net_pnl", ascending=False)


def attribution_frame(execution_frame, *, analysis_frame=None) -> pd.DataFrame:
    """The attribution as a table, for display.

    One row per term, with the amount and its share of the total. The
    residual row is kept even when it is zero: a decomposition that silently
    drops its residual is a decomposition nobody can check.
    """
    attribution = compute_pnl_attribution(
        execution_frame, analysis_frame=analysis_frame
    )
    labels = {
        "instrument_pnl": "Instrument price move",
        "fx_pnl": "Exchange rate move",
        "spread_pnl": "Spread",
        "slippage_pnl": "Slippage",
        "financing_pnl": "Overnight financing",
        "fees_pnl": "Currency conversion",
    }
    total = attribution["total_pnl"]

    rows = [
        {
            "component": labels[term],
            "amount": attribution[term],
            "share_of_total": (
                attribution[term] / total if total else float("nan")
            ),
        }
        for term in ATTRIBUTION_TERMS
    ]
    rows.append(
        {
            "component": "Unattributed residual",
            "amount": attribution["residual"],
            "share_of_total": (
                attribution["residual"] / total if total else float("nan")
            ),
        }
    )
    return pd.DataFrame(rows).set_index("component")


# --------------------------------------------------------------------------- #
# From persisted artefacts
# --------------------------------------------------------------------------- #
def attribution_from_trades(trades, equity) -> dict[str, Any]:
    """Attribution reconstructed from a trade log and an equity curve.

    The pipeline persists trades and equity but **not** the per-bar P&L
    decomposition, so a dashboard reading a completed run has no ``pnl``
    frame to group. It does not need one: every cost is charged on a trade
    and recorded there, so the cost legs are exact sums, and the market leg
    is then whatever is left::

        market = total + costs

    where ``total`` is the change in equity over the run. That makes the
    identity exact by construction rather than by luck — the market leg
    absorbs nothing because it is *defined* as the residual.

    What is lost is the split between the overnight gap and the intraday
    move, which only the engine's own decomposition carries.
    """
    empty = {
        **{term: 0.0 for term in ATTRIBUTION_TERMS},
        "market_pnl": 0.0,
        "total_pnl": 0.0,
        "residual": 0.0,
        "reconstructed": True,
        "currency_split": {"available": False, "reason": "reconstructed from trades"},
    }
    if equity is None or len(equity) < 2:
        return empty

    series = pd.to_numeric(pd.Series(equity).squeeze(), errors="coerce").dropna()
    if len(series) < 2:
        return empty
    total = float(series.iloc[-1] - series.iloc[0])

    def charged(column: str) -> float:
        if trades is None or getattr(trades, "empty", True):
            return 0.0
        if column not in trades.columns:
            return 0.0
        return float(
            pd.to_numeric(trades[column], errors="coerce").fillna(0.0).sum()
        )

    # Costs are recorded positive on a trade and enter P&L negative.
    spread = -charged("spread_cost")
    slippage = -charged("slippage")
    financing = -charged("financing")
    fees = -(
        charged("fx_conversion_fee") + charged("fx_spread") + charged("fx_slippage")
    )
    market = total - (spread + slippage + financing + fees)

    return {
        "instrument_pnl": market,
        "fx_pnl": 0.0,
        "spread_pnl": spread,
        "slippage_pnl": slippage,
        "financing_pnl": financing,
        "fees_pnl": fees,
        "market_pnl": market,
        "total_pnl": total,
        "residual": 0.0,
        "reconstructed": True,
        "currency_split": {
            "available": False,
            "reason": "the instrument and currency legs are already combined in "
            "the stored equity curve; supply the analysis frame to split them",
        },
    }


def cost_breakdown_from_trades(trades, equity) -> dict[str, Any]:
    """The cost table, reconstructed the same way."""
    attribution = attribution_from_trades(trades, equity)
    costs = {
        "spread": -attribution["spread_pnl"],
        "slippage": -attribution["slippage_pnl"],
        "financing": -attribution["financing_pnl"],
        "currency_conversion": -attribution["fees_pnl"],
    }
    total_cost = sum(costs.values())
    gross = attribution["market_pnl"]

    return {
        **costs,
        "total_cost": total_cost,
        "gross_pnl": gross,
        "net_pnl": attribution["total_pnl"],
        "cost_share_of_gross": total_cost / gross if gross > 0 else float("nan"),
        "shares": {
            name: (value / total_cost if total_cost > 0 else float("nan"))
            for name, value in costs.items()
        },
        "reconstructed": True,
    }
