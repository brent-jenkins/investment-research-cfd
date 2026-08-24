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


def test_load_dotenv_missing_file_is_noop(tmp_path):
    assert load_dotenv(tmp_path / "nope.env") == {}


def test_ensure_creates_directory_tree(config):
    config.paths.ensure()
    for directory in config.paths.all_dirs():
        assert directory.is_dir()
