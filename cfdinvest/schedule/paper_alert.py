"""The email that says what the paper book just did.

Built from the paper book's own events rather than from the advisor's
separate suggestion logic, and that choice is the whole point. The reason
these are sent is so a selection can be placed by hand on the Trading 212
virtual account and compared against the simulated ones; an email describing
a *different* decision from the one the book recorded would compare two
things that were never the same.

So: one email per pass, listing what opened and what closed, with enough on
each line to place or close it without opening the dashboard. No daily
performance summary -- that lives on the dashboard, and an email nobody
needs to act on trains you to stop reading the ones you do.

Nothing here places an order. It writes an email describing what a
simulation did.
"""

from __future__ import annotations

import html
import logging
from typing import Any, Iterable, Mapping, Sequence

logger = logging.getLogger(__name__)

__all__ = ["build_paper_alert", "send_paper_alert"]

#: Colour per event, in both directions. Chosen to survive a dark-mode mail
#: client: mid-tone hues that stay legible on white and on near-black,
#: rather than the pale greens that vanish on one and the deep ones that
#: vanish on the other.
COLOURS: dict[str, str] = {
    "long": "#0E7C77",
    "short": "#B4531A",
    "close": "#5B6B78",
}

#: An icon per event. Text glyphs rather than images: a remote image in an
#: email is blocked by default in most clients and leaves a broken box where
#: the meaning was.
ICONS: dict[str, str] = {"long": "▲", "short": "▼", "close": "■"}


def _t212_name(ticker: str) -> str:
    """What to search for in the Trading 212 app, or the ticker itself."""
    try:
        from cfdinvest.livehelper import t212_instruments

        held = t212_instruments.mapping_for(str(ticker))
        return held.name if getattr(held, "tradable", False) else str(ticker)
    except Exception:  # noqa: BLE001 - a missing mapping is not worth a stall
        return str(ticker)


def _money(value: Any, places: int = 2) -> str:
    try:
        return f"£{float(value):,.{places}f}"
    except (TypeError, ValueError):
        return "—"


def _number(value: Any, places: int = 4) -> str:
    try:
        return f"{float(value):,.{places}f}"
    except (TypeError, ValueError):
        return "—"


