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
    time_stop_days: int | None = None
    #: Days an instrument is left alone after a position in it closes.
    reentry_cooldown_days: int = 0
    #: Relative drift before an open position is resized.
    rebalance_threshold: float = 0.25
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
    #: Confidence a signal must reach to be offered as a trade. Note that
    #: the environment multiplier is folded into confidence, so a bar above
    #: 0.5 is unreachable whenever volatility is high -- roughly one day in
    #: three.
    suggestion_confidence: float = 0.60


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
