"""Require a breakout before opening, but only where trends run.

A breakout in a trending market is *confirmation*: price clearing a recent
range means the trend is extending. In a sideways market the same event is
the textbook false breakout, and reverts. So the requirement is added where
there is something to confirm and left off elsewhere.

**The figures that justified this DO NOT describe this code.** They were
measured in a harness that gated only the first bar of a signal run, because
its "already open" test looked at the previous bar's *intended size* rather
than at whether a position existed. This module, applied through
``execution.engine._apply_entry_gate``, gates every entry into a flat
position -- and with a 0.29-day time stop, positions close constantly while
signals persist, so it demands a fresh breakout for every re-entry. Much
stricter, and materially different.

Measured, same parameters, 3.65 years:

    harness rule (gate the first bar of a run)   78.1%   2,398 trades
    this rule    (gate every entry)              63.0%   1,866 trades

So the standalone case for this filter is **withdrawn**. What the harness
validated -- trending-only beating always-on at every lookback by 1.1 to 1.5
points -- was a test of the weaker rule, and does not transfer.

What survives is narrower and is about risk rather than return. Combined
with a confidence-based size cut, this gate reduces drawdown monotonically
in lookback (-24.1%, -29.0%, -28.8%, -31.9% at 6, 12, 24 and 48 bars against
-34.4% for no filter) on consistently fewer trades. Its effect on *return*
across those same arms is scattered -- 93.1, 82.4, 84.9, 76.5 -- and is not
distinguishable from noise.

**Nothing here is wired to production.** The mechanism exists and is tested;
the case for switching it on is unfinished, and the drawdown finding above
is entangled with a normalisation flaw in the size cut it was measured
alongside. See CLAUDE.md, 2026-08-30.

The conditioning on trend regime is kept because it was measured in both
directions and the sideways-only arm was clearly worse -- that comparison
was internally consistent whichever rule the harness implemented.

**This gates opening only.** A position already held is never closed for
failing the test -- that is the engine's job to enforce, and
``_apply_entry_gate`` does it there where position state is known. Computing
the mask here and zeroing size instead would drop the signal below the exit
threshold and liquidate the book, which is a different rule wearing this
one's clothes.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

__all__ = ["entry_mask", "DEFAULT_LOOKBACK", "DEFAULT_REGIMES"]

#: Session bars of range a new position must clear.
#:
#: **Not currently justified by a measurement of this code.** The plateau
#: that chose 24 was measured on the harness rule described above, which
#: this module does not implement. Under the rule it does implement, return
#: across lookbacks is scattered and drawdown improves monotonically as the
#: lookback *shortens* -- so if this is ever switched on, the number should
#: be chosen from a sweep of the real thing rather than inherited from here.
DEFAULT_LOOKBACK = 24

#: Where the requirement applies. Everything else is left unfiltered.
DEFAULT_REGIMES: tuple[str, ...] = ("uptrend", "downtrend")


def entry_mask(close, direction, trend=None, *, lookback: int = DEFAULT_LOOKBACK,
               regimes: tuple[str, ...] = DEFAULT_REGIMES,
               on_new_signal_only: bool = True):
    """Where a *new* position may open. ``True`` everywhere it is allowed.

    Parameters
    ----------
    close:
        Price frame, bars by instrument.
    direction:
        ``long`` / ``short`` / ``flat`` per bar, same shape.
    trend:
        Book-level regime label per bar, or ``None`` to apply the filter
        everywhere. Absent labels leave the filter off for that bar rather
        than on: an unknown regime is not evidence that a trend is running,
        and refusing entries on that basis would be inferring from absence.

    Returns
    -------
    A boolean frame. ``True`` means unrestricted -- either the filter does
    not apply here, or price has cleared its range in the signal's own
    direction.
    """
    import pandas as pd

    if close is None or getattr(close, "empty", True):
        return None
    if lookback and int(lookback) < 2:
        return None

    window = int(lookback)
    # Shifted by one bar so the range excludes the bar being traded.
    # Without it every new high trivially clears a window containing
    # itself, which is lookahead by arithmetic rather than by intent.
    highs = close.rolling(window).max().shift(1)
    lows = close.rolling(window).min().shift(1)

    heading = direction.reindex_like(close) if direction is not None else None
    if heading is None:
        return None
    cleared = ((heading == "long") & (close > highs)) | (
        (heading == "short") & (close < lows)
    )

    if on_new_signal_only:
        # Require confirmation only where a signal STARTS, not on every
        # entry into a flat position.
        #
        # This is the difference between the rule that was measured and the
        # rule first implemented, and it is worth fifteen points. With a
        # 0.29-day time stop, positions close constantly while signals
        # persist; demanding a fresh breakout for each re-entry gates a move
        # that has already been confirmed once, and returned 63.0% against
        # 78.1% for confirming only at the start.
        #
        # The reading that fits: a breakout confirms a move is under way.
        # Re-confirming the same move on every re-entry is not a stricter
        # version of that idea, it is a different and worse one.
        holding = (direction.reindex_like(close) != "flat")
        continuing = holding & holding.shift(1).fillna(False)
        cleared = cleared | continuing

    allowed = cleared
    if trend is not None and len(trend):
        state = pd.Series(trend).reindex(close.index).ffill()
        applies = state.isin(list(regimes)).fillna(False).to_numpy()
        allowed = cleared.copy()
        # Outside the named regimes the filter is off, so everything is
        # permitted -- the requirement is *added* where it helps rather
        # than removed where it hurts.
        allowed.loc[~applies] = True

    # Bars with no range yet (the leading window) are permitted rather than
    # refused: "not enough history to test" is not "failed the test".
    allowed = allowed.where(highs.notna() & lows.notna(), True)
    return allowed.fillna(True)


#: Which regime decides where a breakout confirmation is required.
#:
#: The gate has always been conditioned on the *trend* regime -- confirm a
#: move where a trend is running -- and that was never measured against the
#: alternatives, it was simply the frame the runner happened to pass.
#:
#: The interesting alternative is volatility, and the reason is a finding
#: this project already has: magnitude is predictable here and direction is
#: not. Predicted-loud names move 2.18x as far as predicted-quiet, and 44.6%
#: of nominated top-quintile movers land there against a 19.6% base rate --
#: but news, earnings, attention, order-flow proxies and absorption all
#: predict how far and none predicts which way. A breakout says which way
#: once a move starts. Requiring one specifically where a large move is
#: forecast composes the two: magnitude picks the moment, the breakout picks
#: the side.
GATING_REGIMES: dict[str, tuple[str, ...]] = {
    "trend": ("uptrend", "downtrend"),
    "volatility": ("high",),
    "both": ("gate",),
    "any": (),
}


def gating_frame(regimes, mode: str = "trend"):
    """The label frame and labels the breakout filter should be gated on.

    Returns ``(frame, labels)``. A ``None`` frame means apply the filter
    everywhere, which is what ``entry_mask`` already does with no regime.

    ``both`` is not the intersection of two masks and cannot be built by
    combining them. ``entry_mask`` returns True where the filter does *not*
    apply, so ANDing a trend mask with a volatility mask yields "confirm if
    trending OR loud" -- the union of the conditions, which is the opposite
    of what asking for both means. So a derived frame is labelled ``gate``
    only where a trend is running *and* volatility is high, and the filter
    is conditioned on that single label.

    Unknown labels leave the filter off for that bar rather than on, which
    ``entry_mask`` already guarantees: an absent regime is not evidence that
    a trend is running, and refusing an entry on that basis would be
    inferring from absence.
    """
    if regimes is None:
        return None, ()
    key = str(mode or "trend").lower()
    if key not in GATING_REGIMES:
        raise ValueError(
            f"unknown breakout gating regime {mode!r}; "
            f"expected one of {sorted(GATING_REGIMES)}"
        )
    if key == "any":
        return None, ()
    if key == "trend":
        return getattr(regimes, "trend_regime", None), GATING_REGIMES["trend"]
    if key == "volatility":
        return (
            getattr(regimes, "volatility_regime", None),
            GATING_REGIMES["volatility"],
        )

    trend = getattr(regimes, "trend_regime", None)
    volatility = getattr(regimes, "volatility_regime", None)
    if trend is None or volatility is None:
        return None, ()
    import pandas as pd

    # Both are book-level Series -- one label per bar, not per instrument --
    # which is also what ``entry_mask`` expects. An earlier version of this
    # reindexed on ``.columns`` and only worked on a DataFrame; the tests
    # passed because they supplied one, which is a shape this pipeline never
    # produces. Testing the wrong shape is testing nothing.
    aligned = volatility.reindex(trend.index)
    both = (
        trend.isin(GATING_REGIMES["trend"])
        & aligned.isin(GATING_REGIMES["volatility"])
    )
    return pd.Series("off", index=trend.index).mask(both, "gate"),         GATING_REGIMES["both"]
