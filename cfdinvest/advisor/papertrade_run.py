"""One pass of the paper book: close what is due, open what qualifies.

Runs from the scheduler, writes a file, and touches nothing else. No order
is placed, no broker interface exists, and the Trading 212 account this is
compared against is a virtual one.

The rules it follows are the ones the backtest actually established, not
the ones it started with:

*   only when the instrument's own market is open, because a fill at a
    price nobody was quoting is not a fill
*   £1,000 of exposure per trade, so leverage cannot silently vary the
    stake -- see papertrade.STAKE_GBP
*   a one-day time stop: 73% of eventual losers were already behind on
    their first day against 29% of eventual winners, and on indices the
    stop took the win rate from 39.5% to 69.5%
*   a fourteen-day cooldown before returning to an instrument: trips
    reopened within 21 days lost £1,489 across 145 of them while those
    opened after a longer gap made £19,954 across 512
*   no new position into a reporting date, where the expected move is
    8.9% on Lululemon's own history and the direction is a coin flip
*   every cost the broker charges, from the recorded table

^VIX is excluded. It is an input to the regime classification and has
never been tradable here -- it carries no volume and Trading 212 offers no
instrument for it.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

logger = logging.getLogger(__name__)

__all__ = [
    "EXCLUDED", "COHORTS", "RAISED_BARS", "cohorts_for",
    "run_paper_pass", "run_all_cohorts",
]

#: Bars above the shipped entry threshold, to test whether raising it helps.
#:
#: Measured on 2,018 historical round trips the win rate stepped from 34.6%
#: below 0.40 to 55.7% between 0.60 and 0.70 -- a cliff, not a gradient.
#: Run as a book rather than as a trip filter it looks very different, which
#: is exactly why it is worth watching forward.
RAISED_BARS: tuple[float, ...] = (0.50, 0.60, 0.70)


def cohorts_for(strategy=None) -> tuple[tuple[str, float], ...]:
    """The arms: the strategy's own entry bar first, then the raised ones.

    The baseline arm is not a round number somebody liked -- it is read from
    ``strategy.entry_confidence``, the bar the engine actually enters on.

    That had to be fixed rather than tidied. The arms were 0.50/0.60/0.70,
    chosen as round numbers without checking, while the engine entered at
    0.35. So every signal between 0.35 and 0.50 was traded by the strategy
    being backtested and by no arm at all, and the forward record -- the one
    measurement that cannot be overfitted -- was validating a configuration
    returning 85.6% a year against the shipped 126.7%.

    Three numbers had drifted apart with no single owner. There is one now.
    """
    entry = 0.35 if strategy is None else float(
        getattr(strategy, "entry_confidence", 0.35)
    )
    arms: list[tuple[str, float]] = [(f"c{round(entry * 100):02.0f}", entry)]
    arms += [
        (f"c{round(bar * 100):02.0f}", bar) for bar in RAISED_BARS if bar > entry
    ]
    return tuple(arms)


#: The default arms, for callers with no config to hand.
COHORTS: tuple[tuple[str, float], ...] = cohorts_for()

#: Signals, not instruments.
EXCLUDED = frozenset({"^VIX"})


def _open_universe(config) -> frozenset[str] | None:
    """Instruments a *new* position may be taken in.

    Narrower than the signal universe on purpose. A name dropped from the
    traded list keeps its signals so an open position can run to its own
    exit rather than being liquidated by the config change -- see
    ``schedule.app._with_held`` -- but it must not be re-entered on the way
    out, or the universe would only ever change on paper.

    ``None`` when nothing is configured, which leaves the pass exactly as
    permissive as it was before this existed. An empty list is *not*
    silently treated as "everything": that is the one reading which would
    turn a broken config into an unrestricted book.
    """
    market = getattr(config, "market", None)
    names = getattr(market, "tickers", None)
    if names is None:
        return None
    return frozenset(str(t) for t in names if str(t))


def _fx_rate_for(ticker: str, rules, quotes: dict[str, float]) -> float:
    """Account currency per unit of the instrument's currency.

    The £1,000 stake is exposure in sterling, so a dollar-priced
    instrument has to come back through the rate or the position size is
    wrong by the rate itself. Getting this upside down misstates every GBP
    figure by its square, which is why the lake's own pair is used rather
    than anything inferred.

    Falls back to 1.0 only for an instrument recorded as sterling. An
    unrecorded currency returns 1.0 too, and is visible as such because
    the trade's fx_rate is written into the book.
    """
    currency = str(getattr(rules.get(ticker), "currency", "") or "").upper()
    if not currency or currency == "GBP":
        return 1.0

    # GBPUSD is dollars per pound, so a dollar instrument wants its
    # reciprocal. The crosses go through the dollar, as they do in the
    # execution layer.
    gbpusd = quotes.get("GBPUSD=X")
    if currency == "USD":
        return 1.0 / gbpusd if gbpusd else 1.0
    if currency == "EUR":
        eurgbp = quotes.get("EURGBP=X")
        return float(eurgbp) if eurgbp else 1.0

    pair = {"JPY": "USDJPY=X", "CAD": "USDCAD=X", "CHF": "USDCHF=X"}.get(currency)
    if pair and quotes.get(pair) and gbpusd:
        return 1.0 / (float(quotes[pair]) * float(gbpusd))
    return 1.0


def _risk_state(config=None, account: str | None = None) -> dict[str, dict[str, float]]:
    """Per-instrument ATR and volume ratio from the last analysis frame.

    The signals frame carries only direction, size, confidence and a
    reason, so neither of these reaches the book through it -- they were
    recording as absent, which is honest but useless. Both are read from
    the analysis panel instead, at the last bar it holds.

    ATR is recorded, never acted on. The stake stays fixed at £1,000 so
    the only thing varying between trades is whether the call was right;
    recording the volatility each trade was struck at means a
    volatility-weighted book can be reconstructed from this file later
    without running the weeks again.

    Never raises. A missing panel leaves the readings absent rather than
    zero -- "not measured" and "measured and flat" are different claims.
    """
    out: dict[str, dict[str, float]] = {}
    try:
        import pandas as pd

        from cfdinvest.persist import readers

        run_id = readers.load_latest_run(config, account=account)
        if run_id is None:
            return out
        frame = readers.load_analysis_frame(run_id, config, account)
        if frame is None or getattr(frame, "empty", True):
            return out

        def panel(name: str):
            rows = frame[frame["panel"].astype(str) == name]
            if rows.empty:
                return None
            wide = rows.pivot_table(
                index="timestamp", columns="series", values="value"
            ).sort_index()
            return wide if not wide.empty else None

        atr, close, volume = panel("volatility.atr"), panel("prices.close"), panel("prices.volume")
        if atr is None or close is None:
            return out

        last_atr, last_close = atr.iloc[-1], close.iloc[-1]

        # Volume against its own recent normal -- the one risk reading that
        # measured strongly (t=+39 against next-day range), though against
        # direction it said nothing.
        #
        # The last bar is dropped when the run was built during that same
        # session, because a part-day's volume against full-day medians
        # reads low for every instrument at once: the first cut of this
        # returned 0.08x across the whole universe, which is a clock
        # artefact and not a quiet market. A partial bar is not a
        # measurement of the day.
        ratios = None
        if volume is not None and len(volume) >= 21:
            bars = volume
            try:
                run_day = pd.Timestamp(run_id).tz_localize(None).normalize()
                last_day = pd.Timestamp(bars.index[-1]).tz_localize(None).normalize()
                if last_day >= run_day:
                    bars = bars.iloc[:-1]
            except (TypeError, ValueError):
                pass
            if len(bars) >= 21:
                median = bars.iloc[-21:-1].median()
                ratios = (bars.iloc[-1] / median).replace(
                    [float("inf"), float("-inf")], pd.NA
                )

        for ticker in atr.columns:
            reading: dict[str, float] = {}
            value, price = last_atr.get(ticker), last_close.get(ticker)
            if pd.notna(value) and pd.notna(price) and float(price) > 0:
                reading["atr_pct"] = float(value) / float(price) * 100.0
            if ratios is not None and ticker in ratios.index and pd.notna(ratios[ticker]):
                reading["volume_ratio"] = float(ratios[ticker])
            if reading:
                out[str(ticker)] = reading
    except Exception as error:  # noqa: BLE001 - a missing panel is not a reason to stall
        logger.debug("No risk state available: %s", error)
    return out


def _sentiment_for(ticker: str) -> dict[str, Any]:
    """The news reading, or an honest absence. Never raises.

    It decides nothing -- seven tests have found no directional edge in it
    standalone, and the one question that might have -- whether it adds to
    what the trend engine knows -- had 37 usable observations. It is
    recorded so that question can be answered later.
    """
    try:
        from cfdinvest.advisor.sentiment import sentiment_for

        reading = sentiment_for(ticker)
        return {"sentiment": reading.score, "sentiment_n": reading.counted}
    except Exception as error:  # noqa: BLE001 - a news feed is not worth a stall
        logger.debug("No sentiment for %s: %s", ticker, error)
        return {"sentiment": None, "sentiment_n": None}


def _market_open(ticker: str, now) -> bool:
    """Whether this instrument's own market is trading. Fails open."""
    try:
        from cfdinvest.schedule import scheduler

        return bool(scheduler.any_open([ticker], now))
    except Exception as error:  # noqa: BLE001 - a calendar gap is not a reason to stop
        logger.debug("No market calendar for %s: %s", ticker, error)
        return True


