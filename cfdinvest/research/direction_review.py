"""Can Claude call the side of a name we already expect to move a lot?

Pre-registered in CLAUDE.md on 2026-09-30, before any review was requested.
The owner's framing: "we're trying to determine direction on trades we know
will have large movements." This project forecasts magnitude well and
direction not at all, so the test gives Claude Sonnet 5 the loudest names
and everything knowable at 10:00 New York, and scores the side it picks
from 10:00 to the last bar's open, net of the measured spread.

**The window starts 2026-07-01 because of hindsight.** The Models API gives
Sonnet 5's release date as 2026-06-29; nothing after it can have been in its
training data.

Stages, each resumable, each writing to the data root's
``research/direction``:

    sample   choose 300 loud name-days (seeded) and build their dossiers
    review   ask Sonnet 5 for a side on each, one request per dossier
    study    the registered test, the secondary readings and the control

    python -m cfdinvest.research.direction_review sample|review|study

Research only. Nothing here is read by the dealer.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-5"
#: Published per-million-token prices, used only to log what a run cost.
PRICE_IN, PRICE_OUT = 2.00, 10.00
START, END = "2026-07-01", "2026-09-29"
SAMPLE = 300
SEED = 20260930
LOUD_SHARE = 0.20
PROMPT_VERSION = "direction-2026-09-30.1"
NEW_YORK = "America/New_York"

SYSTEM = """You are assessing one US stock at 10:00 New York time, for a
position held from 10:00 to about 15:00 the same day and then closed.

The stock was chosen because it is expected to move more than usual today.
Your job is only the direction: will it be higher or lower at about 15:00
than at 10:00? You must choose one.

You are given everything that was knowable at 10:00: recent hourly and
daily prices, today's pre-market trading, a trend-following engine's
reading, the wider market's move, and any company filings from the last
three days. Nothing after 10:00 is included. Reason from the evidence given,
not from anything you may believe about this company from elsewhere.

Answer with the direction, your confidence from 0.5 (a coin flip) to 1.0
(certain), and one sentence giving the main reason."""


def _schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "direction": {"type": "string", "enum": ["long", "short"]},
            "confidence": {"type": "number"},
            "reason": {"type": "string"},
        },
        "required": ["direction", "confidence", "reason"],
        "additionalProperties": False,
    }


# --------------------------------------------------------------------------- #
# Clocks
# --------------------------------------------------------------------------- #
def ten_of(day) -> pd.Timestamp:
    """10:00 New York on a calendar day, in UTC."""
    ny = pd.Timestamp(pd.Timestamp(day).date()).tz_localize(NEW_YORK)
    return (ny + pd.Timedelta(hours=10)).tz_convert("UTC")


def last_bar_of(day) -> pd.Timestamp:
    """The session's last hourly bar, 15:00 New York, in UTC."""
    ny = pd.Timestamp(pd.Timestamp(day).date()).tz_localize(NEW_YORK)
    return (ny + pd.Timedelta(hours=15)).tz_convert("UTC")


# --------------------------------------------------------------------------- #
# The dossier -- pure, and cut at 10:00
# --------------------------------------------------------------------------- #
def _table(frame: pd.DataFrame, columns, fmt="{:.2f}", *, dates=False) -> str:
    """CSV rows. Hourly bars in New York time; daily bars by their own date.

    A daily bar is stamped at midnight UTC, and converting that to New York
    labels it with the PREVIOUS day -- every daily row would have been
    shown to the model a day out.
    """
    lines = [("date," if dates else "time,") + ",".join(columns)]
    for stamp, row in frame.iterrows():
        cells = [fmt.format(row[c]) if pd.notna(row[c]) else "" for c in columns]
        when = (f"{pd.Timestamp(stamp):%Y-%m-%d}" if dates
                else f"{pd.Timestamp(stamp).tz_convert(NEW_YORK):%Y-%m-%d %H:%M}")
        lines.append(when + "," + ",".join(cells))
    return "\n".join(lines)


