"""Does the opening half-hour's signed order flow call the rest of the day?

Pre-registered in CLAUDE.md on 2026-11-03 and amended the same day, before
any outcome was read, from the consolidated EQUS.MINI to Nasdaq's own
XNAS.ITCH: the consolidated feed marks the aggressor side on about 4% of
volume, Nasdaq's on about 55%.

Every direction test in this project has read mid-quote prices. This one
reads who initiated each trade. For each session's loudest fifth of the
candidates -- the definition `rolling_direction` uses, through its own
`build_panel` -- the imbalance of buyer- against seller-initiated shares
from 09:30 to 10:00 New York gives the side, scored from the 10:00 bar's
open to the last bar's open net of Capital's measured spread. To pass it
must also beat the same names' 09:30-10:00 PRICE move, paired by session,
because that move was measured as nothing on 2026-09-27.

    python -m cfdinvest.research.order_flow fetch   # spends credit, capped
    python -m cfdinvest.research.order_flow score

Research only; nothing here is read by the dealer. The key is read from
the environment (`DATABENTO_API_KEY`) and never logged. Only per-name
aggregates are kept on disk, never the raw trades.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import logging
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

from cfdinvest.research import rolling_direction as rd

logger = logging.getLogger(__name__)

DATASET = "XNAS.ITCH"
START, END = "2023-03-28", "2026-10-30"
#: Earlier than START so the 20-session volatility that picks the loud
#: names exists on the first scored day.
PANEL_START = "2023-01-01"
OPEN_AT, CUT_AT = (9, 30), (10, 0)
#: Stop before the credit runs out; the estimate was $70-110.
SPEND_CAP_USD = 115.0
SEED = 20261103
BASE_URL = "https://hist.databento.com/v0/"
#: Databento's trade `side`: the side that INITIATED it. B is a buy
#: aggressor, A a sell aggressor, N unmarked. Read off their schema page
#: on 2026-11-03; reading it backwards would invert every call.
BUY, SELL = "B", "A"


# --------------------------------------------------------------------------- #
# The window and the reading -- pure
# --------------------------------------------------------------------------- #
def window(day) -> tuple[pd.Timestamp, pd.Timestamp]:
    """09:30 to 10:00 New York on ``day``, in UTC, DST-correct per day.

    Half-open: Databento's ``end`` is exclusive, and :func:`aggregate`
    applies the same bound, so a trade at 10:00:00 is never in.
    """
    date = pd.Timestamp(day).normalize()
    if date.tzinfo is not None:
        date = date.tz_localize(None)

    def at(hm):
        return (date + pd.Timedelta(hours=hm[0], minutes=hm[1])).tz_localize(
            rd.NEW_YORK).tz_convert("UTC")
    return at(OPEN_AT), at(CUT_AT)


def aggregate(trades: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """Per symbol: signed and unsigned shares, trades, first and last price.

    ``trades`` has ``ts_recv`` (UTC), ``side``, ``size``, ``price`` and
    ``symbol``. The bound is on RECEIPT time, which is what was knowable at
    10:00 -- and it is re-applied here rather than trusted to the request.
    """
    columns = ["buy", "sell", "unsigned", "trades", "first_px", "last_px"]
    if trades is None or trades.empty:
        return pd.DataFrame(columns=columns)
    t = trades[(trades["ts_recv"] >= start) & (trades["ts_recv"] < end)]
    t = t.sort_values("ts_recv", kind="stable")
    grouped = t.groupby("symbol")
    size = t["size"].astype(float)
    out = pd.DataFrame({
        "buy": size.where(t["side"] == BUY, 0.0).groupby(t["symbol"]).sum(),
        "sell": size.where(t["side"] == SELL, 0.0).groupby(t["symbol"]).sum(),
        "unsigned": size.where(~t["side"].isin([BUY, SELL]), 0.0).groupby(t["symbol"]).sum(),
        "trades": grouped.size().astype(float),
        "first_px": grouped["price"].first(),
        "last_px": grouped["price"].last(),
    })
    return out[columns]


def imbalance(buy, sell):
    """(buy - sell) / (buy + sell); NaN, never zero, when nothing is signed.

    Zero would be a confident claim of balance, and the caller drops it as
    "no call" -- an absent reading must not look like a balanced one.
    """
    buy, sell = np.asarray(buy, float), np.asarray(sell, float)
    total = buy + sell
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(total > 0, (buy - sell) / total, np.nan)


# --------------------------------------------------------------------------- #
# Calls and scoring -- pure
# --------------------------------------------------------------------------- #
def calls(loud: pd.DataFrame, flows: pd.DataFrame) -> pd.DataFrame:
    """Join the loud name-days to their flow; one row per name-day.

    ``loud`` carries ``day, ticker, move, mkt_move, cost`` from
    `rolling_direction.build_panel`; ``flows`` carries ``day, ticker`` and
    the aggregate columns. Adds ``imbalance``, ``window_return`` and the two
    sides. A side of 0 is no call.
    """
    frame = loud.merge(flows, on=["day", "ticker"], how="inner")
    frame["imbalance"] = imbalance(frame["buy"], frame["sell"])
    frame["window_return"] = frame["last_px"] / frame["first_px"] - 1
    frame["side_flow"] = np.sign(frame["imbalance"]).fillna(0.0)
    frame["side_price"] = np.sign(frame["window_return"]).fillna(0.0)
    return frame


def shuffled_within_session(frame: pd.DataFrame, column: str, seed: int = SEED) -> pd.Series:
    """The column permuted inside each session, so the day's mix is kept."""
    rng = np.random.default_rng(seed)
    out = frame[column].copy()
    for _, index in frame.groupby("day").groups.items():
        values = out.loc[index].to_numpy()
        out.loc[index] = rng.permutation(values)
    return out


