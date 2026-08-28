"""Form 4 insider transactions, and the only directional signal found so far.

Every price-derived feature this project has tested has come out null --
once against 2,018 historical round trips, again on 2026-08-28 against
1,159. Confidence, trend strength, volatility, spread, order-flow proxies
and 8-K filings all separate winners from losers at effectively zero. The
8-K work established that filings predict *magnitude* (2.43x the usual move
after a report) and nothing whatever about direction.

Form 4 is different in kind rather than in degree. An officer or director
buying their own company's stock on the open market is a person with
information committing their own money, and saying so within two business
days. It is directional by construction, which no price feature is.

What was measured (2026-08-28, 47 instruments, 2023-01 to 2026-03)
------------------------------------------------------------------
Market-adjusted return over the session after the filing becomes public:

    insider purchases   n=105   +1.04%   t=3.42   p=0.0009
    insider sales     n=3,518   -0.00%   nothing at all

The asymmetry is the point and was predicted in advance: a purchase has one
plausible motive, a sale has many -- diversification, a tax bill, a house, a
scheduled 10b5-1 plan. Robust to outliers (median +0.51%, 59% positive,
Wilcoxon p=0.0024) and larger purchases move more (+0.33/+1.00/+1.80 by
tercile). Same sign in both halves of the span, but only significant in the
second, so it is one and a bit halves of evidence rather than two.

**Rare.** 228 purchases against 20,508 sales -- this universe is mega-cap
technology, where insiders are paid in stock, sell constantly and almost
never buy. That is roughly 32 usable events a year across the whole pool,
under one per instrument per year. It is recorded rather than traded for
exactly that reason: the only way to earn the sample size that would
justify sizing on it is to start collecting.

Transaction codes carry everything
----------------------------------
Most Form 4 activity is payroll and says nothing: ``A`` a grant, ``M`` an
option exercise, ``F`` shares withheld for tax, ``G`` a gift. Only ``P``, an
open-market purchase, and ``S``, an open-market sale, are decisions to take
a position at the prevailing price. Pooling every code drowns the signal in
compensation and is the commonest way this analysis is got wrong.

Point in time
-------------
Keyed on the **filing** date and never the transaction date. Insiders have
two business days to report; using the transaction date would trade on
information that was not yet public, and is what makes a naive version of
this study look far better than it is.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.request
from pathlib import Path
from typing import Iterable, Mapping

logger = logging.getLogger(__name__)

__all__ = [
    "MEANINGFUL_CODES",
    "SCHEMA_VERSION",
    "fetch_insiders",
    "store_insiders",
    "load_insiders",
    "insider_flow",
]

#: SEC requires a real contact address and permits ten requests a second.
USER_AGENT = "cfdinvest research <contact>"

#: Open-market decisions. Everything else is compensation machinery: ``A``
#: grant, ``M`` option exercise, ``F`` tax withholding, ``G`` gift.
MEANINGFUL_CODES = frozenset({"P", "S"})

SCHEMA_VERSION = 1

INSIDER_DIR = "insiders"

#: Enough to compute a trailing signal without re-reading years of history
#: on every pass. The signal decays within a session or two.
DEFAULT_LOOKBACK_DAYS = 90


def _get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(request, timeout=30).read()


def _cik_map() -> dict[str, str]:
    raw = json.loads(_get("https://www.sec.gov/files/company_tickers.json"))
    return {
        str(entry["ticker"]).upper(): str(entry["cik_str"]).zfill(10)
        for entry in raw.values()
    }


def _transactions(xml: str) -> list[dict]:
    """Pull the non-derivative transactions out of a Form 4 document.

    Parsed with expressions rather than an XML parser because the documents
    are small, the shape is fixed by the SEC's own schema, and a malformed
    filing must yield nothing rather than raise -- one bad document cannot
    be allowed to stop a daily pass.
    """
    rows: list[dict] = []
    for block in re.findall(
        r"<nonDerivativeTransaction>(.*?)</nonDerivativeTransaction>", xml, re.S
    ):
        def field(tag: str) -> str:
            match = re.search(rf"<{tag}>\s*<value>(.*?)</value>", block, re.S)
            if match:
                return match.group(1).strip()
            match = re.search(rf"<{tag}>(.*?)</{tag}>", block, re.S)
            return match.group(1).strip() if match else ""

        code = field("transactionCode")
        if code not in MEANINGFUL_CODES:
            continue
        try:
            shares = float(field("transactionShares") or 0.0)
        except ValueError:
            continue
        try:
            price = float(field("transactionPricePerShare") or 0.0)
        except ValueError:
            price = 0.0
        rows.append({
            "code": code,
            "shares": shares,
            "price": price,
            "transacted": field("transactionDate"),
        })
    return rows


def fetch_insiders(
    tickers: Iterable[str],
    *,
    since: str | None = None,
    pause: float = 0.15,
    max_documents: int = 400,
) -> dict[str, list[dict]]:
    """``{ticker: [{filed, code, shares, price}, ...]}``. Never raises.

    ``since`` is an ISO date; filings on or before it are skipped, which is
    what makes a daily pass incremental -- a handful of new documents rather
    than the thirty-five thousand in the archive.

    ``max_documents`` bounds a single pass. A first run against years of
    history would otherwise take an hour; the cap is reported by the caller
    rather than silently truncating, and the next pass continues.

    A ticker EDGAR has nothing for is **omitted rather than recorded
    empty**, so a lookup failure stays distinguishable from a company whose
    insiders simply have not dealt.
    """
    try:
        ciks = _cik_map()
    except Exception as error:  # noqa: BLE001 - no map is not an outage
        logger.warning("Could not read the SEC ticker map: %s", error)
        return {}

    found: dict[str, list[dict]] = {}
    budget = int(max_documents)
    for ticker in tickers:
        symbol = str(ticker).strip().upper()
        cik = ciks.get(symbol)
        if not cik:
            continue                      # indices and FX have no CIK
        try:
            raw = json.loads(_get(f"https://data.sec.gov/submissions/CIK{cik}.json"))
            recent = raw["filings"]["recent"]
        except Exception as error:  # noqa: BLE001
            logger.warning("No submissions for %s: %s", symbol, error)
            continue
        time.sleep(pause)

        rows: list[dict] = []
        for form, accession, filed, document in zip(
            recent.get("form", []), recent.get("accessionNumber", []),
            recent.get("filingDate", []), recent.get("primaryDocument", []),
        ):
            if str(form) != "4" or budget <= 0:
                continue
            if since and str(filed) <= str(since):
                continue
            # The primary document is the XSL-rendered view; the machine
            # readable filing sits beside it without that prefix.
            name = str(document).split("/")[-1]
            url = (
                f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
                f"{str(accession).replace('-', '')}/{name}"
            )
            try:
                xml = _get(url).decode("utf-8", "replace")
            except Exception as error:  # noqa: BLE001 - one bad document
                logger.debug("Form 4 unreadable at %s: %s", url, error)
                continue
            budget -= 1
            time.sleep(pause)
            for entry in _transactions(xml):
                rows.append({"filed": str(filed), **entry})

        if rows:
            found[symbol] = rows
    if budget <= 0:
        logger.warning(
            "Insider fetch hit its document budget; the next pass continues"
        )
    return found


def _path(config=None, account: str | None = None) -> Path:
    from cfdinvest.persist.storage import account_root

    return Path(account_root(config, account)) / INSIDER_DIR / "transactions.json"


def store_insiders(
    rows: Mapping[str, list[dict]], config=None, account: str | None = None
) -> int:
    """Merge into what is stored and rewrite. Returns rows written.

    Merged rather than appended: EDGAR amends filings, and a rewrite of the
    union keeps one row per transaction. Deduplicated on the filing date,
    code, share count and price together, because an insider genuinely can
    make two identical trades on one day and dropping the second would
    understate the position.
    """
    path = _path(config, account)
    existing: dict[str, list[dict]] = {}
    if path.is_file():
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            existing = document.get("transactions", {}) or {}
        except (OSError, json.JSONDecodeError) as error:
            logger.warning("Insider store unreadable at %s: %s", path, error)

    merged = dict(existing)
    for ticker, incoming in (rows or {}).items():
        seen = {
            (r.get("filed"), r.get("code"), r.get("shares"), r.get("price"))
            for r in merged.get(ticker, [])
        }
        combined = list(merged.get(ticker, []))
        for row in incoming:
            key = (row.get("filed"), row.get("code"), row.get("shares"),
                   row.get("price"))
            if key not in seen:
                seen.add(key)
                combined.append(row)
        merged[ticker] = sorted(combined, key=lambda r: str(r.get("filed") or ""))

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"schema_version": SCHEMA_VERSION, "transactions": merged},
            indent=1, sort_keys=True,
        ),
        encoding="utf-8",
    )
    return sum(len(v) for v in merged.values())


def load_insiders(config=None, account: str | None = None) -> dict[str, list[dict]]:
    """What was stored, or an empty mapping. Never raises."""
    path = _path(config, account)
    try:
        if not path.is_file():
            logger.debug("No insider transactions recorded at %s", path)
            return {}
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        logger.warning("Insider transactions unreadable at %s: %s", path, error)
        return {}
    rows = document.get("transactions") if isinstance(document, Mapping) else None
    return rows if isinstance(rows, Mapping) else {}


def insider_flow(
    rows: Mapping[str, list[dict]],
    now=None,
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> dict[str, dict]:
    """Net open-market insider activity per instrument, as of ``now``.

    Purchases and sales are reported **separately as well as netted**,
    because they are not opposite signs of one quantity: buying measured
    +1.04% over the following session and selling measured nothing. A caller
    that only reads the net would dilute the half that works with the half
    that does not.

    Nothing after ``now`` is included, and the filing date is what counts --
    a transaction is not public until it is filed.
    """
    import pandas as pd

    cutoff = pd.Timestamp(now or pd.Timestamp.utcnow())
    if cutoff.tzinfo is None:
        cutoff = cutoff.tz_localize("UTC")
    horizon = cutoff - pd.Timedelta(days=int(lookback_days))

    out: dict[str, dict] = {}
    for ticker, entries in (rows or {}).items():
        bought = sold = 0.0
        buy_value = sell_value = 0.0
        buys = sells = 0
        for entry in entries or ():
            try:
                filed = pd.Timestamp(str(entry.get("filed")))
            except Exception:  # noqa: BLE001
                continue
            if filed.tzinfo is None:
                filed = filed.tz_localize("UTC")
            if not (horizon < filed <= cutoff):
                continue
            shares = float(entry.get("shares") or 0.0)
            price = float(entry.get("price") or 0.0)
            if str(entry.get("code")) == "P":
                bought += shares
                buy_value += shares * price
                buys += 1
            elif str(entry.get("code")) == "S":
                sold += shares
                sell_value += shares * price
                sells += 1
        if buys or sells:
            out[str(ticker)] = {
                "buys": buys, "sells": sells,
                "shares_bought": bought, "shares_sold": sold,
                "value_bought": round(buy_value, 2),
                "value_sold": round(sell_value, 2),
                "net_value": round(buy_value - sell_value, 2),
            }
    return out
