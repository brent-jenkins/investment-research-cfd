"""Reading the artefacts and assembling one snapshot.

The only part of this package that touches a filesystem, and it only reads.
:mod:`cfdinvest.risk.model` stays pure so a test can construct any
combination of missing artefacts without a disk; this function does the
loading and hands them over.

**No artefact is required.** A lake with a run but no analysis panels gives
position units and no exposure; one with no runs at all gives a snapshot that
says so. Every absence is named in ``unavailable`` rather than filled in.

**The positions come from a real book, never from the backtest.** This is
the correction of a defect that made the page useless: it read
``positions.parquet`` from the newest run -- a simulation whose equity had
compounded for two years -- and measured it against the limits of a £2,000
account. On 2026-08-27 that reported gross exposure of £810,945 against a
£3,000 limit, five breaches, critical. Nothing anybody held was involved.
The figure had also never been converted: units times a dollar close,
labelled GBP.

A limit that is breached every day is a limit nobody reads, so a real breach
would have arrived on a page that was already red -- while the monitoring
snapshot said green in the same minute.

The order is confirmed book, then nothing. The paper book was the middle
rung until it was removed on 2026-08-30; `paper:<arm>` still appears in
snapshots written before then, and nothing writes new ones:

* **confirmed** -- what you recorded actually placing. The real account, so
  it wins whenever any confirmation exists at all.
* **paper:<arm>** -- the forward simulation's baseline arm, at £1,000 of
  notional per position. Not what you hold, but the same size as what you
  would hold, which is what makes it comparable to the limits.
* **nothing** -- reported absent. Not flat: "no book was found" and "the
  book is flat" are different statements and only one of them is calm.

Marks are converted to sterling from the rate the position was opened at,
which each book records. A stale rate is a small error; no rate at all is a
figure that is wrong by the exchange rate and looks right.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

import pandas as pd

from cfdinvest.risk.model import RiskLimits, RiskSnapshot, build_risk_snapshot
from cfdinvest.risk.scheduler_status import compute_scheduler_status

logger = logging.getLogger(__name__)

__all__ = ["snapshot_from_disk"]


def _currencies(metadata: Mapping[str, Any] | None) -> dict[str, str]:
    """The instrument-to-currency map, if the run recorded one.

    Usually it did not: the execution metadata carries the FX *policy* but
    not the map, so currency exposure is reported against whatever is known
    and the unmapped instruments are listed rather than assumed domestic in
    silence.
    """
    mapping = (metadata or {}).get("instrument_currencies")
    return dict(mapping) if isinstance(mapping, Mapping) else {}


def _confirmed_book(config, account: str | None):
    """Signed units per instrument from the confirmations, or ``None``.

    ``None`` means no confirmation has ever been recorded. An empty dict
    means every confirmed position has been closed, which is a flat book and
    a different thing.
    """
    try:
        from cfdinvest.persist import confirm

        recorded = confirm.load_confirmations(config, account)
        if not recorded:
            return None
        return {
            ticker: dict(position)
            for ticker, position in confirm.confirmed_positions(recorded).items()
        }
    except Exception as error:  # noqa: BLE001 - a risk page never raises
        logger.warning("Could not read the confirmed book: %s", error)
        return None


def _engine_intent(config, account: str | None):
    """Signed units per instrument, from the run the engine last decided.

    Read from ``persist.intent`` rather than from a book of its own. It used
    to read the shadow book's baseline arm, which applied a different rule
    set from the engine -- no breakout gate, no size hysteresis, no decayed
    trim, no leverage cap, no concentration cap -- so the risk being
    reported was the risk of a strategy nobody had chosen.

    Units are what the engine recorded, so every cap and gate is already
    inside them and none is re-expressed here.
    """
    try:
        from cfdinvest.persist import intent

        rows = intent.intended_positions(config, account)
        if rows is None:
            # No readable run. Unchecked, not flat.
            return None, None
        if not rows:
            # A run that holds nothing is a *fact* about the book, and the
            # snapshot must report it as flat rather than as unknown.
            return {}, "engine"
        positions = {}
        for row in rows:
            quantity = float(row.get("units") or 0.0)
            if str(row.get("direction") or "").lower() == "short":
                quantity = -quantity
            positions[str(row["ticker"])] = {**row, "quantity": quantity}
        return positions, "engine"
    except Exception as error:  # noqa: BLE001 - a risk page never raises
        logger.warning("Could not read the engine's intent: %s", error)
        return None, None


def _positions_frame(positions: Mapping[str, Mapping[str, Any]], now):
    """One row of signed units, stamped with the latest event in the book."""
    stamps = [
        stamp
        for stamp in (pd.Timestamp(p["at"]) for p in positions.values() if p.get("at"))
        if stamp is not pd.NaT
    ]
    as_of = max(stamps) if stamps else (pd.Timestamp(now) if now is not None else None)
    if as_of is None:
        as_of = pd.Timestamp.now(tz="UTC")
    as_of = as_of.tz_localize("UTC") if as_of.tz is None else as_of.tz_convert("UTC")
    return pd.DataFrame(
        [{t: float(p.get("quantity") or 0.0) for t, p in positions.items()}],
        index=pd.DatetimeIndex([as_of], name="timestamp"),
    )


def _sterling_marks(positions: Mapping[str, Mapping[str, Any]], analysis_frame):
    """Current close per instrument, converted at the rate it was opened at.

    An instrument whose rate was not recorded gets no mark, so its exposure
    is reported absent rather than quoted in the wrong currency. That is the
    bug this whole function exists to stop repeating: the old figure was
    units times a dollar close and was labelled GBP.
    """
    from cfdinvest.risk.model import _latest_prices

    closes = _latest_prices(analysis_frame)
    if closes is None or len(closes) == 0:
        return None

    marks: dict[str, float] = {}
    for ticker, position in positions.items():
        close = closes.get(ticker)
        rate = position.get("fx_rate")
        try:
            rate = float(rate) if rate is not None else 1.0
        except (TypeError, ValueError):
            continue
        if close is None or rate <= 0:
            continue
        try:
            marks[ticker] = float(close) * rate
        except (TypeError, ValueError):
            continue
    return pd.Series(marks, dtype="float64") if marks else None


def snapshot_from_disk(
    config,
    *,
    run_id: str | None = None,
    account: str | None = None,
    limits: RiskLimits | None = None,
    instrument_leverage: Mapping[str, float] | None = None,
    now: pd.Timestamp | None = None,
) -> RiskSnapshot:
    """Build a snapshot from persisted artefacts. Reads only.

    Returns a snapshot even when nothing has been recorded — with the
    absences named — because a risk dashboard that renders nothing when the
    pipeline has not run is a dashboard that cannot tell you the pipeline has
    not run.
    """
    from cfdinvest.persist import readers, scheduler_state

    try:
        chosen = run_id or readers.load_latest_run(config)
    except Exception as error:  # noqa: BLE001
        logger.warning("Could not list runs: %s", error)
        chosen = None

    status = compute_scheduler_status(
        scheduler_state.load_scheduler_state(config, account), now=now
    )

    # The book first, because it does not depend on a run having happened.
    # A confirmed position is real whether or not the pipeline ran today.
    confirmed = _confirmed_book(config, account)
    if confirmed is not None:
        positions, source = confirmed, "confirmed"
    else:
        positions, source = _engine_intent(config, account)

    if chosen is None:
        return build_risk_snapshot(
            positions_frame=(
                None if positions is None else _positions_frame(positions, now)
            ),
            book=source,
            scheduler_status=status,
            limits=limits or RiskLimits(),
            now=now,
        )

    def read(name: str):
        try:
            return getattr(readers, f"load_{name}")(chosen, config, account)
        except Exception as error:  # noqa: BLE001 - one absent artefact, not all
            logger.warning("Could not read %s for run %s: %s", name, chosen, error)
            return None

    execution_metadata = read("execution_metadata") or {}
    analysis_frame = read("analysis_frame")

    return build_risk_snapshot(
        positions_frame=(
            None if positions is None else _positions_frame(positions, now)
        ),
        prices=(
            None if not positions else _sterling_marks(positions, analysis_frame)
        ),
        book=source,
        regime_frame=read("regime_frame"),
        pnl_frame=read("pnl_frame"),
        analysis_frame=analysis_frame,
        assessments_frame=_assessments(config, account),
        scheduler_status=status,
        limits=limits or RiskLimits(),
        run_id=chosen,
        run_initial_equity=_initial_equity(execution_metadata),
        instrument_currencies=_currencies(execution_metadata),
        instrument_leverage=instrument_leverage,
        now=now,
    )


def _initial_equity(metadata: Mapping[str, Any] | None) -> float | None:
    """What the run itself started with, for the drawdown denominator."""
    try:
        equity = float((metadata or {}).get("initial_equity"))
    except (TypeError, ValueError):
        return None
    return equity if equity > 0 else None


def _assessments(config, account: str | None):
    """Live-helper assessments, if any have been recorded."""
    try:
        from cfdinvest.persist import assessments

        return assessments.load_assessments(config, account)
    except Exception as error:  # noqa: BLE001
        logger.debug("No recorded assessments: %s", error)
        return None
