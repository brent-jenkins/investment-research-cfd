"""A universe sweep that can be re-run, and that checks itself first.

WHAT WAS WRONG
--------------
A backtest built straight from ``schedule.app._backtest_config`` returns
-84.2% at -99.9% on 38,877 trades, where the 2026-09-08 battery recorded
+191.0% at 6.6% for the same cap and target. Three explanations were tested
and eliminated: the session flatten (worth 0.6pp), ``spread_panel`` being
None (the engine falls through to the frame's own ``spread_bp``), and
``entry_gate`` being None (the runner composes the breakout mask at run
time).

What remained is that the production config describes ONE DAY'S DECISION and
a sweep describes three and a half years. In production the traded list is
today's recorded screen selection and the run decides about the newest bar.
Over history the screen RE-SELECTS, and a backtest that ignores that lets
any name in the list open on any bar for years.

``backtest.runner.compose_entry_gates`` says so in as many words -- "a
universe screen is another" contributor to ``entry_gate`` -- and records a
bug where a caller-supplied gate was silently discarded, which made a
universe sweep "return an identical number at every size". That is the shape
of what was lost.

WHAT THIS DOES
--------------
Rebuilds the screen's membership over history and passes it as
``entry_gate``, so a name may open only while the screen actually held it.

* The screen is recomputed at each rebalance date from data available on
  that date -- ``rank_candidates`` is pure and truncates at ``as_of`` itself,
  so nothing looks forward.
* Daily bars, regardless of what the engine trades, because that is what
  ``select_universe`` does and liquidity is a property of an instrument
  rather than of a bar size.
* Prices and spreads are loaded ONCE. ``select_universe`` reloads the lake
  on every call, which measured 4.5-5.4s -- about 75 minutes for a daily
  rebalance over the span, per arm. Hoisted out, the loop is arithmetic.
* The session flatten is applied, because this configuration day trades and
  the flatten is not a config field: ``schedule.app.for_live_intent``
  expresses it as ``overnight_profit_take = FLATTEN_AT_SESSION_END``.

``target`` and ``cap`` are swept DIRECTLY rather than resolved through
``universe.traded.resolve_target``. That function applies a viability
ceiling computed from today's account balance, and a sweep over 2023 has no
business reading it.

WHAT IT REFUSES TO DO
---------------------
Report an arm before its baseline reproduces a recorded figure. CLAUDE.md
already requires that of a sweep -- "the corrected harness asserts its
baseline reproduces a recorded figure before any arm is read" -- and the
requirement kept being satisfied by hand, in scripts that were then thrown
away. Here it is :func:`assert_reproduces`, and it raises.
"""
from __future__ import annotations

import dataclasses
import logging
from typing import Any, Iterable, Mapping, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "RECORDED",
    "ReproductionError",
    "arm_metrics",
    "assert_reproduces",
    "membership_mask",
    "research_backtest",
    "run_arm",
    "screen_selections",
]


#: Results this harness must be able to reproduce before any arm is read.
#:
#: Keyed by (max_per_sector, universe target). Values are the CAGR and the
#: maximum drawdown as recorded in CLAUDE.md on 2026-09-08, which is the one
#: battery taken on the corrected engine over books the screen actually
#: produces.
#:
#: A figure here is a CLAIM about what this code can regenerate. If the
#: harness cannot hit one, either the harness is wrong or the recorded
#: number is -- and both are worth stopping for, which is why
#: `assert_reproduces` raises rather than warns.
#:
#: **RETIRED AS THE ANCHOR, 2026-09-26.** The battery came from a script that
#: was never kept, over a lake since re-ingested, so no correct code need
#: reproduce it -- and a miss could not say whether the harness or the figure
#: was wrong. The anchor is now the account: ``research.replay`` checks that
#: the harness makes the live engine's own entry decisions, bar for bar.
#: These figures stay, and ``assert_reproduces`` still refuses them, so that
#: nobody reads an arm against them in the belief that they were ever met.
RECORDED: dict[tuple[int, int], dict[str, float]] = {
    (5, 30): {"cagr": 1.160, "maxdd": -0.072},
    (8, 50): {"cagr": 1.910, "maxdd": -0.066},
    (12, 70): {"cagr": 2.703, "maxdd": -0.077},
}

