"""Regulatory filings, and how busy a company has been telling the regulator.

An 8-K is a company disclosing a material event -- a departure, an
acquisition, a restatement, a guidance change. Filed within four business
days and usually after the close, which means it reaches EDGAR before the
journalist reads it and writes the story tomorrow. Verified on Lululemon:
the 8-K headed "UPDATE TO LEADERSHIP TEAM" was accepted at 20:20 UTC on
2026-08-13 and the "AI Chief Departs" headline is dated the 14th.

Why this and not news sentiment
-------------------------------
Both were measured. Neither predicts direction. The difference is that this
one can be checked: EDGAR keeps every filing since 1993, timestamped to the
second, so a rule built on it is testable before it is trusted. A news feed
carries three days and no archive.

What was measured, across the nineteen shares in the universe:

*   the session after a filing moves 1.47x as far as an ordinary one, and
    17 of 18 shares show it
*   a move over 5% is 2.6 times as likely -- 11.1% of sessions against 4.3%
*   direction is a coin flip: mean +/-0.03%, and no Item code reaches |t|=2
*   **filings in the previous 90 days predict next month's volatility at
    r=+0.124, t=+5.13** across 1,691 ticker-months, and next month's return
    at t=-0.77

That last line is the one worth building on, and it is the strongest result
this project has measured. A company filing a lot is a company in flux, and
flux persists. It says how big the next move will be, never which way -- so
what it feeds is position sizing and a decision to stand aside, never a
direction.

Only equities file. Indices, commodities and FX return nothing here, which
is correct rather than a gap.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

logger = logging.getLogger(__name__)

__all__ = [
    "FILINGS_DIR",
    "USER_AGENT",
    "user_agent",
    "fetch_filings",
    "store_filings",
    "load_filings",
    "filing_rate",
]

FILINGS_DIR = "filings"
SCHEMA_VERSION = 1

#: The SEC requires a contact address in the User-Agent and rate-limits to
#: ten requests a second. Both are conditions of use rather than etiquette.
#: The address is read from the environment rather than written here, so the
#: repository carries nobody's email; with none set, fetching is refused
#: rather than sent anonymously, which the SEC would block anyway.
CONTACT_ENV = "CFD_SEC_CONTACT"
USER_AGENT = "cfdinvest research"


def user_agent() -> str:
    """The User-Agent with the operator's contact address. Raises without one."""
    contact = os.environ.get(CONTACT_ENV, "").strip()
    if not contact:
        raise RuntimeError(
            f"set {CONTACT_ENV} to a contact email address: the SEC requires "
            "one in the User-Agent of every EDGAR request"
        )
    return f"{USER_AGENT} {contact}"

#: Items that are attached as a formality and say nothing about the event:
#: exhibits, a shareholder vote, a bylaw amendment. Filings carrying only
#: these moved 1.64% against 2.45% for filings carrying something else.
FORMAL_ITEMS = frozenset({"9.01", "5.07", "5.03"})


def _get(url: str) -> bytes:
    import urllib.request

    request = urllib.request.Request(url, headers={"User-Agent": user_agent()})
    return urllib.request.urlopen(request, timeout=30).read()


def _cik_map() -> dict[str, str]:
    raw = json.loads(_get("https://www.sec.gov/files/company_tickers.json"))
    return {
        str(entry["ticker"]).upper(): str(entry["cik_str"]).zfill(10)
        for entry in raw.values()
    }


