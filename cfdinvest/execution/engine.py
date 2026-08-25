"""The execution loop: signals in, trades and an equity curve out.

**Why there is a loop at all.** Everything upstream is vectorised over the
whole panel because each bar's answer depends only on that bar's inputs.
Execution is different: today's position depends on yesterday's, the risk
engine's leverage cap divides by an equity that is itself a running total,
and a stop-out is by definition path-dependent. That sequence is
irreducible. What *can* be vectorised is the asset axis, so the loop walks
time one bar at a time and every step inside it is a handful of numpy
operations across the whole universe at once.

**Order of operations within a bar** — this is the part that is easy to get
subtly wrong, so it is fixed and documented:

1. Accrue financing on the position carried *into* the bar.
2. Earn the overnight gap on that same old position, up to the fill price.
3. Decide the new target from the (lagged) signal, the hysteresis band and
   the risk caps.
4. Trade the difference at the fill price, paying spread and slippage;
   crystallise realised P&L on whatever was closed; convert the opened and
   closed portions across currencies and pay for that too.
5. Earn the rest of the bar on the new position, fill price to close.
6. Roll equity forward.

Steps 2 and 5 split the bar at the fill so a position opened mid-bar is not
credited with the move that happened before it existed.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from cfdinvest.execution import fills as fills_module
from cfdinvest.execution import financing as financing_module
from cfdinvest.execution import fx as fx_module
from cfdinvest.execution import pnl as pnl_module
from cfdinvest.execution import state as state_module
from cfdinvest.execution import stops as stops_module
from cfdinvest.schema import TICKER, TIMESTAMP

logger = logging.getLogger(__name__)

__all__ = ["TRADE_COLUMNS", "run_execution"]

#: Columns of the trade log, in order.
TRADE_COLUMNS: tuple[str, ...] = (
    "timestamp",
    "asset",
    "action",
    "units",
    "traded_units",
    "price",
    "fill_price",
    "spread_cost",
    "slippage",
    "financing",
    "realized_pnl",
    "notional",
    "equity",
    "reason",
    # Stop / take-profit context. Present on every row: the levels a position
    # is carrying are as useful on an ordinary trade as on the exit they
    # eventually cause.
    "stop_level",
    "take_profit_level",
    "stop_fill_price",
    "take_profit_fill_price",
    "gap_through",
    "slippage_applied",
    "breach_type",
    # FX context. The rate a trade was struck at is the one number a manual
    # execution most needs and the one a backtest most often forgets.
    *fx_module.FX_TRADE_COLUMNS,
)


def _as_matrix(frame: pd.DataFrame, index, columns, fill=np.nan) -> np.ndarray:
    """Align a panel to the run's grid and return it as a float matrix."""
    if frame is None or frame.empty:
        return np.full((len(index), len(columns)), fill, dtype="float64")
    return (
        frame.reindex(index=index, columns=columns)
        .to_numpy(dtype="float64", na_value=fill)
    )


def _as_labels(frame: pd.DataFrame, index, columns) -> np.ndarray:
    """Align a label panel, filling gaps with ``flat``."""
    if frame is None or frame.empty:
        return np.full((len(index), len(columns)), "flat", dtype=object)
    aligned = frame.reindex(index=index, columns=columns)
    return aligned.astype("object").fillna("flat").to_numpy()


