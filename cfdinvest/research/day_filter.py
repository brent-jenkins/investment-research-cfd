"""A weather vane: can the days worth trading be told apart in advance?

THE QUESTION
------------
The owner's, 2026-10-13: the book loses when the market does not do what
the strategy expects, so is there a reading, known before the session, that
says which days to trade and which to sit out? Pre-registered in CLAUDE.md
the same day, before any number was seen.

WHAT A FILTER CAN DO
--------------------
Select days. It cannot create an edge, and on a book that pays the spread on
every trip any rule that trades less loses less -- two earlier
"improvements" were exactly that. So a rule is judged against the same
number of days drawn at random (:func:`random_control`), not against the
whole record.

HOW IT READS THE BOOK
---------------------
The account-faithful backtest (``replay.history_segments`` and the run per
segment) gives one row per session: the net return of the chained equity,
and gross (intraday + gap) and cost as fractions of the equity the day
started on, from the engine's own pnl frame. The book flattens every
evening, so a day's result depends on earlier days only through the equity
it is sized on -- which is what makes selecting days from one run fair. It
is still a screen: a pass buys an engine build, never an adoption.

WHAT A FEATURE MAY SEE
----------------------
Only sessions strictly BEFORE the day it decides, plus -- for the opening
move alone -- the open of the 10:00 New York bar, which is known at 10:00
and is the first bar the account can deal on. Every feature is computed
from earlier rows and then shifted, and :func:`above_trailing_median`
compares a reading only with readings before it.
"""
from __future__ import annotations

import logging
from typing import Sequence

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

__all__ = [
    "OWN_LOOKBACK",
    "MEDIAN_LOOKBACK",
    "TREND_SESSIONS",
    "daily_book",
    "own_recent_gross",
    "efficiency_ratio",
    "dispersion",
    "opening_move",
    "prior_close_value",
    "above_trailing_median",
    "score",
    "random_control",
]

#: Sessions of the book's own gross return the primary rule sums.
OWN_LOOKBACK = 10
#: Sessions a secondary reading is compared with.
MEDIAN_LOOKBACK = 60
#: Sessions of hourly closes the trendiness reading spans.
TREND_SESSIONS = 5

_GROSS = ("intraday", "gap")
_COST = ("spread", "slippage", "financing", "fx")


def _utc_day(stamps) -> pd.DatetimeIndex:
    index = pd.DatetimeIndex(stamps)
    if index.tz is None:
        index = index.tz_localize("UTC")
    return index.tz_convert("UTC").normalize()


# --------------------------------------------------------------------- book

def day_rows(pnl: pd.DataFrame, equity: pd.Series, days: Sequence) -> pd.DataFrame:
    """Net, gross and cost per session, from one run's pnl and equity.

    Gross and cost are divided by the equity the day STARTED on -- the last
    equity before its first bar -- so a day's figures are fractions of what
    it was sized on, and they sum with the net to within financing on the
    carried leg the book never has.
    """
    equity = pd.Series(equity).dropna()
    bar_day = _utc_day(equity.index)
    closes = equity.groupby(bar_day).last()
    frame = pd.DataFrame(pnl).reindex(equity.index).fillna(0.0)
    gross = frame[[c for c in _GROSS if c in frame.columns]].sum(axis=1)
    cost = frame[[c for c in _COST if c in frame.columns]].sum(axis=1)
    financing = (frame["financing"] if "financing" in frame.columns
                 else pd.Series(0.0, index=frame.index))
    gross_by_day = gross.groupby(bar_day).sum()
    cost_by_day = cost.groupby(bar_day).sum()
    financing_by_day = financing.groupby(bar_day).sum()

    rows = []
    for day in _utc_day(days):
        before = closes[closes.index < day]
        if day not in closes.index or before.empty:
            continue
        base = float(before.iloc[-1])
        if base <= 0:
            continue
        rows.append({
            "day": day,
            "net": float(closes[day]) / base - 1.0,
            "gross": float(gross_by_day.get(day, 0.0)) / base,
            "cost": float(cost_by_day.get(day, 0.0)) / base,
            "financing": float(financing_by_day.get(day, 0.0)) / base,
        })
    return pd.DataFrame(rows).set_index("day") if rows else pd.DataFrame(
        columns=["net", "gross", "cost", "financing"])


