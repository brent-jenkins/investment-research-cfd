"""What to do now: the suggestions worth acting on, and positions to watch.

Deliberately **not** in ``ui/``. The dashboard is an optional install and the
scheduler has to run on a box where it was never put -- so the logic that
decides what is worth telling you lives here, and both the page and the
notifier read it. A notifier that imported the dashboard to work out what to
say would fail on exactly the machine it exists to send from.

Pure with respect to the world: these functions take positions, prices and a
threshold, and return rows. Reading any of that is somebody else's job.
"""

from __future__ import annotations

from cfdinvest.advisor.actions import (
    DEFAULT_CONFIDENCE,
    Suggestion,
    Watch,
    positions_to_watch,
    confidence_ceiling,
    suggested_trades,
)

__all__ = [
    "DEFAULT_CONFIDENCE",
    "Suggestion",
    "Watch",
    "positions_to_watch",
    "confidence_ceiling",
    "suggested_trades",
]