def _fx_matrix(index, columns, analysis_frame, fx_config) -> np.ndarray:
    """A ``bars x assets`` matrix of account-currency-per-instrument rates.

    A matrix rather than a per-ticker constant because the rate genuinely
    moves, and because using one rate for a whole decade would put an
    end-of-run rate on a start-of-run trade — look-ahead, and of a kind that
    is easy to miss because it hides inside a cost model.

    The ``"lake"`` source therefore reads the FX series bar by bar and
    **forward-fills only**: a rate is carried forward from the last day it
    was known, never backward from a day it was not.
    """
    ones = np.ones((len(index), len(columns)), dtype="float64")
    if not fx_config.enabled:
        return ones

    # The lake source supplies its own per-bar rates below, so the resolved
    # constants are only its fallback and must not warn about being one.
    resolved = fx_module.resolve_fx_rates(
        columns, fx_config, warn=fx_config.fx_rate_source != "lake"
    ).to_numpy()
    matrix = ones * resolved

    if fx_config.fx_rate_source != "lake":
        return matrix

    close_panel = analysis_frame.prices.get("close", pd.DataFrame())
    currencies = fx_config.instrument_currencies or {}
    pairs = fx_config.fx_pair_tickers or {}

    for position, ticker in enumerate(columns):
        currency = currencies.get(str(ticker), fx_config.account_currency)
        if currency == fx_config.account_currency:
            continue
        pair = pairs.get(currency)
        if pair is None or close_panel.empty or pair not in close_panel.columns:
            logger.warning(
                "fx_rate_source='lake' but no %s series for %s in the lake "
                "(fx_pair_tickers: %r); using the configured rate %.6g.",
                currency, ticker, pair, matrix[0, position],
            )
            continue

        series = close_panel[pair].reindex(index).ffill()
        usable = series.to_numpy(dtype="float64", na_value=np.nan)
        # Where the lake has nothing yet -- before the pair's first print --
        # keep the configured fallback rather than a NaN that would silently
        # blank out every price for that instrument.
        matrix[:, position] = np.where(
            np.isfinite(usable) & (usable > 0), usable, matrix[:, position]
        )

    return matrix


