"""Reading the broker for the dashboard, in one place a test can reach.

This lived inside the page, which meant the page's whole body was
unreachable in tests: with no credentials configured — which is the normal
state, and which the test suite now guarantees deliberately — the script
stopped at the second paragraph and every line after it went unexecuted.

That is how a tile with three fields instead of four reached the deployed
dashboard with a green suite behind it. The test asserted the page *rendered*
and it did, having done almost nothing.

So the read is here, importable and patchable, and the page is the part that
draws. Nothing here touches Streamlit, and nothing here can deal.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["read_broker"]


def read_broker(config=None) -> dict[str, Any]:
    """Balance, positions and live costs. Never raises.

    Returns a plain dictionary — nothing callable, because the caller caches
    it and a lambda in a cached payload fails to pickle, which takes a
    *different* page down with it rather than this one.

    Every failure is carried rather than raised. An account holding nothing
    and an account that could not be read look identical to a page that only
    draws a table, and only one of them means it is safe to open more.
    """
    out: dict[str, Any] = {
        "configured": False,
        "environment": "",
        "error": "",
        "account": None,
        "positions": [],
        "positions_error": "",
        "markets": [],
    }
    try:
        from cfdinvest.broker.capital import client as capital
        from cfdinvest.broker.capital import dealing
        from cfdinvest.config import load_config

        settings = capital.CapitalConfig.from_env()
        out["environment"] = settings.environment
        if not settings.complete:
            out["error"] = (
                "No Capital.com credentials in the environment, so nothing "
                "can be read. This is not a failure — the account is "
                "optional — but it is also not an empty account."
            )
            return out
        out["configured"] = True

        session = capital.open_session(settings)
        if not session.ok:
            out["error"] = f"Could not authenticate: {session.error}"
            return out

        account = capital.fetch_account(session, settings)
        out["account"] = {
            "id": account.account_id,
            "currency": account.currency,
            "balance": account.balance,
            "available": account.available,
            "profit_loss": account.profit_loss,
            "deposit": account.deposit,
        }

        found, error = dealing.list_positions(session, settings)
        out["positions_error"] = error
        out["positions"] = [
            {
                "Instrument": p.epic, "Side": p.direction, "Size": p.size,
                "Opened at": p.level, "Unrealised": p.upl, "Ccy": p.currency,
                "Deal id": p.deal_id,
            }
            for p in found
        ]

        tickers = list((config or load_config()).market.tickers or ())
        markets = capital.fetch_market_details(tickers, session, settings)
        out["markets"] = [
            {
                "Instrument": m.ticker,
                "Bid": m.bid,
                "Offer": m.offer,
                "Spread bp": None if m.spread_pct is None else m.spread_pct * 100,
                "Min size": m.min_deal_size,
                "Status": "tradeable" if m.tradable else "closed",
            }
            for m in markets
        ]
    except Exception as error:  # noqa: BLE001 - a page must render
        logger.warning("Could not read the broker: %s", error)
        out["error"] = f"{type(error).__name__}: {error}"
    return out
