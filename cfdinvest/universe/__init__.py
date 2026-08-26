"""Which instruments to trade, decided by a rule rather than by hand.

See :mod:`cfdinvest.universe.screen` for why this exists and what it
deliberately does not measure.
"""

from cfdinvest.universe.screen import (
    Candidate,
    ScreenConfig,
    ScreenResult,
    rank_candidates,
    select_universe,
)

__all__ = [
    "Candidate",
    "ScreenConfig",
    "ScreenResult",
    "rank_candidates",
    "select_universe",
]
