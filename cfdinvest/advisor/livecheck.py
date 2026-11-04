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
    "engine_check_config",
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


def _bar_spacing(working, stamp_column, ticker_column):
    """The panel's own bar interval, or ``None`` when it cannot be told.

    Read from the data because the check is handed whatever panel the caller
    built. ``None`` means "do not assume the newest bar is the one in
    progress", which falls back to appending -- weaker, and honest about it.
    """
    import pandas as pd

    try:
        stamps = pd.to_datetime(working[stamp_column], utc=True)
        one = working[ticker_column].astype(str).iloc[-1]
        series = stamps[working[ticker_column].astype(str) == one].sort_values()
        if len(series) < 3:
            return None
        gap = series.diff().dropna().median()
        return gap if pd.notna(gap) and gap > pd.Timedelta(0) else None
    except Exception:  # noqa: BLE001 - an unreadable panel is not fatal here
        return None


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

        # How far apart the bars are, from the data rather than from config:
        # the check must work on whatever panel it is handed.
        spacing = _bar_spacing(working, stamp_column, ticker_column)

        # Whether the bar in progress was updated in place. Without this the
        # early return below discards `working` -- and once every name takes
        # the update branch, `rows` is empty on every pass, so the whole
        # check silently returns the lake untouched. That is what it did
        # between 4ac0cbe and this commit: measured on 2026-09-10, moving
        # every quote by 5% moved 0 of 50 readings. A check that cannot move
        # is not a check, whatever number it reports.
        touched = False

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
            stamps = pd.to_datetime(existing[stamp_column], utc=True)
            newest = stamps.max()
            if at <= newest:
                continue

            # **Update the bar in progress rather than appending a point.**
            #
            # This appended a new bar with open, high, low and close all set
            # to the quote -- zero range by construction. Every range-scaled
            # input then read that bar as motionless, which does not dilute a
            # weak signal (already near the floor) but collapses a strong one.
            #
            # Measured across the 50-name universe on 2026-09-10, against the
            # engine's own reading on the same bar:
            #
            #     engine band   n    mean engine   mean live   gap
            #     >= 0.75       2        0.816       0.492   -0.324
            #     0.60-0.75     1        0.650       0.656   +0.006
            #     0.40-0.60    17        0.469       0.475   +0.006
            #     < 0.40       26        0.316       0.306   -0.010
            #
            # Agreement to a hundredth everywhere except at the top, where it
            # collapsed by a third. NOT ONE of the 46 could reach 0.75; the
            # highest live reading in the whole book was 0.656. The gate had
            # a pass mark the check could not reach, so the account could
            # barely open a position -- and the ones that did get through were
            # the pathological cases where dilution inverts.
            #
            # It went unnoticed because the last look was validated at
            # entry_confidence 0.35, where a shift of 0.32 rarely crosses the
            # line. Raising the gate to 0.75 on 2026-09-04 moved the mark onto
            # a scale the check could not reach, silently.
            #
            # The lake's newest bar IS the bar in progress and already carries
            # a real open, high and low from the session so far. So the quote
            # updates it -- close moves, high and low extend if the quote is
            # outside them -- which is what a bar in progress actually looks
            # like.
            if spacing is not None and (at - newest) <= spacing:
                where = existing.index[stamps == newest]
                if len(where):
                    index = where[0]
                    working.at[index, "close"] = value
                    if "high" in working.columns:
                        working.at[index, "high"] = max(
                            float(working.at[index, "high"]), value)
                    if "low" in working.columns:
                        working.at[index, "low"] = min(
                            float(working.at[index, "low"]), value)
                    touched = True
                    continue

            # **The bar in progress is missing, so one is built -- with a
            # REAL RANGE, seeded from the last completed close.**
            #
            # This appended `open = high = low = close = quote`: zero range,
            # which is the same motionless bar that collapsed strong signals
            # by 0.32 before the update path existed. Writing that the
            # fallback was "honest about being weaker" was wrong. It does
            # not produce a weaker opinion; it produces a systematically
            # wrong one, and it refuses good trades.
            #
            # **It fires every hour, which is what made it expensive.**
            # Ingestion runs on its own cadence, so between a bar completing
            # and the next fetch the lake's newest bar is more than one
            # spacing behind and the update path above cannot be used.
            # Measured live on 2026-09-11 at 16:04, with the lake's newest
            # bar at 15:00 and the engine deciding on 14:00:
            #
            #     name   on the bar   live (appended flat)
            #     ABT        0.789          0.457
            #     LMT        0.774          0.415
            #     REGN       0.777          0.438
            #
            # Every intended name refused, uniformly ~0.34 below its own
            # reading, for roughly the first twenty minutes of every hour.
            # The quote was reaching the computation -- moving it 5% moved
            # the reading from 0.000 to 0.95 -- so this was not the earlier
            # fault returning. It was the fallback doing what it was written
            # to do.
            #
            # A bar in progress opens near the previous close and has moved
            # to the quote since. So that is what is built: open at the last
            # completed close, high and low spanning it to the quote, close
            # at the quote. The range is then the real move since the last
            # bar rather than nothing.
            #
            # Falls back to the flat bar when the previous close cannot be
            # read, which is weaker and is the safe direction -- an
            # unreadable panel must not invent a range.
            previous = None
            prior = existing.index[stamps == newest]
            if len(prior) and "close" in working.columns:
                try:
                    candidate = float(working.at[prior[0], "close"])
                    previous = candidate if candidate > 0 else None
                except (TypeError, ValueError):
                    previous = None

            opening = previous if previous is not None else value
            row = {
                stamp_column: at, ticker_column: str(ticker),
                "open": opening,
                "high": max(opening, value),
                "low": min(opening, value),
                "close": value,
            }
            if "volume" in working.columns:
                row["volume"] = float("nan")
            rows.append(row)

        if not rows and not touched:
            return frame

        extended = (
            pd.concat([working, pd.DataFrame(rows)], ignore_index=True)
            if rows else working
        )
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


