"""Can a model retrained every day on the last fortnight call the side?

Pre-registered in CLAUDE.md on 2026-10-01, before any model was trained.
The owner's framing: the book needs a side for hours, not months, so a
model refitted daily on only the most recent sessions can follow whatever
is working now -- and that is all anyone can trade on. The one direction
model fitted before was a single fit over twenty months; this design had
not been tested.

Each session, the loudest fifth of the candidates by trailing realised
volatility is given a side from 10:00 New York to the last bar's open, by
a model fitted on ALL candidates over the previous W completed sessions.
Features use only bars complete by 10:00. Scored net of each bar's
measured spread, and market-neutral, per session, t across sessions.

    python -m cfdinvest.research.rolling_direction

Research only. Nothing here is read by the dealer. The gradient-boosting
arm needs scikit-learn, which is not a project dependency; without it that
arm is reported absent rather than skipped silently.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

NEW_YORK = "America/New_York"
START, END = "2023-01-01", "2026-09-29"
LOUD_SHARE = 0.20
VOL_SESSIONS = 20
#: Sessions skipped at the start so every arm, up to W = 20, is scored on
#: the same days.
WARM_UP = 45
PRIMARY_WINDOW = 10
SEED = 20261001
FEATURES = (
    "pre", "last_hour", "r1", "r5", "r10", "range_pos", "prev_shape",
    "prev_last_hour", "rel_volume", "vol",
    "mkt_pre", "mkt_r1", "mkt_r5", "pre_vs_mkt", "r1_vs_mkt", "r5_vs_mkt",
)


# --------------------------------------------------------------------------- #
# Features -- per ticker, cut at 10:00
# --------------------------------------------------------------------------- #
def _clock(days: pd.DatetimeIndex, hour: int) -> pd.DatetimeIndex:
    """``hour``:00 New York on each date, in UTC (DST-correct per day)."""
    return (days + pd.Timedelta(hours=hour)).tz_localize(NEW_YORK).tz_convert("UTC")


def ticker_days(hourly: pd.DataFrame, daily: pd.DataFrame | None,
                days: pd.DatetimeIndex) -> pd.DataFrame:
    """One row per session date for one name. Pure.

    Every feature on row ``d`` is built from bars that are COMPLETE by
    10:00 New York on ``d``: the 09:00 bar (closes at 10:00), earlier
    sessions, and daily volume up to the previous date. ``move`` and
    ``cost`` -- the outcome -- are the only fields that read later bars.
    """
    ten, nine, last = _clock(days, 10), _clock(days, 9), _clock(days, 15)
    at = lambda stamps, col: hourly[col].reindex(stamps).to_numpy()
    frame = pd.DataFrame(index=days)
    frame["o10"] = at(ten, "open")
    frame["o_last"] = at(last, "open")
    frame["c_last"] = at(last, "close")
    frame["o9"], frame["c9"] = at(nine, "open"), at(nine, "close")
    spread = (at(ten, "spread_bp") + at(last, "spread_bp")) / 2 / 1e4
    frame["cost"] = spread
    frame["move"] = frame["o_last"] / frame["o10"] - 1

    # Session-hour bars only, 09:00-15:00 New York, for range and volatility.
    local = hourly.index.tz_convert(NEW_YORK)
    in_session = hourly[(local.hour >= 9) & (local.hour <= 15)]
    session_day = in_session.index.tz_convert(NEW_YORK).normalize().tz_localize(None)
    grouped = in_session.groupby(session_day)
    hi, lo = grouped["high"].max().reindex(days), grouped["low"].min().reindex(days)
    vol = np.log(in_session["close"]).diff().groupby(session_day).std().reindex(days)

    # Keep only days the name actually traded, so a shift is one SESSION.
    valid = frame[["o10", "o_last", "c_last", "c9"]].notna().all(axis=1)
    frame, hi, lo, vol = frame[valid], hi[valid], lo[valid], vol[valid]
    close = frame["c_last"]
    prev_close = close.shift(1)
    frame["pre"] = frame["c9"] / prev_close - 1
    frame["last_hour"] = frame["c9"] / frame["o9"] - 1
    frame["r1"] = close.shift(1) / close.shift(2) - 1
    frame["r5"] = close.shift(1) / close.shift(6) - 1
    frame["r10"] = close.shift(1) / close.shift(11) - 1
    high10 = hi.shift(1).rolling(10, min_periods=8).max()
    low10 = lo.shift(1).rolling(10, min_periods=8).min()
    frame["range_pos"] = (prev_close - low10) / (high10 - low10)
    frame["prev_shape"] = (close / frame["o10"] - 1).shift(1)
    frame["prev_last_hour"] = (close / frame["o_last"] - 1).shift(1)
    frame["vol"] = vol.rolling(VOL_SESSIONS, min_periods=15).mean().shift(1)
    if daily is not None and "volume" in daily:
        v = daily["volume"].copy()
        v.index = pd.DatetimeIndex(pd.to_datetime(v.index, utc=True).date)
        rel = v / v.shift(1).rolling(20, min_periods=15).median()
        # Yesterday's: known after its close, so it informs today.
        frame["rel_volume"] = rel.reindex(frame.index, method="ffill").shift(1).to_numpy()
    else:
        frame["rel_volume"] = np.nan
    return frame


def build_panel(hourly: dict, daily: dict, start=START, end=END) -> pd.DataFrame:
    """All names, long format, with the market terms. Pure."""
    days = pd.bdate_range(start, end)
    rows = []
    for ticker, bars in hourly.items():
        part = ticker_days(bars, daily.get(ticker), days)
        part["ticker"] = ticker
        rows.append(part)
    panel = pd.concat(rows).rename_axis("day").reset_index()
    market = panel.groupby("day")[["pre", "r1", "r5", "move"]].mean()
    market.columns = ["mkt_pre", "mkt_r1", "mkt_r5", "mkt_move"]
    panel = panel.merge(market, left_on="day", right_index=True)
    panel["pre_vs_mkt"] = panel["pre"] - panel["mkt_pre"]
    panel["r1_vs_mkt"] = panel["r1"] - panel["mkt_r1"]
    panel["r5_vs_mkt"] = panel["r5"] - panel["mkt_r5"]
    # Loudness, ranked within each day.
    panel["loud"] = panel.groupby("day")["vol"].rank(pct=True) > 1 - LOUD_SHARE
    return panel.dropna(subset=list(FEATURES) + ["move"]).reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
def fit_logistic(X: np.ndarray, y: np.ndarray, l2: float = 1.0, iterations: int = 25):
    """L2 logistic regression by Newton's method; the intercept is unpenalised.

    Returns weights including the intercept as element 0. ``y`` is 0/1.
    """
    A = np.column_stack([np.ones(len(X)), X])
    w = np.zeros(A.shape[1])
    penalty = np.full(A.shape[1], l2)
    penalty[0] = 0.0
    for _ in range(iterations):
        p = 1 / (1 + np.exp(-np.clip(A @ w, -30, 30)))
        gradient = A.T @ (p - y) + penalty * w
        hessian = (A * (p * (1 - p))[:, None]).T @ A + np.diag(penalty)
        step = np.linalg.solve(hessian + 1e-9 * np.eye(len(w)), gradient)
        w -= step
        if np.abs(step).max() < 1e-8:
            break
    return w


def predict_logistic(w: np.ndarray, X: np.ndarray) -> np.ndarray:
    return 1 / (1 + np.exp(-np.clip(np.column_stack([np.ones(len(X)), X]) @ w, -30, 30)))


def training_days(sessions: list, i: int, window: int) -> list:
    """The W completed sessions before session ``i`` -- never ``i`` itself."""
    return sessions[max(0, i - window):i]


def calls(panel: pd.DataFrame, *, window: int, model: str = "logistic",
          loud_only_training: bool = False, shuffle: bool = False) -> pd.DataFrame:
    """Walk forward one session at a time; a side for each loud name."""
    sessions = sorted(panel["day"].unique())
    by_day = {d: g for d, g in panel.groupby("day")}
    rng = np.random.default_rng(SEED)
    out = []
    for i in range(WARM_UP, len(sessions)):
        day = sessions[i]
        train = pd.concat([by_day[d] for d in training_days(sessions, i, window)])
        if loud_only_training:
            train = train[train["loud"]]
        test = by_day[day][by_day[day]["loud"]]
        if len(train) < 30 or test.empty:
            continue
        X = train[list(FEATURES)].to_numpy(float)
        mean, sd = X.mean(0), X.std(0)
        sd[sd == 0] = 1.0
        X = (X - mean) / sd
        y = (train["move"].to_numpy() > 0).astype(float)
        if shuffle:
            y = rng.permutation(y)
        T = (test[list(FEATURES)].to_numpy(float) - mean) / sd
        if model == "logistic":
            p = predict_logistic(fit_logistic(X, y), T)
        else:
            from sklearn.ensemble import HistGradientBoostingClassifier

            if len(np.unique(y)) < 2:
                continue
            clf = HistGradientBoostingClassifier(max_iter=100, max_depth=3,
                                                 learning_rate=0.1, random_state=SEED)
            p = clf.fit(X, y).predict_proba(T)[:, 1]
        part = test[["day", "ticker", "move", "mkt_move", "cost"]].copy()
        part["side"] = np.where(p > 0.5, 1, -1)
        out.append(part)
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #
def _summary(frame: pd.DataFrame, column: str) -> dict:
    per_day = frame.groupby("day")[column].mean().sort_index()
    n, half = len(per_day), len(per_day) // 2
    sd = per_day.std(ddof=1)
    return {
        "days": int(n), "mean_pct": round(100 * float(per_day.mean()), 4),
        "t": round(float(per_day.mean() / (sd / np.sqrt(n))), 2) if n > 2 and sd > 0 else None,
        "h1_pct": round(100 * float(per_day.iloc[:half].mean()), 4),
        "h2_pct": round(100 * float(per_day.iloc[half:].mean()), 4),
    }


def score(made: pd.DataFrame) -> dict:
    if made.empty:
        return {"absent": "no calls were made"}
    frame = made.copy()
    cost = frame["cost"].fillna(frame["cost"].median())
    frame["gross"] = frame["side"] * frame["move"]
    frame["net"] = frame["gross"] - cost
    frame["neutral"] = frame["side"] * (frame["move"] - frame["mkt_move"])
    year = pd.to_datetime(frame["day"]).dt.year
    return {
        "calls": int(len(frame)),
        "gross": _summary(frame, "gross"), "net": _summary(frame, "net"),
        "market_neutral_gross": _summary(frame, "neutral"),
        "hit_rate": round(float((frame["gross"] > 0).mean()), 4),
        "long_share": round(float((frame["side"] > 0).mean()), 3),
        "net_by_year_pct": {int(y): round(100 * float(g.groupby("day")["net"].mean().mean()), 4)
                            for y, g in frame.groupby(year)},
        "median_cost_pct": round(100 * float(cost.median()), 4),
    }


def study(panel: pd.DataFrame) -> dict:
    result = {"primary": score(calls(panel, window=PRIMARY_WINDOW))}
    for w in (5, 20):
        result[f"window_{w}"] = score(calls(panel, window=w))
    result["loud_only_training"] = score(calls(panel, window=PRIMARY_WINDOW, loud_only_training=True))
    try:
        import sklearn  # noqa: F401

        result["gradient_boosting"] = score(calls(panel, window=PRIMARY_WINDOW, model="boosting"))
    except ImportError:
        result["gradient_boosting"] = {"absent": "scikit-learn is not installed"}
    result["control_shuffled_labels"] = score(calls(panel, window=PRIMARY_WINDOW, shuffle=True))
    momentum = panel[panel["loud"] & panel["day"].isin(sorted(panel["day"].unique())[WARM_UP:])].copy()
    momentum["side"] = np.where(momentum["r5"] > 0, 1, -1)
    result["control_momentum_5"] = score(momentum[["day", "ticker", "move", "mkt_move", "cost", "side"]])
    return result


def main(argv=None) -> int:
    from cfdinvest.config import load_config
    from cfdinvest.persist.storage import account_root
    from cfdinvest.research.direction_review import _bars

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    config = load_config()
    raw = Path(config.paths.raw_market_dir)
    names = list(config.universe.candidates)
    hourly = {t: b for t in names if (b := _bars(raw / str(config.market.interval), t)) is not None}
    daily = {t: b for t in names if (b := _bars(raw / "1d", t)) is not None}
    panel = build_panel(hourly, daily)
    logger.info("%d name-days, %d sessions, %d loud", len(panel), panel["day"].nunique(), int(panel["loud"].sum()))
    result = study(panel)
    folder = Path(account_root(config, None)) / "research" / "rolling_direction"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "results.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