#: The engine's opening delay in a research run, at least.
#:
#: The live dealer never fills on a session's opening bar. It decides on the
#: newest COMPLETED bar, and before the first bar of a session completes that
#: is the previous evening's last -- a book flattened by construction, so it
#: opens nothing. The research engine decides on bar N and fills at N+1, so
#: without this it filled the morning's first bar on the previous evening's
#: signal: a trade the account cannot take. The config's own delay is 0,
#: recorded on 2026-09-10 as "redundant", which is true of the live path and
#: false of a backtest.
#:
#: Any delay from 1 to 60 minutes refuses exactly the opening bar on hourly
#: data, because the engine measures elapsed time from the day's first bar.
#: Found 2026-09-26 replaying the live period; see ``research.replay``.
FIRST_BAR_DELAY_MINUTES: int = 1


def _noise_floor_pp() -> float:
    """The project's noise floor, from its single owner.

    Read rather than restated. This module first carried its own literal,
    which made it a second copy of exactly the threshold `risk.capacity`
    exists to own -- and "one owner per threshold" is a house rule because
    two copies drift and the day they drift the banner says fine while the
    email says critical.
    """
    from cfdinvest.risk.capacity import NOISE_FLOOR_PP

    return float(NOISE_FLOOR_PP)


#: How far an arm may sit from its recorded figure and still count as
#: reproduced, in percentage points of CAGR.
#:
#: A harness that reproduces to within the floor is measuring the same
#: system; one that does not is measuring a different one, whatever it is
#: called.
REPRODUCTION_TOLERANCE_PP: float = _noise_floor_pp()


class ReproductionError(RuntimeError):
    """The harness did not reproduce a recorded figure.

    Raised rather than logged. A sweep whose baseline is wrong produces arms
    that are differences between two wrong numbers, and this project has
    twice shipped findings from exactly that.
    """


def _screen_inputs(repository, candidates: Sequence[str], app_config, account):
    """Daily frames and spreads for every candidate, loaded once.

    Mirrors what ``select_universe`` does per call, hoisted out of the loop.
    Returns the mapping ``rank_candidates`` expects.
    """
    from cfdinvest.universe import screen as screen_module

    market = repository.load_market(tickers=list(candidates), interval="1d")
    if market is None or getattr(market, "empty", True):
        raise ReproductionError("the lake returned no daily bars to screen on")

    working = market.reset_index()
    ticker_column = "ticker" if "ticker" in working.columns else working.columns[1]
    stamp_column = (
        "timestamp" if "timestamp" in working.columns else working.columns[0]
    )

    frames: dict[str, Any] = {}
    for ticker, group in working.groupby(ticker_column):
        frames[str(ticker)] = group.set_index(stamp_column).sort_index()
    for ticker in candidates:
        frames.setdefault(str(ticker), None)

    spreads = screen_module._spread_series(  # noqa: SLF001 - the same source
        repository, candidates, app_config, account
    )
    return frames, spreads


def screen_selections(
    repository,
    app_config,
    dates: Iterable,
    *,
    cap: int | None,
    target: int,
    account: str | None = None,
) -> dict[Any, list[str]]:
    """What the screen would have selected on each date.

    Pure with respect to the future: ``rank_candidates`` truncates its own
    input at ``as_of``, so a selection for 2023 cannot see 2024.
    """
    import pandas as pd

    from cfdinvest.universe import screen as screen_module

    candidates = list(getattr(app_config.universe, "candidates", None) or [])
    if not candidates:
        raise ReproductionError("no candidate pool: the screen has nothing to rank")

    sectors = dict(getattr(app_config.universe, "sectors", {}) or {})
    frames, spreads = _screen_inputs(repository, candidates, app_config, account)

    settings = screen_module.ScreenConfig(target=int(target))
    if cap is not None:
        settings = dataclasses.replace(settings, max_per_sector=int(cap) or None)

    selections: dict[Any, list[str]] = {}
    for date in dates:
        as_of = pd.Timestamp(date)
        if as_of.tzinfo is None:
            as_of = as_of.tz_localize("UTC")
        result = screen_module.rank_candidates(
            frames, as_of=as_of, config=settings,
            sectors=sectors or None, spreads=spreads,
        )
        selections[as_of] = list(getattr(result, "selected", None) or [])

    return selections


def membership_mask(selections: Mapping[Any, Sequence[str]], index, columns):
    """``bars x instrument`` -- True where the screen held the name.

    A bar takes the most recent selection at or before it, which is what the
    daemon does: it trades the screen's latest recorded selection, not one
    computed from the future.

    Bars before the first selection are False. That is deliberate and it is
    the conservative reading -- "no screen had run yet" is not "everything
    was permitted", and permitting them would open the whole candidate pool
    across the warm-up.
    """
    import pandas as pd

    stamps = pd.DatetimeIndex(index)
    frame = pd.DataFrame(False, index=stamps, columns=list(columns))
    if not selections:
        return frame

    ordered = sorted(selections)
    as_of = pd.DatetimeIndex(ordered)
    # For each bar, the position of the newest selection at or before it.
    slots = as_of.searchsorted(stamps, side="right") - 1

    known = {str(c) for c in frame.columns}
    for slot in sorted(set(int(s) for s in slots if s >= 0)):
        chosen = [str(n) for n in selections[ordered[slot]] if str(n) in known]
        if not chosen:
            continue
        rows = stamps[slots == slot]
        if len(rows):
            frame.loc[rows, chosen] = True

    return frame


