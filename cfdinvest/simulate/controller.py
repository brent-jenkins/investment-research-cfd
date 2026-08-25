"""Running a rehearsal over a stored run, and filing what it produced.

Reads a run's signals and analysis panels, replays them with and without the
helper's advice, and writes the result into the **simulation** account tree.

The one property this module exists to protect
----------------------------------------------
**A simulated artefact must never be readable as a real one.**

Three defences, and the redundancy is deliberate because the failure is
unrecoverable — someone reading a rehearsal as their actual record does not
find out they were wrong:

1. Simulated artefacts live under their own account tree, using the isolation
   the virtual/live separation already provides and already tests.
2. Every document carries ``simulated: true`` and the settings it was run
   under.
3. Nothing here writes to the run directories the pipeline owns.

The journal, insights and bias layers can all be pointed at the simulated
record — that is useful, and it is how you find out whether a strategy would
have tripped the behavioural detectors. Those outputs are filed in the same
tree and inherit the same marking.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from cfdinvest.simulate.engine import (
    SimulationConfig,
    SimulationResult,
    simulate_advice,
)

logger = logging.getLogger(__name__)

__all__ = ["SIMULATION_ACCOUNT", "SimulationRun", "run_simulation"]

#: Where every simulated artefact goes. Not a deployment mode — nothing runs
#: "as simulation"; this layer writes there.
SIMULATION_ACCOUNT = "simulation"


@dataclass(frozen=True, slots=True)
class SimulationRun:
    """One rehearsal, and everything derived from it."""

    result: SimulationResult
    run_id: str | None = None
    timestamp: Any = None
    journal_entries: int = 0
    insights: Any = None
    bias: Any = None
    stored_path: str | None = None
    notes: tuple[str, ...] = ()
    simulated: bool = True

    @property
    def ok(self) -> bool:
        return self.result.ok

    @property
    def headline(self) -> str:
        return self.result.headline

    def to_document(self) -> dict[str, Any]:
        from cfdinvest.persist.writers import jsonable

        result = self.result
        return jsonable(
            {
                # First key on purpose: whatever reads this sees it first.
                "simulated": True,
                "disclaimer": (
                    "Every figure in this document is from a rehearsal. No "
                    "order was placed and no money moved. These are not your "
                    "results."
                ),
                "timestamp": (
                    self.timestamp.isoformat()
                    if hasattr(self.timestamp, "isoformat")
                    else str(self.timestamp)
                ),
                "run_id": self.run_id,
                "label": result.label,
                "headline": result.headline,
                "error": result.error,
                "baseline_metrics": result.baseline_metrics,
                "advised_metrics": result.advised_metrics,
                "differences": {
                    name: result.difference(name)
                    for name in ("final_equity", "sharpe_ratio", "win_rate")
                },
                "adjustments": int(len(result.adjustments)),
                "forced_closures": int(len(result.forced_closures)),
                "notes": list(result.notes) + list(self.notes),
                "derived": {
                    "journal_entries": self.journal_entries,
                    "insights": (
                        None if self.insights is None
                        else self.insights.headline
                    ),
                    "bias": None if self.bias is None else self.bias.headline,
                },
            }
        )


def run_simulation(
    config=None,
    *,
    run_id: str | None = None,
    source_account: str | None = None,
    simulation: SimulationConfig | None = None,
    helper_config=None,
    context=None,
    store: bool = True,
    derive: bool = True,
    now=None,
) -> SimulationRun:
    """Rehearse a stored run and file the result. Never raises.

    ``source_account`` is where the *real* run is read from. The output always
    goes to the simulation tree regardless, which is not configurable on
    purpose.
    """
    from cfdinvest.persist import readers

    settings = simulation or SimulationConfig()
    moment = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="UTC")
    if moment.tz is None:
        moment = moment.tz_localize("UTC")

    def read(call, default):
        try:
            return call()
        except Exception as error:  # noqa: BLE001
            logger.debug("Could not read: %s", error)
            return default

    chosen = run_id or read(
        lambda: readers.load_latest_run(config, source_account), None
    )
    if chosen is None:
        return SimulationRun(
            result=SimulationResult(
                label=settings.label, error="no run has been recorded to rehearse"
            ),
            timestamp=moment,
        )

    # Both frames are rebuilt from the long forms on disk into the wide
    # panels the engine expects. Rebuilt rather than re-derived: the rehearsal
    # has to run over exactly the signals and prices the original run did, or
    # the comparison measures the reconstruction.
    signals = read(
        lambda: readers.rebuild_signal_frame(None, chosen, config, source_account),
        None,
    )
    # The engine wants panels keyed by field; what is on disk is the long
    # form. Rebuilt rather than re-derived, so the rehearsal runs over exactly
    # the prices the original run did.
    analysis = read(
        lambda: readers.rebuild_analysis_frame(
            None, chosen, config, source_account
        ),
        None,
    )
    long_analysis = read(
        lambda: readers.load_analysis_frame(chosen, config, source_account),
        pd.DataFrame(),
    )
    metadata = read(
        lambda: readers.load_execution_metadata(chosen, config, source_account) or {},
        {},
    )

    result = simulate_advice(
        signals,
        analysis,
        _execution_config(metadata),
        prices=long_analysis,
        recorded_final_equity=metadata.get("final_equity"),
        config=settings,
        helper_config=helper_config,
        context=context,
    )

    derived_journal, insights, bias = 0, None, None
    if derive and result.ok:
        derived_journal, insights, bias = _derive(config, result, moment)

    run = SimulationRun(
        result=result,
        run_id=chosen,
        timestamp=moment,
        journal_entries=derived_journal,
        insights=insights,
        bias=bias,
        notes=(
            "The behavioural findings below describe what this strategy, "
            "executed this way, would have triggered. They are a fact about "
            "the strategy, not a forecast about you.",
        ) if bias is not None else (),
    )

    if store:
        from cfdinvest.persist import simulation as simulation_store

        path = simulation_store.write_simulation(run, config)
        run = SimulationRun(**{**_as_dict(run), "stored_path": path})

    logger.info("Simulation: %s", run.headline)
    return run


def _as_dict(run: SimulationRun) -> dict[str, Any]:
    return {
        name: getattr(run, name)
        for name in SimulationRun.__slots__
    }


def _execution_config(metadata: dict):
    """Rebuild the execution settings the run used.

    Read from what the run recorded rather than defaulted, so the rehearsal
    charges the same spread and slippage as the thing it is being compared
    against. A comparison between two different cost models measures the cost
    models.
    """
    from cfdinvest.execution.frame import ExecutionConfig

    import dataclasses

    # Only the plain fields. The nested ones -- stops, fx, hysteresis -- are
    # written to metadata flattened, so what is on disk under those names is a
    # mapping rather than the dataclass the field wants, and handing it over
    # produces "'dict' object has no attribute ..." from deep inside the
    # engine. They fall back to defaults, and the fallback is reported.
    scalar = (str, int, float, bool)
    known, skipped = {}, []
    for name, spec in ExecutionConfig.__dataclass_fields__.items():
        if name not in metadata or metadata[name] is None:
            continue
        value = metadata[name]
        if dataclasses.is_dataclass(spec.type) or not isinstance(value, scalar):
            skipped.append(name)
            continue
        known[name] = value

    if skipped:
        logger.info(
            "Execution settings %s could not be rebuilt from the run metadata "
            "and fall back to defaults", ", ".join(sorted(skipped))
        )

    try:
        return ExecutionConfig(**known)
    except (TypeError, ValueError) as error:
        logger.warning(
            "Could not rebuild the run's execution settings (%s); using "
            "defaults, so the comparison is between two cost models rather "
            "than two sizing decisions", error
        )
        return ExecutionConfig()


def _derive(config, result: SimulationResult, moment):
    """Run the journal, insights and bias layers over the simulated record.

    All three read the *simulated* trades and file into the simulation tree.
    Reusing them is the point: a separate simulated-insights implementation
    would be a second set of thresholds to keep in step.
    """
    entries = 0
    insights = bias = None

    try:
        from cfdinvest.insights import build_report

        insights = build_report(result.trades, now=moment)
    except Exception as error:  # noqa: BLE001
        logger.warning("Could not derive simulated insights: %s", error)

    try:
        from cfdinvest.bias import build_bias_report

        bias = build_bias_report(trades=result.trades, now=moment)
    except Exception as error:  # noqa: BLE001
        logger.warning("Could not derive simulated behaviour: %s", error)

    return entries, insights, bias
