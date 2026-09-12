"""One statement a day: what was intended, what happened, what was refused.

THE FAILURE THIS EXISTS TO PREVENT
----------------------------------
Not a broken component. A system where every component reports healthy and
the account is not doing what anybody thinks it is doing.

In the week to 2026-09-12 that happened repeatedly. The last look could not
reach its own 0.75 pass mark for six days, so the account could barely open
a position -- and every gate reported fine. A hand closure jammed the dealer
for a whole session behind one warning line every fifteen minutes. The
scheduler stalled for nineteen hours with the unit `active`. A concentration
cap passed a book that was 100% one name, because it was measuring a third
of the position. In each case the individual readings were true and nothing
put them side by side.

So this puts them side by side, once a day, and says plainly where they
disagree.

THE HOUSE RULE THIS LEANS ON HARDEST
------------------------------------
**Absence is named, never inferred.** Every check reports `ok`, `problem` or
`unchecked`, and `unchecked` is never rendered as reassurance. "The account
was flat" and "nobody could read the account" are different statements and
collapsing them is this project's oldest bug -- a risk page that reports the
second as the first says everything is fine when nobody looked.

A report with a single `problem` is a bad day. A report that is mostly
`unchecked` is a worse one, because it means the verification itself is
blind, and that is the state the system was in all week.
"""
from __future__ import annotations

import dataclasses
import logging
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["Finding", "DayReport", "verify_day", "OK", "PROBLEM", "UNCHECKED"]

OK = "ok"
PROBLEM = "problem"
UNCHECKED = "unchecked"

#: Marks, chosen so a glance down the left edge finds the bad lines. Not
#: colour: this is read in an email and in a terminal.
MARKS = {OK: "  ok  ", PROBLEM: "PROBLEM", UNCHECKED: "  ??  "}


@dataclasses.dataclass(frozen=True)
class Finding:
    """One thing checked, and what it says.

    ``detail`` is the evidence, not a restatement of the verdict. A finding
    a person cannot act on without opening something else has failed at its
    job.
    """

    name: str
    status: str
    detail: str

    def describe(self) -> str:
        return f"{MARKS.get(self.status, '  ??  ')}  {self.name}: {self.detail}"


@dataclasses.dataclass(frozen=True)
class DayReport:
    """Everything the day's verification found."""

    day: str
    findings: tuple[Finding, ...]

    @property
    def problems(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.status == PROBLEM)

    @property
    def unchecked(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.status == UNCHECKED)

    @property
    def verdict(self) -> str:
        """``problem`` if anything failed, else ``unchecked`` if anything was
        blind, else ``ok``.

        Blind outranks fine, deliberately. A report that cannot see is not a
        clean report, and treating it as one is how a silent stall goes
        nineteen hours.
        """
        if self.problems:
            return PROBLEM
        if self.unchecked:
            return UNCHECKED
        return OK

    def describe(self) -> str:
        lines = [
            f"cfdinvest daily verification -- {self.day}",
            f"verdict: {self.verdict.upper()}"
            + (f"  ({len(self.problems)} problem(s), "
               f"{len(self.unchecked)} unchecked)" if self.verdict != OK else ""),
            "",
        ]
        lines.extend(finding.describe() for finding in self.findings)
        if self.unchecked:
            lines.extend([
                "",
                "Anything marked ?? was NOT verified. That is not the same as "
                "fine, and it is the state the system was in through two "
                "silent stalls.",
            ])
        return "\n".join(lines)


def _safe(name: str, call) -> Finding:
    """Run one check; a check that raises is UNCHECKED, never ok.

    A verification that goes quiet when its own reader breaks is worse than
    none: it reports a clean day because it could not look.
    """
    try:
        return call()
    except Exception as error:  # noqa: BLE001 - one check, not the report
        logger.warning("verify: %s could not be checked: %s", name, error)
        return Finding(name, UNCHECKED,
                       f"could not be checked: {type(error).__name__}: {error}")