def _with_side(frame: pd.DataFrame, side: pd.Series) -> pd.DataFrame:
    part = frame[["day", "ticker", "move", "mkt_move", "cost"]].copy()
    part["side"] = side.to_numpy()
    return part[part["side"] != 0]


def _net_by_day(part: pd.DataFrame) -> pd.Series:
    cost = part["cost"].fillna(part["cost"].median())
    return (part["side"] * part["move"] - cost).groupby(part["day"]).mean()


def paired(flow_part: pd.DataFrame, price_part: pd.DataFrame) -> dict:
    """Flow's net minus the price control's net, paired by session.

    Only sessions where BOTH made calls are compared, so a day one of them
    sat out cannot be counted as a win for the other.
    """
    a, b = _net_by_day(flow_part), _net_by_day(price_part)
    common = a.index.intersection(b.index)
    diff = (a.loc[common] - b.loc[common]).sort_index()
    n = len(diff)
    sd = diff.std(ddof=1) if n > 1 else np.nan
    half = n // 2
    return {
        "days": int(n),
        "mean_pct": round(100 * float(diff.mean()), 4) if n else None,
        "t": round(float(diff.mean() / (sd / np.sqrt(n))), 2) if n > 2 and sd > 0 else None,
        "h1_pct": round(100 * float(diff.iloc[:half].mean()), 4) if half else None,
        "h2_pct": round(100 * float(diff.iloc[half:].mean()), 4) if n - half else None,
    }


def verdict(primary: dict, versus_price: dict) -> str:
    """The registered branches, read mechanically."""
    net, gross = primary.get("net") or {}, primary.get("gross") or {}
    passes_net = (net.get("t") or 0) >= 2 and (net.get("h1_pct") or 0) > 0 and (net.get("h2_pct") or 0) > 0
    beats_price = (versus_price.get("t") or 0) >= 2
    if passes_net and beats_price:
        return "pass: a real direction source -- build it as an engine input and measure it there"
    if passes_net or (gross.get("t") or 0) >= 2:
        return "real or redundant, and not tradeable on Capital's spreads"
    return "otherwise: opening order flow does not call the rest of the day on these names"


