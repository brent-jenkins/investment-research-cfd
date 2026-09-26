"""Replay the live period through the engine and check it made the same calls.

WHY THE ANCHOR MOVED
--------------------
``harness.RECORDED`` anchored the harness to the 2026-09-08 battery, which
came from a script that was never kept, run over a lake that has since been
re-ingested. No correct code need reproduce it, so failing to hit it could
not say whether the harness or the figure was wrong -- and for two weeks it
blocked every parameter question in CLAUDE.md.

The account is a better anchor, because it cannot have been measured wrong.
Every dealing pass writes a plan row per intended position, and a row whose
``entered_bars_ago`` is 0 -- or any approved open -- is the LIVE ENGINE
deciding to enter on the newest completed bar, before the last look, the
caps or the broker touch it. A harness that makes those same decisions, bar
for bar, is demonstrably running the system that deals.

WHAT REPRODUCING IT TOOK, measured 2026-09-26
---------------------------------------------
One panel over every name ever selected reproduced 64% of live entries.
Three causes, each found by pulling a mismatch apart:

* **The volatility regime is market-level.** It aggregates across whatever
  names are in the panel, so a name's signal depends on the rest of the
  list: adding GE flipped QCOM from long 0.803 to flat on 2026-09-25 14:00.
  So each day must run on the universe the daemon traded THAT day, which is
  what :func:`selection_segments` groups by.
* **The research config lets the engine fill on the session's opening bar**
  from the previous evening's signal. The live path never does -- it waits
  for a bar of the new session to complete. ``harness.research_backtest``
  now refuses the opening bar.
* **The engine's closing window measured to the last bar's stamp**, one bar
  short of the close, and refused the 18:00 entry live takes. Fixed in the
  engine; see ``tests/test_the_closing_window_counts_the_last_bar.py``.

After all three, over 2026-09-11 to 2026-09-25: every one of 96 harness
entries was a position the live engine intended on that exact bar, and of
88 live entries 70 matched exactly, 17 were chases (the account opened late
on an entry the harness has at its true bar), and one was unexplained.

Deliberately NOT modelled: the last look, the per-pass opening budget, the
margin guard and the broker. They act AFTER the engine's decision, and this
compares the decision.
"""
from __future__ import annotations

import dataclasses
import logging
from typing import Any, Iterable, Mapping, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "AccountReplay",
    "DEALING_CUT_HOUR",
    "DECISION_OVERRIDES",
    "MAX_UNEXPLAINED_SHARE",
    "ReplayError",
    "agreement",
    "assert_reproduces_account",
    "entries_from_positions",
    "history_segments",
    "live_engine_entries",
    "replay_account",
    "run_segments",
    "segment_config",
    "selection_in_force",
    "selection_segments",
    "summarise",
]

#: The hour (UTC) by which a day's selection must have been recorded to be
#: the one the daemon traded that day. The screen records at about 03:13 UTC
#: and the first dealing pass acts at about 14:05, so anything recorded
#: after 14:00 belongs to a later day. Using a LATER selection would let the
#: replay trade names chosen with hindsight.
DEALING_CUT_HOUR: int = 14

#: How much of the live side may go unexplained before the replay is refused.
#:
#: Unexplained means the live engine entered and the harness neither entered
#: then nor earlier the same day. One in eighty-eight was the measured
#: residue on 2026-09-26; five per cent leaves room for that and not for a
#: systematic divergence.
MAX_UNEXPLAINED_SHARE: float = 0.05

#: Settings under which the replay runs, where they differ from the harness.
#:
#: The harness models the ACCOUNT, and at the gross ceiling the dealer
#: selects -- refuses the weakest new opens and leaves held positions alone.
#: The replay checks the live ENGINE, and the live intent run scales, because
#: ``leverage_cap_selects`` is not wired into the live config. Replaying with
#: selection dropped 33 of 88 live entries on 2026-09-26, each one a decision
#: the live engine did make; replaying with scaling reproduced all but one.
DECISION_OVERRIDES: dict = {"leverage_cap_selects": False}

#: Refusal reasons that are the CLOCK rather than a view. Both windows refuse
#: an open the engine wanted, and the engine models them itself, so neither
#: side "wanted" such an entry in a comparable sense.
_CLOCK_REASONS = ("within the opening window", "too near the session close to open")


class ReplayError(RuntimeError):
    """The harness did not make the decisions the live engine made."""


