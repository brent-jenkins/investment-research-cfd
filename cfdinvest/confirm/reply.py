"""Reading a confirmation out of a replied-to alert email.

The alerts already arrive by mail, so replying to one is the shortest path
from "I filled it" to a recorded confirmation::

    CONFIRM OPEN SPX500 PRICE=4434.8 QTY=0.684
    CONFIRM CLOSE NAS100 PRICE=18923.4

**This module parses text. It does not fetch mail.** There is no IMAP client,
no mailbox poller and no network call here — the project's rule is no external
API calls except SMTP, and reading a mailbox is not SMTP. The body is handed
in by whatever put it there: a pasted reply, a file an operator saved, a
procmail rule. That keeps the trust boundary somewhere a person can see it.

Why the parser is strict
------------------------
A confirmation overrides the engine's position. An email body is text that
arrived from outside, and a forwarded alert quoting the original instruction
contains the *recommendation* — which reads exactly like a confirmation if the
parser is relaxed about what a command looks like.

So: the command must be the first thing on its line, quoted lines are ignored,
every field must be named, and anything ambiguous is refused rather than
guessed. A refusal costs a retype. A wrong parse silently rewrites the book.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from cfdinvest.persist.confirm import ACTIONS, Confirmation

logger = logging.getLogger(__name__)

__all__ = [
    "ConfirmationCommand",
    "parse_confirmation_email",
    "parse_confirmation_line",
]

#: ``CONFIRM <ACTION> <TICKER> KEY=VALUE ...`` and nothing before it.
#:
#: The leading ``\s{0,3}`` is the whole defence against prefix-quoted lines: a
#: ``> CONFIRM ...`` or ``| CONFIRM ...`` from a reply chain cannot match,
#: because the prefix is not whitespace. Loosening this to ``\s*`` or dropping
#: the anchor would let a quoted recommendation through as a confirmation.
_COMMAND = re.compile(
    r"^\s{0,3}CONFIRM\s+(?P<action>[A-Z]+)\s+(?P<ticker>[A-Za-z0-9._-]{1,32})"
    r"(?P<rest>(?:\s+[A-Za-z_]+\s*=\s*[^\s]+)*)\s*$",
    re.IGNORECASE,
)
_FIELD = re.compile(r"([A-Za-z_]+)\s*=\s*([^\s]+)")

#: What each field is called in the model. Aliases are the words a person
#: actually types, not a second syntax to remember.
_FIELDS: dict[str, str] = {
    "price": "price",
    "fill": "price",
    "qty": "quantity",
    "quantity": "quantity",
    "size": "quantity",
    "close_price": "close_price",
    "closeprice": "close_price",
    "close": "close_price",
    "open_price": "price",
    "openprice": "price",
    "exposure": "exposure",
    "run": "run_id",
    "run_id": "run_id",
    "direction": "direction",
    "side": "direction",
}

#: The line above the original message. **Everything after it is quoted**,
#: whether or not the client prefixed it: Outlook and Apple Mail both drop the
#: original in below a separator with no ``>`` at all. A forwarded alert below
#: one of these carries a real CONFIRM line, and reading that as a fresh
#: instruction would rewrite the book with a fill nobody made.
_QUOTE_BEGINS = re.compile(
    r"^\s*("
    r"On\s.{0,200}?\swrote:"
    r"|-{2,}\s*(Original Message|Forwarded message)"
    r"|_{5,}"
    r"|From:\s"
    r"|Begin forwarded message:"
    r")",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ConfirmationCommand:
    """One parsed line, and what went wrong with the ones that failed."""

    confirmation: Confirmation | None = None
    line: str = ""
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.confirmation is not None


def parse_confirmation_line(
    line: str, *, timestamp=None, source: str = "email"
) -> ConfirmationCommand:
    """Parse one ``CONFIRM ...`` line. Never raises."""
    match = _COMMAND.match(line)
    if match is None:
        return ConfirmationCommand(line=line, error="not a CONFIRM command")

    action = match.group("action").lower()
    if action not in ACTIONS:
        return ConfirmationCommand(
            line=line,
            error=f"unknown action {action!r}; expected one of "
            f"{', '.join(a.upper() for a in ACTIONS)}",
        )

    fields: dict[str, Any] = {}
    for key, value in _FIELD.findall(match.group("rest") or ""):
        name = _FIELDS.get(key.lower())
        if name is None:
            return ConfirmationCommand(
                line=line, error=f"unknown field {key!r}"
            )
        if name in fields:
            return ConfirmationCommand(
                line=line, error=f"{name} given twice"
            )
        fields[name] = value

    direction = fields.pop("direction", None)
    for name in ("price", "quantity", "close_price", "exposure"):
        if name not in fields:
            continue
        try:
            # A thousands separator is what a person types; a currency symbol
            # is what they paste out of the ticket.
            fields[name] = float(
                str(fields[name]).replace(",", "").lstrip("£$€")
            )
        except ValueError:
            return ConfirmationCommand(
                line=line, error=f"{name} is not a number: {fields[name]!r}"
            )

    try:
        confirmation = Confirmation.of(
            match.group("ticker"),
            action,
            direction=direction,
            quantity=fields.pop("quantity", 0.0),
            source=source,
            timestamp=timestamp,
            **fields,
        )
    except (TypeError, ValueError) as error:
        return ConfirmationCommand(line=line, error=str(error))

    return ConfirmationCommand(confirmation=confirmation, line=line)


def parse_confirmation_email(
    body: str, *, timestamp=None, source: str = "email"
) -> list[ConfirmationCommand]:
    """Every ``CONFIRM`` command in a mail body, in order.

    Returns commands rather than confirmations so a caller can report the
    lines that failed. A body with one good command and one malformed one
    must not go through as a success — the sender believes both were recorded.

    **Parsing stops at the quoted section.** A reply carries the original
    alert below a separator, and the original alert is a recommendation; a
    command found down there was written by this system, not by you.

    Lines that are not commands at all are ignored silently, since a real
    reply has prose in it.
    """
    commands: list[ConfirmationCommand] = []
    for line in (body or "").splitlines():
        if _QUOTE_BEGINS.match(line):
            break
        if not re.match(r"^\s{0,3}CONFIRM\b", line, re.IGNORECASE):
            continue
        commands.append(
            parse_confirmation_line(line, timestamp=timestamp, source=source)
        )
    return commands
