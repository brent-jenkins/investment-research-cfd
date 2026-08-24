"""Run-artefact persistence: deterministic writes, read-only reads.

Extends the run store the scheduler already writes rather than creating a
second one, so there remains exactly one place to look for what a run did.

Writes are atomic and append-only; a past run is never rewritten. Readers
never raise, and distinguish "not written" from "unreadable" — only the
second is a fault.
"""

from __future__ import annotations

from cfdinvest.persist.readers import (
    artefact_status,
    list_runs,
    load_analysis_frame,
    load_attribution,
    load_cost_breakdown,
    load_execution_frame,
    load_execution_metadata,
    load_latest_run,
    load_pnl_frame,
    load_regime_frame,
    load_run_metadata,
    load_run_warnings,
    load_stop_events,
)
from cfdinvest.persist.runmeta import RunMetadata, build_run_metadata
from cfdinvest.persist.scheduler_state import (
    load_scheduler_state,
    update_last_error,
    update_last_run,
    update_last_success,
    write_scheduler_state,
)
from cfdinvest.persist.schema import ARTEFACTS, SCHEMA_VERSION, describe_run
from cfdinvest.persist.storage import (
    ensure_run_directory,
    ensure_scheduler_state,
    get_run_directory,
    get_scheduler_state_path,
    generate_run_id,
)
from cfdinvest.persist.writers import (
    write_all,
    write_analysis_frame,
    write_execution_frame,
    write_execution_metadata,
    write_pnl_frame,
    write_regime_frame,
    write_run_metadata,
)

__all__ = [
    "ARTEFACTS",
    "SCHEMA_VERSION",
    "describe_run",
    "RunMetadata",
    "build_run_metadata",
    "generate_run_id",
    "get_run_directory",
    "ensure_run_directory",
    "get_scheduler_state_path",
    "ensure_scheduler_state",
    "write_all",
    "write_analysis_frame",
    "write_regime_frame",
    "write_execution_frame",
    "write_pnl_frame",
    "write_execution_metadata",
    "write_run_metadata",
    "list_runs",
    "load_latest_run",
    "artefact_status",
    "load_analysis_frame",
    "load_regime_frame",
    "load_execution_frame",
    "load_pnl_frame",
    "load_execution_metadata",
    "load_run_metadata",
    "load_run_warnings",
    "load_cost_breakdown",
    "load_attribution",
    "load_stop_events",
    "load_scheduler_state",
    "write_scheduler_state",
    "update_last_run",
    "update_last_success",
    "update_last_error",
]
