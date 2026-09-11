"""Capital.com bars into a lake of their own.

This writes ``raw/market/<interval>/<ticker>.parquet`` under an interval
label that is *not* one yfinance uses, so the two histories sit side by side
and nothing that reads the existing lake changes behaviour. That separation
is the point rather than a convenience: the two disagree about what a bar
is, and merging them would produce a series that is neither.

What is different about these bars
----------------------------------
They carry ``bid``, ``ask`` and ``spread_bp``. The backtest currently charges
one estimated constant for spread across every instrument and every hour of
the day, and that constant is the single largest modelled cost. These bars
say what was actually quoted.

They also cover 08:00 to 23:00 UTC rather than the regular session, so a
question about extended hours can be measured instead of argued.

What is deliberately absent
---------------------------
No ``dividends`` or ``stock_splits``. Capital does not report them, and the
schema marks both optional, so they are left out rather than written as
zero -- "not reported" and "there was no dividend" are different statements
and only one of them is true here.

``volume`` is Capital's own traded volume, which is its clients rather than
the market: a few hundred contracts on an AAPL hourly bar against tens of
millions of shares on the exchange. It is kept because it is real, and it is
flagged here because it must never feed the universe screen, which ranks on
turnover and would end up ranking by what one broker's customers happen to
like.

Resuming
--------
A backfill of twenty instruments runs for the better part of an hour against
a session that expires every ten minutes. Both are handled: the walk renews
its own session, and each instrument is written as it completes, so an
interrupted run resumes from what is already on disk rather than starting
over. Nothing here is atomic across instruments, and it does not need to be
-- each file is independently valid and independently resumable.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Mapping, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "write_manifest",
    "INTERVAL", "DEFAULT_START", "MAX_LOOKBACK_DAYS", "SESSION_HOURS",
    "frame_from_rows",
    "backfill_ticker", "backfill", "restrict_to_session", "is_capital_interval", "bar_span",
]

#: New York hours whose bars are the regular session. Capital quotes from
#: 04:00 local, so its raw feed carries pre-market and after-hours bars that
#: yfinance's does not.
#:
#: **This is load-bearing, not cosmetic.** Measured on the traded universe,
#: Capital serves 11.5 bars a day against yfinance's 6.9 -- so feeding the
#: raw Capital feed to an engine whose lookbacks are counted in *bars*
#: rescales every window by 1.67x at once: the trend averages, the
#: volatility windows, the breakout lookback, the time stop. Nothing would
#: fail; the strategy would simply become a different one while every test
#: still passed.
#:
#: Applied at the point of loading rather than by each caller, because
#: "which callers remembered to filter" is exactly the kind of thing that
#: drifts -- and did: every research sweep this weekend filtered by hand
#: while the production path did not filter at all.
SESSION_HOURS: tuple[int, int] = (9, 15)

#: The lake directory these land in. Distinct from ``1h`` on purpose; see the
#: module docstring. ``preprocessing`` infers periods-per-year from the bar
#: spacing it observes rather than from this label, so an unfamiliar name
#: costs nothing downstream.
INTERVAL = "capital_1h"

#: Earliest hourly history the server holds. Measured, not assumed: asking
#: for 2022 returns bars from 2023-01-03 and nothing before it.
DEFAULT_START = "2023-01-01T00:00:00"

#: The furthest back a request may reach, per resolution, in days.
#:
#: Capital serves fine resolutions for about 400 days -- probed on
#: 2026-09-10, where 1m, 5m and 15m all returned bars from 400 days back and
#: nothing from 980. ``DEFAULT_START`` of 2023-01-01 is beyond that, so every
#: fine-resolution request asked for a window the server refuses and came
#: back **HTTP 404 with nothing written**.
#:
#: That is why ``capital_1m`` holds 778 rows from January 2025 and has
#: collected nothing since: ``ingest-minute`` ran nightly for eight months,
#: resumed from a tail 615 days old, was refused, and logged a warning
#: nobody read. A permission fault on 2026-08-30 then hid it further by
#: producing a louder error for the same silent outcome.
#:
#: 180 days is well inside the served window and far more than any use here
#: needs -- the confidence-path work wants weeks, not years. Daily and hourly
#: are unbounded because the server does serve them from 2023 and the
#: backtest genuinely spans that.
MAX_LOOKBACK_DAYS: dict[str, int] = {
    "1m": 180,
    "5m": 180,
    "15m": 180,
}



#: Re-read this much of the tail on a resume. The last bar on disk may have
#: been written while its hour was still forming, so it is refetched rather
#: than trusted. Mirrors ``market.refetch_overlap_days`` in spirit.
RESUME_OVERLAP_HOURS = 6


def frame_from_rows(rows: Sequence[Mapping[str, Any]], ticker: str):
    """Rows from the API as a lake frame, or an empty frame for no rows.

    The index is the tz-aware UTC ``timestamp`` every other market file uses,
    so a reader cannot tell this frame from a yfinance one except by the
    columns it gained.
    """
    import pandas as pd

    from cfdinvest.schema import TICKER, TIMESTAMP

    if not rows:
        return pd.DataFrame()

    frame = pd.DataFrame(list(rows))
    frame[TIMESTAMP] = pd.to_datetime(frame["timestamp"], utc=True)
    frame = frame.set_index(TIMESTAMP).sort_index()
    frame = frame[~frame.index.duplicated(keep="last")]

    for column in ("open", "high", "low", "close", "bid", "ask", "spread_bp", "volume"):
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")

    frame[TICKER] = ticker
    frame[TICKER] = frame[TICKER].astype("category")
    ordered = [
        c for c in ("open", "high", "low", "close", "volume",
                    "bid", "ask", "spread_bp", TICKER)
        if c in frame.columns
    ]
    return frame[ordered]


def backfill_ticker(
    ticker: str,
    session,
    config=None,
    app_config=None,
    *,
    epic: str | None = None,
    interval: str = INTERVAL,
    resolution: str = "1h",
    start: str = DEFAULT_START,
    refetch: bool = False,
    renew=None,
    fetch=None,
    max_calls: int = 60,
) -> dict[str, Any]:
    """Fetch, merge with whatever is on disk, and write one instrument.

    Returns a small report rather than raising. A backfill runs unattended
    over many instruments and one bad response must not take the rest down;
    the caller decides what a failure means.

    Existing bars are kept where the new fetch has nothing to say, and
    overwritten where it does. The server is the authority on a bar it
    returned; disk is the authority on bars older than the walk reached.

    By default this resumes from the tail of what is on disk. Widening the
    history backwards is ``refetch=True`` rather than something inferred
    from the dates, because the two cases are indistinguishable from the
    data: the earliest bar on disk is *normally* later than the requested
    start, since the server's history simply begins where it begins. Reading
    intent from that would refetch three years on every ordinary resume.
    """
    import pandas as pd

    from cfdinvest import storage
    from cfdinvest.broker.capital import prices as capital_prices
    from cfdinvest.config import load_config

    paths = (app_config or load_config()).paths
    path = storage.market_path(paths, ticker, interval)
    existing = storage.read_parquet(path)

    from_when = start
    if len(existing.index) and not refetch:
        resume = existing.index.max() - pd.Timedelta(hours=RESUME_OVERLAP_HOURS)
        if resume > pd.Timestamp(start, tz="UTC"):
            from_when = resume.strftime("%Y-%m-%dT%H:%M:%S")

    # Never ask for more history than the server will serve at this
    # resolution. Asking is not free: the request is refused outright with a
    # 404, so the whole fetch returns nothing and the series stops advancing
    # -- which is how capital_1m collected two days in January 2025 and
    # nothing for the eight months after, one silent warning a night.
    #
    # Clamped rather than raising, because a stale tail is a normal state to
    # resume from and the caller cannot know the server's window. Moving the
    # start forward loses the bars between the tail and the window, and those
    # are bars the server no longer has: the alternative is fetching nothing
    # at all, forever.
    horizon = MAX_LOOKBACK_DAYS.get(str(resolution))
    if horizon:
        floor = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=horizon)
        if pd.Timestamp(from_when, tz="UTC") < floor:
            logger.info(
                "%s: %s history begins %s at the server; asking from there "
                "rather than %s",
                ticker, resolution, floor.strftime("%Y-%m-%d"), from_when[:10],
            )
            from_when = floor.strftime("%Y-%m-%dT%H:%M:%S")

    reader = fetch or capital_prices.fetch_prices
    rows = reader(
        epic or ticker, session, config,
        resolution=resolution, start=from_when, renew=renew, max_calls=max_calls,
    )
    fetched = frame_from_rows(rows, ticker)

    if len(fetched.index) == 0 and len(existing.index) == 0:
        return {"ticker": ticker, "written": 0, "added": 0, "ok": False,
                "error": "no bars returned and nothing on disk"}

    if len(existing.index) and len(fetched.index):
        merged = pd.concat([existing, fetched])
        merged = merged[~merged.index.duplicated(keep="last")].sort_index()
    else:
        merged = fetched if len(fetched.index) else existing

    if "ticker" in merged.columns:
        merged["ticker"] = merged["ticker"].astype("category")

    added = len(merged.index) - len(existing.index)
    try:
        storage.write_parquet(merged, path)
    except Exception as error:  # noqa: BLE001 - one file, not the run
        logger.warning("%s: could not write %s: %s", ticker, path, error)
        return {"ticker": ticker, "written": 0, "added": 0, "ok": False,
                "error": str(error)}

    return {
        "ticker": ticker,
        "written": len(merged.index),
        "added": added,
        "ok": True,
        "error": "",
        "first": merged.index.min(),
        "last": merged.index.max(),
        "median_spread_bp": (
            float(merged["spread_bp"].median())
            if "spread_bp" in merged.columns and merged["spread_bp"].notna().any()
            else None
        ),
    }


def write_manifest(reports, interval: str, app_config=None) -> str:
    """Record what this feed holds, in the shape ``repository.coverage`` reads.

    The Capital path wrote bars and never wrote a manifest, and the manifest
    is the lake's own account of itself: ``coverage()`` reads it,
    ``coverage_span()`` derives freshness from it, and the Status page and
    the health checks read both. So the decision feed -- the one the orders
    are placed on -- was the one series the lake could not describe.

    That stayed invisible until it did damage. On 2026-08-31 a routing bug
    sent every intraday fetch to yfinance, which has never heard of
    "capital_1h", and the yfinance ingestor dutifully recorded all 109
    instruments as ``status: empty, rows_total: 0``. The routing was fixed
    within the hour; the manifest was not, because nothing on the Capital
    path could rewrite it. The dashboard would have shown a critical banner
    over a healthy lake indefinitely -- and a banner that is always red is a
    banner that hides the next real fault.

    Rewritten rather than appended, like the other manifests: this says what
    the lake holds *now*, which is the documented exception to observations
    being append-only.

    Never raises. A lake that cannot describe itself is worth a warning, not
    a failed ingest -- the bars are already on disk by this point.
    """
    from cfdinvest import storage
    from cfdinvest.config import load_config

    paths = (app_config or load_config()).paths
    manifest_path = storage.metadata_path(paths, f"market_{interval}_manifest")
    existing = storage.read_json(manifest_path, default={}) or {}

    stamp = _pd().Timestamp.now(tz="UTC").isoformat()
    for report in reports or ():
        ticker = str((report or {}).get("ticker") or "")
        if not ticker:
            continue
        rows = int(report.get("written") or 0)
        # Three outcomes, kept distinct. "failed" is a vendor or write
        # error; "empty" is a clean read that found nothing; "ok" has bars.
        # Collapsing the first two would report a broken fetch as an
        # instrument that simply has no history.
        if report.get("ok"):
            status = "ok" if rows else "empty"
        else:
            status = "empty" if not rows and not report.get("error") else "failed"
        first, last = report.get("first"), report.get("last")
        existing[ticker] = {
            "status": status,
            "rows_total": rows,
            "first_timestamp": None if first is None else str(first),
            "last_timestamp": None if last is None else str(last),
            "updated_at": stamp,
            "params": {"interval": interval, "vendor": "capital"},
            "anomalies": {},
            "error": str(report.get("error") or ""),
        }

    try:
        storage.write_json(existing, manifest_path)
    except Exception as error:  # noqa: BLE001 - the bars are already written
        logger.warning("Could not write the %s manifest: %s", interval, error)
    return str(manifest_path)


def _pd():
    import pandas as pd

    return pd


def backfill(
    tickers: Iterable[str],
    config=None,
    app_config=None,
    *,
    interval: str = INTERVAL,
    resolution: str = "1h",
    start: str = DEFAULT_START,
    refetch: bool = False,
    epics: Mapping[str, str] | None = None,
    open_session=None,
    progress=None,
    **kwargs,
) -> list[dict[str, Any]]:
    """Every instrument, one at a time, resuming and renewing as it goes.

    Sequential on purpose. The documented ceiling is ten requests a second
    across the whole key, so parallel walks would have to coordinate their
    pacing with each other; one walk at the client's own interval cannot
    exceed it by construction.
    """
    from cfdinvest.broker.capital import client as capital

    settings = config or capital.CapitalConfig.from_env()
    opener = open_session or capital.open_session

    session = opener(settings)
    if not getattr(session, "ok", False):
        logger.warning("Capital: no session (%s); nothing ingested",
                       getattr(session, "error", ""))
        return []

    def renew():
        return opener(settings)

    reports: list[dict[str, Any]] = []
    wanted = [str(t) for t in tickers if str(t)]
    for index, ticker in enumerate(wanted, start=1):
        report = backfill_ticker(
            ticker, session, settings, app_config,
            epic=(epics or {}).get(ticker),
            interval=interval, resolution=resolution, start=start,
            refetch=refetch, renew=renew, **kwargs,
        )
        reports.append(report)
        if progress is not None:
            progress(index, len(wanted), report)
        else:
            logger.info(
                "%s (%d/%d): %s bars, %s new",
                ticker, index, len(wanted),
                report.get("written"), report.get("added"),
            )

    # The lake has to be able to describe the feed it trades on.
    write_manifest(reports, interval, app_config)
    return reports


def is_capital_interval(interval) -> bool:
    """Whether an interval label names one of Capital's own feeds."""
    return str(interval or "").startswith("capital_")