def _past_the_open(ticker: str, now, delay_minutes) -> bool:
    """Whether this market has been open for ``delay_minutes`` already.

    Asked of the market calendar -- "was it *also* open that long ago" --
    rather than computed from a session start, so daylight saving, holidays
    and per-instrument sessions are handled by the component that already
    knows about them.

    The calendar covers the regular session only: it reports nothing open at
    13:29 UTC and everything open at 13:31, so pre-market cannot answer the
    question early. That is the way this check fails silently if it ever
    changes, which is why a test pins it.

    Fails **closed**: a calendar that cannot be read means the answer is not
    known, and not knowing is not permission.
    """
    delay = int(delay_minutes or 0)
    if delay <= 0:
        return True
    try:
        import pandas as pd

        from cfdinvest.schedule import scheduler

        return bool(scheduler.any_open([ticker], now - pd.Timedelta(minutes=delay)))
    except Exception as error:  # noqa: BLE001
        logger.warning(
            "%s: no calendar for the opening window (%s); not opening", ticker, error
        )
        return False


def _reports_within(ticker: str, now, days: int, earnings) -> bool:
    """Whether a reporting date falls inside the next ``days``."""
    import pandas as pd

    for value in (earnings or {}).get(ticker) or ():
        try:
            when = pd.Timestamp(value)
        except (TypeError, ValueError):
            continue
        if when.tzinfo is None:
            when = when.tz_localize("UTC")
        if now <= when <= now + pd.Timedelta(days=days):
            return True
    return False