def build_dossier(
    *,
    ticker: str,
    company: str,
    sector: str,
    ten: pd.Timestamp,
    hourly: pd.DataFrame,
    daily: pd.DataFrame,
    engine: Mapping[str, Any],
    market: Mapping[str, float],
    filings: list[Mapping[str, Any]],
) -> str:
    """Everything knowable at ``ten``, and nothing at or after it.

    An hourly bar is stamped at its START and complete an hour later, so
    only bars stamped at least an hour before ``ten`` are complete by then.
    Daily bars are included only for days before ``ten``'s own day.
    """
    complete = hourly[hourly.index <= ten - pd.Timedelta(hours=1)]
    recent = complete[complete.index >= ten - pd.Timedelta(days=15)]
    past_days = daily[daily.index.normalize() < ten.normalize()].tail(30)
    parts = [
        f"Stock: {ticker} ({company}), sector: {sector or 'unknown'}.",
        f"Decision time: {ten.tz_convert(NEW_YORK):%A %Y-%m-%d} 10:00 New York.",
        "",
        "Hourly bars, New York time (bar start), including pre- and after-market:",
        _table(recent, ["open", "high", "low", "close"]),
        "",
        "Daily bars (volume in shares), before today:",
        _table(past_days, ["close", "volume"], fmt="{:.2f}", dates=True) if "volume" in past_days
        else _table(past_days, ["close"], dates=True),
        "",
        "Trend engine on its decision bar (components in [-1, 1]):",
        json.dumps({k: (round(v, 3) if isinstance(v, float) else v) for k, v in engine.items()}),
        "",
        "Market (equal-weighted large caps): "
        f"previous close to 10:00 {market.get('today', float('nan')):+.2%}, "
        f"last five sessions {market.get('five_days', float('nan')):+.2%}.",
        "",
        "Company 8-K filings in the last three days: "
        + ("; ".join(f"{f['accepted']} items {f['items']}: {f['event']} ({f['impact']})"
                     for f in filings) if filings else "none."),
    ]
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Stores
# --------------------------------------------------------------------------- #
def _dir(config=None) -> Path:
    from cfdinvest.persist.storage import account_root

    path = Path(account_root(config, None)) / "research" / "direction"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _read(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _append(path: Path, rows) -> int:
    with path.open("a", encoding="utf-8") as handle:
        count = 0
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")
            count += 1
    return count


# --------------------------------------------------------------------------- #
# Sample
# --------------------------------------------------------------------------- #
def _bars(root: Path, ticker: str) -> pd.DataFrame | None:
    path = root / f"{ticker}.parquet"
    if not path.is_file():
        return None
    frame = pd.read_parquet(path)
    if "timestamp" in frame.columns:
        frame = frame.set_index("timestamp")
    frame.index = pd.to_datetime(frame.index, utc=True)
    return frame.sort_index()


def sample(config) -> int:
    """Choose the name-days and write their dossiers and hidden outcomes."""
    from cfdinvest.backtest.runner import run_pipeline
    from cfdinvest.ingestion.news_rss import company_names
    from cfdinvest.repository import DataRepository
    from cfdinvest.research import filing_events
    from cfdinvest.research.harness import research_backtest
    from cfdinvest.signals import trend as trend_module
    from cfdinvest.signals.frame import SignalConfig

    names = list(config.universe.candidates)
    raw = Path(config.paths.raw_market_dir)
    hourly = {t: b for t in names if (b := _bars(raw / str(config.market.interval), t)) is not None}
    daily = {t: b for t in names if (b := _bars(raw / "1d", t)) is not None}

    # The engine's own reading, from a run over the window and its warm-up.
    run = run_pipeline(DataRepository(config),
                       research_backtest(config, tickers=names, start="2026-01-01", end=END))
    signals, analysis = run.signals, run.analysis
    settings = SignalConfig()
    components = trend_module.trend_components(
        analysis.close, analysis.returns.get("rolling"), analysis.prices.get("high"),
        analysis.prices.get("low"), fast=settings.fast_ma, slow=settings.slow_ma,
        breakout_window=settings.breakout_window, momentum_full_scale=settings.momentum_full_scale,
    )
    regime = getattr(run.regimes, "combined_regime", None)
    index = pd.DatetimeIndex(signals.direction.index)
    index = index.tz_localize("UTC") if index.tz is None else index.tz_convert("UTC")

    # Loudness: trailing 20-session realised volatility of session hourly
    # returns, up to the previous close -- the magnitude the project forecasts.
    session_returns = np.log(analysis.close).diff()
    session_returns.index = index
    vol = session_returns.groupby(session_returns.index.normalize()).std()
    predicted = vol.rolling(20, min_periods=15).mean().shift(1)

    sectors = dict(getattr(config.universe, "sectors", {}) or {})
    filings = filing_events._read(filing_events._dir(config) / "labels.jsonl")
    texts = {r["accession"]: r for r in filing_events._read(filing_events._dir(config) / "texts.jsonl")}
    closes = pd.DataFrame({t: b["close"] for t, b in daily.items()})

    candidates = []
    for day in pd.date_range(START, END, freq="B", tz="UTC"):
        ten, last = ten_of(day), last_bar_of(day)
        if day.normalize() not in predicted.index:
            continue
        loud = predicted.loc[day.normalize()].dropna()
        loud = loud[loud >= loud.quantile(1 - LOUD_SHARE)]
        for ticker in loud.index:
            bars = hourly.get(ticker)
            if bars is None or ten not in bars.index or last not in bars.index:
                continue
            candidates.append((day, ticker))
    rng = np.random.default_rng(SEED)
    chosen = [candidates[i] for i in sorted(rng.choice(len(candidates), size=min(SAMPLE, len(candidates)),
                                                       replace=False))]
    logger.info("%d loud name-days in the window; %d sampled", len(candidates), len(chosen))

    rows = []
    for day, ticker in chosen:
        ten, last = ten_of(day), last_bar_of(day)
        bars = hourly[ticker]
        decision = ten - pd.Timedelta(hours=1)
        engine = {}
        if decision in index:
            position = index.get_loc(decision)
            engine = {
                "direction": str(signals.direction.iloc[position][ticker]),
                "confidence": float(signals.confidence.iloc[position][ticker]),
                **{name: float(frame.iloc[position][ticker]) for name, frame in components.items()
                   if ticker in frame.columns and pd.notna(frame.iloc[position][ticker])},
            }
            if regime is not None and len(regime):
                engine["market_regime"] = str(pd.Series(regime.to_numpy(), index=index).iloc[position])
        before = closes[closes.index.normalize() < day.normalize()]
        prior = before.iloc[-1] if len(before) else None
        opens = pd.Series({t: b.loc[ten, "open"] for t, b in hourly.items() if ten in b.index})
        market = {
            "today": float((opens / prior.reindex(opens.index) - 1).mean()) if prior is not None else float("nan"),
            "five_days": float((before.iloc[-1] / before.iloc[-6] - 1).mean()) if len(before) > 6 else float("nan"),
        }
        recent_filings = [
            {**f, "items": texts.get(f["accession"], {}).get("items", "")}
            for f in filings if f["ticker"] == ticker
            and ten - pd.Timedelta(days=3) < pd.Timestamp(f["accepted"]) <= ten
        ]
        dossier = build_dossier(
            ticker=ticker, company=(company_names(ticker, config=config) or [ticker])[0],
            sector=sectors.get(ticker, ""), ten=ten, hourly=bars,
            daily=daily.get(ticker, pd.DataFrame(columns=["close"])),
            engine=engine, market=market, filings=recent_filings,
        )
        entry, exit_ = bars.loc[ten], bars.loc[last]
        rows.append({
            "key": f"{day:%Y-%m-%d}:{ticker}", "day": f"{day:%Y-%m-%d}", "ticker": ticker,
            "dossier": dossier,
            # Hidden from the model; read only by the study.
            "move": float(exit_["open"] / entry["open"] - 1),
            "cost": float((entry.get("spread_bp", np.nan) + exit_.get("spread_bp", np.nan)) / 2 / 1e4),
            "engine_side": {"long": 1, "short": -1}.get(engine.get("direction", ""), 0),
        })
    path = _dir(config) / "sample.jsonl"
    path.unlink(missing_ok=True)
    return _append(path, rows)


# --------------------------------------------------------------------------- #
# Review
# --------------------------------------------------------------------------- #
def review(config, *, client=None, limit: int | None = None) -> dict:
    """One Sonnet 5 request per dossier, resumable. Never sees the outcome."""
    import anthropic

    client = client or anthropic.Anthropic()
    folder = _dir(config)
    done = {r["key"] for r in _read(folder / "reviews.jsonl")}
    todo = [r for r in _read(folder / "sample.jsonl") if r["key"] not in done][:limit]
    usage = {"input": 0, "output": 0, "reviewed": 0, "asked": len(todo)}
    for row in todo:
        try:
            response = client.messages.create(
                model=MODEL, max_tokens=16000, system=SYSTEM,
                messages=[{"role": "user", "content": row["dossier"]}],
                output_config={"format": {"type": "json_schema", "schema": _schema()}},
            )
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError):
            raise
        except anthropic.APIError as error:
            logger.warning("Review of %s failed: %s", row["key"], error)
            continue
        usage["input"] += response.usage.input_tokens
        usage["output"] += response.usage.output_tokens
        if response.stop_reason != "end_turn":
            logger.warning("Review of %s stopped early (%s)", row["key"], response.stop_reason)
            continue
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            answer = json.loads(text)
        except json.JSONDecodeError:
            continue
        if answer.get("direction") not in ("long", "short"):
            continue
        usage["reviewed"] += _append(folder / "reviews.jsonl", [{
            "key": row["key"], "model": MODEL, "prompt_version": PROMPT_VERSION,
            "direction": answer["direction"],
            "confidence": float(np.clip(float(answer.get("confidence", 0.5)), 0.5, 1.0)),
            "reason": str(answer.get("reason", ""))[:400],
        }])
    usage["cost_usd"] = round(usage["input"] / 1e6 * PRICE_IN + usage["output"] / 1e6 * PRICE_OUT, 3)
    return usage