def study(frame: pd.DataFrame) -> dict:
    """Every registered reading, from the joined name-days. Pure."""
    flow = _with_side(frame, frame["side_flow"])
    price = _with_side(frame, frame["side_price"])
    shuffled = _with_side(frame, shuffled_within_session(frame, "side_flow"))
    strong_cut = frame["imbalance"].abs().quantile(2 / 3)
    strong = frame[frame["imbalance"].abs() >= strong_cut]
    signed = frame["buy"] + frame["sell"]
    total = signed + frame["unsigned"]
    ok = frame[["imbalance", "window_return"]].dropna()
    primary = rd.score(flow)
    versus = paired(flow, price)
    return {
        "dataset": DATASET,
        "name_days": int(len(frame)),
        "sessions": int(frame["day"].nunique()),
        "no_call_name_days": int((frame["side_flow"] == 0).sum()),
        "unsigned_share_of_volume": round(float(frame["unsigned"].sum() / total.sum()), 4)
        if total.sum() else None,
        # A check on the sign convention, not on the outcome: buy pressure
        # inside 09:30-10:00 should go with the price rising inside it.
        "sign_check_rank_corr_imbalance_vs_window_return":
            round(float(ok["imbalance"].rank().corr(ok["window_return"].rank())), 4) if len(ok) > 2 else None,
        "primary": primary,
        "control_price_move": rd.score(price),
        "flow_minus_price_paired": versus,
        "control_shuffled_within_session": rd.score(shuffled),
        "secondary_strongest_third": rd.score(_with_side(strong, strong["side_flow"])),
        "verdict": verdict(primary, versus),
    }


# --------------------------------------------------------------------------- #
# Inputs: the loud panel, and the flow from Databento
# --------------------------------------------------------------------------- #
def _repaired(bars: pd.DataFrame, daily: pd.DataFrame | None, ticker: str) -> pd.DataFrame:
    """Capital's hourly bars with the split repair `load_market` applies."""
    from cfdinvest.ingestion import price_repair

    if daily is None or "close" not in daily or "close" not in bars:
        return bars
    tidy = bars.copy()
    tidy.index = pd.MultiIndex.from_arrays([bars.index, [ticker] * len(bars)])
    reference = daily["close"].copy()
    reference.index = pd.MultiIndex.from_arrays([pd.to_datetime(reference.index, utc=True),
                                                 [ticker] * len(reference)])
    factors, _ = price_repair.repair_factors(tidy, reference)
    out = bars.copy()
    for column in price_repair.PRICE_COLUMNS:
        if column in out:
            out[column] = out[column] * factors.to_numpy()
    return out


def loud_panel(config) -> pd.DataFrame:
    from cfdinvest.research.direction_review import _bars

    raw = Path(config.paths.raw_market_dir)
    names = list(config.universe.candidates)
    daily = {t: b for t in names if (b := _bars(raw / "1d", t)) is not None}
    hourly = {t: _repaired(b, daily.get(t), t) for t in names
              if (b := _bars(raw / str(config.market.interval), t)) is not None}
    panel = rd.build_panel(hourly, daily, start=PANEL_START, end=END)
    panel = panel[(panel["day"] >= pd.Timestamp(START)) & (panel["day"] <= pd.Timestamp(END))]
    return panel[panel["loud"]][["day", "ticker", "move", "mkt_move", "cost"]].reset_index(drop=True)


def _get(endpoint: str, params: dict, key: str, *, attempts: int = 4) -> bytes:
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(f"{BASE_URL}{endpoint}?{query}")
    request.add_header("Authorization",
                       "Basic " + base64.b64encode(f"{key}:".encode()).decode())
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            if error.code < 500 or attempt == attempts - 1:
                raise RuntimeError(f"{endpoint} {error.code}: {error.read()[:300]!r}") from None
        except urllib.error.URLError:
            if attempt == attempts - 1:
                raise
        time.sleep(5 * (attempt + 1))
    raise RuntimeError("unreachable")


def _request(symbols, start, end) -> dict:
    return {"dataset": DATASET, "schema": "trades", "symbols": ",".join(symbols),
            "stype_in": "raw_symbol", "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": end.strftime("%Y-%m-%dT%H:%M:%SZ")}


