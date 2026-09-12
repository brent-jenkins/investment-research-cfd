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
import time
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

#: What the account started with, so "have we made money" can be answered.
#:
#: The broker cannot supply this. Capital reports `balance`, `deposit` (cash:
#: the opening deposit plus everything realised) and `profitLoss`
#: (unrealised, on open positions), and none of them is the opening figure --
#: inferring it from `deposit` is wrong by exactly the realised P&L, which is
#: the quantity being sought.
#:
#: It sits beside the credentials rather than in config.toml because it is a
#: property of the ACCOUNT those credentials reach. Pointing the system at a
#: live account changes the account, and its opening balance changes with it;
#: a figure in the shared config would quietly describe the wrong one.
#:
#: Optional, and absent means absent. With this unset the answer is reported
#: as unavailable rather than guessed, because a wrong lifetime P&L is worse
#: than none: it is the number a person uses to decide whether the thing
#: works.
OPENING_BALANCE_VAR = "CFD_CAPITAL_OPENING_BALANCE"

#: Documented as ten per second overall and one per second for the session.
#: Respected by pausing between calls rather than by retrying after a 429:
#: a rate limiter that only reacts to being refused has already been rude.
REQUEST_INTERVAL_SECONDS = 0.2


def _opening_balance(raw) -> float | None:
    """Parse the configured opening balance, or report it absent.

    A value that will not parse is treated as unset rather than as zero. An
    opening balance of zero would make every account look infinitely
    profitable, and "somebody typed it wrong" is not a reason to publish
    that. Non-positive values are refused for the same reason.
    """
    if raw is None or str(raw).strip() == "":
        return None
    try:
        value = float(str(raw).strip().replace(",", ""))
    except (TypeError, ValueError):
        logger.warning(
            "%s is not a number (%r); the opening balance is unset",
            OPENING_BALANCE_VAR, raw,
        )
        return None
    if value <= 0:
        logger.warning(
            "%s must be positive, got %r; the opening balance is unset",
            OPENING_BALANCE_VAR, raw,
        )
        return None
    return value


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
    #: What the account was funded with. ``None`` when unset -- see
    #: :data:`OPENING_BALANCE_VAR`. Not a secret, so not hidden from repr:
    #: it is the one field here somebody may need to see to debug a figure.
    opening_balance: float | None = None

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
            opening_balance=_opening_balance(source.get(OPENING_BALANCE_VAR)),
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
    #: **Unrealised** P&L on currently open positions, as the broker reports
    #: it. Not profit since the account opened, and it was displayed as that
    #: for a while: with a 5,000 account down to 4,890 the page read
    #: "+26.12 since the account opened" while the account was 110 down.
    profit_loss: float | None = None
    #: Cash: the opening deposit plus everything realised since. Carried so
    #: the identity `deposit + profit_loss == balance` can be shown rather
    #: than asserted, which is what makes the two P&L figures legible as
    #: different quantities.
    deposit: float | None = None
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
    #: Margin as a PERCENTAGE of exposure -- 20 means 20%, which is 5x
    #: leverage. Read `margin_fraction` rather than this wherever the figure
    #: is multiplied by a notional: the two differ by 100x, and a consumer
    #: that guesses wrong is out by two orders of magnitude in the direction
    #: that refuses every trade.
    margin_factor: float | None = None
    #: Overnight funding per side, as the broker states it. The unit is not
    #: documented; see the note in ``persist.spreads``.
    funding_long: float | None = None
    funding_short: float | None = None
    tradable: bool | None = None
    fetched_at: str = ""

    @property
    def margin_fraction(self) -> float | None:
        """``margin_factor`` as a fraction of notional: 20.0 becomes 0.20.

        The conversion lives here so it happens once. It was got wrong the
        first time it was consumed -- the margin ceiling multiplied a notional
        by 20.0 instead of 0.20 and would have demanded twenty times the
        position in margin, refusing every open on an account that looked
        fine. The tests passed because they were written with the same wrong
        unit, which is what a correctly-implemented wrong measurement looks
        like.

        ``None`` stays ``None``: an unquoted requirement is unknown, and the
        margin gate refuses rather than assuming.
        """
        if self.margin_factor is None:
            return None
        try:
            return float(self.margin_factor) / 100.0
        except (TypeError, ValueError):
            return None
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


#: How long to wait before retrying a rate-limited authentication, in
#: seconds, one entry per retry.
#:
#: Capital.com limits how often a session may be created. Two jobs half a
#: second apart is enough to trip it: on 2026-08-31 ``record-spreads``
#: started at :38.754 and ``trade-capital`` at :39.276, each opening its own
#: session, and the second was refused with 429. It happened twice and cost
#: a dealing pass each time -- the book sat four positions short of what the
#: engine wanted while the market was open.
#:
#: Short and bounded. A rate limit clears on its own in about a second, so
#: waiting is the correct response; waiting *long* is not, because the job
#: behind this runs every fifteen minutes and a pass that arrives late is
#: worth less than one that arrives never is worth nothing.
SESSION_RETRY_WAITS: tuple[float, ...] = (1.0, 2.0)