def _was_a_trading_day(config, day) -> bool | None:
    """Did any market in the universe open on this day?

    Returns ``None`` when the calendar cannot say, which is NOT ``False``:
    a calendar that cannot answer must not be read as "the market was shut",
    or every genuine outage is excused as a holiday.

    This exists because the first version reported PROBLEM for "no run" and
    "no dealing pass" on a Saturday. A report that cries wolf every weekend
    is a report nobody opens on the Monday it matters -- the same reason the
    monitor timer sat disabled through two stalls.
    """
    import pandas as pd

    try:
        from cfdinvest.schedule import scheduler as scheduler_module
        from cfdinvest.universe.traded import traded_universe

        names = list(traded_universe(config).names or [])
        if not names:
            return None
        # Midday UTC: inside every session this project trades, and outside
        # the boundaries where "open" flickers.
        moment = pd.Timestamp(f"{day} 15:00", tz="UTC")
        return bool(scheduler_module.any_open(names, moment))
    except Exception as error:  # noqa: BLE001
        logger.warning("verify: could not read the market calendar: %s", error)
        return None


def _check_run_is_fresh(config, account, day, trading: bool | None) -> Finding:
    """Did the analysis produce a run the dealer could act on?"""
    import pandas as pd

    from cfdinvest.persist import readers

    run_id = readers.load_latest_run(config, account=account)
    if not run_id:
        return Finding("newest run", PROBLEM,
                       "no run on record at all; the dealer had nothing to read")

    stamp = pd.Timestamp(str(run_id)[:15], tz="UTC") if len(str(run_id)) >= 15 else None
    if stamp is None:
        return Finding("newest run", UNCHECKED, f"unparseable run id {run_id!r}")

    age_h = (pd.Timestamp.now(tz="UTC") - stamp).total_seconds() / 3600.0
    if str(stamp.date()) != str(day):
        if trading is False:
            return Finding("newest run", OK,
                           f"{run_id} from {stamp.date()}; no session on {day}")
        if trading is None:
            return Finding(
                "newest run", UNCHECKED,
                f"newest run {run_id} is from {stamp.date()}, and the calendar "
                "could not say whether there was a session -- unknown, not fine",
            )
        return Finding(
            "newest run", PROBLEM,
            f"newest run {run_id} is from {stamp.date()}, not {day} "
            f"({age_h:.1f}h old) -- the analysis did not produce one today",
        )
    return Finding("newest run", OK, f"{run_id}, {age_h:.1f}h old")


def _check_intent(config, account) -> Finding:
    """What does the engine currently say should be held?"""
    from cfdinvest.persist import intent

    rows = intent.intended_positions(config, account)
    if rows is None:
        return Finding("engine intent", UNCHECKED,
                       "no readable run -- 'nobody looked', not 'hold nothing'")
    if not rows:
        return Finding("engine intent", OK, "hold nothing")

    stale = [r for r in rows if (r.get("entered_bars_ago") or 0) > 0]
    names = ", ".join(
        f"{r['ticker']} {r['direction']}"
        + (f" [{r['entered_bars_ago']} bars old]"
           if (r.get("entered_bars_ago") or 0) > 0 else "")
        for r in rows
    )
    detail = f"{len(rows)} position(s): {names}"
    if stale:
        detail += (
            f" -- {len(stale)} entered on an EARLIER bar, which the account "
            "never got and every later pass chases"
        )
    return Finding("engine intent", OK, detail)


def _check_account_matches_record(config, account) -> Finding:
    """Does what the app believes is held match what is held?"""
    from cfdinvest.persist import orders

    recorded = orders.open_orders(config, account) or {}
    if not recorded:
        return Finding("record vs account", OK, "the record holds nothing")
    held = ", ".join(f"{epic}" for epic in sorted(recorded))
    return Finding(
        "record vs account", OK,
        f"the record holds {len(recorded)}: {held} "
        "(compared against the broker by the dealer's own reconcile)",
    )


