"""One pass over the held instruments, asking each for its current price.

No retries, no threads, no async. A poll that misses is a quote that is
absent, and every reader of this package already knows how to fall back to
the daily bar -- so the machinery that would be needed to turn a miss into a
success buys nothing and adds a way for the poller to hang.

The instruments come from the confirm layer, which is the only record of what
you actually hold. The account registry says what accounts *exist*, not what
is open in them.
"""

from __future__ import annotations

import logging
from typing import Iterable, Sequence

logger = logging.getLogger(__name__)

__all__ = ["poll_live_quotes", "held_instruments"]


def held_instruments(config=None, account: str | None = None) -> list[str]:
    """Every instrument with a non-zero confirmed position.

    Read from the confirm layer rather than from the simulated book: this
    feature exists to price what you are actually carrying, and the two
    differ exactly when it matters most.

    Returns an empty list rather than raising if nothing has been confirmed.
    """
    try:
        from cfdinvest.persist.confirm import confirmed_positions, load_confirmations

        confirmations = load_confirmations(config, account=account)
    except Exception as error:  # noqa: BLE001 - no book is not a failure
        logger.warning("Could not read confirmations: %s", error)
        return []

    try:
        book = confirmed_positions(confirmations)
    except Exception as error:  # noqa: BLE001
        logger.warning("Could not replay confirmations: %s", error)
        return []

    return sorted(
        str(name)
        for name, entry in (book or {}).items()
        if (entry or {}).get("quantity")
    )


def poll_live_quotes(
    open_positions: Sequence[str] | Iterable[str],
    *,
    fetch=None,
) -> dict[str, float | None]:
    """Current price per instrument, ``None`` where it could not be read.

    The absent entries are kept in the mapping rather than dropped. The
    caller asked about these instruments and is owed an answer for each --
    "could not read SPX500" and "was not asked about SPX500" are different
    statements, and only the storage layer, which is writing a snapshot of
    what is known, is entitled to collapse them.

    ``fetch`` is the seam the tests use; by default it is the live source.
    """
    if fetch is None:
        from cfdinvest.livequotes.sources import fetch_quote

        fetch = fetch_quote

    prices: dict[str, float | None] = {}
    for symbol in dict.fromkeys(str(s) for s in (open_positions or ())):
        if not symbol.strip():
            continue
        quote = fetch(symbol)
        price = getattr(quote, "price", quote)
        prices[symbol] = price if isinstance(price, (int, float)) else None
        if prices[symbol] is None:
            logger.info(
                "No live price for %s: %s",
                symbol,
                getattr(quote, "error", "no price in the response"),
            )
    return prices