def open_session(
    config: CapitalConfig | None = None, *, opener=None, sleeper=None
) -> CapitalSession:
    """Authenticate. Never raises; a failure comes back as a reason.

    Reports which variables are missing by name, and never their values --
    "the key is wrong" and "the key is absent" are different problems and
    only one of them is fixed by looking at the environment file.

    Retries a **429 and nothing else**. A rate limit is a request to wait
    and says nothing about whether the credentials are good. Every other
    status is returned on the first attempt, deliberately: retrying a 401 or
    a 403 cannot succeed, and repeating a rejected credential at a broker is
    how an account gets locked rather than authenticated.
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
    wait = sleeper if sleeper is not None else time.sleep
    payload = json.dumps(
        {"identifier": settings.identifier, "password": settings.password}
    ).encode("utf-8")

    document = headers = None
    for attempt in range(len(SESSION_RETRY_WAITS) + 1):
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
            break
        except urllib.error.HTTPError as error:
            # The body carries an errorCode worth surfacing --
            # "error.invalid.details" and a 429 are different problems with
            # the same status class.
            detail = ""
            try:
                detail = json.loads(error.read().decode("utf-8")).get("errorCode", "")
            except Exception:  # noqa: BLE001 - the status is the useful part
                detail = ""
            if error.code == 429 and attempt < len(SESSION_RETRY_WAITS):
                pause = SESSION_RETRY_WAITS[attempt]
                logger.info(
                    "Capital: rate limited authenticating; retrying in %.0fs "
                    "(attempt %d of %d)",
                    pause, attempt + 2, len(SESSION_RETRY_WAITS) + 1,
                )
                wait(pause)
                continue
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
        deposit=_number(balance.get("deposit")),
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


def fetch_market_details(
    epics: Sequence[str],
    session: CapitalSession,
    config: CapitalConfig | None = None,
    *,
    opener=None,
    now=None,
) -> list[CapitalMarket]:
    """Every instrument in one call. Never raises.

    ``GET /markets?epics=a,b,c`` returns the same document per instrument as
    the single-epic endpoint -- snapshot, instrument and dealingRules -- for
    twenty names in about a tenth of a second. ``fetch_markets`` beside this
    makes two calls per instrument because it resolves symbols to epics
    first; once the epics are known, which they are after the first run,
    this is the one to use.

    That matters for a reading taken every fifteen minutes: forty calls a
    reading against the documented ten a second is a rate limit waiting to
    be hit, and one call is not.
    """
    settings = config or CapitalConfig.from_env()
    wanted = [str(e) for e in epics if str(e)]
    if not wanted:
        return []
    if not session.ok:
        return [
            CapitalMarket(epic=e, ticker=e, error=session.error or "no session")
            for e in wanted
        ]

    read = opener or _read
    query = urllib.parse.urlencode({"epics": ",".join(wanted)})
    try:
        document, _ = read(
            f"{session.host}/markets?{query}",
            headers=session.headers(settings.api_key),
        )
    except urllib.error.HTTPError as error:
        return [
            CapitalMarket(epic=e, ticker=e, error=f"HTTP {error.code}")
            for e in wanted
        ]
    except Exception as error:  # noqa: BLE001
        return [
            CapitalMarket(epic=e, ticker=e, error=f"{type(error).__name__}: {error}")
            for e in wanted
        ]

    found: dict[str, CapitalMarket] = {}
    for entry in (document or {}).get("marketDetails") or []:
        market = _market_from_details(entry, now=now)
        if market.epic:
            found[market.epic] = market

    # Every epic asked about comes back, in order, carrying its reason if it
    # is missing. An instrument the broker did not return is a finding.
    return [
        found.get(
            epic,
            CapitalMarket(
                epic=epic, ticker=epic,
                error="not in the response; the broker did not return it",
            ),
        )
        for epic in wanted
    ]


def _market_from_details(document: Mapping[str, Any], *, now=None) -> CapitalMarket:
    """One instrument's details document, as a row. Shared by both readers."""
    snapshot = (document or {}).get("snapshot") or {}
    instrument = (document or {}).get("instrument") or {}
    rules = (document or {}).get("dealingRules") or {}

    bid = _number(snapshot.get("bid"))
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
        status = str(snapshot.get("marketStatus") or "unknown")
        price_error = f"no dealing prices (market {status.lower()})"

    epic = str(instrument.get("epic") or document.get("epic") or "")
    fee = instrument.get("overnightFee") or {}
    return CapitalMarket(
        epic=epic,
        ticker=str(instrument.get("symbol") or epic),
        name=str(instrument.get("name") or ""),
        bid=bid, offer=offer,
        spread_points=spread_points, spread_pct=spread_pct,
        currency=str(instrument.get("currency") or ""),
        min_deal_size=_number((rules.get("minDealSize") or {}).get("value")),
        instrument_type=str(instrument.get("type") or ""),
        margin_factor=_number(instrument.get("marginFactor")),
        funding_long=_number(fee.get("longRate")),
        funding_short=_number(fee.get("shortRate")),
        tradable=(
            None if snapshot.get("marketStatus") is None
            else str(snapshot.get("marketStatus")) == "TRADEABLE"
        ),
        fetched_at=str(now or ""),
        error=price_error,
    )


