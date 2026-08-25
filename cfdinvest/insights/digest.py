"""The insight report as an email.

Ordered so the confident findings come first and the "not enough record yet"
lines come last, under a heading that says what they are. Burying the sample
warnings would turn a report of two findings and seven absences into what
looks like nine findings.

Pure: a report in, an :class:`~email.message.EmailMessage` out.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

__all__ = ["build_insight_digest", "send_insight_digest"]

_ORDER = {"confident": 0, "provisional": 1, "insufficient": 2}


def build_insight_digest(report, config=None):
    """Compose the digest. Pure."""
    from cfdinvest.schedule.notify import SmtpConfig, _compose

    settings = config or SmtpConfig()
    insights = sorted(
        getattr(report, "insights", ()) or (),
        key=lambda item: (_ORDER.get(item.confidence, 9), item.title),
    )

    lines = [getattr(report, "headline", ""), ""]

    said = [item for item in insights if item.usable]
    unsaid = [item for item in insights if not item.usable]

    if said:
        lines.append("WHAT THE RECORD SUPPORTS")
        lines.append("")
        for item in said:
            lines.append(f"  {item.title}  [{item.confidence}, n={item.trades}]")
            lines.append(f"    {item.finding}")
            for caveat in item.caveats:
                lines.append(f"    ~ {caveat}")
            lines.append("")

    if unsaid:
        lines.append("NOT ENOUGH RECORD YET")
        lines.append("")
        lines.append(
            "  These were asked and could not be answered. That is different "
            "from being answered with nothing."
        )
        lines.append("")
        for item in unsaid:
            lines.append(f"  {item.title}")
            for caveat in item.caveats:
                lines.append(f"    ~ {caveat}")
        lines.append("")

    lines.append(
        "Every figure here is a slice of one record. Nine questions asked of "
        "the same trades will turn up a striking answer or two by chance, so "
        "treat an ordering as a question rather than a result."
    )
    lines.append("")
    lines.append(
        "This system simulates trades and places no orders with any broker."
    )

    subject = f"Insights — {len(said)} of {len(insights)} supported"
    return _compose(settings, subject, "\n".join(lines))


def send_insight_digest(report, config=None, transport=None):
    """Send the digest. Returns whether it went."""
    from cfdinvest.schedule.notify import send

    return send(build_insight_digest(report, config), config, transport)
