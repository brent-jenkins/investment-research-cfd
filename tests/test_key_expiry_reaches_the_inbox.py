"""A key about to lapse must reach a person, a month ahead.

Until 2026-10-20 the warning window was seven days and the warning went
only to monitoring, which mails on RED alone -- an expiring key is a
warning, so the notice reached the dashboard and never an inbox. The
window is now four weeks and the daily verification carries it, on the
owner's cadence: weekly (28, 21 and 14 days left), then daily for the last
seven.

The key in service expires 2027-08-27; the cases below are dated from it.

Mutations checked:

  M2120 the window left at seven days
  M2121 the daily mail not carrying the credentials line at all
  M2122 a key inside the window reported ok
  M2124 daily through the whole window, not weekly
  M2125 the last week weekly, not daily
  M2123 an unrecorded expiry reported ok rather than unchecked
"""
from __future__ import annotations

import pandas as pd

from cfdinvest.monitoring import core
from cfdinvest.verify import daily

EXPIRES = "2027-08-27"


def _on(day: str) -> pd.Timestamp:
    return pd.Timestamp(day, tz="UTC")


def test_the_window_is_four_weeks():
    """M2120: 28 days out speaks, 29 does not."""
    assert core.check_credential_expiry(EXPIRES, now=_on("2027-07-30"))
    assert core.check_credential_expiry(EXPIRES, now=_on("2027-07-29")) == []


def test_weekly_then_daily(monkeypatch):
    """M2122, M2124, M2125: the mail goes on exactly these days."""
    monkeypatch.setenv("CFD_CAPITAL_KEY_EXPIRES", EXPIRES)
    mailed = [
        str(day.date())
        for day in pd.date_range("2027-07-20", "2027-08-26", tz="UTC")
        if daily._check_credentials(now=day).status == daily.PROBLEM
    ]
    assert mailed == ["2027-07-30", "2027-08-06", "2027-08-13",
                      "2027-08-20", "2027-08-21", "2027-08-22", "2027-08-23",
                      "2027-08-24", "2027-08-25", "2027-08-26"]
    between = daily._check_credentials(now=_on("2027-08-01"))
    assert between.status == daily.OK and "2027-08-27" in between.detail


def test_a_key_with_time_left_is_ok_and_says_when(monkeypatch):
    monkeypatch.setenv("CFD_CAPITAL_KEY_EXPIRES", EXPIRES)
    finding = daily._check_credentials(now=_on("2026-10-20"))
    assert finding.status == daily.OK
    assert "2027-08-27" in finding.detail and "311 days" in finding.detail


def test_an_expired_key_is_a_problem(monkeypatch):
    monkeypatch.setenv("CFD_CAPITAL_KEY_EXPIRES", EXPIRES)
    assert daily._check_credentials(now=_on("2027-09-01")).status == daily.PROBLEM


def test_an_unrecorded_expiry_is_unchecked(monkeypatch):
    """M2123: a lifetime nobody wrote down is not a key with time left."""
    monkeypatch.delenv("CFD_CAPITAL_KEY_EXPIRES", raising=False)
    assert daily._check_credentials().status == daily.UNCHECKED
    monkeypatch.setenv("CFD_CAPITAL_KEY_EXPIRES", "not-a-date")
    assert daily._check_credentials().status == daily.UNCHECKED


def test_verify_day_reports_the_credentials(monkeypatch):
    """M2121: the line is in the report, not just in a helper."""
    monkeypatch.setenv("CFD_CAPITAL_KEY_EXPIRES", EXPIRES)
    report = daily.verify_day(day="2026-10-20")
    assert "credentials" in [f.name for f in report.findings]
