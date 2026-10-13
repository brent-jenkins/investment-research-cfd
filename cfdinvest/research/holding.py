"""Day trading or holding for days? The pre-registered comparison.

The owner's question, 2026-10-13, registered in CLAUDE.md before any run:
is flattening every evening the right horizon for this signal, or should
the book hold for days with the overnight costs paid?

Every arm is the account-faithful backtest (``day_filter.daily_book``) on
repaired prices, differing only in ``ExecutionConfig`` settings the engine
reads -- so there is no second implementation of a rule here, only arms:

    A    as shipped: the evening flatten, time stop 0.29 days
    B    PRIMARY: no flatten, no time stop -- held until the signal exits
    B3   no flatten, time stop 3 days      (secondary)
    B5   no flatten, time stop 5 days      (secondary)

The comparison is per session and paired: the same days, the same
universe, the same signal.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from cfdinvest.research import day_filter

logger = logging.getLogger(__name__)

__all__ = ["ARMS", "compare"]

#: The registered arms. ``None`` for ``overnight_profit_take`` removes the
#: evening flatten, which ``research_backtest`` otherwise switches on.
ARMS: dict[str, dict] = {
    "A": {},
    "B": {"overnight_profit_take": None, "time_stop_days": None},
    "B3": {"overnight_profit_take": None, "time_stop_days": 3.0},
    "B5": {"overnight_profit_take": None, "time_stop_days": 5.0},
}


def _t(values) -> float:
    v = pd.Series(values).dropna()
    if len(v) < 2 or v.std() == 0:
        return float("nan")
    return float(v.mean() / (v.std() / np.sqrt(len(v))))


def compare(arm: pd.DataFrame, base: pd.DataFrame) -> dict:
    """An arm's sessions, and its paired difference from the baseline."""
    days = arm.index.intersection(base.index)
    a, b = arm.loc[days], base.loc[days]
    half = len(days) // 2
    diff = a["net"] - b["net"]
    return {
        "days": len(days),
        "net": float(a["net"].mean()), "net_t": _t(a["net"]),
        "h1": float(a["net"].iloc[:half].mean()), "h2": float(a["net"].iloc[half:].mean()),
        "gross": float(a["gross"].mean()), "cost": float(a["cost"].mean()),
        "financing": float(a["financing"].mean()) if "financing" in a else float("nan"),
        "vs_base": float(diff.mean()), "vs_base_t": _t(diff),
        "vs_h1": float(diff.iloc[:half].mean()), "vs_h2": float(diff.iloc[half:].mean()),
        "cagr": float((1 + a["net"]).prod() ** (252 / max(len(days), 1)) - 1),
        "maxdd": float(((1 + a["net"]).cumprod() / (1 + a["net"]).cumprod().cummax() - 1).min()),
    }


if __name__ == "__main__":  # pragma: no cover - an operator's command
    import argparse
    from pathlib import Path

    from cfdinvest.config import load_config
    from cfdinvest.repository import DataRepository

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--start", default="2023-07-03")
    parser.add_argument("--end", default="2026-10-09")
    parser.add_argument("--cache", default="/tmp/holding")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    settings = load_config()
    repo = DataRepository(settings)

    books = {}
    for name, overrides in ARMS.items():
        path = Path(f"{args.cache}_{name}.parquet")
        if path.exists():
            books[name] = pd.read_parquet(path)
        else:
            books[name] = day_filter.daily_book(repo, settings, args.start, args.end, **overrides)
            books[name].to_parquet(path)
        print(f"arm {name} done", flush=True)

    base = books["A"]
    print(f"\nsessions {len(base)}, {base.index[0].date()} to {base.index[-1].date()}  (% per session)")
    print("arm   net      t      H1      H2     gross    cost   financing   CAGR    maxDD   vs A     t     H1     H2")
    for name, book in books.items():
        r = compare(book, base)
        print(f"{name:<4} {100*r['net']:+.3f} {r['net_t']:+6.2f} {100*r['h1']:+.3f} {100*r['h2']:+.3f}"
              f"  {100*r['gross']:+.3f}  {100*r['cost']:+.3f}   {100*r['financing']:+.3f}"
              f"  {100*r['cagr']:+6.1f}% {100*r['maxdd']:+6.1f}%"
              f"  {100*r['vs_base']:+.3f} {r['vs_base_t']:+5.2f} {100*r['vs_h1']:+.3f} {100*r['vs_h2']:+.3f}")
    r = compare(books["B"], base)
    edge = r["net"] > 0 and r["net_t"] >= 2 and r["h1"] > 0 and r["h2"] > 0
    better = r["vs_base"] > 0 and r["vs_base_t"] >= 2 and r["vs_h1"] > 0 and r["vs_h2"] > 0
    print("\nVERDICT:", "holding has an edge" if edge else
          "holding loses less, not an edge" if better else "day trading stays")