def daily_book(repository, app_config, start, end, **overrides) -> pd.DataFrame:
    """The faithful backtest, one row per session. Reads the lake only.

    ``overrides`` are ``ExecutionConfig`` settings, refused by name if the
    engine has none -- see ``harness.research_backtest``.
    """
    from cfdinvest.backtest.runner import run_pipeline
    from cfdinvest.research import replay

    segments = replay.history_segments(repository, app_config, start, end)
    parts = []
    for i, (universe, days) in enumerate(segments):
        result = run_pipeline(
            repository, replay.segment_config(app_config, universe, days, **overrides))
        execution = result.execution
        parts.append(day_rows(execution.pnl, execution.equity_curve, days))
        if i % 20 == 0:
            logger.warning("segment %d of %d", i + 1, len(segments))
    book = pd.concat(parts).sort_index()
    return book[~book.index.duplicated(keep="first")]


# ----------------------------------------------------------------- features

def own_recent_gross(gross: pd.Series, lookback: int = OWN_LOOKBACK) -> pd.Series:
    """The PRIMARY reading: the book's gross over the previous N sessions.

    Shifted one session, so a day never sees its own result.
    """
    return gross.rolling(lookback, min_periods=lookback).sum().shift(1)


def _session_last(close: pd.DataFrame) -> pd.DataFrame:
    return close.groupby(_utc_day(close.index)).last()


def efficiency_ratio(close: pd.DataFrame, sessions: int = TREND_SESSIONS) -> pd.Series:
    """How trendy the equal-weighted market was over the previous sessions.

    |net move| over the sum of |hourly moves|, on the equal-weighted mean of
    the names' hourly returns. 1 is a straight line, 0 is going nowhere.
    Each day gets the value over the ``sessions`` sessions BEFORE it.
    """
    market = close.pct_change(fill_method=None).mean(axis=1)
    day = _utc_day(market.index)
    net = market.groupby(day).sum()
    path = market.abs().groupby(day).sum()
    ratio = (net.rolling(sessions, min_periods=sessions).sum().abs()
             / path.rolling(sessions, min_periods=sessions).sum())
    return ratio.shift(1)


def dispersion(close: pd.DataFrame) -> pd.Series:
    """How differently the names moved in the PREVIOUS session."""
    last = _session_last(close)
    return last.pct_change(fill_method=None).std(axis=1).shift(1)


def opening_move(close: pd.DataFrame, open_: pd.DataFrame) -> pd.Series:
    """|Equal-weighted move| from the previous close to the 10:00 NY open.

    The one reading that uses the day itself, and only the open of the bar
    the account first deals on -- known at 10:00.
    """
    last = _session_last(close)
    out = {}
    stamps = pd.DatetimeIndex(open_.index)
    for day in last.index[1:]:
        ny = pd.Timestamp(day.date()).tz_localize("America/New_York")
        ten = (ny + pd.Timedelta(hours=10)).tz_convert("UTC")
        if ten not in stamps:
            continue
        prev = last[last.index < day]
        if prev.empty:
            continue
        move = (open_.loc[ten] / prev.iloc[-1] - 1.0).mean()
        out[day] = abs(float(move)) if np.isfinite(move) else np.nan
    return pd.Series(out, dtype="float64")


def prior_close_value(series: pd.Series, days) -> pd.Series:
    """A daily series' latest observation strictly BEFORE each day.

    VIXCLS is stamped with the day it closed, so it is knowable only after
    that close; the same day's value would be the session's own outcome.
    """
    s = pd.Series(series).dropna()
    s.index = _utc_day(s.index)
    s = s[~s.index.duplicated(keep="last")].sort_index()
    out = {}
    for day in _utc_day(days):
        earlier = s[s.index < day]
        out[day] = float(earlier.iloc[-1]) if len(earlier) else np.nan
    return pd.Series(out, dtype="float64")


def above_trailing_median(reading: pd.Series,
                          lookback: int = MEDIAN_LOOKBACK) -> pd.Series:
    """True where a reading is above the median of the ``lookback`` BEFORE it.

    NaN while there is not yet a full lookback, so the warm-up decides
    nothing rather than being read as "below".
    """
    reading = pd.Series(reading, dtype="float64")
    median = reading.rolling(lookback, min_periods=lookback).median().shift(1)
    out = pd.Series(np.nan, index=reading.index, dtype="float64")
    known = median.notna() & reading.notna()
    out[known] = (reading[known] > median[known]).astype(float)
    return out


# -------------------------------------------------------------------- score

def _t(values: pd.Series) -> float:
    v = pd.Series(values).dropna()
    if len(v) < 2 or v.std() == 0:
        return float("nan")
    return float(v.mean() / (v.std() / np.sqrt(len(v))))


