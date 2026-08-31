"""The journal as an email you can read on a phone.

A digest of what you did over a period, not an alert. Sent when asked for
rather than on a trigger: a journal that emailed itself after every trade
would be a notification, and notifications get filtered.

Pure: entries in, an :class:`~email.message.EmailMessage` out. Composed
through :mod:`cfdinvest.schedule.notify` so the subject prefix, the account
suffix and the recipient list are decided in one place.
"""

from __future__ import annotations

from cfdinvest.disclosure import DEALING_NOTICE
import logging
from typing import Sequence

logger = logging.getLogger(__name__)

__all__ = ["build_journal_digest", "send_journal_digest"]


def build_journal_digest(entries: Sequence, config=None, *, period: str = "recent"):
    """Compose the digest. Pure."""
    from cfdinvest.journal.reader import render_entry
    from cfdinvest.schedule.notify import SmtpConfig, _compose

    settings = config or SmtpConfig()

    if not entries:
        return _compose(
            settings,
            f"Journal — {period}",
            "Nothing was journalled in this period.\n\n"
            "That is not the same as no trades: entries are written from "
            "confirmations, so an unconfirmed trade leaves no entry.\n",
        )

    ordered = sorted(entries, key=lambda e: str(e.timestamp))
    kinds: dict[str, int] = {}
    for entry in ordered:
        kinds[entry.kind] = kinds.get(entry.kind, 0) + 1

    lines = [
        f"{len(ordered)} journal entr{'y' if len(ordered) == 1 else 'ies'}, "
        f"{period}.",
        "",
        "  " + ", ".join(f"{count} {kind}" for kind, count in sorted(kinds.items())),
        "",
    ]

    for entry in ordered:
        lines.extend(render_entry(entry))
        lines.append("")
        lines.append("-" * 68)
        lines.append("")

    lines.append(
        "Entries are written from what was known at the time and are never "
        "rewritten. Where an outcome is recorded it sits as its own entry, "
        "beside the decision rather than over it."
    )
    lines.append("")
    lines.append(
        DEALING_NOTICE
    )

    subject = f"Journal — {len(ordered)} entr{'y' if len(ordered) == 1 else 'ies'}"
    return _compose(settings, subject, "\n".join(lines))


def send_journal_digest(entries, config=None, transport=None, *, period: str = "recent"):
    """Send the digest. Returns whether it went."""
    from cfdinvest.schedule.notify import send

    return send(build_journal_digest(entries, config, period=period), config, transport)