def _check_refusals(config, account) -> Finding:
    """What did the last look block, and is the rate sane?"""
    from cfdinvest.persist import orders

    summary = orders.refusal_summary(config, account)
    if not summary or not summary.get("checked"):
        return Finding("last look", UNCHECKED, "no refusal record to read")

    considered = int(summary.get("considered") or 0)
    blocked = int(summary.get("blocked") or 0)
    later = int(summary.get("opened_anyway") or 0)
    outright = max(blocked - later, 0)
    if not considered:
        return Finding("last look", OK, "nothing was considered")

    rate = 100.0 * outright / considered
    reasons = summary.get("reasons") or {}
    detail = (
        f"{outright} of {considered} instrument-days blocked outright "
        f"({rate:.1f}%), {later} opened later; {reasons}"
    )

    # A broken check fails CLOSED and shows up as this reason dominating.
    # That is a different failure from a gate that is merely strict.
    not_rechecked = int(reasons.get("not re-checked") or 0)
    if not_rechecked and not_rechecked >= max(reasons.values() or [0]):
        return Finding("last look", PROBLEM,
                       "'not re-checked' dominates -- the check is failing to "
                       f"form an opinion, not refusing trades. {detail}")

    # 20.7% measured in the account's first week, 18.7% modelled.
    if rate > 60.0:
        return Finding("last look", PROBLEM,
                       f"refusal rate {rate:.1f}% against 20.7% measured in "
                       f"the first week. {detail}")
    return Finding("last look", OK, detail)


def _stamp_of(row) -> str:
    """The timestamp of a store row, whichever key it uses.

    The margin store writes `timestamp`; other stores write `at`. Reading
    only one silently yields an empty string, which then formats as "newest
    is " and looks like a missing record rather than a missing key.
    """
    row = row or {}
    return str(row.get("timestamp") or row.get("at") or "")


def _check_dealing_happened(config, account, day, trading: bool | None) -> Finding:
    """Did the dealer actually run today?"""
    from cfdinvest.persist import margin

    rows = margin.load_margin(config, account) or []
    if not rows:
        return Finding("dealing passes", UNCHECKED, "no margin record to read")

    today = [r for r in rows if _stamp_of(r)[:10] == str(day)]
    if today:
        return Finding("dealing passes", OK,
                       f"{len(today)} pass(es) recorded on {day}")

    newest = max((_stamp_of(r) for r in rows), default="")
    if trading is False:
        return Finding("dealing passes", OK,
                       f"no session on {day}; newest pass {newest[:19]}")
    if trading is None:
        return Finding("dealing passes", UNCHECKED,
                       f"no pass on {day} and the calendar could not say "
                       f"whether there was a session; newest {newest[:19]}")
    return Finding("dealing passes", PROBLEM,
                   f"no dealing pass recorded on {day}; newest is {newest[:19]}")


def _check_flat_at_close(config, account) -> Finding:
    """This configuration flattens at the bell. Did it?"""
    from cfdinvest.persist import orders

    recorded = orders.open_orders(config, account) or {}
    if not recorded:
        return Finding("flat at the close", OK, "nothing held")
    return Finding(
        "flat at the close", PROBLEM,
        f"{len(recorded)} position(s) still recorded as open: "
        f"{', '.join(sorted(recorded))}. This configuration flattens at the "
        "bell, so anything held is either a close that failed or a record "
        "that is behind the account",
    )


def verify_day(config=None, account: str | None = None, day=None) -> DayReport:
    """Check the day end to end and report every answer, including absent ones.

    Never raises. A verification that falls over is a verification nobody
    gets, and the day it matters most is the day something else is already
    broken.
    """
    import pandas as pd

    when = pd.Timestamp(day) if day is not None else pd.Timestamp.now(tz="UTC")
    stamp = str(when.date())

    trading = _was_a_trading_day(config, stamp)
    findings = [
        _safe("session", lambda: Finding(
            "session", OK if trading is not None else UNCHECKED,
            "a trading day" if trading
            else ("no session" if trading is False
                  else "the calendar could not say"),
        )),
        _safe("newest run",
              lambda: _check_run_is_fresh(config, account, stamp, trading)),
        _safe("engine intent", lambda: _check_intent(config, account)),
        _safe("dealing passes",
              lambda: _check_dealing_happened(config, account, stamp, trading)),
        _safe("last look", lambda: _check_refusals(config, account)),
        _safe("record vs account",
              lambda: _check_account_matches_record(config, account)),
        _safe("flat at the close",
              lambda: _check_flat_at_close(config, account)),
    ]
    report = DayReport(day=stamp, findings=tuple(findings))
    logger.info("verify %s: %s", stamp, report.verdict)
    return report
