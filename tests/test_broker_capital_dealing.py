"""The only module in this project that can move money, and its gates.

The failure being prevented is not "an order is placed". It is an order
placed by code nobody thought could trade, or against an account nobody
meant to reach.

M278 dealing on by default, or the gate opening on any value
M279 live permitted on one switch
M280 the gates checked once at start-up instead of per order
M281 a deal reference reported as a fill
M282 a rejected deal carrying a level anyway
M283 an unconfirmed placement reported quietly
M284 a refused close reported as a close
M285 a zero size rounded up to a minimum position
M286 an unreadable position list read as an empty account
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cfdinvest.broker.capital import dealing
from cfdinvest.broker.capital.client import CapitalConfig, CapitalSession

SESSION = CapitalSession(
    cst="c", security_token="t", host="https://demo.example", account_id="A"
)
DEMO = CapitalConfig(api_key="k", identifier="i", password="p", environment="demo")
LIVE = CapitalConfig(api_key="k", identifier="i", password="p", environment="live")


def _config(broker=DEMO, dealing_on=True, allow_live=False):
    return dealing.DealingConfig(
        broker=broker, dealing_enabled=dealing_on, allow_live=allow_live
    )


# --------------------------------------------------------------------------- #
# The gates
# --------------------------------------------------------------------------- #
def test_dealing_is_off_unless_switched_on():
    """M278: a config file drifting into a live order.

    Off is the default, and the environment must say so explicitly.
    """
    assert dealing.DealingConfig.from_env({}).dealing_enabled is False
    assert dealing.DealingConfig().dealing_enabled is False

    result = dealing.place_position("AAPL", "BUY", 1.0, SESSION, _config(dealing_on=False))
    assert not result.ok
    assert dealing.DEALING_VAR in result.error


@pytest.mark.parametrize(
    "value,expected",
    [("1", True), ("true", True), ("TRUE", True), ("yes", True), ("on", True),
     ("", False), ("0", False), ("2", False), ("maybe", False),
     ("enabled", False), ("yes please", False), ("no", False)],
)
def test_the_gate_opens_only_on_a_closed_set(value, expected):
    """M278: a switch that opens on any non-empty string.

    That is a switch somebody turns on by pasting the wrong line into an env
    file, and "2" or "enabled" reading as yes is exactly how.
    """
    config = dealing.DealingConfig.from_env({dealing.DEALING_VAR: value})
    assert config.dealing_enabled is expected


def test_live_needs_a_second_switch_in_different_words():
    """M279: one typo away from a funded account.

    Two gates mean the mistake has to be made twice, in different words.
    """
    refused = _config(broker=LIVE, dealing_on=True, allow_live=False)
    assert dealing.ALLOW_LIVE_VAR in refused.refusal()

    permitted = _config(broker=LIVE, dealing_on=True, allow_live=True)
    assert permitted.refusal() == ""

    # And the demo account never needs the live switch.
    assert _config(broker=DEMO, dealing_on=True, allow_live=False).refusal() == ""


def test_the_gates_are_read_on_every_order_not_once():
    """M280: a daemon running for a week on the environment it started in.

    ``refusal()`` is called inside each order, so turning dealing off takes
    effect on the next one rather than on the next restart.
    """
    source = Path("cfdinvest/broker/capital/dealing.py").read_text(encoding="utf-8")
    for function in ("def place_position(", "def close_position("):
        body = source[source.index(function):]
        body = body[: body.index("\ndef ", 1)] if "\ndef " in body[1:] else body
        assert "refusal()" in body, f"{function} does not check the gates"


def test_closing_is_gated_exactly_like_opening():
    """Safety that depends on which function you called is not safety."""
    result = dealing.close_position("D1", SESSION, _config(dealing_on=False))
    assert not result.ok
    assert dealing.DEALING_VAR in result.error


# --------------------------------------------------------------------------- #
# A reference is not a fill
# --------------------------------------------------------------------------- #
def _opener(place=None, confirm=None, fail_confirm=False):
    def opener(url, data=None, headers=None, method=None):
        if url.endswith("/positions") and method == "POST":
            return (place if place is not None else {"dealReference": "REF1"}), {}
        if "/confirms/" in url:
            if fail_confirm:
                raise TimeoutError("confirm timed out")
            return (confirm or {}), {}
        raise AssertionError(f"unexpected call to {url}")
    return opener


def test_a_deal_reference_alone_is_not_success():
    """M281: recording fills that never happened.

    Capital.com accepts the request and decides afterwards. Treating the
    reference as success reintroduces, through the broker, the exact failure
    the paper book exists to avoid.
    """
    result = dealing.place_position(
        "AAPL", "BUY", 1.0, SESSION, _config(),
        opener=_opener(confirm={"dealStatus": "REJECTED", "reason": "MARKET_CLOSED"}),
    )

    assert not result.ok
    assert result.reference == "REF1"
    assert result.status == "REJECTED"
    assert result.reason == "MARKET_CLOSED"


def test_a_rejected_deal_carries_no_level():
    """M282: a price for a fill that did not happen."""
    result = dealing.place_position(
        "AAPL", "BUY", 1.0, SESSION, _config(),
        opener=_opener(confirm={"dealStatus": "REJECTED", "reason": "NO"}),
    )
    assert result.level is None
    assert not result.ok


def test_a_confirmed_deal_carries_its_level():
    result = dealing.place_position(
        "AAPL", "BUY", 1.0, SESSION, _config(),
        opener=_opener(confirm={
            "dealStatus": "ACCEPTED", "dealId": "D9", "level": 314.47,
            "size": 1.0, "epic": "AAPL", "direction": "BUY",
        }),
    )
    assert result.ok
    assert result.deal_id == "D9"
    assert result.level == pytest.approx(314.47)


def test_an_unconfirmed_placement_shouts():
    """M283: the one state that needs a human.

    The order may well have become a position. Reporting that quietly is how
    a position ends up running that nobody is watching.
    """
    result = dealing.place_position(
        "AAPL", "BUY", 1.0, SESSION, _config(), opener=_opener(fail_confirm=True),
    )

    assert not result.ok
    assert "UNCONFIRMED" in result.error
    assert result.reference == "REF1", "the reference must survive, to look it up"


def test_a_refused_close_says_the_position_is_still_open():
    """M284: the more dangerous refusal.

    A refused open leaves the account as it was. A refused close leaves a
    position running that somebody believes is shut.
    """
    for config in (_config(dealing_on=False), _config(broker=LIVE, allow_live=False)):
        result = dealing.close_position("D1", SESSION, config)
        assert "STILL OPEN" in result.error.upper()


def test_a_zero_size_is_a_refusal_not_a_minimum():
    """M285: overriding the sizing step's decision.

    A zero size means sizing declined -- no headroom, or a limit bound.
    Rounding that up to a minimum position would override a refusal by
    turning it into a trade.
    """
    for size in (0.0, -1.0):
        result = dealing.place_position("AAPL", "BUY", size, SESSION, _config())
        assert not result.ok
        assert "declined" in result.error or "not positive" in result.error


def test_an_unknown_direction_is_refused():
    result = dealing.place_position("AAPL", "sideways", 1.0, SESSION, _config())
    assert not result.ok and "direction" in result.error


@pytest.mark.parametrize(
    "given,sent", [("long", "BUY"), ("BUY", "BUY"), ("short", "SELL"), ("sell", "SELL")]
)
def test_the_book_s_words_and_the_broker_s_words_are_mapped(given, sent):
    """The book says long and short; the API says BUY and SELL."""
    seen = {}

    def opener(url, data=None, headers=None, method=None):
        if method == "POST":
            seen.update(json.loads(data.decode("utf-8")))
            return {"dealReference": "REF1"}, {}
        return {"dealStatus": "ACCEPTED", "level": 1.0, "dealId": "D"}, {}

    dealing.place_position("AAPL", given, 1.0, SESSION, _config(), opener=opener)
    assert seen["direction"] == sent


# --------------------------------------------------------------------------- #
# Reading back
# --------------------------------------------------------------------------- #
def test_an_unreadable_position_list_is_not_an_empty_account():
    """M286: "we could not ask" read as "nothing is open".

    Only one of those means it is safe to open more.
    """
    def opener(url, data=None, headers=None, method=None):
        raise TimeoutError("no answer")

    positions, error = dealing.list_positions(SESSION, DEMO, opener=opener)
    assert positions == []
    assert error, "an unreadable book must report why"


def test_open_positions_are_parsed():
    def opener(url, data=None, headers=None, method=None):
        return ({"positions": [{
            "position": {"dealId": "D1", "direction": "BUY", "size": 2.0,
                         "level": 100.0, "upl": 5.0, "currency": "USD"},
            "market": {"epic": "AAPL"},
        }]}, {})

    positions, error = dealing.list_positions(SESSION, DEMO, opener=opener)
    assert not error
    assert positions[0].deal_id == "D1" and positions[0].epic == "AAPL"
    assert positions[0].size == pytest.approx(2.0)


# --------------------------------------------------------------------------- #
# The separation that makes the read-only client trustworthy
# --------------------------------------------------------------------------- #
def _imported_modules(path: str) -> set[str]:
    """Every module a file imports, however it spells it.

    Parsed rather than grepped. The first version of this searched for the
    word and failed on the client's own docstring, which describes at length
    what it may not do -- so the test was reading prose about the rule as a
    breach of it.
    """
    import ast

    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_the_read_only_client_does_not_import_this():
    """The split is the whole design; a client that imported this could deal."""
    imported = _imported_modules("cfdinvest/broker/capital/client.py")
    assert not any("dealing" in name for name in imported), imported


def test_the_paper_book_cannot_reach_a_broker():
    """The book's worth is being a record nobody could edit afterwards.

    A simulation that sometimes places an order is not that, and CLAUDE.md
    names it as a hard constraint.
    """
    for name in ("papertrade.py", "papertrade_run.py"):
        imported = _imported_modules(f"cfdinvest/advisor/{name}")
        assert not any(
            word in module.lower()
            for module in imported
            for word in ("broker.capital", "dealing", "urllib", "requests")
        ), f"{name} imports {sorted(imported)}"
