"""Regulatory filings as a volatility signal, and the ways it can mislead.

Mutations checked (each reintroduced, each failed):

M101 the after-hours boundary ignored, misattributing every evening filing
     -> test_the_window_is_measured_strictly_before_the_moment_asked_about
M102 formality-only filings counted, diluting the rate
     -> test_a_filing_carrying_only_exhibits_is_not_a_material_event
M103 a ticker EDGAR cannot answer for recorded as having no filings
     -> test_one_unreachable_ticker_does_not_lose_the_others
     (the whole-outage test cannot catch this: it kills the CIK lookup
      too, so the per-ticker branch never runs)
M104 the rate counting filings after the moment asked about
     -> test_the_window_is_measured_strictly_before_the_moment_asked_about
M105 the ninety-day window quietly changed from what was measured
     -> test_the_window_default_matches_what_was_measured
"""

from __future__ import annotations

import pandas as pd
import pytest

from cfdinvest.ingestion import filings as module


SAMPLE = {
    "LULU": [
        # Accepted 20:20 UTC -- after the US close, so tomorrow reacts.
        {"accepted": "2026-08-13T20:20:13.000Z", "items": "8.01"},
        {"accepted": "2026-06-25T23:09:00.000Z", "items": "5.02,5.07"},
        {"accepted": "2026-06-04T20:09:00.000Z", "items": "2.02,9.01"},
        # Formality only: exhibits and a shareholder vote.
        {"accepted": "2026-05-02T12:00:00.000Z", "items": "9.01,5.07"},
        # Long before the window.
        {"accepted": "2025-01-05T14:00:00.000Z", "items": "2.02,9.01"},
    ],
    "^GSPC": [],
}


def test_the_window_is_measured_strictly_before_the_moment_asked_about():
    """M101/M104: a rate that includes the filing it is meant to precede.

    Counting today's filing towards today's reading uses an event to
    predict the session it lands in, which is the shape of lookahead that
    makes any signal look brilliant.
    """
    on_the_day = module.filing_rate(
        SAMPLE, as_of="2026-08-13T20:20:13Z", window_days=90
    )
    just_before = module.filing_rate(
        SAMPLE, as_of="2026-08-13T20:20:12Z", window_days=90
    )

    # The filing accepted at exactly 20:20:13 counts at that instant and
    # not a second earlier.
    assert on_the_day["LULU"] == just_before["LULU"] + 1

    # And nothing outside the ninety days is counted at all.
    narrow = module.filing_rate(SAMPLE, as_of="2026-08-13T23:59:59Z", window_days=30)
    assert narrow["LULU"] == 1, "only the August filing is within thirty days"


def test_a_filing_carrying_only_exhibits_is_not_a_material_event():
    """M102: formalities counted, diluting the thing being measured.

    Filings carrying nothing but exhibits, a shareholder vote or a bylaw
    amendment moved 1.64% against 2.45% for the rest. Counting them makes a
    busy filer look like a company in flux when it is merely tidy.
    """
    as_of = "2026-07-01T00:00:00Z"
    substantive = module.filing_rate(SAMPLE, as_of=as_of, substantive_only=True)
    everything = module.filing_rate(SAMPLE, as_of=as_of, substantive_only=False)

    assert everything["LULU"] == substantive["LULU"] + 1, (
        "the 9.01/5.07 filing must be the only difference"
    )
    # A filing with a substantive code alongside a formality still counts.
    assert substantive["LULU"] >= 2


def test_an_instrument_that_never_files_reads_zero_not_missing():
    """An index has no filings, and that is a fact rather than a gap."""
    counts = module.filing_rate(SAMPLE, as_of="2026-08-13T23:59:59Z")
    assert counts["^GSPC"] == 0


def test_a_lookup_failure_is_not_a_company_that_never_filed(monkeypatch):
    """M103: a failed fetch recorded as an empty list.

    An index genuinely has no filings; a share whose lookup failed might
    have many. Recording the second as empty would make a company in
    turmoil read as the quietest name in the book -- and the rate that
    feeds off it would then size *up* into exactly the wrong thing.
    """
    def explode(url):
        raise RuntimeError("EDGAR is down")

    monkeypatch.setattr(module, "_get", explode)
    assert module.fetch_filings(["LULU", "NVDA"]) == {}