def fetch_activity(
    session: "CapitalSession",
    config: "CapitalConfig | None" = None,
    *,
    since=None,
    epic: str | None = None,
    opener=None,
) -> list[dict]:
    """The account's own activity log. READ ONLY, and never raises.

    **Why this exists.** A position closed outside the dealing path leaves
    no fill in this app's record, so `record_departure` writes the close
    with `level=None` -- absent rather than invented, because inventing one
    would put a price that never traded into the forward record.

    That is correct and it has a cost: `completed_trips` cannot pair a trip
    with no exit, so the trip is dropped. Measured on 2026-09-12, realised
    P&L read -209.62 against a balance change of -246.04, and the gap is
    those dropped trips. The forward record is the only trustworthy evidence
    this project holds, and it was quietly understating losses.

    The broker knows the price. TGT's close on 2026-09-10 is in this log at
    158.43 against a 156.10 open -- read by hand at the time, which is why
    this is now a function.

    A GET, on a module whose whole guarantee is that it places nothing:
    `tests/test_broker_capital.py` asserts no dealing endpoint appears here
    and that the only POST is the session. Reading history does not breach
    that and must not be allowed to -- there is no body and no method
    override.

    Returns the raw activity documents, newest first as Capital returns
    them, or ``[]`` when the log cannot be read. **Empty means unknown, not
    "nothing happened"**, and every caller has to treat it that way: a
    departure whose price cannot be recovered keeps `level=None` rather than
    acquiring a zero.
    """
    import urllib.parse

    settings = config or CapitalConfig.from_env()
    if not session.ok:
        logger.warning("No session; the activity log cannot be read")
        return []

    query: dict[str, str] = {"detailed": "true"}
    if since is not None:
        # Capital wants a naive ISO stamp. A tz-aware one is converted
        # rather than rejected, because every timestamp in this project is
        # UTC-aware and passing one should not silently return nothing.
        try:
            import pandas as pd

            stamp = pd.Timestamp(since)
            if stamp.tzinfo is not None:
                stamp = stamp.tz_convert("UTC").tz_localize(None)
            query["from"] = stamp.isoformat(timespec="seconds")
        except Exception as error:  # noqa: BLE001
            logger.warning("Could not format the activity window: %s", error)
    if epic:
        query["filter"] = f"epic=={epic}"

    url = f"{session.host}/history/activity?{urllib.parse.urlencode(query)}"
    read = opener or _read
    try:
        document, _ = read(url, headers=session.headers(settings.api_key))
    except Exception as error:  # noqa: BLE001 - absence is not an outage
        logger.warning("Could not read the activity log: %s", error)
        return []

    activities = (document or {}).get("activities")
    if not isinstance(activities, list):
        return []
    return [row for row in activities if isinstance(row, dict)]


def closing_level(
    activities, epic: str, *, direction: str | None = None
) -> float | None:
    """The price a position in ``epic`` was closed at, from the activity log.

    Pure. Returns ``None`` when the log does not say, which is the whole
    point: an exit price that cannot be recovered stays absent, and
    `completed_trips` goes on skipping the trip rather than pairing it
    against a number nobody verified.

    Takes the NEWEST matching close. The log is ordered newest first and a
    reconciliation asks about the departure that just happened, not an
    earlier one on the same instrument -- and this book has held the same
    name repeatedly within a week.
    """
    for row in activities or []:
        if str((row or {}).get("epic") or "") != str(epic):
            continue
        status = str((row or {}).get("status") or "").upper()
        if status and "ACCEPT" not in status and "EXECUT" not in status:
            continue

        details = (row or {}).get("details")
        details = details if isinstance(details, dict) else {}
        level = details.get("level")
        if level is None:
            level = (row or {}).get("level")
        value = _number(level)
        if value is None or value <= 0:
            continue

        # A close is the leg opposing what was held. When the caller knows
        # the side, use it: on a netting account an opposing order closes,
        # and taking any fill would return the OPENING price on a name that
        # was opened and closed in the same window.
        if direction:
            side = str(details.get("direction") or row.get("direction") or "").upper()
            wanted = "BUY" if str(direction).lower() in {"short", "sell"} else "SELL"
            if side and side != wanted:
                continue
        return value
    return None


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
