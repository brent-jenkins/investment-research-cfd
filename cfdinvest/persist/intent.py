"""What the engine intends to hold, read back from the run that decided it.

One owner for a question four callers were each answering their own way.
The dealing path, the risk snapshot, the quote poller and the order
reconciliation all need to know what the book *should* hold; until
2026-08-30 they each read the shadow book, which implemented the time stop,
the cooldown, the blackout and the confidence gate and none of the breakout
gate, the size hysteresis, the decayed trim, the leverage cap or the
concentration cap. So the account traded a rule set no backtest describes:
163.7% at -52.8% measured through the engine, against 130.5% at -40.3% for
the rules that were actually chosen.

Read, never recomputed. ``run-intraday`` runs the pipeline and persists it;
this reads that run's final bar. Running the engine again here would be a
second computation to keep in step, which is the family of bug this module
exists to close.

Why units rather than a rule list
---------------------------------
Every gate, trim and cap is already inside the position size the engine
recorded. A caller that knows a rule by name is a caller that can drift from
the engine's version of it; a caller that reads units cannot. So nothing
here re-expresses a strategy rule, and nothing downstream needs to.

The share a caller sizes against its own balance is

    share = units x price / (equity x exposure_per_unit_size)

which reproduces the fraction of its own equity the engine held. Sizing is
separate from reading because the instruments to price are not known until
the positions are read -- what may be *closed* is whatever the broker says
is open, not what a configuration lists.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

__all__ = ["intended_positions", "apply_prices", "held_tickers",
           "exit_reasons"]


def exit_reasons(config=None, account: str | None = None) -> dict[str, str]:
    """Why the engine closed each position it has closed, by instrument.

    The dealing path could see THAT a position had left the engine's book
    and never why. Every close written to the order store said
    ``reason=(none recorded)``, so "did the time stop fire or did the signal
    reverse" was unanswerable from the log that exists to answer it -- the
    same shape of gap the engine's own ``_trade_reason`` docstring describes
    having had, where every close claimed to be the signal's doing.

    The engine already records it. ``_trade_reason`` writes one of
    ``stop_out``, ``take_profit``, ``stop_loss``, ``time_stop`` or ``signal``
    on every trade row, and this reads the most recent zero-units row per
    instrument -- the trade that flattened it.

    Never raises and returns ``{}`` on any failure. A missing reason must not
    stop an order being placed; it is an explanation, not a gate.
    """
    from cfdinvest.persist import readers

    try:
        frames = readers.load_execution_frame(None, config, account)
        trades = frames.get("trades")
    except Exception as error:  # noqa: BLE001 - absence is not an outage
        logger.debug("No run to read exit reasons from: %s", error)
        return {}
    if trades is None or getattr(trades, "empty", True):
        return {}
    if not {"asset", "units", "reason"} <= set(getattr(trades, "columns", [])):
        return {}

    found: dict[str, str] = {}
    try:
        closes = trades[trades["units"].astype(float) == 0.0]
        if "timestamp" in closes.columns:
            closes = closes.sort_values("timestamp")
        for _, row in closes.iterrows():
            name = str(row.get("asset") or "")
            reason = str(row.get("reason") or "").strip()
            if name and reason:
                found[name] = reason
    except Exception as error:  # noqa: BLE001
        logger.debug("Could not read exit reasons: %s", error)
        return {}
    return found


def intended_positions(
    config=None, account: str | None = None
) -> list[dict] | None:
    """The engine's latest target, one row per instrument it holds.

    Rows carry ``ticker``, ``direction``, ``confidence``, ``units``,
    ``equity`` and ``exposure_per_unit_size``; ``size`` is present but
    ``None`` until :func:`apply_prices` fills it.

    **Returns ``None`` when no run could be read, and ``[]`` when a run was
    read and holds nothing.** Those are different statements and collapsing
    them is this project's oldest bug: "flat" is a fact about the book and
    "unchecked" is the absence of one, and a risk page that reports the
    second as the first says everything is fine when nobody looked.

    Never raises either way. A caller that does not care may treat ``None``
    as empty; one that reports risk must not.
    """
    from cfdinvest.persist import readers

    exposure = float(
        getattr(getattr(config, "strategy", None), "exposure_per_unit_size", 4.0)
        or 4.0
    )
    try:
        frames = readers.load_execution_frame(None, config, account)
    except Exception as error:  # noqa: BLE001 - absence is not an outage
        logger.warning("No readable run to read intent from: %s", error)
        return None

    positions = frames.get("positions")
    equity = frames.get("equity")
    if positions is None or getattr(positions, "empty", True):
        logger.warning("The latest run recorded no positions")
        return None
    if equity is None or getattr(equity, "empty", True):
        logger.warning("The latest run recorded no equity; nothing can be sized")
        return None

    last = positions.iloc[-1]
    ages = _entry_ages(positions)
    equity_value = float(
        equity["equity"].iloc[-1]
        if "equity" in getattr(equity, "columns", [])
        else equity.iloc[-1]
    )
    if not equity_value or equity_value <= 0:
        logger.warning("The run's final equity is %r; nothing can be sized",
                       equity_value)
        return None

    confidence = _confidence(config, account)
    rows = []
    for ticker, units in last.items():
        name = str(ticker)
        try:
            held = float(units)
        except (TypeError, ValueError):
            continue
        # Reference currency pairs sit on the same panel and are never
        # traded; treating one as a position would place an FX order.
        if not held or "=X" in name:
            continue
        rows.append({
            "ticker": name,
            "direction": "long" if held > 0 else "short",
            "size": None,
            "confidence": confidence.get(name),
            "units": abs(held),
            "equity": equity_value,
            "exposure_per_unit_size": exposure,
            # How many completed bars ago the ENGINE entered this, which is
            # not the same as how long the ACCOUNT has held it. Zero means
            # the engine opened it on the newest completed bar.
            #
            # Carried so the stale-intent loop can be counted rather than
            # inferred: when the account never gets a position the engine
            # entered, the engine goes on intending it and every later pass
            # attempts an open the last look correctly refuses. That is safe
            # and noisy, and it is why the refusal rate reads 47.1% against
            # 20.7% measured in the account's first week.
            #
            # Reported, NOT enforced. Refusing to open on a non-zero age
            # would suppress entries in a book where 20% of trips produce
            # all the profit, and no measurement yet says which refusals are
            # the loop. The number has to exist before that can be settled.
            "entered_bars_ago": ages.get(name),
        })
    logger.info(
        "Engine intends %d position(s) on equity %.2f: %s",
        len(rows), equity_value,
        ", ".join(
            f"{r['ticker']} {r['direction']}"
            + ("" if not r.get("entered_bars_ago")
               else f" (entered {r['entered_bars_ago']} bar(s) ago)")
            for r in rows
        ) or "nothing",
    )
    stale = [r["ticker"] for r in rows if (r.get("entered_bars_ago") or 0) > 0]
    if stale:
        logger.info(
            "%d of %d intended position(s) were entered on an EARLIER bar: "
            "%s. An open the account never got is chased on every later "
            "pass and correctly refused by the last look.",
            len(stale), len(rows), ", ".join(stale),
        )
    return rows


def _entry_ages(positions) -> dict[str, int]:
    """Completed bars since the engine entered each position it still holds.

    Zero means it was entered on the newest completed bar -- a fresh
    opportunity. A larger number means the engine has been holding it while
    the account, for whatever reason, has not.

    Walks back while the holding is non-zero AND KEEPS ITS SIGN. A long that
    becomes a short is a new position, not an old one continuing, and
    counting it as continuing would hide exactly the case worth seeing.

    Pure. Returns ages only for names held on the final bar; a name that is
    flat there is not intended and has no age.
    """
    ages: dict[str, int] = {}
    if positions is None or getattr(positions, "empty", True):
        return ages

    height = len(positions.index)
    final = positions.iloc[-1]

    for ticker, units in final.items():
        try:
            held = float(units)
        except (TypeError, ValueError):
            continue
        if not held:
            continue

        sign = 1.0 if held > 0 else -1.0
        age = 0
        # Start one bar back: the final bar is age zero by definition.
        for offset in range(2, height + 1):
            try:
                earlier = float(positions.iloc[-offset][ticker])
            except (TypeError, ValueError, KeyError, IndexError):
                break
            if not earlier or (1.0 if earlier > 0 else -1.0) != sign:
                break
            age += 1
        ages[str(ticker)] = age

    return ages


#: How old the decision bar may be before the dealer stops OPENING on it.
#:
#: Two bar-spacings on the shipped hourly grid. One is not enough -- the
#: newest completed bar is by construction up to one spacing old the moment
#: it is written, so a one-spacing limit would refuse on a perfectly fresh
#: run. Two gives the analysis a full cycle to miss and be noticed.
STALE_DECISION_SPACINGS = 2.0


def decision_age(config, account=None, now=None):
    """How old the bar the dealer would act on is, or ``None`` if unknowable.

    Returned as a ``Timedelta`` from the decision bar to ``now``. ``None``
    means there is no readable run at all, which is a different statement
    from "the run is old" and the caller must treat it as such.
    """
    import pandas as pd

    from cfdinvest.persist import readers

    try:
        frames = readers.load_execution_frame(None, config, account)
        positions = frames.get("positions")
        if positions is None or getattr(positions, "empty", True):
            return None
        bar = pd.DatetimeIndex(positions.index)[-1]
        if bar.tz is None:
            bar = bar.tz_localize("UTC")
        moment = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="UTC")
        if moment.tz is None:
            moment = moment.tz_localize("UTC")
        return moment - bar
    except Exception as error:  # noqa: BLE001 - never block a pass
        logger.warning("Could not date the decision bar: %s", error)
        return None


def decision_is_stale(config, account=None, now=None, interval=None):
    """Is the run too old to OPEN a position on?

    **This guard exists because the dealer runs in its own process.** While
    dealing sat behind the pipeline in one loop, a stale run was impossible
    by construction: the run had just been written by the job before it. Once
    the two are separated, the analysis can fail, hang or fall behind while
    the dealer carries on perfectly happily, acting on whatever was last
    written. That is a better failure than the one it replaces -- on
    2026-09-11 a wedged pipeline meant the account could not CLOSE for an
    entire session -- but it is only better if opening is guarded.

    So the asymmetry is deliberate and matches the rest of this codebase:
    **a stale run must not OPEN anything, and must never stop a CLOSE.** A
    book the app cannot form a current view about is exactly the book that
    most needs to be reducible -- the same argument the margin guard and the
    reconciliation halt already make.

    ``True`` only when the age is known AND beyond the limit. An unreadable
    run returns ``False`` here and is refused elsewhere: `plan_orders`
    already declines an instrument it has no intent for, so absence does not
    need to be punished twice.
    """
    from cfdinvest.ingestion.capital_prices import bar_span

    age = decision_age(config, account, now=now)
    if age is None:
        return False

    spacing = bar_span(
        interval or getattr(getattr(config, "market", None), "interval", None)
    )
    if spacing is None:
        return False
    return age > spacing * STALE_DECISION_SPACINGS


def decision_is_a_previous_session_close(config, account=None, now=None):
    """Would acting on the intent LIQUIDATE the book on an earlier day's view?

    ``intended_positions`` reads ``positions.iloc[-1]``. On the last bar of
    a session that book is **flat by construction** -- the account flattens
    before the bell and the engine models it -- so the intent there is
    "hold nothing".

    That is true of 19:59 and false of 13:35 the next morning. Between the
    open and the completion of this session's first bar, the newest
    COMPLETED bar IS the previous session's last, and reading it says "hold
    nothing" when what it means is "yesterday ended flat". A position
    carried into the morning is then closed before any bar of the new day
    exists -- and if the instrument is still wanted an hour later it is
    bought straight back, paying the spread twice to arrive where it
    started. The owner's objection on 2026-09-10, and it is right.

    **The test is the harm, not the calendar.** An earlier draft asked
    whether the bar was the last of its session, and could not tell that
    apart from "the last bar we happen to have, because ingestion stopped
    at 15:00" -- so it would have suppressed dealing whenever the lake went
    stale, and the account would quietly never close anything again. Its
    own test caught that.

    What actually matters is whether acting would EMPTY the book on
    evidence from another day. So: an earlier day, and a flat intent. A
    stale bar that still holds positions is a different fault with a
    different answer, and it keeps the old behaviour.

    Nothing here decides whether to keep a position. It declines to decide
    on evidence that is not about today, and waits at most one bar for
    evidence that is -- after which ``plan_orders`` does what it already
    does: target against held, ``none`` when they agree, a close when they
    do not.

    Returns ``False`` when it cannot be told: absence of evidence is not a
    reason to stop acting.
    """
    import pandas as pd

    from cfdinvest.persist import readers

    try:
        frames = readers.load_execution_frame(None, config, account)
        positions = frames.get("positions")
        if positions is None or getattr(positions, "empty", True):
            return False

        index = pd.DatetimeIndex(positions.index)
        bar = index[-1]
        moment = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="UTC")
        if moment.tz is None:
            moment = moment.tz_localize("UTC")
        if bar.tz is None:
            bar = bar.tz_localize("UTC")

        # Within today the bar is this session's own, and the bell must
        # still be able to flatten.
        if bar.date() >= moment.date():
            return False

        # Only when acting would empty the book. A previous day's view that
        # still holds something is ordinary staleness, and acting on it
        # closes some things rather than everything.
        book = positions.iloc[-1]
        for value in book.tolist() if hasattr(book, "tolist") else list(book):
            try:
                if abs(float(value)) > 1e-9:
                    return False
            except (TypeError, ValueError):
                continue
        return True
    except Exception as error:  # noqa: BLE001 - never block a pass
        logger.warning("Could not date the intent's decision bar: %s", error)
        return False


def apply_prices(rows, prices) -> list[dict]:
    """Fill each row's share of the balance, once marks are known.

    An instrument that cannot be priced keeps ``size`` absent rather than
    being given a guess: "not priced" and "priced at nothing" are different
    statements, and the dealing bridge already refuses an unsized row by
    name.
    """
    marks = {str(k): v for k, v in (prices or {}).items()}
    for row in rows or ():
        price = marks.get(str(row.get("ticker")))
        equity = float(row.get("equity") or 0.0)
        exposure = float(row.get("exposure_per_unit_size") or 0.0)
        if not price or equity <= 0 or exposure <= 0:
            logger.debug("%s has no usable mark; left unsized", row.get("ticker"))
            continue
        row["size"] = float(row["units"]) * float(price) / (equity * exposure)
    return rows


def held_tickers(config=None, account: str | None = None) -> list[str]:
    """Just the instruments, for callers that only need the list."""
    # None and [] are different upstream; a caller that only wants names
    # does not care, so both become an empty list here -- deliberately, and
    # once, rather than at four call sites.
    return [str(row["ticker"]) for row in (intended_positions(config, account) or ())]


def _confidence(config, account) -> dict[str, float]:
    """The latest bar's confidence per instrument, or an empty map.

    Passed through so a caller's own entry gate agrees with the one the
    engine already applied, rather than second-guessing it on a default.
    """
    import pandas as pd

    from cfdinvest.persist import readers

    try:
        frame = readers.load_frame("signals", None, config, account)
    except Exception as error:  # noqa: BLE001
        logger.debug("No signals to read confidence from: %s", error)
        return {}
    if frame is None or getattr(frame, "empty", True):
        return {}
    working = frame.reset_index()
    if "timestamp" not in working.columns or "confidence" not in working.columns:
        return {}
    working["timestamp"] = pd.to_datetime(
        working["timestamp"], utc=True, errors="coerce"
    )
    working = working[working["timestamp"] == working["timestamp"].max()]
    return {
        str(row.get("ticker")): row.get("confidence")
        for _, row in working.iterrows()
        if row.get("ticker")
    }
