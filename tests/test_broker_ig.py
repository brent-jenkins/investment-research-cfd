"""The IG client: read-only, demo by default, and silent about secrets.

This is the first code in the project permitted to talk to a broker, and the
constraint it replaced was there for good reasons. These tests are what keeps
the replacement honest.
"""

from __future__ import annotations

import json
import urllib.error

import pytest

from cfdinvest.broker.ig import (
    HOSTS,
    IgConfig,
    IgMarket,
    IgSession,
    fetch_market,
    fetch_markets,
    open_session,
)

CREDS = {
    "CFD_IG_API_KEY": "an-api-key",
    "CFD_IG_USERNAME": "a-username",
    "CFD_IG_PASSWORD": "a-password",
}


def a_session():
    return IgSession(
        host=HOSTS["demo"], environment="demo", account_id="ABC12",
        cst="cst-token", security_token="x-security-token",
    )


def market_document(bid=199.90, offer=200.10, status="TRADEABLE"):
    return {
        "instrument": {
            "name": "Apple Inc",
            "currencies": [{"code": "USD"}],
            "marginFactor": 20.0,
            "overnightFee": {"longRate": -0.0192, "shortRate": 0.0041},
        },
        "snapshot": {"bid": bid, "offer": offer, "marketStatus": status},
        "dealingRules": {"minDealSize": {"value": 0.5, "unit": "AMOUNT"}},
    }


# --------------------------------------------------------------------------- #
# It cannot trade
# --------------------------------------------------------------------------- #
def test_the_client_has_no_way_to_place_an_order():
    """M177: a read-only client that quietly grew a deal endpoint.

    The constraint this replaced forbade broker calls outright. What makes
    the replacement safe is not intent but the absence of a code path: there
    is no function here that could place, amend or cancel, so no config
    drift and no mistaken argument can cause one.

    Order placement, if it is ever built, belongs in a module whose name
    says so and which is gated on an explicit setting.
    """
    import inspect

    from cfdinvest.broker.ig import client

    source = inspect.getsource(client)

    # IG's dealing endpoints, by name.
    for endpoint in ("/positions/otc", "/workingorders", "/positions/sprintmarkets"):
        assert endpoint not in source, f"{endpoint} is an order path"

    # And no verb that writes. GET-shaped calls only, plus the session POST.
    posts = source.count('method="POST"')
    assert posts == 1, f"{posts} POSTs -- only the session may be one"
    for verb in ('method="DELETE"', 'method="PUT"'):
        assert verb not in source, f"{verb} can only be an order operation"

    exported = {name for name in dir(client) if not name.startswith("_")}
    for forbidden in ("place_order", "open_position", "close_position", "deal"):
        assert forbidden not in exported


def test_the_paper_book_does_not_import_the_broker():
    """The simulation must not become the thing that trades.

    Its whole value is being a record nobody could have edited after the
    outcome was known, and a book that sometimes places an order is not
    that.
    """
    import ast
    from pathlib import Path

    for name in ("papertrade.py", "papertrade_run.py"):
        tree = ast.parse(
            (Path("cfdinvest/advisor") / name).read_text(encoding="utf-8")
        )
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        assert not any("broker" in module for module in imported), (
            f"{name} imports a broker"
        )


# --------------------------------------------------------------------------- #
# It defaults to demo
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "value,expected",
    [("demo", "demo"), ("live", "live"), ("LIVE", "live"), ("liv", "demo"),
     ("", "demo"), (None, "demo"), ("production", "demo")],
    ids=["demo", "live", "upper", "typo", "empty", "unset", "wrong word"],
)
def test_live_has_to_be_spelled_exactly(value, expected):
    """M178: a typo reaching a funded account.

    Anything that is not the exact string "live" resolves to demo. A
    misspelt environment silently selecting the real account would be the
    worst possible way to find out about a typo.
    """
    env = dict(CREDS)
    if value is not None:
        env["CFD_IG_ENVIRONMENT"] = value

    config = IgConfig.from_env(env)
    assert config.environment == expected
    assert config.host == HOSTS[expected]
    assert config.is_live is (expected == "live")


# --------------------------------------------------------------------------- #
# It does not leak
# --------------------------------------------------------------------------- #
def test_no_secret_appears_in_a_repr():
    """M179: a password in a log line.

    These objects end up in debug output and exception context. A broker key
    is not a FRED key -- the blast radius is a funded account.
    """
    config = IgConfig.from_env(
        {**CREDS, "CFD_IG_API_KEY": "KEYKEYKEY", "CFD_IG_PASSWORD": "PWPWPWPW"}
    )
    text = repr(config)
    assert "PWPWPWPW" not in text, "the password is in the repr"
    assert "KEYKEYKEY" not in text, "the api key is in the repr"
    assert "a-username" in text, "the username is fine and useful for support"

    session = a_session()
    assert "cst-token" not in repr(session)
    assert "x-security-token" not in repr(session)


def test_an_authentication_failure_does_not_echo_the_body():
    """IG's error bodies carry detail not worth logging beside a username."""
    def opener(url, data=None, headers=None, method=None):
        raise urllib.error.HTTPError(
            url, 403, "Forbidden", {}, None
        )

    session = open_session(IgConfig.from_env(CREDS), opener=opener)
    assert not session.ok
    assert "403" in session.error
    assert "a-password" not in session.error
    assert "a-api-key" not in session.error


