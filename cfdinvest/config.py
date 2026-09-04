"""Typed configuration for the cfdinvest engine.

Configuration resolution order (later wins):

1. Dataclass defaults defined in this module.
2. A TOML file (``config/config.toml`` by default, or ``$CFD_CONFIG_FILE``).
3. Environment variables (optionally seeded from a ``.env`` file).
4. An explicit ``overrides`` mapping passed by the caller.

No path is hardcoded in library code: every module receives an
:class:`AppConfig` (or a sub-config) and derives its paths from it.

Example
-------
>>> cfg = load_config()                      # doctest: +SKIP
>>> cfg.paths.raw_market_dir                 # doctest: +SKIP
WindowsPath('data/raw/market')
"""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass, replace
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)

#: Environment variable holding an alternative path to the TOML config file.
ENV_CONFIG_FILE = "CFD_CONFIG_FILE"


# --------------------------------------------------------------------------- #
# Sub-configurations
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class PathConfig:
    """Filesystem layout of the data lake.

    All directories derive from :attr:`data_root`, so relocating the lake
    (e.g. onto a fast SSD or a network share) is a single-value change.
    """

    data_root: Path = Path("data")

    def __post_init__(self) -> None:
        # Coerced rather than required: a data root arrives as a string from
        # TOML, from an environment variable and from every --data-root flag,
        # and without this the failure is a TypeError raised deep inside a
        # property -- "unsupported operand type(s) for /: 'str' and 'str'" --
        # which says nothing about where the string came from.
        if not isinstance(self.data_root, Path):
            object.__setattr__(self, "data_root", Path(self.data_root))

    @property
    def raw_dir(self) -> Path:
        """Vendor payloads: normalised in shape, unaltered in value."""
        return self.data_root / "raw"

    @property
    def raw_market_dir(self) -> Path:
        """OHLCV bars, one Parquet file per ``interval``/``ticker``."""
        return self.raw_dir / "market"

    @property
    def raw_macro_dir(self) -> Path:
        """FRED series, one Parquet file per ``series_id``."""
        return self.raw_dir / "macro"

    @property
    def metadata_dir(self) -> Path:
        """Series/ticker descriptors and ingestion provenance."""
        return self.data_root / "metadata"

    @property
    def processed_dir(self) -> Path:
        """Reserved for the preprocessing layer (aligned panels, features)."""
        return self.data_root / "processed"

    def all_dirs(self) -> tuple[Path, ...]:
        """Return every directory the engine expects to exist."""
        return (
            self.raw_market_dir,
            self.raw_macro_dir,
            self.metadata_dir,
            self.processed_dir,
        )

    def ensure(self) -> "PathConfig":
        """Create the directory tree if missing. Returns ``self`` for chaining."""
        for directory in self.all_dirs():
            directory.mkdir(parents=True, exist_ok=True)
        return self


@dataclass(frozen=True, slots=True)
class MarketDataConfig:
    """Settings for the yfinance ingestion path."""

    tickers: tuple[str, ...] = ()
    #: yfinance bar size. Daily and hourly are the engine's supported grids.
    interval: str = "1d"
    start: str = "2005-01-01"
    #: ``None`` means "up to now".
    end: str | None = None
    #: Apply split/dividend adjustment to OHLC (recommended for backtests).
    auto_adjust: bool = True
    #: Also download dividend and split event columns.
    include_actions: bool = True
    #: Tickers per yfinance batch request. Keeps URLs and memory bounded.
    batch_size: int = 25
    #: Worker threads inside yfinance for a single batch.
    threads: int = 8
    #: Re-download this many trailing days on an incremental update, because
    #: vendors revise recent bars (late prints, adjustment factors).
    refetch_overlap_days: int = 5