def run_execution(signal_frame, analysis_frame, config):
    """Simulate execution of a :class:`SignalFrame`.

    Parameters
    ----------
    signal_frame:
        A :class:`~cfdinvest.signals.SignalFrame`.
    analysis_frame:
        The :class:`~cfdinvest.preprocessing.AnalysisFrame` the signals were
        built from; supplies the prices trades are struck against.
    config:
        An :class:`~cfdinvest.execution.frame.ExecutionConfig`.

    Returns
    -------
    dict
        The raw pieces an :class:`~cfdinvest.execution.frame.ExecutionFrame`
        is assembled from.
    """
    close_panel = analysis_frame.prices.get("close", pd.DataFrame())
    if close_panel.empty or signal_frame.direction.empty:
        logger.warning("No prices or no signals; execution has nothing to simulate")
        return _empty_result()

    index = close_panel.index
    columns = close_panel.columns
    bars, assets = len(index), len(columns)

    # -- inputs, aligned and lagged -------------------------------------- #
    lag = config.execution_lag
    direction = _as_labels(
        fills_module.shift_for_execution(signal_frame.direction, lag), index, columns
    )
    size = _as_matrix(
        fills_module.shift_for_execution(signal_frame.size, lag), index, columns, 0.0
    )
    confidence = _as_matrix(
        fills_module.shift_for_execution(signal_frame.confidence, lag),
        index, columns, 0.0,
    )

    # Every price the loop touches is restated in the account currency
    # before anything else sees it, so the state machine, the leverage cap
    # and the equity curve all work in one unit. Rates are 1.0 with FX
    # disabled, which makes the conversion the identity.
    fx_config = config.fx
    fx_rates = _fx_matrix(index, columns, analysis_frame, fx_config)
    # Which instruments genuinely cross a currency boundary. A domestic
    # instrument converts nothing and is charged nothing.
    fx_converts = fx_module.converting_assets(columns, fx_config)
    # Which of those the broker actually charges its conversion fee on --
    # a smaller set, and not derivable from the currency.
    fx_fee_mask = fx_module.fee_charging_assets(columns, fx_config)

    close = fills_module.convert_prices(
        _as_matrix(close_panel, index, columns), fx_rates
    )
    fill_reference = fills_module.convert_prices(
        _as_matrix(
            fills_module.fill_prices(analysis_frame.prices, config.fill_mode),
            index, columns,
        ),
        fx_rates,
    )

    stop_config = config.stops
    bar_open = fills_module.convert_prices(
        _as_matrix(analysis_frame.prices.get("open"), index, columns), fx_rates
    )
    bar_high = fills_module.convert_prices(
        _as_matrix(analysis_frame.prices.get("high"), index, columns), fx_rates
    )
    bar_low = fills_module.convert_prices(
        _as_matrix(analysis_frame.prices.get("low"), index, columns), fx_rates
    )
    # The ATR is a price range, so it converts exactly like a price -- and it
    # must, or an ATR-multiple stop on a dollar instrument would be placed a
    # dollar distance away from a sterling price.
    atr_panel = fills_module.convert_prices(
        _as_matrix(analysis_frame.volatility.get("atr"), index, columns), fx_rates
    )

    stops_active = stop_config.enabled
    if stops_active and not np.isfinite(atr_panel).any():
        # Levels are placed in ATRs. Without an ATR panel there is nothing to
        # place them against, and running on silently would misrepresent the
        # configuration rather than the market.
        logger.warning(
            "Stops are enabled but no ATR panel is available (a close-only "
            "lake has no high/low); running without stops."
        )
        stops_active = False
    if stops_active and not (
        np.isfinite(bar_high).any() and np.isfinite(bar_low).any()
    ):
        logger.warning(
            "Stops are enabled but no high/low panels exist; running without stops."
        )
        stops_active = False

    spread = fills_module.resolve_spreads(
        columns,
        asset_classes=config.asset_classes,
        per_asset_bps=config.spread_bps,
        default_bps=config.default_spread_bps,
    ).to_numpy()
    long_rates, short_rates = financing_module.resolve_rates(
        columns,
        benchmark=config.benchmark_rate,
        markup=config.financing_markup,
        per_asset=config.financing_rates,
    )
    nights = financing_module.nights_between(index)

    # -- running state ---------------------------------------------------- #
    units = np.zeros(assets)
    held = np.zeros(assets, dtype=np.int8)
    entry_price = np.full(assets, np.nan)
    stop_level = np.full(assets, np.nan)
    target_level = np.full(assets, np.nan)
    stop_events: list[dict] = []

    equity = float(config.initial_equity)
    peak_equity = equity
    halted = False

    positions = np.zeros((bars, assets))
    notionals = np.zeros((bars, assets))
    components = {
        name: np.zeros(bars) for name in (*pnl_module.PNL_COMPONENTS, "total")
    }
    realized_series = np.zeros(bars)
    unrealized_series = np.zeros(bars)
    fx_open_series = np.zeros(bars)
    fx_close_series = np.zeros(bars)
    equity_series = np.zeros(bars)
    exposure_series = np.zeros(bars)
    trades: list[dict] = []

    previous_close = np.full(assets, np.nan)
    previous_entry = np.full(assets, np.nan)

    for bar in range(bars):
        price_now = close[bar]
        reference = fill_reference[bar]
        # The equity the risk cap is sized against: what the account is worth
        # before this bar's P&L, because that is all that is known when the
        # trade is placed.
        equity_at_trade = equity
        tradable = np.isfinite(reference) & (reference > 0)

        # 1. Financing on what was carried into this bar.
        carry = financing_module.financing_cost(
            units, previous_close, nights[bar], long_rates, short_rates,
            day_count=config.day_count,
        )
        carry = np.nan_to_num(carry)

        # 2. Levels resting from a previous bar are checked BEFORE any new
        #    decision. A stop reached during this bar took the position out;
        #    what the model wants now cannot undo that, and re-entering at
        #    this bar's open would mean entering at a price that occurred
        #    before the exit.
        breach = None
        stopped = np.zeros(assets, dtype=bool)
        if stops_active and (units != 0).any():
            breach = stops_module.detect_breaches(
                held, bar_open[bar], bar_high[bar], bar_low[bar],
                stop_level, target_level, stop_config,
            )
            stopped = breach["breached"] & (units != 0)

        # 3. Decide the new target.
        desired = state_module.desired_direction(direction[bar])
        new_held = state_module.next_direction(
            held, desired, size[bar], confidence[bar], config.thresholds
        )

        target_units = _target_units(
            new_held, size[bar], reference, equity, config, tradable
        )
        resize = state_module.needs_resize(
            units, target_units, threshold=config.rebalance_threshold
        )
        # A position whose direction is unchanged only moves when it has
        # drifted far enough to be worth the spread.
        keep = (new_held == held) & (new_held != 0) & ~resize
        target_units = np.where(keep, units, target_units)

        # Cap AFTER the keep, not before. Applying it earlier let a position
        # opened when equity was higher survive untouched, quietly breaching
        # the limit the cap exists to enforce.
        target_units = _apply_leverage_cap(target_units, reference, equity, config)

        stop_out = False
        if config.stop_out_level is not None and equity <= config.stop_out_level * peak_equity:
            stop_out = True
            halted = True
            target_units = np.zeros(assets)
            new_held = np.zeros(assets, dtype=np.int8)
        elif halted and not config.resume_after_stop_out:
            target_units = np.zeros(assets)
            new_held = np.zeros(assets, dtype=np.int8)

        # An untradable bar (no price) cannot be acted on; hold what we have.
        target_units = np.where(tradable, target_units, units)
        new_held = np.where(tradable, new_held, held).astype(np.int8)

        # A breached level closes the position and, unless explicitly allowed,
        # keeps it flat for the remainder of the bar.
        effective_fill = reference
        if stopped.any():
            flatten = stopped if not stop_config.reenter_same_bar else (
                stopped & False
            )
            target_units = np.where(flatten, 0.0, target_units)
            new_held = np.where(flatten, 0, new_held).astype(np.int8)
            effective_fill = np.where(stopped, breach["fill_price"], reference)

        traded = target_units - units

        # 4. Costs and realised P&L on the closed portion, struck at the price
        #    each asset actually traded at: the stop fill where a level was
        #    reached, the bar reference everywhere else.
        spread_cost, slippage_cost = fills_module.transaction_costs(
            traded, effective_fill, spread, config.slippage_fraction
        )
        spread_cost = np.nan_to_num(spread_cost)
        slippage_cost = np.nan_to_num(slippage_cost)

        realized = pnl_module.realized_on_close(
            units, target_units, entry_price, effective_fill
        )
        realized = np.nan_to_num(realized)

        # 4b. Currency conversion, on the part opened and the part closed.
        #     A flip does both: the old position is repatriated in full and
        #     the new one funded in full, which is what the broker converts.
        opened_units, closed_units = fx_module.split_open_close(
            units, target_units
        )
        fx_rate_row = fx_rates[bar]
        if fx_config.enabled:
            fx_open = fills_module.fx_open_costs(
                effective_fill, opened_units, fx_rate_row, fx_config,
                converts=fx_converts, charges_fee=fx_fee_mask,
            )
            fx_close = pnl_module.fx_close_costs(
                effective_fill, closed_units, fx_rate_row, fx_config,
                realized_pnl=realized, financing=-carry,
                converts=fx_converts, charges_fee=fx_fee_mask,
            )
            fx_cost = np.nan_to_num(fx_open["total"]) + np.nan_to_num(
                fx_close["total"]
            )
        else:
            fx_open = fx_close = None
            fx_cost = np.zeros(assets)

        # 4. Book the bar.
        # Passing the stop fill as that asset's fill price makes the
        # decomposition exact without touching pnl.py: the "gap" leg becomes
        # units * (stop_fill - previous_close), the whole move up to the exit,
        # and the "intraday" leg is zero because the new position is flat.
        bar_components = pnl_module.bar_pnl(
            units, target_units, previous_close, effective_fill, price_now,
            spread_cost, slippage_cost, carry, fx_cost,
        )
        for name, values in bar_components.items():
            components[name][bar] = float(np.nansum(values))

        if fx_open is not None:
            fx_open_series[bar] = float(np.nansum(fx_open["total"]))
            fx_close_series[bar] = float(np.nansum(fx_close["total"]))

        equity += components["total"][bar]
        peak_equity = max(peak_equity, equity)

        # 5. Record trades before the state rolls forward.
        actions = state_module.classify_actions(held, new_held, resize)
        if stopped.any():
            actions = np.where(
                stopped,
                np.where(breach["is_take_profit"], "take_profit", "stop_loss"),
                actions,
            )
        moved = np.abs(traded) > config.minimum_trade_units
        for position in np.flatnonzero(moved):
            trades.append(
                {
                    "timestamp": index[bar],
                    "asset": columns[position],
                    "action": actions[position]
                    if actions[position] != "hold"
                    else "resize",
                    "units": float(target_units[position]),
                    "traded_units": float(traded[position]),
                    "price": float(price_now[position]),
                    "fill_price": float(
                        fills_module.effective_fill_price(
                            reference[position : position + 1],
                            traded[position : position + 1],
                            spread[position : position + 1],
                            config.slippage_fraction,
                        )[0]
                    ),
                    "spread_cost": float(spread_cost[position]),
                    "slippage": float(slippage_cost[position]),
                    "financing": float(carry[position]),
                    "realized_pnl": float(realized[position]),
                    "notional": float(abs(target_units[position] * reference[position])),
                    "equity": equity,
                    "reason": _trade_reason(
                        stop_out, bool(stopped[position]), breach, position
                    ),
                    **_stop_fields(breach, stopped, position, stop_level, target_level),
                    **_fx_fields(
                        fx_open, fx_close, position, fx_rate_row,
                        effective_fill, traded, fx_config,
                    ),
                }
            )

        if stopped.any():
            for position in np.flatnonzero(stopped):
                stop_events.append(
                    {
                        "timestamp": index[bar],
                        "asset": columns[position],
                        "kind": "take_profit"
                        if breach["is_take_profit"][position]
                        else "stop",
                        "direction": "long" if units[position] > 0 else "short",
                        "breach_type": str(breach["breach_type"][position]),
                        "gap_through": breach["breach_type"][position] == "gap",
                        "level": float(breach["level"][position]),
                        "reference_price": float(breach["reference_price"][position]),
                        "fill_price": float(breach["fill_price"][position]),
                        "recommended_fill": float(
                            breach["recommended_fill"][position]
                        ),
                        "slippage_applied": float(
                            breach["slippage_applied"][position]
                        ),
                        "slippage_bps": float(breach["slippage_bps"][position]),
                        "units_closed": float(units[position]),
                        "ambiguous": bool(breach["ambiguous"][position]),
                    }
                )

        entry_price = _roll_entry_price(
            units, target_units, entry_price, effective_fill
        )
        units = target_units
        held = new_held

        if stops_active:
            # Levels follow the entry: recomputed when a position is opened,
            # flipped or added to, cleared when it goes flat, and left alone
            # when it is merely reduced.
            fresh_stop, fresh_target = stops_module.stop_levels(
                entry_price, atr_panel[bar], held, stop_config
            )
            moved_entry = ~(
                (np.isnan(entry_price) & np.isnan(previous_entry))
                | np.isclose(
                    np.nan_to_num(entry_price), np.nan_to_num(previous_entry)
                )
            )
            # Also place a level on an open position that still has none.
            # ATR needs a warm-up, so a position opened in the first fortnight
            # has no range to size against at entry; without this it would
            # keep its NaN level for the rest of its life and never be
            # stopped out at all.
            unset = (held != 0) & ~np.isfinite(stop_level)
            place = moved_entry | unset

            stop_level = np.where(place, fresh_stop, stop_level)
            target_level = np.where(place, fresh_target, target_level)
            stop_level = np.where(held == 0, np.nan, stop_level)
            target_level = np.where(held == 0, np.nan, target_level)
        previous_entry = entry_price.copy()

        previous_close = np.where(np.isfinite(price_now), price_now, previous_close)

        positions[bar] = units
        notionals[bar] = units * np.nan_to_num(price_now)
        fill_notional = float(np.abs(units * np.nan_to_num(reference)).sum())
        realized_series[bar] = float(np.nansum(realized))
        unrealized_series[bar] = float(
            np.nansum(units * (np.nan_to_num(price_now) - np.nan_to_num(entry_price)))
        )
        equity_series[bar] = equity
        # Gross leverage as the cap enforced it: fill-price notional over the
        # equity known at trade time. Marking at the close, or dividing by
        # post-bar equity, would show the limit breached on any bar that lost
        # money -- true of the resulting position, but not something the cap
        # could have prevented. `notionals` supports that view separately.
        exposure_series[bar] = fill_notional / max(equity_at_trade, 1e-9)

    return _assemble(
        index, columns, positions, notionals, components, realized_series,
        unrealized_series, equity_series, exposure_series, trades, config,
        stop_events, stop_level, target_level, entry_price,
        fx_open_series, fx_close_series, fx_rates[-1] if bars else None,
    )


