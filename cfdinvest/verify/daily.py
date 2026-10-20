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
import math
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["Finding", "DayReport", "verify_day", "OK", "PROBLEM", "UNCHECKED"]

OK = "ok"
PROBLEM = "problem"
UNCHECKED = "unchecked"

#: Marks, chosen so a glance down the left edge finds the bad lines. Not
#: colour: this is read in an email and in a terminal.
MARKS = {OK: "  ok  ", PROBLEM: "PROBLEM", UNCHECKED: "  ??  "}

#: Instrument-days the last look must have considered before its refusal
#: RATE is read as a verdict. At five, a gate that truly refuses three in ten
#: shows above 60% about one day in twenty; at one, every single correct
#: refusal is a 100% alarm. 2026-10-08's real fault was nine of nine.
MIN_REFUSAL_SAMPLE = 5


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


def _check_refusals(config, account, day=None) -> Finding:
    """What did the last look block on ``day``, and is the rate sane?

    Scoped to the day being verified. It read the trailing seven days until
    2026-10-11, so a Saturday's mail repeated the week's refusals under a
    "no session" heading and three mails in a row carried one figure.
    """
    import pandas as pd

    from cfdinvest.persist import orders

    if day is None:
        summary = orders.refusal_summary(config, account)
    else:
        end = pd.Timestamp(day).tz_localize("UTC") + pd.Timedelta(days=1)
        summary = orders.refusal_summary(config, account, days=1, now=end)
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

    # A rate needs a sample. On 2026-10-19 one name was considered, the
    # check refused it, and the engine's own book went flat on it at the
    # next bar -- a correct refusal mailed as "refusing nearly everything".
    # Below the floor the figures stay in the detail and no claim is made.
    # The broken-check branch above is deliberately NOT floored: it reads a
    # reason, not a rate.
    if considered < MIN_REFUSAL_SAMPLE:
        return Finding("last look", OK,
                       f"too few to judge a rate (fewer than "
                       f"{MIN_REFUSAL_SAMPLE} instrument-days). {detail}")

    # No baseline is quoted. The 20.7% this used to cite was measured on the
    # 0.35 gate, and CLAUDE.md forbids comparing it with the 0.75 gate. What
    # remains is the shape: a check refusing nearly everything it sees is
    # the 2026-09-10 failure, whatever the gate.
    if rate > 60.0:
        return Finding("last look", PROBLEM,
                       f"refusal rate {rate:.1f}% -- the check is refusing "
                       f"nearly everything it sees. {detail}")
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


def _check_credentials(now=None) -> Finding:
    """Is the broker key close to lapsing?

    Monitoring already asks this, through ``check_credential_expiry``, and
    mails only on RED -- so a key inside its warning window reached the
    dashboard and never an inbox. This carries the same answer to the daily
    mail: one owner for the rule and the window, read here, not restated.
    """
    import os

    import pandas as pd

    from cfdinvest.monitoring import core

    expires = os.environ.get("CFD_CAPITAL_KEY_EXPIRES")
    issues = core.check_credential_expiry(expires, now=now)
    if not issues:
        moment = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
        expiry = pd.Timestamp(str(expires).strip())
        if expiry.tz is None:
            expiry = expiry.tz_localize("UTC")
        days = (expiry - moment).total_seconds() / 86_400.0
        return Finding("credentials", OK,
                       f"Capital API key expires {expiry:%Y-%m-%d}, "
                       f"in {days:.0f} days")
    issue = issues[0]
    # An unrecorded or unreadable date carries no days_remaining: that is not
    # a key about to lapse, it is a lifetime nobody wrote down.
    if "days_remaining" not in (issue.details or {}):
        return Finding("credentials", UNCHECKED, issue.message)
    # The owner's cadence: weekly through the window, daily in its last week.
    # A PROBLEM is what makes the daily mail send, so on the days between the
    # weekly reminders the same reading is reported ok -- still in the
    # report, and still on the dashboard, just not mailed.
    left = math.ceil(float(issue.details["days_remaining"]))
    reminder = (left <= core.KEY_EXPIRY_DAILY_DAYS
                or (left - core.KEY_EXPIRY_DAILY_DAYS) % 7 == 0)
    text = f"{issue.message} {issue.remedy or ''}".strip()
    if reminder:
        return Finding("credentials", PROBLEM, text)
    return Finding("credentials", OK, f"{text} (weekly reminder; not mailed today)")


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
        _safe("last look", lambda: _check_refusals(config, account, stamp)),
        _safe("record vs account",
              lambda: _check_account_matches_record(config, account)),
        _safe("flat at the close",
              lambda: _check_flat_at_close(config, account)),
        _safe("credentials", _check_credentials),
        # Last, and never a PROBLEM: it measures rather than guards. A day of
        # poor signals is not a fault, and a verification that shouts on an
        # ordinary bad session is one nobody reads by the end of the month.
        _safe("signal skill",
              lambda: _check_signal_skill(config, account, stamp, trading)),
    ]
    report = DayReport(day=stamp, findings=tuple(findings))
    logger.info("verify %s: %s", stamp, report.verdict)
    return report


