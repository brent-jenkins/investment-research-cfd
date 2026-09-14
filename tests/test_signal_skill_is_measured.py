"""The daily verification measures whether the signals predicted.

Asked for on 2026-09-14, after a session where the account opened two
positions from a hundred tracked names and made GBP 36.73. The question --
is the app too cautious, or are the signals poor -- could not be answered,
because the project had exactly one day of this reading and no way to
accumulate more.

The distinction it exists to draw:

    the signals predict and costs eat them   -> trade fewer, not better
    the signals do not predict               -> the guards are hiding it

Those call for opposite responses, and P&L cannot tell them apart. Three
readings do.

  HIT RATE and MEAN SIGNED MOVE, gross. A low hit rate is not by itself a
  fault here: the recorded profile runs 30.5% on short holds and 97.7% on
  long ones, because the design pays many small losses to find a few large
  wins. The mean is the reading, not the count.

  CONFIDENCE ORDERING, as a rank correlation. This is what
  ``confidence_sizing_strength = 0.75`` rests on, and the recorded figure on
  the decision bar over 3.65 years is +0.023 -- so a single day near zero is
  normal, and a single day at +0.3 is not a discovery.

  EXCESS over the universe's own move, which is the one that matters. A
  short book on a falling day wins without picking anything. On 2026-09-14
  the full session read 16/40 right, mean signed -0.515%, mean EXCESS
  -0.397% against a universe that moved +0.236% -- the book was short into
  an intraday drift upward, and the day's profit came from one name.

**It never reports PROBLEM.** A day of poor signals is not a fault, and a
verification that shouts on an ordinary bad session is one nobody reads by
the end of the month -- the same reason the monthly job got its own
staleness rule.

Mutations checked:

  M1710 a bad day reported as PROBLEM, so the report cries wolf
  M1711 excess not subtracting the universe move, conflating skill with beta
  M1712 the rank correlation delegated to pandas, which needs scipy and
        raises on the deployment
  M1713 ties not sharing a rank, which is a correlation of argsort
  M1714 an unreadable day reported as fine rather than as unchecked
"""
from __future__ import annotations

import pandas as pd
import pytest

from cfdinvest.verify import daily


def test_the_rank_correlation_is_right_on_a_known_case() -> None:
    """M1712, M1713: the helper that exists because scipy is not installed.

    Checked against cases with a known answer rather than against pandas,
    which is the thing it replaces.
    """
    assert daily._rank_correlation([1, 2, 3, 4], [1, 2, 3, 4]) == pytest.approx(1.0)
    assert daily._rank_correlation([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)

    # Ties share the average rank. Without that this is a correlation of
    # argsort, which invents an order the data does not have.
    tied = daily._rank_correlation([1, 1, 2, 2], [1, 2, 3, 4])
    assert tied == pytest.approx(0.894, abs=0.01), (
        f"ties gave {tied}; equal confidences must share a rank"
    )

    # Monotone but not linear: Spearman sees through the curve, Pearson does
    # not, which is why this is the rank version.
    assert daily._rank_correlation(
        [1, 2, 3, 4, 5], [1, 4, 9, 16, 25]
    ) == pytest.approx(1.0)

    assert daily._rank_correlation([1, 2], [1, 2]) is None, "too few points"
    assert daily._rank_correlation([1, 1, 1], [1, 2, 3]) is None, "no spread"


def test_it_reports_a_bad_day_without_calling_it_a_problem(monkeypatch) -> None:
    """M1710: the measurement turned into an alarm.

    The day it describes was genuinely poor -- 40% right, negative excess --
    and that is not a fault. A verification that reports PROBLEM whenever the
    market is unkind teaches the reader to skip it.
    """
    finding = _run_against(monkeypatch, moves={
        # signal short, and it ROSE: wrong.
        "AAA": +2.0, "BBB": +1.0, "CCC": +3.0,
        # signal short, and it fell: right.
        "DDD": -2.0, "EEE": -1.0,
    }, directions={
        "AAA": "short", "BBB": "short", "CCC": "short",
        "DDD": "short", "EEE": "short",
    }, confidences={
        "AAA": 0.9, "BBB": 0.8, "CCC": 0.7, "DDD": 0.6, "EEE": 0.5,
    })

    assert finding.status == daily.OK, (
        f"a poor day was reported as {finding.status!r}. It is a "
        "measurement, not a guard."
    )
    assert "2/5 right" in finding.detail, finding.detail


def test_excess_separates_skill_from_the_tape(monkeypatch) -> None:
    """M1711: the reading that makes the whole check worth having.

    Every name falls 2% and the book is short everything. The signed move is
    a flattering +2%, and the skill is exactly zero -- it picked nothing,
    the tape fell. Only the excess says so.
    """
    names = ["AAA", "BBB", "CCC", "DDD", "EEE"]
    finding = _run_against(
        monkeypatch,
        moves={name: -2.0 for name in names},
        directions={name: "short" for name in names},
        confidences={name: 0.5 for name in names},
    )

    assert "mean signed +2.000%" in finding.detail, finding.detail
    assert "mean EXCESS +0.000%" in finding.detail, (
        "every name moved identically, so nothing was picked and the excess "
        f"must be zero. Got: {finding.detail}"
    )


def test_a_day_it_cannot_see_is_reported_unchecked(monkeypatch) -> None:
    """M1714: absence reported as fine, which is this project's oldest fault."""
    monkeypatch.setattr(daily, "_first_run_of", lambda *a, **k: None)
    finding = daily._check_signal_skill(_config(), None, "2026-09-14", True)
    assert finding.status == daily.UNCHECKED, (
        f"no run to read and the check said {finding.status!r}"
    )


def test_a_closed_day_is_not_scored(monkeypatch) -> None:
    """A weekend has no signals to judge, and saying so is not a failure."""
    finding = daily._check_signal_skill(_config(), None, "2026-09-12", False)
    assert finding.status == daily.OK
    assert "no session" in finding.detail


def _config():
    from cfdinvest.config import load_config

    return load_config()


def _run_against(monkeypatch, *, moves, directions, confidences):
    """Drive `_check_signal_skill` over a made-up day.

    The prices and the signals are stubbed at the readers, so the check's own
    arithmetic is what is under test rather than the lake.
    """
    day = "2026-09-14"
    stamp = pd.Timestamp(f"{day} 19:00", tz="UTC")

    signals = pd.DataFrame({
        "timestamp": [stamp] * len(moves),
        "ticker": list(moves),
        "direction": [directions[name] for name in moves],
        "confidence": [confidences[name] for name in moves],
        "size": [0.1] * len(moves),
    })

    rows = []
    for name, move in moves.items():
        first = 100.0
        last = first * (1.0 + move / 100.0)
        rows.append({"timestamp": pd.Timestamp(f"{day} 14:00", tz="UTC"),
                     "ticker": name, "open": first, "close": first,
                     "high": first, "low": first})
        rows.append({"timestamp": pd.Timestamp(f"{day} 18:00", tz="UTC"),
                     "ticker": name, "open": last, "close": last,
                     "high": last, "low": last})
    market = pd.DataFrame(rows)

    from cfdinvest.persist import readers

    monkeypatch.setattr(daily, "_first_run_of", lambda *a, **k: "20260914T140000Z")
    monkeypatch.setattr(readers, "load_frame", lambda *a, **k: signals)

    class _Repo:
        def __init__(self, *a, **k):
            pass

        def load_market(self, *a, **k):
            return market

    monkeypatch.setattr("cfdinvest.repository.DataRepository", _Repo)
    return daily._check_signal_skill(_config(), None, day, True)
