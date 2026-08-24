"""Parquet persistence primitives for the cfdinvest data lake.

This module owns *how* data is written to disk; the ingestion modules own
*what* is written. Keeping the two apart means a future switch to DuckDB,
a partitioned Arrow dataset or object storage touches only this file.

Layout
------
``<data_root>/raw/market/<interval>/<TICKER>.parquet``
``<data_root>/raw/macro/<SERIES_ID>.parquet``
``<data_root>/metadata/<name>.json``

One file per instrument is a deliberate trade-off: it keeps incremental
updates cheap (rewrite one small file, not a monolith), makes partial
failures isolated, and still reads fast because pyarrow can consume a list
of files as a single dataset.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Iterable, Sequence

import pandas as pd

from cfdinvest.config import PathConfig, StorageConfig

logger = logging.getLogger(__name__)

#: Characters that are illegal in Windows filenames, replaced on write.
_UNSAFE_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\s]+')


class StorageError(RuntimeError):
    """Raised when a read or write against the data lake fails."""


# --------------------------------------------------------------------------- #
# Path helpers (pure)
# --------------------------------------------------------------------------- #
def safe_filename(symbol: str) -> str:
    """Return a cross-platform-safe filename stem for a symbol.

    The original symbol is always preserved inside the file as a column, so
    this transformation never has to be inverted.

    >>> safe_filename("^GSPC")
    '^GSPC'
    >>> safe_filename("BRK/B")
    'BRK_B'
    """
    return _UNSAFE_FILENAME_CHARS.sub("_", symbol.strip())


def market_path(paths: PathConfig, ticker: str, interval: str) -> Path:
    """Return the Parquet path for one ticker at one bar interval."""
    return paths.raw_market_dir / interval / f"{safe_filename(ticker)}.parquet"


def macro_path(paths: PathConfig, series_id: str) -> Path:
    """Return the Parquet path for one FRED series."""
    return paths.raw_macro_dir / f"{safe_filename(series_id)}.parquet"


def metadata_path(paths: PathConfig, name: str) -> Path:
    """Return the JSON path for a metadata document."""
    return paths.metadata_dir / f"{safe_filename(name)}.json"


# --------------------------------------------------------------------------- #
# Frame merging (pure)
# --------------------------------------------------------------------------- #
def merge_frames(existing: pd.DataFrame, incoming: pd.DataFrame) -> pd.DataFrame:
    """Combine an on-disk frame with freshly downloaded rows.

    Pure function -- neither input is mutated. On duplicate index values the
    *incoming* row wins, which is what we want for vendor revisions of recent
    bars and for FRED restatements.

    Parameters
    ----------
    existing, incoming:
        Frames sharing an index name and column set. Either may be empty.

    Returns
    -------
    pandas.DataFrame
        Sorted, de-duplicated union of both frames.
    """
    if existing.empty:
        return incoming.sort_index()
    if incoming.empty:
        return existing.sort_index()

    combined = pd.concat([existing, incoming])
    # keep="last" -> incoming rows override existing ones at the same timestamp.
    deduped = combined[~combined.index.duplicated(keep="last")]
    return deduped.sort_index()


# --------------------------------------------------------------------------- #
# I/O
# --------------------------------------------------------------------------- #
def write_parquet(
    frame: pd.DataFrame,
    path: Path,
    config: StorageConfig | None = None,
) -> Path:
    """Write ``frame`` to ``path`` atomically.

    The frame is written to a sibling ``.tmp`` file and then moved into place,
    so a crash mid-write can never leave a truncated Parquet file that later
    reads would choke on.

    Returns
    -------
    pathlib.Path
        The path written.
    """
    settings = config or StorageConfig()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".parquet.tmp")

    try:
        frame.to_parquet(
            tmp_path,
            engine="pyarrow",
            compression=settings.compression,
            index=True,
            row_group_size=settings.row_group_size,
        )
        os.replace(tmp_path, path)
    except Exception as exc:  # noqa: BLE001 - re-raised as a domain error
        tmp_path.unlink(missing_ok=True)
        raise StorageError(f"Failed writing Parquet to {path}: {exc}") from exc

    logger.debug("Wrote %d rows -> %s", len(frame), path)
    return path


def read_parquet(
    path: Path,
    columns: Sequence[str] | None = None,
) -> pd.DataFrame:
    """Read a Parquet file, returning an empty frame when it does not exist.

    A missing file is a normal state (nothing ingested yet), not an error, so
    callers can treat "no data" and "no file" identically.
    """
    if not path.is_file():
        return pd.DataFrame()
    try:
        return pd.read_parquet(path, engine="pyarrow", columns=columns)
    except Exception as exc:  # noqa: BLE001
        raise StorageError(f"Failed reading Parquet from {path}: {exc}") from exc


def upsert_parquet(
    frame: pd.DataFrame,
    path: Path,
    config: StorageConfig | None = None,
) -> pd.DataFrame:
    """Merge ``frame`` into the file at ``path`` and persist the result.

    Returns the merged frame that was written, so callers can report row
    counts without re-reading from disk.
    """
    if frame.empty and path.is_file():
        logger.debug("Nothing new for %s; leaving file untouched", path.name)
        return read_parquet(path)

    merged = merge_frames(read_parquet(path), frame)
    write_parquet(merged, path, config)
    return merged


def read_many(
    paths: Iterable[Path],
    columns: Sequence[str] | None = None,
) -> pd.DataFrame:
    """Read and concatenate several Parquet files into one frame.

    Missing files are skipped with a warning rather than aborting the load,
    because a partially ingested lake should still be usable.
    """
    frames = []
    for path in paths:
        frame = read_parquet(path, columns=columns)
        if frame.empty:
            logger.warning("No data at %s; skipping", path)
            continue
        frames.append(frame)

    if not frames:
        return pd.DataFrame()
    return pd.concat(frames)


def write_json(payload: Any, path: Path) -> Path:
    """Write a JSON document atomically (used for metadata and provenance)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    try:
        tmp_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True, default=str),
            encoding="utf-8",
        )
        os.replace(tmp_path, path)
    except Exception as exc:  # noqa: BLE001
        tmp_path.unlink(missing_ok=True)
        raise StorageError(f"Failed writing JSON to {path}: {exc}") from exc
    return path


def read_json(path: Path, default: Any = None) -> Any:
    """Read a JSON document, returning ``default`` when the file is absent."""
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise StorageError(f"Failed reading JSON from {path}: {exc}") from exc


def last_timestamp(path: Path) -> pd.Timestamp | None:
    """Return the newest timestamp stored in a Parquet file, or ``None``.

    Reads the index only (``columns=[]``), so it stays cheap even on large
    files. This is what drives incremental downloads.
    """
    if not path.is_file():
        return None
    try:
        index = pd.read_parquet(path, engine="pyarrow", columns=[]).index
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not read index of %s (%s); refetching in full", path, exc)
        return None
    if len(index) == 0:
        return None
    return pd.Timestamp(index.max())