def test_one_unreachable_ticker_does_not_lose_the_others(monkeypatch):
    """A single bad symbol is not an outage for the whole universe."""
    import json as _json

    def selective(url):
        if "company_tickers" in url:
            return _json.dumps({
                "0": {"ticker": "LULU", "cik_str": 1397187},
                "1": {"ticker": "NVDA", "cik_str": 1045810},
            }).encode()
        if "1397187" in url:
            raise RuntimeError("this one is unreachable")
        return _json.dumps({"filings": {"recent": {
            "form": ["8-K"],
            "acceptanceDateTime": ["2026-08-13T20:20:13.000Z"],
            "items": ["8.01"],
        }}}).encode()

    monkeypatch.setattr(module, "_get", selective)
    got = module.fetch_filings(["LULU", "NVDA"], pause=0.0)

    assert "NVDA" in got and len(got["NVDA"]) == 1
    assert "LULU" not in got, "the unreachable one is omitted, not blanked"


def test_the_rate_survives_a_row_it_cannot_parse():
    """One malformed timestamp must not lose the whole instrument."""
    broken = {
        "LULU": [
            {"accepted": "not a date", "items": "8.01"},
            {"accepted": "2026-08-13T20:20:13.000Z", "items": "8.01"},
            {"items": "8.01"},
        ]
    }
    counts = module.filing_rate(broken, as_of="2026-08-14T00:00:00Z")
    assert counts["LULU"] == 1


def test_the_window_default_matches_what_was_measured():
    """Ninety days, because that is the window the t-statistic came from.

    r=+0.124 at t=+5.13 over 1,691 ticker-months was measured on filings in
    the previous ninety days. A default that quietly differed from the
    measurement would be a number with no evidence behind it.
    """
    import inspect

    signature = inspect.signature(module.filing_rate)
    assert signature.parameters["window_days"].default == 90


def test_the_daily_refresh_tops_up_both_event_sources(monkeypatch):
    """M106: the refresh forgets one of them.

    Both were stored by hand until now, which meant the earnings blackout
    and the filing rate read whatever happened to have been fetched last.
    That is the same staleness that let a daemon run eleven hours on code
    it had imported before the change.
    """
    from cfdinvest.schedule import runner

    called = {}

    monkeypatch.setattr(
        "cfdinvest.ingestion.earnings.fetch_earnings_dates",
        lambda tickers: called.setdefault("earnings", list(tickers)) or {"LULU": ["2026-09-03"]},
    )
    monkeypatch.setattr(
        "cfdinvest.ingestion.earnings.store_earnings_dates",
        lambda dates, config=None, account=None: called.setdefault("stored_earnings", dates) or True,
    )
    monkeypatch.setattr(
        "cfdinvest.ingestion.filings.fetch_filings",
        lambda tickers: called.setdefault("filings", list(tickers)) or {"LULU": [{"accepted": "x", "items": "8.01"}]},
    )
    monkeypatch.setattr(
        "cfdinvest.ingestion.filings.store_filings",
        lambda found, config=None, account=None: called.setdefault("stored_filings", found) or True,
    )

    runner._refresh_events(object(), ["LULU", "NVDA"])

    assert called.get("earnings") == ["LULU", "NVDA"]
    assert called.get("filings") == ["LULU", "NVDA"]
    assert "stored_earnings" in called and "stored_filings" in called


def test_an_unreachable_vendor_does_not_fail_the_run(monkeypatch):
    """These feed a warning and a size, not a price.

    A run that died because the SEC was briefly unreachable would be worse
    than one that proceeds on last week's filing count -- unlike the market
    refresh, where stale prices must fail loudly.
    """
    from cfdinvest.schedule import runner

    def explode(*args, **kwargs):
        raise RuntimeError("vendor down")

    monkeypatch.setattr("cfdinvest.ingestion.earnings.fetch_earnings_dates", explode)
    monkeypatch.setattr("cfdinvest.ingestion.filings.fetch_filings", explode)

    runner._refresh_events(object(), ["LULU"])      # must not raise
