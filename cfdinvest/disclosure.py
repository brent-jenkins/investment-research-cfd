"""What every outbound message says about what this system can do.

One owner for a sentence that appeared in four places and was wrong in all
of them. Until 2026-08-27 the footer read "This system simulates trades and
places no orders with any broker", which was true when it was written. The
daemon has been dealing on a Capital.com demo account since that date, and
the reassurance survived unchanged in the bias digest, the insights digest,
the journal digest and the operational alerts.

A stale reassurance is worse than none. Somebody reading an alert at three
in the morning is entitled to know whether the thing that just warned them
can place an order, and four copies of a claim is four chances for it to
rot -- which is exactly what happened.

Deliberately a leaf module with no imports. ``bias``, ``insights`` and
``journal`` are forbidden by their own tests from importing the scheduler,
so the shared text cannot live beside the notifier that sends it.
"""

from __future__ import annotations

__all__ = ["DEALING_NOTICE", "dealing_notice"]

#: The standing description of what this system does with money.
#:
#: Stated in terms of the *gate* rather than the current setting, because a
#: message describing configuration would need updating the day the
#: configuration changed -- which is the failure this constant replaces.
DEALING_NOTICE = (
    "This system deals on a Capital.com demo account. It cannot reach a "
    "live account without a second, separate environment setting, and no "
    "message from it has moved real money."
)


def dealing_notice(live_armed: bool | None = None) -> str:
    """The notice, told plainly if a live account is ever armed.

    ``None`` means the caller did not check, which is reported as such
    rather than assumed safe: "not checked" and "checked and demo" are
    different statements, and only one of them is reassuring. That
    distinction is this project's oldest rule and it matters most here,
    because the reader is deciding whether to worry.
    """
    if live_armed is None:
        return DEALING_NOTICE
    if live_armed:
        return (
            "WARNING: this system is armed against a LIVE account. Orders it "
            "places move real money."
        )
    return DEALING_NOTICE
