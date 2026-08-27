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

__all__ = ["reevaluate", "ProvisionalRepository"]


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
        out: dict[str, dict[str, Any]] = {}
        for ticker in wanted:
            if ticker not in direction.columns:
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