def fetch_filings(
    tickers: Iterable[str], *, form: str = "8-K", pause: float = 0.15
) -> dict[str, list[dict]]:
    """``{ticker: [{accepted, items}, ...]}`` from EDGAR. Never raises.

    ``accepted`` is the acceptance timestamp in UTC, to the second. That
    precision matters: a filing accepted after 21:00 UTC is after the US
    close, so the session that reacts to it is the next one, and getting
    that boundary wrong misattributes every after-hours disclosure by a day.

    A ticker EDGAR has nothing for is **omitted rather than recorded
    empty**, so a lookup failure stays distinguishable from a company that
    genuinely has not filed.
    """
    try:
        ciks = _cik_map()
    except Exception as error:  # noqa: BLE001 - no map is not an outage
        logger.warning("Could not read the SEC ticker map: %s", error)
        return {}

    found: dict[str, list[dict]] = {}
    for ticker in tickers:
        symbol = str(ticker).strip().upper()
        cik = ciks.get(symbol)
        if not cik:
            # Indices, commodities and FX have no CIK. Not a failure.
            continue
        try:
            raw = json.loads(_get(f"https://data.sec.gov/submissions/CIK{cik}.json"))
            recent = raw["filings"]["recent"]
        except Exception as error:  # noqa: BLE001
            logger.warning("No filings for %s: %s", symbol, error)
            continue

        items = recent.get("items") or [""] * len(recent.get("form", []))
        rows = [
            {"accepted": str(accepted), "items": str(codes or "")}
            for kind, accepted, codes in zip(recent["form"], recent["acceptanceDateTime"], items)
            if str(kind).startswith(form)
        ]
        if rows:
            found[symbol] = rows
        time.sleep(pause)
    return found


def store_filings(
    filings: Mapping[str, Iterable[Mapping[str, Any]]],
    config=None,
    account: str | None = None,
) -> bool:
    """Rewrite the stored filings. Never raises.

    Rewritten rather than appended: EDGAR restates and amends, so the file
    says what the record shows *now*. Same reasoning as the account
    registry and the earnings dates.
    """
    from cfdinvest.persist.storage import atomic_write_text

    path = _path(config, account)
    document = {
        "schema_version": SCHEMA_VERSION,
        "filings": {
            str(ticker): [dict(row) for row in rows]
            for ticker, rows in (filings or {}).items()
        },
    }
    try:
        atomic_write_text(json.dumps(document, indent=1, sort_keys=True), path)
        return True
    except Exception as error:  # noqa: BLE001
        logger.warning("Could not write filings to %s: %s", path, error)
        return False


def load_filings(config=None, account: str | None = None) -> dict[str, list[dict]]:
    """What was stored, or an empty mapping. Never raises."""
    path = _path(config, account)
    try:
        if not path.is_file():
            logger.debug("No filings recorded at %s", path)
            return {}
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        logger.warning("Filings unreadable at %s: %s", path, error)
        return {}

    stored = document.get("filings") if isinstance(document, Mapping) else None
    if not isinstance(stored, Mapping):
        return {}
    return {
        str(ticker): [dict(row) for row in (rows or [])]
        for ticker, rows in stored.items()
    }


def filing_rate(
    filings: Mapping[str, Iterable[Mapping[str, Any]]],
    as_of=None,
    *,
    window_days: int = 90,
    substantive_only: bool = True,
) -> dict[str, int]:
    """``{ticker: filings in the window ending at as_of}``. Pure.

    Ninety days because that is what was measured: filings in the previous
    ninety days against the next month's volatility, r=+0.124 at t=+5.13
    over 1,691 ticker-months.

    ``substantive_only`` drops filings carrying nothing but exhibits, a
    shareholder vote or a bylaw amendment. Those moved 1.64% against 2.45%
    for the rest, so counting them dilutes the very thing being counted.

    Strictly ``<= as_of``: a rate that included today's filing would be
    using an event to predict the session it lands in.
    """
    import pandas as pd

    if as_of is None:
        as_of = pd.Timestamp.now(tz="UTC")
    else:
        as_of = pd.Timestamp(as_of)
        if as_of.tzinfo is None:
            as_of = as_of.tz_localize("UTC")
    start = as_of - pd.Timedelta(days=window_days)

    counts: dict[str, int] = {}
    for ticker, rows in (filings or {}).items():
        total = 0
        for row in rows or ():
            try:
                when = pd.Timestamp(row.get("accepted"))
            except (TypeError, ValueError):
                continue
            if when.tzinfo is None:
                when = when.tz_localize("UTC")
            if not (start < when <= as_of):
                continue
            if substantive_only:
                codes = {
                    c.strip() for c in str(row.get("items") or "").split(",")
                    if c.strip()
                }
                if codes and not (codes - FORMAL_ITEMS):
                    continue
            total += 1
        counts[str(ticker)] = total
    return counts


def _path(config=None, account: str | None = None) -> Path:
    from cfdinvest.persist.storage import account_root

    return account_root(config, account) / FILINGS_DIR / "filings.json"
