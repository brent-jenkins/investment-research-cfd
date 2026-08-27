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

That number is the whole point of the exercise, and measuring it corrected
the figure this docstring used to quote. The estimated spread is a constant
of **10bp round trip**, not 4bp -- ``spread_bps`` is unset and no
asset-class map is supplied, so the lookup falls through to
``default_spread_bps = 5.0``, a *half* spread -- and it is 46.6% of modelled
cost, not 79%.

Measured on 2026-08-27: median 5.3bp round trip, from 2.8 (META, COST) to
28.6 (NKE). So the model was overstating its largest line rather than
understating it, which is the comfortable direction to be wrong in and still
wrong.
"""

from cfdinvest.broker.capital.client import (
    CapitalAccount,
    CapitalConfig,
    CapitalMarket,
    CapitalSession,
    fetch_account,
    fetch_market,
    fetch_markets,
    find_epic,
    open_session,
)

__all__ = [
    "CapitalAccount",
    "CapitalConfig",
    "CapitalMarket",
    "CapitalSession",
    "fetch_account",
    "fetch_market",
    "fetch_markets",
    "find_epic",
    "open_session",
]
