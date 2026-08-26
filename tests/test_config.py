"""Tests for configuration resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from cfdinvest.config import (
    AppConfig,
    _apply_mapping,
    _coerce,
    env_overrides,
    load_config,
    load_dotenv,
)


def test_defaults_are_self_consistent():
    config = AppConfig()
    assert config.paths.raw_market_dir == Path("data/raw/market")
    assert config.paths.raw_macro_dir == Path("data/raw/macro")
    assert config.market.interval == "1d"


@pytest.mark.parametrize(
    ("value", "annotation", "expected"),
    [
        ("SPY, QQQ ,GLD", "tuple[str, ...]", ("SPY", "QQQ", "GLD")),
        (["A", "B"], "tuple[str, ...]", ("A", "B")),
        ("true", "bool", True),
        ("off", "bool", False),
        ("42", "int", 42),
        ("1.5", "float", 1.5),
        ("lake", "Path", Path("lake")),
        (None, "str | None", None),
    ],
)
def test_coerce(value, annotation, expected):
    assert _coerce(value, annotation) == expected


def test_env_overrides_are_pure():
    updates = env_overrides(
        {"CFD_TICKERS": "SPY,QQQ", "FRED_API_KEY": "abc", "CFD_LOG_LEVEL": "DEBUG"}
    )
    assert updates == {
        "market": {"tickers": "SPY,QQQ"},
        "macro": {"api_key": "abc"},
        "logging": {"level": "DEBUG"},
    }


def test_env_ignores_blank_values():
    assert env_overrides({"CFD_TICKERS": ""}) == {}


def test_load_config_layers_toml_then_env(tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[market]\ntickers = ["AAA"]\ninterval = "1h"\n'
        '[paths]\ndata_root = "from_toml"\n',
        encoding="utf-8",
    )

    config = load_config(
        config_file,
        env={"CFD_DATA_ROOT": "from_env", "FRED_API_KEY": "key123"},
        dotenv_path=None,
    )

    assert config.market.interval == "1h"          # from TOML
    assert config.market.tickers == ("AAA",)       # from TOML
    assert config.paths.data_root == Path("from_env")  # env beats TOML
    assert config.macro.api_key == "key123"


def test_explicit_overrides_win_over_env(tmp_path):
    config = load_config(
        tmp_path / "missing.toml",
        env={"CFD_INTERVAL": "1h"},
        dotenv_path=None,
        overrides={"market": {"interval": "1wk"}},
    )
    assert config.market.interval == "1wk"


def test_unknown_keys_are_ignored(caplog):
    config = _apply_mapping(AppConfig(), {"nonsense": 1})
    assert config == AppConfig()
    assert "unknown config key" in caplog.text


def test_config_objects_are_immutable():
    config = AppConfig()
    with pytest.raises(Exception):
        config.market.interval = "1h"  # type: ignore[misc]


def test_load_dotenv_parses_quotes_comments_and_export(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        '# a comment\n\nexport FRED_API_KEY="quoted"\nCFD_INTERVAL=1h\nbad line\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    monkeypatch.delenv("CFD_INTERVAL", raising=False)

    parsed = load_dotenv(env_file)
    assert parsed == {"FRED_API_KEY": "quoted", "CFD_INTERVAL": "1h"}

    # load_dotenv writes into os.environ, so the values it set outlive the
    # delenv calls above and would leak into every later test in the session.
    for name in parsed:
        monkeypatch.delenv(name, raising=False)


def test_load_dotenv_missing_file_is_noop(tmp_path):
    assert load_dotenv(tmp_path / "nope.env") == {}


def test_ensure_creates_directory_tree(config):
    config.paths.ensure()
    for directory in config.paths.all_dirs():
        assert directory.is_dir()


def test_a_part_session_time_stop_survives_being_read():
    """M147: a fractional stop truncated to zero by its own annotation.

    The config loader coerces each value to the type its dataclass field is
    annotated with. ``time_stop_days`` was ``int``, so on hourly bars a stop
    of 0.29 days -- seven hours, one US session -- came back as 0 and the
    stop was silently disabled. Nothing raised; the strategy simply stopped
    having a time stop, which is exactly the setting measured to take the
    two-year return from 247% to 774%.
    """
    from cfdinvest.config import StrategyConfig, _coerce

    for value in (0.29, 0.125, 1.5):
        assert _coerce(value, StrategyConfig.__annotations__["time_stop_days"]) == value
        assert _coerce(
            value, StrategyConfig.__annotations__["reentry_cooldown_days"]
        ) == value

    # And a whole number still reads as one.
    assert _coerce(1, StrategyConfig.__annotations__["time_stop_days"]) == 1


def test_the_configured_stop_is_shorter_than_the_bar_interval_allows_to_be_useless():
    """The shipped configuration is internally consistent.

    A time stop measured in days only means something next to the bar it is
    applied to. On hourly bars a one-day stop keeps the carry -- 169 hours of
    it -- and gives up the extra decisions the smaller bar was adopted for.
    """
    from cfdinvest.config import load_config

    config = load_config()
    if config.market.interval != "1h":
        pytest.skip("only meaningful while the engine runs on hourly bars")

    assert config.strategy.time_stop_days is not None, "the stop must not be off"
    assert 0 < config.strategy.time_stop_days <= 1.0, (
        "a multi-day stop on hourly bars pays the carry it was meant to avoid"
    )
    # The cooldown must not lock out most of the universe most of the time.
    assert config.strategy.reentry_cooldown_days <= 3.0


def test_the_margin_ceiling_reaches_the_execution_engine():
    """M152: a limit that exists in code and is set by nobody.

    ``max_leverage`` bounds gross notional as a multiple of equity, and at
    Trading 212's 5x on shares that is the margin dial: 5.0 commits the
    whole balance, 2.5 commits half. It was never passed from the strategy
    config into ExecutionConfig, so the execution default of 5.0 applied and
    the book ran at full margin utilisation by omission -- no file said so,
    and the backtest reported 374% a year on positions the account could not
    have carried through an adverse move.

    One owner, and it must actually arrive.
    """
    from cfdinvest.config import StrategyConfig, load_config
    import cfdinvest.schedule.app as app

    config = load_config()
    execution = app._execution_config(list(config.market.tickers), config.strategy)
    assert execution.max_leverage == config.strategy.max_leverage, (
        "the configured ceiling does not reach the engine"
    )

    # A book at full margin has no room before the broker closes it.
    assert config.strategy.max_leverage <= 2.5, (
        f"{config.strategy.max_leverage / 5 * 100:.0f}% of the balance as margin"
    )

    # And an override travels rather than being ignored.
    strategy = StrategyConfig(max_leverage=1.5)
    assert app._execution_config(["AAPL"], strategy).max_leverage == 1.5