@dataclasses.dataclass(frozen=True)
class AccountReplay:
    """What the comparison found, as counts and the entries behind them."""

    live: frozenset
    harness: frozenset
    identical: frozenset
    chases: frozenset
    unexplained: frozenset
    not_intended: frozenset

    @property
    def unexplained_share(self) -> float:
        return len(self.unexplained) / len(self.live) if self.live else 0.0

    def summary(self) -> str:
        return (
            f"live {len(self.live)}, harness {len(self.harness)}, identical "
            f"{len(self.identical)}, chases {len(self.chases)}, unexplained "
            f"{len(self.unexplained)} ({self.unexplained_share:.1%}), harness "
            f"entries the live engine never intended {len(self.not_intended)}"
        )


def _utc(stamp):
    import pandas as pd

    stamp = pd.Timestamp(stamp)
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def _side(direction) -> int | None:
    return {"BUY": 1, "LONG": 1, "SELL": -1, "SHORT": -1}.get(str(direction).upper())


def live_engine_entries(plan_rows: Iterable[Mapping[str, Any]]):
    """The live engine's entry decisions and every position it intended.

    Returns ``(entries, intents)``, each a set of ``(decision bar, ticker,
    side)``. The decision bar is the hour before the pass, because the pass
    acts on the newest COMPLETED bar.

    An entry is a fresh refusal (``entered_bars_ago == 0``) or ANY approved
    open: approvals carried no age before 2026-09-23, and treating their
    absence as "not an entry" would drop every open before that date. A
    refusal on the clock is excluded, for the reason in ``_CLOCK_REASONS``.
    An intent is any plan row carrying a side, whatever happened to it.
    """
    import math

    import pandas as pd

    entries, intents = set(), set()
    for row in plan_rows:
        if row.get("kind", "plan") != "plan":
            continue
        side = _side(row.get("direction"))
        if side is None or not row.get("at") or not row.get("ticker"):
            continue
        bar = _utc(row["at"]).floor("h") - pd.Timedelta(hours=1)
        key = (bar, str(row["ticker"]), side)
        intents.add(key)

        age = row.get("entered_bars_ago")
        fresh = age is not None and not (isinstance(age, float) and math.isnan(age)) \
            and float(age) == 0.0
        clock = str(row.get("reason") or "") in _CLOCK_REASONS
        if (row.get("action") == "open" or fresh) and not clock:
            entries.add(key)
    return entries, intents


def agreement(live: set, harness: set, intents: set) -> AccountReplay:
    """Classify every entry on both sides.

    * identical -- same decision bar, name and side on both;
    * chase -- live only, and the harness entered the same name and side
      EARLIER the same day: the account opened late on an entry the engine
      made on time, which is the last look's refusals echoing, not a
      disagreement about the decision;
    * unexplained -- live only, and not a chase;
    * not intended -- harness only, and the live engine never intended that
      position on that bar. This is the one that must be zero: the harness
      deciding something live would not have.
    """
    identical = live & harness
    chases, unexplained = set(), set()
    for bar, ticker, side in live - harness:
        earlier = any(
            t == ticker and s == side and b < bar and b.normalize() == bar.normalize()
            for b, t, s in harness
        )
        (chases if earlier else unexplained).add((bar, ticker, side))
    return AccountReplay(
        live=frozenset(live), harness=frozenset(harness),
        identical=frozenset(identical), chases=frozenset(chases),
        unexplained=frozenset(unexplained),
        not_intended=frozenset(harness - intents),
    )


def assert_reproduces_account(report: AccountReplay, *,
                              max_unexplained_share: float = MAX_UNEXPLAINED_SHARE) -> None:
    """Raise unless the harness made the live engine's decisions.

    Two conditions, and the first is absolute. A harness entry the live
    engine never intended means the harness is running a different rule --
    there is no tolerance for that. Live entries the harness cannot account
    for are allowed up to ``max_unexplained_share``.

    An empty live side raises too: agreement with nothing is not agreement,
    and a window with no sessions in it would otherwise pass.
    """
    if not report.live:
        raise ReplayError("no live entries in the window: nothing was compared")
    if report.not_intended:
        sample = sorted(report.not_intended)[:5]
        raise ReplayError(
            f"the harness entered {len(report.not_intended)} position(s) the live "
            f"engine never intended, e.g. {sample}. It is running a different rule. "
            + report.summary()
        )
    if report.unexplained_share > max_unexplained_share:
        raise ReplayError(
            f"{report.unexplained_share:.1%} of live entries are unexplained, past "
            f"{max_unexplained_share:.0%}. " + report.summary()
        )