@dataclass(frozen=True, slots=True)
class MacroDataConfig:
    """Settings for the FRED ingestion path."""

    #: FRED series ids, e.g. ``("DGS10", "T10Y2Y", "VIXCLS")``.
    series_ids: tuple[str, ...] = ()
    #: Read from ``$FRED_API_KEY`` when absent; never commit a key to TOML.
    api_key: str | None = None
    base_url: str = "https://api.stlouisfed.org/fred"
    observation_start: str = "1990-01-01"
    observation_end: str | None = None
    #: ALFRED vintage window. Leaving both ``None`` returns the *latest*
    #: revision of every observation, which introduces look-ahead bias in a
    #: backtest: today's value of a 2008 GDP print is not what a trader saw in
    #: 2008. Set these (or use a release-lag shift downstream) for
    #: point-in-time correctness.
    realtime_start: str | None = None
    realtime_end: str | None = None
    #: ``"publication_lag"`` (default), ``"initial_release"``, ``"latest"``
    #: or ``"vintage"``. Leaving this ``None`` resolves to
    #: ``"vintage"`` when ALFRED real-time bounds are set, otherwise to
    #: :data:`cfdinvest.schema.DEFAULT_VINTAGE_MODE`.
    #: See :func:`cfdinvest.ingestion.macro.resolve_vintage_mode`.
    vintage_mode: str | None = None
    #: Per-series overrides, e.g.
    #: ``{"DGS10": {"vintage_mode": "latest", "release_lag_days": 1}}``.
    #: In TOML: ``[macro.series_overrides.DGS10]``. A series not listed here
    #: uses the run-level settings above.
    series_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Publication lag applied when first prints are unavailable and a
    #: synthetic publication date must be derived. The default of 1 day
    #: suits daily rate series, which are the only ones that ever fall back.
    fallback_release_lag_days: int = 1
    #: Macro series are revised for months after first publication, so an
    #: incremental update re-reads a generous trailing window.
    refetch_overlap_days: int = 45
    #: Concurrent HTTP requests. FRED permits 120 requests/minute.
    max_workers: int = 6
    #: Minimum seconds between request starts, enforced per process.
    min_request_interval: float = 0.55
    #: Fetch human-readable series metadata (title, units, frequency).
    fetch_metadata: bool = True


@dataclass(frozen=True, slots=True)
class NetworkConfig:
    """Retry/timeout policy shared by every outbound request."""

    timeout: float = 30.0
    max_retries: int = 4
    backoff_base: float = 1.5
    backoff_cap: float = 30.0


