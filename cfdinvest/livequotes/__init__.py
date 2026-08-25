"""Current prices for what you are holding, and nothing more.

The lake is daily. Between one close and the next, every price this project
reasons about is yesterday's -- which is fine for a backtest and wrong for a
person about to place a trade. This package closes that gap for the open book
only, and closes it as narrowly as it can.

``storage`` is pure file I/O and is what the live helper, the risk layer and
monitoring import. ``sources`` is the only module that opens a socket. That
split is the point: a package that reads a price must not be able to fetch
one.
"""

from __future__ import annotations

from cfdinvest.livequotes.poller import held_instruments, poll_live_quotes
from cfdinvest.livequotes.storage import (
    DEFAULT_MAX_AGE_MINUTES,
    age_minutes,
    latest_path,
    load_quotes,
    quote_for,
    stale_symbols,
    write_quotes,
)

__all__ = [
    "DEFAULT_MAX_AGE_MINUTES",
    "age_minutes",
    "held_instruments",
    "latest_path",
    "load_quotes",
    "poll_live_quotes",
    "quote_for",
    "stale_symbols",
    "write_quotes",
]