def _check_signal_skill(config, account, day, trading: bool | None) -> Finding:
    """Did the direction calls predict, and did confidence order them?

    **A measurement, not a guard.** It reports ``ok`` whatever the numbers
    say. What it is for is building the series: the only honest answer to
    "is the app being too cautious" is a run of these readings, and on
    2026-09-14 the project had exactly one.

    Three readings, and the third is the one that matters.

    HIT RATE and MEAN SIGNED MOVE, gross of costs. Costs are the guards'
    business; this asks only whether the direction was right. A low hit rate
    is NOT by itself bad here -- the recorded profile runs 30.5% on short
    holds against 97.7% on long ones, because the design pays many small
    losses to find a few large wins. The mean matters, the count does not.

    CONFIDENCE ORDERING, as a rank correlation against the signed move. This
    is the claim ``confidence_sizing_strength = 0.75`` rests on. The recorded
    figure on the decision bar over 3.65 years is +0.023, so a single day
    near zero is normal rather than alarming.

    EXCESS OVER THE UNIVERSE'S OWN MOVE, which separates skill from the tape.
    A short book on a falling day wins without having picked anything;
    subtracting the universe's mean leaves only what a cross-sectional
    strategy can claim. On 2026-09-14 the signed move was -0.568% and the
    excess -0.597% against a universe that moved -0.050%, so the apparent
    "right side of the tape" was nothing of the kind and the day's profit
    came from one name rather than from the book being short.
    """
    import pandas as pd

    if trading is False:
        return Finding("signal skill", OK, f"no session on {day}")

    from cfdinvest.persist import readers
    from cfdinvest.repository import DataRepository

    run_id = _first_run_of(config, account, day)
    if run_id is None:
        return Finding("signal skill", UNCHECKED,
                       f"no run from {day} to read signals from")

    signals = readers.load_frame("signals", run_id, config, account)
    if signals is None or getattr(signals, "empty", True):
        return Finding("signal skill", UNCHECKED,
                       f"run {run_id} holds no signals")

    frame = signals.reset_index()
    stamps = pd.DatetimeIndex(frame["timestamp"])
    if stamps.tz is None:
        stamps = stamps.tz_localize("UTC")
    rows = frame[stamps == stamps.max()].copy()
    rows["confidence"] = pd.to_numeric(rows["confidence"], errors="coerce")

    names = sorted({str(t) for t in rows["ticker"]})
    market = DataRepository(config).load_market(
        tickers=names, interval=config.market.interval,
        end=pd.Timestamp(day) + pd.Timedelta(days=1),
    )
    if market is None or getattr(market, "empty", True):
        return Finding("signal skill", UNCHECKED, "no prices to score against")

    prices = market.reset_index()
    column = next((c for c in prices.columns
                   if "time" in c.lower() or "date" in c.lower()), None)
    index = pd.DatetimeIndex(prices[column])
    prices["_t"] = index.tz_localize("UTC") if index.tz is None else index
    session = prices[prices["_t"].dt.date == pd.Timestamp(day).date()]
    if session.empty:
        return Finding("signal skill", UNCHECKED, f"no bars on {day}")

    # The session's open to its close, per name: the whole day the signal was
    # live, rather than the window one position happened to be held.
    moves = {}
    for name, block in session.groupby("ticker"):
        block = block.sort_values("_t")
        first = float(block["open"].iloc[0])
        last = float(block["close"].iloc[-1])
        if first > 0:
            moves[str(name)] = 100.0 * (last - first) / first
    if len(moves) < 5:
        return Finding("signal skill", UNCHECKED,
                       f"only {len(moves)} name(s) priced on {day}")

    universe = sum(moves.values()) / len(moves)

    signed, excess, confidences = [], [], []
    for _, row in rows.iterrows():
        name, side = str(row["ticker"]), str(row["direction"])
        if side == "flat" or name not in moves:
            continue
        sign = 1.0 if side == "long" else -1.0
        signed.append(sign * moves[name])
        excess.append(sign * (moves[name] - universe))
        confidences.append(float(row["confidence"]))

    if len(signed) < 5:
        return Finding("signal skill", UNCHECKED,
                       f"only {len(signed)} directional signal(s) on {day}")

    hits = sum(1 for value in signed if value > 0)
    rho = _rank_correlation(confidences, signed)
    return Finding(
        "signal skill", OK,
        "%d signal(s): %d/%d right (%.0f%%), mean signed %+.3f%%, "
        "mean EXCESS %+.3f%% over a universe that moved %+.3f%%, "
        "confidence rho %s. Excess is what separates skill from the tape."
        % (len(signed), hits, len(signed), 100.0 * hits / len(signed),
           sum(signed) / len(signed), sum(excess) / len(excess), universe,
           "unreadable" if rho is None else "%+.3f" % rho)
    )


