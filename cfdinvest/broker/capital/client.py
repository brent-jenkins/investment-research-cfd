"""Reading Capital.com. Sessions, instrument search, and live prices.

**Read-only by construction.** There is no function here that places,
amends or cancels anything -- not one that is disabled, one that does not
exist. A test asserts the dealing endpoints appear nowhere in this source,
that the only ``POST`` is the session, and that no ``PUT`` or ``DELETE``
appears at all.

The point is measurement. Twenty instruments charge an *estimated* spread --
one per-class constant of 4bp round trip, 79% of all modelled cost, against
a break-even near 6bp. Capital.com returns bid and offer on the market
details endpoint, so that constant can be replaced with twenty measured
numbers without dealing at all. IG could not: it declined to quote share
prices to the account, so the spread there had to be discovered by placing
orders and reading the fills.

Authentication, and the two things about it worth knowing
--------------------------------------------------------
``POST /session`` takes ``identifier`` and ``password`` with the API key in
an ``X-CAP-API-KEY`` header, and returns two tokens in *response headers*:
``CST`` and ``X-SECURITY-TOKEN``. Both are needed on every later call, and
they expire ten minutes after last use rather than ten minutes after issue.

The API key is generated per account in Settings, and generating it sets a
custom password which is **not** the login password. Demo and live are
separate accounts with separate keys and separate base URLs, which is a
better arrangement than one key that can reach either.

Demo unless ``live`` is spelled exactly, including on a typo. A misspelt
environment silently selecting a funded account is the worst possible way to
learn about a typo.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "CapitalConfig",
    "CapitalSession",
    "CapitalMarket",
    "open_session",
    "find_epic",
    "fetch_market",
    "fetch_markets",
]

#: Separate hosts, separate accounts, separate keys. Nothing here can reach
#: the live host without ``CFD_CAPITAL_ENV`` spelling out ``live``.
DEMO_HOST = "https://demo-api-capital.backend-capital.com/api/v1"
LIVE_HOST = "https://api-capital.backend-capital.com/api/v1"

KEY_VAR = "CFD_CAPITAL_API_KEY"
IDENTIFIER_VAR = "CFD_CAPITAL_IDENTIFIER"
PASSWORD_VAR = "CFD_CAPITAL_PASSWORD"
ENVIRONMENT_VAR = "CFD_CAPITAL_ENV"

#: Documented as ten per second overall and one per second for the session.
#: Respected by pausing between calls rather than by retrying after a 429:
#: a rate limiter that only reacts to being refused has already been rude.
REQUEST_INTERVAL_SECONDS = 0.2


@dataclass(frozen=True, slots=True)
class CapitalConfig:
    """Credentials and which account they reach.

    The secrets carry ``repr=False``. These objects reach debug output and
    exception context, and a broker key is not a FRED key -- the blast
    radius is a funded account rather than a rate limit.
    """

    api_key: str | None = field(default=None, repr=False)
    identifier: str | None = field(default=None, repr=False)
    password: str | None = field(default=None, repr=False)
    environment: str = "demo"

    @property
    def host(self) -> str:
        return LIVE_HOST if self.is_live else DEMO_HOST

    @property
    def is_live(self) -> bool:
        return self.environment == "live"

    @property
    def complete(self) -> bool:
        return bool(self.api_key and self.identifier and self.password)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "CapitalConfig":
        """Read the environment. Demo unless ``live`` is spelled out."""
        import os

        source = os.environ if env is None else env
        wanted = str(source.get(ENVIRONMENT_VAR, "demo") or "demo").strip().lower()
        return cls(
            api_key=source.get(KEY_VAR) or None,
            identifier=source.get(IDENTIFIER_VAR) or None,
            password=source.get(PASSWORD_VAR) or None,
            environment="live" if wanted == "live" else "demo",
        )


@dataclass(frozen=True, slots=True)
class CapitalSession:
    """An authenticated session, or the reason there is not one."""

    cst: str | None = field(default=None, repr=False)
    security_token: str | None = field(default=None, repr=False)
    host: str = DEMO_HOST
    account_id: str = ""
    currency: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.cst and self.security_token)

    def headers(self, api_key: str | None) -> dict[str, str]:
        """Every authenticated call needs both tokens and the key."""
        return {
            "X-CAP-API-KEY": api_key or "",
            "CST": self.cst or "",
            "X-SECURITY-TOKEN": self.security_token or "",
            "Content-Type": "application/json",
        }


@dataclass(frozen=True, slots=True)
class CapitalAccount:
    """What the account holds, as the broker reports it.

    ``balance`` and ``available`` are different questions and both are
    needed. The leverage cap is a statement about equity, so it reads the
    balance; whether a position can actually be opened right now depends on
    what is left after the margin already committed, which is ``available``.
    Using one for the other silently permits a trade the account cannot
    fund, or refuses one it can.
    """

    account_id: str = ""
    currency: str = ""
    balance: float | None = None
    available: float | None = None
    profit_loss: float | None = None
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and self.balance is not None


@dataclass(frozen=True, slots=True)
class CapitalMarket:
    """One instrument's prices and dealing rules, as read."""

    epic: str
    ticker: str = ""
    name: str = ""
    bid: float | None = None
    offer: float | None = None
    spread_points: float | None = None
    spread_pct: float | None = None
    currency: str = ""
    min_deal_size: float | None = None
    instrument_type: str = ""
    tradable: bool | None = None
    fetched_at: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and self.spread_pct is not None

    def as_row(self) -> dict[str, Any]:
        from dataclasses import asdict

        return asdict(self)


