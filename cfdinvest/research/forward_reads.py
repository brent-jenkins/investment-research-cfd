"""The three forward experiments, read as registered. An early futility look.

Registered in CLAUDE.md: headlines (2026-09-27), client sentiment
(2026-09-28), headline events (2026-09-30). Each is a side per name per
session from data complete before 10:00 New York, applied to that session's
loudest fifth of the candidates, against the 10:00 bar's open to the last
bar's open, net of the measured spread -- the same panel and scoring as
every recent direction test (``rolling_direction``).

The futility reading is the signed mean on the loudest fifth, GROSS: at or
below zero an experiment stops. Above zero it decides nothing before its
decision date, and on a few weeks it would mean nothing anyway.
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd

from cfdinvest.research import rolling_direction as rd

logger = logging.getLogger(__name__)

NEW_YORK = "America/New_York"
#: Registered: a name's crowd against its own trailing 20-session median.
SENTIMENT_MEDIAN_SESSIONS = 20


def cutoff(day) -> pd.Timestamp:
    """10:00 New York on ``day``, in UTC."""
    local = pd.Timestamp(day).tz_localize(None).tz_localize(NEW_YORK) + pd.Timedelta(hours=10)
    return local.tz_convert("UTC")


def window_signs(events: pd.DataFrame, days) -> pd.DataFrame:
    """``day, ticker, side`` from ``ticker, first_seen, sign`` events.

    An event counts for a session when first seen in the 24 hours BEFORE
    10:00 New York that day -- half-open, so a story first seen at 10:00 is
    not news for the bar it opens. Side is the sign of the summed signs;
    zero is no call.
    """
    rows = []
    if events is None or events.empty:
        return pd.DataFrame(columns=["day", "ticker", "side"])
    seen = pd.to_datetime(events["first_seen"], utc=True)
    for day in days:
        end = cutoff(day)
        inside = events[(seen >= end - pd.Timedelta(hours=24)) & (seen < end)]
        for ticker, total in inside.groupby("ticker")["sign"].sum().items():
            if total:
                rows.append({"day": pd.Timestamp(day), "ticker": ticker, "side": int(np.sign(total))})
    return pd.DataFrame(rows, columns=["day", "ticker", "side"])


def headline_events(rows) -> pd.DataFrame:
    """Word-list sentiment of usable, non-price-report stories."""
    from cfdinvest.advisor.sentiment import is_price_report, score_headline

    out = []
    for row in rows:
        title = str(row.get("title") or "")
        if row.get("price_report") or is_price_report(title):
            continue
        sign, _ = score_headline(title)
        if sign:
            out.append({"ticker": row.get("ticker"), "first_seen": row.get("first_seen"),
                        "sign": sign})
    return pd.DataFrame(out, columns=["ticker", "first_seen", "sign"])


def crowd_sides(readings, days, sessions: int = SENTIMENT_MEDIAN_SESSIONS) -> pd.DataFrame:
    """Contrarian side from each name's long share against its own usual.

    The reading is the last one BEFORE 10:00 New York; the baseline is the
    median of the name's previous ``sessions`` such readings, never today's.
    Unusually long -> short. A null reading is absent, never 0% long.
    """
    frame = pd.DataFrame(list(readings))
    if frame.empty:
        return pd.DataFrame(columns=["day", "ticker", "side"])
    frame = frame.dropna(subset=["long_pct"])
    frame["at"] = pd.to_datetime(frame["at"], utc=True)
    daily = []
    for day in days:
        end = cutoff(day)
        before = frame[(frame["at"] < end) & (frame["at"] >= end - pd.Timedelta(hours=24))]
        last = before.sort_values("at").groupby("ticker")["long_pct"].last()
        for ticker, value in last.items():
            daily.append({"day": pd.Timestamp(day), "ticker": ticker, "long_pct": float(value)})
    daily = pd.DataFrame(daily, columns=["day", "ticker", "long_pct"]).sort_values(["ticker", "day"])
    daily["baseline"] = daily.groupby("ticker")["long_pct"].transform(
        lambda s: s.shift(1).rolling(sessions, min_periods=sessions).median())
    daily["side"] = -np.sign(daily["long_pct"] - daily["baseline"])
    daily = daily.dropna(subset=["side"])
    daily = daily[daily["side"] != 0]
    return daily[["day", "ticker", "side"]].astype({"side": int}).reset_index(drop=True)


def scored(loud: pd.DataFrame, sides: pd.DataFrame) -> dict:
    made = loud.merge(sides, on=["day", "ticker"], how="inner")
    result = rd.score(made)
    if "gross" in result:
        result["futility_stop"] = bool(result["gross"]["mean_pct"] <= 0)
    result["sessions_with_calls"] = int(made["day"].nunique()) if len(made) else 0
    return result


def loud_panel(config, start: str, end: str) -> pd.DataFrame:
    from cfdinvest.research.order_flow import _repaired
    from cfdinvest.research.direction_review import _bars

    raw = Path(config.paths.raw_market_dir)
    names = list(config.universe.candidates)
    daily = {t: b for t in names if (b := _bars(raw / "1d", t)) is not None}
    hourly = {t: _repaired(b, daily.get(t), t) for t in names
              if (b := _bars(raw / str(config.market.interval), t)) is not None}
    begin = (pd.Timestamp(start) - pd.Timedelta(days=60)).strftime("%Y-%m-%d")
    panel = rd.build_panel(hourly, daily, start=begin, end=end)
    panel = panel[(panel["day"] >= pd.Timestamp(start)) & (panel["day"] <= pd.Timestamp(end))]
    return panel[panel["loud"]][["day", "ticker", "move", "mkt_move", "cost"]].reset_index(drop=True)


def main(end: str | None = None) -> dict:  # pragma: no cover - reads the box
    import json

    from cfdinvest.config import load_config
    from cfdinvest.ingestion.headline_labels import PROMPT_VERSION
    from cfdinvest.persist.client_sentiment import load_client_sentiment
    from cfdinvest.persist.headline_labels import load_labels
    from cfdinvest.persist.headlines import usable_headlines
    from cfdinvest.signals.news import events_from_labels

    config = load_config()
    end = end or (pd.Timestamp.now("UTC") - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    loud = loud_panel(config, "2026-09-28", end)
    days = sorted(loud["day"].unique())
    out = {
        "end": end, "loud_sessions": len(days),
        "headlines": scored(loud, window_signs(headline_events(usable_headlines(config)), days)),
        "headline_events": scored(loud, window_signs(
            events_from_labels(load_labels(config), prompt_version=PROMPT_VERSION), days)),
        "client_sentiment": scored(loud, crowd_sides(load_client_sentiment(config), days)),
    }
    print(json.dumps(out, indent=1, default=str))
    return out


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.WARNING)
    main()
