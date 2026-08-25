"""Rehearsing the manual-trading loop — not simulating prices again.

::

    from cfdinvest.simulate import run_simulation

    run = run_simulation(config)
    print(run.headline)

Also a page in the dashboard: **Simulation**.

**This is not a second backtester.** :mod:`cfdinvest.execution` already fills
orders, charges spread and slippage, converts currencies, applies financing
and resolves stops. This reuses it unchanged and adds the layer above: the
decisions. It answers what would have happened had you followed the helper's
advice, including every time it said to trade smaller.

Every artefact is marked ``simulated`` and written under its own account tree.
A rehearsal read as a real record is the one failure here that nobody
recovers from.
"""

from __future__ import annotations

from cfdinvest.simulate.controller import (
    SIMULATION_ACCOUNT,
    SimulationRun,
    run_simulation,
)
from cfdinvest.simulate.meta import MetaJournal, PostureChange, simulate_postures
from cfdinvest.simulate.engine import (
    SimulationConfig,
    SimulationResult,
    apply_advice_to_signals,
    detect_forced_closures,
    simulate_advice,
)

__all__ = [
    "SIMULATION_ACCOUNT",
    "SimulationConfig",
    "SimulationResult",
    "SimulationRun",
    "simulate_advice",
    "apply_advice_to_signals",
    "detect_forced_closures",
    "run_simulation",
    "MetaJournal",
    "PostureChange",
    "simulate_postures",
]
