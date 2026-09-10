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
        legs = fx_module.rate_legs(pair)
        missing = [
            leg for leg, _ in legs
            if close_panel.empty or leg not in close_panel.columns
        ]
        if not legs or missing:
            logger.warning(
                "fx_rate_source='lake' but no %s series for %s in the lake "
                "(fx_pair_tickers: %r, missing: %r); using the configured "
                "rate %.6g.",
                currency, ticker, pair, missing, matrix[0, position],
            )
            continue

        # A rate is the product of its legs, each raised to +1 or -1. Only
        # EURGBP is quoted the way this account needs it; USD needs the
        # reciprocal of GBPUSD, and JPY, CAD and CHF need a cross through
        # it. Building them here rather than ingesting derived series keeps
        # the lake holding only what a source actually published.
        combined = None
        for leg, exponent in legs:
            values = (
                close_panel[leg].reindex(index).ffill()
                .to_numpy(dtype="float64", na_value=np.nan)
            )
            with np.errstate(divide="ignore", invalid="ignore"):
                term = values if exponent > 0 else np.where(
                    values > 0, 1.0 / values, np.nan
                )
            combined = term if combined is None else combined * term
        usable = combined
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

    # Shifted like everything else: the mask is derived from the same bar
    # the signal is, so it must reach the loop on the same delay or a
    # position would be gated on information the signal did not have.
    # Missing entries default to True -- permitted -- because "no reading"
    # is not "refused".
    entry_gate = None
    if getattr(config, "entry_gate", None) is not None:
        entry_gate = _as_matrix(
            fills_module.shift_for_execution(config.entry_gate, lag),
            index, columns, 1.0,
        ).astype(bool)

    # The opening window, combined with whatever gate is already there.
    #
    # Deliberately NOT shifted, unlike the gate above. That mask is derived
    # from the signal bar and must travel with it; this one is a fact about
    # the bar being dealt on, and the live path asks the same question at the
    # moment it places the order rather than at the moment it formed a view.
    delay = int(getattr(config, "open_delay_minutes", 0) or 0)
    if delay > 0 and len(index):
        stamps = pd.DatetimeIndex(index)
        opens = pd.Series(stamps, index=stamps).groupby(stamps.normalize()).transform("min")
        elapsed = (stamps - pd.DatetimeIndex(opens)).total_seconds() / 60.0
        allowed = np.asarray(elapsed >= float(delay), dtype=bool)
        window = np.repeat(allowed[:, None], len(columns), axis=1)
        entry_gate = window if entry_gate is None else (entry_gate & window)

    # And the mirror at the other end of the session: no opening into the
    # close. Same construction as the opening window, same reason for being
    # unshifted.
    shut = int(getattr(config, "close_delay_minutes", 0) or 0)
    if shut > 0 and len(index):
        _s = pd.DatetimeIndex(index)
        _ends = pd.Series(_s, index=_s).groupby(_s.normalize()).transform("max")
        remaining = (pd.DatetimeIndex(_ends) - _s).total_seconds() / 60.0
        allowed_late = np.asarray(remaining >= float(shut), dtype=bool)
        # The frame's final bar is not near a close; it is simply the last
        # bar there is. Same reasoning as `session_end` below, and the same
        # failure without it: on a live run the newest bar is also its own
        # day's maximum, so `remaining` is zero and the closing window
        # refuses every entry on the bar being decided on. On 2026-09-10
        # that emptied the intent completely -- the day had one bar so far,
        # and it read as both the session's first and its last.
        allowed_late[-1] = True
        late = np.repeat(allowed_late[:, None], len(columns), axis=1)
        entry_gate = late if entry_gate is None else (entry_gate & late)

    # The last look, modelled. Also unshifted, and for the same reason: the
    # question is whether the decision still stands on the bar being dealt
    # on, which is what `advisor.livecheck` asks the broker's own quote
    # before it places the order.
    #
    # Compared against the LAGGED direction rather than recomputed, because
    # the lagged array is the decision the engine is acting on. Agreement
    # means the signal survived the bar; disagreement means it faded to flat
    # or turned over, which live reports as two different refusals and both
    # of which refuse.
    # The last bar of each session, for the overnight profit take. Computed
    # once here rather than per bar, and from the frame's own index: the
    # panel is session-restricted, so the last bar of a calendar day IS the
    # session's close.
    #
    # **Except the final bar of the frame, which is never a session end.**
    # A bar is only the session's last once the session has finished, and
    # for the newest bar in the data it has not -- it is simply the last
    # thing that has happened yet. In a backtest the distinction never
    # mattered, because the run ended on a day that was over.
    #
    # It matters live, and it cost a session on 2026-09-09. The scheduled
    # run's newest bar is the one in progress, so treating it as a close
    # flattened the very bar the engine was deciding on: `persist.intent`
    # reads `positions.iloc[-1]`, found it zeroed, and the daemon's intent
    # became "hold nothing" for the whole day. Measured on the same data at
    # the same moment -- flatten treating the last bar as a close, 0
    # positions; flatten excluding it, 6.
    #
    # Excluding one bar changes nothing in a backtest beyond the final day's
    # close, which is the honest position anyway: a run stopping mid-session
    # should not pretend the book was flattened.
    session_end = np.zeros(len(index), dtype=bool)
    if len(index):
        _stamps = pd.DatetimeIndex(index)
        _closes = pd.Series(_stamps, index=_stamps).groupby(
            _stamps.normalize()).transform("max")
        session_end = np.asarray(
            _stamps == pd.DatetimeIndex(_closes), dtype=bool
        )
        session_end[-1] = False

    if bool(getattr(config, "entry_recheck", False)):
        fresh_direction = _as_labels(signal_frame.direction, index, columns)
        fresh_confidence = _as_matrix(
            signal_frame.confidence, index, columns, 0.0
        )
        bar = float(getattr(config.thresholds, "entry_confidence", 0.0) or 0.0)
        still = (fresh_direction == direction) & (fresh_confidence >= bar)
        entry_gate = still if entry_gate is None else (entry_gate & still)

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

    # Financing accrues on every bar a position is carried, not only on the
    # bars it is traded. The trade log records the carry booked alongside a
    # fill and nothing else, so attributing cost from the log alone loses
    # every night a position was simply held -- which for a strategy holding
    # a median of five days is most of them. Accumulated per asset here so
    # the per-instrument attribution can sum to the run's actual P&L.
    financing_by_asset = np.zeros(assets, dtype="float64")
    # When each instrument was last closed out, for the re-entry cooldown.
    # NaT until something has been closed, so nothing is suppressed before
    # there is anything to be suppressed by.
    # Held as integer nanoseconds rather than datetime64: the index is
    # tz-aware and numpy has no representation for that, so converting each
    # bar would warn on every iteration and silently drop the zone. NaN
    # means nothing has closed yet.
    closed_at = np.full(assets, np.nan, dtype="float64")
    # Normalised to nanoseconds explicitly. The lake's calendar arrives as
    # datetime64[us] here and datetime64[ns] elsewhere, and reading the raw
    # integers without pinning the unit makes a day measure 0.001 days --
    # which does not fail, it just puts every instrument permanently inside
    # its cooldown and quietly cuts the book from 3,383 trades to 171.
    bar_stamps = pd.DatetimeIndex(index).as_unit("ns").asi8.astype("float64")
    # Float, not int. Both limits are compared against ages computed in
    # fractional days (`/ NANOS_PER_DAY`), so the arithmetic has always
    # supported part-days -- only the cast did not. On hourly bars that
    # silently destroyed the setting: a configured stop of 0.29 days became
    # int(0.29) = 0, as did 0.13 and 0.50, which is why three different
    # configurations returned byte-identical results and looked like a
    # finding about robustness. It was one number, truncated three ways.
    cooldown = max(float(getattr(config, "reentry_cooldown_days", 0) or 0), 0.0)
    blackout = _earnings_blackout(index, columns, config)
    time_stop = getattr(config, "time_stop_days", None)
    time_stop = None if time_stop is None else max(float(time_stop), 0.0)
    time_stop_threshold = float(getattr(config, "time_stop_threshold", 0.0) or 0.0)
    # When each open position was entered, for the time stop.
    opened_at = np.full(assets, np.nan, dtype="float64")
    NANOS_PER_DAY = 86_400_000_000_000.0

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

    spread_constant = fills_module.resolve_spreads(
        columns,
        asset_classes=config.asset_classes,
        per_asset_bps=config.spread_bps,
        default_bps=config.default_spread_bps,
    ).to_numpy()
    # One value per asset becomes one per bar per asset. Where a measured
    # spread exists it is used; elsewhere the constant stands, and the split
    # is counted so a run can say how much of its largest cost was read off
    # a quote rather than assumed.
    # An explicit panel wins: a caller that supplied one meant it. Otherwise,
    # if measured spreads are switched on, the analysis frame already carries
    # ``spread_bp`` beside the prices on broker data -- the figure was present
    # all along and simply not read. Off by default, so a recorded run keeps
    # charging what it charged.
    spread_source = config.spread_panel
    if spread_source is None and getattr(config, "use_measured_spreads", False):
        spread_source = analysis_frame.prices.get("spread_bp")
        if spread_source is None:
            logger.warning(
                "Measured spreads are enabled but this frame carries no "
                "spread_bp panel; every bar falls back to the %.1fbp constant",
                config.default_spread_bps,
            )
    spread_matrix, spread_measured, spread_cells = fills_module.resolve_spread_matrix(
        index, columns, spread_constant, spread_source
    )

    # Bars the operator may act on. Absent means every bar, which is what
    # every run before this assumed and must keep producing.
    dealing_mask = None
    if getattr(config, "dealing_mask", None) is not None:
        dealing_mask = (
            pd.Series(config.dealing_mask)
            .reindex(index)
            .fillna(False)
            .to_numpy(dtype=bool)
        )
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
        financing_by_asset += carry

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

        if cooldown:
            # Suppress a *new* position taken too soon after the last one in
            # the same instrument closed. Positions already open are left
            # alone: this is a rule about re-entering, not about holding.
            since = (bar_stamps[bar] - closed_at) / NANOS_PER_DAY
            blocked = (
                (held == 0) & (new_held != 0)
                & np.isfinite(since) & (since < cooldown)
            )
            new_held = np.where(blocked, 0, new_held)

        if blackout is not None:
            # No position may be open into a print. The rule closes as well
            # as blocks: a position taken a fortnight ago is in exactly the
            # same danger as one opened today, and only one of those is
            # prevented by refusing new entries.
            new_held = np.where(blackout[bar], 0, new_held)

        # Nothing timed out until the rule below says so. Bound here rather
        # than inside the branch: with the time stop disabled the trade log
        # still has to be written, and a name that only exists when a feature
        # is switched on is a crash waiting for the config that switches it
        # off.
        timed_out = np.zeros_like(held, dtype=bool)

        if time_stop is not None:
            # A position that has had long enough to work and has not is
            # closed, however small the loss. Financing is deducted first,
            # because a position flat on price has still cost something to
            # hold and "not working" includes not covering its own carry.
            age = (bar_stamps[bar] - opened_at) / NANOS_PER_DAY
            # Judged at the price the exit actually transacts at.
            #
            # This read `price_now` -- THIS bar's close -- until 2026-09-04,
            # while the fill happens at this bar's open. It therefore decided
            # using the bar's own outcome and acted at the bar's start.
            #
            # Measured directly from the trade log rather than inferred: on
            # bars the time stop exited, the signed open-to-close move
            # averaged -0.9199% against +0.1010% on bars a position was
            # merely held, a difference of 1.02% per exit across 2,278 exits,
            # with a median of -0.79% so it was the typical exit rather than
            # a tail. No rule of the form "held long enough and has not made
            # money" selects bars about to fall a percent unless it is using
            # the fall.
            #
            # Not a switch. The old behaviour is a bug, every figure measured
            # with it has been retracted, and an option preserving it is a
            # way for it to return. The synthetic cases in tests/ assert the
            # correct behaviour directly.
            #
            # `reference` is the fill price -- this bar's open under the
            # default fill mode. Known at the moment of dealing, and the
            # price the exit gets. The overnight gap into this bar is
            # included, because the dealer genuinely knows it.
            judged = reference
            with np.errstate(invalid="ignore", divide="ignore"):
                move = np.where(
                    (entry_price > 0) & np.isfinite(entry_price),
                    (judged / entry_price - 1.0) * np.sign(held) * 100.0,
                    np.nan,
                )
            rate = np.where(held > 0, long_rates, short_rates)
            carry_pct = np.nan_to_num(rate) / config.day_count * np.nan_to_num(age) * 100.0
            net_pct = move - carry_pct
            stale = (
                (held != 0) & np.isfinite(age) & (age >= time_stop)
                & np.isfinite(net_pct) & (net_pct <= time_stop_threshold)
            )
            new_held = np.where(stale, 0, new_held)
            timed_out = stale

        # Bank a gain on the last bar of a session rather than carrying it
        # overnight.
        #
        # Deliberately NOT folded into the time stop above, though it reuses
        # the same quantity. That branch only runs when a time stop is
        # configured, and this rule has to work whether or not one is -- the
        # first version of this sat inside it and swallowed `timed_out`,
        # which is the sort of thing an indentation level costs you.
        take = getattr(config, "overnight_profit_take", None)
        stall = int(getattr(config, "overnight_profit_stall_bars", 0) or 0)
        if take is not None and session_end[bar] and bar >= 1 + stall:
            # Decided on the PREVIOUS bar's close, not this one's.
            #
            # The fill happens at this bar's open, so using this bar's close
            # to decide it is look-ahead within the bar -- and a rule gated
            # on a price threshold is precisely the kind that benefits from
            # knowing how the bar turned out. The first version did that and
            # measured +165pp; on the previous close it is measured again
            # below, which is the only figure worth quoting.
            #
            # This is what the live path would genuinely have: at the last
            # bar of a session the daemon knows the last COMPLETED bar and
            # deals into the one in progress.
            # The same principle as the time stop above: judge at the
            # price this will transact at. The first version used
            # close[bar-1], which is safe from look-ahead but discards the
            # gap into the bar being dealt on -- information the dealer has.
            decided = reference
            with np.errstate(invalid="ignore", divide="ignore"):
                gain = np.where(
                    (entry_price > 0) & np.isfinite(entry_price),
                    (decided / entry_price - 1.0) * np.sign(held) * 100.0,
                    np.nan,
                )
            banking = (held != 0) & np.isfinite(gain) & (gain >= float(take))

            # Optionally, only once it has stopped paying: no gain over the
            # last N bars in the direction held, again on completed bars.
            if stall > 0:
                with np.errstate(invalid="ignore", divide="ignore"):
                    recent = (
                        (close[bar - 1] / close[bar - 1 - stall] - 1.0)
                        * np.sign(held)
                    )
                banking &= np.isfinite(recent) & (recent <= 0.0)
            new_held = np.where(banking, 0, new_held)

        # Stamped here, after every rule that can close a position has had
        # its say -- the cooldown suppression, the blackout and the time
        # stop. It used to happen before the time stop, so a position the
        # time stop closed never recorded a close time and the cooldown
        # never applied to it. In a configuration where the time stop is the
        # usual exit that made the cooldown inert while the config file
        # still named one: 15 re-entries in 60 bars whether the cooldown was
        # zero or a full day.
        #
        # After the suppression too, so an entry the cooldown just blocked
        # does not restart its own clock.
        if time_stop is not None:
            opened_at = np.where(
                (held == 0) & (new_held != 0), bar_stamps[bar], opened_at
            )
            opened_at = np.where(new_held == 0, np.nan, opened_at)
        if cooldown:
            closed_at = np.where(
                (held != 0) & (new_held == 0), bar_stamps[bar], closed_at,
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

        # Slots before sizing. How many positions may be held is a different
        # question from how large each may be, and answering it after the
        # caps would size a book that is about to lose half its members.
        # Before the slot limit: a refused entry frees no slot for anybody
        # else, so refusing first keeps the two independent. Both gate
        # opening and neither closes.
        if entry_gate is not None:
            target_units, new_held = _apply_entry_gate(
                target_units, new_held, units, entry_gate[bar]
            )

        # After the gates, before the sizing caps: this trims what is held
        # rather than deciding what may be held, so the caps should see the
        # trimmed figure.
        target_units = _trim_decayed(
            target_units, units, confidence[bar], config
        )

        # After the trim and before the caps: conviction decides how large a
        # position should be, and the caps then decide how large it may be.
        target_units = _size_by_confidence(target_units, confidence[bar], config)

        target_units, new_held = _apply_position_limit(
            target_units, new_held, units, config
        )

        # Cap AFTER the keep, not before. Applying it earlier let a position
        # opened when equity was higher survive untouched, quietly breaching
        # the limit the cap exists to enforce.
        # Per instrument first, then the book. Trimming one name frees
        # room under the gross ceiling for the others; the other order would
        # shrink the whole book to make space for a position about to be cut
        # anyway.
        target_units = _apply_concentration_cap(
            target_units, reference, equity, config
        )

        # The per-pass opening budget, BEFORE the book-wide cap.
        #
        # This ran after the leverage cap until 2026-09-07, on a comment
        # saying "that is where the dealing path applies it". The dealing
        # path applies it the other way round -- bridge.plan_orders defers
        # at line 434 and caps at line 462 -- and its own comment says why:
        # the survivors are then sized against the room the refusals freed,
        # rather than every position being shrunk to fit a book that is
        # about to lose members anyway. Both comments claimed to match the
        # other and they disagreed; the dealing path is what the account
        # runs, so the engine moves.
        #
        # Same argument as the concentration cap directly above, which was
        # already ordered on it: refusing first frees room for the rest.
        target_units = _defer_surplus_opens(
            target_units, units, confidence[bar], config
        )

        target_units = _apply_leverage_cap(
            target_units, reference, equity, config,
            held=units, confidence=confidence[bar],
        )

        # And the margin the book would commit. Applied after the leverage
        # cap because they answer different questions: leverage bounds the
        # exposure we choose to take, margin bounds what the counterparty
        # will fund, and the second can tighten without the first moving.
        target_units = _apply_margin_cap(
            target_units, reference, equity, columns, config
        )

        # Last, because it quantises the answer every cap above produced --
        # the same place bridge._round_down sits, after the ceiling rather
        # than before it. A size rounded before a cap would be rounded again
        # by the cap and stop being a multiple of anything.
        target_units = _quantise_to_deal_size(
            target_units, units, columns, config
        )

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

        if dealing_mask is not None and not dealing_mask[bar]:
            # Nobody is dealing on this bar. Hold everything exactly as it
            # is -- including through a stop level, because the stops here
            # are the app's own and it is not running to act on them. The
            # position is still marked to market below; it is only acting
            # that is blocked.
            target_units = units.copy()
            new_held = held.copy()
            effective_fill = reference

        traded = target_units - units

        # 4. Costs and realised P&L on the closed portion, struck at the price
        #    each asset actually traded at: the stop fill where a level was
        #    reached, the bar reference everywhere else.
        spread = spread_matrix[bar]
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
                        stop_out, bool(stopped[position]), breach, position,
                        bool(timed_out[position]),
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
        financing_by_asset=financing_by_asset,
        spread_coverage=(spread_measured, spread_cells),
    )