@dataclass(frozen=True, slots=True)
class StorageConfig:
    """Parquet writer settings."""

    compression: str = "zstd"
    #: ``None`` lets pyarrow choose; explicit values aid reproducibility.
    row_group_size: int | None = 100_000


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    """Logging behaviour for CLI/example entry points."""

    level: str = "INFO"
    format: str = "%(asctime)s | %(levelname)-8s | %(name)-32s | %(message)s"
    #: Optional path for a rotating log file. ``None`` disables file logging.
    log_file: str | None = None


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    """What the system does, as opposed to how it fetches and stores data.

    Every other section here describes plumbing -- where the lake lives, how
    often to call FRED, which compression to use. None of it changes a
    trading decision. This section is the opposite: every field alters what
    the engine does, and until it existed all of them lived as dataclass
    defaults across three modules and a hardcoded block in the scheduler.

    That distinction matters more than tidiness. Changing a default in
    Python means editing code and restarting a service, and on 2026-08-25 a
    long-running daemon went on executing the modules it had imported at
    start-up while the files on disk said something else -- so FX conversion
    was switched on, and stayed off in production, for eleven hours. A
    setting that lives in a file the process reads on each run cannot fail
    that way, and a run's metadata can record what it actually used.

    The defaults reproduce the behaviour recorded before this existed, so
    adopting the section changes nothing until a value is changed.
    """

    # -- the regime filter -------------------------------------------------- #
    #: ``"gate"`` permits or forbids a direction; ``"tilt"`` scales size by
    #: how favourable the regime is. Measured across ten two-year windows,
    #: tilting returned -40.9% against the gate's -54.7%.
    regime_mode: str = "gate"

    # -- holding and exits -------------------------------------------------- #
    #: Whether a flat signal closes an open position. ``False`` reads flat
    #: as "no view", which quadrupled gross P&L in testing.
    exit_on_flat: bool = True
    #: Whether a signal turning against a position closes it.
    exit_on_opposing_signal: bool = True
    #: Close a position still behind after this many days. ``None`` disables.
    #: On indices it took the win rate from 39.5% to 69.5%.
    #:
    #: Float, not int. On hourly bars the useful stop is a part-session --
    #: 0.29 days is seven hours -- and an int annotation truncated that to
    #: zero, which silently disables the stop rather than failing. The
    #: config loader coerces to the annotated type, so the annotation is
    #: what decides whether a fraction survives being read.
    time_stop_days: float | None = None
    #: Days an instrument is left alone after a position in it closes.
    #: Float for the same reason: a cooldown of hours is now meaningful.
    reentry_cooldown_days: float = 0.0
    #: Relative drift before an open position is resized.
    rebalance_threshold: float = 0.25
    #: Confidence a signal needs to open a position, and the level below
    #: which an open one is closed.
    #:
    #: This is the strategy's real entry bar and it had no owner: it was the
    #: HysteresisThresholds default, so nothing in any configuration file
    #: named the number the engine actually traded on. That mattered more
    #: than the usual tidiness argument -- the paper book's arms were set to
    #: 0.50/0.60/0.70 without anyone checking, so the forward record was
    #: validating a strategy a third worse than the one being backtested
    #: (0.35 returns 126.7% a year against 0.50's 85.6%).
    #:
    #: Third limit in a row found living only in a dataclass default, after
    #: max_leverage and slippage_bps. Anything the engine reads should be
    #: named somewhere a person can see it.
    entry_confidence: float = 0.35
    exit_confidence: float = 0.20

    #: The signal size required to open, and the level an open position is
    #: closed below. **Fourth limit found living only in a dataclass
    #: default**, after max_leverage, slippage_bps and entry_confidence --
    #: ``_execution_config`` built HysteresisThresholds passing only the
    #: confidence pair, so nothing in any configuration file named these and
    #: no sweep could reach them.
    #:
    #: It was put there as a hysteresis floor, to stop a position being
    #: opened for a rounding error and closed again next bar. It had become
    #: the binding *selection* rule: of 51,232 bar x ticker cells, 29,079
    #: carried a view, 23,841 cleared the confidence gate and only 8,269
    #: cleared this. It discarded 65% of what confidence had already
    #: approved, and the book sat flat on 50.6% of bars holding 1.45 names
    #: against a screen offering ten.
    #:
    #: Measured 2026-08-30 through the engine on Capital bars with measured
    #: spreads, screen-gated at target 10, entry and exit scaled together
    #: because the band must satisfy exit <= entry:
    #:
    #:     entry/exit    CAGR    maxDD   Calmar   trades   flat bars
    #:     0.02/0.01   130.5%   -40.3%     3.24    3,968        6.5%
    #:     0.04/0.02   126.2%   -40.8%     3.09    2,770       12.1%
    #:     0.06/0.03    89.5%   -50.7%     1.77    2,063       28.3%
    #:     0.08/0.04    89.3%   -47.3%     1.89    1,639       42.5%
    #:     0.10/0.05    82.3%   -41.3%     1.99    1,283       52.5%
    #:     0.15/0.075   36.6%   -45.1%     0.81      727       71.5%
    #:     0.20/0.10    24.4%   -37.7%     0.65      442       81.3%
    #:
    #: **Not leverage in disguise, and the control is the reason that can be
    #: said.** Removing the floor raises mean gross leverage 0.695 to 1.123,
    #: but levering the 0.10 book by the same 1.62x returns 101.8% at -43.4%
    #: -- it reproduces under half the gain and worsens drawdown. The rest
    #: comes from holding 4.12 names instead of 1.45, which is
    #: diversification rather than exposure. This project once credited such
    #: a gain to selection when it was a mixture nobody had separated, so
    #: the control ran before the finding was believed.
    #:
    #: 0.02 is the floor of ``signal.min_size``, so this is "remove the
    #: floor" rather than "lower it" -- 100% of non-zero signals clear it.
    #: And because exit must not exceed entry, an entry at the signal floor
    #: necessarily retires the size-based exit as well: positions then leave
    #: on a direction flip or the time stop. Loosening the exit alone is
    #: worth +11.5pp of the +48.2pp, so the entry side carries the majority,
    #: but the two are not separable at the bottom of the range and the
    #: figure should be read as the pair.
    entry_size: float = 0.02
    exit_size: float = 0.01

    #: How strongly position size follows conviction above the entry bar.
    #:
    #: Until 2026-08-30 confidence was used only as a gate: everything that
    #: cleared 0.35 was sized identically, whether it scored 0.36 or 0.95, so
    #: the ordering the score carries was discarded at the moment of acting
    #: on it. Sizing by it is worth the table below.
    #:
    #: **RETRACTED 2026-09-03**: "confidence is the best predictor of outcome
    #: this book has -- +0.248 rank correlation against realised trip P&L,
    #: where a purpose-built sixteen-feature model managed +0.129". Both
    #: figures were read on the FILL bar, the basis retracted on 2026-08-30.
    #: ``execution_lag`` is 1, so the engine gates on the previous bar's
    #: value; measured there confidence reads **+0.023** against +0.160 on
    #: the fill bar, a sevenfold difference.
    #:
    #: Rebuilt on the decision bar, walk-forward, refitting each quarter on
    #: everything before it, the sixteen-feature model **beats confidence**
    #: rather than halving it -- +0.0777 against +0.0202 versus trip P&L, and
    #: +0.0767 against +0.0415 versus whether the trip won. So the claim was
    #: not merely mis-scaled, it was backwards.
    #:
    #: It does not change this parameter, and that is worth being explicit
    #: about: the value below was set from arms measured through the engine
    #: and controlled for leverage, not from the correlation. A stale figure
    #: was quoted in its justification; the justification does not rest on
    #: it. What the model *cannot* do is filter -- no band of it loses money
    #: out of sample -- so the estimate belongs in sizing, which is here.
    #: See ``_size_by_confidence`` for why more skill at win rate is not
    #: obviously worth having in a book that earns from a fat tail.
    #:
    #: Measured through the engine, screen-gated at target 10, Capital bars,
    #: measured spreads:
    #:
    #:     strength    CAGR    maxDD   Calmar   mean lev
    #:     0 (off)   130.5%   -40.3%     3.24      1.123
    #:     0.5       254.9%   -41.4%     6.17      1.655
    #:     1.0       319.8%   -47.2%     6.77      1.824
    #:     2.0       357.9%   -50.7%     7.06      1.977
    #:     4.0       371.3%   -53.6%     6.93      2.109
    #:
    #: 0.5 rather than the peak: return nearly doubles while drawdown barely
    #: moves, and the higher strengths buy return with risk instead of
    #: earning it.
    #:
    #: **Not leverage.** Sizing by conviction lifts mean gross leverage from
    #: 1.123 to 1.655, and this project once credited exactly such a gain to
    #: selection when the mechanism was a mixture nobody had separated -- then
    #: retracted it before the control ran. So the control ran first here, and
    #: matched on *realised* leverage rather than nominal, because the caps
    #: bind differently: uniform exposure at leverage 1.659 returns 198.9% at
    #: -46.7%, against conviction's 254.9% at -41.4%. Allocating the same
    #: exposure by conviction is worth +56pp and 5.3pp less drawdown.
    #:
    #: Holds in both halves independently -- +106.6pp in the first, +151.9pp
    #: in the second -- with Calmar improving in each, so it is not one
    #: episode and not risk-bought return.
    #:
    #: The absolute figures are not a forecast. They sit on the screen's own
    #: +150pp ranking edge over a 3.65-year window, and the demo account is
    #: the only thing that can test whether any of it transfers.
    confidence_sizing_strength: float | None = 0.5

    #: The smallest multiplier conviction sizing may produce. A signal that
    #: cleared the gate is worth taking; this trims it rather than erasing
    #: it, which would be a second unmeasured gate wearing a multiplier.
    confidence_sizing_floor: float = 0.25
    #: Execution friction beyond the spread, in basis points of price.
    #:
    #: The spread is charged separately and properly; this is what the
    #: quoted price moves between the decision and the fill. The backtest
    #: assumed zero, which on a book turning over ~15,000 times in two
    #: years is the most flattering assumption in it.
    #:
    #: Measured break-even is near 4bp: at 2bp the strategy returns 81.3% a
    #: year against buy-and-hold's 22.4%, at 5bp it returns 9.4% and is
    #: beaten by it. That number matters more than any headline return,
    #: because it says how much of the edge is available to a real account.
    slippage_bps: float = 2.0
    #: Gross notional the book may carry, as a multiple of equity.
    #:
    #: This is the margin dial. Trading 212 gives 5x on shares, so gross
    #: exposure of G times equity commits roughly G/5 of the account as
    #: margin: 5.0 spends the entire balance, 2.5 spends half.
    #:
    #: It was never passed through from here at all -- the execution
    #: default of 5.0 applied, so the book ran at full margin utilisation
    #: by omission rather than by choice, and no configuration file said so.
    max_leverage: float = 2.5
    #: The largest share of the gross ceiling one instrument may take.
    #:
    #: Read by both the execution engine and `RiskLimits.for_balance`, which
    #: is the point of it living here: it was a default argument in two
    #: places, and two copies of a limit drift.
    max_instrument_concentration: float = 0.40
    #: Minutes after the exchange opens before a *new* position may be
    #: taken. Closes and resizes are never delayed -- being unable to leave
    #: is a worse failure than entering badly.
    #:
    #: The opening hour's median spread is 9.26bp against 7.14bp in the core
    #: session (1.22x), and 28.89bp on INTC. Against an edge that breaks
    #: even near 6bp of friction, the auction is the most expensive moment
    #: of the day to enter.
    open_delay_minutes: int = 30
    #: The most positions that may be *opened* in a single pass. ``None``
    #: places no limit.
    #:
    #: A book starting from flat otherwise opens everything at once: on
    #: 2026-08-28 a cold start would have taken thirteen positions in one
    #: pass, at the widest spreads of the day, on a seventeen-hour-old
    #: signal. Spreading them over successive passes costs nothing when the
    #: book is already warm, because a warm book rarely opens more than one
    #: or two at a time.
    max_opens_per_pass: int | None = 3
    #: The most of the balance that committed margin may consume, or ``None``
    #: to not check it.
    #:
    #: Off by default and deliberately not inherited from
    #: ``RiskLimits.max_margin_usage_pct``, which is 0.50 and belongs to the
    #: manual helper -- borrowing it would silently change hand-trading
    #: advice as well, and one number answering two questions is how this
    #: project has been caught before.
    #:
    #: Turning it on changes what the account does. Measured on the demo
    #: account: 44.2% utilisation on 2026-09-03 and 66.6% on 2026-09-01, so a
    #: ceiling of 0.50 would have refused opens on the second of those. The
    #: gate fails CLOSED -- an unreadable headroom or a missing margin factor
    #: refuses rather than proceeds -- because a margin guard that disables
    #: itself reads as protection while providing none.
    max_margin_usage: float | None = None
    #: Notional per unit of signal size, as a multiple of equity.
    exposure_per_unit_size: float = 1.0
    #: Days either side of a reporting date to hold no position. ``0`` off.
    earnings_blackout_days: int = 0

    # -- volume ------------------------------------------------------------- #
    #: Shrink a position when volume is unusually heavy. Volume against the
    #: next day's range measured r=+0.177 at t=+39.03 on shares and +24.06
    #: on indices, monotone across every decile. It is the strongest signal
    #: measured in this project and the only risk signal that works on
    #: indices, which file nothing with any regulator.
    use_volume: bool = False
    #: Multiples of the instrument's own 20-day median that count as heavy.
    volume_heavy_ratio: float = 1.5
    #: What a heavy session shrinks the next position to.
    volume_heavy_multiplier: float = 0.6

    # -- currency ----------------------------------------------------------- #
    #: Whether foreign instruments are converted at all. Off, every rate
    #: resolves to 1.0 and a $150 share is marked at £150.
    fx_enabled: bool = True
    account_currency: str = "GBP"
    fx_conversion_fee_pct: float = 0.005

    # -- what the advisor will suggest -------------------------------------- #
    #: Whether the environment multiplier is folded into the confidence
    #: score. ``True`` is the recorded behaviour and makes any bar above
    #: 0.5 unreachable in high volatility -- roughly one day in three --
    #: because the multiplier halves the score. ``False`` leaves the
    #: environment to do its real job of scaling size, and lets a
    #: confidence threshold mean what it says.
    environment_in_confidence: bool = True

    #: Take no signal action on the first bar of a session. The decision is
    #: formed on the last bar that closed -- yesterday evening's, at the open
    #: -- while the fill happens at a price holding the whole overnight move.
    #: Entries on that bar won 44.2% against 51.9% everywhere else across 208
    #: trips (t=-2.26, p=0.025). See ``signals.SignalConfig``.
    hold_across_session_open: bool = False

    #: Confidence a signal must reach to be offered as a trade. Note that
    #: the environment multiplier is folded into confidence, so a bar above
    #: 0.5 is unreachable whenever volatility is high -- roughly one day in
    #: three.
    suggestion_confidence: float = 0.60

    #: What a held position is scaled to once its confidence falls below
    #: ``entry_confidence``. ``None`` disables it.
    #:
    #: The hysteresis band leaves an interior region unmanaged: entry needs
    #: 0.35 and exit triggers at 0.20, so a position whose conviction decays
    #: to 0.30 is too weak to open, too strong to close, and was held at
    #: full size. This halves it and nothing else -- it closes nothing, and
    #: the exit band still decides when the position goes.
    #:
    #: Measured by sweeping the cut point over 3.65 years, every arm holding
    #: genuinely less rather than normalised: 77.4% at -29.4% against a
    #: baseline of 74.4% at -34.4%, a ratio of 2.64 against 2.16. Cutting at
    #: 0.30 is inert (the exit band has already taken those), and above the
    #: entry bar it collapses -- 65.5% at 0.40, 41.7% at 0.50.
    #:
    #: The mirror is what makes it believable: cutting the *top* by the same
    #: amount returns 35.6%, the worst arm of the sweep and 41.8 points
    #: below cutting the bottom. So it is the direction that pays rather
    #: than the reduction.
    #:
    #: The threshold is ``entry_confidence`` itself and deliberately not a
    #: number of its own -- the swept peak landed on it, which is what the
    #: rule means. One owner, and it follows any change to the entry bar.
    decayed_position_multiplier: float | None = 0.5

    #: Session bars of range a move must clear before it is entered, where a
    #: trend is running. ``None`` disables the gate.
    #:
    #: A breakout in a trending market confirms the move is extending. In a
    #: sideways market the same event is the textbook false breakout and
    #: reverts, so the requirement is added where there is something to
    #: confirm and left off elsewhere -- applied only in sideways markets it
    #: returns 73.0% against 74.4% for no filter at all.
    #:
    #: Confirmation is required where a move STARTS, not on every re-entry
    #: into it. With a 0.29-day time stop positions close constantly while
    #: signals persist, and re-gating each re-entry demands evidence for a
    #: move already confirmed: 63.0% against 78.1%. That distinction cost a
    #: day, because the first implementation had it the wrong way round.
    #:
    #: Measured through the engine, against a baseline already including the
    #: decayed-position trim: 79.5 / 80.5 / 80.2 / 80.3 percent at 6, 12, 24
    #: and 48 bars against 73.6 for no gate, drawdown -26.1 against -30.0.
    #: Twelve is the middle of a plateau rather than a peak.
    breakout_lookback: int | None = 12

    #: Charge the spread the broker quoted, rather than a flat constant.
    #:
    #: Capital's bars carry a bid and an ask, and preprocessing already keeps
    #: the resulting ``spread_bp`` panel beside the prices -- so the measured
    #: figure was present at execution time and was not being read. Measured
    #: over Capital's full history on the eight-name book: median 11.32bp
    #: against the 5.0bp constant, 2.26x. Only TSLA is cheaper than the
    #: assumption; INTC runs 28.75bp, 5.75x.
    #:
    #: It matters disproportionately here because the median position lives
    #: about four hours. At that horizon spread is nearly the whole cost and
    #: financing barely registers, so undercharging it by half understates
    #: the cost of the strategy rather than a corner of it.
    use_measured_spreads: bool = True