def selection_in_force(selections: Sequence[tuple], day) -> tuple | None:
    """The universe the daemon traded on ``day``.

    ``selections`` is ``[(recorded at, names), ...]``. The newest recorded
    before ``DEALING_CUT_HOUR`` on that day, never a later one.
    """
    import pandas as pd

    cut = _utc(day).normalize() + pd.Timedelta(hours=DEALING_CUT_HOUR)
    chosen = None
    for stamp, names in sorted(selections, key=lambda item: _utc(item[0])):
        if _utc(stamp) <= cut and names:
            chosen = tuple(names)
    return chosen


def selection_segments(selections: Sequence[tuple], days: Iterable):
    """Consecutive days grouped by the universe in force.

    Consecutive, not merely equal: a list that returns after a change is a
    new segment, because the regime between them was computed on another
    list. Days with no selection in force are dropped rather than guessed.
    """
    segments: list[tuple[tuple, list]] = []
    for day in days:
        universe = selection_in_force(selections, day)
        if universe is None:
            continue
        if segments and segments[-1][0] == universe:
            segments[-1][1].append(day)
        else:
            segments.append((universe, [day]))
    return segments


def segment_config(app_config, universe: Sequence[str], days: Sequence, *,
                   window_months: int = 6, **overrides):
    """The live-faithful configuration for one segment.

    * the universe the daemon traded on those days;
    * the live window of ``window_months`` before the first day, and the live
      regime setting, ``macro_on_own_calendar`` -- which the live window
      requires, see ``schedule.app.for_live_intent``;
    * the research lag and the harness's opening-bar rule;
    * an end FOUR DAYS past the last day. The engine treats a frame's final
      bar as still open, which exempts it from the closing window; ending on
      a segment day made every segment's last 19:00 bar tradable, and three
      entries on 2026-09-26 were that artefact rather than decisions.
    """
    import pandas as pd

    from cfdinvest.research import harness

    first, last = _utc(days[0]).normalize(), _utc(days[-1]).normalize()
    config = harness.research_backtest(
        app_config, tickers=list(universe),
        start=first - pd.DateOffset(months=window_months),
        end=last + pd.Timedelta(days=4),
        **overrides,
    )
    return dataclasses.replace(
        config, regime=dataclasses.replace(config.regime, macro_on_own_calendar=True)
    )


def entries_from_positions(positions, days: Iterable) -> set:
    """Engine entries on ``days``, as ``(decision bar, ticker, side)``.

    An entry is a bar whose position is non-zero with a different sign from
    the bar before. At the research lag of 1 the fill bar is the bar AFTER
    the decision, so the decision bar is the previous row of the index.
    """
    import numpy as np

    keep = {_utc(d).normalize() for d in days}
    held = positions.fillna(0.0)
    sign = np.sign(held.to_numpy())
    before = np.vstack([np.zeros((1, sign.shape[1])), sign[:-1]])
    found = set()
    for row, col in zip(*np.where((sign != 0) & (sign != before))):
        if row == 0:
            continue
        fill = held.index[row]
        if _utc(fill).normalize() in keep:
            found.add((_utc(held.index[row - 1]), str(held.columns[col]), int(sign[row, col])))
    return found


def run_segments(repository, app_config, segments, **overrides):
    """Run each segment on its own universe; return entries and daily returns.

    The book flattens every evening, so a day's return does not depend on
    the day before beyond the equity it is sized on, and daily returns from
    separate runs chain into one curve.
    """
    import pandas as pd

    from cfdinvest.backtest.runner import run_pipeline

    entries, daily = set(), []
    for universe, days in segments:
        result = run_pipeline(
            repository, segment_config(app_config, universe, days, **overrides)
        )
        execution = result.execution
        entries |= entries_from_positions(execution.positions, days)
        equity = pd.Series(execution.equity_curve).dropna()
        closes = equity.groupby(pd.DatetimeIndex(equity.index).normalize()).last()
        returns = closes.pct_change()
        wanted = {_utc(d).normalize() for d in days}
        daily.append(returns[[d in wanted for d in returns.index]])
    returns = pd.concat(daily).sort_index() if daily else pd.Series(dtype="float64")
    return entries, returns


