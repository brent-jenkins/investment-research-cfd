"""Does the last hour, in minutes, call the next one to four hours?

Pre-registered in CLAUDE.md on 2026-10-01, before any model was trained.
Every hourly test read a bar's outcome; this reads the shape inside the
hour, which only Capital's minute bars carry -- acceleration, tick-activity
surges, spread widening, price against the hour's tick-weighted mean.

At 11:00, 12:00, 13:00 and 14:00 New York, each day's loudest fifth of the
66 names with minute bars is given a side over three horizons, by a model
per horizon refitted every session on the previous W sessions of all names
and all decision times. Features read only minutes stamped BEFORE the
decision. Scored against each end's own quoted spread.

    python -m cfdinvest.research.minute_direction

Research only. Nothing here is read by the dealer.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from cfdinvest.research.rolling_direction import fit_logistic, predict_logistic, training_days

logger = logging.getLogger(__name__)

NEW_YORK = "America/New_York"
START, END = "2026-03-16", "2026-09-30"
DECISION_HOURS = (11, 12, 13, 14)
HORIZONS = {"1h": 60, "2h": 120, "close": None}  # close = the 15:00 bar's open
CLOSE_HOUR = 15
LOUD_SHARE = 0.20
WARM_UP = 30
PRIMARY_WINDOW = 10
SEED = 20261001
#: An entry or exit minute may be missing; take the nearest within this.
TOLERANCE = pd.Timedelta(minutes=5)
FEATURES = (
    "r5", "r15", "r60", "r_day", "gap", "accel", "pos60", "vs_mean60",
    "ticks15_vs_60", "ticks60_vs_usual", "spread_vs_day", "rv60", "rv_day",
    "mkt_r60", "mkt_r_day", "r60_vs_mkt", "r_day_vs_mkt", "hour",
)


def _at(series: pd.Series, stamps: pd.DatetimeIndex, method: str) -> np.ndarray:
    return series.reindex(stamps, method=method, tolerance=TOLERANCE).to_numpy()


def ticker_rows(bars: pd.DataFrame, days: pd.DatetimeIndex) -> pd.DataFrame:
    """One row per (session, decision hour) for one name. Pure.

    Every feature is read at ``before`` = the minute stamped one minute
    before the decision, whose bar ends AT the decision, with rolling
    windows ending there. Only ``move_*`` and ``cost_*`` -- the outcome --
    read the decision minute or later.
    """
    bars = bars[~bars.index.duplicated()].sort_index()
    local = bars.index.tz_convert(NEW_YORK)
    session = bars[(local.hour * 60 + local.minute >= 570) & (local.hour < 16)]
    day_of = session.index.tz_convert(NEW_YORK).normalize().tz_localize(None)
    close, opens, ticks = session["close"], session["open"], session["volume"]
    logret = np.log(close).diff()
    logret[day_of != pd.Series(day_of, index=session.index).shift(1).to_numpy()] = np.nan

    roll = lambda s, w, f: getattr(s.rolling(f"{w}min"), f)()
    high60, low60 = roll(session["high"], 60, "max"), roll(session["low"], 60, "min")
    ticks60, ticks15 = roll(ticks, 60, "sum"), roll(ticks, 15, "sum")
    mean60 = roll(close * ticks, 60, "sum") / ticks60.replace(0, np.nan)
    rv60 = roll(logret, 60, "std")
    spread15 = roll(session["spread_bp"], 15, "mean")
    grouped = pd.Series(day_of, index=session.index)
    spread_day = session["spread_bp"].groupby(grouped.to_numpy()).expanding().mean().droplevel(0)
    rv_day = logret.groupby(grouped.to_numpy()).expanding().std().droplevel(0)

    open_930 = opens.groupby(day_of).first().reindex(days)
    last_close = close.groupby(day_of).last().reindex(days)
    daily_rv = logret.groupby(day_of).std().reindex(days)
    predicted = daily_rv.dropna().rolling(20, min_periods=15).mean().shift(1).reindex(days)

    rows = []
    for hour in DECISION_HOURS:
        decision = (days + pd.Timedelta(hours=hour)).tz_localize(NEW_YORK).tz_convert("UTC")
        before = decision - pd.Timedelta(minutes=1)
        c_before = _at(close, before, "ffill")
        frame = pd.DataFrame({"day": days, "hour": hour})
        frame["entry"] = _at(opens, decision, "bfill")
        frame["cost_entry"] = _at(session["spread_bp"], decision, "bfill")
        for name, minutes in HORIZONS.items():
            exit_at = (decision + pd.Timedelta(minutes=minutes) if minutes else
                       (days + pd.Timedelta(hours=CLOSE_HOUR)).tz_localize(NEW_YORK).tz_convert("UTC"))
            valid = exit_at > decision
            frame[f"move_{name}"] = np.where(valid, _at(opens, exit_at, "bfill") / frame["entry"] - 1, np.nan)
            frame[f"cost_{name}"] = (frame["cost_entry"] + _at(session["spread_bp"], exit_at, "bfill")) / 2 / 1e4
        for minutes in (5, 15, 60):
            # ffill, never bfill: a missing minute must not reach forward
            # to the decision minute itself.
            frame[f"r{minutes}"] = c_before / _at(opens, decision - pd.Timedelta(minutes=minutes), "ffill") - 1
        frame["r_day"] = c_before / open_930.to_numpy() - 1
        frame["gap"] = open_930.to_numpy() / last_close.shift(1).to_numpy() - 1
        frame["accel"] = frame["r15"] - frame["r60"] / 4
        hi, lo = _at(high60, before, "ffill"), _at(low60, before, "ffill")
        frame["pos60"] = (c_before - lo) / np.where(hi - lo > 0, hi - lo, np.nan)
        frame["vs_mean60"] = c_before / _at(mean60, before, "ffill") - 1
        t60, t15 = _at(ticks60, before, "ffill"), _at(ticks15, before, "ffill")
        frame["ticks15_vs_60"] = t15 / np.where(t60 > 0, t60 / 4, np.nan)
        frame["ticks60_raw"] = t60
        frame["spread_vs_day"] = _at(spread15, before, "ffill") / _at(spread_day, before, "ffill")
        frame["rv60"] = _at(rv60, before, "ffill")
        frame["rv_day"] = _at(rv_day, before, "ffill")
        frame["predicted_vol"] = predicted.to_numpy()
        # The name's usual activity for this hour: previous 20 sessions.
        usual = pd.Series(t60).rolling(20, min_periods=10).median().shift(1).to_numpy()
        frame["ticks60_vs_usual"] = t60 / np.where(usual > 0, usual, np.nan)
        rows.append(frame)
    return pd.concat(rows, ignore_index=True)


def build_panel(minute: dict, start=START, end=END) -> pd.DataFrame:
    days = pd.bdate_range(start, end)
    parts = []
    for ticker, bars in minute.items():
        part = ticker_rows(bars, days)
        part["ticker"] = ticker
        parts.append(part)
    panel = pd.concat(parts, ignore_index=True)
    panel = panel.dropna(subset=["entry", "r60", "predicted_vol"])
    keys = ["day", "hour"]
    market = panel.groupby(keys)[["r60", "r_day"] + [f"move_{h}" for h in HORIZONS]].mean()
    market.columns = ["mkt_r60", "mkt_r_day"] + [f"mkt_move_{h}" for h in HORIZONS]
    panel = panel.merge(market, left_on=keys, right_index=True)
    panel["r60_vs_mkt"] = panel["r60"] - panel["mkt_r60"]
    panel["r_day_vs_mkt"] = panel["r_day"] - panel["mkt_r_day"]
    loud = panel.groupby("day")["predicted_vol"].rank(pct=True) > 1 - LOUD_SHARE
    panel["loud"] = loud
    return panel.dropna(subset=list(FEATURES)).reset_index(drop=True)


def calls(panel: pd.DataFrame, horizon: str, *, window: int, model: str = "logistic",
          shuffle: bool = False) -> pd.DataFrame:
    target = f"move_{horizon}"
    data = panel.dropna(subset=[target])
    sessions = sorted(data["day"].unique())
    by_day = {d: g for d, g in data.groupby("day")}
    rng = np.random.default_rng(SEED)
    out = []
    for i in range(WARM_UP, len(sessions)):
        train = pd.concat([by_day[d] for d in training_days(sessions, i, window)])
        test = by_day[sessions[i]]
        test = test[test["loud"]]
        if len(train) < 50 or test.empty:
            continue
        X = train[list(FEATURES)].to_numpy(float)
        mean, sd = X.mean(0), X.std(0)
        sd[sd == 0] = 1.0
        y = (train[target].to_numpy() > 0).astype(float)
        if shuffle:
            y = rng.permutation(y)
        Xs, T = (X - mean) / sd, (test[list(FEATURES)].to_numpy(float) - mean) / sd
        if model == "logistic":
            p = predict_logistic(fit_logistic(Xs, y), T)
        else:
            from sklearn.ensemble import HistGradientBoostingClassifier

            p = HistGradientBoostingClassifier(max_iter=100, max_depth=3, learning_rate=0.1,
                                               random_state=SEED).fit(Xs, y).predict_proba(T)[:, 1]
        part = test[["day", "hour", "ticker"]].copy()
        part["move"], part["mkt_move"] = test[target].to_numpy(), test[f"mkt_{target}"].to_numpy()
        part["cost"] = test[f"cost_{horizon}"].to_numpy()
        part["side"] = np.where(p > 0.5, 1, -1)
        out.append(part)
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def _summary(frame: pd.DataFrame, column: str) -> dict:
    per_day = frame.groupby("day")[column].mean().sort_index()
    n, half = len(per_day), len(per_day) // 2
    sd = per_day.std(ddof=1)
    return {"days": int(n), "mean_pct": round(100 * float(per_day.mean()), 4),
            "t": round(float(per_day.mean() / (sd / np.sqrt(n))), 2) if n > 2 and sd > 0 else None,
            "h1_pct": round(100 * float(per_day.iloc[:half].mean()), 4),
            "h2_pct": round(100 * float(per_day.iloc[half:].mean()), 4)}


def score(made: pd.DataFrame) -> dict:
    if made.empty:
        return {"absent": "no calls were made"}
    f = made.copy()
    cost = f["cost"].fillna(f["cost"].median())
    f["gross"] = f["side"] * f["move"]
    f["net"] = f["gross"] - cost
    f["neutral"] = f["side"] * (f["move"] - f["mkt_move"])
    return {"calls": int(len(f)), "hit_rate": round(float((f["gross"] > 0).mean()), 4),
            "long_share": round(float((f["side"] > 0).mean()), 3),
            "mean_abs_move_pct": round(100 * float(f["move"].abs().mean()), 4),
            "median_cost_pct": round(100 * float(cost.median()), 4),
            "gross": _summary(f, "gross"), "net": _summary(f, "net"),
            "market_neutral_gross": _summary(f, "neutral")}


def study(panel: pd.DataFrame) -> dict:
    result = {}
    for horizon in HORIZONS:
        arms = {"primary": score(calls(panel, horizon, window=PRIMARY_WINDOW)),
                "window_20": score(calls(panel, horizon, window=20)),
                "control_shuffled": score(calls(panel, horizon, window=PRIMARY_WINDOW, shuffle=True))}
        try:
            import sklearn  # noqa: F401

            arms["gradient_boosting"] = score(calls(panel, horizon, window=PRIMARY_WINDOW, model="boosting"))
        except ImportError:
            arms["gradient_boosting"] = {"absent": "scikit-learn is not installed"}
        data = panel.dropna(subset=[f"move_{horizon}"])
        data = data[data["loud"] & data["day"].isin(sorted(data["day"].unique())[WARM_UP:])]
        momentum = data[["day", "hour", "ticker"]].copy()
        momentum["move"], momentum["mkt_move"] = data[f"move_{horizon}"], data[f"mkt_move_{horizon}"]
        momentum["cost"], momentum["side"] = data[f"cost_{horizon}"], np.where(data["r60"] > 0, 1, -1)
        arms["control_last_hour_sign"] = score(momentum)
        result[horizon] = arms
    return result


def main(argv=None) -> int:
    from cfdinvest.config import load_config
    from cfdinvest.persist.storage import account_root
    from cfdinvest.research.direction_review import _bars

    argparse.ArgumentParser(description=__doc__.splitlines()[0]).parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    config = load_config()
    folder = Path(config.paths.raw_market_dir) / "capital_1m"
    minute = {}
    for path in sorted(folder.glob("*.parquet")):
        bars = _bars(folder, path.stem)
        if bars is not None and bars.index.max() >= pd.Timestamp(START, tz="UTC"):
            minute[path.stem] = bars[bars.index >= pd.Timestamp(START, tz="UTC") - pd.Timedelta(days=45)]
    panel = build_panel(minute)
    logger.info("%d rows, %d names, %d sessions, %d loud", len(panel), panel["ticker"].nunique(),
                panel["day"].nunique(), int(panel["loud"].sum()))
    result = study(panel)
    out = Path(account_root(config, None)) / "research" / "minute_direction"
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