def fetch_day(day, symbols, key: str) -> tuple[pd.DataFrame, float, int]:
    """One session's window for the given names. Returns (flows, cost, rows)."""
    start, end = window(day)
    params = _request(symbols, start, end)
    cost = float(_get("metadata.get_cost", params, key))
    body = _get("timeseries.get_range", {**params, "encoding": "csv", "compression": "zstd",
                                          "pretty_px": "true", "pretty_ts": "true",
                                          "map_symbols": "true"}, key)
    text = subprocess.run(["zstd", "-dc"], input=body, capture_output=True, check=True).stdout
    if not text.strip():
        return aggregate(None, start, end), cost, 0
    trades = pd.read_csv(io.BytesIO(text), usecols=["ts_recv", "side", "price", "size", "symbol"])
    trades["ts_recv"] = pd.to_datetime(trades["ts_recv"], utc=True, format="ISO8601")
    return aggregate(trades, start, end), cost, len(trades)


def quote_day(day, symbols, key: str) -> float:
    start, end = window(day)
    return float(_get("metadata.get_cost", _request(symbols, start, end), key))


def run_fetch(days_symbols: dict, folder: Path, key: str, *, cap: float = SPEND_CAP_USD,
              fetch=fetch_day, quote=quote_day) -> dict:
    """Fetch every session not already on disk, stopping BEFORE the cap.

    Each day is priced first and skipped-and-stopped if it would take the
    running total over the cap, so the cap is never crossed -- checking
    after the spend would cross it by one day.
    """
    folder.mkdir(parents=True, exist_ok=True)
    ledger_path = folder / "spend.json"
    ledger = json.loads(ledger_path.read_text()) if ledger_path.is_file() else {"spent": 0.0, "days": 0}
    for day, symbols in sorted(days_symbols.items()):
        out = folder / f"{pd.Timestamp(day):%Y-%m-%d}.json"
        if out.is_file() or not symbols:
            continue
        price = quote(day, symbols, key)
        if ledger["spent"] + price > cap:
            ledger["stopped_at"] = f"{pd.Timestamp(day):%Y-%m-%d}"
            logger.warning("Stopping before %s: $%.2f spent, next $%.3f would pass the $%.2f cap",
                           ledger["stopped_at"], ledger["spent"], price, cap)
            break
        flows, cost, rows = fetch(day, symbols, key)
        out.write_text(json.dumps({
            "day": f"{pd.Timestamp(day):%Y-%m-%d}", "requested": list(symbols), "rows": rows,
            "cost": cost, "flows": json.loads(flows.to_json(orient="index")),
        }))
        ledger["spent"] = round(ledger["spent"] + cost, 6)
        ledger["days"] += 1
        ledger_path.write_text(json.dumps(ledger))
    ledger_path.write_text(json.dumps(ledger))
    return ledger


def load_flows(folder: Path) -> pd.DataFrame:
    rows = []
    for path in sorted(folder.glob("????-??-??.json")):
        record = json.loads(path.read_text())
        for symbol, values in (record.get("flows") or {}).items():
            rows.append({"day": pd.Timestamp(record["day"]), "ticker": symbol, **values})
    return pd.DataFrame(rows)


def main(argv=None) -> int:
    from cfdinvest.config import load_config
    from cfdinvest.persist.storage import account_root

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("step", choices=["fetch", "score"])
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    config = load_config()
    folder = Path(account_root(config, None)) / "research" / "order_flow"
    days = folder / "days"
    loud = loud_panel(config)
    logger.info("%d loud name-days over %d sessions", len(loud), loud["day"].nunique())
    if args.step == "fetch":
        key = os.environ.get("DATABENTO_API_KEY")
        if not key:
            logger.error("DATABENTO_API_KEY is not set")
            return 2
        wanted = {d: sorted(g["ticker"]) for d, g in loud.groupby("day")}
        print(json.dumps(run_fetch(wanted, days, key), indent=2))
        return 0
    flows = load_flows(days)
    if flows.empty:
        logger.error("no flows fetched yet")
        return 2
    result = study(calls(loud, flows))
    (folder / "results.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