def history_segments(repository, app_config, start, end, *, rebalance: str = "W-SUN",
                     cap=None, target=None, account=None):
    """The segments a live-faithful run over history needs.

    The screen is re-run from the data at each rebalance date --
    ``rank_candidates`` truncates at ``as_of``, so no selection sees its own
    future -- and the days are grouped by the universe then in force, as
    :func:`selection_segments` does for the recorded selections. The daemon
    re-screens daily; weekly is the affordable default, on this project's
    measurement that cadence costs nothing in return.

    ``cap`` and ``target`` default to what the shipped configuration uses.
    """
    import pandas as pd

    from cfdinvest.research import harness
    from cfdinvest.universe.screen import effective_sector_cap

    start, end = _utc(start), _utc(end)
    cap = effective_sector_cap(app_config) if cap is None else cap
    if target is None:
        target = int(getattr(app_config.universe, "max_target", 0) or 0) or 50
    dates = pd.date_range(start - pd.Timedelta(days=7), end, freq=rebalance, tz="UTC")
    chosen = harness.screen_selections(
        repository, app_config, dates, cap=cap, target=target, account=account
    )
    selections = [(stamp, names) for stamp, names in chosen.items() if names]
    days = pd.date_range(start, end, freq="B", tz="UTC")
    return selection_segments(selections, days)


def summarise(returns) -> dict:
    """Headline figures from chained daily returns. Absent when there are none."""
    import numpy as np

    r = returns.dropna()
    if len(r) < 2:
        return {}
    equity = (1.0 + r).cumprod()
    years = max((r.index[-1] - r.index[0]).days / 365.25, 1e-9)
    sd = float(r.std())
    return {
        "cagr": float(equity.iloc[-1]) ** (1.0 / years) - 1.0,
        "maxdd": float((equity / equity.cummax() - 1.0).min()),
        "mean_daily": float(r.mean()),
        "t": float(r.mean() / (sd / np.sqrt(len(r)))) if sd else float("nan"),
        "days": int(len(r)),
        "by_year": {int(y): float(v) for y, v in ((1.0 + r).groupby(r.index.year).prod() - 1.0).items()},
    }


def replay_account(repository, app_config, since, until=None, account=None, **overrides):
    """Replay the live period and compare it with the order store.

    Reads the order store and the recorded selections; writes nothing.
    """
    import pandas as pd

    from cfdinvest.persist import orders as orders_store
    from cfdinvest.universe.screen import load_screens

    since = _utc(since)
    rows = [r for r in orders_store.load_orders()
            if r.get("kind") == "plan" and r.get("at") and _utc(r["at"]) >= since
            and (until is None or _utc(r["at"]) <= _utc(until))]
    live, intents = live_engine_entries(rows)
    if not rows:
        raise ReplayError("the order store holds no plan rows in the window")
    last = max(_utc(r["at"]) for r in rows).normalize()

    selections = [(row["as_of"], row["selected"])
                  for row in load_screens(app_config, account) if row.get("selected")]
    days = pd.date_range(since.normalize(), last, freq="B", tz="UTC")
    segments = selection_segments(selections, days)
    settings = {**DECISION_OVERRIDES, **overrides}
    harness_entries, returns = run_segments(repository, app_config, segments, **settings)
    harness_entries = {e for e in harness_entries if since <= e[0] <= last + pd.Timedelta(days=1)}
    report = agreement(live, harness_entries, intents)
    logger.info("Account replay since %s: %s", since.date(), report.summary())
    return report, returns


if __name__ == "__main__":  # pragma: no cover - an operator's command
    import argparse

    from cfdinvest.config import load_config
    from cfdinvest.repository import DataRepository

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("since", help="first session to replay, YYYY-MM-DD")
    parser.add_argument("--until", default=None)
    parser.add_argument("--history", action="store_true",
                        help="run the live-faithful backtest from SINCE to --until "
                             "instead of comparing with the account")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)

    settings = load_config()
    if args.history:
        repo = DataRepository(settings)
        import pandas as pd

        segments = history_segments(repo, settings, args.since,
                                    args.until or pd.Timestamp.now(tz="UTC").normalize())
        _, daily = run_segments(repo, settings, segments)
        figures = summarise(daily)
        print(f"segments {len(segments)}")
        for key, value in figures.items():
            print(f"  {key}: {value}")
        raise SystemExit(0)
    found, _ = replay_account(DataRepository(settings), settings, args.since, args.until)
    print(found.summary())
    for label, part in (("unexplained", found.unexplained),
                        ("not intended", found.not_intended)):
        for entry in sorted(part):
            print(f"  {label}: {entry}")
    assert_reproduces_account(found)
    print("REPRODUCED")
