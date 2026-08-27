"""One standard evaluation, so the same questions get asked every time.

Every performance number in this project was, until now, produced by a
throwaway script. That is how two tables came to disagree about "the full
nineteen" while both were correct -- one used the shipped entry threshold and
the other replaced it, and nothing recorded which. It is also how a headline
CAGR got quoted for weeks with the time stop silently truncated to zero.

So this is the battery, fixed and named. It answers the questions that have
actually mattered here, in the order they matter:

**Is there anything there at all?** The shuffle control. Take the identical
trades with identical costs and choose the direction at random, many times.
A signal that cannot beat a coin toss paying the same spread is not a signal.

**Is it beta?** The benchmark and the long/short split. This universe was
assembled with hindsight and rose 22.4% a year over the window, so a
long-only result proves nothing. Shorts working is the hard thing to fake.

**Can it be harvested?** The slippage curve. The break-even is the number
that decides whether an edge is reachable by a real account, and it has
mattered more than any headline here.

**Does it survive being chosen?** Walk-forward. Fit on a rolling window,
trade the next stretch blind, and score only what was never seen.

**What did it cost?** The breakdown. Spread has been 79% of it since the
move to hourly bars, which reversed a year of assuming carry dominated.

Nothing here is fitted. Every figure is computed from the configuration as
shipped, and the walk-forward is the only part that selects anything.
"""

from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "Evaluation",
    "evaluate",
    "render",
    "SLIPPAGE_STEPS",
]

#: Friction levels the curve is measured at, in basis points.
#:
#: Spans the plausible range for a retail CFD: nothing, through what a
#: £1,000 order should suffer at a broker's own quote, to the point where
#: the edge is gone. The break-even matters more than any of the individual
#: figures.
SLIPPAGE_STEPS: tuple[float, ...] = (0.0, 1.0, 2.0, 4.0, 6.0, 10.0)

#: Random direction draws for the shuffle control. Enough that the real
#: result is compared against a distribution rather than one lucky sample.
SHUFFLE_DRAWS = 200


