"""Splits and broken bars in Capital's history, repaired against the daily store.

WHAT WAS WRONG, found 2026-10-13
--------------------------------
Found by running a data-audit checklist against the lake, not by any check
here. Capital's hourly history is mostly split-adjusted -- NVDA's and WMT's
splits are clean -- but not always, and its faults come in two shapes:

* **A level left unadjusted.** PANW's history before its 2-for-1 split on
  2024-12-16 sits at twice the true price for 456 sessions, so the engine
  read a 49% crash that never happened. MMM sits about 25% high until the
  Solventum spin-off in April 2024. NOW has ONE session, 2025-12-10, at 0.6x
  the price either side of it.
* **A single broken bar.** AVGO's 13:00 bar on its split day, 2024-07-15,
  is at a tenth of the price; the bars either side are correct.

A fake crash is not a small error to a trend follower. Moving averages,
breakout bands and volatility all read it for weeks, it produces short
signals and shrinks sizes, and because the volatility and correlation
regimes are MARKET-level it leaks into every other name in the panel.

WHAT THIS DOES
--------------
The daily store (yfinance) is split-adjusted, so the ratio of Capital's
session close to it is flat except where Capital's level is wrong.

* **Levels.** Per name, wherever that ratio JUMPS from one session to the
  next by more than ``JUMP``, everything before the jump is rescaled by it.
  Only jumps: the daily store is also dividend-adjusted, so for a
  high-yielding name the ratio drifts by a few per cent a year, and a
  first version that compared each stretch with the newest level rescaled
  the whole of PFE, VZ, UPS and CMCSA -- the newest bars included, which
  would have put the history out of step with every live quote. A jump
  is a split or a spin-off; drift is not. A bad SESSION (NOW, 2025-12-10)
  is two jumps that cancel, so only that session moves. The newest bars
  are never rescaled: corrections run backwards from them.
* **Bars.** A bar far from BOTH neighbours, while the neighbours agree with
  each other, is rescaled onto them. A real move does not come back within
  the hour; a mis-scaled bar does.
* **What cannot be checked is reported, not guessed.** A day with no daily
  reference -- today, before the nightly ingest -- cannot be verified, so a
  large move on it is recorded as ``unverified`` and left alone. That is the
  case a split on the day of trading falls into, and saying so is better
  than a repair made on no evidence.

Nothing on disk is rewritten: the raw store is what the broker served, and
the repair is applied where Capital bars are loaded, once, like the session
filter beside it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, asdict

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

__all__ = [
    "PRICE_COLUMNS",
        "JUMP",
    "RESIDUE",
        "BAR",
    "AGREE",
    "UNVERIFIED",
    "Repair",
    "repair_factors",
]

#: Columns that are prices and scale together. ``spread_bp`` is relative and
#: ``volume`` is a tick count; neither moves with a split.
PRICE_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close", "bid", "ask")
#: A day-to-day change in the ratio, in log terms, that starts a new segment.
JUMP = 0.10
#: A cumulative log factor smaller than this is cancellation noise.
RESIDUE = 0.02
#: A bar this far from both neighbours, in either direction, is broken...
BAR = 0.30
#: ...provided its neighbours agree with each other to within this.
AGREE = 0.10
#: An unreferenced bar moving this far is reported as unverified.
UNVERIFIED = 0.25


@dataclass(frozen=True)
class Repair:
    ticker: str
    kind: str          # "level", "bar" or "unverified"
    start: pd.Timestamp
    end: pd.Timestamp
    factor: float      # multiplied into the prices; 1.0 for "unverified"

    def to_document(self) -> dict:
        out = asdict(self)
        out["start"], out["end"] = str(self.start), str(self.end)
        return out


def _days(index) -> pd.DatetimeIndex:
    stamps = pd.DatetimeIndex(index)
    if stamps.tz is None:
        stamps = stamps.tz_localize("UTC")
    return stamps.tz_convert("UTC").normalize()


def _level_factors(ticker: str, close: pd.Series, daily: pd.Series | None):
    """Per-DAY factor for one name, and the repairs it implies.

    Each session's factor is the product of every jump AFTER it, so the
    newest session is always 1.0 and a day with no reference takes the
    factor of the next day that has one.
    """
    session = close.groupby(_days(close.index)).last()
    factors = pd.Series(1.0, index=session.index)
    none = pd.Series(False, index=session.index)
    if daily is None or daily.dropna().empty:
        return factors, [], none
    reference = daily.copy()
    reference.index = _days(reference.index)
    reference = reference[~reference.index.duplicated(keep="last")]
    ratio = (session / reference.reindex(session.index)).replace([np.inf, -np.inf], np.nan)
    known = (ratio.notna() & (ratio > 0)).reindex(session.index, fill_value=False)
    r = ratio[known]
    if len(r) < 2:
        return factors, [], known

    step = np.log(r).diff()
    jumps = step.where(step.abs() > JUMP, 0.0).fillna(0.0)
    # Sum of the jumps strictly after each referenced day.
    after = jumps[::-1].cumsum()[::-1] - jumps
    # Two jumps that cancel (a bad session) leave a residue of the day-to-day
    # noise on everything older; a genuine jump is at least JUMP, so a
    # cumulative factor this close to 1 is that residue, not a correction.
    after = after.where(after.abs() > RESIDUE, 0.0)
    by_day = np.exp(after)
    # An unreferenced day takes the next referenced day's factor; after the
    # last referenced day there is nothing to correct against, so 1.0.
    factors = by_day.reindex(session.index).bfill().fillna(1.0)

    repairs = []
    run = (factors.round(6) != factors.round(6).shift()).cumsum()
    for _, days in factors.groupby(run):
        value = float(days.iloc[0])
        if abs(value - 1.0) > 1e-6:
            repairs.append(Repair(ticker, "level", days.index[0], days.index[-1], value))
    return factors, repairs, known


def repair_factors(bars: pd.DataFrame, daily_close: pd.Series | None):
    """A factor per bar, and what was repaired.

    ``bars`` is a tidy ``(timestamp, ticker)`` frame with ``close`` (and
    ``open`` when available); ``daily_close`` the daily store's close, tidy
    the same way. Returns ``(factors, repairs)``: ``factors`` aligned to
    ``bars.index``, to be multiplied into every column of
    :data:`PRICE_COLUMNS`.
    """
    factors = pd.Series(1.0, index=bars.index, dtype="float64")
    repairs: list[Repair] = []
    if bars is None or bars.empty or "close" not in bars.columns:
        return factors, repairs

    tickers = bars.index.get_level_values(1)
    daily_by_name = {}
    if daily_close is not None and not daily_close.empty:
        for name, part in daily_close.groupby(level=1, observed=True):
            daily_by_name[str(name)] = part.droplevel(1)

    for name in pd.unique(tickers):
        mask = np.asarray(tickers == name)
        rows = bars[mask]
        stamps = rows.index.get_level_values(0)
        close = pd.Series(rows["close"].to_numpy(dtype="float64"), index=stamps)
        opens = (pd.Series(rows["open"].to_numpy(dtype="float64"), index=stamps)
                 if "open" in rows.columns else None)

        day_factor, found, referenced = _level_factors(
            str(name), close, daily_by_name.get(str(name)))
        repairs += found
        bar_day = _days(stamps)
        per_bar = np.array(day_factor.reindex(bar_day).fillna(1.0), dtype="float64")

        # Broken single bars, judged on the level-corrected prices.
        c = close.to_numpy() * per_bar
        o = opens.to_numpy() * per_bar if opens is not None else c
        mid = (o + c) / 2.0
        prev_c = np.r_[np.nan, c[:-1]]
        next_o = np.r_[o[1:], np.nan]
        with np.errstate(invalid="ignore", divide="ignore"):
            off_prev = mid / prev_c - 1.0
            off_next = mid / next_o - 1.0
            agree = np.abs(prev_c / next_o - 1.0) < AGREE
            broken = (agree & (np.abs(off_prev) > BAR) & (np.abs(off_next) > BAR)
                      & (np.sign(off_prev) == np.sign(off_next)))
        for i in np.flatnonzero(broken):
            fix = float(np.sqrt(prev_c[i] * next_o[i]) / mid[i])
            per_bar[i] *= fix
            repairs.append(Repair(str(name), "bar", stamps[i], stamps[i], fix))

        # What cannot be verified is said, not repaired.
        unreferenced = ~referenced.reindex(bar_day, fill_value=False).to_numpy()
        with np.errstate(invalid="ignore", divide="ignore"):
            moved = np.abs(c / prev_c - 1.0) > UNVERIFIED
        for i in np.flatnonzero(unreferenced & moved & ~broken):
            repairs.append(Repair(str(name), "unverified", stamps[i], stamps[i], 1.0))

        factors[mask] = per_bar

    return factors, repairs
