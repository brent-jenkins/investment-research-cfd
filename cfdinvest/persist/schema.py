"""What a run directory contains, and what each artefact must carry.

The registry below is the contract between the runner that writes and the
dashboard and analytics that read. It is data rather than prose so that both
sides can be checked against it, and so a missing column fails at the write
rather than as a ``KeyError`` in a web request three days later.

**Versioned, and the version travels with the artefact.** Every JSON document
carries ``schema_version``; every Parquet artefact has its version recorded
in the run's ``metadata.json``. A reader can then tell "written by an older
version" — which is recoverable — from "corrupt", which is not.

**Required columns are a floor, not a fence.** An artefact with extra columns
is fine and is passed through untouched; one missing a required column is
refused. The pipeline grows columns (stops added seven, currency added five)
and a schema that rejected unknown ones would make every such addition a
breaking change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import pandas as pd

__all__ = [
    "SCHEMA_VERSION",
    "ARTEFACTS",
    "ArtefactSpec",
    "spec_for",
    "validate_frame",
    "missing_columns",
    "describe_run",
]

#: Bumped when any artefact's shape changes incompatibly.
SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class ArtefactSpec:
    """One file in a run directory.

    Attributes
    ----------
    required:
        Columns a reader may rely on. Checked at write time.
    optional:
        Columns that appear once a feature is enabled — stop levels, currency
        detail. Documented so their absence reads as "that feature was off"
        rather than "something went wrong".
    index:
        What the frame is indexed by, for the ones where it matters.
    """

    name: str
    filename: str
    kind: str
    description: str
    required: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()
    index: str | None = None
    version: int = SCHEMA_VERSION

    @property
    def is_frame(self) -> bool:
        return self.kind == "parquet"


#: Everything a completed run may contain. The first five already exist and
#: are written by the scheduler; the rest are what this layer adds.
ARTEFACTS: dict[str, ArtefactSpec] = {
    # -- written by the scheduler today ---------------------------------- #
    "equity": ArtefactSpec(
        name="equity",
        filename="equity.parquet",
        kind="parquet",
        description="Mark-to-market account value, one row per bar.",
        required=("equity",),
        index="timestamp",
    ),
    "trades": ArtefactSpec(
        name="trades",
        filename="trades.parquet",
        kind="parquet",
        description="One row per executed change in position.",
        required=(
            "timestamp", "asset", "action", "units", "traded_units", "price",
            "fill_price", "spread_cost", "slippage", "financing",
            "realized_pnl", "notional", "equity", "reason",
        ),
        optional=(
            "stop_level", "take_profit_level", "stop_fill_price",
            "take_profit_fill_price", "gap_through", "slippage_applied",
            "breach_type", "fx_rate", "fx_spread", "fx_conversion_fee",
            "fx_slippage", "fx_adjusted_fill_price",
        ),
    ),
    "positions": ArtefactSpec(
        name="positions",
        filename="positions.parquet",
        kind="parquet",
        description="Signed units held, timestamp by ticker.",
        index="timestamp",
    ),
    "signals": ArtefactSpec(
        name="signals",
        filename="signals.parquet",
        kind="parquet",
        description="Active signals in long form. Flat cells are not stored.",
        required=("direction", "size", "confidence", "reason"),
    ),
    "summary": ArtefactSpec(
        name="summary",
        filename="summary.json",
        kind="json",
        description="The run summary the scheduler writes.",
    ),
    # -- added by this layer --------------------------------------------- #
    "pnl": ArtefactSpec(
        name="pnl",
        filename="pnl.parquet",
        kind="parquet",
        description=(
            "The engine's own per-bar decomposition. Its six components sum "
            "to `total`, which is the identity the analytics layer currently "
            "has to reconstruct because this was never stored."
        ),
        required=(
            "gap", "intraday", "spread", "slippage", "financing", "fx",
            "total",
        ),
        optional=("realized", "unrealized"),
        index="timestamp",
    ),
    "execution_metadata": ArtefactSpec(
        name="execution_metadata",
        filename="execution_metadata.json",
        kind="json",
        description=(
            "Every setting the execution engine ran under, plus its headline "
            "figures. Without this the Config page can only show built-in "
            "defaults and claim nothing about what the run actually used."
        ),
    ),
    "metadata": ArtefactSpec(
        name="metadata",
        filename="metadata.json",
        kind="json",
        description="Run-level metadata: identity, timing, universe, warnings.",
    ),
    "analysis": ArtefactSpec(
        name="analysis",
        filename="analysis.parquet",
        kind="parquet",
        description=(
            "Preprocessed panels in long form — prices, returns, volatility, "
            "ATR, correlations. Stored so analytics need not re-run "
            "preprocessing for every point of a parameter sweep."
        ),
        required=("timestamp", "panel", "series", "value"),
        index="timestamp",
    ),
    "regimes": ArtefactSpec(
        name="regimes",
        filename="regime.parquet",
        kind="parquet",
        description=(
            "Regime classifications, one column per classifier. They are "
            "market-level Series — one label per bar, not per instrument. "
            "Computed in memory today and discarded, which is why the Signals "
            "page can only show the reason string."
        ),
        required=("timestamp",),
        optional=(
            "volatility_regime", "trend_regime", "correlation_regime",
            "macro_regime", "combined_regime",
        ),
        index="timestamp",
    ),
    "costs": ArtefactSpec(
        name="costs",
        filename="costs.parquet",
        kind="parquet",
        description="Cost totals per instrument, from the trade log.",
    ),
    "attribution": ArtefactSpec(
        name="attribution",
        filename="attribution.parquet",
        kind="parquet",
        description="P&L split into its sources; the terms sum to the total.",
        required=("component", "amount"),
    ),
    "stop_events": ArtefactSpec(
        name="stop_events",
        filename="stop_events.parquet",
        kind="parquet",
        description="One row per stop-loss or take-profit breach.",
    ),
}


def spec_for(name: str) -> ArtefactSpec:
    """The spec for one artefact.

    Raises
    ------
    KeyError
        On an unknown name, with the known ones listed — a typo here would
        otherwise write a file nothing ever reads.
    """
    try:
        return ARTEFACTS[name]
    except KeyError:
        known = ", ".join(sorted(ARTEFACTS))
        raise KeyError(f"unknown artefact {name!r}; known artefacts: {known}") from None


def missing_columns(frame: pd.DataFrame, name: str) -> tuple[str, ...]:
    """Required columns the frame does not have.

    Pure. An empty frame is exempt: "the run produced no trades" is a valid
    outcome, and demanding columns of an empty frame would turn a quiet
    period into a failure.
    """
    spec = spec_for(name)
    if frame is None or frame.empty or not spec.required:
        return ()
    present = set(frame.columns) | ({frame.index.name} if frame.index.name else set())
    return tuple(column for column in spec.required if column not in present)


def validate_frame(frame: pd.DataFrame, name: str, *, strict: bool = False) -> tuple[str, ...]:
    """Check a frame against its spec, returning what is missing.

    ``strict`` raises instead. The writer uses ``strict=False`` and logs,
    because refusing to persist a run because one optional-in-practice column
    moved would lose the whole run's record over a cosmetic mismatch — and
    the record is the thing being protected.
    """
    missing = missing_columns(frame, name)
    if missing and strict:
        raise ValueError(
            f"{name} is missing required column(s): {', '.join(missing)}"
        )
    return missing


def describe_run() -> pd.DataFrame:
    """The registry as a table, for documentation and for the Config page."""
    return pd.DataFrame(
        [
            {
                "artefact": spec.name,
                "filename": spec.filename,
                "kind": spec.kind,
                "index": spec.index or "",
                "required_columns": len(spec.required),
                "description": spec.description,
            }
            for spec in ARTEFACTS.values()
        ]
    ).set_index("artefact")