def _earnings_blackout(index, columns, config) -> np.ndarray | None:
    """A ``bars x assets`` mask of sessions too close to a reporting date.

    Pure. ``None`` when the rule is off or nothing is recorded, so the
    engine skips the check entirely rather than carrying an all-false
    array through every bar.

    The window runs from ``earnings_blackout_days`` before the date up to
    and including the session after it, because the reaction lands on the
    session *following* the announcement -- most companies report after the
    close, and a rule that reopened on the morning of the reaction would
    take the gap it exists to avoid.
    """
    days = int(getattr(config, "earnings_blackout_days", 0) or 0)
    recorded = getattr(config, "earnings_dates", None) or {}
    if days <= 0 or not recorded:
        return None

    stamps = pd.DatetimeIndex(index)
    mask = np.zeros((len(stamps), len(columns)), dtype=bool)
    for position, ticker in enumerate(columns):
        dates = recorded.get(str(ticker))
        if not dates:
            continue
        for value in dates:
            try:
                when = pd.Timestamp(value)
            except (TypeError, ValueError):
                continue
            if when.tzinfo is None:
                when = when.tz_localize("UTC")
            else:
                when = when.tz_convert("UTC")
            start = when - pd.Timedelta(days=days)
            end = when + pd.Timedelta(days=1)
            mask[:, position] |= (stamps >= start) & (stamps <= end)
    return mask


