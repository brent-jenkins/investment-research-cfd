"""The :class:`ExecutionFrame`: positions, trades, P&L and equity.

The end of the pipeline as it stands. Reads a
:class:`~cfdinvest.signals.SignalFrame` and an
:class:`~cfdinvest.preprocessing.AnalysisFrame`, and writes to neither.

Performance *analytics* — Sharpe, Sortino, win rate, expectancy — are
deliberately absent. This layer's job is to be an honest simulator; judging
the strategy is a separate concern with its own layer, and mixing the two
makes it tempting to tune the simulator until the numbers look good.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from cfdinvest.execution import engine as engine_module
from cfdinvest.execution import pnl as pnl_module
from cfdinvest.execution.state import HysteresisThresholds
from cfdinvest.execution.fx import FXConfig
from cfdinvest.execution.stops import StopConfig, recommended_orders
from cfdinvest.schema import TIMESTAMP

logger = logging.getLogger(__name__)

__all__ = ["ExecutionConfig", "ExecutionFrame", "run_backtest"]


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    """Fill, cost, financing and risk settings for a simulation."""

    # -- account ---------------------------------------------------------- #
    initial_equity: float = 100_000.0
    #: Notional per unit of signal size, as a multiple of equity. At the
    #: default, a signal of size 1.0 asks for a position equal to equity.
    exposure_per_unit_size: float = 1.0

    # -- fills ------------------------------------------------------------ #
    #: Bars between a signal and the fill it produces. ``1`` means a signal
    #: from bar t is executed on bar t+1 — the only setting free of
    #: look-ahead, and the reason it is the default.
    execution_lag: int = 1
    #: ``"open"`` (default), ``"mid"`` or ``"close"``. See
    #: :func:`~cfdinvest.execution.fills.fill_prices` for why anything other
    #: than ``"open"`` is mildly optimistic.
    fill_mode: str = "open"
    slippage_fraction: float = 0.0
    default_spread_bps: float = 5.0
    #: Per-asset half-spread overrides, in basis points of price.
    spread_bps: dict[str, float] | None = None
    asset_classes: dict[str, str] | None = None
    #: Measured spread per bar per asset, in **basis points of price**, as a
    #: time x ticker frame. Where a value is present it replaces the constant
    #: for that bar; where it is missing the constant applies and the bar is
    #: counted as estimated rather than measured.
    #:
    #: This exists because the constant is the largest single item in the
    #: modelled cost and is uniform across instruments that differ by eight
    #: times. Broker bars carry a bid and an ask, so the figure can be read
    #: rather than assumed -- but only for the instruments and hours actually
    #: quoted, which is why the fallback is counted and reported rather than
    #: blended in silently.
    #:
    #: A per-instrument median is not a substitute. Spread distributions are
    #: heavily right-skewed, so a median charges the quiet moments and misses
    #: every wide one; measured on the eight-name book the median understated
    #: the cost by a few points where the per-bar panel understated it by
    #: nothing. At roughly 1,200 round trips a year that difference is the
    #: headline figure.
    spread_panel: pd.DataFrame | None = None
    #: Read the spread off the analysis frame's own ``spread_bp`` panel when
    #: no explicit ``spread_panel`` is supplied.
    #:
    #: Broker bars carry bid, ask and the spread between them, and
    #: preprocessing already keeps that panel beside the prices -- so on
    #: Capital data the measured figure is present at execution time and was
    #: simply not being read. Measured on the eight-name book over Capital's
    #: full history: median 11.32bp against the 5.0bp constant, 2.26x, with
    #: only TSLA cheaper than the assumption and INTC at 5.75x.
    #:
    #: That gap matters more here than it would elsewhere. The median
    #: position lives about four hours and 80% close within a day, so spread
    #: is the dominant cost and financing is nearly irrelevant -- an error in
    #: the spread is an error in almost the whole cost model.
    #:
    #: Default ``False`` deliberately. Turning this on changes what every
    #: backtest charges, so it is a named, deliberate switch rather than a
    #: silent improvement: a recorded figure stays reproducible, and a run
    #: that charges measured spreads says so. An explicit ``spread_panel``
    #: still wins, because a caller that supplied one meant it.
    use_measured_spreads: bool = False
    #: Which regime decides where a breakout confirmation is required:
    #: ``"trend"`` (the historical behaviour), ``"volatility"``, ``"both"``
    #: or ``"any"``. See ``signals.breakout.gating_frame``.
    #:
    #: It was conditioned on the trend regime because that is the frame the
    #: runner happened to pass, not because the alternatives were measured.
    #: Volatility is the interesting one: magnitude is predictable on this
    #: book and direction is not, so requiring confirmation where a large
    #: move is forecast lets the breakout supply the side that the magnitude
    #: forecast cannot.
    #:
    #: **Measured 2026-08-30, and the composition fails.** Screen-gated at
    #: target 10 on Capital bars with measured spreads:
    #:
    #:     gated on        CAGR    maxDD   Calmar   trades
    #:     any           131.7%   -40.7%     3.24    3,730
    #:     trend         130.5%   -40.3%     3.24    3,968
    #:     volatility    118.3%   -42.0%     2.82    4,051
    #:     both          122.0%   -41.9%     2.91    4,117
    #:
    #: Conditioning on volatility costs 12.2pp against trend, beyond the
    #: 8.0pp floor. The mechanism reads clearly in hindsight: gating *only*
    #: in loud names means not requiring confirmation in quiet ones, and this
    #: book's quiet positions are its better ones -- the minute study found
    #: them gaining +0.779% from here against +0.317% for volatile ones. It
    #: removes the confirmation exactly where confirmation was paying.
    #:
    #: The incidental finding is the more useful one: ``any`` matches
    #: ``trend`` within the floor, so the regime conditioning is decoration.
    #: The gate earns its +9.9pp by existing, not by being restricted to
    #: trending bars, and that had never been compared against not
    #: conditioning at all. Left at ``trend`` regardless: +1.3pp is no
    #: evidence to move a live parameter, even toward something simpler.
    breakout_regime: str = "trend"
    #: How strongly position size follows confidence above the entry bar.
    #: ``None`` or 0 sizes every accepted signal identically, which is the
    #: behaviour up to 2026-08-30. See ``engine._size_by_confidence``.
    confidence_sizing_strength: float | None = None
    #: The smallest multiplier confidence sizing may produce. A signal that
    #: cleared the gate is worth taking; this trims it rather than erasing it.
    confidence_sizing_floor: float = 0.25
    #: Bars on which the operator may change a position, as a boolean Series
    #: over the analysis index. ``None`` means every bar, which is what every
    #: recorded run assumed.
    #:
    #: This is not a market-hours flag -- it is an *operator* flag. The
    #: daemon deals during market hours and is not running otherwise, so on
    #: any other bar it cannot open, close or resize whatever the signal
    #: says. Marking to market still happens on every bar, because the
    #: position is genuinely exposed overnight; only *acting* is blocked.
    #:
    #: Without it, a share-of-equity target silently drifts as prices move
    #: overnight and trips the rebalance threshold, producing resizes at
    #: 3am that no operator placed. Measured on Capital's extended-hours
    #: bars: 45.7% of all trades executed outside dealing hours, at 12.7bp
    #: against 6.1bp inside.
    #:
    #: Preferred over simply discarding extended-hours bars, which is the
    #: cruder fix: dropping them removes the overnight exposure from the
    #: measurement as well as the trading, and the position is genuinely
    #: held through the night.
    dealing_mask: pd.Series | None = None
    #: Minutes after a session opens during which no NEW position is taken.
    #:
    #: The bell is the dearest moment of the day and the cheapest thing to
    #: skip -- the spread is at its widest and the first prints are noise.
    #:
    #: **This existed in production and not in the engine, which is why it is
    #: here.** ``schedule/app.py`` has applied a 30-minute delay to live
    #: dealing since the bridge was written, and the backtest modelled none
    #: of it: it opened trades at 13:00 UTC that the daemon would have
    #: refused, so the two disagreed about when trading starts. A backtest
    #: that models a different strategy from the one running is the failure
    #: this project has already paid for with the shadow book.
    #:
    #: Applied to ENTRIES only. A position already held may still be closed
    #: or resized inside the window -- refusing an exit is never the safe
    #: direction, and the live path draws the same line.
    #:
    #: Measured against the bar index rather than a calendar, because the
    #: panel is already session-restricted so the first bar of a day IS the
    #: session's first bar. On hourly bars a 30-minute delay therefore
    #: refuses exactly the opening bar. That is an approximation of the live
    #: rule, which compares against wall-clock market hours, and it is the
    #: closest a bar-resolution model can come.
    open_delay_minutes: int = 0
    #: Trades smaller than this in units are ignored as noise.
    minimum_trade_units: float = 1e-9

    # -- hysteresis ------------------------------------------------------- #
    thresholds: HysteresisThresholds = field(default_factory=HysteresisThresholds)
    #: Relative drift required before an existing position is resized.
    rebalance_threshold: float = 0.25

    # -- financing -------------------------------------------------------- #
    benchmark_rate: float = 0.05
    financing_markup: float = 0.03
    day_count: float = 365.0
    #: Per-asset ``{ticker: (long_rate, short_rate)}`` overrides.
    financing_rates: dict[str, tuple[float, float]] | None = None

    # -- stops and targets -------------------------------------------------- #
    #: Intrabar stop-loss and take-profit policy. **Disabled by default.**
    #: Enabling it changes the result of every backtest -- positions that
    #: previously rode through a spike now exit -- so it is opt-in rather
    #: than a silent revision of numbers already recorded. Turn it on for
    #: realism: without it, reported drawdowns are smaller than the ones that
    #: actually happened.
    stops: StopConfig = field(default_factory=StopConfig)

    # -- currency ----------------------------------------------------------- #
    #: Currency conversion policy. **Disabled by default**, which treats every
    #: instrument as already denominated in the account currency -- exactly
    #: what the engine assumed before this existed, so no recorded number
    #: moves. Enable it for any book holding foreign instruments: the fee is
    #: charged on turnover rather than on skill, so ignoring it flatters a
    #: high-turnover strategy most.
    fx: FXConfig = field(default_factory=FXConfig)

    # -- re-entry ----------------------------------------------------------- #
    #: Calendar days an instrument is left alone after a position in it
    #: closes. ``0`` disables it, which is what every recorded run used.
    #:
    #: Measured on the 2005-2026 run: round trips reopened within 21 days of
    #: the last one closing lost £1,489 across 145 trips, while those opened
    #: after a longer gap made £19,954 across 512. The prior trip's outcome
    #: barely mattered -- a re-entry after a win won 48.9% of the time and
    #: after a loss 51.2% -- so this is not about chasing a gain. A fast
    #: return to the same instrument is simply a weaker signal.
    #:
    #: It suppresses new positions only. A position already open is never
    #: closed by it, and nothing here stops the *other* side being taken --
    #: a trend that genuinely reverses is a different trade.
    reentry_cooldown_days: int = 0

    #: ``{ticker: [ISO date, ...]}`` of reporting dates, from
    #: :mod:`cfdinvest.ingestion.earnings`. Instruments absent from the
    #: mapping are never blacked out -- an index has no earnings, and a
    #: share whose dates could not be fetched must not be silently treated
    #: as though it had none.
    earnings_dates: dict[str, list] | None = None
    #: Days before a reporting date on which no new position is opened, and
    #: an open one is closed. ``0`` disables it.
    #:
    #: Across the nineteen shares here, an earnings session moves 3.8x as
    #: far as an ordinary one, and those sessions sum to -282% of log
    #: return while every other day sums to +7,161%. The move is real, it
    #: is large, and its direction is not knowable in advance -- which is
    #: an argument for not having a position rather than for guessing.
    earnings_blackout_days: int = 0

    #: Days after which a position still under water is closed. ``None``
    #: disables it, which is what every recorded run used.
    #:
    #: Not a stop. A stop asks how far the price has gone against you; this
    #: asks how long it has failed to go for you, which the trade log says
    #: is the better question. Marked to market with financing deducted and
    #: measured on index CFDs long, 73% of eventual losers were already
    #: negative on their first day against 29% of eventual winners, and the
    #: gap widens: by day 13 it is 89% against 4%. Losers do not plunge,
    #: they fail to start.
    time_stop_days: float | None = None
    #: How far under water counts, as a percent of the entry price. ``0.0``
    #: means any loss at all; a small negative figure tolerates noise.
    time_stop_threshold: float = 0.0

    # -- risk ------------------------------------------------------------- #
    #: The most positions that may be open at once. ``None`` places no
    #: limit, which is what every recorded run assumed.
    #:
    #: Until this existed the *universe* was the position limit: nothing in
    #: the engine counted positions, so holding the traded list to eight
    #: names was the only thing keeping the book to eight. That conflated
    #: two questions -- which instruments are worth watching, and how many
    #: can be held at once -- and answered the second by deciding the first
    #: a month in advance.
    #:
    #: Separating them measured better. Every one of the forty-nine
    #: candidates eligible with eight slots returned 13.6% against 7.5% for
    #: a monthly list of eight, and was ahead in both halves of the span
    #: (0.64/1.11 against 0.58/0.58 return-per-drawdown). Six slots measured
    #: better still on a risk-adjusted basis. The gain is *eligibility*, not
    #: the cap: an uncapped book scored higher again, and is unavailable
    #: only because thirty-one concurrent positions fails on minimum deal
    #: sizes long before it fails on margin.
    #:
    #: Incumbents keep their slots. A position already open is not
    #: reconsidered against a newcomer, because churning one name out for a
    #: marginally better-ranked one pays a round trip to act on a difference
    #: the ranking is not accurate enough to resolve -- the same reason the
    #: universe is not reselected weekly.
    max_open_positions: int | None = None

    #: Bars by instrument, ``True`` where a *new* position may open. ``None``
    #: leaves everything permitted. Built by
    #: ``signals.breakout.entry_mask``; see that module for why it is
    #: conditioned on the trend regime and why it gates opening only.
    entry_gate: Any = None

    #: What a position is scaled to once its confidence falls below the
    #: level at which it would have been opened. ``None`` disables it.
    #:
    #: The hysteresis band has an interior region nobody was managing:
    #: entry needs ``entry_confidence`` (0.35) and exit triggers at
    #: ``exit_confidence`` (0.20), so a position whose conviction decays to
    #: 0.30 is too weak to open and too strong to close -- and was held at
    #: full size. This trims that region and nothing else.
    #:
    #: Measured by sweeping the cut point, 3.65 years, all arms holding
    #: genuinely less rather than normalised:
    #:
    #:     cut below   CAGR    maxDD   ratio
    #:       0.30      74.4    -34.4   2.16   (inert: nothing left to cut)
    #:       0.35      77.4    -29.4   2.64
    #:       0.37      74.4    -28.2   2.64
    #:       0.40      65.5    -34.1   1.92
    #:       0.50      41.7    -40.9   1.02
    #:     MIRROR >0.50 35.6   -37.6   0.95
    #:
    #: Single-peaked, monotone collapse above the peak, and the mirror --
    #: cutting the *top* by the same amount -- is the worst arm of all, 41.8
    #: points below. So this is confidence selecting, not exposure falling.
    #:
    #: **The threshold is `entry_confidence` itself, not a number of its
    #: own.** The peak coincided with it, which is what the rule means:
    #: halve a position you would no longer open. One owner, and it follows
    #: any change to the entry bar.
    decayed_position_multiplier: float | None = None

    #: The confidence below which a held position is trimmed. ``None`` means
    #: ``thresholds.entry_confidence``, which is the shipped behaviour and
    #: what the rule means -- halve a position you would no longer open.
    #:
    #: It exists as a separate field only so the threshold can be *swept*
    #: through this config rather than by editing the signal frame. Sweeping
    #: ``entry_confidence`` itself would move the entry bar at the same
    #: time, changing two things at once; sweeping it in a harness measures
    #: a rule the engine does not implement, which is how the first
    #: threshold was chosen and why it has to be re-derived.
    decayed_threshold: float | None = None

    #: Session bars of range a new move must clear before it is entered,
    #: where a trend is running. ``0`` or ``None`` disables the gate.
    #:
    #: The mask itself is built in ``backtest.runner`` -- it needs the price
    #: panel, the direction frame and the regime, none of which exist here.
    #: This is the switch and the parameter; ``signals.breakout`` is the rule.
    #:
    #: Measured through the engine, against a baseline that already includes
    #: the decayed-position trim:
    #:
    #:     no gate    73.6%   -30.0%   ratio 2.46   2,671 trades
    #:      6 bars    79.5%   -26.5%   ratio 3.00   2,588
    #:     12 bars    80.5%   -26.1%   ratio 3.09   2,579
    #:     24 bars    80.2%   -26.1%   ratio 3.08   2,576
    #:     48 bars    80.3%   -26.1%   ratio 3.08   2,577
    #:
    #: One point of spread across an eightfold range of lookback, drawdown
    #: flat to 0.4 points, trades within twelve. That insensitivity is the
    #: reason to believe it -- an earlier version of this gate scattered
    #: over 16.6 points across the same range, and turned out to implement a
    #: different rule from the one that justified it.
    breakout_lookback: int | None = None
    #: Cap on gross notional divided by equity.
    max_leverage: float = 5.0
    #: The largest share of the *gross ceiling* any one instrument may take.
    #: ``None`` places no limit, which is what every recorded run assumed --
    #: and means a single name could carry the entire book.
    #:
    #: The live bridge has always applied a cap and the engine never has, so
    #: the backtest was measuring a book the account would not hold. It
    #: binds hardest exactly where the book is thinnest: measured over 3.65
    #: years the book holds one name on 17.3% of bars and two or fewer on
    #: 30.3%, and at 3.5x gross a single name gapping 8.6% is a margin call.
    #:
    #: Measured, capping it improves the return as well as the drawdown --
    #: this strategy's concentrated positions are its worse bets. See
    #: `config.toml`.
    concentration: float | None = None
    #: Flatten everything when equity falls to this fraction of its peak.
    #: ``None`` disables the stop-out.
    stop_out_level: float | None = None
    #: Whether trading resumes after a stop-out, or the run stays flat.
    resume_after_stop_out: bool = True

    def __post_init__(self) -> None:
        if self.initial_equity <= 0:
            raise ValueError(f"initial_equity must be > 0, got {self.initial_equity}")
        if self.max_leverage <= 0:
            raise ValueError(f"max_leverage must be > 0, got {self.max_leverage}")
        if self.slippage_fraction < 0:
            raise ValueError(
                f"slippage_fraction must be >= 0, got {self.slippage_fraction}"
            )
        if self.stop_out_level is not None and not 0 < self.stop_out_level < 1:
            raise ValueError(
                f"stop_out_level must be in (0, 1), got {self.stop_out_level}"
            )
        if self.execution_lag == 0:
            logger.warning(
                "execution_lag=0 fills on the same bar the signal was computed "
                "from, which is look-ahead. Results are not achievable."
            )


@dataclass(frozen=True, slots=True)
class ExecutionFrame:
    """The result of a simulation.

    Attributes
    ----------
    positions:
        ``timestamp x ticker`` of signed units held.
    notionals:
        The same positions marked at each bar's close.
    trades:
        One row per executed change; see
        :data:`~cfdinvest.execution.engine.TRADE_COLUMNS`.
    pnl:
        Per-bar decomposition. The five components sum to ``total``.
    equity_curve:
        Mark-to-market account value.
    exposure:
        Gross notional divided by equity, per bar.
    metadata:
        Configuration, calendar bounds and headline figures.
    """

    positions: pd.DataFrame = field(default_factory=pd.DataFrame)
    notionals: pd.DataFrame = field(default_factory=pd.DataFrame)
    trades: pd.DataFrame = field(default_factory=pd.DataFrame)
    pnl: pd.DataFrame = field(default_factory=pd.DataFrame)
    equity_curve: pd.Series = field(default_factory=lambda: pd.Series(dtype="float64"))
    exposure: pd.Series = field(default_factory=lambda: pd.Series(dtype="float64"))
    stop_events: pd.DataFrame = field(default_factory=pd.DataFrame)
    stop_levels: pd.Series = field(default_factory=lambda: pd.Series(dtype="float64"))
    take_profit_levels: pd.Series = field(
        default_factory=lambda: pd.Series(dtype="float64")
    )
    entry_prices: pd.Series = field(default_factory=lambda: pd.Series(dtype="float64"))
    #: Financing accrued per instrument across every bar it was carried,
    #: not only the bars it was traded on. The trade log holds the latter;
    #: the difference between them was 60% of the total.
    financing_by_asset: pd.Series = field(
        default_factory=lambda: pd.Series(dtype="float64")
    )
    fx_open_cost: pd.Series = field(
        default_factory=lambda: pd.Series(dtype="float64")
    )
    fx_close_cost: pd.Series = field(
        default_factory=lambda: pd.Series(dtype="float64")
    )
    fx_rate_used: pd.Series = field(
        default_factory=lambda: pd.Series(dtype="float64")
    )
    metadata: dict[str, Any] = field(default_factory=dict)

    # -- views ------------------------------------------------------------ #
    @property
    def index(self) -> pd.Index:
        """The calendar every panel shares."""
        return self.equity_curve.index

    @property
    def fx_total_cost(self) -> float:
        """Everything currency conversion took out of the account.

        The number to hold against the strategy's edge: if conversion costs
        a comparable fraction of equity to what the signal earns, the edge
        is in the broker's pocket rather than the account's.
        """
        return float(self.fx_open_cost.sum() + self.fx_close_cost.sum())

    def drawdown(self) -> pd.DataFrame:
        """Running peak, drawdown and drawdown fraction."""
        return pnl_module.drawdown(self.equity_curve)

    def fx_summary(self) -> pd.DataFrame:
        """FX cost per instrument, beside the rate it was converted at.

        Empty when FX is disabled. The column to read is ``fx_cost`` against
        ``realized_pnl``: an instrument whose conversion cost rivals its P&L
        is one the account would be better off not holding in that currency.
        """
        if self.trades.empty or "fx_conversion_fee" not in self.trades.columns:
            return pd.DataFrame(
                columns=["fx_rate", "fx_cost", "fx_conversion_fee", "trades"]
            )

        costs = self.trades.assign(
            fx_cost=self.trades["fx_conversion_fee"]
            + self.trades["fx_spread"]
            + self.trades["fx_slippage"]
        )
        grouped = costs.groupby("asset")
        return pd.DataFrame(
            {
                "fx_rate": grouped["fx_rate"].last(),
                "fx_cost": grouped["fx_cost"].sum(),
                "fx_conversion_fee": grouped["fx_conversion_fee"].sum(),
                "trades": grouped.size(),
            }
        ).sort_index()

    def working_orders(self) -> pd.DataFrame:
        """The stop and take-profit orders to place, for the current book.

        **Nothing is sent anywhere.** The simulator assumes a resting order
        exists; a manually traded account only has one if you place it. This
        is the list to place, and the gap between the two is the difference
        between the backtest and the account.

        Empty when stops are disabled, or when nothing is open.
        """
        if self.positions.empty:
            return recommended_orders(
                pd.Series(dtype="float64"), pd.Series(dtype="float64"),
                pd.Series(dtype="float64"), pd.Series(dtype="float64"),
                pd.Series(dtype="float64"),
            )

        latest = self.positions.iloc[-1]
        last_price = (
            self.notionals.iloc[-1] / latest.where(latest != 0)
            if not self.notionals.empty
            else pd.Series(float("nan"), index=latest.index)
        )
        return recommended_orders(
            latest,
            self.entry_prices,
            self.stop_levels,
            self.take_profit_levels,
            last_price,
            timestamp=self.index[-1] if len(self.index) else None,
        )

    def stop_summary(self) -> pd.DataFrame:
        """Counts and average slippage per breach kind and type.

        The row that matters is ``gap``: it is where a stop cost more than
        its level, and a strategy whose exits are mostly gaps is one whose
        backtested stop distances are fiction.
        """
        if self.stop_events.empty:
            return pd.DataFrame(
                columns=["events", "mean_slippage", "total_units_closed", "ambiguous"]
            )

        # abs() before grouping: a SeriesGroupBy has no .abs(), and summing
        # signed units would net a long exit against a short one.
        events = self.stop_events.assign(
            units_closed=self.stop_events["units_closed"].abs()
        )
        grouped = events.groupby(["kind", "breach_type"])
        return pd.DataFrame(
            {
                "events": grouped.size(),
                "mean_slippage": grouped["slippage_applied"].mean(),
                "total_units_closed": grouped["units_closed"].sum(),
                "ambiguous": grouped["ambiguous"].sum(),
            }
        ).sort_index()

    def active_positions(self) -> pd.DataFrame:
        """Boolean panel of bars holding a position."""
        if self.positions.empty:
            return pd.DataFrame()
        return self.positions != 0

    def trade_summary(self) -> pd.DataFrame:
        """Counts and totals per action, for a sanity check on churn."""
        if self.trades.empty:
            return pd.DataFrame(
                columns=["trades", "realized_pnl", "spread_cost", "slippage"]
            )
        grouped = self.trades.groupby("action")
        return pd.DataFrame(
            {
                "trades": grouped.size(),
                "realized_pnl": grouped["realized_pnl"].sum(),
                "spread_cost": grouped["spread_cost"].sum(),
                "slippage": grouped["slippage"].sum(),
            }
        ).sort_index()

    def per_asset(self) -> pd.DataFrame:
        """Trade count, costs and realised P&L per instrument."""
        if self.trades.empty:
            return pd.DataFrame(columns=["trades", "realized_pnl", "cost"])
        grouped = self.trades.groupby("asset")
        summary = pd.DataFrame(
            {
                "trades": grouped.size(),
                "realized_pnl": grouped["realized_pnl"].sum(),
                "cost": grouped["spread_cost"].sum() + grouped["slippage"].sum(),
            }
        )
        return summary.sort_index()

    def holding_periods(self) -> pd.Series:
        """Bars held per completed position, as a churn diagnostic.

        A median of one or two means the hysteresis band is too narrow and
        the strategy is paying the spread to stand still.
        """
        if self.positions.empty:
            return pd.Series(dtype="float64", name="bars_held")

        spans = []
        for ticker in self.positions.columns:
            column = self.positions[ticker]
            active = column != 0
            if not active.any():
                continue
            groups = (active != active.shift()).cumsum()
            for _, run in column.groupby(groups):
                if run.iloc[0] != 0:
                    spans.append(len(run))
        return pd.Series(spans, dtype="float64", name="bars_held")


def run_backtest(
    signal_frame,
    analysis_frame,
    config: ExecutionConfig | None = None,
) -> ExecutionFrame:
    """Simulate a strategy end to end.

    Parameters
    ----------
    signal_frame:
        Target positions from the signal layer.
    analysis_frame:
        The prices those signals were built from.
    config:
        Fill, cost, financing and risk settings.

    Returns
    -------
    ExecutionFrame
    """
    settings = config or ExecutionConfig()
    result = engine_module.run_execution(signal_frame, analysis_frame, settings)

    equity = result["equity_curve"]
    summary = (
        pnl_module.summarise(result["pnl"], equity, settings.initial_equity)
        if not equity.empty
        else {}
    )

    metadata = {
        "bars": len(equity),
        "calendar_start": equity.index.min() if len(equity) else None,
        "calendar_end": equity.index.max() if len(equity) else None,
        "tickers": tuple(result["positions"].columns),
        "trades": len(result["trades"]),
        "execution_lag": settings.execution_lag,
        "fill_mode": settings.fill_mode,
        "max_leverage": settings.max_leverage,
        "stop_out_level": settings.stop_out_level,
        "stops_enabled": settings.stops.enabled,
        "stop_mode": settings.stops.stop_mode,
        "stop_distance": settings.stops.stop_distance,
        "stop_distance_mode": settings.stops.stop_distance_mode,
        "tp_distance": settings.stops.tp_distance,
        "tp_distance_mode": settings.stops.tp_distance_mode,
        "tp_priority": settings.stops.tp_priority,
        "slippage_bps": settings.stops.slippage_bps,
        "max_slippage_bps": settings.stops.max_slippage_bps,
        "fx_enabled": settings.fx.enabled,
        "account_currency": settings.fx.account_currency,
        "fx_conversion_fee_pct": settings.fx.fx_conversion_fee_pct,
        "fx_spread_bps": settings.fx.fx_spread_bps,
        "fx_slippage_bps": settings.fx.fx_slippage_bps,
        "fx_rate_source": settings.fx.fx_rate_source,
        # How much of the spread was read off a quote rather than assumed.
        # Carried out of the engine because a run charging measured spreads
        # and one charging a constant are otherwise identical objects, and
        # the difference is the largest single item in the cost model.
        "spread_coverage": result.get("spread_coverage", {}),
        "fx_rounding_mode": settings.fx.fx_rounding_mode,
        "fx_fee_basis": settings.fx.fx_fee_basis,
        "stop_events": len(result["stop_events"]),
        "gap_through_exits": int(
            (result["stop_events"]["breach_type"] == "gap").sum()
        )
        if not result["stop_events"].empty
        else 0,
        "thresholds": {
            "entry_size": settings.thresholds.entry_size,
            "exit_size": settings.thresholds.exit_size,
            "entry_confidence": settings.thresholds.entry_confidence,
            "exit_confidence": settings.thresholds.exit_confidence,
        },
        "financing": {
            "benchmark_rate": settings.benchmark_rate,
            "markup": settings.financing_markup,
        },
        **summary,
    }

    return ExecutionFrame(
        positions=result["positions"],
        notionals=result["notionals"],
        trades=result["trades"],
        pnl=result["pnl"],
        equity_curve=equity,
        exposure=result["exposure"],
        stop_events=result["stop_events"],
        stop_levels=result["stop_levels"],
        take_profit_levels=result["take_profit_levels"],
        entry_prices=result["entry_prices"],
        financing_by_asset=result["financing_by_asset"],
        fx_open_cost=result["fx_open_cost"],
        fx_close_cost=result["fx_close_cost"],
        fx_rate_used=result["fx_rate_used"],
        metadata=metadata,
    )
