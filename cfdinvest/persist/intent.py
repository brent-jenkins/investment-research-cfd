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

__all__ = ["intended_positions", "apply_prices", "held_tickers"]


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
        })
    logger.info(
        "Engine intends %d position(s) on equity %.2f: %s",
        len(rows), equity_value,
        ", ".join(f"{r['ticker']} {r['direction']}" for r in rows) or "nothing",
    )
    return rows


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