def engine_check_config(settings, *, account=None):
    """The engine's own LIVE configuration, changed only where a provisional
    bar requires it. Pure apart from reading the traded universe.

    **Until 2026-10-11 the last look built its own**, and it drifted: its
    panel was the handful of candidates rather than the traded universe, and
    it lacked `for_live_intent`'s macro dating and six-month window. On
    2026-10-08 six energy-heavy candidates read as a correlation BREAKDOWN
    where the engine's fifty read diversified, a labour cut applied that the
    engine's run did not, and nine fresh longs at 0.84 read exactly flat all
    day. A safety check computing a different strategy from the engine is
    the "one engine" failure inside the check meant to enforce it.

    So this takes `_backtest_config` and `for_live_intent` -- the same two
    calls that configure the run the dealer reads -- and changes exactly:

    * ``end``: none. The engine stops at the newest COMPLETED bar; this
      exists to read the bar in progress.
    * ``signal.hold_across_session_open``: off, see the module docstring.
    * ``signal.breakout_band_lag``: 2, for the extra bar (2026-09-24).
    """
    import dataclasses

    from cfdinvest.schedule import app as app_module

    class _Args:
        interval = start = end = universe = tickers = None

    args = _Args()
    args.account = account
    live = app_module.for_live_intent(app_module._backtest_config(args, settings), settings)  # noqa: SLF001
    return dataclasses.replace(
        live,
        end=None,
        signal=dataclasses.replace(
            live.signal,
            hold_across_session_open=False,
            # THE ONE CALLER THAT SETS THIS, and the reason is the extra bar.
            #
            # This panel carries a provisional bar for the hour in progress,
            # so its last row is one bar PAST the bar the engine decided on.
            # At the engine's lag of 1 the breakout band then includes the
            # decision bar's own high -- and for a name that just broke out
            # that high IS the band top, so the component reads zero even on
            # an unchanged price. Measured 2026-09-24; at lag 2 the band is
            # the SAME one the engine tested against.
            breakout_band_lag=2,
        ),
    )


