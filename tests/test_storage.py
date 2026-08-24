"""Tests for the Parquet persistence layer."""

from __future__ import annotations

import pandas as pd
import pytest

from cfdinvest import storage
from cfdinvest.schema import TIMESTAMP


def _frame(start: str, periods: int, value: float) -> pd.DataFrame:
    index = pd.date_range(start, periods=periods, freq="D", tz="UTC", name=TIMESTAMP)
    return pd.DataFrame({"close": [value] * periods}, index=index)


def test_safe_filename_replaces_illegal_characters():
    assert storage.safe_filename("^GSPC") == "^GSPC"
    assert storage.safe_filename("BRK/B") == "BRK_B"
    assert storage.safe_filename("A B") == "A_B"


def test_paths_are_derived_from_config(config):
    market = storage.market_path(config.paths, "SPY", "1d")
    assert market.parent.name == "1d"
    assert market.name == "SPY.parquet"
    assert storage.macro_path(config.paths, "DGS10").name == "DGS10.parquet"


def test_merge_frames_prefers_incoming_on_overlap():
    existing = _frame("2020-01-01", 3, 1.0)
    incoming = _frame("2020-01-03", 3, 2.0)

    merged = storage.merge_frames(existing, incoming)

    assert len(merged) == 5
    assert merged.index.is_monotonic_increasing
    # 2020-01-03 overlaps: the incoming value must win (vendor revision).
    assert merged.loc["2020-01-03", "close"] == 2.0
    assert merged.loc["2020-01-01", "close"] == 1.0


def test_merge_frames_does_not_mutate_inputs():
    existing = _frame("2020-01-01", 3, 1.0)
    incoming = _frame("2020-01-03", 3, 2.0)
    before = existing.copy()

    storage.merge_frames(existing, incoming)

    pd.testing.assert_frame_equal(existing, before)


def test_merge_frames_handles_empty_sides():
    populated = _frame("2020-01-01", 2, 1.0)
    assert storage.merge_frames(pd.DataFrame(), populated).equals(populated)
    assert storage.merge_frames(populated, pd.DataFrame()).equals(populated)


def test_write_then_read_roundtrip(config):
    path = storage.market_path(config.paths, "AAA", "1d")
    frame = _frame("2020-01-01", 4, 7.0)

    storage.write_parquet(frame, path, config.storage)
    restored = storage.read_parquet(path)

    # check_freq=False: Parquet does not round-trip a DatetimeIndex freq.
    pd.testing.assert_frame_equal(restored, frame, check_freq=False)


def test_read_missing_file_returns_empty(config):
    assert storage.read_parquet(config.paths.raw_macro_dir / "nope.parquet").empty


def test_write_leaves_no_temp_file_behind(config):
    path = storage.market_path(config.paths, "AAA", "1d")
    storage.write_parquet(_frame("2020-01-01", 2, 1.0), path, config.storage)
    assert not path.with_suffix(".parquet.tmp").exists()


def test_upsert_appends_only_new_rows(config):
    path = storage.market_path(config.paths, "AAA", "1d")

    storage.upsert_parquet(_frame("2020-01-01", 3, 1.0), path, config.storage)
    merged = storage.upsert_parquet(_frame("2020-01-03", 3, 2.0), path, config.storage)

    assert len(merged) == 5
    assert storage.read_parquet(path).equals(merged)


def test_last_timestamp(config):
    path = storage.market_path(config.paths, "AAA", "1d")
    assert storage.last_timestamp(path) is None

    storage.write_parquet(_frame("2020-01-01", 3, 1.0), path, config.storage)
    assert storage.last_timestamp(path) == pd.Timestamp("2020-01-03", tz="UTC")


def test_json_roundtrip(config):
    path = storage.metadata_path(config.paths, "manifest")
    storage.write_json({"b": 2, "a": 1}, path)
    assert storage.read_json(path) == {"a": 1, "b": 2}
    assert storage.read_json(path.with_name("missing.json"), default={}) == {}


def test_read_many_skips_missing(config, caplog):
    good = storage.market_path(config.paths, "AAA", "1d")
    storage.write_parquet(_frame("2020-01-01", 2, 1.0), good, config.storage)

    combined = storage.read_many([good, config.paths.raw_market_dir / "1d" / "ZZZ.parquet"])

    assert len(combined) == 2
    assert "No data at" in caplog.text


def test_storage_error_on_unwritable_path(config, monkeypatch):
    def explode(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(pd.DataFrame, "to_parquet", explode)
    with pytest.raises(storage.StorageError):
        storage.write_parquet(
            _frame("2020-01-01", 1, 1.0),
            storage.market_path(config.paths, "AAA", "1d"),
            config.storage,
        )