def _opens(events: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [e for e in events if str(e.get("event")) == "open"]


def _closes(events: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [e for e in events if str(e.get("event")) == "close"]


def _plain(events: Sequence[Mapping[str, Any]]) -> str:
    """The real message. The HTML is a nicer rendering of exactly this."""
    lines: list[str] = []

    opened = _opens(events)
    if opened:
        lines.append("OPEN")
        for row in opened:
            side = str(row.get("direction") or "").upper()
            name = _t212_name(str(row.get("ticker")))
            lines.append(
                f"  {side} {row.get('ticker')} ({name})"
            )
            lines.append(
                f"    {_number(row.get('quantity'))} units at "
                f"{_number(row.get('price'))} · "
                f"{_money(row.get('notional_gbp'), 0)} exposure · "
                f"margin {_money(row.get('margin_gbp'))}"
            )
            confidence = row.get("confidence")
            arm = row.get("cohort") or ""
            detail = f"    confidence {float(confidence):.0%}" if confidence else ""
            if arm:
                detail += f" · arm {arm}"
            if detail:
                lines.append(detail)
        lines.append("")

    shut = _closes(events)
    if shut:
        lines.append("CLOSE")
        for row in shut:
            side = str(row.get("direction") or "").upper()
            lines.append(f"  {side} {row.get('ticker')} — {row.get('reason') or 'exit'}")
            net = row.get("net_pnl_gbp")
            if net is not None:
                held = row.get("days_held")
                lines.append(
                    f"    {_money(net)} after costs"
                    + (f" · held {float(held) * 24:.1f} hours" if held else "")
                )
        lines.append("")

    lines.append(
        "These are simulated. Nothing has been placed. Place a selection by "
        "hand on the virtual account to compare."
    )
    return "\n".join(lines)


def _row_html(row: Mapping[str, Any], kind: str) -> str:
    ticker = html.escape(str(row.get("ticker") or "?"))
    side = str(row.get("direction") or "").lower()
    key = "close" if kind == "close" else side
    colour = COLOURS.get(key, COLOURS["close"])
    icon = ICONS.get(key, "■")

    if kind == "open":
        action = f"{side.upper()} {ticker}"
        detail = (
            f"{_number(row.get('quantity'))} units @ {_number(row.get('price'))}"
            f" &middot; {_money(row.get('notional_gbp'), 0)} exposure"
            f" &middot; margin {_money(row.get('margin_gbp'))}"
        )
        name = html.escape(_t212_name(str(row.get("ticker"))))
        confidence = row.get("confidence")
        badge = f"{float(confidence):.0%}" if confidence else "&mdash;"
        aside = f"Trading 212: <b>{name}</b> &middot; arm {html.escape(str(row.get('cohort') or ''))}"
    else:
        action = f"CLOSE {side.upper()} {ticker}"
        net = row.get("net_pnl_gbp")
        held = row.get("days_held")
        detail = (
            f"{_money(net)} after costs"
            + (f" &middot; held {float(held) * 24:.1f}h" if held else "")
        )
        badge = html.escape(str(row.get("reason") or "exit"))
        aside = ""
        if net is not None and float(net) > 0:
            colour = COLOURS["long"]

    return (
        f'<tr>'
        f'<td style="padding:10px 8px;border-left:4px solid {colour};'
        f'background:#FAFBFC;">'
        f'<div style="font:600 15px/1.3 -apple-system,Segoe UI,Roboto,sans-serif;'
        f'color:{colour};">{icon}&nbsp; {html.escape(action)}</div>'
        f'<div style="font:13px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;'
        f'color:#33404D;">{detail}</div>'
        + (
            f'<div style="font:12px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;'
            f'color:#5B6B78;">{aside}</div>' if aside else ""
        )
        + f'</td>'
        f'<td style="padding:10px 8px;text-align:right;white-space:nowrap;'
        f'background:#FAFBFC;font:600 13px -apple-system,Segoe UI,Roboto,sans-serif;'
        f'color:{colour};">{badge}</td>'
        f'</tr>'
    )


def _html(events: Sequence[Mapping[str, Any]]) -> str:
    """A table, inline-styled.

    Inline because every mail client strips <style> blocks, and a table
    because float and flex layouts collapse in Outlook. This is 2004 HTML on
    purpose.
    """
    blocks: list[str] = []

    for title, rows, kind in (
        ("Open these", _opens(events), "open"),
        ("Close these", _closes(events), "close"),
    ):
        if not rows:
            continue
        body = "".join(_row_html(row, kind) for row in rows)
        blocks.append(
            f'<h2 style="font:700 13px/1 -apple-system,Segoe UI,Roboto,sans-serif;'
            f'letter-spacing:.08em;text-transform:uppercase;color:#5B6B78;'
            f'margin:22px 0 8px;">{title}</h2>'
            f'<table role="presentation" cellpadding="0" cellspacing="0" '
            f'border="0" width="100%" style="border-collapse:separate;'
            f'border-spacing:0 6px;">{body}</table>'
        )

    return (
        '<div style="max-width:640px;margin:0 auto;padding:8px 12px;'
        'font-family:-apple-system,Segoe UI,Roboto,sans-serif;color:#1F2833;">'
        + "".join(blocks)
        + '<p style="font:12px/1.6 -apple-system,Segoe UI,Roboto,sans-serif;'
        'color:#5B6B78;border-top:1px solid #E3E8EC;margin-top:22px;'
        'padding-top:12px;">These are <b>simulated</b>. Nothing has been '
        'placed and this system has no broker connection. Place a selection '
        'by hand on the virtual account to compare against the paper book.'
        '</p></div>'
    )


def build_paper_alert(events: Iterable[Mapping[str, Any]], config=None):
    """One email describing this pass. ``None`` when there is nothing to say.

    Silence is the default. A pass that opened and closed nothing is the
    common case -- most bars clear no confidence bar at all -- and an email
    saying so every five minutes is how the ones that matter get filtered
    into a folder nobody opens.
    """
    from cfdinvest.schedule.notify import SmtpConfig, _compose

    rows = [dict(row) for row in (events or ())]
    if not rows:
        return None

    opened, shut = len(_opens(rows)), len(_closes(rows))
    if not opened and not shut:
        return None

    parts = []
    if opened:
        parts.append(f"{opened} to open")
    if shut:
        parts.append(f"{shut} to close")
    subject = "Paper signals: " + ", ".join(parts)

    return _compose(
        config or SmtpConfig(), subject, _plain(rows), _html(rows)
    )


def send_paper_alert(events, config=None, transport=None) -> bool:
    """Send it, if there is anything to send. Never raises."""
    from cfdinvest.schedule import notify

    try:
        message = build_paper_alert(events, config)
        if message is None:
            return False
        return notify.send(message, config=config, transport=transport)
    except Exception as error:  # noqa: BLE001 - a mail failure is not an outage
        logger.warning("Could not send the paper alert: %s", error)
        return False
