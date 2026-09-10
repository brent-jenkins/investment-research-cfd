"""The last look before a trade is opened.

The engine decides on the last bar that *closed*. The fill happens now. At
the open of a US session those are a night apart, and the price being filled
at already contains everything that happened in between -- an earnings
release, a downgrade, a sector move. The decision is not wrong; it is
answering a question about a world that has since changed.

This recomputes the signal with the current price standing in as the bar in
progress, and reports whether the decision still holds. It decides nothing
itself: the caller compares and acts.

What the measurement says
-------------------------
Over 2,125 round trips, requiring the freshly computed direction to still
match *and* its confidence to still clear the entry bar would have refused
201 of them, 9.5%. Those refused trips won 36.3% of the time and averaged
-0.331%; the 1,924 kept won 52.7% and averaged +0.011% (t=+4.63, p<0.0001).
On entries taken at a session open the check refuses 46 of 208, averaging
-0.498%.

Note what is *not* being checked. A rule refusing entries because the price
has run against the signal would remove the best cohort measured, not the
worst: past 0.25% adverse those trips win 57.7%, against 41.2% for entries
that chased a favourable move. Getting a better price than the signal was
struck at is an advantage. What matters is whether the signal still stands
at all, which is why this recomputes it rather than measuring a distance.

How it is computed, and the one place it approximates
-----------------------------------------------------
Through the same preprocessing, regime and signal code the pipeline uses,
over the same lake, with one provisional bar appended per instrument built
from the live quote. Nothing is reimplemented here: a second copy of the
trend blend would drift from the first one, and the day it drifted the check
would refuse trades the engine had every reason to take.

The provisional bar carries the live price as open, high, low and close.
That understates the bar's true range, which matters only to the breakout
component, and understating a range makes a breakout *harder* to register --
the conservative direction for a check whose job is to withhold permission.

Volume is left absent rather than guessed. A bar minutes old has a fraction
of a session's volume, and a zero would read as a dead market.

``hold_across_session_open`` is forced off for this computation. That rule
carries the previous bar's stance across a session's first bar, which is
right for the engine and would be self-defeating here: the whole point of
this check is to look at the new bar rather than to inherit the old one.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "MIN_SESSION_MINUTES",
    "reevaluate",
    "ProvisionalRepository",
]


#: How much trading a session must have seen before the last look's opinion
#: is worth having, in minutes.
#:
#: The check extends the lake with a PROVISIONAL bar built from one quote:
#: ``open``, ``high``, ``low`` and ``close`` all the same number, so its range
#: is zero by construction. Mid-session that sits behind well-formed bars and
#: costs little -- measured on 2026-09-09 at 52 minutes into a bar, the live
#: reading and the engine's agreed to three decimals on five of six names.
#:
#: **At the open it inverts.** On 2026-09-10, six minutes after the bell:
#:
#:     name   engine 13:00 bar   live last look   outcome
#:     TGT           0.463          >= 0.75       OPENED, lost money
#:     BLK           0.806           < 0.75       refused
#:
#: Wrong in both directions on one pass. The cause is not the provisional bar
#: alone -- it is that the bar BEHIND it held six minutes of trading, so every
#: range-based input was reading noise. The engine's own bar was equally young
#: and disagreed with the live one because the two are built differently.
#:
#: So below this threshold the check declines rather than answering. That is
#: the house rule applied where it was missing: an uncomputable reading caps
#: the verdict, and "not checked" is not "passed". ``holds`` already refuses
#: an instrument it has no reading for, so declining here fails closed with no
#: new vocabulary.
#:
#: **30 is provisional and NOT measured.** It is the value ``open_delay_
#: minutes`` used to carry, which kept dealing out of this window by accident
#: while being justified on spreads. The honest threshold is where the live
#: reading and the engine's stop diverging, and that is measurable from the
#: 5-minute lake -- not yet done. Until it is, this errs long.
MIN_SESSION_MINUTES = 30.0


class ProvisionalRepository:
    """The lake, plus one unfinished bar per instrument from a live quote.

    A wrapper rather than a write: nothing here reaches the lake. The
    provisional bar exists for the length of one check and is never
    persisted, because it is not an observation -- it is a guess about a bar
    that has not finished, and the lake holds only bars that have.
    """

    def __init__(self, inner, prices: Mapping[str, float], at) -> None:
        self._inner = inner
        self._prices = dict(prices or {})
        self._at = at

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    @property
    def config(self):
        return self._inner.config

    def load_market(self, *args, **kwargs):
        import pandas as pd

        frame = self._inner.load_market(*args, **kwargs)
        if frame is None or getattr(frame, "empty", True) or not self._prices:
            return frame

        working = frame.reset_index()
        stamp_column = "timestamp" if "timestamp" in working.columns else working.columns[0]
        ticker_column = "ticker" if "ticker" in working.columns else working.columns[1]

        at = pd.Timestamp(self._at)
        at = at.tz_localize("UTC") if at.tz is None else at.tz_convert("UTC")

        rows = []
        for ticker, price in self._prices.items():
            try:
                value = float(price)
            except (TypeError, ValueError):
                continue
            if not (value > 0):
                continue
            existing = working[working[ticker_column].astype(str) == str(ticker)]
            if existing.empty:
                continue
            # Never behind the lake. A quote older than the newest stored bar
            # would rewrite history rather than extend it, and a vendor that
            # returns a stale price is a thing that happens.
            newest = pd.to_datetime(existing[stamp_column], utc=True).max()
            if at <= newest:
                continue
            row = {
                stamp_column: at, ticker_column: str(ticker),
                "open": value, "high": value, "low": value, "close": value,
            }
            if "volume" in working.columns:
                row["volume"] = float("nan")
            rows.append(row)

        if not rows:
            return frame

        extended = pd.concat([working, pd.DataFrame(rows)], ignore_index=True)
        extended = extended.sort_values([stamp_column, ticker_column])
        return extended.set_index(list(frame.index.names)) if frame.index.names[0] else extended


def _too_early(tickers, moment, settings) -> set[str]:
    """Instruments whose session is too young for the check to mean anything.

    Returns the set to DECLINE. An unreadable calendar declines everything:
    not being able to tell how long a market has been open is not a reason to
    trust a reading that depends on it, and the alternative -- assume mature
    -- is the inert-gate failure this project keeps finding.
    """
    import pandas as pd

    from cfdinvest.schedule.market_calendar import describe

    names = [str(t) for t in tickers]
    try:
        now = describe(names, moment)["is_open"]
        before = describe(
            names, pd.Timestamp(moment) - pd.Timedelta(minutes=MIN_SESSION_MINUTES)
        )["is_open"]
    except Exception as error:  # noqa: BLE001 - a check that cannot run refuses
        logger.warning(
            "Last look cannot read the calendar (%s); declining every "
            "instrument rather than assuming the session is mature", error
        )
        return set(names)

    # Open now but NOT open MIN_SESSION_MINUTES ago means the session started
    # inside the window. Asked of the calendar rather than computed from a
    # session start, for the same reason the dealing path does: holidays,
    # half days and per-instrument sessions are already handled there.
    return {
        t for t in names
        if bool(now.get(t, False)) and not bool(before.get(t, False))
    }


def reevaluate(
    tickers: Sequence[str],
    prices: Mapping[str, float],
    *,
    config=None,
    at=None,
) -> dict[str, dict[str, Any]]:
    """Direction and confidence per instrument, as of the live price.

    Returns ``{}`` when it cannot be computed -- an unreadable lake, an empty
    universe, a failure anywhere in the pipeline. An empty result means *not
    checked*, and the caller must treat that as unchecked rather than as
    approval: a gate that opens when it fails is not a gate.
    """
    import dataclasses

    try:
        import pandas as pd

        from cfdinvest.backtest import BacktestConfig
        from cfdinvest.config import load_config
        from cfdinvest.preprocessing import analysis_frame_from_repository
        from cfdinvest.regimes import classify_regimes
        from cfdinvest.repository import DataRepository
        from cfdinvest.signals import generate_signals
        from cfdinvest.signals.frame import SignalConfig

        settings = config or load_config()
        wanted = [str(t) for t in tickers if str(t)]
        if not wanted:
            return {}

        moment = pd.Timestamp(at or pd.Timestamp.now(tz="UTC"))
        if moment.tzinfo is None:
            moment = moment.tz_localize("UTC")

        strategy = settings.strategy
        signal_config = dataclasses.replace(
            SignalConfig(),
            regime_mode=strategy.regime_mode,
            use_volume=strategy.use_volume,
            volume_heavy_ratio=strategy.volume_heavy_ratio,
            volume_heavy_multiplier=strategy.volume_heavy_multiplier,
            environment_in_confidence=strategy.environment_in_confidence,
            # Off deliberately -- see the module docstring.
            hold_across_session_open=False,
        )
        backtest = BacktestConfig(
            tickers=wanted, interval=settings.market.interval, signal=signal_config
        )

        repository = ProvisionalRepository(
            DataRepository(settings), {t: prices.get(t) for t in wanted}, moment
        )
        # The macro series come too. Without them the macro classifier has
        # nothing to read, every regime resolves to unknown, and
        # ``trade_when_regime_unknown`` is False -- so the check would
        # return flat for the entire universe and refuse every trade,
        # which looks exactly like a check working very well.
        analysis = analysis_frame_from_repository(
            repository, wanted, list(settings.macro.series_ids or ()),
            interval=settings.market.interval,
            config=backtest.preprocess,
        )
        if analysis is None or analysis.close.empty:
            return {}

        regimes = classify_regimes(analysis, backtest.regime)
        signals = generate_signals(analysis, regimes, signal_config)

        direction, confidence = signals.direction, signals.confidence
        if direction is None or direction.empty:
            return {}

        stamp = direction.index.max()

        # How long each market has been trading. Below MIN_SESSION_MINUTES the
        # provisional bar sits behind a stub rather than a bar, and the
        # reading is not worth having -- so none is returned and `holds`
        # refuses the instrument as "not re-checked".
        #
        # Asked per instrument rather than once, because the universe spans
        # sessions: a name that opened an hour ago is checkable while one that
        # opened five minutes ago is not, and a single answer for the book
        # would be wrong for whichever half it did not describe.
        immature = _too_early(wanted, moment, settings)
        if immature:
            logger.info(
                "Last look declines %d instrument(s), under %.0f minutes into "
                "the session: %s",
                len(immature), MIN_SESSION_MINUTES, ", ".join(sorted(immature)),
            )

        out: dict[str, dict[str, Any]] = {}
        for ticker in wanted:
            if ticker not in direction.columns or ticker in immature:
                continue
            out[ticker] = {
                "direction": str(direction.at[stamp, ticker]),
                "confidence": float(confidence.at[stamp, ticker]),
                "as_of": stamp,
                # Whether the bar this was computed on is the provisional one
                # or the last real bar. If the quote was missing or stale the
                # answer is honest but adds nothing, and the caller is owed
                # the difference.
                "provisional": bool(stamp == moment),
            }
        return out
    except Exception as error:  # noqa: BLE001 - a failed check is not an approval
        logger.warning("Could not re-evaluate before opening: %s", error)
        return {}


def holds(
    checked: Mapping[str, Mapping[str, Any]],
    ticker: str,
    direction: str,
    threshold: float,
) -> tuple[bool, str]:
    """Does the recorded decision still stand? Returns ``(ok, why_not)``.

    An instrument absent from ``checked`` is **not** approved. "The check did
    not run" and "the check passed" are different statements, and only one of
    them is a reason to put money on.
    """
    reading = (checked or {}).get(ticker)
    if not reading:
        return False, "not re-checked"

    now_direction = str(reading.get("direction") or "").lower()
    wanted = str(direction).lower()
    if now_direction != wanted:
        # Two different events, and they were reported as one. "Flat" means
        # the signal faded on fresh data -- often because the bar in
        # progress has barely moved, which dilutes momentum. "Short where
        # the book wanted long" means the trend actually turned overnight.
        # Both refuse, and both should; but a week of skip counts that
        # cannot tell them apart cannot say whether the gate is catching
        # reversals or just declining marginal signals.
        if now_direction in ("", "flat"):
            return False, "no longer a signal"
        return False, "direction reversed since the signal"
    try:
        now = float(reading.get("confidence"))
    except (TypeError, ValueError):
        return False, "not re-checked"
    if now < float(threshold):
        return False, "no longer clears the bar"
    return True, ""
