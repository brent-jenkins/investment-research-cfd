"""Trend following on indices, commodities, currencies and crypto.

Pre-registered in CLAUDE.md on 2026-10-01, before any run. After six routes
to direction on US large caps at hours came back at a coin flip, the owner
changed the market and the horizon: time-series trend following over weeks
to months, the best-documented direction effect in finance, on markets
Capital deals at a fraction of a share's spread.

Through ``execution.run_backtest`` -- "one engine" -- with the shipped
signal and sizing read on DAILY bars, and with the day-trading rules
removed (the session flatten, the time stop, the opening and closing
windows, the screen). Each market is charged its own Capital spread and
overnight rates, read from the broker by ``terms`` and passed in as a file.

**It never writes to the lake.** A scratch data root is assembled under
``--root`` from symlinks to the lake's daily bars, macro series and
metadata, plus the two crypto series fetched for this run alone, so the
experiment cannot add a price series to the lake as a side effect.

    python -m cfdinvest.research.trend_markets --terms terms.json --root DIR

Research only. Nothing here is read by the dealer.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

INDICES = ("^GSPC", "^NDX", "^DJI", "^FTSE", "^GDAXI", "^FCHI", "^STOXX50E", "^N225")
COMMODITIES = ("GC=F", "CL=F", "BZ=F", "HG=F", "PL=F")
CURRENCIES = ("EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X", "NZDUSD=X",
              "USDCAD=X", "USDCHF=X", "EURGBP=X")
CRYPTO = ("BTC-USD", "ETH-USD")
PRIMARY = INDICES + COMMODITIES + CURRENCIES
START, END = "2005-01-01", "2026-08-26"
CRYPTO_START = "2015-01-01"
SPLIT = "2016-01-01"


def broker_rates(terms: dict) -> tuple[dict, dict]:
    """Capital's quotes as the engine's spread and financing inputs. Pure.

    Capital quotes overnight fees as a percentage of notional per day,
    negative meaning the holder PAYS. The engine wants annual fractions,
    positive meaning a charge -- so the sign flips and the day count is 365.
    A market with no reading is left out, to fall back to the engine's own
    default, rather than charged zero.
    """
    spreads, financing = {}, {}
    for ticker, row in terms.items():
        if row.get("spread_bp") is not None:
            spreads[ticker] = float(row["spread_bp"])
        if row.get("long_rate") is not None and row.get("short_rate") is not None:
            financing[ticker] = (-float(row["long_rate"]) * 365 / 100,
                                 -float(row["short_rate"]) * 365 / 100)
    return spreads, financing


def trend_config(app_config, tickers, *, start, end, spreads, financing, **overrides):
    """The shipped strategy on daily bars, without the day-trading rules."""
    from cfdinvest.schedule import app as app_module

    class _Args:
        interval = "1d"
        start = end = universe = account = tickers = None

    base = app_module._backtest_config(_Args(), app_config)  # noqa: SLF001
    fields = dict(
        overnight_profit_take=None,      # no session flatten
        time_stop_days=None,             # no 0.29-day time stop
        open_delay_minutes=0,
        close_delay_minutes=0,
        entry_gate=None,                 # no screen
        earnings_blackout_days=0,        # no reporting dates on these
        use_measured_spreads=False,
        spread_bps=spreads,
        financing_rates=financing,
    )
    # The entry gate lives on the hysteresis thresholds, not on the
    # execution config itself; a top-level name would be refused below.
    if "entry_confidence" in overrides:
        fields["thresholds"] = dataclasses.replace(
            base.execution.thresholds, entry_confidence=overrides.pop("entry_confidence"))
    known = {f.name for f in dataclasses.fields(base.execution)}
    unknown = sorted((set(fields) | set(overrides)) - known)
    if unknown:
        raise TypeError(f"no engine field for {unknown}")
    fields.update(overrides)
    reference = frozenset(base.signal.reference_tickers) - set(tickers)
    return dataclasses.replace(
        base, tickers=list(tickers), start=start, end=end,
        execution=dataclasses.replace(base.execution, **fields),
        signal=dataclasses.replace(base.signal, reference_tickers=reference),
    )


def book_returns(result) -> pd.DataFrame:
    """Daily net and gross return of the book, from the engine's own ledger."""
    execution = result.execution
    curve = execution.equity_curve
    equity = curve["equity"] if hasattr(curve, "columns") else curve
    equity = pd.Series(equity).dropna()
    prior = equity.shift(1)
    frame = pd.DataFrame({"net": equity.pct_change()})
    pnl = pd.DataFrame(execution.pnl).reindex(equity.index)
    gross = pnl[[c for c in ("intraday", "gap") if c in pnl]].sum(axis=1)
    frame["gross"] = gross / prior
    return frame.dropna()