def _target_units(held, size_row, reference, equity, config, tradable):
    """Convert a signal size into a unit target, before any risk cap."""
    exposure = size_row * config.exposure_per_unit_size * equity
    notional = np.where(held != 0, exposure * np.sign(held), 0.0)
    notional = np.where(tradable, notional, 0.0)

    safe_price = np.where(tradable, reference, np.nan)
    with np.errstate(invalid="ignore", divide="ignore"):
        units = notional / safe_price
    return np.nan_to_num(units)


def _size_by_confidence(target_units, confidence, config):
    """Scale a position by how much conviction stands behind it.

    Until 2026-08-30 confidence was used only as a *gate*: clear 0.35 or do
    not trade. Everything above the bar was sized identically, which throws
    away the ordering the score carries.

    **RETRACTED 2026-09-03**: "confidence is the best predictor of outcome
    this project has -- +0.248 rank correlation against realised trip P&L,
    where a purpose-built model on sixteen features managed +0.129." Both
    numbers were read on the fill bar rather than the decision bar the engine
    gates on, which is the error retracted on 2026-08-30. On the decision bar
    confidence reads +0.023; a walk-forward sixteen-feature model reads
    +0.0777. The model is roughly four times better, not half as good.

    Sizing by confidence still earns its place -- it was adopted on engine
    arms with a leverage-matched control, not on that correlation -- but the
    reason it is confidence rather than a fitted model is now an empirical
    finding rather than an assumption, and the finding cuts both ways:

      * No band of the model's estimate is unprofitable out of sample. The
        bottom fifth still earns +1.6M at a 42.7% win rate, so there is
        nothing for a filter to remove, which is the same wall every
        selection idea in this project has hit.
      * The model's skill is at WIN RATE, and in this book win rate runs
        *against* mean P&L above the bottom band: its bands earn +2,359,
        +10,404, +10,005, +9,750, +9,339 while win rate climbs 42.7% to
        53.4%. Twenty per cent of trips produce all the profit here, so a
        model trained to be right more often trades the fat tail away.

    That is the argument for sizing on a score the strategy already produces
    rather than on a classifier fitted to outcomes.

    The multiplier is linear in confidence above the entry bar, centred so
    that a position exactly at the bar is unchanged:

        multiplier = 1 + strength x (confidence - bar) / (1 - bar)

    ``strength`` of 0 is the current behaviour. 1.0 doubles a
    maximum-confidence position relative to one at the bar. Bounded by
    ``confidence_sizing_floor`` so a weak signal is trimmed rather than
    erased, since the gate has already decided it is worth taking.

    **Read the result against a leverage control.** This raises mean
    exposure, and this project once credited exactly such a gain to
    selection when the mechanism was a mixture nobody had separated -- a
    +10.7pp result that turned out to be part signal, part leverage, and was
    itself wrongly retracted before the control ran. Sizing differentially
    and levering uniformly are different claims and only one of them is
    interesting.

    Pure. Applies at entry and to held positions alike: the conviction
    behind a position is a fact about now, not about when it was opened.
    """
    strength = getattr(config, "confidence_sizing_strength", None)
    if not strength:
        return target_units
    bar = float(getattr(config.thresholds, "entry_confidence", 0.0) or 0.0)
    span = max(1.0 - bar, 1e-9)
    edge = (np.asarray(confidence, dtype=float) - bar) / span
    # An absent reading scales nothing rather than scaling to zero: "no
    # confidence recorded" is not "no conviction".
    edge = np.nan_to_num(edge, nan=0.0)
    floor = float(getattr(config, "confidence_sizing_floor", 0.25) or 0.0)
    multiplier = np.clip(1.0 + float(strength) * edge, floor, None)
    return target_units * multiplier


