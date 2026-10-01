"""Is the crypto trend result real? The two checks pre-registered 2026-10-01.

1. **Coins chosen without hindsight.** The classic rule of
   ``trend_markets`` on the largest coins by market value at the start of
   2018, other than Bitcoin and Ether -- picked before anyone knew which
   would survive -- one coin per run, equal-weighted, 2018 to 2026.
2. **The engine against a calculation it did not make.** The same three
   votes computed directly from Bitcoin's and Ether's closes. It is a CHECK,
   deliberately outside the engine, and decides nothing: it passes when
   its side agrees with the engine's on at least 90% of held days and its
   net return has the same sign.

Plus a side arm, deciding nothing: spot, long or flat, through the engine
with ``allow_short = False``, no financing, and 0.25% a side.

    python -m cfdinvest.research.crypto_checks --terms T --root DIR --out F

Research only; writes to its own scratch root, never to the lake.
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

from cfdinvest.research import trend_markets as tm

logger = logging.getLogger(__name__)

COINS_2018 = ("XRP-USD", "BCH-USD", "ADA-USD", "LTC-USD", "XEM-USD", "XLM-USD",
              "MIOTA-USD", "TRX-USD", "DASH-USD", "NEO-USD", "EOS-USD", "XMR-USD")
START_2018 = "2018-01-01"
SPLIT_2018 = "2022-05-01"   # the date midpoint of 2018-01 to 2026-08
SPOT_FEE_ROUND_TRIP_BP = 50.0
AGREEMENT_FLOOR = 0.90


def fetch_daily(root: Path, tickers, start="2014-09-01", end="2026-08-27") -> list[str]:
    """yfinance daily bars into the scratch root. Returns those it could get."""
    import yfinance as yf

    folder = root / "raw" / "market" / "1d"
    template = pd.read_parquet(folder / "GBPUSD=X.parquet")
    got = []
    for ticker in tickers:
        target = folder / f"{ticker}.parquet"
        if not target.exists():
            raw = yf.Ticker(ticker).history(start=start, end=end, interval="1d", auto_adjust=False)
            if raw.empty:
                logger.warning("no daily history for %s", ticker)
                continue
            bars = raw.rename(columns=str.lower).rename(columns={"stock splits": "stock_splits"})
            bars.index = pd.to_datetime(bars.index.date).tz_localize("UTC")
            bars["ticker"] = ticker
            for column in template.columns:
                if column not in bars:
                    bars[column] = 0.0
            bars = bars[list(template.columns)]
            bars.index.name = template.index.name
            bars.to_parquet(target)
        got.append(ticker)
    return got


def coin_terms(terms: dict, tickers) -> dict:
    """Capital's quote where it lists a coin; otherwise 3x BTC's spread and
    BTC's overnight rates, as registered."""
    btc = terms["BTC-USD"]
    return {t: terms.get(t) or {**btc, "spread_bp": 3 * float(btc["spread_bp"])} for t in tickers}


def independent_votes(bars: pd.DataFrame, fast=20, slow=100, window=55, lookback=21) -> pd.Series:
    """The three votes from closes alone, each a side, and their mean's sign.

    Crossover: fast mean against slow. Breakout: the close outside the
    previous ``window`` bars' high/low band (an event, as the engine reads
    it). Momentum: the sign of the ``lookback``-bar return.
    """
    close, high, low = bars["close"], bars["high"], bars["low"]
    crossover = np.sign(close.rolling(fast).mean() - close.rolling(slow).mean())
    upper, lower = high.rolling(window).max().shift(1), low.rolling(window).min().shift(1)
    breakout = pd.Series(np.where(close > upper, 1.0, np.where(close < lower, -1.0, 0.0)), index=close.index)
    momentum = np.sign(close / close.shift(lookback) - 1)
    return np.sign((crossover + breakout + momentum) / 3).where(crossover.notna())