# --------------------------------------------------------------------------- #
# Study
# --------------------------------------------------------------------------- #
def _summary(frame: pd.DataFrame, column: str) -> dict:
    per_day = frame.groupby("day")[column].mean().sort_index()
    n, half = len(per_day), len(per_day) // 2
    sd = per_day.std(ddof=1)
    return {
        "name_days": int(len(frame)), "days": int(n),
        "mean": float(per_day.mean()) if n else float("nan"),
        "t": float(per_day.mean() / (sd / np.sqrt(n))) if n > 2 and sd > 0 else float("nan"),
        "h1": float(per_day.iloc[:half].mean()) if half else float("nan"),
        "h2": float(per_day.iloc[half:].mean()) if n - half else float("nan"),
    }


def study(samples: list[dict], reviews: list[dict]) -> dict:
    """The registered test, the secondary readings and the engine control."""
    frame = pd.DataFrame(samples).merge(pd.DataFrame(reviews), on="key")
    frame["side"] = frame["direction"].map({"long": 1, "short": -1})
    cost = frame["cost"].fillna(frame["cost"].median())
    frame["gross"] = frame["side"] * frame["move"]
    frame["net"] = frame["gross"] - cost
    result = {
        "gross": _summary(frame, "gross"), "net": _summary(frame, "net"),
        "hit_rate": float((frame["gross"] > 0).mean()),
        "long_share": float((frame["side"] > 0).mean()),
    }
    confident = frame["confidence"] >= frame["confidence"].median()
    result["more_confident"] = _summary(frame[confident], "net")
    result["less_confident"] = _summary(frame[~confident], "net")
    engine = frame[frame["engine_side"] != 0].copy()
    engine["engine_net"] = engine["engine_side"] * engine["move"] - cost.loc[engine.index]
    result["engine_control"] = _summary(engine, "engine_net")
    result["agrees_with_engine"] = float((engine["side"] == engine["engine_side"]).mean()) if len(engine) else float("nan")
    return result


def main(argv=None) -> int:
    from cfdinvest.config import load_config

    parser = argparse.ArgumentParser(prog="python -m cfdinvest.research.direction_review")
    parser.add_argument("stage", choices=("sample", "review", "study"))
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = load_config()
    if args.stage == "sample":
        print("sampled", sample(config))
    elif args.stage == "review":
        print(review(config, limit=args.limit))
    else:
        folder = _dir(config)
        print(json.dumps(study(_read(folder / "sample.jsonl"), _read(folder / "reviews.jsonl")), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