def _trim_decayed(target_units, units, confidence, config):
    """Scale back a position whose conviction has fallen below the entry bar.

    Only positions already held, and only those that would no longer clear
    ``entry_confidence``. A new position cannot be below it -- the
    hysteresis band refuses that -- so this touches nothing at entry.

    It closes nothing. Trimming to half leaves a position that continues to
    run and exits on its own terms; the exit band at ``exit_confidence``
    still decides when it goes. Measured, this is worth 3 points of return
    and 5 of drawdown, and cutting the *top* by the same amount instead is
    the worst arm of the whole sweep -- so it is the direction that pays,
    not the reduction.

    Pure.
    """
    multiplier = getattr(config, "decayed_position_multiplier", None)
    if multiplier is None:
        return target_units
    override = getattr(config, "decayed_threshold", None)
    bar = float(
        override if override is not None
        else (getattr(config.thresholds, "entry_confidence", 0.0) or 0.0)
    )
    if bar <= 0:
        return target_units
    decayed = (units != 0) & (target_units != 0) & (
        np.asarray(confidence, dtype=float) < bar
    )
    if not decayed.any():
        return target_units
    return np.where(decayed, target_units * float(multiplier), target_units)


def _apply_entry_gate(target_units, new_held, units, allowed):
    """Refuse to *open* where the gate is shut. Close nothing.

    An incumbent is never touched, whatever the gate says. The requirement
    is a condition on entering, and a position already held has entered --
    applying it afterwards would turn a filter into a liquidation trigger,
    which is the same distinction ``_apply_position_limit`` exists to keep
    and the one that has been got wrong twice in this file's history.

    A refused newcomer is flat rather than short: zero the units *and* the
    direction, or the position frame records a holding the book never took.

    Pure.
    """
    if allowed is None:
        return target_units, new_held
    opening = (target_units != 0) & (units == 0)
    refused = opening & ~np.asarray(allowed, dtype=bool)
    if not refused.any():
        return target_units, new_held
    return (
        np.where(refused, 0.0, target_units),
        np.where(refused, 0, new_held).astype(new_held.dtype),
    )


