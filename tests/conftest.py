"""Shared fixtures. Every test runs fully offline against a temp data lake."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from cfdinvest.config import AppConfig, MacroDataConfig, MarketDataConfig, PathConfig


@pytest.fixture
def config(tmp_path) -> AppConfig:
    """An AppConfig rooted at a throwaway directory."""
    return AppConfig(
        paths=PathConfig(data_root=tmp_path / "lake"),
        market=MarketDataConfig(
            tickers=("AAA", "BBB"),
            start="2020-01-01",
            batch_size=2,
            refetch_overlap_days=2,
        ),
        macro=MacroDataConfig(
            series_ids=("DGS10",),
            api_key="test-key",
            observation_start="2020-01-01",
            min_request_interval=0.0,
            # Pinned: these fixtures predate publication_lag becoming the
            # default and exercise unrelated behaviour. Vintage-specific
            # tests build their own config.
            vintage_mode="latest",
        ),
    )


@pytest.fixture(autouse=True)
def _isolated_broker_rules(monkeypatch, tmp_path):
    """Point the Trading 212 rules table at a path that does not exist.

    Not a convenience. ``DEFAULT_RULES_PATH`` is under ``~/.config``, so
    without this the suite reads whatever the developer happens to have
    filled in -- and two tests asserting the "not checked" wording passed
    for years only because the file was absent. They failed the moment a
    real table was installed, on a machine where nothing about the code had
    changed. The deployment box has one, so the suite was green here and
    would not have been there.

    A test that wants a table builds one and passes it explicitly.
    """
    monkeypatch.setattr(
        "cfdinvest.livehelper.t212_rules.DEFAULT_RULES_PATH",
        str(tmp_path / "no-such-rules.toml"),
    )


@pytest.fixture
def sessions() -> pd.DatetimeIndex:
    """Ten tz-naive business days, as yfinance returns for daily bars."""
    return pd.bdate_range("2020-01-01", periods=10, name="Date")


def make_yf_frame(sessions: pd.DatetimeIndex, base: float = 100.0) -> pd.DataFrame:
    """Build a single-ticker frame shaped like raw yfinance output."""
    steps = np.arange(len(sessions), dtype="float64")
    close = base + steps
    return pd.DataFrame(
        {
            "Open": close - 0.5,
            "High": close + 1.0,
            "Low": close - 1.0,
            "Close": close,
            "Volume": np.full(len(sessions), 1_000_000.0),
            "Dividends": np.zeros(len(sessions)),
            "Stock Splits": np.zeros(len(sessions)),
        },
        index=sessions,
    )


@pytest.fixture
def yf_payload_factory(sessions):
    """Return a factory producing MultiIndex payloads like ``yfinance.download``."""

    def _factory(tickers, level_order: str = "ticker_first") -> pd.DataFrame:
        frames = {t: make_yf_frame(sessions, 100.0 + 10 * i) for i, t in enumerate(tickers)}
        combined = pd.concat(frames, axis=1)  # level 0 = ticker, level 1 = field
        if level_order == "field_first":
            combined.columns = combined.columns.swaplevel(0, 1)
            combined = combined.sort_index(axis=1)
        return combined

    return _factory