@dataclass(frozen=True, slots=True)
class UniverseConfig:
    """The pool the screen chooses from, and the map it caps sectors by.

    The pool is the screen's one remaining judgement call, and the point of
    putting it here is that it stays *visible*. Selecting mechanically from
    a hand-drawn list moves the bias rather than removing it; what it buys
    is that the selection within the pool is reproducible and the pool is
    one list somebody can argue with, rather than nineteen decisions nobody
    wrote down.

    It lived in a scratch script until now, which meant every screen this
    project has run was reproducible only by whoever still had the file --
    the same defect the screen exists to fix, one level up.

    ``candidates`` excludes nothing by itself: an instrument here is one the
    screen may rank, not one it will hold. The traded universe stays
    ``market.tickers`` and is only ever changed by a person.
    """

    #: Instruments the screen may rank. Empty means "the traded universe",
    #: which makes a screen with no pool a re-ranking of what is held rather
    #: than an error.
    candidates: tuple[str, ...] = ()

    #: Ticker to sector, for the concentration cap. Absent, the cap cannot
    #: bind and the screen says so rather than silently not applying it.
    sectors: dict[str, str] = field(default_factory=dict)

    #: Whether the daemon trades the screen's latest recorded selection
    #: instead of ``market.tickers``. **Off by default, and off is the state
    #: the project ran in until it was measured.**
    #:
    #: The screen has ranked the pool monthly and recorded the result for
    #: some time; what it could not do was act on it. That was deliberate --
    #: a scheduled job rebalancing on a ranking never shown to predict
    #: return would be claiming more than the evidence carries. It still
    #: does not predict return: rank inside the selected list is worth
    #: nothing, measured at £476,774 for ranks 1-4 against £484,668 for
    #: ranks 5-8 with no trend between them. What the screen is good at is
    #: deciding *membership*, and membership is all this setting hands it.
    #:
    #: The traded list is still never rewritten. ``market.tickers`` stays
    #: exactly as a person left it and is the fallback; the selection is
    #: read from the append-only record the screen already keeps. Nothing
    #: here mutates configuration, which is the constraint that shaped the
    #: whole design.
    #:
    #: Turning this on does not let anything trade. Execution stays gated on
    #: its own environment variables, and a departing name still runs out
    #: rather than being closed -- see ``schedule.app._with_held``.
    follow_screen: bool = False

    #: How many instruments a screen selects. **An override.** Left unset,
    #: the number is *computed* from ``size_curve`` and the account balance
    #: -- see ``risk.capacity.operating_size``. Set it only to pin the
    #: answer deliberately.
    target: int | None = None
    #: The most names to hold, whatever the return curve says.
    #:
    #: The curve peaks at 70 and is still worth 209.7% at 100, a difference
    #: inside the 9.3pp noise floor -- so "largest within the floor" would
    #: pick the whole candidate pool. This is the deliberate stop.
    #:
    #: Distinct from the VIABILITY ceiling, which is computed from the
    #: balance and the broker's minimum deal sizes. In principle that one
    #: binds first on a small account; measured on 2026-09-04 it does not,
    #: and this is what stops 70 being shipped.
    #:
    #: ``position_capacity`` is optimistic in two ways that compound. It
    #: sizes against ``max_leverage`` -- 3.5x, where the book actually runs
    #: 2.05x -- and it takes the 90th-percentile minimum ticket, 50, rather
    #: than the dearest, 137. Across the 54 instruments with recorded sizes:
    #:
    #:     at 3.50x   83 slots by the p90 ticket, 30 by the dearest
    #:     at 2.05x   48 slots by the p90 ticket, 17 by the dearest
    #:
    #: At 70 names the notional per slot is 239 at 3.5x and **140 at the
    #: leverage actually run** -- against a dearest ticket of 137, so the
    #: expensive names would sit at one times their minimum where the model
    #: requires four. The honest range is 17 to 48 depending on which
    #: assumption is taken, and 25 sits in the middle of it.
    #:
    #: Raise this as the balance grows, or better, teach
    #: ``position_capacity`` to size against realised leverage and the
    #: dearest ticket rather than the ninetieth percentile -- at which point
    #: the viability ceiling binds on its own and this constant can go.
    max_target: int | None = 25

    #: Measured CAGR against universe size, from which the traded count is
    #: derived. Data rather than configuration: it is the output of a sweep
    #: and is replaced when the sweep is re-run, not tuned by hand.
    #:
    #: Measured 2026-08-30 over 3.65 years, daily screening, per-bar
    #: spreads, run-out semantics. There is no optimum in it -- every year
    #: wants a different size -- so it is read through the noise floor
    #: rather than by taking its maximum.
    size_curve: dict[int, float] = field(default_factory=dict)

    #: Which end of the statistically-indistinguishable band to take.
    #: ``largest`` is the owner's stated objective (maximum growth);
    #: ``smallest`` takes the least risk for the same measured return.
    size_preference: str = "largest"

    #: Most instruments from any one sector. ``None`` leaves the screen's own
    #: default in force -- one owner, and this is an override rather than a
    #: second copy.
    #:
    #: Worth knowing that this only started mattering when a sector map was
    #: written. Until then every screen reported the cap as not applied, so
    #: turning the map on changed the answer by more than the map itself: it
    #: activated a limit that had been inert since the screen was built.
    max_per_sector: int | None = None


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Root configuration object threaded through the engine."""

    paths: PathConfig = field(default_factory=PathConfig)
    market: MarketDataConfig = field(default_factory=MarketDataConfig)
    macro: MacroDataConfig = field(default_factory=MacroDataConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    universe: UniverseConfig = field(default_factory=UniverseConfig)


# --------------------------------------------------------------------------- #
# Loading helpers (pure where possible)
# --------------------------------------------------------------------------- #
def _coerce(value: Any, target_type: Any) -> Any:
    """Coerce a raw TOML/env value to the annotated dataclass field type.

    Handles the small set of types this config uses -- ``Path``, ``bool``,
    ``int``, ``float``, ``str`` and ``tuple[str, ...]`` -- plus their
    ``| None`` variants. Comma-separated strings become tuples, so
    ``CFD_TICKERS=SPY,QQQ`` works from the shell.
    """
    type_name = str(target_type)

    if value is None:
        return None
    if "dict" in type_name:
        # Nested mapping (e.g. series_overrides); copied, never stringified.
        return {str(k): v for k, v in dict(value).items()}
    if "Path" in type_name:
        return Path(value)
    if "tuple" in type_name:
        if isinstance(value, str):
            value = [part.strip() for part in value.split(",") if part.strip()]
        return tuple(str(item) for item in value)
    if "bool" in type_name:
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)
    if "int" in type_name and "float" not in type_name:
        return int(value)
    if "float" in type_name:
        return float(value)
    return str(value)


def _apply_mapping(config_obj: Any, updates: Mapping[str, Any]) -> Any:
    """Return a copy of a frozen dataclass with ``updates`` applied.

    Pure: the input object is never mutated. Unknown keys are logged and
    skipped so a stale config file cannot crash the engine.
    """
    known = {f.name: f for f in fields(config_obj)}
    changes: dict[str, Any] = {}

    for key, value in updates.items():
        spec = known.get(key)
        if spec is None:
            logger.warning(
                "Ignoring unknown config key %r for %s",
                key,
                type(config_obj).__name__,
            )
            continue
        current = getattr(config_obj, key)
        if is_dataclass(current) and isinstance(value, Mapping):
            changes[key] = _apply_mapping(current, value)
        else:
            changes[key] = _coerce(value, spec.type)

    return replace(config_obj, **changes) if changes else config_obj


def load_dotenv(
    path: str | os.PathLike[str] = ".env", *, override: bool = False
) -> dict[str, str]:
    """Load ``KEY=VALUE`` pairs from a ``.env`` file into ``os.environ``.

    A dependency-free stand-in for ``python-dotenv``. Blank lines, ``#``
    comments, a leading ``export`` and surrounding quotes are tolerated.

    Parameters
    ----------
    path:
        File to read. A missing file is a no-op.
    override:
        When ``False`` (default) existing environment variables win.

    Returns
    -------
    dict
        The key/value pairs parsed from the file.
    """
    env_path = Path(path)
    try:
        if not env_path.is_file():
            return {}
        body = env_path.read_text(encoding="utf-8")
    except OSError as error:
        # A .env that exists but cannot be read is the same as none: the
        # environment and the config file still have everything needed.
        # `is_file()` *raises* on permission denied rather than returning
        # False, so a service running as one user in a directory holding
        # another user's .env crashed on import rather than falling back.
        logger.warning("Ignoring unreadable %s: %s", env_path, error)
        return {}

    parsed: dict[str, str] = {}
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        key = key.strip()
        value = value.strip().strip("\"'")
        parsed[key] = value
        if override or key not in os.environ:
            os.environ[key] = value
    return parsed


#: Recognised ``CFD_*`` environment variables mapped to ``(section, field)``.
ENV_OVERRIDE_MAP: dict[str, tuple[str, str]] = {
    "CFD_DATA_ROOT": ("paths", "data_root"),
    "CFD_TICKERS": ("market", "tickers"),
    "CFD_INTERVAL": ("market", "interval"),
    "CFD_START": ("market", "start"),
    "CFD_END": ("market", "end"),
    "CFD_FRED_SERIES": ("macro", "series_ids"),
    "CFD_FRED_OBSERVATION_START": ("macro", "observation_start"),
    "CFD_LOG_LEVEL": ("logging", "level"),
    "CFD_LOG_FILE": ("logging", "log_file"),
    "CFD_PARQUET_COMPRESSION": ("storage", "compression"),
    "CFD_REGIME_MODE": ("strategy", "regime_mode"),
    "CFD_TIME_STOP_DAYS": ("strategy", "time_stop_days"),
    "CFD_REENTRY_COOLDOWN_DAYS": ("strategy", "reentry_cooldown_days"),
    "CFD_EXPOSURE_PER_UNIT_SIZE": ("strategy", "exposure_per_unit_size"),
    "CFD_SUGGESTION_CONFIDENCE": ("strategy", "suggestion_confidence"),
}


def env_overrides(env: Mapping[str, str]) -> dict[str, Any]:
    """Translate ``CFD_*`` environment variables into a nested update mapping.

    Pure -- reads only from the supplied mapping, which makes it trivial to
    unit-test without touching the real process environment.
    """
    updates: dict[str, Any] = {}
    for env_name, (section, key) in ENV_OVERRIDE_MAP.items():
        if env.get(env_name):
            updates.setdefault(section, {})[key] = env[env_name]

    # The API key is deliberately env-only, never persisted to TOML.
    api_key = env.get("FRED_API_KEY") or env.get("CFD_FRED_API_KEY")
    if api_key:
        updates.setdefault("macro", {})["api_key"] = api_key

    return updates


def load_toml(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Parse a TOML config file, returning ``{}`` when it does not exist."""
    config_path = Path(path)
    if not config_path.is_file():
        logger.debug("No config file at %s; using defaults", config_path)
        return {}
    with config_path.open("rb") as handle:
        return tomllib.load(handle)


