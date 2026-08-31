"""The Capital.com client: read-only by construction, and measuring spread.

The point of this broker is that the number that matters can be read without
dealing. IG would not quote share prices to that account, so its spread had
to be discovered by placing orders and reading the fills -- which is how the
222bp round-trip commission was found, expensively. Capital.com returns bid
and offer, so nothing here needs to trade.

M267 a dealing endpoint added to a read-only client
M268 any l-word treated as live
M269 the API key or password in a repr
M270 a fuzzy search match accepted as the instrument
M271 an instrument that failed dropped from the table
M272 spread compared in points across instruments of different price
M273 the session tokens read from the body instead of the headers
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from cfdinvest.broker.capital import client as capital

SOURCE = Path("cfdinvest/broker/capital/client.py").read_text(encoding="utf-8")

CONFIG = capital.CapitalConfig(
    api_key="key-value", identifier="someone@example.com",
    password="secret-value", environment="demo",
)
SESSION = capital.CapitalSession(
    cst="cst-value", security_token="token-value", host=capital.DEMO_HOST,
    account_id="ACC1", currency="GBP",
)


# --------------------------------------------------------------------------- #
# It cannot trade
# --------------------------------------------------------------------------- #
def test_the_client_has_no_way_to_deal():
    """M267: the failure is not "an order is placed".

    It is an order placed by code nobody thought could trade. So the dealing
    endpoints are absent rather than disabled -- there is nothing to switch
    on by accident, and nothing a caller could reach by passing an unusual
    argument.
    """
    for endpoint in ("/positions", "/workingorders", "/confirms"):
        assert endpoint not in SOURCE, f"{endpoint} appears in a read-only client"

    tree = ast.parse(SOURCE)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            assert not any(
                word in node.name.lower()
                for word in ("place", "deal", "open_position", "close_position",
                             "buy", "sell", "amend", "cancel")
            ), f"{node.name} sounds like it deals"


def test_the_only_write_is_the_session():
    """Authenticating is a POST. Nothing else may be."""
    posts = SOURCE.count('method="POST"')
    assert posts == 1, f"{posts} POSTs in a read-only client"
    assert 'method="PUT"' not in SOURCE
    assert 'method="DELETE"' not in SOURCE


# --------------------------------------------------------------------------- #
# Which account it reaches
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "value", ["", "demo", "Demo", "LIVE ", "liv", "live-ish", "production", "1", "l"]
)
def test_only_the_exact_word_live_reaches_the_live_host(value):
    """M268: a typo selecting a funded account.

    Everything that is not exactly ``live`` resolves to demo, including
    "liv" and "live-ish". A misspelt environment silently reaching the real
    account is the worst possible way to learn about a typo.
    """
    config = capital.CapitalConfig.from_env({capital.ENVIRONMENT_VAR: value})
    if value.strip().lower() == "live":
        assert config.is_live and config.host == capital.LIVE_HOST
    else:
        assert not config.is_live
        assert config.host == capital.DEMO_HOST


def test_live_is_reachable_when_it_is_spelled_out():
    """The test above must not pass by nothing ever being live."""
    config = capital.CapitalConfig.from_env({capital.ENVIRONMENT_VAR: "live"})
    assert config.is_live and config.host == capital.LIVE_HOST


def test_no_secret_appears_in_a_repr():
    """M269: these objects reach debug output and exception context.

    A broker key is not a FRED key. The blast radius of leaking one is a
    funded account rather than a rate limit.
    """
    for text in (repr(CONFIG), repr(SESSION)):
        for secret in ("key-value", "secret-value", "cst-value", "token-value"):
            assert secret not in text, f"{secret} leaked into a repr"


def test_missing_credentials_are_named_without_their_values():
    session = capital.open_session(capital.CapitalConfig(api_key="k"))

    assert not session.ok
    assert capital.IDENTIFIER_VAR in session.error
    assert capital.PASSWORD_VAR in session.error
    assert "k" not in session.error.replace(capital.KEY_VAR, "")


# --------------------------------------------------------------------------- #
# The session
# --------------------------------------------------------------------------- #
def test_the_tokens_are_read_from_the_response_headers():
    """M273: Capital.com returns them as headers, not in the body.

    A reader that only parsed the document would authenticate successfully
    and then fail on every subsequent call, which reads as an entitlement
    problem rather than as a bug here.
    """
    def opener(url, data=None, headers=None, method=None):
        assert method == "POST"
        assert headers["X-CAP-API-KEY"] == "key-value"
        body = json.loads(data.decode("utf-8"))
        assert body == {
            "identifier": "someone@example.com", "password": "secret-value"
        }
        return (
            {"currentAccountId": "ACC1",
             "accounts": [{"accountId": "ACC1", "currency": "GBP"}]},
            {"CST": "cst-1", "X-SECURITY-TOKEN": "tok-1"},
        )

    session = capital.open_session(CONFIG, opener=opener)
    assert session.ok
    assert session.account_id == "ACC1" and session.currency == "GBP"
    assert session.headers("key-value")["CST"] == "cst-1"


def test_header_casing_does_not_decide_whether_it_worked():
    """Proxies normalise header case; the tokens must survive it."""
    def opener(url, data=None, headers=None, method=None):
        return ({}, {"cst": "cst-1", "x-security-token": "tok-1"})

    assert capital.open_session(CONFIG, opener=opener).ok


def test_a_session_without_tokens_is_a_failure_not_a_success():
    """A 200 with no tokens is not an authenticated session."""
    def opener(url, data=None, headers=None, method=None):
        return ({"currentAccountId": "ACC1"}, {})

    session = capital.open_session(CONFIG, opener=opener)
    assert not session.ok
    assert "CST" in session.error


# --------------------------------------------------------------------------- #
# Resolving instruments
# --------------------------------------------------------------------------- #
def test_only_an_exact_symbol_match_resolves():
    """M270: dealing on the wrong instrument because a search was close.

    "AAPL" must not resolve to "AAPL34" or "Apple Inc CFD (mini)". The
    failure has no upper bound, and refusing costs one line in a report.
    """
    def opener(url, data=None, headers=None, method=None):
        return ({"markets": [
            {"symbol": "AAPL34", "epic": "AAPL34"},
            {"symbol": "AAPLX", "epic": "AAPLX"},
        ]}, {})

    epic, error = capital.find_epic("AAPL", SESSION, CONFIG, opener=opener)
    assert epic == ""
    assert "no exact symbol match" in error


def test_an_exact_match_is_found_among_others():
    def opener(url, data=None, headers=None, method=None):
        return ({"markets": [
            {"symbol": "AAPL34", "epic": "wrong"},
            {"symbol": "aapl", "epic": "AAPL"},
        ]}, {})

    epic, error = capital.find_epic("AAPL", SESSION, CONFIG, opener=opener)
    assert epic == "AAPL" and not error


# --------------------------------------------------------------------------- #
# The measurement
# --------------------------------------------------------------------------- #
def _details(bid, offer, **extra):
    document = {
        "instrument": {"name": "Apple", "currency": "USD", "type": "SHARES"},
        "dealingRules": {"minDealSize": {"value": 1.0}},
        "snapshot": {"bid": bid, "offer": offer, "marketStatus": "TRADEABLE"},
    }
    document["snapshot"].update(extra)
    return document


def test_the_spread_is_reported_as_a_share_of_the_mid():
    """M272: points are not comparable across a $40 share and a $400 one.

    Comparing them is exactly the mistake a single per-class constant was
    already making, and the reason the cost model's largest line is a guess.
    """
    def opener(url, data=None, headers=None, method=None):
        return (_details(199.0, 201.0), {})

    market = capital.fetch_market("AAPL", SESSION, CONFIG, ticker="AAPL", opener=opener)

    assert market.ok
    assert market.spread_points == pytest.approx(2.0)
    assert market.spread_pct == pytest.approx(1.0)      # 2 / 200
    assert market.min_deal_size == pytest.approx(1.0)
    assert market.currency == "USD"


def test_the_ask_is_read_under_either_spelling():
    """Capital.com spells it ``offer`` in places and ``ofr`` in others."""
    def opener(url, data=None, headers=None, method=None):
        document = _details(199.0, None)
        document["snapshot"].pop("offer")
        document["snapshot"]["ofr"] = 201.0
        return (document, {})

    market = capital.fetch_market("AAPL", SESSION, CONFIG, opener=opener)
    assert market.spread_pct == pytest.approx(1.0)


def test_a_market_with_no_prices_says_why():
    """An empty reason is how nineteen instruments reported 'error:' once."""
    def opener(url, data=None, headers=None, method=None):
        document = _details(None, None)
        document["snapshot"]["marketStatus"] = "CLOSED"
        return (document, {})

    market = capital.fetch_market("AAPL", SESSION, CONFIG, opener=opener)

    assert not market.ok
    assert market.error.strip()
    assert "closed" in market.error


def test_an_instrument_that_failed_stays_in_the_table():
    """M271: a partial reading that looks complete.

    The interesting rows here are the failures -- an instrument the broker
    does not offer is a finding, not a gap.
    """
    def opener(url, data=None, headers=None, method=None):
        if "searchTerm" in url:
            symbol = url.split("searchTerm=")[1].split("&")[0]
            if symbol == "NOPE":
                return ({"markets": []}, {})
            return ({"markets": [{"symbol": symbol, "epic": symbol}]}, {})
        return (_details(199.0, 201.0), {})

    rows = capital.fetch_markets(
        ["AAPL", "NOPE", "MSFT"], SESSION, CONFIG, opener=opener, sleep=lambda s: None
    )

    assert [row.ticker for row in rows] == ["AAPL", "NOPE", "MSFT"]
    assert rows[1].error and not rows[1].ok
    assert rows[0].ok and rows[2].ok


def test_the_reader_is_paced():
    """Ten a second is documented; a limiter that only reacts to a 429 has
    already been rude."""
    pauses: list[float] = []

    def opener(url, data=None, headers=None, method=None):
        if "searchTerm" in url:
            return ({"markets": [{"symbol": "AAPL", "epic": "AAPL"}]}, {})
        return (_details(199.0, 201.0), {})

    capital.fetch_markets(
        ["AAPL"], SESSION, CONFIG, opener=opener, sleep=pauses.append
    )
    assert pauses, "no pacing at all"
    assert all(p >= capital.REQUEST_INTERVAL_SECONDS for p in pauses)


# --------------------------------------------------------------------------- #
# M-live: two jobs half a second apart, and the second one refused
# --------------------------------------------------------------------------- #
# Capital.com limits how often a session may be created, and the daemon runs
# record-spreads and trade-capital back to back. On 2026-08-31 they started
# at :38.754 and :39.276, each opening its own session, and the second was
# refused with 429. It cost a dealing pass twice over, while the book sat
# four positions short of what the engine wanted with the market open.
#
# A rate limit is a request to wait. It says nothing about the credentials,
# which is why only a 429 is retried.

import io as _io
import urllib.error as _urlerror


def _config():
    return capital.CapitalConfig(
        api_key="k", identifier="i", password="p", environment="demo"
    )


def _rate_limited(times: int):
    """An opener that returns 429 `times` times, then succeeds."""
    state = {"calls": 0}

    def read(url, data=None, headers=None, method=None):
        state["calls"] += 1
        if state["calls"] <= times:
            raise _urlerror.HTTPError(
                url, 429, "rate", {},
                _io.BytesIO(b'{"errorCode":"error.too-many.requests"}'),
            )
        return ({"clientId": "c"}, {"CST": "a", "X-SECURITY-TOKEN": "b"})

    return read, state


def test_a_rate_limited_session_is_retried_and_succeeds():
    """The reported failure, and that it now recovers within one pass."""
    waits: list[float] = []
    read, state = _rate_limited(1)

    session = capital.open_session(_config(), opener=read, sleeper=waits.append)

    assert session.ok, session.error
    assert state["calls"] == 2, "the 429 was not retried"
    assert waits == [1.0], "retried without waiting, which trips the limit again"


def test_the_retry_is_bounded_and_keeps_the_reason():
    """A wait must not become a hang.

    The job behind this runs every fifteen minutes. A pass that arrives late
    is worth something; one that never returns is worth less than a failure,
    because a failure at least says so.
    """
    waits: list[float] = []
    read, state = _rate_limited(99)

    session = capital.open_session(_config(), opener=read, sleeper=waits.append)

    assert not session.ok
    assert state["calls"] == len(capital.SESSION_RETRY_WAITS) + 1
    assert waits == list(capital.SESSION_RETRY_WAITS)
    # The reason survives the retries. "Rate limited" and "wrong password"
    # are different problems and only one is fixed by waiting.
    assert "429" in session.error and "too-many" in session.error


def test_a_credential_failure_is_not_retried():
    """The half that matters more than the fix.

    Retrying a 401 or 403 cannot succeed, and repeating a rejected
    credential at a broker is how an account gets locked rather than
    authenticated. Only a 429 is a request to wait.
    """
    for status, code in ((401, b"error.invalid.details"),
                         (403, b"error.forbidden"),
                         (500, b"error.server")):
        calls = {"n": 0}
        waits: list[float] = []

        def read(url, data=None, headers=None, method=None, _s=status, _c=code):
            calls["n"] += 1
            raise _urlerror.HTTPError(
                url, _s, "no", {}, _io.BytesIO(b'{"errorCode":"' + _c + b'"}')
            )

        session = capital.open_session(_config(), opener=read, sleeper=waits.append)

        assert not session.ok
        assert calls["n"] == 1, f"HTTP {status} was retried"
        assert waits == [], f"HTTP {status} caused a wait"
        assert str(status) in session.error


def test_the_password_never_reaches_the_error_text():
    """Unchanged by the retry, and worth re-asserting where it is easy to lose.

    A retried failure builds its message more than once; a leak here would
    put a broker password in a log that is read every time dealing breaks.
    """
    read, _ = _rate_limited(99)
    session = capital.open_session(_config(), opener=read, sleeper=lambda _s: None)

    assert "p" not in session.error.replace("error.too-many.requests", "")
    assert "k" not in session.error.replace("error.too-many.requests", "")


# --------------------------------------------------------------------------- #
# M-live: unrealised P&L displayed as profit since the account opened
# --------------------------------------------------------------------------- #
# The broker reports two different quantities and the dashboard showed one
# under the other's name. With a 5,000 account standing at 4,890 -- down 110
# -- the Broker page read "Profit and loss  GBP 26.12  since the account
# opened". 26.12 was the unrealised P&L on open positions.
#
# A label that turns a loss into a profit is worse than no tile, and this is
# the page a person reads before deciding whether the thing is working.


def _account_payload():
    """The shape Capital actually returns, taken from the demo account."""
    return {
        "accounts": [{
            "accountId": "328418483926475806",
            "accountName": "Account",
            "currency": "GBP",
            "preferred": True,
            "balance": {
                "balance": 4890.35,
                "deposit": 4863.92,
                "profitLoss": 26.43,
                "available": 1933.53,
            },
        }]
    }


def test_cash_and_unrealised_are_read_as_separate_quantities():
    """Both are needed, and only one was being carried.

    Without `deposit` there is no way to show that the P&L figure is
    unrealised rather than lifetime -- the page has one number and no way to
    place it.
    """
    session = capital.CapitalSession(
        host="https://x", cst="a", security_token="b",
        account_id="328418483926475806",
    )
    account = capital.fetch_account(
        session, _config(), opener=lambda *a, **k: (_account_payload(), {})
    )

    assert account.ok, account.error
    assert account.balance == pytest.approx(4890.35)
    assert account.deposit == pytest.approx(4863.92)
    assert account.profit_loss == pytest.approx(26.43)
    assert account.available == pytest.approx(1933.53)


def test_the_two_figures_reconcile_to_the_balance():
    """The identity that makes them legible: cash + unrealised = balance.

    Asserted rather than described, because it is the thing that shows the
    P&L figure covers open positions only. If it were profit since opening
    it would not reconcile this way, and a reader has no other way to tell
    the two apart from a single number on a tile.
    """
    session = capital.CapitalSession(
        host="https://x", cst="a", security_token="b",
        account_id="328418483926475806",
    )
    account = capital.fetch_account(
        session, _config(), opener=lambda *a, **k: (_account_payload(), {})
    )

    assert account.deposit + account.profit_loss == pytest.approx(account.balance)
    # And on this payload the account is DOWN against a 5,000 opening
    # deposit while the unrealised figure is positive -- which is the case
    # the old label got backwards.
    assert account.balance < 5_000.0 < account.balance + 200
    assert account.profit_loss > 0


# --------------------------------------------------------------------------- #
# "If I gave you 5,000, would you earn or lose me money?"
# --------------------------------------------------------------------------- #
# The owner's framing, and the question the whole forward record exists to
# answer. It could not be answered at all until the opening balance was
# recorded, because the broker never reports it: `deposit` is the opening
# figure plus everything realised, so inferring the opening from it is wrong
# by exactly the realised P&L -- which is the quantity being sought.


def _account(balance=4890.35, deposit=4863.92, profit_loss=26.43):
    return capital.CapitalAccount(
        currency="GBP", balance=balance, available=1933.53,
        profit_loss=profit_loss, deposit=deposit,
    )


def test_the_lifetime_answer_is_computed_from_what_was_put_in():
    """The reported case, to the penny.

    A 5,000 account standing at 4,890 is DOWN 110, while its unrealised P&L
    is up 26. Those are different numbers with different signs, and the page
    used to show the second under the first's name.
    """
    from cfdinvest.ui.broker import performance_since_opening

    got = performance_since_opening(_account(), 5_000.0)

    assert got["since_opening"] == pytest.approx(-109.65)
    assert got["realised"] == pytest.approx(-136.08)
    assert got["unrealised"] == pytest.approx(26.43)
    assert got["since_opening_pct"] == pytest.approx(-0.02193, abs=1e-5)
    assert got["since_opening"] < 0 < got["unrealised"], (
        "the case that matters: down overall while open positions show a "
        "profit, which is exactly when the old label lied"
    )


def test_the_halves_sum_to_the_whole():
    """Asserted, not described. Realised plus unrealised is the lifetime
    figure, and if that identity fails the decomposition is telling a
    different story from the headline."""
    from cfdinvest.ui.broker import performance_since_opening

    for balance, deposit, unrealised in (
        (4890.35, 4863.92, 26.43),
        (5500.00, 5400.00, 100.00),
        (5000.00, 5000.00, 0.00),
    ):
        got = performance_since_opening(
            _account(balance, deposit, unrealised), 5_000.0
        )
        assert got["realised"] + got["unrealised"] == pytest.approx(
            got["since_opening"]
        )


def test_an_unset_opening_balance_is_absent_not_zero():
    """M: unset read as 0, making every account infinitely profitable.

    "Not available" and "broke even" are different statements, and this is
    the number somebody uses to decide whether the thing works.
    """
    from cfdinvest.ui.broker import performance_since_opening

    got = performance_since_opening(_account(), None)

    assert got["since_opening"] is None
    assert got["realised"] is None
    assert got["since_opening_pct"] is None
    assert got["performance_note"], "the absence was not explained"
    # The unrealised figure is still reported -- it is known, and withholding
    # it because a different number is missing would be its own inference.
    assert got["unrealised"] == pytest.approx(26.43)


@pytest.mark.parametrize("raw", ["", "abc", "0", "-100", None])
def test_an_unusable_opening_balance_is_treated_as_unset(raw):
    """A typo must not publish a wrong lifetime P&L.

    Zero is the dangerous one: it would divide into a percentage and make
    any account look infinitely profitable. Refused rather than accepted.
    """
    env = {} if raw is None else {capital.OPENING_BALANCE_VAR: raw}

    assert capital.CapitalConfig.from_env(env).opening_balance is None


def test_the_opening_balance_travels_with_the_account_not_the_config():
    """Why it is an environment variable beside the credentials.

    The opening balance is a property of the account those credentials
    reach. Pointing the system at a live account changes the account and its
    opening balance together; a figure in the shared config file would go on
    describing the demo one.
    """
    demo = capital.CapitalConfig.from_env({
        capital.ENVIRONMENT_VAR: "demo",
        capital.OPENING_BALANCE_VAR: "5000",
    })
    live = capital.CapitalConfig.from_env({
        capital.ENVIRONMENT_VAR: "live",
        capital.OPENING_BALANCE_VAR: "25000",
    })

    assert demo.opening_balance == 5_000.0 and not demo.is_live
    assert live.opening_balance == 25_000.0 and live.is_live


def test_the_opening_balance_is_not_treated_as_a_secret():
    """It is not one, and hiding it would make a wrong figure undebuggable.

    The credentials beside it stay hidden -- that assertion is the point of
    pairing the two here.
    """
    shown = repr(capital.CapitalConfig(
        api_key="SECRET-KEY", identifier="me@example.com",
        password="SECRET-PASS", opening_balance=5_000.0,
    ))

    assert "5000" in shown
    for secret in ("SECRET-KEY", "me@example.com", "SECRET-PASS"):
        assert secret not in shown