def test_credentials_are_never_read_from_an_argument_default():
    """They come from the environment, and an unset one says so."""
    import inspect

    from cfdinvest.broker.ig import client

    for function in (client.open_session, client.fetch_market):
        for parameter in inspect.signature(function).parameters.values():
            assert not isinstance(parameter.default, str) or not parameter.default, (
                f"{function.__name__} has a string default: {parameter}"
            )

    session = open_session(IgConfig())
    assert not session.ok
    assert "not configured" in session.error
    assert "CFD_IG_API_KEY" in session.error


# --------------------------------------------------------------------------- #
# What it is for: measured spreads
# --------------------------------------------------------------------------- #
def test_the_spread_comes_back_as_a_percentage_not_only_points():
    """M180: points compared across instruments of different price.

    20p of spread on a $40 share and on a $400 one are not the same cost,
    and treating them alike is precisely the mistake the per-class constant
    was already making. The percentage of mid is what the cost model wants.
    """
    def opener(url, data=None, headers=None, method=None):
        return market_document(bid=199.90, offer=200.10), {}

    market = fetch_market("IX.D.AAPL.CASH.IP", a_session(), IgConfig.from_env(CREDS),
                          ticker="AAPL", opener=opener)

    assert market.ok
    assert market.spread_points == pytest.approx(0.20)
    # 0.20 on a mid of 200.00 is 0.10%, i.e. 10bp round trip.
    assert market.spread_pct == pytest.approx(0.10, abs=1e-9)
    assert market.bid == pytest.approx(199.90)
    assert market.offer == pytest.approx(200.10)


def test_the_measured_spread_is_the_thing_worth_having():
    """The estimate it replaces is 79% of all modelled cost.

    Every one of the nineteen instruments charges a per-class constant of
    4bp round trip. If IG's real spread is materially wider, the edge --
    which breaks even at about 4bp of *further* slippage -- is thinner than
    the backtest says.
    """
    from cfdinvest.execution.fills import DEFAULT_SPREAD_BPS

    estimate_bps = 2 * DEFAULT_SPREAD_BPS["equity"]

    def opener(url, data=None, headers=None, method=None):
        return market_document(bid=199.90, offer=200.10), {}

    market = fetch_market("EPIC", a_session(), IgConfig.from_env(CREDS), opener=opener)
    measured_bps = market.spread_pct * 100

    # Not an assertion about which is bigger -- that is what the fetch is
    # for. What is asserted is that the two are in the same unit, so the
    # comparison is possible at all.
    assert measured_bps > 0
    assert estimate_bps > 0
    assert isinstance(measured_bps, float)


def test_an_instrument_that_could_not_be_read_keeps_its_row():
    """M181: a silent hole in the spread table.

    A missing name would fall back to the per-class constant without saying
    so, which is the estimate this exists to remove reappearing in the one
    place nobody would look.
    """
    def opener(url, data=None, headers=None, method=None):
        if "GOOD" in url:
            return market_document(), {}
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    markets = fetch_markets(
        {"AAPL": "GOOD.EPIC", "NOPE": "BAD.EPIC"},
        a_session(), IgConfig.from_env(CREDS), opener=opener,
    )

    assert len(markets) == 2, "a failed instrument was dropped"
    by_ticker = {m.ticker: m for m in markets}
    assert by_ticker["AAPL"].ok
    assert not by_ticker["NOPE"].ok
    assert "404" in by_ticker["NOPE"].error
    assert by_ticker["NOPE"].spread_pct is None, "a failure was given a number"


def test_a_missing_price_is_absent_rather_than_zero():
    """Absence is named, as everywhere else here."""
    def opener(url, data=None, headers=None, method=None):
        document = market_document()
        document["snapshot"]["bid"] = None
        return document, {}

    market = fetch_market("EPIC", a_session(), IgConfig.from_env(CREDS), opener=opener)
    assert market.spread_pct is None
    assert market.spread_points is None
    assert not market.ok


def test_a_session_without_tokens_is_not_a_session():
    """IG can answer 200 and still not authenticate."""
    def opener(url, data=None, headers=None, method=None):
        return {"currentAccountId": "ABC12"}, {}          # no CST header

    session = open_session(IgConfig.from_env(CREDS), opener=opener)
    assert not session.ok
    assert "no tokens" in session.error


def test_a_good_session_carries_the_account_and_the_host():
    def opener(url, data=None, headers=None, method=None):
        assert url.startswith(HOSTS["demo"]), "the demo host was not used"
        return (
            {"currentAccountId": "ABC12"},
            {"CST": "cst-1", "X-SECURITY-TOKEN": "tok-1"},
        )

    session = open_session(IgConfig.from_env(CREDS), opener=opener)
    assert session.ok
    assert session.account_id == "ABC12"
    assert session.environment == "demo"
    assert session.host == HOSTS["demo"]

    headers = session.headers("an-api-key")
    assert headers["CST"] == "cst-1"
    assert headers["X-SECURITY-TOKEN"] == "tok-1"
    assert headers["X-IG-API-KEY"] == "an-api-key"
