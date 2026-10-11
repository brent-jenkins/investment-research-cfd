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
        _plan("2026-10-09 15:05:00.1+00:00", "AAA", "no longer clears the bar"),
    ])
    thursday = daily._check_refusals(None, None, "2026-10-08")
    assert thursday.status == daily.OK and "0 of 1" in thursday.detail, thursday.detail
    assert daily._check_refusals(None, None, "2026-10-09").status == daily.PROBLEM
    assert "nothing was considered" in daily._check_refusals(None, None, "2026-10-10").detail