def resolution_for(interval) -> str:
    """The broker resolution behind an interval label.

    ``capital_1h`` is stored under its own name so it never collides with
    the yfinance ``1h`` bars in the lake, but the API is asked for ``1h``.
    Callers previously carried the pair by hand -- the minute job passes
    ``interval="capital_1m", resolution="1m"`` -- which is two places for
    one fact and a way for a top-up to fetch the wrong resolution into the
    right directory.

    Raises
    ------
    ValueError
        On an interval this does not name a resolution for. Guessing would
        silently fill a directory with bars of the wrong size, which is the
        kind of error that surfaces as a strategy behaving oddly rather
        than as a failure.
    """
    label = str(interval or "")
    if not is_capital_interval(label):
        raise ValueError(f"{label!r} is not a Capital interval")
    resolution = label[len("capital_"):]
    if resolution not in {"1m", "5m", "15m", "1h", "1d"}:
        raise ValueError(f"no known Capital resolution for {label!r}")
    return resolution


def bar_span(interval):
    """How long one bar of ``interval`` lasts, or ``None`` if it cannot be told.

    **One owner.** An interval in this project is a LAKE STORE NAME --
    ``capital_1h`` is a directory, kept distinct so Capital's hourly bars
    never collide with yfinance's ``1h`` in one lake -- and
    ``pd.Timedelta("capital_1h")`` is a ValueError. Every caller that needs
    a duration has to go through `resolution_for` first, and each one that
    reimplemented that pairing got it wrong in its own way:

    * the completed-bar trim shipped INERT TWICE on 2026-09-11, first
      reading a ``None`` argument and then handing the store name straight
      to ``Timedelta``;
    * the staleness guard written the same evening promptly grew a second
      copy of the same parsing.

    The lake's spellings and pandas' have also drifted -- a bare unit is a
    ValueError and ``"1d"`` is deprecated for ``"1D"`` -- so both are
    normalised here rather than at each call site.

    ``None`` means "cannot be told", never a guess: a caller that cannot
    learn the bar size must fall back to its own safe behaviour rather than
    act on an invented one.
    """
    import pandas as pd

    text = str(interval or "").strip()
    if not text:
        return None
    if is_capital_interval(text):
        try:
            text = resolution_for(text)
        except ValueError:
            return None
    if text[0].isalpha():
        text = "1" + text
    if text.endswith("d"):
        text = text[:-1] + "D"
    try:
        span = pd.Timedelta(text)
    except (TypeError, ValueError):
        return None
    return span if span > pd.Timedelta(0) else None


def restrict_to_session(frame, *, hours: tuple[int, int] = SESSION_HOURS):
    """Drop pre-market and after-hours bars from a Capital frame.

    Idempotent: filtering twice is filtering once, so a caller that already
    did it loses nothing. Returns the frame unchanged when it carries no
    usable timestamp index, because silently returning nothing would turn a
    shape surprise into an empty book.
    """
    import pandas as pd

    if frame is None or getattr(frame, "empty", True):
        return frame
    index = frame.index
    stamps = (
        index.get_level_values("timestamp")
        if isinstance(index, pd.MultiIndex) and "timestamp" in (index.names or ())
        else index
    )
    try:
        stamps = pd.DatetimeIndex(stamps)
    except (TypeError, ValueError):
        logger.debug("No timestamp index to restrict; frame returned unchanged")
        return frame
    if stamps.tz is None:
        stamps = stamps.tz_localize("UTC")
    local = stamps.tz_convert("America/New_York").hour
    low, high = hours
    return frame[(local >= low) & (local <= high)]
