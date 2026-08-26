"""Placing orders on IG: two gates, and neither opens by accident.

This is the only module in the project that can cause money to move. The
constraint it operates under says dealing is opt-in, explicit and off by
default, and that reaching a live account is a separate decision from
enabling dealing at all. These tests are what makes that true rather than
intended.
"""

from __future__ import annotations

import urllib.error

import pytest

from cfdinvest.broker.ig import HOSTS, IgConfig, IgSession
from cfdinvest.broker.ig.dealing import (
    DealingConfig,
    close_position,
    confirm_deal,
    measure_spread,
    place_order,
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


def demo():
    return IgConfig.from_env(CREDS)


def live():
    return IgConfig.from_env({**CREDS, "CFD_IG_ENVIRONMENT": "live"})


# --------------------------------------------------------------------------- #
# The gates
# --------------------------------------------------------------------------- #
def test_dealing_is_off_unless_switched_on():
    """M183: an order placed because nothing said not to.

    The default has to be refusal. A module that deals unless configured
    otherwise is one bad deployment away from trading an account nobody
    meant to trade.
    """
    assert DealingConfig().enabled is False
    assert DealingConfig.from_env({}).enabled is False

    reached = []

    def opener(url, data=None, headers=None, method=None):
        reached.append(url)
        return {"dealReference": "REF"}, {}

    result = place_order(
        "EPIC", "BUY", 1.0, a_session(), demo(),
        DealingConfig(enabled=False), opener=opener,
    )
    assert not reached, "an order reached the network with dealing off"
    assert result.error
    assert "CFD_IG_DEALING_ENABLED" in result.error, (
        "the refusal does not say how to enable it"
    )


@pytest.mark.parametrize(
    "value,enabled",
    [("1", True), ("true", True), ("YES", True), ("on", True),
     ("0", False), ("false", False), ("no", False), ("", False),
     ("maybe", False), ("2", False)],
)
def test_only_an_explicit_yes_opens_the_gate(value, enabled):
    """A gate that opens on any non-empty value is not a gate."""
    assert DealingConfig.from_env(
        {"CFD_IG_DEALING_ENABLED": value}
    ).enabled is enabled


def test_a_live_account_needs_a_second_switch():
    """M184: one typo away from a funded account.

    Enabling dealing is not the same decision as pointing it at real money.
    Two switches mean the mistake has to be made twice, deliberately, in
    different words.
    """
    assert live().is_live

    one_switch = DealingConfig(enabled=True, allow_live=False)
    refusal = one_switch.refusal(live())
    assert refusal, "dealing against live was permitted by one switch"
    assert "LIVE" in refusal and "CFD_IG_ALLOW_LIVE" in refusal

    reached = []

    def opener(url, data=None, headers=None, method=None):
        reached.append(url)
        return {"dealReference": "REF"}, {}

    result = place_order("EPIC", "BUY", 1.0, a_session(), live(), one_switch,
                         opener=opener)
    assert not reached, "an order reached a live account on one switch"
    assert "LIVE" in result.error

    # Both switches, and live proceeds. Demo needs only the first.
    both = DealingConfig(enabled=True, allow_live=True)
    assert both.refusal(live()) == ""
    assert both.refusal(demo()) == ""
    assert one_switch.refusal(demo()) == ""


def test_closing_is_gated_exactly_like_opening():
    """Safety that depends on which function you called is not safety."""
    reached = []

    def opener(url, data=None, headers=None, method=None):
        reached.append(url)
        return {"dealReference": "REF"}, {}

    result = close_position(
        "DEAL1", "SELL", 1.0, a_session(), demo(),
        DealingConfig(enabled=False), opener=opener,
    )
    assert not reached
    assert "CFD_IG_DEALING_ENABLED" in result.error


def test_the_gate_is_read_before_every_order_not_once_at_startup():
    """A process that read the gate hours ago is not evidence of now."""
    import inspect

    from cfdinvest.broker.ig import dealing

    for function in (dealing.place_order, dealing.close_position):
        source = inspect.getsource(function)
        assert "refusal(" in source, (
            f"{function.__name__} does not consult the gate"
        )


# --------------------------------------------------------------------------- #
# A reference is not a fill
# --------------------------------------------------------------------------- #
def test_a_deal_reference_is_not_a_fill():
    """M185: a receipt for asking treated as a receipt for getting.

    IG accepts the request and decides afterwards. A caller that recorded
    the reference as a position would be booking trades that were rejected.
    """
    def opener(url, data=None, headers=None, method=None):
        if url.endswith("/positions/otc"):
            return {"dealReference": "REF123"}, {}
        return {
            "dealStatus": "REJECTED", "reason": "INSUFFICIENT_BALANCE",
            "dealReference": "REF123", "epic": "EPIC", "level": None,
        }, {}

    placed = place_order("EPIC", "BUY", 1.0, a_session(), demo(),
                         DealingConfig(enabled=True), opener=opener)
    assert placed.deal_reference == "REF123"
    assert placed.status == "SUBMITTED", "a submission was called a fill"
    assert not placed.ok, "an unconfirmed order reported as ok"

    confirmed = confirm_deal("REF123", a_session(), demo(), opener=opener)
    assert not confirmed.ok
    assert confirmed.status == "REJECTED"
    assert "REJECTED" in confirmed.error
    assert confirmed.level is None, "a rejected deal was given a fill price"
    assert confirmed.reason == "INSUFFICIENT_BALANCE"


# --------------------------------------------------------------------------- #
# The measurement this all exists for
# --------------------------------------------------------------------------- #
def test_the_spread_is_measured_from_the_two_fills():
    """M186: the spread inferred from anything other than what was paid.

    A buy fills at the offer and a sell at the bid, so the two levels
    bracket the spread actually charged -- no quote, no entitlement, no
    per-class constant. IG will not show this account share prices at all
    (`unauthorised.access.to.equity.exception`), so a fill is not merely a
    better measurement than a quote here, it is the only one available.
    """
    levels = {"REF_OPEN": 200.10, "REF_CLOSE": 199.90}

    def opener(url, data=None, headers=None, method=None):
        if url.endswith("/positions/otc"):
            body = data or {}
            reference = "REF_OPEN" if body.get("direction") == "BUY" else "REF_CLOSE"
            return {"dealReference": reference}, {}
        reference = url.rsplit("/", 1)[-1]
        return {
            "dealStatus": "ACCEPTED", "dealId": "D1", "dealReference": reference,
            "epic": "EPIC", "level": levels[reference], "size": 1.0,
        }, {}

    probe = measure_spread(
        "EPIC", a_session(), demo(), DealingConfig(enabled=True),
        ticker="AAPL", opener=opener, sleep=lambda _s: None,
    )

    assert probe.ok, probe.error
    assert probe.opened_at_level == pytest.approx(200.10)
    assert probe.closed_at_level == pytest.approx(199.90)
    assert probe.spread_points == pytest.approx(0.20)
    # 0.20 on a mid of 200.00 is 10bp, against the 4bp the model assumes.
    assert probe.spread_bps == pytest.approx(10.0, abs=1e-6)


def test_a_probe_that_could_not_close_shouts_about_it():
    """M187: a measurement that quietly leaves a position open.

    The worst outcome here is not a failed measurement. It is a position
    opened by a measurement and left running, which nobody is watching
    because nobody thinks a measurement carries risk.
    """
    def opener(url, data=None, headers=None, method=None):
        if url.endswith("/positions/otc"):
            body = data or {}
            if body.get("direction") == "BUY":
                return {"dealReference": "REF_OPEN"}, {}
            raise urllib.error.HTTPError(url, 500, "Server Error", {}, None)
        return {
            "dealStatus": "ACCEPTED", "dealId": "DEAL-XYZ",
            "dealReference": "REF_OPEN", "epic": "EPIC", "level": 200.10,
        }, {}

    probe = measure_spread(
        "EPIC", a_session(), demo(), DealingConfig(enabled=True),
        opener=opener, sleep=lambda _s: None,
    )

    assert not probe.ok
    assert "OPENED BUT NOT CLOSED" in probe.error
    assert "DEAL-XYZ" in probe.error, "the open deal id is not in the message"
    assert probe.spread_bps is None
    # The level it opened at is still reported: it is evidence.
    assert probe.opened_at_level == pytest.approx(200.10)


def test_a_probe_refuses_before_it_opens_anything_when_gated():
    """The gate must stop the first order, not the second."""
    reached = []

    def opener(url, data=None, headers=None, method=None):
        reached.append(url)
        return {"dealReference": "REF"}, {}

    probe = measure_spread(
        "EPIC", a_session(), demo(), DealingConfig(enabled=False),
        opener=opener, sleep=lambda _s: None,
    )
    assert not reached, "a gated probe still placed an order"
    assert "CFD_IG_DEALING_ENABLED" in probe.error


# --------------------------------------------------------------------------- #
# It stays away from the simulation
# --------------------------------------------------------------------------- #
def test_the_paper_book_does_not_import_dealing():
    """The simulation must never become the thing that places orders.

    Its whole worth is being a record nobody could have edited once the
    outcome was known.
    """
    import ast
    from pathlib import Path

    for name in ("papertrade.py", "papertrade_run.py"):
        source = (Path("cfdinvest/advisor") / name).read_text(encoding="utf-8")
        assert "dealing" not in source, f"{name} references dealing"
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert "broker" not in node.module, f"{name} imports a broker"


def test_the_read_only_client_still_cannot_deal():
    """Adding a dealing module must not have loosened the read-only one."""
    import inspect

    from cfdinvest.broker.ig import client

    source = inspect.getsource(client)
    assert "/positions/otc" not in source
    assert source.count('method="POST"') == 1


def test_a_rejection_carries_its_reason_into_the_probe():
    """M188: the answer discarded from the response that contained it.

    IG puts REJECTED in dealStatus and the *why* in `reason`. The why is
    what distinguishes a malformed order from a short account. This dropped
    REJECT_CFD_ORDER_ON_SPREADBET_ACCOUNT once and cost an hour of guessing
    at something the response had already said.
    """
    def opener(url, data=None, headers=None, method=None):
        if url.endswith("/positions/otc"):
            return {"dealReference": "REF"}, {}
        return {
            "dealStatus": "REJECTED",
            "reason": "REJECT_CFD_ORDER_ON_SPREADBET_ACCOUNT",
            "dealReference": "REF", "epic": "EPIC", "level": None,
        }, {}

    probe = measure_spread(
        "EPIC", a_session(), demo(), DealingConfig(enabled=True),
        opener=opener, sleep=lambda _s: None,
    )
    assert not probe.ok
    assert "REJECT_CFD_ORDER_ON_SPREADBET_ACCOUNT" in probe.error, (
        f"the reason was discarded: {probe.error!r}"
    )


def test_a_spread_bet_asks_for_the_expiry_it_needs():
    """A daily funded bet is expiry DFB; a CFD is "-".

    Getting it wrong does not fail cleanly -- IG accepts the order and then
    rejects it with a message that reads like an account problem.
    """
    sent = {}

    def opener(url, data=None, headers=None, method=None):
        if url.endswith("/positions/otc"):
            sent.update(data or {})
            return {"dealReference": "REF"}, {}
        return {"dealStatus": "ACCEPTED", "dealId": "D", "level": 31210.0}, {}

    place_order("UA.D.AAPL.DAILY.IP", "BUY", 0.24, a_session(), demo(),
                DealingConfig(enabled=True), expiry="DFB", opener=opener)
    assert sent.get("expiry") == "DFB"

    sent.clear()
    place_order("UA.D.AAPL.CASH.IP", "BUY", 1.0, a_session(), demo(),
                DealingConfig(enabled=True), opener=opener)
    assert sent.get("expiry") == "-", "the CFD default changed"