def reevaluate(
    tickers: Sequence[str],
    prices: Mapping[str, float],
    *,
    config=None,
    at=None,
    account=None,
) -> dict[str, dict[str, Any]]:
    """Direction and confidence per instrument, as of the live price.

    Computed over the engine's own live configuration and universe
    (`engine_check_config`), with ``prices`` standing in as the bar in
    progress for every instrument it covers -- pass a quote for the whole
    traded universe, not only ``tickers``, or the market-level regimes are
    read over a panel half of which has no current bar. Readings are
    returned for ``tickers`` only.

    Returns ``{}`` when it cannot be computed -- an unreadable lake, an empty
    universe, a failure anywhere in the pipeline. An empty result means *not
    checked*, and the caller must treat that as unchecked rather than as
    approval: a gate that opens when it fails is not a gate.
    """
    try:
        import pandas as pd

        from cfdinvest.config import load_config
        from cfdinvest.preprocessing import analysis_frame_from_repository
        from cfdinvest.regimes import classify_regimes
        from cfdinvest.repository import DataRepository
        from cfdinvest.signals import generate_signals

        settings = config or load_config()
        wanted = [str(t) for t in tickers if str(t)]
        if not wanted:
            return {}

        moment = pd.Timestamp(at or pd.Timestamp.now(tz="UTC"))
        if moment.tzinfo is None:
            moment = moment.tz_localize("UTC")

        backtest = engine_check_config(settings, account=account)
        signal_config = backtest.signal
        universe = list(dict.fromkeys([*(backtest.tickers or ()), *wanted]))

        repository = ProvisionalRepository(
            DataRepository(settings),
            {t: prices.get(t) for t in universe if prices.get(t) is not None},
            moment,
        )
        # The macro series come too. Without them the macro classifier has
        # nothing to read, every regime resolves to unknown, and
        # ``trade_when_regime_unknown`` is False -- so the check would
        # return flat for the entire universe and refuse every trade,
        # which looks exactly like a check working very well.
        analysis = analysis_frame_from_repository(
            repository, universe,
            list(backtest.series_ids or settings.macro.series_ids or ()),
            interval=backtest.interval or settings.market.interval,
            start=backtest.start,
            config=backtest.preprocess,
        )
        if analysis is None or analysis.close.empty:
            return {}

        regimes = classify_regimes(analysis, backtest.regime)
        signals = generate_signals(analysis, regimes, signal_config)
        if getattr(backtest, "news_mode", "off") == "trade" and backtest.news_events is not None:
            # The same news the run the dealer reads was given, through the
            # same loader and the same step -- otherwise every news trade
            # would be refused here as "no longer a signal". Not in veto
            # mode: a veto only removes opens, so the engine never intends a
            # vetoed one and there is nothing here to re-check.
            from cfdinvest.signals.news import apply_news, news_sign_panel

            signals = apply_news(
                signals,
                news_sign_panel(backtest.news_events, signals.direction.index,
                                signals.direction.columns),
                min_size=signal_config.min_size,
            )

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

        reasons = getattr(signals, "reason", None)
        combined = getattr(regimes, "combined_regime", None)
        regime_now = None
        try:
            if combined is not None and len(combined):
                regime_now = str(pd.Series(combined.to_numpy(), index=direction.index).iloc[-1])
        except Exception:  # noqa: BLE001 - a diagnostic, never a decision
            regime_now = None

        out: dict[str, dict[str, Any]] = {}
        for ticker in wanted:
            if ticker not in direction.columns or ticker in immature:
                continue
            out[ticker] = {
                "direction": str(direction.at[stamp, ticker]),
                "confidence": float(confidence.at[stamp, ticker]),
                # WHY, beside the what -- added 2026-10-11. On 2026-10-08 the
                # check read nine engine longs as exactly flat all day and the
                # record could say that it disagreed, never why. Diagnostic
                # only: `holds` reads direction and confidence, nothing else.
                "reason": (str(reasons.at[stamp, ticker])
                           if reasons is not None and ticker in reasons.columns else None),
                "regime": regime_now,
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


def _reading(reading) -> float | None:
    """The live confidence, or None when there is not one to report."""
    try:
        return float(reading.get("confidence"))
    except (TypeError, ValueError, AttributeError):
        return None


def holds(
    checked: Mapping[str, Mapping[str, Any]],
    ticker: str,
    direction: str,
    threshold: float,
) -> tuple[bool, str, float | None]:
    """Does the recorded decision still stand?

    Returns ``(ok, why_not, live_confidence)``.

    An instrument absent from ``checked`` is **not** approved. "The check did
    not run" and "the check passed" are different statements, and only one of
    them is a reason to put money on.

    **The reading comes back with the verdict**, added 2026-09-19. This
    returned only the verdict, so the record said THAT a name had fallen
    below the bar and never HOW FAR -- and those two outcomes mean opposite
    things. Clustered just under the bar is marginality, which is what a
    gate sitting at the 90th percentile of the strength distribution should
    produce. Collapsed to a third of the engine's reading is the 2026-09-10
    provisional-bar failure returning, where no name in the book could reach
    0.75 and the highest live reading was 0.656.

    A number that reaches a decision and not the record can only be
    recovered by re-running the check, which is the same gap
    `entered_bars_ago` had until the same day.

    ``None`` when there is no reading to report -- absent, unparseable, or
    refused before confidence was consulted -- never 0.0, because zero is a
    confident statement that the signal died.
    """
    reading = (checked or {}).get(ticker)
    if not reading:
        return False, "not re-checked", None

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
            return False, "no longer a signal", _reading(reading)
        return False, "direction reversed since the signal", _reading(reading)
    try:
        now = float(reading.get("confidence"))
    except (TypeError, ValueError):
        return False, "not re-checked", None
    if now < float(threshold):
        return False, "no longer clears the bar", now
    return True, "", now
