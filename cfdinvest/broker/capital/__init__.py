"""Capital.com: read-only market data, for measuring what a trade costs.

There is no dealing module here and no function that could open a position.
That is the same construction the IG client uses and for the same reason --
the failure being prevented is not "an order is placed" but "an order placed
by code nobody thought could trade".

Why this broker, after IG was measured and rejected: the spread is readable
without dealing. IG would not quote share prices to that account, so the
only way to learn what a trade cost was to place one, and the answer -- a
$15-per-side commission, 222bp round trip on £1,000 -- was expensive to
find out. Capital.com returns ``bid`` and ``offer`` on the market details
endpoint, so the number that matters can be read.

That number is the whole point of the exercise. Every one of the twenty
instruments currently charges an *estimated* spread: one per-class constant
of 4bp round trip, which is 79% of all modelled cost against a break-even
near 6bp of further friction. The largest line in the cost model is a guess,
and this exists to replace it with twenty measured ones.
"""

from cfdinvest.broker.capital.client import (
    CapitalConfig,
    CapitalMarket,
    CapitalSession,
    fetch_market,
    fetch_markets,
    find_epic,
    open_session,
)

__all__ = [
    "CapitalConfig",
    "CapitalMarket",
    "CapitalSession",
    "fetch_market",
    "fetch_markets",
    "find_epic",
    "open_session",
]