def _apply_position_limit(target_units, new_held, units, config):
    """Refuse new positions once every slot is taken.

    Incumbents keep their slots. A position already open is never displaced
    by a newcomer, however well the newcomer ranks: swapping them pays a
    round trip to act on a difference the ranking cannot resolve, and the
    incumbent is the one already earning. Only genuinely free slots -- from
    a position closing this bar -- are filled.

    Priority among the newcomers is the size the signal layer asked for,
    which already carries the conviction, the regime tilt and every filter
    that has shrunk it. Ranking on anything else here would be a second
    opinion about a question already answered upstream.

    Returns the units and directions with refused entries zeroed. Pure.
    """
    limit = getattr(config, "max_open_positions", None)
    if limit is None:
        return target_units, new_held
    limit = int(limit)
    if limit <= 0:
        # No new positions at all. Incumbents still keep theirs -- a limit
        # of zero means "open nothing", not "close everything", and the
        # difference is a book being liquidated by a configuration value.
        hold = (target_units != 0) & (units != 0)
        return (
            np.where(hold, target_units, 0.0),
            np.where(hold, new_held, 0).astype(new_held.dtype),
        )

    wanted = target_units != 0
    if int(wanted.sum()) <= limit:
        return target_units, new_held

    # **Every incumbent keeps its position, without exception.** The limit
    # is a bound on *opening*, consulted when a new position is proposed,
    # and never a reason to close one that is already open.
    #
    # It did not behave that way. Slots were handed to the largest
    # incumbents up to the limit and the surplus were zeroed -- which is a
    # close, whatever the comment above it said about letting them run. So
    # lowering the cap under a live book, which is exactly what a capacity
    # figure computed from a falling balance does, would have liquidated
    # positions for an administrative reason and at a moment of the book's
    # choosing rather than the strategy's.
    #
    # The book is therefore allowed to sit *above* the limit after the cap
    # is lowered, and drains below it as positions exit on their own terms.
    # That is the same rule departing instruments already follow: leaving
    # the list stops re-entry and nothing else.
    incumbent = wanted & (units != 0)
    keep = incumbent.copy()
    order = np.argsort(-np.abs(target_units))
    room = limit - int(incumbent.sum())
    for index in order:
        if room <= 0:
            break
        if wanted[index] and not keep[index]:
            keep[index] = True
            room -= 1

    # A refused newcomer is flat, not short: zero the units *and* the
    # direction, or the position frame would record a holding the book
    # never took.
    target_units = np.where(keep, target_units, 0.0)
    new_held = np.where(keep, new_held, 0).astype(new_held.dtype)
    return target_units, new_held