@dataclass(frozen=True, slots=True)
class Evaluation:
    """Everything one evaluation found. Absent parts stay absent."""

    window: dict[str, Any] = field(default_factory=dict)
    headline: dict[str, Any] = field(default_factory=dict)
    benchmark: dict[str, Any] = field(default_factory=dict)
    costs: dict[str, Any] = field(default_factory=dict)
    sides: dict[str, Any] = field(default_factory=dict)
    slippage: tuple[tuple[float, float], ...] = ()
    breakeven_bps: float | None = None
    shuffle: dict[str, Any] = field(default_factory=dict)
    walkforward: dict[str, Any] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    def as_row(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def _curve_stats(equity) -> dict[str, Any]:
    """CAGR, drawdown and Sharpe from an equity curve. Absent if too short."""
    import numpy as np
    import pandas as pd

    series = equity.dropna() if equity is not None else None
    if series is None or len(series) < 3:
        return {"cagr": None, "max_drawdown": None, "sharpe": None, "years": None}

    index = pd.DatetimeIndex(series.index)
    years = (index[-1] - index[0]).days / 365.25
    if years <= 0 or series.iloc[0] <= 0:
        return {"cagr": None, "max_drawdown": None, "sharpe": None, "years": years}

    total = float(series.iloc[-1] / series.iloc[0])
    returns = series.pct_change().dropna()
    sharpe = None
    if len(returns) > 2 and returns.std() > 0:
        sharpe = float(returns.mean() / returns.std() * np.sqrt(len(returns) / years))

    return {
        "cagr": (total ** (1 / years) - 1) * 100,
        "total_return": (total - 1) * 100,
        "max_drawdown": float(((series / series.cummax()) - 1).min() * 100),
        "sharpe": sharpe,
        "years": years,
    }


def _round_trips(result, confidence=None) -> Any:
    """Completed round trips, with the side and confidence each was taken at.

    A flip closes one position and opens another, so it counts as a close
    here. Positions still open at the end are excluded: an unrealised
    position is not a result.
    """
    import numpy as np
    import pandas as pd

    trips: list[dict[str, Any]] = []
    live: dict[str, dict[str, Any]] = {}
    for row in result.trades.sort_values("timestamp").itertuples():
        action = str(row.action)
        if action.startswith("open"):
            level = np.nan
            if confidence is not None:
                try:
                    level = float(confidence.loc[row.timestamp, row.asset])
                except Exception:  # noqa: BLE001
                    level = np.nan
            live[row.asset] = {
                "asset": row.asset,
                "pnl": row.realized_pnl,
                "notional": abs(row.notional),
                "side": "long" if row.units > 0 else "short",
                "confidence": level,
                "opened": row.timestamp,
            }
        elif action == "resize":
            if row.asset in live:
                live[row.asset]["pnl"] += row.realized_pnl
        elif action.startswith("close") or action.startswith("flip"):
            held = live.pop(row.asset, None)
            if held and held["notional"] > 0:
                held["pnl"] += row.realized_pnl
                trips.append(held)

    frame = pd.DataFrame(trips)
    if not frame.empty:
        frame["won"] = (frame.pnl > 0).astype(float)
        frame["pct"] = frame.pnl / frame.notional * 100
    return frame


def _interpolate_breakeven(curve, benchmark_cagr) -> float | None:
    """Where the strategy's CAGR crosses the benchmark's, in basis points.

    Linear between the two bracketing steps. The exact figure is less useful
    than its order of magnitude -- what matters is whether it is 4bp or 40.
    """
    if benchmark_cagr is None:
        return None
    for (low_bps, low), (high_bps, high) in zip(curve, curve[1:]):
        if low is None or high is None:
            continue
        if low >= benchmark_cagr > high:
            span = low - high
            if span <= 0:
                return float(high_bps)
            share = (low - benchmark_cagr) / span
            return float(low_bps + share * (high_bps - low_bps))
    return None


def evaluate(
    app_config=None,
    *,
    repository=None,
    include_shuffle: bool = True,
    include_walkforward: bool = False,
    slippage_steps: Sequence[float] = SLIPPAGE_STEPS,
    seed: int = 20260827,
) -> Evaluation:
    """Run the battery against the shipped configuration.

    ``include_walkforward`` is off by default because it re-runs the whole
    pipeline once per window and takes minutes rather than seconds. It is
    the most honest figure produced here, so it belongs in any evaluation
    that is going to be quoted.
    """
    import numpy as np
    import pandas as pd

    from cfdinvest.backtest import BacktestConfig, run_backtest_pipeline
    from cfdinvest.config import load_config
    from cfdinvest.execution import run_backtest
    from cfdinvest.repository import DataRepository
    import cfdinvest.schedule.app as schedule_app

    config = app_config or load_config()
    tickers = list(config.market.tickers)
    strategy = config.strategy
    execution = schedule_app._execution_config(tickers, strategy)
    repo = repository or DataRepository(config)

    settings = BacktestConfig(
        tickers=tickers, execution=execution, interval=config.market.interval
    )
    settings = dataclasses.replace(settings, signal=dataclasses.replace(
        settings.signal,
        regime_mode=strategy.regime_mode,
        use_volume=strategy.use_volume,
        environment_in_confidence=strategy.environment_in_confidence,
        hold_across_session_open=strategy.hold_across_session_open,
    ))

    pipeline = run_backtest_pipeline(repo, settings)
    confidence = pipeline.signals.confidence
    notes: list[str] = []

    if confidence is None or getattr(confidence, "empty", True):
        return Evaluation(notes=("no signals were produced; nothing to evaluate",))

    result = run_backtest(pipeline.signals, pipeline.analysis, execution)
    equity = result.equity_curve
    headline = _curve_stats(equity)

    index = pd.DatetimeIndex(equity.dropna().index)
    window = {
        "start": str(index[0]) if len(index) else "",
        "end": str(index[-1]) if len(index) else "",
        "interval": config.market.interval,
        "instruments": len(tickers),
        "entry_confidence": strategy.entry_confidence,
        "slippage_bps": getattr(strategy, "slippage_bps", None),
        "max_leverage": getattr(strategy, "max_leverage", None),
    }

    trips = _round_trips(result, confidence)
    headline["trips"] = int(len(trips))
    headline["trips_per_year"] = (
        len(trips) / headline["years"] if headline.get("years") else None
    )
    headline["win_rate"] = float(trips.won.mean() * 100) if len(trips) else None

    # -- benchmark ---------------------------------------------------------
    # Equal-weight buy-and-hold of the same instruments over the same window.
    # Without it a return on a universe assembled with hindsight says nothing.
    close = pipeline.analysis.prices["close"].reindex(index).ffill()
    hold = close.pct_change().mean(axis=1).fillna(0).add(1).cumprod()
    benchmark = _curve_stats(hold)
    benchmark["description"] = "equal-weight buy and hold, same instruments"

    # -- costs -------------------------------------------------------------
    costs: dict[str, Any] = {}
    pnl = getattr(result, "pnl", None)
    if pnl is not None and not getattr(pnl, "empty", True):
        for name in ("intraday", "gap", "spread", "slippage", "financing", "fx"):
            if name in pnl.columns:
                costs[name] = float(pd.to_numeric(pnl[name], errors="coerce").sum())
        charged = sum(
            abs(costs.get(k, 0.0)) for k in ("spread", "slippage", "financing", "fx")
        )
        costs["total_charged"] = charged
        if charged > 0:
            costs["shares"] = {
                k: abs(costs.get(k, 0.0)) / charged * 100
                for k in ("spread", "slippage", "financing", "fx")
            }

    # -- sides -------------------------------------------------------------
    sides: dict[str, Any] = {}
    if len(trips):
        for side in ("long", "short"):
            part = trips[trips.side == side]
            sides[side] = {
                "trips": int(len(part)),
                "share": float(len(part) / len(trips) * 100),
                "win_rate": float(part.won.mean() * 100) if len(part) else None,
                "mean_pct": float(part.pct.mean()) if len(part) else None,
                "total_pct": float(part.pct.sum()) if len(part) else None,
            }
        if sides.get("short", {}).get("trips", 0) == 0:
            notes.append(
                "no short trips: a long-only result on this universe is beta"
            )

    # -- slippage curve ----------------------------------------------------
    curve: list[tuple[float, float | None]] = []
    for bps in slippage_steps:
        tuned = dataclasses.replace(execution, slippage_fraction=bps / 10_000.0)
        stats = _curve_stats(
            run_backtest(pipeline.signals, pipeline.analysis, tuned).equity_curve
        )
        curve.append((float(bps), stats.get("cagr")))
    breakeven = _interpolate_breakeven(curve, benchmark.get("cagr"))

    # -- shuffle control ---------------------------------------------------
    shuffle: dict[str, Any] = {}
    if include_shuffle and len(trips):
        rng = np.random.default_rng(seed)
        real = float(trips.pct.sum())
        values = trips.pct.to_numpy()
        draws = np.array([
            float((values * rng.choice([1.0, -1.0], size=len(values))).sum())
            for _ in range(SHUFFLE_DRAWS)
        ])
        spread = float(draws.std(ddof=1))
        shuffle = {
            "real_pct": real,
            "random_mean_pct": float(draws.mean()),
            "random_sd_pct": spread,
            "draws": SHUFFLE_DRAWS,
            "beat_real": int((draws >= real).sum()),
            "z": (real - float(draws.mean())) / spread if spread > 0 else None,
        }
        if shuffle["beat_real"] > SHUFFLE_DRAWS * 0.05:
            notes.append(
                "the shuffle control is not decisive: random directions beat "
                "the real signal too often to claim the direction carries "
                "information"
            )

    # -- walk-forward ------------------------------------------------------
    forward: dict[str, Any] = {}
    if include_walkforward:
        try:
            forward = _walkforward(config, settings, execution, repo)
        except Exception as error:  # noqa: BLE001 - a long run must not lose the rest
            logger.warning("Walk-forward failed: %s", error)
            notes.append(f"walk-forward could not be run: {error}")

    if headline.get("cagr") is not None and benchmark.get("cagr") is not None:
        if headline["cagr"] <= benchmark["cagr"]:
            notes.append(
                "the strategy did not beat buy-and-hold of the same universe"
            )

    return Evaluation(
        window=window, headline=headline, benchmark=benchmark, costs=costs,
        sides=sides, slippage=tuple(curve), breakeven_bps=breakeven,
        shuffle=shuffle, walkforward=forward, notes=tuple(notes),
    )


def _walkforward(config, settings, execution, repo) -> dict[str, Any]:
    """Rolling windows, scored only on the stretch each never saw."""
    import numpy as np
    import pandas as pd

    from cfdinvest.analytics.walkforward import rolling_date_windows, run_walkforward

    close = repo.load_market(
        tickers=list(config.market.tickers), interval=config.market.interval
    )
    stamps = pd.DatetimeIndex(close.reset_index()["timestamp"])
    start, end = stamps.min(), stamps.max()

    windows = rolling_date_windows(start, end, train="180D", test="30D")
    if not windows:
        return {"windows": 0, "note": "the history is too short for a walk-forward"}

    results = run_walkforward(settings, windows, repository=repo)
    scored = [
        float(r.test_metrics["total_return"]) * 100
        for r in results
        if not r.error and np.isfinite(r.test_metrics.get("total_return", np.nan))
    ]
    if not scored:
        return {"windows": 0, "note": "no window produced a score"}

    values = np.array(scored)
    stitched = float(np.prod(1 + values / 100) - 1) * 100
    years = len(values) * 30 / 365.25
    return {
        "windows": len(values),
        "mean_pct_per_window": float(values.mean()),
        "positive": int((values > 0).sum()),
        "stitched_pct": stitched,
        "cagr": ((1 + stitched / 100) ** (1 / years) - 1) * 100 if years > 0 else None,
    }


def _pct(value, places: int = 1) -> str:
    return "—" if value is None else f"{value:,.{places}f}%"


def render(evaluation: Evaluation) -> str:
    """The evaluation as text, in the order the questions matter."""
    out: list[str] = []
    w, h, b = evaluation.window, evaluation.headline, evaluation.benchmark

    out.append("=" * 68)
    out.append("EVALUATION")
    out.append("=" * 68)
    if w:
        out.append(
            f"  {w.get('instruments')} instruments on {w.get('interval')} bars, "
            f"{w.get('start', '')[:10]} to {w.get('end', '')[:10]}"
        )
        out.append(
            f"  entry {w.get('entry_confidence')}  "
            f"slippage {w.get('slippage_bps')}bp  "
            f"max leverage {w.get('max_leverage')}x"
        )

    out.append("")
    out.append("HEADLINE")
    out.append(f"  CAGR             {_pct(h.get('cagr'))}")
    out.append(f"  max drawdown     {_pct(h.get('max_drawdown'))}")
    sharpe = h.get("sharpe")
    out.append(f"  Sharpe           {'—' if sharpe is None else f'{sharpe:.2f}'}")
    out.append(f"  round trips      {h.get('trips')} "
               f"({h.get('trips_per_year'):,.0f}/yr)" if h.get("trips_per_year")
               else f"  round trips      {h.get('trips')}")
    out.append(f"  win rate         {_pct(h.get('win_rate'))}")

    out.append("")
    out.append("AGAINST BUY AND HOLD (same instruments, same window)")
    out.append(f"  benchmark CAGR   {_pct(b.get('cagr'))}")
    out.append(f"  benchmark maxDD  {_pct(b.get('max_drawdown'))}")
    if h.get("cagr") is not None and b.get("cagr") is not None:
        out.append(f"  difference       {_pct(h['cagr'] - b['cagr'])}")

    if evaluation.sides:
        out.append("")
        out.append("BY SIDE (shorts working is the hard thing to fake)")
        for side in ("long", "short"):
            part = evaluation.sides.get(side) or {}
            out.append(
                f"  {side:6s} {part.get('trips', 0):5d} trips "
                f"({part.get('share', 0):.0f}%)  win {_pct(part.get('win_rate'))}  "
                f"total {_pct(part.get('total_pct'))}"
            )

    if evaluation.costs.get("shares"):
        out.append("")
        out.append("COSTS")
        for name, share in sorted(
            evaluation.costs["shares"].items(), key=lambda kv: -kv[1]
        ):
            amount = abs(evaluation.costs.get(name, 0.0))
            out.append(f"  {name:12s} {amount:12,.0f}  {share:5.1f}% of charged")

    if evaluation.slippage:
        out.append("")
        out.append("SLIPPAGE SENSITIVITY (the number that decides reachability)")
        for bps, cagr in evaluation.slippage:
            out.append(f"  {bps:5.1f} bp      {_pct(cagr)}")
        out.append(
            f"  break-even against buy and hold: "
            + ("—" if evaluation.breakeven_bps is None
               else f"~{evaluation.breakeven_bps:.1f} bp")
        )

    if evaluation.shuffle:
        s = evaluation.shuffle
        out.append("")
        out.append("SHUFFLE CONTROL (same trades, random directions)")
        out.append(f"  real signal      {_pct(s.get('real_pct'))}")
        out.append(
            f"  random           {_pct(s.get('random_mean_pct'))} "
            f"(sd {s.get('random_sd_pct', 0):.1f})"
        )
        out.append(
            f"  draws beating it {s.get('beat_real')} of {s.get('draws')}"
            + (f"   z = {s['z']:+.2f}" if s.get("z") is not None else "")
        )

    if evaluation.walkforward:
        f = evaluation.walkforward
        out.append("")
        out.append("WALK-FORWARD (scored only on stretches never seen)")
        if f.get("windows"):
            out.append(
                f"  {f['windows']} windows, {f.get('positive')} positive, "
                f"mean {_pct(f.get('mean_pct_per_window'), 2)} per window"
            )
            out.append(f"  stitched CAGR    {_pct(f.get('cagr'))}")
        else:
            out.append(f"  {f.get('note', 'not run')}")

    if evaluation.notes:
        out.append("")
        out.append("NOTES")
        for note in evaluation.notes:
            out.append(f"  - {note}")

    return "\n".join(out)
