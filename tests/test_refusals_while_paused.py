"""The daily mail's last-look check must stay meaningful while paused.

From 2026-10-01 every open the last look APPROVED was then refused by the
pause, and `refusal_summary` counted it as neither, so the rate read 100% by
construction and three mails in a row reported a PROBLEM that was the pause.
The check also read seven days, so a Saturday repeated Friday's figure.

Mutations checked:

  M2010 a pause refusal not counted as the gate clearing the name
  M2011 a held position's paused resize counted as a cleared open
  M2012 the verifier reading seven days, not the day it reports on
  M2013 the window compared as text, which drops the store's own stamps
  M2014 the window with no end, so a day's check reads every day after it
  M2110 no sample floor, so one correct refusal is a 100% alarm (2026-10-19)
  M2111 the floor applied to the broken-check branch, hiding 'not re-checked'
  M2112 the floor read as "at most" rather than "fewer than"

The stamps below are written as the store writes them, with a space. The
first version of this test used ISO "T" stamps and passed against M2013,
which the real record then caught.
"""
from __future__ import annotations

import json

from cfdinvest.persist import orders
from cfdinvest.verify import daily


def _plan(at, ticker, reason, held=0.0):
    return {"kind": "plan", "at": at, "ticker": ticker, "action": "refused",
            "reason": reason, "held_units": held}


def _store(tmp_path, monkeypatch, rows):
    path = tmp_path / "orders.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    monkeypatch.setattr(orders, "_path", lambda config=None, account=None: path)


def test_a_paused_open_is_the_gate_clearing_it(tmp_path, monkeypatch):
    """M2010, M2011."""
    _store(tmp_path, monkeypatch, [
        _plan("2026-10-08 15:05:00.123456+00:00", "AAA", orders.PAUSED_REASON),
        _plan("2026-10-08 15:05:00.123456+00:00", "BBB", "no longer clears the bar"),
        _plan("2026-10-08 15:05:00.123456+00:00", "CCC", orders.PAUSED_REASON, held=5.0),
    ])
    summary = orders.refusal_summary(None, days=3650)
    assert summary["considered"] == 2 and summary["blocked"] == 1


def test_the_mail_reads_the_day_it_names(tmp_path, monkeypatch):
    """M2012: a refusal on Friday is not Saturday's."""
    _store(tmp_path, monkeypatch, [
        _plan("2026-10-08 15:05:00.1+00:00", "AAA", orders.PAUSED_REASON),
        *[_plan("2026-10-09 15:05:00.1+00:00", name, "no longer clears the bar")
          for name in ("AAA", "BBB", "CCC", "DDD", "EEE")],
    ])
    thursday = daily._check_refusals(None, None, "2026-10-08")
    assert thursday.status == daily.OK and "0 of 1" in thursday.detail, thursday.detail
    assert daily._check_refusals(None, None, "2026-10-09").status == daily.PROBLEM
    assert "nothing was considered" in daily._check_refusals(None, None, "2026-10-10").detail


def test_one_correct_refusal_is_not_a_collapse(tmp_path, monkeypatch):
    """M2110, from 2026-10-19: MPC alone was considered and refused, and the
    engine's own book went flat on it at the next bar. One of one is not a
    rate; the figures stay in the detail, the verdict is withheld."""
    _store(tmp_path, monkeypatch, [
        _plan("2026-10-19 14:17:35.1+00:00", "MPC", "no longer a signal"),
    ])
    monday = daily._check_refusals(None, None, "2026-10-19")
    assert monday.status == daily.OK, monday.detail
    assert "1 of 1" in monday.detail and "too few" in monday.detail


def test_the_floor_is_the_smallest_sample_judged(tmp_path, monkeypatch):
    """M2112: exactly MIN_REFUSAL_SAMPLE names refused is judged; one fewer
    is not."""
    floor = daily.MIN_REFUSAL_SAMPLE
    names = [f"N{i}" for i in range(floor)]
    _store(tmp_path, monkeypatch, [
        *[_plan("2026-10-19 15:05:00.1+00:00", n, "no longer a signal") for n in names],
        *[_plan("2026-10-20 15:05:00.1+00:00", n, "no longer a signal") for n in names[1:]],
    ])
    assert daily._check_refusals(None, None, "2026-10-19").status == daily.PROBLEM
    assert daily._check_refusals(None, None, "2026-10-20").status == daily.OK


def test_a_broken_check_is_reported_however_few_it_saw(tmp_path, monkeypatch):
    """M2111: 'not re-checked' is a reason, not a rate, and is not floored."""
    _store(tmp_path, monkeypatch, [
        _plan("2026-10-19 15:05:00.1+00:00", "AAA", "not re-checked"),
    ])
    result = daily._check_refusals(None, None, "2026-10-19")
    assert result.status == daily.PROBLEM and "not re-checked" in result.detail