def independent_returns(bars: pd.DataFrame, side: pd.Series, *, spread_bp: float,
                        long_rate: float, short_rate: float) -> pd.Series:
    """A unit position on yesterday's side, filled at today's open.

    Held from one open to the next; spread on each change of position (half
    the round trip per unit traded); financing per calendar night, as the
    engine charges it.
    """
    position = side.shift(1).fillna(0.0)
    open_ = bars["open"]
    held = position * (open_.shift(-1) / open_ - 1)
    cost = position.diff().abs().fillna(position.abs()) * spread_bp / 2 / 1e4
    nights = pd.Series(open_.index, index=open_.index).diff().dt.days.shift(-1).fillna(1)
    rate = np.where(position > 0, long_rate, np.where(position < 0, short_rate, 0.0))
    return (held - cost - np.abs(position) * rate * nights / 365).dropna()


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

    root = Path(args.root)
    config = load_config(overrides={"paths": {"data_root": str(root)}})
    coins = fetch_daily(root, COINS_2018)
    terms = coin_terms(json.loads(Path(args.terms).read_text()), coins + ["BTC-USD", "ETH-USD"])
    spreads, financing = tm.broker_rates(terms)
    results = {"coins_with_history": coins,
               "coins_missing": sorted(set(COINS_2018) - set(coins)),
               "spread_bp_charged": {t: round(spreads[t], 1) for t in coins}}

    def classic(ticker, start, **signal):
        cfg = tm.classic_config(config, ticker, start=start, end=tm.END,
                                spreads=spreads, financing=financing)
        if signal:
            cfg = dataclasses.replace(cfg, signal=dataclasses.replace(cfg.signal, **signal))
        return run_pipeline(DataRepository(config), cfg)

    # 1. Coins chosen without hindsight.
    frames = {}
    for ticker in coins:
        frames[ticker] = tm.book_returns(classic(ticker, START_2018))
    results["check1_book"] = tm.summarise(None, frame=tm.combine(frames), split=SPLIT_2018)
    results["check1_per_coin"] = {t: tm.summarise(None, frame=f, split=SPLIT_2018) for t, f in frames.items()}

    # 2. The engine against an independent calculation.
    from cfdinvest.research.direction_review import _bars

    check2 = {}
    for ticker, start in (("BTC-USD", tm.CRYPTO_START), ("ETH-USD", tm.CRYPTO_START)):
        result = classic(ticker, start)
        engine_side = np.sign(result.execution.positions[ticker].fillna(0))
        engine_side.index = pd.DatetimeIndex(engine_side.index).normalize()
        bars = _bars(root / "raw" / "market" / "1d", ticker)
        bars = bars[bars.index >= pd.Timestamp(start, tz="UTC")]
        side = independent_votes(bars)
        # The engine's position on day t was decided on t-1: compare like with like.
        mine = side.shift(1).reindex(engine_side.index)
        held = engine_side != 0
        agree = float((mine[held] == engine_side[held]).mean())
        rates = financing[ticker]
        own = independent_returns(bars, side, spread_bp=spreads[ticker],
                                  long_rate=rates[0], short_rate=rates[1])
        engine_net = tm.book_returns(result)["net"]
        check2[ticker] = {
            "agreement_on_engine_held_days": round(agree, 3),
            "independent_net_mean_bp": round(1e4 * float(own.mean()), 3),
            "independent_net_t": round(float(own.mean() / (own.std(ddof=1) / np.sqrt(len(own)))), 2),
            "engine_net_mean_bp": round(1e4 * float(engine_net.mean()), 3),
            "same_sign": bool(np.sign(own.mean()) == np.sign(engine_net.mean())),
            "passes": bool(agree >= AGREEMENT_FLOOR and np.sign(own.mean()) == np.sign(engine_net.mean())),
        }
    results["check2"] = check2

    # Side arm: spot, long or flat.
    spot_spreads = {**spreads, "BTC-USD": SPOT_FEE_ROUND_TRIP_BP, "ETH-USD": SPOT_FEE_ROUND_TRIP_BP}
    spot_financing = {**financing, "BTC-USD": (0.0, 0.0), "ETH-USD": (0.0, 0.0)}
    spot = {}
    for ticker in ("BTC-USD", "ETH-USD"):
        cfg = tm.classic_config(config, ticker, start=tm.CRYPTO_START, end=tm.END,
                                spreads=spot_spreads, financing=spot_financing)
        cfg = dataclasses.replace(cfg, signal=dataclasses.replace(cfg.signal, allow_short=False))
        spot[ticker] = tm.book_returns(run_pipeline(DataRepository(config), cfg))
    results["spot_long_flat_book"] = tm.summarise(None, frame=tm.combine(spot), split="2021-01-01")

    Path(args.out).write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(json.dumps({k: v for k, v in results.items() if k != "check1_per_coin"}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
