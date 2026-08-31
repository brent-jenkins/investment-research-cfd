"""Placing and closing positions on Capital.com. This module can trade.

It is named so that nothing imports it by accident, and it is the only file
in this package that can move anything. ``client.py`` beside it has no
dealing function at all -- not a disabled one, one that does not exist.

**Two gates, checked inside every order rather than once at start-up.**

* ``CFD_CAPITAL_DEALING_ENABLED`` must be explicitly truthy, and "truthy" is
  a closed set. ``maybe``, ``2`` and ``yes please`` are refusals. A gate that
  opens on any non-empty value is not a gate.
* A **live** account needs ``CFD_CAPITAL_ALLOW_LIVE`` as well. One switch is
  a typo away from a funded account; two mean the mistake has to be made
  twice, in different words.

Closing is gated identically to opening. Safety that depends on which
function you happened to call is not safety -- but the refusal to *close* is
the more dangerous of the two, so a close that is refused says loudly that a
position is still open rather than returning quietly.

**A deal reference is not a fill.** Capital.com accepts the request and
decides afterwards: ``POST /positions`` returns a ``dealReference``, and only
``GET /confirms/{dealReference}`` says whether it became a position, at what
level, and if not, why. A caller that treated the reference as success would
record fills that never happened -- which is precisely the failure the paper
book exists to avoid, reintroduced through the broker.

Sizing is not this module's decision. It takes a quantity and places it. What
the quantity should be comes from the balance through the risk limits, and
keeping that out of here means the thing that can trade holds no opinion
about how much.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Mapping

from cfdinvest.broker.capital.client import (
    CapitalConfig,
    CapitalSession,
    _read,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DealingConfig",
    "DealResult",
    "OpenPosition",
    "place_position",
    "close_position",
    "list_positions",
]

DEALING_VAR = "CFD_CAPITAL_DEALING_ENABLED"
ALLOW_LIVE_VAR = "CFD_CAPITAL_ALLOW_LIVE"

#: A closed set, deliberately. Anything else is a refusal, including "2",
#: "maybe" and "enabled" -- a switch that opens on any non-empty string is a
#: switch somebody turns on by pasting the wrong line into an env file.
TRUTHY = frozenset({"1", "true", "yes", "on"})


def _enabled(value: str | None) -> bool:
    return str(value or "").strip().lower() in TRUTHY


@dataclass(frozen=True, slots=True)
class DealingConfig:
    """Whether dealing is permitted, and against which account."""

    broker: CapitalConfig = field(default_factory=CapitalConfig)
    dealing_enabled: bool = False
    allow_live: bool = False

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "DealingConfig":
        import os

        source = os.environ if env is None else env
        return cls(
            broker=CapitalConfig.from_env(source),
            dealing_enabled=_enabled(source.get(DEALING_VAR)),
            allow_live=_enabled(source.get(ALLOW_LIVE_VAR)),
        )

    def refusal(self) -> str:
        """Why dealing is not permitted, or an empty string when it is.

        Both gates, every time. Evaluated per order rather than cached at
        start-up, so a daemon that has been running for a week is still
        subject to the environment it is running in now.
        """
        if not self.dealing_enabled:
            return f"dealing is off: {DEALING_VAR} is not set to one of {sorted(TRUTHY)}"
        if self.broker.is_live and not self.allow_live:
            return (
                f"this is a LIVE account and {ALLOW_LIVE_VAR} is not set; "
                "refusing"
            )
        return ""


@dataclass(frozen=True, slots=True)
class DealResult:
    """What happened, as far as the broker has said."""

    reference: str = ""
    deal_id: str = ""
    epic: str = ""
    direction: str = ""
    size: float | None = None
    level: float | None = None
    status: str = ""
    reason: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        """The broker confirmed the deal did what it was asked to do.

        Deliberately asymmetric, because "it worked" is a different fact for
        an open and for a close. This used to read ``status == "OPEN" and
        level is not None`` -- correct for an open, and false for every
        successful close, because a closed position confirms as CLOSED and
        has no open level.

        That was not a cosmetic bug. Eight stranded positions were closed
        correctly on 2026-08-31 and the pass reported "0 placed, 8 failed"
        with the subject "FAILED -- the account may not match the record".
        The account matched the record exactly. A false alarm on the one
        alert that says whether the book and the broker agree is as
        corrosive as a false all-clear, and harder to notice, because
        nobody investigates being told the worse news.

        Safe to read the status this way: ``place_position`` normalises a
        successful open to OPEN and ``close_position`` normalises a
        successful close to CLOSED, while a rejection keeps the broker's own
        word (REJECTED and the rest), so no failure can present as either.

        An open additionally requires a level and a close does not. A
        position with no fill price is a reference the broker has not turned
        into anything, and treating that as filled is the failure this whole
        module is shaped around. A close has no such price to carry.
        """
        if self.error:
            return False
        if self.status == "CLOSED":
            return True
        return self.status == "OPEN" and self.level is not None


@dataclass(frozen=True, slots=True)
class OpenPosition:
    """A position the broker says is open."""

    deal_id: str = ""
    epic: str = ""
    direction: str = ""
    size: float | None = None
    level: float | None = None
    upl: float | None = None
    currency: str = ""


def _number(value) -> float | None:
    try:
        if value is None:
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _confirm(
    reference: str,
    session: CapitalSession,
    config: DealingConfig,
    *,
    opener=None,
) -> tuple[dict[str, Any], str]:
    """Ask what became of a deal reference."""
    read = opener or _read
    url = f"{session.host}/confirms/{urllib.parse.quote(reference, safe='')}"
    try:
        document, _ = read(url, headers=session.headers(config.broker.api_key))
        return document or {}, ""
    except urllib.error.HTTPError as error:
        return {}, f"HTTP {error.code} confirming {reference}"
    except Exception as error:  # noqa: BLE001
        return {}, f"{type(error).__name__} confirming {reference}: {error}"


def place_position(
    epic: str,
    direction: str,
    size: float,
    session: CapitalSession,
    config: DealingConfig | None = None,
    *,
    stop_level: float | None = None,
    opener=None,
) -> DealResult:
    """Open a position. Refuses unless both gates are open. Never raises."""
    settings = config or DealingConfig.from_env()

    refusal = settings.refusal()
    if refusal:
        return DealResult(epic=epic, direction=direction, size=size, error=refusal)
    if not session.ok:
        return DealResult(
            epic=epic, direction=direction, size=size,
            error=session.error or "no session",
        )

    side = str(direction).strip().upper()
    if side in ("LONG", "BUY"):
        side = "BUY"
    elif side in ("SHORT", "SELL"):
        side = "SELL"
    else:
        return DealResult(epic=epic, direction=direction, error=f"unknown direction {direction!r}")

    try:
        quantity = float(size)
    except (TypeError, ValueError):
        return DealResult(epic=epic, direction=side, error=f"unusable size {size!r}")
    if not quantity > 0:
        # Not an error to be smoothed over: a zero size means the sizing
        # step declined, and turning that into a minimum position would
        # override the refusal it was reporting.
        return DealResult(
            epic=epic, direction=side, size=quantity,
            error="size is not positive; the sizing step declined this trade",
        )

    body: dict[str, Any] = {"epic": str(epic), "direction": side, "size": quantity}
    if stop_level is not None:
        body["stopLevel"] = float(stop_level)

    read = opener or _read
    logger.info(
        "Capital %s: placing %s %s size %s",
        settings.broker.environment, side, epic, quantity,
    )
    try:
        document, _ = read(
            f"{session.host}/positions",
            data=json.dumps(body).encode("utf-8"),
            headers=session.headers(config.broker.api_key if config else settings.broker.api_key),
            method="POST",
        )
    except urllib.error.HTTPError as error:
        detail = ""
        try:
            detail = json.loads(error.read().decode("utf-8")).get("errorCode", "")
        except Exception:  # noqa: BLE001
            detail = ""
        return DealResult(
            epic=epic, direction=side, size=quantity,
            error=f"HTTP {error.code}{f' ({detail})' if detail else ''}",
        )
    except Exception as error:  # noqa: BLE001
        return DealResult(
            epic=epic, direction=side, size=quantity,
            error=f"{type(error).__name__}: {error}",
        )

    reference = str((document or {}).get("dealReference") or "")
    if not reference:
        return DealResult(
            epic=epic, direction=side, size=quantity,
            error="accepted with no dealReference, so nothing can be confirmed",
        )

    confirmation, error = _confirm(reference, session, settings, opener=opener)
    if error:
        # The order may well have become a position. Saying so is the point:
        # an unconfirmed placement is the one state that needs a human.
        return DealResult(
            reference=reference, epic=epic, direction=side, size=quantity,
            error=f"PLACED BUT UNCONFIRMED ({error}); check the account",
        )

    status = str(confirmation.get("dealStatus") or confirmation.get("status") or "")
    return DealResult(
        reference=reference,
        deal_id=str(confirmation.get("dealId") or ""),
        epic=str(confirmation.get("epic") or epic),
        direction=str(confirmation.get("direction") or side),
        size=_number(confirmation.get("size")) or quantity,
        level=_number(confirmation.get("level")),
        status="OPEN" if status.upper() in ("ACCEPTED", "OPEN", "OPENED") else status.upper(),
        reason=str(confirmation.get("reason") or ""),
    )


def close_position(
    deal_id: str,
    session: CapitalSession,
    config: DealingConfig | None = None,
    *,
    opener=None,
) -> DealResult:
    """Close one position by its deal id. Gated identically to opening.

    The refusal here is the more dangerous one. A refused *open* leaves the
    account as it was; a refused *close* leaves a position running that
    somebody believes is shut, so the reason says so in those words.
    """
    settings = config or DealingConfig.from_env()

    refusal = settings.refusal()
    if refusal:
        return DealResult(
            deal_id=deal_id,
            error=f"POSITION STILL OPEN -- close refused: {refusal}",
        )
    if not session.ok:
        return DealResult(
            deal_id=deal_id,
            error=(
                "POSITION STILL OPEN -- close refused: "
                + (session.error or "no session")
            ),
        )

    read = opener or _read
    logger.info("Capital %s: closing %s", settings.broker.environment, deal_id)
    try:
        document, _ = read(
            f"{session.host}/positions/{urllib.parse.quote(str(deal_id), safe='')}",
            headers=session.headers(settings.broker.api_key),
            method="DELETE",
        )
    except urllib.error.HTTPError as error:
        return DealResult(
            deal_id=deal_id,
            error=f"POSITION STILL OPEN -- HTTP {error.code} closing {deal_id}",
        )
    except Exception as error:  # noqa: BLE001
        return DealResult(
            deal_id=deal_id,
            error=(
                f"POSITION STILL OPEN -- {type(error).__name__} closing "
                f"{deal_id}: {error}"
            ),
        )

    reference = str((document or {}).get("dealReference") or "")
    if not reference:
        return DealResult(
            deal_id=deal_id,
            error="POSITION MAY STILL BE OPEN -- close accepted with no reference",
        )

    confirmation, error = _confirm(reference, session, settings, opener=opener)
    if error:
        return DealResult(
            reference=reference, deal_id=deal_id,
            error=f"POSITION MAY STILL BE OPEN -- unconfirmed close ({error})",
        )

    status = str(confirmation.get("dealStatus") or confirmation.get("status") or "")
    return DealResult(
        reference=reference,
        deal_id=str(confirmation.get("dealId") or deal_id),
        epic=str(confirmation.get("epic") or ""),
        direction=str(confirmation.get("direction") or ""),
        size=_number(confirmation.get("size")),
        level=_number(confirmation.get("level")),
        status="CLOSED" if status.upper() in ("ACCEPTED", "CLOSED") else status.upper(),
        reason=str(confirmation.get("reason") or ""),
    )


def list_positions(
    session: CapitalSession,
    config: CapitalConfig | DealingConfig | None = None,
    *,
    opener=None,
) -> tuple[list[OpenPosition], str]:
    """What the broker says is open. Reading, so no gate.

    Returns ``(positions, error)``. An error is *not* an empty book: "the
    account holds nothing" and "we could not ask" are different statements
    and only one of them means it is safe to open more.
    """
    broker = config.broker if isinstance(config, DealingConfig) else (
        config or CapitalConfig.from_env()
    )
    if not session.ok:
        return [], session.error or "no session"

    read = opener or _read
    try:
        document, _ = read(
            f"{session.host}/positions", headers=session.headers(broker.api_key)
        )
    except urllib.error.HTTPError as error:
        return [], f"HTTP {error.code}"
    except Exception as error:  # noqa: BLE001
        return [], f"{type(error).__name__}: {error}"

    found: list[OpenPosition] = []
    for entry in (document or {}).get("positions") or []:
        position = entry.get("position") or {}
        market = entry.get("market") or {}
        found.append(
            OpenPosition(
                deal_id=str(position.get("dealId") or ""),
                epic=str(market.get("epic") or ""),
                direction=str(position.get("direction") or ""),
                size=_number(position.get("size")),
                level=_number(position.get("level")),
                upl=_number(position.get("upl")),
                currency=str(position.get("currency") or ""),
            )
        )
    return found, ""
