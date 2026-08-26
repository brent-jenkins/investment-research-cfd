"""IG: sessions, instruments and their real costs. Read-only.

Nothing here places, amends or cancels an order. See
:mod:`cfdinvest.broker.ig.client` for why this exists and what it refuses to
do.
"""

from cfdinvest.broker.ig.client import (
    HOSTS,
    IgConfig,
    IgMarket,
    IgSession,
    fetch_market,
    fetch_markets,
    open_session,
)

__all__ = [
    "HOSTS",
    "IgConfig",
    "IgMarket",
    "IgSession",
    "fetch_market",
    "fetch_markets",
    "open_session",
]
