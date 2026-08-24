"""Run-level metadata: what ran, when, over what, and what went wrong.

One document per run, written last so that its presence means the run got to
the end. It carries the things a later reader cannot recover from the frames:
the wall-clock timing, the universe as *requested* rather than as delivered,
the warnings, and a snapshot of the settings.

**The config snapshot is the point.** Without it the dashboard can only show
built-in defaults and say honestly that it does not know what the run used.
With it, a result from three months ago can be explained.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Sequence

import pandas as pd

__all__ = ["SCHEMA_VERSION", "RunMetadata", "build_run_metadata"]

SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class RunMetadata:
    """What one pipeline run was and how it went.

    Attributes
    ----------
    status:
        ``"ok"``, ``"failed"`` or ``"empty"`` — the same vocabulary the
        scheduler's run summary already uses, so the two agree.
    warnings:
        Anything worth knowing that did not stop the run. Kept here rather
        than only in a log, because a log is rotated and this is not.
    config_snapshot:
        Every setting, flattened to plain values. Recorded rather than
        reconstructed: a dataclass repr is not something a later comparison
        can index into.
    """

    run_id: str
    schema_version: int = SCHEMA_VERSION
    start_time: str | None = None
    end_time: str | None = None
    duration_seconds: float | None = None
    status: str = "ok"
    tickers: tuple[str, ...] = ()
    interval: str | None = None
    bars: int = 0
    as_of: str | None = None
    market_status: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    error: str | None = None
    artefacts: dict[str, str] = field(default_factory=dict)
    config_snapshot: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def ok(self) -> bool:
        return self.status == "ok" and self.error is None


def _iso(value: Any) -> str | None:
    """Any timestamp-like value as an ISO-8601 UTC string."""
    if value in (None, ""):
        return None
    try:
        stamp = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if stamp.tz is None:
        stamp = stamp.tz_localize("UTC")
    return stamp.tz_convert("UTC").isoformat()


def _snapshot(config) -> dict[str, Any]:
    """Flatten a nested config into ``{dotted.path: value}``.

    Reuses the dashboard's flattener, which already redacts anything whose
    name looks like a credential — this document is read by a web page, and a
    printed key is a key leaked to every proxy log in between.
    """
    if config is None:
        return {}
    try:
        from cfdinvest.ui.configview import flatten

        return {row["setting"]: row["value"] for row in flatten(config, None)}
    except Exception:  # noqa: BLE001 - metadata must not fail over a snapshot
        return {}


def build_run_metadata(
    run_id: str,
    *,
    config=None,
    started_at=None,
    finished_at=None,
    status: str = "ok",
    tickers: Sequence[str] | None = None,
    interval: str | None = None,
    bars: int = 0,
    as_of=None,
    market_status: dict[str, Any] | None = None,
    warnings: Sequence[str] = (),
    error: str | None = None,
    artefacts: dict[str, str] | None = None,
) -> RunMetadata:
    """Assemble a run's metadata. Pure.

    ``duration_seconds`` is computed from the two timestamps rather than
    taken as an argument, so it cannot disagree with them.
    """
    start, end = _iso(started_at), _iso(finished_at)
    duration = None
    if start and end:
        duration = float(
            (pd.Timestamp(end) - pd.Timestamp(start)).total_seconds()
        )

    return RunMetadata(
        run_id=str(run_id),
        start_time=start,
        end_time=end,
        duration_seconds=duration,
        status=status,
        tickers=tuple(tickers or ()),
        interval=interval,
        bars=int(bars),
        as_of=_iso(as_of),
        market_status=dict(market_status or {}),
        warnings=tuple(warnings),
        error=error,
        artefacts=dict(artefacts or {}),
        config_snapshot=_snapshot(config),
    )