def summarise(result, split=SPLIT) -> dict:
    from cfdinvest.research.harness import arm_metrics

    frame = book_returns(result)
    out = {k: (str(v) if isinstance(v, pd.Timestamp) else v)
           for k, v in (arm_metrics(result) or {}).items()}

    def stats(series):
        n = len(series)
        sd = series.std(ddof=1)
        return {"days": n, "mean_bp": round(1e4 * float(series.mean()), 3),
                "t": round(float(series.mean() / (sd / np.sqrt(n))), 2) if n > 2 and sd > 0 else None,
                "annual_sharpe": round(float(series.mean() / sd * np.sqrt(252)), 2) if sd > 0 else None}

    for column in ("net", "gross"):
        out[column] = stats(frame[column])
        out[f"{column}_h1"] = stats(frame[column][frame.index < pd.Timestamp(split, tz="UTC")])
        out[f"{column}_h2"] = stats(frame[column][frame.index >= pd.Timestamp(split, tz="UTC")])
    yearly = (1 + frame["net"]).groupby(frame.index.year).prod() - 1
    out["net_by_year_pct"] = {int(y): round(100 * float(v), 1) for y, v in yearly.items()}
    out["years_positive"] = f"{int((yearly > 0).sum())} of {len(yearly)}"
    trades = getattr(result.execution, "trades", None)
    if trades is not None and not trades.empty and "units" in trades:
        out["long_share_of_entries"] = round(float((trades["units"] > 0).mean()), 3)
    return out


def scratch_root(lake, root: Path, tickers) -> Path:
    """A data root of symlinks to the lake, plus crypto fetched here only."""
    lake = Path(lake)
    daily = root / "raw" / "market" / "1d"
    daily.mkdir(parents=True, exist_ok=True)
    for name in ("macro",):
        link = root / "raw" / name
        if not link.exists():
            link.symlink_to(lake / "raw" / name)
    for name in ("metadata",):
        link = root / name
        if not link.exists():
            link.symlink_to(lake / name)
    (root / "processed").mkdir(exist_ok=True)
    source = lake / "raw" / "market" / "1d"
    for ticker in tickers:
        target = daily / f"{ticker}.parquet"
        if (source / target.name).is_file() and not target.exists():
            target.symlink_to(source / target.name)
    template = pd.read_parquet(source / "GBPUSD=X.parquet")
    for ticker in CRYPTO:
        target = daily / f"{ticker}.parquet"
        if target.exists():
            continue
        import yfinance as yf

        raw = yf.Ticker(ticker).history(start="2014-09-01", end="2026-08-27", interval="1d",
                                        auto_adjust=False)
        bars = raw.rename(columns=str.lower).rename(columns={"stock splits": "stock_splits"})
        bars.index = pd.to_datetime(bars.index.date).tz_localize("UTC")
        bars["ticker"] = ticker
        for column in template.columns:
            if column not in bars:
                bars[column] = 0.0
        bars = bars[list(template.columns)]
        if template.index.name:
            bars.index.name = template.index.name
        bars.to_parquet(target)
    return root


def main(argv=None) -> int:
    from cfdinvest.backtest.runner import run_pipeline
    from cfdinvest.config import load_config
    from cfdinvest.repository import DataRepository

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--terms", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)

    lake_config = load_config()
    root = scratch_root(lake_config.paths.data_root, Path(args.root), PRIMARY)
    config = load_config(overrides={"paths": {"data_root": str(root)}})
    spreads, financing = broker_rates(json.loads(Path(args.terms).read_text()))

    def run(tickers, start=START, **overrides):
        cfg = trend_config(config, tickers, start=start, end=END,
                           spreads=spreads, financing=financing, **overrides)
        result = run_pipeline(DataRepository(config), cfg)
        held = (result.execution.positions.fillna(0) != 0).mean()
        logger.info("held share of days, by market: %s", held.round(2).to_dict())
        return result

    results = {"primary": summarise(run(PRIMARY))}
    results["gate_0.35"] = summarise(run(PRIMARY, entry_confidence=0.35))
    for name, group in (("indices", INDICES), ("commodities", COMMODITIES), ("currencies", CURRENCIES)):
        results[name] = summarise(run(group))
    results["crypto"] = summarise(run(CRYPTO, start=CRYPTO_START), split="2021-01-01")
    Path(args.out).write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(json.dumps(results, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