def _confidence_of(row) -> float:
    """A signal row's confidence, or zero when it does not carry one."""
    try:
        return float(row.get("confidence") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def run_paper_pass(
    latest: Iterable[dict],
    prices: dict[str, float],
    *,
    now=None,
    config=None,
    account: str | None = None,
    threshold: float = 0.60,
    time_stop_days: int = 1,
    cooldown_days: int = 14,
    blackout_days: int = 2,
    cohort: str = "",
    recheck: bool = True,
    rules=None,
    venue: str = "",
    live_armed: bool | None = None,
    rebalance_threshold: float | None = None,
    open_delay_minutes: int | None = None,
    max_opens_per_pass: int | None = None,
) -> dict[str, Any]:
    """Close what is due and open what qualifies. Never raises.

    ``latest`` is the newest signal row per instrument; ``prices`` the mark
    to use. Returns a summary for the log -- nothing is emailed, and the
    caller is a scheduled job with nobody watching.
    """
    import numpy as np
    import pandas as pd

    from cfdinvest.advisor import livecheck, papertrade
    from cfdinvest.execution.state import needs_resize
    from cfdinvest.ingestion.earnings import load_earnings_dates
    from cfdinvest.livehelper.t212_rules import load_rules

    now = pd.Timestamp(now or pd.Timestamp.now(tz="UTC"))
    if now.tzinfo is None:
        now = now.tz_localize("UTC")

    # Whose terms this book is charging. Absent means Trading 212's table,
    # which is what every pass before 2026-08-28 used and what the tests
    # still exercise -- the switch is made by the caller, not assumed here.
    rules = load_rules() if rules is None else rules
    # The same two guards the account applies, on the same settings. They
    # live here as well as in the bridge because the book is what decides:
    # guarding only the follower makes the two enter at different prices,
    # and the gap between them then measures the timing difference rather
    # than how wrong the simulation is.
    strategy = getattr(config, "strategy", None)
    if open_delay_minutes is None:
        open_delay_minutes = int(getattr(strategy, "open_delay_minutes", 0) or 0)
    if max_opens_per_pass is None:
        max_opens_per_pass = getattr(strategy, "max_opens_per_pass", None)
    if rebalance_threshold is None:
        rebalance_threshold = float(
            getattr(getattr(config, "strategy", None), "rebalance_threshold", 0.25)
        )
    resized = 0
    try:
        earnings = load_earnings_dates(config, account)
    except Exception:  # noqa: BLE001
        earnings = {}

    # The state each decision is made in, gathered once. Recorded rather
    # than acted on: every price-derived feature tested against 2,018
    # historical trips added nothing once the trend engine's confidence was
    # accounted for, and sentiment -- the only candidate not made of price
    # -- could not be tested at all, because two years of headlines
    # overlapped 37 of them. Recording it is how that stops being true.
    filings_rate: dict[str, int] = {}
    try:
        from cfdinvest.ingestion.filings import filing_rate, load_filings

        filings_rate = filing_rate(load_filings(config, account), now)
    except Exception as error:  # noqa: BLE001
        logger.debug("No filing rate available: %s", error)

    risk = _risk_state(config, account)
    book = papertrade.load_book(config, account)
    # Scoped to this arm: a position in another cohort must not block one
    # here, or three independent tests quietly become one.
    book = [r for r in book if str(r.get("cohort") or "") == cohort]
    live = papertrade.open_positions(book, cohort)
    events: list[papertrade.PaperTrade] = []

    signals = {
        str(row.get("ticker") or ""): row
        for row in (latest or ())
        if row.get("ticker")
    }

    # ---- close first, so an instrument can be closed and re-entered later
    for ticker, position in list(live.items()):
        price = prices.get(ticker)
        if price is None or not _market_open(ticker, now):
            continue

        # From the open, not from the last adjustment. A resize row's `at`
        # is when it was adjusted, and taking that would restart the time
        # stop every time a position was trimmed -- so a position that kept
        # drifting would never age out.
        opened_at = position.get("opened_at") or position.get("at")
        held = max(
            (now - pd.Timestamp(opened_at)).total_seconds() / 86_400.0, 0.0
        )
        row = signals.get(ticker) or {}
        direction = str(row.get("direction") or "flat").lower()

        reason = ""
        if direction not in ("flat", position.get("direction")):
            reason = "signal reversed"
        elif held >= time_stop_days:
            # Behind after a day, financing included, is the exit the
            # backtest actually rewarded.
            valued = papertrade.value_trade(
                position, price, now.isoformat(), live_armed=live_armed)
            if valued.net_pnl_gbp is not None and valued.net_pnl_gbp <= 0:
                reason = "time stop"
        if not reason and direction == "flat" and held >= time_stop_days:
            reason = "signal gone"

        if reason:
            events.append(
                papertrade.value_trade(
                    position, price, now.isoformat(), reason=reason,
                    live_armed=live_armed)
            )
            live.pop(ticker, None)

    # ---- then adjust what is still held back towards its target
    #
    # The book holds a fixed £1,000 of exposure, so its target unit count
    # moves as the price does. The account's target is a share of its
    # balance and moves for the same reason. Same trigger, different scale,
    # which is what lets the two be reconciled trade for trade -- and the
    # book could not do it at all while it was priced on a venue that
    # cannot resize an open position.
    #
    # The threshold is the strategy's own, read from one place. A second
    # copy here would drift from the engine's, and the day it drifted the
    # book and the account would adjust on different days for no reason
    # anybody could see.
    for ticker, position in list(live.items()):
        price = prices.get(ticker)
        if price is None or not _market_open(ticker, now):
            continue
        target = papertrade.target_quantity(price, position.get("fx_rate"))
        current = position.get("quantity")
        if target is None or not current:
            continue
        if not bool(needs_resize(
            np.asarray([float(current)]), np.asarray([float(target)]),
            threshold=rebalance_threshold,
        )[0]):
            continue
        adjusted = papertrade.resize_trade(
            position, price, now.isoformat(), rules=rules, live_armed=live_armed,
        )
        if adjusted is None:
            continue
        events.append(adjusted)
        live[ticker] = adjusted.as_row()
        resized += 1

    closed_at = {
        str(row.get("ticker")): row.get("at")
        for row in book if row.get("event") == "close"
    }
    for event in events:
        closed_at[event.ticker] = event.at

    # ---- then open
    opened = 0
    skipped: dict[str, int] = {}

    def skip(why: str) -> None:
        skipped[why] = skipped.get(why, 0) + 1

    # Everything that would open on the recorded signal alone. Gathered
    # first so the re-evaluation runs once for the whole pass rather than
    # once per instrument -- it reads the lake and rebuilds the panels, and
    # doing that twenty times a tick would cost more than the check is
    # worth. On most passes the list is empty and it does not run at all.
    may_open = _open_universe(config)
    candidates = [
        ticker
        for ticker, row in signals.items()
        if ticker not in EXCLUDED
        and (may_open is None or ticker in may_open)
        and ticker not in live
        and str(row.get("direction") or "flat").lower() in ("long", "short")
        and _confidence_of(row) >= threshold
        and prices.get(ticker) is not None
        and _market_open(ticker, now)
        and _past_the_open(ticker, now, open_delay_minutes)
    ]
    rechecked = (
        livecheck.reevaluate(candidates, prices, config=config, at=now)
        if candidates and recheck
        else {}
    )

    for ticker, row in signals.items():
        if ticker in EXCLUDED:
            skip("not an instrument")
            continue
        if ticker in live:
            skip("already open")
            continue

        direction = str(row.get("direction") or "flat").lower()
        if direction not in ("long", "short"):
            continue
        confidence = _confidence_of(row)
        if confidence < threshold:
            skip("below the bar")
            continue

        price = prices.get(ticker)
        if price is None:
            skip("no price")
            continue
        if not _market_open(ticker, now):
            skip("market closed")
            continue
        # The bell is the dearest moment of the day to enter: the opening
        # hour's median spread is 9.26bp against 7.14bp in the core session.
        # Closes are never delayed -- they are handled above, before this.
        if not _past_the_open(ticker, now, open_delay_minutes):
            skip("within the opening window")
            continue

        last_close = closed_at.get(ticker)
        if last_close:
            try:
                gap = (now - pd.Timestamp(last_close)).total_seconds() / 86_400.0
                if gap < cooldown_days:
                    skip("cooling off")
                    continue
            except (TypeError, ValueError):
                pass

        if blackout_days and _reports_within(ticker, now, blackout_days, earnings):
            skip("reports soon")
            continue

        # The last look. The signal was formed on the last bar that closed;
        # this asks whether it still stands against the price being filled
        # at, which at a session open is a night later.
        if recheck:
            still, why = livecheck.holds(rechecked, ticker, direction, threshold)
            if not still:
                skip(why)
                continue

        trade = papertrade.open_trade(
            ticker, direction, price, now.isoformat(), rules=rules,
            fx_rate=_fx_rate_for(ticker, rules, prices),
            cohort=cohort, venue=venue, live_armed=live_armed,
            state={
                "confidence": confidence,
                "size": row.get("size"),
                "regime": str(row.get("reason") or "").rsplit("@", 1)[-1].strip(),
                "reason": row.get("reason"),
                "filings_90d": filings_rate.get(ticker),
                "volume_ratio": risk.get(ticker, {}).get("volume_ratio"),
                "atr_pct": risk.get(ticker, {}).get("atr_pct"),
                **_sentiment_for(ticker),
            },
        )
        if trade is None:
            skip("could not be struck")
            continue
        events.append(trade)
        opened += 1

    # At most so many new positions per pass, highest conviction first --
    # the re-evaluated conviction where there is one, because if only three
    # go through they should be the three the strategy believes in now.
    #
    # Closes are never deferred, so only the opens are counted and cut.
    if max_opens_per_pass is not None:
        fresh = [e for e in events if e.event == "open"]
        if len(fresh) > int(max_opens_per_pass):
            def conviction(event):
                seen = (rechecked or {}).get(event.ticker) or {}
                return float(
                    seen.get("confidence")
                    if seen.get("confidence") is not None
                    else (event.confidence or 0.0)
                )

            keep = sorted(fresh, key=conviction, reverse=True)[:int(max_opens_per_pass)]
            kept = {id(e) for e in keep}
            deferred = len(fresh) - len(keep)
            events = [e for e in events if e.event != "open" or id(e) in kept]
            opened = len(keep)
            skipped["deferred to a later pass"] = (
                skipped.get("deferred to a later pass", 0) + deferred
            )

    written = papertrade.record(events, config, account)
    summary = {
        "at": now.isoformat(),
        "cohort": cohort,
        "resized": resized,
        "opened": opened,
        "closed": sum(1 for e in events if e.event == "close"),
        "still_open": len(live) + opened,
        "written": written,
        "skipped": skipped,
    }
    logger.info(
        "Paper book: opened %d, closed %d, %d open. Skipped: %s",
        summary["opened"], summary["closed"], summary["still_open"],
        ", ".join(f"{k} {v}" for k, v in sorted(skipped.items())) or "nothing",
    )
    return summary


def run_all_cohorts(latest, prices, **kwargs) -> list[dict[str, Any]]:
    """Every arm, on the same signals and the same prices.

    Run together deliberately. Comparing books filled on different days,
    at different prices, would confound the threshold with whatever the
    market happened to be doing -- and the whole reason for three arms is
    to hold everything else constant.
    """
    kwargs.pop("threshold", None)
    kwargs.pop("cohort", None)
    config = kwargs.get("config")
    arms = cohorts_for(getattr(config, "strategy", None))
    return [
        run_paper_pass(latest, prices, threshold=bar, cohort=name, **kwargs)
        for name, bar in arms
    ]