def _target_units(held, size_row, reference, equity, config, tradable):
    """Convert a signal size into a unit target, before any risk cap."""
    exposure = size_row * config.exposure_per_unit_size * equity
    notional = np.where(held != 0, exposure * np.sign(held), 0.0)
    notional = np.where(tradable, notional, 0.0)

    safe_price = np.where(tradable, reference, np.nan)
    with np.errstate(invalid="ignore", divide="ignore"):
        units = notional / safe_price
    return np.nan_to_num(units)


def _apply_leverage_cap(units, reference, equity, config):
    """Scale the whole book down until gross notional fits the cap.

    Proportional, so the relative sizing the signal layer produced survives:
    the cap decides how much risk in total, not which asset gets it.
    """
    price = np.nan_to_num(reference)
    gross = float(np.abs(units * price).sum())
    cap = config.max_leverage * max(equity, 0.0)

    if cap <= 0:
        return np.zeros_like(units)
    if gross > cap:
        return units * (cap / gross)
    return units


def _roll_entry_price(previous_units, new_units, entry_price, fill_price):
    """Update the weighted-average entry after a trade.

    Adding to a position blends the new fill into the average; reducing one
    leaves it untouched (the remaining units were bought at the old price);
    flipping or opening resets it to the fill.
    """
    result = entry_price.copy()

    opened = (previous_units == 0) & (new_units != 0)
    flipped = (np.sign(previous_units) != np.sign(new_units)) & (new_units != 0) & (
        previous_units != 0
    )
    result[opened | flipped] = fill_price[opened | flipped]

    added = (
        (np.sign(new_units) == np.sign(previous_units))
        & (np.abs(new_units) > np.abs(previous_units))
        & (previous_units != 0)
    )
    if added.any():
        old, new = previous_units[added], new_units[added]
        blended = (
            old * np.nan_to_num(entry_price[added]) + (new - old) * fill_price[added]
        ) / new
        result[added] = blended

    result[new_units == 0] = np.nan
    return result