def _read(url: str, *, data=None, headers=None, method: str | None = None):
    """One request. Returns ``(document, response headers)``.

    The headers matter here in a way they did not for IG's price calls: the
    session tokens arrive as headers rather than in the body, so a reader
    that returned only the parsed document could not authenticate at all.
    """
    request = urllib.request.Request(
        url, data=data, headers=headers or {}, method=method
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read().decode("utf-8") or "{}"
        return json.loads(body), dict(response.headers)


def open_session(
    config: CapitalConfig | None = None, *, opener=None
) -> CapitalSession:
    """Authenticate. Never raises; a failure comes back as a reason.

    Reports which variables are missing by name, and never their values --
    "the key is wrong" and "the key is absent" are different problems and
    only one of them is fixed by looking at the environment file.
    """
    settings = config or CapitalConfig.from_env()
    if not settings.complete:
        missing = [
            name for name, value in (
                (KEY_VAR, settings.api_key),
                (IDENTIFIER_VAR, settings.identifier),
                (PASSWORD_VAR, settings.password),
            ) if not value
        ]
        return CapitalSession(
            host=settings.host,
            error=f"not configured: {', '.join(missing)} unset",
        )

    read = opener or _read
    payload = json.dumps(
        {"identifier": settings.identifier, "password": settings.password}
    ).encode("utf-8")
    try:
        document, headers = read(
            f"{settings.host}/session",
            data=payload,
            headers={
                "X-CAP-API-KEY": settings.api_key,
                "Content-Type": "application/json",
            },
            method="POST",
        )
    except urllib.error.HTTPError as error:
        # The body carries an errorCode worth surfacing -- "error.invalid.details"
        # and a 429 are different problems with the same status class.
        detail = ""
        try:
            detail = json.loads(error.read().decode("utf-8")).get("errorCode", "")
        except Exception:  # noqa: BLE001 - the status is the useful part
            detail = ""
        return CapitalSession(
            host=settings.host,
            error=f"HTTP {error.code}{f' ({detail})' if detail else ''}",
        )
    except Exception as error:  # noqa: BLE001
        return CapitalSession(
            host=settings.host, error=f"{type(error).__name__}: {error}"
        )

    # Header names arrive with inconsistent casing across proxies.
    lowered = {str(k).lower(): v for k, v in (headers or {}).items()}
    account = (document or {}).get("currentAccountId") or ""
    accounts = (document or {}).get("accounts") or []
    currency = ""
    for entry in accounts:
        if str(entry.get("accountId") or "") == str(account):
            currency = str(entry.get("currency") or "")
            break

    session = CapitalSession(
        cst=lowered.get("cst"),
        security_token=lowered.get("x-security-token"),
        host=settings.host,
        account_id=str(account),
        currency=currency,
    )
    if not session.ok:
        return CapitalSession(
            host=settings.host,
            error="authenticated but no CST / X-SECURITY-TOKEN in the response",
        )
    return session


def fetch_account(
    session: CapitalSession,
    config: CapitalConfig | None = None,
    *,
    opener=None,
) -> CapitalAccount:
    """The preferred account's balance. Never raises.

    An unreadable balance comes back absent rather than as a number, because
    every size downstream is a fraction of it -- a default here would size
    real positions against an invented account.
    """
    settings = config or CapitalConfig.from_env()
    if not session.ok:
        return CapitalAccount(error=session.error or "no session")

    read = opener or _read
    try:
        document, _ = read(
            f"{session.host}/accounts", headers=session.headers(settings.api_key)
        )
    except urllib.error.HTTPError as error:
        return CapitalAccount(error=f"HTTP {error.code}")
    except Exception as error:  # noqa: BLE001
        return CapitalAccount(error=f"{type(error).__name__}: {error}")

    accounts = (document or {}).get("accounts") or []
    if not accounts:
        return CapitalAccount(error="no accounts in the response")

    chosen = next(
        (a for a in accounts if str(a.get("accountId") or "") == session.account_id),
        None,
    )
    if chosen is None:
        chosen = next((a for a in accounts if a.get("preferred")), accounts[0])

    balance = (chosen.get("balance") or {})
    return CapitalAccount(
        account_id=str(chosen.get("accountId") or ""),
        currency=str(chosen.get("currency") or ""),
        balance=_number(balance.get("balance")),
        available=_number(balance.get("available")),
        profit_loss=_number(balance.get("profitLoss")),
    )


def _number(value) -> float | None:
    try:
        if value is None:
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # NaN is not a number here


def find_epic(
    ticker: str,
    session: CapitalSession,
    config: CapitalConfig | None = None,
    *,
    opener=None,
) -> tuple[str, str]:
    """Resolve a ticker to an epic. Returns ``(epic, error)``.

    Search rather than a hardcoded map, because a map of twenty epics would
    be twenty more facts to keep true. The first exact symbol match wins; a
    fuzzy match is refused rather than guessed at, since dealing on the
    wrong instrument because a search was approximately right is a failure
    with no upper bound.
    """
    settings = config or CapitalConfig.from_env()
    if not session.ok:
        return "", session.error or "no session"

    read = opener or _read
    query = urllib.parse.urlencode({"searchTerm": str(ticker)})
    try:
        document, _ = read(
            f"{session.host}/markets?{query}",
            headers=session.headers(settings.api_key),
        )
    except urllib.error.HTTPError as error:
        return "", f"HTTP {error.code}"
    except Exception as error:  # noqa: BLE001
        return "", f"{type(error).__name__}: {error}"

    wanted = str(ticker).strip().upper()
    for market in (document or {}).get("markets") or []:
        if str(market.get("symbol") or "").strip().upper() == wanted:
            return str(market.get("epic") or ""), ""
    return "", f"no exact symbol match for {ticker}"


def fetch_market(
    epic: str,
    session: CapitalSession,
    config: CapitalConfig | None = None,
    *,
    ticker: str = "",
    opener=None,
    now=None,
) -> CapitalMarket:
    """One instrument's prices and dealing rules. Never raises.

    The spread comes back in points and as a percentage of the mid. The
    percentage is what the cost model wants: points are not comparable
    across a $40 share and a $400 one, and comparing them is exactly the
    mistake a single per-class constant was already making.
    """
    settings = config or CapitalConfig.from_env()
    if not session.ok:
        return CapitalMarket(
            epic=epic, ticker=ticker, error=session.error or "no session"
        )

    read = opener or _read
    url = f"{session.host}/markets/{urllib.parse.quote(str(epic), safe='')}"
    try:
        document, _ = read(url, headers=session.headers(settings.api_key))
    except urllib.error.HTTPError as error:
        return CapitalMarket(epic=epic, ticker=ticker, error=f"HTTP {error.code}")
    except Exception as error:  # noqa: BLE001
        return CapitalMarket(
            epic=epic, ticker=ticker, error=f"{type(error).__name__}: {error}"
        )

    snapshot = (document or {}).get("snapshot") or {}
    instrument = (document or {}).get("instrument") or {}
    rules = (document or {}).get("dealingRules") or {}

    bid = _number(snapshot.get("bid"))
    # Capital.com spells the ask both ways depending on endpoint.
    offer = _number(snapshot.get("offer"))
    if offer is None:
        offer = _number(snapshot.get("ofr"))

    spread_points = spread_pct = None
    price_error = ""
    if bid is not None and offer is not None and bid > 0 and offer > 0:
        spread_points = offer - bid
        mid = (offer + bid) / 2.0
        spread_pct = spread_points / mid * 100.0 if mid > 0 else None
    else:
        # Named, not left blank. The IG version returned an empty error
        # string for this case, so nineteen instruments reported "error:"
        # with nothing after it and the reason had to be found by hand.
        status = str(snapshot.get("marketStatus") or "unknown")
        price_error = f"no dealing prices (market {status.lower()})"

    minimum = (rules.get("minDealSize") or {}).get("value")

    return CapitalMarket(
        epic=str(epic),
        ticker=ticker,
        name=str(instrument.get("name") or ""),
        bid=bid,
        offer=offer,
        spread_points=spread_points,
        spread_pct=spread_pct,
        currency=str(instrument.get("currency") or ""),
        min_deal_size=_number(minimum),
        instrument_type=str(instrument.get("type") or ""),
        tradable=(
            None if snapshot.get("marketStatus") is None
            else str(snapshot.get("marketStatus")) == "TRADEABLE"
        ),
        fetched_at=str(now or ""),
        error=price_error,
    )


def fetch_markets(
    tickers: Sequence[str],
    session: CapitalSession,
    config: CapitalConfig | None = None,
    *,
    opener=None,
    now=None,
    sleep=None,
) -> list[CapitalMarket]:
    """Every instrument, one row each, in order.

    An instrument that failed is *kept*, carrying its reason. Dropping it
    would turn a partial reading into one that looks complete, and the
    interesting cases here are exactly the ones that fail -- an instrument
    the broker does not offer is a finding, not a gap in a table.

    Paced at the documented ten requests a second. Two calls per instrument,
    the search and the details, so twenty names is forty requests.
    """
    import time

    pause = sleep if sleep is not None else time.sleep
    rows: list[CapitalMarket] = []
    for ticker in tickers:
        epic, error = find_epic(ticker, session, config, opener=opener)
        if not epic:
            rows.append(CapitalMarket(epic="", ticker=str(ticker), error=error))
            pause(REQUEST_INTERVAL_SECONDS)
            continue
        pause(REQUEST_INTERVAL_SECONDS)
        rows.append(
            fetch_market(
                epic, session, config, ticker=str(ticker), opener=opener, now=now
            )
        )
        pause(REQUEST_INTERVAL_SECONDS)
    return rows
