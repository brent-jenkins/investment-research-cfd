"""The behavioural report as an email.

Written to be read once and thought about, not to be acted on immediately.
Every asserted pattern carries its innocent reading in the same block as its
evidence, because separating them is how a caveat gets skipped.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

__all__ = ["build_bias_digest", "send_bias_digest"]


def build_bias_digest(report, config=None):
    """Compose the digest. Pure."""
    from cfdinvest.schedule.notify import SmtpConfig, _compose

    settings = config or SmtpConfig()
    lines = [getattr(report, "headline", ""), ""]

    asserted = getattr(report, "asserted", ()) or ()
    measured = [f for f in getattr(report, "measured", ()) if f not in asserted]
    unmeasured = getattr(report, "unmeasured", ()) or ()

    if asserted:
        lines += ["PATTERNS THAT STAND OUT", ""]
        for finding in asserted:
            lines.append(
                f"  {finding.title}  [{finding.confidence}, "
                f"n={finding.observations}, {finding.severity:.0%}]"
            )
            lines.append(f"    {finding.evidence}")
            if finding.innocent_reading:
                lines.append(f"    But: {finding.innocent_reading}")
            if finding.action:
                lines.append(f"    -> {finding.action}")
            lines.append("")

    if measured:
        lines += ["LOOKED AT, NOTHING PRONOUNCED", ""]
        for finding in measured:
            lines.append(
                f"  {finding.title} — {finding.severity:.0%} over "
                f"{finding.observations} observations."
            )
        lines.append("")

    if unmeasured:
        lines += ["NOT ENOUGH RECORD TO LOOK", ""]
        lines.append(
            "  These could not be measured. That is not the same as finding "
            "nothing, and it is certainly not a clean bill."
        )
        lines.append("")
        for finding in unmeasured:
            reason = finding.details.get("reason", "")
            lines.append(f"  {finding.title} — {reason}")
        lines.append("")

    lines += [
        "These are patterns in a trade record, not conclusions about you. The "
        "same numbers are usually consistent with having meant to, which is "
        "why every one of them comes with the other reading.",
        "",
        "This system simulates trades and places no orders with any broker.",
    ]

    subject = (
        f"Behaviour — {len(asserted)} pattern(s) standing out"
        if asserted
        else "Behaviour — nothing pronounced"
    )
    return _compose(settings, subject, "\n".join(lines))


def send_bias_digest(report, config=None, transport=None):
    """Send the digest. Returns whether it went."""
    from cfdinvest.schedule.notify import send

    return send(build_bias_digest(report, config), config, transport)