def _trade_reason(stop_out, was_stopped, breach, position) -> str:
    """Why a trade happened, in one word."""
    if stop_out:
        return "stop_out"
    if was_stopped and breach is not None:
        return (
            "take_profit" if breach["is_take_profit"][position] else "stop_loss"
        )
    return "signal"


def _fx_fields(
    fx_open, fx_close, position, fx_rate_row, effective_fill, traded, fx_config
) -> dict:
    """The FX columns for one trade-log row.

    The costs of the open leg and the close leg are summed: a single row can
    be both, and splitting one trade across two rows would break the
    one-row-per-executed-change contract the log already keeps.
    """
    rate = float(fx_rate_row[position])
    if fx_open is None or not fx_config.record_fx_details:
        return {
            "fx_rate": rate,
            "fx_spread": 0.0,
            "fx_conversion_fee": 0.0,
            "fx_slippage": 0.0,
            "fx_adjusted_fill_price": float("nan"),
        }

    buying = traded[position] > 0
    recommended = fx_module.recommended_fx_fill(
        fx_module.convert_from_account_currency(
            effective_fill[position : position + 1], fx_rate_row[position : position + 1]
        ),
        fx_rate_row[position : position + 1],
        fx_config,
        side=1.0 if buying else -1.0,
    )

    return {
        "fx_rate": rate,
        "fx_spread": float(
            fx_open["fx_spread"][position] + fx_close["fx_spread"][position]
        ),
        "fx_conversion_fee": float(
            fx_open["fx_conversion_fee"][position]
            + fx_close["fx_conversion_fee"][position]
        ),
        "fx_slippage": float(
            fx_open["fx_slippage"][position] + fx_close["fx_slippage"][position]
        ),
        "fx_adjusted_fill_price": float(recommended["recommended_fill"][0]),
    }


