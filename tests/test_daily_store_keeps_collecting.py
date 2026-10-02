"""A daily series is not stopped by leaving a list.

Found 2026-10-01: the daily store's indices, commodities and three
currency pairs had stopped on 2026-08-26, because the daily top-up fetched
only the traded names, the conversion pairs and the screen's candidates.
The never-dropped rule covered the intraday store and not this one.

Mutations checked:

  M1980 the daily top-up no longer adds what the store holds
  M1981 the store's currency pairs left out
  M1982 a deliberately ended series fetched anyway
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from cfdinvest.schedule import app


def _config(root):
    return SimpleNamespace(
        paths=SimpleNamespace(data_root=root),
        market=SimpleNamespace(interval="capital_1h"),
        universe=SimpleNamespace(candidates=("AAA",)),
    )


def _store(root, interval, names):
    folder = root / "raw" / "market" / interval
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        (folder / f"{name}.parquet").write_bytes(b"")


def test_everything_the_daily_store_holds_is_topped_up(tmp_path):
    """M1980, M1981."""
    _store(tmp_path, "1d", ["^GSPC", "GC=F", "EURUSD=X", "AAA"])
    wanted = app._with_screen_pool(["TRADED"], _config(tmp_path))
    for name in ("TRADED", "AAA", "^GSPC", "GC=F", "EURUSD=X"):
        assert name in wanted, f"{name} would stop being collected"


def test_a_series_marked_ended_is_not_fetched(tmp_path):
    """M1982: the one deliberate exception."""
    _store(tmp_path, "1d", ["^GSPC", "OLD"])
    (tmp_path / "metadata").mkdir()
    (tmp_path / "metadata" / "delisted.json").write_text(json.dumps(["OLD"]))
    wanted = app._with_screen_pool([], _config(tmp_path))
    assert "^GSPC" in wanted and "OLD" not in wanted


def test_the_intraday_reading_is_unchanged(tmp_path):
    """The decision feed still leaves its pairs to the conversion list."""
    _store(tmp_path, "capital_1h", ["MU", "GBPUSD=X"])
    assert app._already_ingested(_config(tmp_path)) == ["MU"]
