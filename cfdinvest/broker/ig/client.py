"""A read-only IG client: sessions, instruments and their real costs.

Why this exists at all, given the project spent its life refusing to talk to
a broker: every one of the nineteen instruments currently charges an
*estimated* spread. Not one has a measured figure. It is a single per-class
constant -- 4bp round trip -- applied to every name, and that constant is
**79% of all modelled cost**, sitting against a measured break-even of about
4bp of further slippage. The largest number in the cost model is a guess,
and this is how it stops being one.

Trading 212 remains unautomated and unautomatable: no CFD API exists, so any
automation would mean driving the web UI. IG has a real API and a demo
environment, which is where this points.

**Read-only.** Nothing in this module places, amends or cancels an order.
There is no code path to. Order placement, if it is ever built, goes in a
module whose name says so, gated on an explicit setting -- a client that can
trade because a config drifted is the failure this project has spent its
whole life avoiding.

**Credentials come from the environment**, never from an argument with a
default and never from a file in the repository. A broker key is not a FRED
key: the blast radius is a funded account.

**Demo by default.** ``CFD_IG_ENVIRONMENT`` must say ``live`` explicitly, and
the resolved host is reported in every result so a caller can see which one
it reached rather than assuming.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Mapping

logger = logging.getLogger(__name__)

__all__ = [
    "IgConfig",
    "IgSession",
    "IgMarket",
    "open_session",
    "fetch_market",
    "fetch_markets",
]

#: The two hosts. Demo is the default and live has to be asked for by name.
HOSTS: dict[str, str] = {
    "demo": "https://demo-api.ig.com/gateway/deal",
    "live": "https://api.ig.com/gateway/deal",
}

#: Environment variables. Named for this project, not for IG, so a machine
#: hosting several apps cannot cross them.
KEY_VAR = "CFD_IG_API_KEY"
USER_VAR = "CFD_IG_USERNAME"
PASSWORD_VAR = "CFD_IG_PASSWORD"
ENVIRONMENT_VAR = "CFD_IG_ENVIRONMENT"
ACCOUNT_VAR = "CFD_IG_ACCOUNT_ID"

TIMEOUT = 20.0


@dataclass(frozen=True, slots=True)
class IgConfig:
    """Where to connect and as whom. Never carries a secret in its repr."""

    api_key: str | None = field(default=None, repr=False)
    username: str | None = None
    password: str | None = field(default=None, repr=False)
    environment: str = "demo"
    account_id: str | None = None

    @property
    def host(self) -> str:
        return HOSTS.get(self.environment, HOSTS["demo"])

    @property
    def is_live(self) -> bool:
        return self.environment == "live"

    @property
    def complete(self) -> bool:
        return bool(self.api_key and self.username and self.password)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "IgConfig":
        """Read the environment. Demo unless ``live`` is spelled out.

        Anything other than the exact string ``live`` resolves to demo,
        including typos. A misspelt environment reaching a funded account
        would be the worst possible way to learn about a typo.
        """
        import os

        source = os.environ if env is None else env
        wanted = str(source.get(ENVIRONMENT_VAR, "demo") or "demo").strip().lower()
        return cls(
            api_key=source.get(KEY_VAR) or None,
            username=source.get(USER_VAR) or None,
            password=source.get(PASSWORD_VAR) or None,
            environment="live" if wanted == "live" else "demo",
            account_id=source.get(ACCOUNT_VAR) or None,
        )


@dataclass(frozen=True, slots=True)
class IgSession:
    """An authenticated session, or the reason there is not one."""

    host: str = ""
    environment: str = "demo"
    account_id: str | None = None
    cst: str | None = field(default=None, repr=False)
    security_token: str | None = field(default=None, repr=False)
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.cst and self.security_token)

    def headers(self, api_key: str | None, version: str = "1") -> dict[str, str]:
        head = {
            "X-IG-API-KEY": api_key or "",
            "Content-Type": "application/json; charset=UTF-8",
            "Accept": "application/json; charset=UTF-8",
            "Version": version,
        }
        if self.cst:
            head["CST"] = self.cst
        if self.security_token:
            head["X-SECURITY-TOKEN"] = self.security_token
        return head


@dataclass(frozen=True, slots=True)
class IgMarket:
    """One instrument as IG describes it, with the numbers that cost money.

    ``spread_points`` and ``spread_pct`` are the reason this class exists.
    Everything else is context for them.
    """

    epic: str
    ticker: str = ""
    name: str = ""
    bid: float | None = None
    offer: float | None = None
    spread_points: float | None = None
    spread_pct: float | None = None
    currency: str = ""
    min_deal_size: float | None = None
    margin_factor: float | None = None
    #: IG's own overnight funding, per side, as a percentage.
    funding_long: float | None = None
    funding_short: float | None = None
    tradable: bool | None = None
    fetched_at: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.error == "" and self.spread_pct is not None

    def as_row(self) -> dict[str, Any]:
        from dataclasses import asdict

        return asdict(self)


def _read(url: str, *, data=None, headers=None, method: str | None = None):
    """One HTTP round trip returning a parsed document. Raises on failure."""
    body = None if data is None else json.dumps(data).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers=dict(headers or {}), method=method
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        payload = response.read().decode("utf-8")
        parsed = json.loads(payload) if payload.strip() else {}
        return parsed, dict(response.headers)


def open_session(config: IgConfig | None = None, *, opener=None) -> IgSession:
    """Authenticate. Never raises; a failure comes back as a reason.

    ``opener`` exists so the tests can drive this without a network, the
    same seam the quote source uses.
    """
    settings = config or IgConfig.from_env()
    if not settings.complete:
        missing = [
            name for name, value in (
                (KEY_VAR, settings.api_key),
                (USER_VAR, settings.username),
                (PASSWORD_VAR, settings.password),
            ) if not value
        ]
        return IgSession(
            host=settings.host, environment=settings.environment,
            error=f"not configured: {', '.join(missing)} unset",
        )

    read = opener or _read
    try:
        document, headers = read(
            f"{settings.host}/session",
            data={"identifier": settings.username, "password": settings.password},
            headers={
                "X-IG-API-KEY": settings.api_key,
                "Content-Type": "application/json; charset=UTF-8",
                "Accept": "application/json; charset=UTF-8",
                "Version": "2",
            },
            method="POST",
        )
    except urllib.error.HTTPError as error:
        # Deliberately not the body: IG echoes detail that is not worth
        # putting in a log next to a username.
        return IgSession(
            host=settings.host, environment=settings.environment,
            error=f"HTTP {error.code} authenticating",
        )
    except Exception as error:  # noqa: BLE001
        return IgSession(
            host=settings.host, environment=settings.environment,
            error=f"{type(error).__name__}: {error}",
        )

    cst = headers.get("CST")
    token = headers.get("X-SECURITY-TOKEN")
    if not cst or not token:
        return IgSession(
            host=settings.host, environment=settings.environment,
            error="the session returned no tokens",
        )

    account = settings.account_id or (document or {}).get("currentAccountId")
    return IgSession(
        host=settings.host, environment=settings.environment,
        account_id=account, cst=cst, security_token=token,
    )


def _number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def fetch_market(
    epic: str,
    session: IgSession,
    config: IgConfig | None = None,
    *,
    ticker: str = "",
    opener=None,
    now=None,
) -> IgMarket:
    """One instrument's live prices and costs. Never raises.

    The spread is returned both in points and as a percentage of the mid.
    The percentage is what the cost model wants -- points are not comparable
    across a $40 share and a $400 one, and comparing them is exactly the
    mistake that a per-class constant was already making.
    """
    settings = config or IgConfig.from_env()
    if not session.ok:
        return IgMarket(epic=epic, ticker=ticker, error=session.error or "no session")

    read = opener or _read
    url = f"{session.host}/markets/{urllib.parse.quote(str(epic), safe='')}"
    try:
        document, _ = read(url, headers=session.headers(settings.api_key, "3"))
    except urllib.error.HTTPError as error:
        return IgMarket(epic=epic, ticker=ticker, error=f"HTTP {error.code}")
    except Exception as error:  # noqa: BLE001
        return IgMarket(
            epic=epic, ticker=ticker, error=f"{type(error).__name__}: {error}"
        )

    snapshot = (document or {}).get("snapshot") or {}
    instrument = (document or {}).get("instrument") or {}
    rules = (document or {}).get("dealingRules") or {}

    bid, offer = _number(snapshot.get("bid")), _number(snapshot.get("offer"))
    spread_points = spread_pct = None
    price_error = ""
    if bid is not None and offer is not None and offer > 0 and bid > 0:
        spread_points = offer - bid
        mid = (offer + bid) / 2.0
        spread_pct = spread_points / mid * 100.0 if mid > 0 else None
    else:
        # A tradeable market with no dealing prices is a market-data
        # entitlement, not a fault -- IG returns the instrument's metadata,
        # its high and low and its margin factor, and simply omits bid and
        # offer. The first version of this returned an empty error string
        # for that case, so nineteen instruments reported "error:" with
        # nothing after it and the reason had to be found by hand.
        status = str(snapshot.get("marketStatus") or "unknown")
        price_error = (
            f"no dealing prices (market {status.lower()}); this account may "
            "lack the market-data entitlement for this instrument type"
        )

    minimum = (rules.get("minDealSize") or {}).get("value")

    # IG expresses overnight funding per side under different keys across
    # instrument types; absent is left absent rather than guessed at.
    funding = instrument.get("overnightFee") or {}
    return IgMarket(
        epic=str(epic), ticker=ticker,
        name=str(instrument.get("name") or ""),
        bid=bid, offer=offer,
        spread_points=spread_points, spread_pct=spread_pct,
        currency=str(
            (instrument.get("currencies") or [{}])[0].get("code", "")
            if instrument.get("currencies") else ""
        ),
        min_deal_size=_number(minimum),
        margin_factor=_number(instrument.get("marginFactor")),
        funding_long=_number(funding.get("longRate")),
        funding_short=_number(funding.get("shortRate")),
        tradable=(
            None if snapshot.get("marketStatus") is None
            else str(snapshot.get("marketStatus")) == "TRADEABLE"
        ),
        fetched_at=str(now or ""),
        error=price_error,
    )


def fetch_markets(
    epics: Mapping[str, str],
    session: IgSession,
    config: IgConfig | None = None,
    *,
    opener=None,
    now=None,
) -> list[IgMarket]:
    """Every instrument in ``{ticker: epic}``. One failure is one row.

    A name that could not be read comes back carrying its reason rather than
    being dropped, because a spread table with a silent hole in it would
    fall back to the per-class constant for that name without saying so.
    """
    out: list[IgMarket] = []
    for ticker, epic in sorted((epics or {}).items()):
        out.append(
            fetch_market(
                epic, session, config, ticker=str(ticker), opener=opener, now=now
            )
        )
    return out