def research_backtest(app_config, *, tickers: Sequence[str], entry_gate=None,
                      start=None, end=None, **overrides):
    """The shipped configuration, expressed for a multi-year run.

    Two differences from ``_backtest_config``, both because that describes
    one day's decision rather than a span:

    * the session flatten is applied -- this configuration day trades, and
      the flatten lives in ``execution.overnight_profit_take`` rather than in
      a field of its own;
    * the screen's membership is passed as ``entry_gate``, which the runner
      composes with the breakout mask rather than replacing it;
    * the session's opening bar is refused, because the live dealer never
      fills on it -- see ``FIRST_BAR_DELAY_MINUTES``;
    * the gross ceiling SELECTS rather than scales
      (``leverage_cap_selects``), which is what the dealer does at the cap.

    Explicit overrides win over all of these.

    ``execution_lag`` is left where the shipped config puts it. A research
    run must not borrow ``for_live_intent``'s lag of 0: that fills on the bar
    the signal was computed from, and the engine says so itself.
    """
    from cfdinvest.schedule import app as app_module

    class _Args:
        interval = None
        start = end = universe = account = tickers = None

    base = app_module._backtest_config(_Args(), app_config)  # noqa: SLF001
    fields = {
        "overnight_profit_take": app_module.FLATTEN_AT_SESSION_END,
        "entry_gate": entry_gate,
        "open_delay_minutes": max(
            int(base.execution.open_delay_minutes or 0), FIRST_BAR_DELAY_MINUTES
        ),
        # At the gross ceiling the dealer refuses the weakest new opens and
        # leaves held positions alone; scaling instead rescaled the whole
        # book on every bar the cap bound -- 61 resizes on 2026-09-22 alone,
        # the book pinned at 3.5x all session, where the account resizes
        # nothing. Found replaying the live period; see research.replay.
        "leverage_cap_selects": True,
    }
    # An explicit override wins over every default above -- and a name the
    # engine does not have is refused rather than dropped. Dropping it made
    # an arm silently identical to its baseline on 2026-09-26: an override
    # spelled `decayed_multiplier` for `decayed_position_multiplier` returned
    # the shipped book to the trade, which reads as a finding that the rule
    # does nothing.
    known = {f.name for f in dataclasses.fields(base.execution)}
    unknown = sorted(set(overrides) - known)
    if unknown:
        raise TypeError(
            f"research_backtest got override(s) the engine has no field for: "
            f"{unknown}. An unknown name would be ignored and the arm would be "
            "the baseline under another label."
        )
    fields.update(overrides)
    execution = dataclasses.replace(base.execution, **fields)
    return dataclasses.replace(
        base,
        tickers=list(tickers),
        start=start,
        end=end,
        execution=execution,
    )