def _quantise_to_deal_size(target_units, units, columns, config):
    """Round every target down to the broker's dealing increment.

    The engine had no notion of a minimum deal size, and the consequence is
    larger than it sounds: every cost it charges is a fraction of notional,
    so nothing in the model responded to the size of the account at all. A
    1,000 book and a 100,000 book produced the same CAGR, and only one of
    them could place the orders. This is what makes the balance matter.

    ``bridge._round_down`` is the rule being modelled: down to the increment,
    never up, because a size rounded up can breach the limit it was computed
    from. Capital treats ``min_deal_size`` as floor and step at once.

    Three cases, and the second two are why this is not one line:

      * flat, and the target is under one increment -- **refused**. This is
        the case that removes a name from a small account's universe
        entirely, and it is the whole point of modelling it.
      * held, and the rounded target lands on zero -- **left alone**. The
        rounding must not manufacture a close: the position is fundable, it
        is already open, and closing it here would be the model inventing an
        exit the account never makes.
      * a deliberate close, target already zero -- **survives**. Refusing an
        exit is never the safe direction, and a guard that could not tell a
        rounded-away open from an intended close would do exactly that.

    An instrument with no recorded increment is left unconstrained rather
    than refused. That is absence named, not absence treated as permission:
    the alternative reading -- refuse what we cannot measure -- would empty
    the book on the first run against a fresh spread store.
    """
    increments = getattr(config, "min_deal_sizes", None)
    if not increments:
        return target_units

    step = np.array(
        [float(increments.get(str(name), 0.0) or 0.0) for name in columns],
        dtype="float64",
    )
    constrained = step > 0
    if not constrained.any():
        return target_units

    out = np.asarray(target_units, dtype="float64").copy()
    held = np.asarray(units, dtype="float64")

    with np.errstate(invalid="ignore", divide="ignore"):
        steps = np.floor(np.abs(out) / np.where(constrained, step, 1.0))
    rounded = np.sign(out) * steps * step

    apply = constrained & (out != 0.0)
    # A held position the rounding would erase keeps what it holds.
    rescued = apply & (rounded == 0.0) & (held != 0.0)
    out = np.where(apply, rounded, out)
    return np.where(rescued, held, out)


