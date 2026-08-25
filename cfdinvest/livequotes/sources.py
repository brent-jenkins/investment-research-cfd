"""The one place in this package that touches the network.

Kept alone in its own module on purpose. :mod:`cfdinvest.livequotes.storage`
is pure file I/O, so the live helper, the risk layer and monitoring can read a
quote without importing anything that can open a socket -- and the structural
import tests over those packages stay true rather than merely passing.

What is fetched, and what is not
--------------------------------
**The current price, and nothing else.** No bars are requested for their own
sake, none are read, and none are stored. The lake stays daily: an intraday
series written next to it would be a second source of truth for the same
instrument, and the first disagreement between them would be found by a trade.

Why this endpoint
-----------------
The obvious one -- ``/v7/finance/quote`` -- now answers ``401 Unauthorized``
without a cookie and crumb::

    {"finance":{"result":null,"error":{"code":"Unauthorized", ...}}}

Getting past that means scraping a credential to defeat an access control that
was added deliberately, and it breaks whenever the scheme changes. So the
price is read from ``meta.regularMarketPrice`` on the chart endpoint, which
answers unauthenticated. The response happens to carry a bar array; this
module never looks at it. That is a deviation from the spec's choice of URL
and it is recorded here rather than buried.

No broker is involved. This is a public market data endpoint, not Trading
212, and nothing here places or prepares an order.
"""

from __future__ import annotations

import json
import logging
import math
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "QUOTE_URL",
    "USER_AGENT",
    "TIMEOUT_SECONDS",
    "Quote",
    "fetch_quote",
]

#: The chart endpoint, read for its ``meta`` block only. ``range`` and
#: ``interval`` are the smallest request that still returns a meta block; the
#: bars they imply are not read.
QUOTE_URL = (
    "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    "?range=1d&interval=1d"
)

#: Sent because the endpoint refuses a request without one. It identifies the
#: caller honestly rather than impersonating a browser session.
USER_AGENT = "cfdinvest/1.0 (market data reader)"

#: One attempt, this long. There are no retries anywhere in this package: a
#: poll that misses is a quote that is absent, and absent is a thing this
#: project knows how to say.
TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class Quote:
    """One instrument's current price, and when the venue stamped it.

    ``price`` is ``None`` when the field was missing or unusable. That is the
    whole point of the type: a quote that could not be read is reported
    absent, never defaulted and never zero.
    """

    symbol: str
    price: float | None = None
    quoted_at: Any = None
    currency: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.price is not None


def _finite(value: Any) -> float | None:
    """A usable price, or ``None``.

    Zero is rejected along with NaN. No instrument this project follows
    trades at zero, so a zero here is a missing field that arrived as a
    number -- and it would size a position as if it were free.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def _read(url: str) -> dict[str, Any] | None:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        document = json.loads(response.read().decode("utf-8"))
    return document if isinstance(document, dict) else None


def fetch_quote(symbol: str, *, opener=None) -> Quote:
    """The current price for one symbol. Never raises.

    A failure is returned as a :class:`Quote` carrying the reason, because
    the caller's job is to record what is and is not known rather than to
    handle an exception per instrument.

    ``opener`` exists so the tests can drive this without a network. It takes
    a URL and returns the parsed document.
    """
    name = str(symbol or "").strip()
    if not name:
        return Quote(symbol=str(symbol), error="no symbol given")

    url = QUOTE_URL.format(symbol=urllib.parse.quote(name, safe=""))
    read = opener or _read
    try:
        document = read(url)
    except urllib.error.HTTPError as error:
        return Quote(symbol=name, error=f"HTTP {error.code}")
    except Exception as error:  # noqa: BLE001 - one bad symbol is not an outage
        return Quote(symbol=name, error=f"{type(error).__name__}: {error}")

    try:
        result = ((document or {}).get("chart") or {}).get("result") or []
        meta = (result[0] or {}).get("meta") or {} if result else {}
    except (AttributeError, IndexError, TypeError):
        meta = {}

    if not meta:
        return Quote(symbol=name, error="no meta block in the response")

    price = _finite(meta.get("regularMarketPrice"))
    if price is None:
        return Quote(symbol=name, error="no usable regularMarketPrice")

    return Quote(
        symbol=name,
        price=price,
        quoted_at=meta.get("regularMarketTime"),
        currency=meta.get("currency"),
    )