def _stop_fields(breach, stopped, position, stop_level, target_level) -> dict:
    """The stop/take-profit columns for one trade-log row."""
    was_stopped = bool(stopped[position]) and breach is not None
    is_target = was_stopped and bool(breach["is_take_profit"][position])
    fill = float(breach["fill_price"][position]) if was_stopped else float("nan")

    return {
        "stop_level": float(stop_level[position]),
        "take_profit_level": float(target_level[position]),
        "stop_fill_price": fill if was_stopped and not is_target else float("nan"),
        "take_profit_fill_price": fill if is_target else float("nan"),
        "gap_through": bool(
            was_stopped and breach["breach_type"][position] == "gap"
        ),
        "slippage_applied": float(breach["slippage_applied"][position])
        if was_stopped
        else 0.0,
        "breach_type": str(breach["breach_type"][position]) if was_stopped else "",
    }


def _assemble(
    index, columns, positions, notionals, components, realized, unrealized,
    equity, exposure, trades, config,
    stop_events=None, stop_level=None, target_level=None, entry_price=None,
    fx_open=None, fx_close=None, fx_rate_used=None,
):
    """Package the loop's arrays into frames."""
    position_frame = pd.DataFrame(positions, index=index, columns=columns)
    position_frame.index.name = TIMESTAMP
    position_frame.columns.name = TICKER

    notional_frame = pd.DataFrame(notionals, index=index, columns=columns)
    notional_frame.index.name = TIMESTAMP
    notional_frame.columns.name = TICKER

    pnl_frame = pd.DataFrame(components, index=index)
    pnl_frame["realized"] = realized
    pnl_frame["unrealized"] = unrealized
    pnl_frame.index.name = TIMESTAMP

    equity_series = pd.Series(equity, index=index, name="equity")
    equity_series.index.name = TIMESTAMP

    trade_log = (
        pd.DataFrame(trades, columns=list(TRADE_COLUMNS))
        if trades
        else pd.DataFrame(columns=list(TRADE_COLUMNS))
    )

    event_log = (
        pd.DataFrame(stop_events, columns=list(stops_module.STOP_EVENT_COLUMNS))
        if stop_events
        else pd.DataFrame(columns=list(stops_module.STOP_EVENT_COLUMNS))
    )
    blank = pd.Series(np.nan, index=columns, dtype="float64")

    return {
        "positions": position_frame,
        "notionals": notional_frame,
        "pnl": pnl_frame,
        "equity_curve": equity_series,
        "trades": trade_log,
        "exposure": pd.Series(exposure, index=index, name="gross_leverage"),
        "stop_events": event_log,
        "stop_levels": pd.Series(
            blank if stop_level is None else stop_level,
            index=columns, name="stop_level",
        ),
        "take_profit_levels": pd.Series(
            blank if target_level is None else target_level,
            index=columns, name="take_profit_level",
        ),
        "entry_prices": pd.Series(
            blank if entry_price is None else entry_price,
            index=columns, name="entry_price",
        ),
        "fx_open_cost": pd.Series(
            np.zeros(len(index)) if fx_open is None else fx_open,
            index=index, name="fx_open_cost",
        ),
        "fx_close_cost": pd.Series(
            np.zeros(len(index)) if fx_close is None else fx_close,
            index=index, name="fx_close_cost",
        ),
        "fx_rate_used": pd.Series(
            np.ones(len(columns)) if fx_rate_used is None else fx_rate_used,
            index=columns, name="fx_rate_used",
        ),
    }


def _empty_result():
    """A correctly shaped result for an empty run."""
    return {
        "positions": pd.DataFrame(),
        "notionals": pd.DataFrame(),
        "pnl": pd.DataFrame(),
        "equity_curve": pd.Series(dtype="float64", name="equity"),
        "trades": pd.DataFrame(columns=list(TRADE_COLUMNS)),
        "exposure": pd.Series(dtype="float64", name="gross_leverage"),
        "stop_events": pd.DataFrame(
            columns=list(stops_module.STOP_EVENT_COLUMNS)
        ),
        "stop_levels": pd.Series(dtype="float64", name="stop_level"),
        "take_profit_levels": pd.Series(
            dtype="float64", name="take_profit_level"
        ),
        "entry_prices": pd.Series(dtype="float64", name="entry_price"),
        "fx_open_cost": pd.Series(dtype="float64", name="fx_open_cost"),
        "fx_close_cost": pd.Series(dtype="float64", name="fx_close_cost"),
        "fx_rate_used": pd.Series(dtype="float64", name="fx_rate_used"),
    }