def _apply_margin_cap(target_units, reference, equity, columns, config):
    """Scale the book so committed margin stays inside its ceiling.

    The engine had no margin model at all, which CLAUDE.md records as the one
    risk nothing measured had priced: a backtest can hold 4.5x gross through
    a 44% drawdown because no counterparty ever asks it for variation margin,
    while a live account is closed out first. This is the crude version --
    it bounds what the book may commit and does not simulate a close-out.

    Per instrument, because that is the case a constant cannot express. A
    broker raising its factor on semiconductors and not on staples is exactly
    the scenario worth testing, and a single number cannot state it.

    A missing factor falls back to ``default_margin_factor`` rather than to
    zero. Zero would mean "requires no margin", which sizes without limit --
    absence treated as permission, which is the failure this project names
    in every other layer.

    Scales rather than refuses, matching ``_apply_leverage_cap`` directly
    above it: the book is shrunk to fit, and a position already held is not
    liquidated by this function.
    """
    ceiling = getattr(config, "max_margin_usage", None)
    if ceiling is None:
        return target_units
    ceiling = float(ceiling)
    if ceiling <= 0 or equity <= 0:
        return target_units

    factors = getattr(config, "margin_factors", None) or {}
    fallback = float(getattr(config, "default_margin_factor", 0.20) or 0.0)
    per_asset = np.array(
        [float(factors.get(str(name), fallback)) for name in columns],
        dtype="float64",
    )
    price = np.abs(np.nan_to_num(reference))
    required = float(np.sum(np.abs(target_units) * price * per_asset))
    allowed = ceiling * float(equity)
    if required <= allowed or required <= 0:
        return target_units
    return target_units * (allowed / required)


def _defer_surplus_opens(target_units, units, confidence, config):
    """Cap how many NEW positions may open on one bar, by conviction.

    ``bridge.plan_orders`` opens at most ``max_opens_per_pass`` positions in
    a pass and defers the rest, ranked by the confidence the last look read
    back. It is a margin control rather than a selection rule: a day on which
    every name fires at once would otherwise commit the whole balance in one
    go, and the deferred names are reconsidered fifteen minutes later.

    Deferred, not refused, and the distinction matters for what this models.
    Zeroing the target leaves the position unopened on this bar only; the
    signal is re-evaluated on the next one and may open then. That is the
    faithful analogue of waiting for the next pass.

    Opens only. A close is never deferred -- refusing an exit is never the
    safe direction -- and a resize is an adjustment to something already
    held, not a new commitment of margin. Both are left alone, which is the
    line the dealing path draws.

    A reversal is not an open here, for the same reason it is not one there:
    the bridge splits a flip into a close and an open, and only the flat-to-
    held transition consumes the budget.
    """
    limit = getattr(config, "max_opens_per_pass", None)
    if limit is None:
        return target_units
    limit = int(limit)
    if limit < 0:
        return target_units

    opening = (target_units != 0) & (units == 0)
    if int(opening.sum()) <= limit:
        return target_units

    # Rank by conviction, and send an absent score to the back rather than
    # treating it as zero -- "no reading" is not "no confidence", and a nan
    # sorting high would hand the budget to the least-understood name.
    scores = np.where(
        opening, np.nan_to_num(confidence, nan=-np.inf, neginf=-np.inf), -np.inf
    )
    keep = np.argsort(-scores, kind="stable")[:limit]
    allowed = np.zeros(len(target_units), dtype=bool)
    allowed[keep] = True
    return np.where(opening & ~allowed, 0.0, target_units)


def _apply_concentration_cap(units, reference, equity, config):
    """Trim any single instrument back to its share of the gross ceiling.

    Measured against the *ceiling* rather than against gross actually held,
    so the limit does not tighten as the book empties: the single position
    in a one-name book is allowed its full share, not the whole account.

    Per instrument, and deliberately not proportional. The gross cap that
    follows scales the whole book at once, which cannot stop one name
    dominating -- that is this function's job and the reason it exists
    separately.
    """
    share = getattr(config, "concentration", None)
    if share is None:
        return units
    share = float(share)
    if not 0 < share < 1:
        return units

    price = np.abs(np.nan_to_num(reference))
    ceiling = float(config.max_leverage) * max(float(equity), 0.0) * share
    if ceiling <= 0:
        return np.zeros_like(units)

    notional = np.abs(units) * price
    with np.errstate(divide="ignore", invalid="ignore"):
        scale = np.where(notional > ceiling, ceiling / notional, 1.0)
    return units * np.nan_to_num(scale, nan=1.0, posinf=1.0)