def _first_run_of(config, account, day):
    """The earliest run persisted on ``day``, or ``None``.

    The earliest rather than the newest: the question is what the signals
    said when they could still have been acted on, and the last run of a
    session decides on a book the flatten has already emptied.
    """
    from cfdinvest.persist import readers

    for lister in ("list_runs", "run_ids", "runs"):
        call = getattr(readers, lister, None)
        if call is None:
            continue
        try:
            found = list(call(config, account))
        except Exception:  # noqa: BLE001 - a verification must not raise
            continue
        prefix = str(day).replace("-", "")
        same_day = sorted(str(r) for r in found if str(r).startswith(prefix))
        if same_day:
            return same_day[0]
    return None


def _rank_correlation(left, right):
    """Spearman, computed rather than delegated.

    `pandas` sends ``method="spearman"`` to `scipy`, which is not installed
    on the deployment -- so asking for it raises, in a function whose whole
    contract is never to. Ties share the average rank, which is what makes
    this Spearman rather than a correlation of ``argsort``.
    """
    def ranked(values):
        order = sorted(range(len(values)), key=lambda i: values[i])
        out = [0.0] * len(values)
        position = 0
        while position < len(order):
            stop = position
            while (stop + 1 < len(order)
                   and values[order[stop + 1]] == values[order[position]]):
                stop += 1
            share = (position + stop) / 2.0 + 1.0
            for index in range(position, stop + 1):
                out[order[index]] = share
            position = stop + 1
        return out

    if len(left) != len(right) or len(left) < 3:
        return None
    a, b = ranked(list(left)), ranked(list(right))
    count = len(a)
    mean_a, mean_b = sum(a) / count, sum(b) / count
    top = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b))
    spread_a = sum((x - mean_a) ** 2 for x in a)
    spread_b = sum((y - mean_b) ** 2 for y in b)
    if spread_a <= 0 or spread_b <= 0:
        return None
    return top / (spread_a ** 0.5 * spread_b ** 0.5)
