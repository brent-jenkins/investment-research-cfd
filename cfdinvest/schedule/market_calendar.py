"""Static market calendars, by asset class.

Answers three questions — is this market open, when does it next open, when
does it next close — without a network call or a vendor calendar package.

**Holidays are computed from rules, not listed.** A hardcoded table of dates
works until the year after its last entry and then silently reports a
holiday as a trading day, which on an unattended VM means a run that quietly
does nothing. The NYSE rules are stable enough to express directly
(third Monday in January, last Monday in May, Good Friday via the Gregorian
Easter algorithm), so the calendar stays correct indefinitely.

Everything here is pure and deterministic: no clock is read except the one
passed in.

Limitations, stated because they matter for a scheduler:

* **Early closes are not modelled.** Half-days before Thanksgiving and
  Christmas close at 13:00 ET; this calendar reports the normal 16:00. A run
  triggered on the close would fire three hours late on those days.
* **Exchange-specific holidays outside the US** are approximated by the
  weekend rules only.
* **FX observes no holidays** by default. The market is technically open on
  US holidays, though thin — see :data:`SESSIONS`.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Iterable
from zoneinfo import ZoneInfo

import pandas as pd

__all__ = [
    "SESSIONS",
    "DEFAULT_ASSET_CLASSES",
    "SessionRule",
    "easter_sunday",
    "us_market_holidays",
    "asset_class_of",
    "session_for",
    "is_holiday",
    "is_open",
    "next_open",
    "next_close",
    "session_bounds",
    "describe",
]


@dataclass(frozen=True, slots=True)
class SessionRule:
    """When one asset class trades.

    Attributes
    ----------
    kind:
        ``"daily"`` — a fixed window on each trading weekday.
        ``"continuous_week"`` — opens once a week and runs until a weekly
        close, as FX does from Sunday evening to Friday evening.
        ``"always"`` — 24/7.
    timezone:
        IANA zone the times below are expressed in. Using a zone rather than
        a fixed offset is what makes the session survive daylight saving:
        the US equity open is 09:30 local all year, which is 13:30 UTC in
        summer and 14:30 UTC in winter.
    """

    name: str
    kind: str
    timezone: str = "UTC"
    open_time: dt.time = dt.time(0, 0)
    close_time: dt.time = dt.time(23, 59, 59)
    #: Weekdays the session runs, Monday = 0.
    weekdays: tuple[int, ...] = (0, 1, 2, 3, 4)
    #: Weekday the weekly session opens on, for ``continuous_week``.
    week_open_day: int = 6
    week_close_day: int = 4
    #: Whether US market holidays close this session.
    observes_us_holidays: bool = True

    @property
    def zone(self) -> ZoneInfo:
        """The resolved timezone object."""
        return ZoneInfo(self.timezone)


#: Session rules per asset class. Times are local to each session's zone.
SESSIONS: dict[str, SessionRule] = {
    "equity": SessionRule(
        name="US equities",
        kind="daily",
        timezone="America/New_York",
        open_time=dt.time(9, 30),
        close_time=dt.time(16, 0),
    ),
    "equity_eu": SessionRule(
        name="European equities",
        kind="daily",
        timezone="Europe/London",
        open_time=dt.time(8, 0),
        close_time=dt.time(16, 30),
        observes_us_holidays=False,
    ),
    "equity_jp": SessionRule(
        name="Japanese equities",
        kind="daily",
        timezone="Asia/Tokyo",
        open_time=dt.time(9, 0),
        close_time=dt.time(15, 0),
        observes_us_holidays=False,
    ),
    "bond": SessionRule(
        name="US Treasuries",
        kind="daily",
        timezone="America/New_York",
        open_time=dt.time(8, 0),
        close_time=dt.time(17, 0),
    ),
    "commodity": SessionRule(
        name="Commodity futures",
        kind="daily",
        timezone="America/New_York",
        open_time=dt.time(9, 0),
        close_time=dt.time(17, 0),
    ),
    "fx": SessionRule(
        name="Spot FX",
        kind="continuous_week",
        timezone="America/New_York",
        open_time=dt.time(17, 0),
        close_time=dt.time(17, 0),
        week_open_day=6,      # Sunday evening
        week_close_day=4,     # Friday evening
        observes_us_holidays=False,
    ),
    "volatility": SessionRule(
        name="Volatility indices",
        kind="daily",
        timezone="America/New_York",
        open_time=dt.time(9, 30),
        close_time=dt.time(16, 15),
    ),
    "crypto": SessionRule(name="Crypto", kind="always"),
    "other": SessionRule(
        name="Unclassified",
        kind="daily",
        timezone="America/New_York",
        open_time=dt.time(9, 30),
        close_time=dt.time(16, 0),
    ),
}

#: Ticker -> asset class. Mirrors the signal layer's classification so a
#: scheduled run and a backtest agree on what an instrument is.
DEFAULT_ASSET_CLASSES: dict[str, str] = {
    # US indices and US-listed shares -- the same New York session.
    **{
        t: "equity"
        for t in (
            "^GSPC", "^NDX", "^DJI", "^RUT",
            "NVDA", "AAPL", "MSFT", "AMZN", "GOOGL", "META", "TSLA", "AMD",
            "AVGO", "NFLX", "ADBE", "ORCL", "COST",
            "JPM", "XOM", "JNJ", "INTC", "NKE", "LULU", "CAT",
            # Retired from the universe, kept so an older run still reads.
            "SPY", "QQQ", "IWM", "DIA", "XLK", "XLF", "XLE", "XLV", "EEM",
            "PYPL", "DIS", "BA",
            # The rest of the hundred-name pool, 2026-08-29. Classified
            # rather than left to fall through: the "other" session is
            # byte-identical to this one, so an unclassified US share gets
            # the right hours by luck and the first non-US instrument added
            # would be gated on the wrong session with nothing to say so.
            # That is how 34 of 41 instruments once ended up on the wrong
            # calendar.
            "MU", "PLTR", "LLY", "UNH", "AMAT", "LRCX", "WMT", "CRM",
            "COIN", "NOW", "GS", "BAC", "CSCO", "CVX", "KLAC", "QCOM",
            "TXN", "HD", "UBER", "IBM", "PANW", "PG", "INTU", "BKNG",
            "ABBV", "WFC", "MRK", "ANET", "VZ", "PEP", "PFE", "SHOP",
            "TMO", "MCD", "TMUS", "ISRG", "AXP", "ABT", "RTX", "HON",
            "SNPS", "AMGN", "SCHW", "GILD", "SPGI", "NEE", "CMCSA", "DHR",
            "TJX", "UNP", "COP", "SBUX", "BLK", "DE", "MDT", "LMT",
            "SLB", "CDNS", "LOW", "BMY", "PGR", "TGT", "CVS", "ADP",
            "VRTX", "REGN", "GM", "UPS", "ABNB", "AMT", "CB", "MMM",
            "SO", "MPC", "USB", "NOC", "EOG", "PLD", "DUK",
        )
    },
    **{t: "equity_eu" for t in (
        "^FTSE", "^GDAXI", "^FCHI", "^STOXX50E", "EFA", "EWG", "EWU",
    )},
    **{t: "equity_jp" for t in ("^N225", "EWJ")},
    # ^HSI is retired from the universe and there is no Hong Kong session
    # rule, so it is deliberately absent rather than pointed at a class
    # SESSIONS does not define.
    **{t: "bond" for t in ("TLT", "IEF", "HYG", "LQD", "AGG", "BND")},
    # Front-month futures. They trade nearly around the clock, which is what
    # the commodity session already describes.
    **{t: "commodity" for t in (
        "GC=F", "SI=F", "HG=F", "PL=F", "CL=F", "BZ=F", "NG=F",
        "GLD", "SLV", "USO", "DBC", "USCI",
    )},
    **{
        t: "fx"
        for t in (
            "EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X",
            "USDCAD=X", "USDCHF=X", "NZDUSD=X", "EURGBP=X",
        )
    },
    **{t: "volatility" for t in ("^VIX", "VIXY")},
    **{t: "crypto" for t in ("BTC-USD", "ETH-USD")},
}


# --------------------------------------------------------------------------- #
# Holidays, from rules
# --------------------------------------------------------------------------- #
def easter_sunday(year: int) -> dt.date:
    """Return Easter Sunday for ``year`` (Gregorian, anonymous algorithm).

    Needed only for Good Friday, which is the one US market holiday that
    does not sit on a fixed date or an ordinal weekday.

    >>> easter_sunday(2024)
    datetime.date(2024, 3, 31)
    """
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return dt.date(year, month, day + 1)


def _nth_weekday(year: int, month: int, weekday: int, nth: int) -> dt.date:
    """The ``nth`` ``weekday`` of a month; ``nth=-1`` means the last one."""
    if nth > 0:
        first = dt.date(year, month, 1)
        offset = (weekday - first.weekday()) % 7
        return first + dt.timedelta(days=offset + 7 * (nth - 1))

    last_day = (
        dt.date(year + month // 12, month % 12 + 1, 1) - dt.timedelta(days=1)
    )
    offset = (last_day.weekday() - weekday) % 7
    return last_day - dt.timedelta(days=offset)


def _observed(date: dt.date) -> dt.date:
    """Shift a fixed-date holiday off a weekend, as the NYSE does."""
    if date.weekday() == 5:          # Saturday -> observed Friday
        return date - dt.timedelta(days=1)
    if date.weekday() == 6:          # Sunday -> observed Monday
        return date + dt.timedelta(days=1)
    return date


@lru_cache(maxsize=64)
def us_market_holidays(year: int) -> frozenset[dt.date]:
    """NYSE holidays for ``year``, computed from the standing rules.

    Juneteenth is included from 2022, the first year the exchange observed
    it. Early closes are *not* holidays and are not returned here.
    """
    holidays = {
        _observed(dt.date(year, 1, 1)),                       # New Year's Day
        _nth_weekday(year, 1, 0, 3),                          # MLK Jr Day
        _nth_weekday(year, 2, 0, 3),                          # Washington's Birthday
        easter_sunday(year) - dt.timedelta(days=2),           # Good Friday
        _nth_weekday(year, 5, 0, -1),                         # Memorial Day
        _observed(dt.date(year, 7, 4)),                       # Independence Day
        _nth_weekday(year, 9, 0, 1),                          # Labor Day
        _nth_weekday(year, 11, 3, 4),                         # Thanksgiving
        _observed(dt.date(year, 12, 25)),                     # Christmas
    }
    if year >= 2022:
        holidays.add(_observed(dt.date(year, 6, 19)))         # Juneteenth
    return frozenset(holidays)


def is_holiday(date: dt.date, rule: SessionRule) -> bool:
    """Whether ``date`` is a market holiday for this session."""
    if not rule.observes_us_holidays:
        return False
    return date in us_market_holidays(date.year)


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #
def asset_class_of(asset: str, mapping: dict[str, str] | None = None) -> str:
    """Map a ticker to an asset class, defaulting to ``"other"``."""
    resolved = DEFAULT_ASSET_CLASSES if mapping is None else mapping
    return resolved.get(str(asset), "other")


def session_for(asset: str, mapping: dict[str, str] | None = None) -> SessionRule:
    """Return the session rule governing an asset.

    Falls back to ``other`` for a class with no rule. A missing session is a
    gap in a table, and a scheduler that raises on one stops running every
    instrument rather than the one it could not classify.
    """
    kind = asset_class_of(asset, mapping)
    return SESSIONS.get(kind, SESSIONS["other"])


def _to_utc(timestamp) -> pd.Timestamp:
    """Coerce any timestamp to tz-aware UTC. Naive inputs are read as UTC."""
    stamp = pd.Timestamp(timestamp)
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def _is_trading_day(local_date: dt.date, rule: SessionRule) -> bool:
    """Whether the session runs at all on this local date."""
    return local_date.weekday() in rule.weekdays and not is_holiday(local_date, rule)


def session_bounds(
    local_date: dt.date, rule: SessionRule
) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    """Return the UTC open and close of a daily session, or ``None``.

    Pure. Building the boundary in the session's own zone before converting
    is what keeps it correct across a daylight-saving change: the local open
    time is fixed, and the UTC instant moves.
    """
    if rule.kind != "daily" or not _is_trading_day(local_date, rule):
        return None

    zone = rule.zone
    opens = pd.Timestamp(
        dt.datetime.combine(local_date, rule.open_time, tzinfo=zone)
    ).tz_convert("UTC")
    closes = pd.Timestamp(
        dt.datetime.combine(local_date, rule.close_time, tzinfo=zone)
    ).tz_convert("UTC")
    return opens, closes


def _week_bounds(
    local_date: dt.date, rule: SessionRule
) -> tuple[pd.Timestamp, pd.Timestamp]:
    """UTC bounds of the weekly session containing (or following) a date."""
    zone = rule.zone
    days_since_open = (local_date.weekday() - rule.week_open_day) % 7
    open_date = local_date - dt.timedelta(days=days_since_open)

    opens = pd.Timestamp(
        dt.datetime.combine(open_date, rule.open_time, tzinfo=zone)
    ).tz_convert("UTC")

    days_to_close = (rule.week_close_day - open_date.weekday()) % 7
    close_date = open_date + dt.timedelta(days=days_to_close)
    closes = pd.Timestamp(
        dt.datetime.combine(close_date, rule.close_time, tzinfo=zone)
    ).tz_convert("UTC")
    return opens, closes


def is_open(
    asset: str, timestamp, mapping: dict[str, str] | None = None
) -> bool:
    """Whether ``asset`` is trading at ``timestamp``.

    Pure. Naive timestamps are read as UTC.

    Parameters
    ----------
    asset:
        Ticker. Unrecognised tickers fall back to the ``"other"`` session,
        which mirrors US equity hours.

    Returns
    -------
    bool
    """
    rule = session_for(asset, mapping)
    if rule.kind == "always":
        return True

    moment = _to_utc(timestamp)
    local = moment.tz_convert(rule.zone)

    if rule.kind == "continuous_week":
        opens, closes = _week_bounds(local.date(), rule)
        if moment < opens:
            # Before this week's open: check the previous week's session,
            # which is still running if we are between Sunday and Friday.
            opens, closes = _week_bounds(
                local.date() - dt.timedelta(days=7), rule
            )
        return bool(opens <= moment < closes)

    bounds = session_bounds(local.date(), rule)
    if bounds is None:
        return False
    opens, closes = bounds
    return bool(opens <= moment < closes)


def next_open(
    asset: str, timestamp, mapping: dict[str, str] | None = None, *, horizon_days: int = 30
) -> pd.Timestamp | None:
    """The next instant ``asset`` opens, at or after ``timestamp``.

    Returns ``timestamp`` itself when the market is already open, so a
    scheduler can treat "when may I next run" uniformly.

    Returns ``None`` if no open falls within ``horizon_days`` — which for a
    correctly configured session never happens, and is a signal that the
    rule is misconfigured rather than that the market is shut for a month.
    """
    rule = session_for(asset, mapping)
    moment = _to_utc(timestamp)

    if rule.kind == "always" or is_open(asset, moment, mapping):
        return moment

    if rule.kind == "continuous_week":
        local_date = moment.tz_convert(rule.zone).date()
        for offset in range(horizon_days + 1):
            opens, _ = _week_bounds(local_date + dt.timedelta(days=offset), rule)
            if opens >= moment:
                return opens
        return None

    local_date = moment.tz_convert(rule.zone).date()
    for offset in range(horizon_days + 1):
        bounds = session_bounds(local_date + dt.timedelta(days=offset), rule)
        if bounds is not None and bounds[0] >= moment:
            return bounds[0]
    return None


def next_close(
    asset: str, timestamp, mapping: dict[str, str] | None = None, *, horizon_days: int = 30
) -> pd.Timestamp | None:
    """The next instant ``asset`` closes, at or after ``timestamp``.

    While the market is open this is the current session's close; while it
    is shut this is the close of the next session.
    """
    rule = session_for(asset, mapping)
    moment = _to_utc(timestamp)

    if rule.kind == "always":
        return None

    if rule.kind == "continuous_week":
        local_date = moment.tz_convert(rule.zone).date()
        for offset in range(-7, horizon_days + 1):
            _, closes = _week_bounds(local_date + dt.timedelta(days=offset), rule)
            if closes > moment:
                return closes
        return None

    local_date = moment.tz_convert(rule.zone).date()
    for offset in range(horizon_days + 1):
        bounds = session_bounds(local_date + dt.timedelta(days=offset), rule)
        if bounds is not None and bounds[1] > moment:
            return bounds[1]
    return None


def describe(
    universe: Iterable[str], timestamp, mapping: dict[str, str] | None = None
) -> pd.DataFrame:
    """Tabulate the current state of every asset in a universe.

    The debugging view: when a scheduled run does not fire, this says which
    market it was waiting on and until when.

    Returns
    -------
    pandas.DataFrame
        Columns ``asset_class``, ``session``, ``is_open``, ``next_open``,
        ``next_close``, indexed by ticker.
    """
    moment = _to_utc(timestamp)
    rows = []
    for asset in universe:
        rule = session_for(asset, mapping)
        rows.append(
            {
                "asset": asset,
                "asset_class": asset_class_of(asset, mapping),
                "session": rule.name,
                "is_open": is_open(asset, moment, mapping),
                "next_open": next_open(asset, moment, mapping),
                "next_close": next_close(asset, moment, mapping),
            }
        )
    return pd.DataFrame(rows).set_index("asset") if rows else pd.DataFrame()