def _apply_leverage_cap(units, reference, equity, config, *, held=None,
                        confidence=None):
    """Bring gross notional inside the cap, by scaling or by selecting.

    ``units`` is the TARGET book; ``held`` is what is currently held, needed
    only to tell a new open from a position already carried.

    **Scaling is the default and is what every recorded figure was measured
    on.** It shrinks the whole book proportionally, on the reasoning that the
    cap decides how much risk in total rather than which asset gets it. That
    reasoning has a consequence nobody had stated: a trade that just scrapes
    the entry bar is never refused. It opens, and every existing position is
    trimmed pro rata to fund it -- conviction-blind, so a 0.76 entry dilutes
    a 0.95 position by the same percentage, and spread is paid to shrink the
    good ones. The owner's objection on 2026-09-07 was that a book should
    choose its trades rather than fill to capacity; this is that objection
    arriving from the other side of the cap.

    **Selecting** drops the lowest-conviction NEW opens until the book fits.
    Only then does it scale, and only over positions that are actually
    changing, so a held position is never trimmed to fund a new one. That is
    what ``bridge.plan_orders`` does live, where the ceiling shrinks ``open``
    and ``resize`` intents and leaves a position sitting at ``none`` alone.

    The two differ in what they do when the cap still binds after every new
    open is gone: nothing. A book that has drifted above its ceiling on
    falling equity stays there and drains as positions exit on their own
    terms. That is the same deliberate choice as ``max_open_positions`` --
    never liquidate into a drawdown -- and it is what the account does.

    An absent ``held`` or ``confidence`` under selection is a programming
    error rather than missing data, and raises: silently falling back to
    scaling would be a gate reporting a clean pass while inert, which is this
    project's most-repeated failure.
    """
    price = np.nan_to_num(reference)
    cap = config.max_leverage * max(equity, 0.0)
    if cap <= 0:
        return np.zeros_like(units)

    gross = float(np.abs(units * price).sum())
    if gross <= cap:
        return units

    if not getattr(config, "leverage_cap_selects", False):
        return units * (cap / gross)

    if held is None or confidence is None:
        raise ValueError(
            "leverage_cap_selects needs the held book and the confidence row "
            "to rank by; without them it can only scale, which is the "
            "behaviour it exists to replace"
        )

    out = np.asarray(units, dtype="float64").copy()

    # Weakest new opens go first. A score that is absent sorts to the front
    # rather than the back -- "no reading" is not conviction, and the name
    # nobody can rank is the one to drop when room is short. That is the
    # opposite convention from _defer_surplus_opens, deliberately: there an
    # absent score loses its slot to a ranked name, and here it loses its
    # place in the book, which are the same outcome stated from either end.
    opening = np.flatnonzero((out != 0) & (np.asarray(held) == 0))
    if opening.size:
        scores = np.nan_to_num(
            np.asarray(confidence, dtype="float64")[opening],
            nan=-np.inf, neginf=-np.inf,
        )
        for index in opening[np.argsort(scores, kind="stable")]:
            if gross <= cap:
                break
            gross -= float(abs(out[index] * price[index]))
            out[index] = 0.0

    if gross <= cap:
        return out

    # Still over with every new open gone, so the breach is in the book that
    # is already there. Scale only what is changing, exactly as the dealing
    # path does, and leave a held-and-unchanged position untouched.
    changing = out != np.asarray(held)
    if not changing.any():
        return out

    settled = float(np.abs(np.where(changing, 0.0, out) * price).sum())
    moving = float(np.abs(np.where(changing, out, 0.0) * price).sum())
    room = cap - settled
    if moving <= 0:
        return out
    if room <= 0:
        # No room at all: a change may not add exposure, so it reverts to
        # what is held -- which zeroes an open and cancels a resize.
        return np.where(changing, np.asarray(held), out)
    return np.where(changing, out * (room / moving), out)


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


def _trade_reason(
    stop_out, was_stopped, breach, position, timed_out: bool = False
) -> str:
    """Why a trade happened, in one word.

    ``timed_out`` was missing, and its absence was not a gap but a wrong
    answer: the time stop closes a position by zeroing it, which then fell
    through to ``"signal"``. Every close in a two-year run therefore claimed
    to be the signal's doing, including the ones the time stop made, and
    nothing could tell them apart -- so "how often does the time stop fire"
    was unanswerable from the log that exists to answer it.

    Ordered by which rule actually acted. A book-wide stop-out overrides
    everything; a price stop is the position's own; the time stop comes last
    because it only fires on a position no other rule has already closed.
    """
    if stop_out:
        return "stop_out"
    if was_stopped and breach is not None:
        return (
            "take_profit" if breach["is_take_profit"][position] else "stop_loss"
        )
    if timed_out:
        return "time_stop"
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
    financing_by_asset=None, spread_coverage=None,
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
        "financing_by_asset": pd.Series(
            np.zeros(len(columns)) if financing_by_asset is None
            else financing_by_asset,
            index=columns, name="financing",
        ),
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
        # How much of the largest modelled cost was read off a quote rather
        # than assumed. Reported rather than logged: a run that charged a
        # constant and one that charged measured spreads produce the same
        # shaped result, and only this distinguishes them.
        "spread_coverage": _spread_coverage(spread_coverage),
    }


def _spread_coverage(counts) -> dict:
    """The measured/estimated split, or an explicit "not measured".

    ``None`` means nothing was supplied, which is reported as a zero
    measured fraction rather than omitted -- absent and zero are the same
    claim here, and both are honest, but silence would not be.
    """
    measured, total = (counts or (0, 0))
    return {
        "measured_cells": int(measured),
        "total_cells": int(total),
        "measured_fraction": (float(measured) / total) if total else 0.0,
    }


def _empty_result():
    """A correctly shaped result for an empty run."""
    return {
        "financing_by_asset": pd.Series(dtype="float64", name="financing"),
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
        "spread_coverage": _spread_coverage(None),
    }
