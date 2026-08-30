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

__all__ = ["poll_live_quotes", "held_instruments", "instruments_to_price"]


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


def instruments_to_price(
    config=None,
    account: str | None = None,
    *,
    min_confidence: float | None = None,
) -> list[str]:
    """What is worth a quote: what you hold, plus what you are being offered.

    Open positions are the obvious half -- you need to know what they are
    worth now. The other half is the instruments clearing the confidence
    bar, and they matter for a reason worth stating: a suggestion carries a
    **stake and a quantity**, and those are computed from a price. Priced
    from yesterday's close, the quantity you are told to place is the
    quantity that was right yesterday.

    Still a small list. Open positions are few, and most days nothing clears
    the bar at all -- this is not a licence to poll the universe.
    """
    from cfdinvest.advisor.actions import DEFAULT_CONFIDENCE

    wanted = list(held_instruments(config, account))
    bar = DEFAULT_CONFIDENCE if min_confidence is None else float(min_confidence)

    try:
        from cfdinvest.persist import readers

        run_id = readers.load_latest_run(config, account=account)
        frame = (
            None if run_id is None
            else readers.load_frame("signals", run_id, config, account)
        )
        if frame is not None and not getattr(frame, "empty", True):
            working = frame.reset_index() if hasattr(frame, "reset_index") else frame
            if "timestamp" in working.columns:
                import pandas as pd

                stamps = pd.to_datetime(
                    working["timestamp"], utc=True, errors="coerce"
                )
                working = working[stamps == stamps.max()]
            for _, row in working.iterrows():
                if str(row.get("direction", "flat")).lower() == "flat":
                    continue
                try:
                    if float(row.get("confidence", 0.0)) < bar:
                        continue
                except (TypeError, ValueError):
                    continue
                ticker = str(row.get("ticker") or "").strip()
                if ticker:
                    wanted.append(ticker)
    except Exception as error:  # noqa: BLE001 - no signals is not a failure
        logger.warning("Could not read signals for pricing: %s", error)

    # The paper book holds things too, and until now nothing priced them.
    # The dashboard marks those positions against this quote file, so
    # leaving them out meant the forward record -- the whole point of the
    # exercise -- showed as unpriceable on the one page that reports it.
    #
    # Their prices are polled inside the paper pass as well, but in memory
    # and thrown away. Asked for here so there is one answer to "what is
    # worth a quote" rather than two that can disagree.
    try:
        from cfdinvest.persist import intent

        wanted.extend(intent.held_tickers(config, account))
    except Exception as error:  # noqa: BLE001 - no run is not a failure
        logger.warning("Could not read the engine's positions for pricing: %s", error)

    return list(dict.fromkeys(wanted))


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
