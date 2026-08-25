"""The rehearsal as an email.

Every block says it is a rehearsal. The subject says it, the first line says
it, and the sign-off says it — repetition that would be clumsy anywhere else
and is the point here, because a forwarded excerpt read as a real result is
the failure this layer can cause.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

__all__ = ["build_simulation_digest", "send_simulation_digest"]


def build_simulation_digest(run, config=None):
    """Compose the digest. Pure."""
    from cfdinvest.schedule.notify import SmtpConfig, _compose

    settings = config or SmtpConfig()
    result = getattr(run, "result", run)

    lines = [
        "SIMULATION — none of the figures below are real results.",
        "",
        getattr(result, "headline", ""),
        "",
    ]

    if getattr(result, "error", None):
        lines.append(f"The rehearsal did not complete: {result.error}")
        return _compose(settings, "Simulation — did not complete", "\n".join(lines))

    lines += ["THE COMPARISON", ""]
    for metric in ("final_equity", "sharpe_ratio", "win_rate", "max_drawdown"):
        baseline = result.baseline_metrics.get(metric)
        advised = result.advised_metrics.get(metric)
        if baseline is None and advised is None:
            continue
        lines.append(
            f"  {metric:<16} as executed {_number(baseline)}   "
            f"as advised {_number(advised)}"
        )
    lines.append("")
    lines.append(
        f"  The helper would have changed {len(result.adjustments)} position "
        "size(s)."
    )
    if len(result.forced_closures):
        lines.append(
            f"  The book would have crossed the platform's margin ratio on "
            f"{len(result.forced_closures)} bar(s)."
        )
    lines.append("")

    for note in getattr(result, "notes", ()) or ():
        lines.append(f"  ~ {note}")
    for note in getattr(run, "notes", ()) or ():
        lines.append(f"  ~ {note}")

    lines += [
        "",
        "This is a rehearsal of a decision process over a record that has "
        "already happened. It is not a forecast, and following the advice "
        "costing money on one run is not evidence that it would on the next.",
        "",
        "No order was placed and no money moved. These are not your results.",
    ]

    return _compose(
        settings, "SIMULATION — rehearsal of the advice", "\n".join(lines)
    )


def _number(value) -> str:
    try:
        return f"{float(value):>12,.2f}"
    except (TypeError, ValueError):
        return f"{'not computed':>12}"


def send_simulation_digest(run, config=None, transport=None):
    """Send the digest. Returns whether it went."""
    from cfdinvest.schedule.notify import send

    return send(build_simulation_digest(run, config), config, transport)