def load_config(
    config_file: str | os.PathLike[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    dotenv_path: str | os.PathLike[str] | None = ".env",
    overrides: Mapping[str, Any] | None = None,
) -> AppConfig:
    """Build an :class:`AppConfig` from defaults, a TOML file and the env.

    Parameters
    ----------
    config_file:
        TOML path. Defaults to ``$CFD_CONFIG_FILE`` then ``config/config.toml``.
    env:
        Environment mapping to read overrides from. Defaults to ``os.environ``;
        pass an explicit dict in tests to keep the call deterministic.
    dotenv_path:
        ``.env`` file used to seed the process environment. Pass ``None`` to
        skip, which is what tests should do.
    overrides:
        Nested mapping applied last, e.g. ``{"market": {"interval": "1h"}}``.

    Returns
    -------
    AppConfig
        A fully resolved, immutable configuration object.
    """
    if dotenv_path is not None:
        load_dotenv(dotenv_path)

    environment = os.environ if env is None else env
    resolved_file = (
        config_file
        if config_file is not None
        else environment.get(ENV_CONFIG_FILE, "config/config.toml")
    )

    config = AppConfig()
    config = _apply_mapping(config, load_toml(resolved_file))
    config = _apply_mapping(config, env_overrides(environment))
    if overrides:
        config = _apply_mapping(config, overrides)
    return config