def score(book: pd.DataFrame, trade: pd.Series) -> dict:
    """Traded against skipped days. ``trade`` is True, False or NaN (no call)."""
    trade = trade.reindex(book.index)
    decided = trade.notna()
    on = book[decided & (trade.astype(float) == 1.0)]
    off = book[decided & (trade.astype(float) == 0.0)]
    half = len(on) // 2
    return {
        "days": int(decided.sum()),
        "traded": int(len(on)),
        "net": float(on["net"].mean()) if len(on) else float("nan"),
        "net_t": _t(on["net"]),
        "gross": float(on["gross"].mean()) if len(on) else float("nan"),
        "gross_t": _t(on["gross"]),
        "h1": float(on["net"].iloc[:half].mean()) if half else float("nan"),
        "h2": float(on["net"].iloc[half:].mean()) if half else float("nan"),
        "skipped_net": float(off["net"].mean()) if len(off) else float("nan"),
        "skipped_gross": float(off["gross"].mean()) if len(off) else float("nan"),
    }


def random_control(net: pd.Series, count: int, observed: float,
                   draws: int = 10_000, seed: int = 20261013) -> float:
    """Share of random same-size day sets whose mean net is BELOW ``observed``.

    0.95 or more is the pass mark: the rule beat at least 95% of
    selections that knew nothing.
    """
    values = pd.Series(net).dropna().to_numpy()
    if count <= 0 or count > len(values) or not np.isfinite(observed):
        return float("nan")
    rng = np.random.default_rng(seed)
    means = np.array([rng.choice(values, count, replace=False).mean()
                      for _ in range(draws)])
    return float((means < observed).mean())


if __name__ == "__main__":  # pragma: no cover - an operator's command
    import argparse

    from cfdinvest.config import load_config
    from cfdinvest.repository import DataRepository

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--start", default="2023-07-03")
    parser.add_argument("--end", default="2026-10-09")
    parser.add_argument("--book", default=None, help="cache the daily book here")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)

    settings = load_config()
    repo = DataRepository(settings)
    from pathlib import Path

    if args.book and Path(args.book).exists():
        book = pd.read_parquet(args.book)
    else:
        book = daily_book(repo, settings, args.start, args.end)
        if args.book:
            book.to_parquet(args.book)

    names = list(settings.universe.candidates)
    bars = repo.load_market(names, interval=settings.market.interval,
                            start=pd.Timestamp(args.start, tz="UTC") - pd.Timedelta(days=150))
    close = bars["close"].unstack("ticker")
    open_ = bars["open"].unstack("ticker")
    close.index = pd.to_datetime(close.index, utc=True)
    open_.index = pd.to_datetime(open_.index, utc=True)
    vix = repo.load_macro(["VIXCLS"])["VIXCLS"]

    days = book.index
    print(f"sessions {len(book)}, {days[0].date()} to {days[-1].date()}")
    print(f"whole book: net {100 * book.net.mean():+.3f}%/day t {_t(book.net):+.2f}"
          f"  gross {100 * book.gross.mean():+.3f}% t {_t(book.gross):+.2f}"
          f"  cost {100 * book.cost.mean():+.3f}%"
          f"  days gross>0 {100 * (book.gross > 0).mean():.1f}%")

    def line(label, s):
        print(f"  {label:<34} days {s['days']:4d} traded {s['traded']:4d}"
              f"  net {100 * s['net']:+.3f}% t {s['net_t']:+.2f}"
              f"  gross {100 * s['gross']:+.3f}% t {s['gross_t']:+.2f}"
              f"  H1 {100 * s['h1']:+.3f} H2 {100 * s['h2']:+.3f}"
              f"  | skipped net {100 * s['skipped_net']:+.3f} gross {100 * s['skipped_gross']:+.3f}")

    own = own_recent_gross(book["gross"])
    primary = (own > 0).astype(float).where(own.notna())
    s = score(book, primary)
    beats = random_control(book.loc[primary.dropna().index, "net"], s["traded"], s["net"])
    print("\nPRIMARY: trade when the book's gross over the previous 10 sessions > 0")
    line("own recent gross > 0", s)
    print(f"  beats {100 * beats:.1f}% of random same-size day sets")
    passed = (s["net"] > 0 and s["net_t"] >= 2 and s["h1"] > 0 and s["h2"] > 0
              and beats >= 0.95)
    print(f"  VERDICT: {'PASS' if passed else 'does not pass'}")

    print("\nsecondary, above against below each reading's trailing 60-session median")
    readings = {
        "market trendiness (5 sessions)": efficiency_ratio(close).reindex(days),
        "VIX, previous close": prior_close_value(vix, days),
        "dispersion, previous session": dispersion(close).reindex(days),
        "|opening move| to 10:00 NY": opening_move(close, open_).reindex(days),
    }
    for label, reading in readings.items():
        line(label + ", above", score(book, above_trailing_median(reading)))