def arm_metrics(result) -> dict | None:
    """CAGR, maximum drawdown and trade count from a run."""
    import pandas as pd

    execution = getattr(result, "execution", None)
    curve = getattr(execution, "equity_curve", None)
    if curve is None or getattr(curve, "empty", True):
        return None

    series = (
        curve["equity"]
        if hasattr(curve, "columns") and "equity" in curve.columns
        else curve
    )
    series = pd.Series(series).dropna()
    if len(series) < 2 or float(series.iloc[0]) <= 0:
        return None

    stamps = pd.DatetimeIndex(series.index)
    years = max((stamps[-1] - stamps[0]).days / 365.25, 1e-9)
    growth = float(series.iloc[-1]) / float(series.iloc[0])
    trades = getattr(execution, "trades", None)

    metrics = {
        "cagr": (growth ** (1.0 / years) - 1.0) if growth > 0 else float("nan"),
        "maxdd": float((series / series.cummax() - 1.0).min()),
        "trades": 0 if trades is None or getattr(trades, "empty", True) else len(trades),
        "bars": len(series),
        "start": stamps[0],
        "end": stamps[-1],
    }

    # The decomposition, because CAGR alone cannot say WHICH system was
    # measured. The 2026-09-08 battery reports mean leverage 0.89 and a 6.6%
    # drawdown for cap 8 / t50; a reconstruction drawing down 91.8% is not
    # that configuration a few points out, it is a different book. Leverage
    # and cost share identify it where a return figure cannot.
    exposure = getattr(execution, "exposure", None)
    if exposure is not None and not getattr(exposure, "empty", True):
        try:
            gross = pd.Series(exposure).dropna().abs()
            metrics["mean_leverage"] = float(gross.mean())
            metrics["p90_leverage"] = float(gross.quantile(0.90))
        except Exception:  # noqa: BLE001 - a missing reading is not an outage
            pass

    pnl = getattr(execution, "pnl", None)
    if pnl is not None and not getattr(pnl, "empty", True):
        try:
            frame = pd.DataFrame(pnl)
            costs = [c for c in ("spread", "slippage", "financing", "fx")
                     if c in frame.columns]
            if costs:
                cost = float(frame[costs].to_numpy().sum())
                metrics["cost"] = cost
                for column in ("intraday", "gap"):
                    if column in frame.columns:
                        metrics[column] = float(frame[column].sum())
                gross_pnl = sum(
                    metrics.get(c, 0.0) for c in ("intraday", "gap")
                )
                metrics["gross"] = gross_pnl
                if gross_pnl:
                    metrics["cost_share"] = abs(cost) / abs(gross_pnl)
        except Exception:  # noqa: BLE001
            pass

    return metrics


def assert_reproduces(arm: tuple[int, int], metrics: Mapping[str, float] | None,
                      *, tolerance_pp: float = REPRODUCTION_TOLERANCE_PP) -> None:
    """Raise unless this arm lands on its recorded figure.

    The check is on CAGR, in percentage points, against the project's own
    9.3pp noise floor. Drawdown is reported but not asserted: it is a single
    realisation and this file records that comparing two of them is not a
    measurement until the distribution is known.
    """
    recorded = RECORDED.get(tuple(arm))
    if recorded is None:
        raise ReproductionError(
            f"no recorded figure for cap/target {arm}; add one to RECORDED "
            "or sweep an arm that has one"
        )
    if not metrics:
        raise ReproductionError(f"arm {arm} produced no equity curve")

    got = float(metrics["cagr"]) * 100.0
    want = float(recorded["cagr"]) * 100.0
    gap = abs(got - want)
    if gap > tolerance_pp:
        raise ReproductionError(
            f"arm {arm} returned {got:.1f}% against a recorded {want:.1f}% "
            f"-- {gap:.1f}pp apart, past the {tolerance_pp}pp noise floor. "
            f"({metrics.get('trades')} trades.) Either this harness is "
            "measuring a different system or the recorded figure is wrong. "
            "No arm may be read until that is settled."
        )
    logger.info(
        "arm %s reproduces: %.1f%% against a recorded %.1f%% (%.1fpp apart)",
        arm, got, want, gap,
    )


def run_arm(repository, app_config, *, cap: int | None, target: int,
            rebalance: str = "W", start=None, end=None,
            account: str | None = None, **overrides) -> dict:
    """Screen over history, gate on it, run the engine, return the metrics.

    ``rebalance`` is a pandas frequency. Daily is what production does and
    what this defaults to elsewhere; weekly is the affordable default here,
    on this file's own measurement that cadence costs nothing in return
    (daily 74.4% against monthly 74.9%) while the screen costs real time.
    """
    import pandas as pd

    from cfdinvest.backtest.runner import run_pipeline

    candidates = list(getattr(app_config.universe, "candidates", None) or [])
    probe = research_backtest(app_config, tickers=candidates, start=start, end=end)
    first = run_pipeline(repository, probe)
    closes = getattr(getattr(first, "analysis", None), "close", None)
    if closes is None or getattr(closes, "empty", True):
        raise ReproductionError("no price panel to build a membership mask on")

    index = pd.DatetimeIndex(closes.index)
    dates = pd.date_range(index[0], index[-1], freq=rebalance, tz=index.tz)
    selections = screen_selections(
        repository, app_config, dates, cap=cap, target=target, account=account
    )
    held = sorted({name for names in selections.values() for name in names})
    logger.info(
        "screen selected %d distinct names across %d rebalances",
        len(held), len(selections),
    )

    mask = membership_mask(selections, index, closes.columns)
    config = research_backtest(
        app_config, tickers=list(closes.columns), entry_gate=mask,
        start=start, end=end, **overrides,
    )
    metrics = arm_metrics(run_pipeline(repository, config))
    if metrics is not None:
        metrics["names"] = len(held)
        metrics["rebalances"] = len(selections)
    return metrics or {}
