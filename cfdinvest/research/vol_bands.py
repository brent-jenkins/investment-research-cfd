"""A small book in quiet, middling or loud names. The pre-registered test.

The owner's question, 2026-11-03, registered in CLAUDE.md before any run:
keep the strategy, change only what it trades -- 15 names across sectors,
drawn from the least, the middling or the most volatile third of the
candidates -- and see whether any band wins net of Capital's costs.

The bands are read from the screen's OWN volatility measurement
(``rank_candidates`` as of each re-screen, which never looks past the date),
so there is no second definition of volatility here, and the names within a
band are taken in the screen's own score order under its own sector-cap
walk. The engine is the account-faithful one, segment by segment, exactly
as ``day_filter.daily_book`` runs it; only the universe differs.
"""
from __future__ import annotations

import logging
from typing import Mapping, Sequence

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

__all__ = ["BANDS", "TARGET", "CAP", "PASS_T", "bands_on", "band_selections",
           "band_book", "score", "verdict"]

BANDS = ("low", "mid", "high")
TARGET = 15
CAP = 3
#: Raised from 2 for three looks.
PASS_T = 2.4


def bands_on(candidates, target: int = TARGET, cap: int | None = CAP) -> dict[str, list[str]]:
    """The three bands' names from one screen result's candidates.

    Ranked candidates only (a name the screen could not measure has no
    volatility to band it by). Sorted by volatility and cut into thirds by
    count; within each third, best score first, through the screen's own
    sector-cap walk.
    """
    from cfdinvest.universe.screen import select_within_sector_cap

    ranked = [c for c in candidates
              if not c.unranked and c.volatility is not None and c.score is not None]
    ranked.sort(key=lambda c: (c.volatility, c.ticker))
    n = len(ranked)
    cuts = [0, n // 3, (2 * n) // 3, n]
    out = {}
    for band, lo, hi in zip(BANDS, cuts[:-1], cuts[1:]):
        third = sorted(ranked[lo:hi], key=lambda c: (-c.score, c.ticker))
        out[band], _ = select_within_sector_cap(
            [(c.ticker, c.sector) for c in third], target, cap)
    return out


def band_selections(repository, app_config, dates, account=None) -> dict[str, list]:
    """``{band: [(as_of, names), ...]}`` over the re-screen dates."""
    from cfdinvest.research import harness
    from cfdinvest.universe import screen as screen_module

    candidates = list(app_config.universe.candidates)
    sectors = dict(getattr(app_config.universe, "sectors", {}) or {})
    frames, spreads = harness._screen_inputs(  # noqa: SLF001 - the same inputs
        repository, candidates, app_config, account)
    settings = screen_module.ScreenConfig()
    out: dict[str, list] = {band: [] for band in BANDS}
    for date in dates:
        as_of = pd.Timestamp(date)
        as_of = as_of.tz_localize("UTC") if as_of.tzinfo is None else as_of
        result = screen_module.rank_candidates(
            frames, as_of=as_of, config=settings, sectors=sectors or None, spreads=spreads)
        for band, names in bands_on(result.candidates).items():
            if names:
                out[band].append((as_of, tuple(names)))
    return out


def band_book(repository, app_config, selections: Sequence[tuple], start, end) -> pd.DataFrame:
    """One band's sessions through the account-faithful engine."""
    from cfdinvest.backtest.runner import run_pipeline
    from cfdinvest.research import day_filter, replay

    days = pd.date_range(pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC"),
                         freq="B")
    segments = replay.selection_segments(selections, days)
    parts = []
    for i, (universe, seg_days) in enumerate(segments):
        result = run_pipeline(repository, replay.segment_config(app_config, universe, seg_days))
        execution = result.execution
        rows = day_filter.day_rows(execution.pnl, execution.equity_curve, seg_days)
        held = execution.positions.fillna(0.0).ne(0).sum(axis=1)
        rows["names_held"] = held.groupby(day_filter._utc_day(held.index)).mean()  # noqa: SLF001
        rows["universe"] = len(universe)
        parts.append(rows)
        if i % 20 == 0:
            logger.warning("segment %d of %d", i + 1, len(segments))
    book = pd.concat(parts).sort_index()
    return book[~book.index.duplicated(keep="first")]


def _t(values) -> float:
    v = pd.Series(values).dropna()
    if len(v) < 2 or v.std() == 0:
        return float("nan")
    return float(v.mean() / (v.std() / np.sqrt(len(v))))


def score(book: pd.DataFrame) -> dict:
    """The registered reading, and the secondaries, for one band."""
    net = book["net"].dropna()
    half = len(net) // 2
    gross, cost = book["gross"].mean(), book["cost"].mean()
    return {
        "sessions": int(len(net)),
        "net": float(net.mean()), "net_t": _t(net),
        "h1": float(net.iloc[:half].mean()), "h2": float(net.iloc[half:].mean()),
        "gross": float(gross), "gross_t": _t(book["gross"]),
        "cost": float(cost),
        "cost_share_of_gross": float(abs(cost) / abs(gross)) if gross else float("nan"),
        "by_year": {int(y): float(v) for y, v in
                    ((1 + net).groupby(net.index.year).prod() - 1).items()},
        "names_held": float(book.get("names_held", pd.Series(dtype=float)).mean()),
        "universe": float(book.get("universe", pd.Series(dtype=float)).mean()),
        "cagr": float((1 + net).prod() ** (252 / max(len(net), 1)) - 1),
        "maxdd": float(((1 + net).cumprod() / (1 + net).cumprod().cummax() - 1).min()),
    }


def verdict(result: Mapping) -> bool:
    """Pass: net positive at t >= PASS_T and positive in both halves."""
    return bool(result["net"] > 0 and result["net_t"] >= PASS_T
                and result["h1"] > 0 and result["h2"] > 0)


def paired(a: pd.DataFrame, b: pd.DataFrame) -> dict:
    days = a.index.intersection(b.index)
    diff = a.loc[days, "net"] - b.loc[days, "net"]
    return {"days": int(len(days)), "mean": float(diff.mean()), "t": _t(diff)}


if __name__ == "__main__":  # pragma: no cover - an operator's command
    import argparse
    import json
    from pathlib import Path

    from cfdinvest.config import load_config
    from cfdinvest.repository import DataRepository

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--start", default="2023-07-03")
    parser.add_argument("--end", default="2026-10-30")
    parser.add_argument("--cache", default="/tmp/vol_bands")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    settings = load_config()
    repo = DataRepository(settings)

    start = pd.Timestamp(args.start, tz="UTC")
    dates = pd.date_range(start - pd.Timedelta(days=7), pd.Timestamp(args.end, tz="UTC"),
                          freq="W-SUN")
    chosen = band_selections(repo, settings, dates)
    fills = {b: float(np.mean([len(n) for _, n in s])) for b, s in chosen.items()}
    print("mean names per band", fills, flush=True)

    books, results = {}, {}
    for band in BANDS:
        path = Path(f"{args.cache}_{band}.parquet")
        if path.exists():
            books[band] = pd.read_parquet(path)
        else:
            books[band] = band_book(repo, settings, chosen[band], args.start, args.end)
            books[band].to_parquet(path)
        results[band] = score(books[band])
        results[band]["pass"] = verdict(results[band])
        print(band, json.dumps(results[band]), flush=True)
    results["paired"] = {f"{a}-{b}": paired(books[a], books[b])
                         for a, b in (("high", "mid"), ("high", "low"), ("mid", "low"))}
    results["fills"] = fills
    print(json.dumps(results, indent=1))
    Path(f"{args.cache}_results.json").write_text(json.dumps(results, indent=1))
