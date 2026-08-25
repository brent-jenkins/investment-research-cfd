"""Turning what is on disk into journal entries, once each.

The writer's whole job is knowing what has already been written. A journal
that re-narrated every confirmation on every run would grow by the size of
your history each time, and the entry you wrote a month ago would be replaced
by one written with a month of hindsight — which is the thing this layer
exists to prevent.

So it is idempotent by key: one entry per confirmation, and a confirmation
already journalled is skipped.

**Nothing here recomputes.** It reads confirmations, assessments, regimes and
the P&L decomposition, and composes English from them. No P&L is calculated,
no size is checked, no verdict is reached.
"""

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from cfdinvest.journal.entry import JournalEntry, compose_entry

logger = logging.getLogger(__name__)

__all__ = ["entry_key", "build_entries", "write_journal"]


def entry_key(entry: Any) -> str:
    """What makes an entry the same entry.

    The instrument and the moment. Two confirmations for one instrument in the
    same second would collide, which is the same tolerance the confirmation
    store itself accepts.
    """
    moment = getattr(entry, "timestamp", None)
    try:
        stamp = pd.Timestamp(moment).isoformat()
    except Exception:  # noqa: BLE001
        stamp = str(moment)
    return f"{getattr(entry, 'kind', '')}|{getattr(entry, 'instrument', '')}|{stamp}"


def _assessment_for(assessments: pd.DataFrame, ticker: str, when) -> dict | None:
    """The helper's verdict closest before this fill.

    Before, never after: an assessment made after the trade is a different
    thought, and attaching it would put a judgement in the entry that was not
    available when the decision was made.
    """
    if assessments is None or assessments.empty:
        return None
    if "instrument" not in assessments.columns:
        return None

    mine = assessments[assessments["instrument"].astype(str) == str(ticker)]
    if mine.empty:
        return None

    if "timestamp" in mine.columns and when is not None:
        stamps = pd.to_datetime(mine["timestamp"], utc=True, errors="coerce")
        try:
            moment = pd.Timestamp(when)
            if moment.tz is None:
                moment = moment.tz_localize("UTC")
        except Exception:  # noqa: BLE001
            moment = None
        if moment is not None:
            earlier = mine[stamps <= moment]
            if not earlier.empty:
                mine = earlier
    return dict(mine.iloc[-1])


def build_entries(
    confirmations,
    *,
    assessments: pd.DataFrame | None = None,
    regimes: dict | None = None,
    elevated=(),
    run_id: str | None = None,
    previous=None,
    already: set[str] | None = None,
) -> list[JournalEntry]:
    """Compose an entry for every confirmation not already journalled.

    Pure given its inputs. ``previous`` seeds "what changed" for the first new
    entry; each subsequent one compares against the entry before it.
    """
    written = set(already or ())
    entries: list[JournalEntry] = []
    last = previous

    for confirmation in sorted(
        confirmations or [], key=lambda c: getattr(c, "timestamp", 0)
    ):
        assessment = _assessment_for(
            assessments,
            getattr(confirmation, "ticker", None),
            getattr(confirmation, "timestamp", None),
        )
        entry = compose_entry(
            confirmation,
            assessment=assessment,
            regimes=regimes,
            elevated=elevated,
            previous=last,
            run_id=run_id,
        )
        if entry_key(entry) in written:
            continue
        written.add(entry_key(entry))
        entries.append(entry)
        last = entry

    return entries


def write_journal(
    config=None,
    *,
    account: str | None = None,
    run_id: str | None = None,
) -> list[JournalEntry]:
    """Read the artefacts, compose what is new, append it. Never raises.

    Returns the entries written, which is empty when everything already was.
    """
    from cfdinvest.persist import assessments as assessment_store
    from cfdinvest.persist import confirm, journal as journal_store, readers

    try:
        confirmations = confirm.load_confirmations(config, account)
    except Exception as error:  # noqa: BLE001
        logger.warning("Could not read confirmations: %s", error)
        return []

    if not confirmations:
        return []

    try:
        assessments = assessment_store.load_assessments(config, account)
    except Exception as error:  # noqa: BLE001
        logger.debug("No assessments to attach: %s", error)
        assessments = pd.DataFrame()

    regimes, elevated = {}, ()
    try:
        from cfdinvest.risk import compute_regime_risk

        result = compute_regime_risk(
            readers.load_regime_frame(run_id, config, account)
        )
        regimes = dict(result.get("regimes") or {})
        elevated = tuple(result.get("elevated") or ())
    except Exception as error:  # noqa: BLE001
        logger.debug("No regime context: %s", error)

    existing = journal_store.load_entries(config, account)
    entries = build_entries(
        confirmations,
        assessments=assessments,
        regimes=regimes,
        elevated=elevated,
        run_id=run_id,
        previous=existing[-1] if existing else None,
        already={entry_key(entry) for entry in existing},
    )

    if entries:
        journal_store.append_entries(entries, config, account=account)
        logger.info("Journalled %d new entr(ies)", len(entries))
    return entries
