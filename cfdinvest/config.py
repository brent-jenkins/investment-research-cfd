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
    #: ``"latest"``, ``"vintage"`` or ``"initial_release"``. Leaving this
    #: ``None`` derives the mode from ``realtime_start``/``realtime_end``, so
    #: existing configs keep their current behaviour. Set
    #: ``"initial_release"`` to fetch each observation's *first print* along
    #: with its true publication date -- the point-in-time correct choice.
    #: See :func:`cfdinvest.ingestion.macro.resolve_vintage_mode`.
    vintage_mode: str | None = None
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
class AppConfig:
    """Root configuration object threaded through the engine."""

    paths: PathConfig = field(default_factory=PathConfig)
    market: MarketDataConfig = field(default_factory=MarketDataConfig)
    macro: MacroDataConfig = field(default_factory=MacroDataConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)


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
    if not env_path.is_file():
        return {}

    parsed: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
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
