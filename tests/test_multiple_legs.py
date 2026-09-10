"""One epic can be several positions, and a close must shut all of them.

Capital nets only OPPOSING trades. The account is in netting mode --
confirmed by the owner on 2026-09-03 and verified with a real order on
2026-09-04 -- but that means a BUY closes a SELL, not that two SELLs merge.
Three same-direction opens on one epic stay three positions with three deal
ids.

On 2026-09-10 the account held SPGI as three legs:

    17:36:52  SELL 1.99 @ 414.93
    18:18:36  SELL 8.64 @ 413.14
    18:40:14  SELL 8.67 @ 412.13    total 19.30 units

`_command_trade_capital` builds its view as ``{p.epic: ... for p in held}``
-- a dict keyed by epic -- so the three collapsed to whichever came last and
the bridge planned against 8.67 units where the account held 19.30.

Two consequences:

  * **It kept opening.** Each pass saw a third of the position, measured it
    against the target and added the difference again. That is what the
    three legs ARE.
  * **A close would have closed one leg.** `close_position` takes a single
    deal id, so the flatten before the bell would have shut 8.67 units and
    carried 10.63 overnight -- on a book that believed it was flat, in the
    configuration whose entire purpose is not to hold overnight.

The second is why this was fixed during the session rather than in the
morning. Only the close path is changed: closes are never refused, so this
cannot stop the account trading, and it is additive -- the named leg is shut
exactly as before and the remainder follow.

**The sizing half is NOT fixed here.** The collapsed dict still under-states
what is held, so the engine will still measure a fraction of a multi-leg
position against its target. That is a real fault and it is left for a
measured change rather than a second edit to the dealing path minutes
before a close.

Mutations checked, each reintroduced as a bug and confirmed to fail:
  M910 only the named leg closed, carrying the rest overnight
  M920 the named leg closed twice instead of the others
  M930 legs of a DIFFERENT epic closed, shutting positions nobody asked to
  M940 a malformed record raising instead of being skipped
"""

from __future__ import annotations

import inspect

from cfdinvest.schedule import app
from cfdinvest.schedule.app import _other_legs


class Position:
    def __init__(self, epic, deal_id):
        self.epic = epic
        self.deal_id = deal_id


def book():
    return [
        Position("SPGI", "spgi-1"),
        Position("SPGI", "spgi-2"),
        Position("SPGI", "spgi-3"),
        Position("LULU", "lulu-1"),
        Position("TMUS", "tmus-1"),
    ]


def test_every_other_leg_of_the_epic_is_returned():
    """M910: the leg the bridge named is shut by the caller; these are the rest."""
    assert sorted(_other_legs(book(), "SPGI", "spgi-2")) == ["spgi-1", "spgi-3"]


def test_the_named_leg_is_not_returned_again():
    """M920: closing the same deal twice is not closing the others.

    The caller has already shut it, and a second close of a closed deal is
    an error the log would carry as a failure.
    """
    assert "spgi-2" not in _other_legs(book(), "SPGI", "spgi-2")


def test_no_other_epic_is_touched():
    """M930: the dangerous direction.

    Shutting a position nobody asked to close is worse than leaving one
    open, because it is money moved on a mistake rather than exposure left
    where it was.
    """
    legs = _other_legs(book(), "SPGI", "spgi-2")

    assert all(leg.startswith("spgi") for leg in legs), legs
    assert "lulu-1" not in legs and "tmus-1" not in legs


def test_a_single_position_yields_nothing_extra():
    """The ordinary case must stay ordinary: one leg, one close."""
    assert _other_legs(book(), "LULU", "lulu-1") == []
    assert _other_legs(book(), "TMUS", "tmus-1") == []


def test_an_unreadable_book_never_blocks_a_close():
    """M940: a close that raises is worse than a close that is incomplete.

    This runs inside the order loop, after the named leg has already been
    shut. An exception here would abandon the remaining intents of the pass
    -- including other closes -- which is the opposite of what it is for.
    """
    assert _other_legs(None, "SPGI", "spgi-1") == []
    assert _other_legs([], "SPGI", "spgi-1") == []
    assert _other_legs([object()], "SPGI", "spgi-1") == []

    class Hostile:
        @property
        def epic(self):
            raise RuntimeError("the record is malformed")

    assert _other_legs([Hostile()], "SPGI", "spgi-1") == []


def test_the_close_path_shuts_the_remaining_legs():
    """The wiring, asserted on the source.

    The effect needs a broker to observe, and a helper that is never called
    looks identical to one that is -- which is this project's inert-gate
    failure, met three times today.
    """
    source = inspect.getsource(app._command_trade_capital)

    assert "for deal in _other_legs(" in source, (
        "the close path does not shut the remaining legs, so a multi-leg "
        "position is carried overnight on a book that believes it is flat"
    )
    close = source.index('if intent.action == "close":')
    call = source.index("_other_legs(")
    assert call > close, "the extra legs are handled outside the close branch"
    assert "record_result(" in source[call:call + 700], (
        "the extra closes are not recorded, so the order store will "
        "disagree with the account and halt the next pass"
    )
