"""What Capital's own clients hold: the share long and short, per market.

WHY IT IS COLLECTED
-------------------
Every free technical reading this project can make on its universe -- trend,
momentum, breakout, reversal, pre-market, after-hours, the market's morning
direction, big moves, volume -- forecasts how FAR a name moves and not which
WAY (CLAUDE.md, 2026-08-28 to 2026-09-28). Client positioning is a different
kind of information: what a crowd of retail traders is betting, which the
literature treats as a contrarian reading when it is lopsided.

The broker serves it free -- ``GET /clientsentiment?marketIds=a,b,c`` -- and
serves only the CURRENT reading. There is no history to backfill, so the only
way it can ever be tested is to record it forward, which is what the
``record-client-sentiment`` job does. Started on the owner's decision,
2026-09-28.

The level is always long-heavy for shares -- 85-92% long on the day it was
first read -- so whatever signal exists is a name's positioning against its
OWN usual level, not the raw percentage. That is for the reader to compute;
this module records what the broker said.

WHAT IT NEVER DOES
------------------
It does not guess. A market the broker does not answer for is reported as
``None``, never as 0% or 50%: 0% long is a confident statement that the
crowd is entirely short, and folding an absence in as that would manufacture
the extreme the reading is for. It never raises: one bad batch retries its
markets one at a time, so a single refused identifier cannot lose the rest.
"""
from __future__ import annotations

import logging
import urllib.parse
from typing import Any, Iterable, Mapping

logger = logging.getLogger(__name__)

__all__ = ["MAX_IDS_PER_REQUEST", "fetch_client_sentiment"]

#: Markets asked for in one request. The endpoint takes a comma-separated
#: list; a modest batch keeps one refused identifier from costing many.
MAX_IDS_PER_REQUEST = 25


def _reading(entry: Mapping[str, Any] | None) -> dict | None:
    """``{"long": x, "short": y}`` in percent, or ``None`` if unusable."""
    if not isinstance(entry, Mapping):
        return None
    try:
        long_pct = float(entry.get("longPositionPercentage"))
        short_pct = float(entry.get("shortPositionPercentage"))
    except (TypeError, ValueError):
        return None
    if not (0.0 <= long_pct <= 100.0 and 0.0 <= short_pct <= 100.0):
        return None
    return {"long": long_pct, "short": short_pct}


def _ask(ids, session, settings, read) -> dict[str, dict | None] | None:
    """One request for ``ids``. ``None`` means the request itself failed."""
    query = urllib.parse.urlencode({"marketIds": ",".join(ids)})
    try:
        document, _ = read(
            f"{session.host}/clientsentiment?{query}",
            headers=session.headers(settings.api_key),
        )
    except Exception as error:  # noqa: BLE001 - a batch, not an outage
        logger.debug("Client sentiment request failed for %s: %s", ids, error)
        return None
    found = {}
    for entry in (document or {}).get("clientSentiments") or []:
        market = str((entry or {}).get("marketId") or "")
        if market:
            found[market] = _reading(entry)
    return {market: found.get(market) for market in ids}


def fetch_client_sentiment(
    epics: Iterable[str],
    session,
    config=None,
    *,
    opener=None,
) -> dict[str, dict | None]:
    """Current client positioning for every epic. Never raises.

    Returns ``{epic: {"long": %, "short": %} or None}`` for every epic
    asked about, in the order given. ``None`` is an absence: the broker did
    not answer for that market, and the reader must not treat it as a value.
    """
    from cfdinvest.broker.capital.client import CapitalConfig, _read

    wanted = list(dict.fromkeys(str(e) for e in (epics or ()) if str(e)))
    out: dict[str, dict | None] = {epic: None for epic in wanted}
    if not wanted:
        return out
    if not getattr(session, "ok", False):
        logger.warning("No Capital session; client sentiment not read")
        return out

    settings = config or CapitalConfig.from_env()
    read = opener or _read
    for start in range(0, len(wanted), MAX_IDS_PER_REQUEST):
        batch = wanted[start:start + MAX_IDS_PER_REQUEST]
        answer = _ask(batch, session, settings, read)
        if answer is None:
            # The whole batch was refused -- usually one identifier the
            # broker does not know. Ask one at a time so the rest survive.
            answer = {}
            for epic in batch:
                single = _ask([epic], session, settings, read)
                answer[epic] = (single or {}).get(epic)
        out.update(answer)
    return out
