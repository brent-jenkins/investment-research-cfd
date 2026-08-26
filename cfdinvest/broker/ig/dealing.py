"""Placing orders on IG. Off unless switched on, demo unless told otherwise.

The module is called ``dealing`` because that is what it does, and the
project's constraint says anything that can trade must say so in its name.
Nothing imports it by accident.

**Two gates, not one.** ``CFD_IG_DEALING_ENABLED`` must be truthy before any
order is placed at all, and reaching a *live* account needs
``CFD_IG_ALLOW_LIVE`` as well. A single switch is one typo away from a funded
account; two switches mean the mistake has to be made twice, deliberately,
in different words.

**Why this exists: to measure, not to trade.** Every one of the nineteen
instruments charges an estimated spread -- one per-class constant of 4bp
round trip, 79% of all modelled cost, against a break-even of about 4bp
further slippage. IG will not show this account share prices
(``unauthorised.access.to.equity.exception``), so the spread cannot be
quoted. It can be *filled*: buy and you are filled at the offer, sell and
you are filled at the bid, and the difference between them is the spread
that was actually charged. A fill is a better measurement than a quote
anyway, because a quote never includes slippage and slippage is what the
edge dies on.

**The paper book must never import this.** Its worth is being a record
nobody could have edited once the outcome was known, and a simulation that
sometimes places an order is not one. A test enforces it.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Mapping

from cfdinvest.broker.ig.client import IgConfig, IgSession, _read, _number

logger = logging.getLogger(__name__)

__all__ = [
    "DealingConfig",
    "DealResult",
    "SpreadProbe",
    "place_order",
    "confirm_deal",
    "close_position",
    "measure_spread",
]

ENABLED_VAR = "CFD_IG_DEALING_ENABLED"
ALLOW_LIVE_VAR = "CFD_IG_ALLOW_LIVE"

#: Values that count as yes. Anything else -- including "false", "no", "0"
#: and an empty string -- is no, because a gate that opens on any non-empty
#: value is not a gate.
TRUTHY = frozenset({"1", "true", "yes", "on"})


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in TRUTHY


@dataclass(frozen=True, slots=True)
class DealingConfig:
    """Whether orders may be placed, and against which account."""

    enabled: bool = False
    allow_live: bool = False

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "DealingConfig":
        import os

        source = os.environ if env is None else env
        return cls(
            enabled=_truthy(source.get(ENABLED_VAR)),
            allow_live=_truthy(source.get(ALLOW_LIVE_VAR)),
        )

    def refusal(self, broker: IgConfig) -> str:
        """Why an order must not be placed, or empty if it may be.

        Checked before every order rather than once at start-up: a process
        that read the gate hours ago and has been running since is not
        evidence of what the gate says now.
        """
        if not self.enabled:
            return f"dealing is off; set {ENABLED_VAR}=1 to enable it"
        if broker.is_live and not self.allow_live:
            return (
                f"refusing to deal against a LIVE account; {ALLOW_LIVE_VAR} "
                "is not set"
            )
        return ""


@dataclass(frozen=True, slots=True)
class DealResult:
    """What happened to one order. Never a guess.

    ``level`` is the price actually filled, which is the entire reason this
    module exists. ``None`` means the deal did not fill or did not say --
    never zero, and never the price that was asked for.
    """

    epic: str = ""
    direction: str = ""
    size: float | None = None
    deal_reference: str = ""
    deal_id: str = ""
    status: str = ""
    level: float | None = None
    reason: str = ""
    error: str = ""
    placed_at: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ACCEPTED" and self.level is not None

    def as_row(self) -> dict[str, Any]:
        from dataclasses import asdict

        return asdict(self)


@dataclass(frozen=True, slots=True)
class SpreadProbe:
    """A round trip, and the spread it revealed."""

    epic: str = ""
    ticker: str = ""
    opened_at_level: float | None = None
    closed_at_level: float | None = None
    spread_points: float | None = None
    spread_bps: float | None = None
    size: float | None = None
    error: str = ""
    measured_at: str = ""

    @property
    def ok(self) -> bool:
        return self.error == "" and self.spread_bps is not None

    def as_row(self) -> dict[str, Any]:
        from dataclasses import asdict

        return asdict(self)


def place_order(
    epic: str,
    direction: str,
    size: float,
    session: IgSession,
    broker: IgConfig | None = None,
    dealing: DealingConfig | None = None,
    *,
    currency: str = "USD",
    opener=None,
    now=None,
) -> DealResult:
    """Open a position at market. Never raises.

    Refuses before it does anything if the gates are shut, and says which
    one. The refusal is checked here rather than by the caller, because a
    safety check a caller can forget is not a safety check.
    """
    gates = dealing or DealingConfig.from_env()
    settings = broker or IgConfig.from_env()

    refusal = gates.refusal(settings)
    if refusal:
        return DealResult(epic=epic, direction=direction, size=size, error=refusal)

    side = str(direction or "").strip().upper()
    if side not in ("BUY", "SELL"):
        return DealResult(
            epic=epic, direction=direction, size=size,
            error=f"direction must be BUY or SELL, got {direction!r}",
        )
    if not session.ok:
        return DealResult(
            epic=epic, direction=side, size=size,
            error=session.error or "no session",
        )

    read = opener or _read
    body = {
        "epic": str(epic),
        "expiry": "-",
        "direction": side,
        "size": str(size),
        "orderType": "MARKET",
        "guaranteedStop": False,
        "forceOpen": True,
        "currencyCode": currency,
    }
    logger.info(
        "IG dealing: %s %s size %s on %s", side, epic, size, settings.environment
    )
    try:
        document, _headers = read(
            f"{session.host}/positions/otc",
            data=body,
            headers=session.headers(settings.api_key, "2"),
            method="POST",
        )
    except urllib.error.HTTPError as error:
        detail = ""
        try:
            detail = json.loads(error.read().decode() or "{}").get("errorCode", "")
        except Exception:  # noqa: BLE001
            pass
        return DealResult(
            epic=epic, direction=side, size=size,
            error=f"HTTP {error.code}" + (f": {detail}" if detail else ""),
        )
    except Exception as error:  # noqa: BLE001
        return DealResult(
            epic=epic, direction=side, size=size,
            error=f"{type(error).__name__}: {error}",
        )

    reference = str((document or {}).get("dealReference") or "")
    if not reference:
        return DealResult(
            epic=epic, direction=side, size=size,
            error="the order returned no deal reference",
        )
    return DealResult(
        epic=epic, direction=side, size=size, deal_reference=reference,
        status="SUBMITTED", placed_at=str(now or ""),
    )


def confirm_deal(
    reference: str,
    session: IgSession,
    broker: IgConfig | None = None,
    *,
    opener=None,
) -> DealResult:
    """What became of a submitted order, including the level it filled at.

    A deal reference is a receipt for asking, not for getting. IG accepts
    the request and decides afterwards, so a caller that treated a
    reference as a fill would be recording positions that were rejected.
    """
    settings = broker or IgConfig.from_env()
    read = opener or _read
    try:
        document, _headers = read(
            f"{session.host}/confirms/{reference}",
            headers=session.headers(settings.api_key, "1"),
        )
    except urllib.error.HTTPError as error:
        return DealResult(deal_reference=reference, error=f"HTTP {error.code}")
    except Exception as error:  # noqa: BLE001
        return DealResult(
            deal_reference=reference, error=f"{type(error).__name__}: {error}"
        )

    status = str((document or {}).get("dealStatus") or "")
    return DealResult(
        epic=str((document or {}).get("epic") or ""),
        direction=str((document or {}).get("direction") or ""),
        size=_number((document or {}).get("size")),
        deal_reference=reference,
        deal_id=str((document or {}).get("dealId") or ""),
        status=status,
        level=_number((document or {}).get("level")),
        reason=str((document or {}).get("reason") or ""),
        error="" if status == "ACCEPTED" else f"deal {status or 'unknown'}",
    )


def close_position(
    deal_id: str,
    direction: str,
    size: float,
    session: IgSession,
    broker: IgConfig | None = None,
    dealing: DealingConfig | None = None,
    *,
    opener=None,
) -> DealResult:
    """Close by deal id. ``direction`` is the *closing* side.

    Gated identically to opening. Closing is the safer half of a round trip
    but it is still an order, and a module that guarded one and not the
    other would be a module whose safety depended on which function you
    happened to call.
    """
    gates = dealing or DealingConfig.from_env()
    settings = broker or IgConfig.from_env()

    refusal = gates.refusal(settings)
    if refusal:
        return DealResult(deal_id=deal_id, error=refusal)
    if not session.ok:
        return DealResult(deal_id=deal_id, error=session.error or "no session")

    read = opener or _read
    headers = session.headers(settings.api_key, "1")
    # IG closes through a POST carrying a delete override, because their
    # DELETE does not take a body.
    headers["_method"] = "DELETE"
    try:
        document, _headers = read(
            f"{session.host}/positions/otc",
            data={
                "dealId": str(deal_id),
                "direction": str(direction).upper(),
                "size": str(size),
                "orderType": "MARKET",
            },
            headers=headers,
            method="POST",
        )
    except urllib.error.HTTPError as error:
        detail = ""
        try:
            detail = json.loads(error.read().decode() or "{}").get("errorCode", "")
        except Exception:  # noqa: BLE001
            pass
        return DealResult(
            deal_id=deal_id,
            error=f"HTTP {error.code}" + (f": {detail}" if detail else ""),
        )
    except Exception as error:  # noqa: BLE001
        return DealResult(deal_id=deal_id, error=f"{type(error).__name__}: {error}")

    reference = str((document or {}).get("dealReference") or "")
    if not reference:
        return DealResult(deal_id=deal_id, error="the close returned no reference")
    return DealResult(deal_id=deal_id, deal_reference=reference, status="SUBMITTED")


def measure_spread(
    epic: str,
    session: IgSession,
    broker: IgConfig | None = None,
    dealing: DealingConfig | None = None,
    *,
    ticker: str = "",
    size: float = 1.0,
    currency: str = "USD",
    settle: float = 1.0,
    opener=None,
    sleep=time.sleep,
    now=None,
) -> SpreadProbe:
    """Buy, then sell, and report what the round trip cost.

    This is the point of the module. A buy fills at the offer and a sell
    fills at the bid, so the two levels bracket the spread that was
    actually charged -- no quote, no entitlement, no per-class constant.

    It costs the spread to run, which on a demo account is nothing and on a
    live one is exactly the number being measured.

    Every failure is returned rather than raised, and a probe that opened
    but could not close says so loudly: a position left open by a
    measurement is the worst outcome here, worse than no measurement.
    """
    opened = place_order(
        epic, "BUY", size, session, broker, dealing,
        currency=currency, opener=opener, now=now,
    )
    if opened.error:
        return SpreadProbe(epic=epic, ticker=ticker, size=size, error=opened.error)

    sleep(settle)
    filled = confirm_deal(opened.deal_reference, session, broker, opener=opener)
    if not filled.ok:
        return SpreadProbe(
            epic=epic, ticker=ticker, size=size,
            error=filled.error or "the opening deal did not fill",
        )

    shut = close_position(
        filled.deal_id, "SELL", size, session, broker, dealing, opener=opener
    )
    if shut.error:
        return SpreadProbe(
            epic=epic, ticker=ticker, size=size,
            opened_at_level=filled.level,
            error=(
                f"OPENED BUT NOT CLOSED -- deal {filled.deal_id} is still "
                f"open: {shut.error}"
            ),
        )

    sleep(settle)
    closed = confirm_deal(shut.deal_reference, session, broker, opener=opener)
    if not closed.ok:
        return SpreadProbe(
            epic=epic, ticker=ticker, size=size,
            opened_at_level=filled.level,
            error=(
                f"OPENED BUT NOT CONFIRMED CLOSED -- deal {filled.deal_id} "
                f"may still be open: {closed.error or 'no confirmation'}"
            ),
        )

    bought, sold = filled.level, closed.level
    spread_points = spread_bps = None
    if bought is not None and sold is not None and bought > 0 and sold > 0:
        spread_points = bought - sold
        mid = (bought + sold) / 2.0
        spread_bps = spread_points / mid * 10_000.0 if mid > 0 else None

    return SpreadProbe(
        epic=epic, ticker=ticker,
        opened_at_level=bought, closed_at_level=sold,
        spread_points=spread_points, spread_bps=spread_bps,
        size=size, measured_at=str(now or ""),
    )
