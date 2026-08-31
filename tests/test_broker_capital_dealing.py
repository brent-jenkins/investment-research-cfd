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


def test_the_brokers_tradeable_flag_is_not_a_trading_gate():
    """M287: the venue saying yes is not the same as it being sensible.

    At 20:46 UTC, forty-six minutes after the US session closed, Capital.com
    reported eighteen of twenty instruments as ``TRADEABLE``. It will happily
    deal them -- at a median of 1.85x the in-session spread, and JNJ at 83.3bp
    against 5.6bp in session.

    So the gate is the exchange session, not the broker's flag. Owner's
    decision, 2026-08-27, taken on that measurement plus the finding that the
    signal does not predict the overnight move at all (49.4% right, p=0.375).

    Asserted structurally because the mistake is so easy to make: the field is
    right there in the market details, it is called ``tradable``, and reading
    it as permission is the obvious thing to do.
    """
    dealing_source = Path("cfdinvest/broker/capital/dealing.py").read_text(
        encoding="utf-8"
    )
    assert "tradable" not in dealing_source, (
        "the dealing module reads the broker's tradeable flag; the session "
        "calendar is what decides whether to trade"
    )
    assert "marketStatus" not in dealing_source


def test_the_session_calendar_is_the_thing_that_says_when():
    """And it says the regular session, for US equities.

    The paper book already gates on this. Whatever places orders must gate on
    the same one owner, or the two books diverge on which bars they traded.
    """
    import pandas as pd

    from cfdinvest.schedule.market_calendar import describe

    # 20:46 UTC, when Capital reported almost everything as tradeable.
    after = describe(["AAPL", "JNJ"], pd.Timestamp("2026-08-27 20:46", tz="UTC"))
    assert not after["is_open"].any(), "the calendar thinks the session is open"

    during = describe(["AAPL", "JNJ"], pd.Timestamp("2026-08-27 15:00", tz="UTC"))
    assert during["is_open"].all(), "the calendar thinks the session is shut"


# --------------------------------------------------------------------------- #
# M301 the test suite itself armed to trade
# --------------------------------------------------------------------------- #
def test_the_suite_cannot_deal_even_after_loading_a_config():
    """M301: the gate open inside every test process.

    ``load_dotenv`` exports the real .env into ``os.environ`` and
    ``load_config`` calls it by default, so once any test loaded a config the
    live credentials were in the process -- and with them
    CFD_CAPITAL_DEALING_ENABLED=1. Measured directly: the dealing gate then
    returned no refusal at all.

    Nothing in the suite calls place_position without an explicit config, so
    nothing traded. That is luck, not design, and a suite one careless test
    away from placing a real order is not offline.
    """
    from cfdinvest.config import load_config

    load_config()

    from cfdinvest.broker.capital.client import CapitalConfig
    from cfdinvest.broker.capital.dealing import DealingConfig

    assert not CapitalConfig.from_env().complete, (
        "live broker credentials are readable inside the test suite"
    )
    assert DealingConfig.from_env().refusal(), (
        "the dealing gate is OPEN inside the test suite"
    )


def test_a_default_configured_order_is_refused_in_the_suite():
    """The property that matters, exercised rather than inferred.

    Calling place_position with no config at all -- the way a careless test
    or a stray script would -- must refuse rather than reach the network.
    """
    from cfdinvest.broker.capital.client import CapitalSession

    result = dealing.place_position(
        "AAPL", "BUY", 1.0, CapitalSession(cst="c", security_token="t")
    )
    assert not result.ok
    assert dealing.DEALING_VAR in result.error


# --------------------------------------------------------------------------- #
# M-live: every successful close reported as a failure
# --------------------------------------------------------------------------- #
# On 2026-08-31 eight stranded positions were closed correctly and the pass
# mailed "0 placed, 8 failed" under the subject "FAILED -- the account may
# not match the record". The account matched the record exactly. The broker
# confirmed all eight closes and the order store recorded them CLOSED with
# no error.
#
# ``ok`` was written for opens -- status OPEN with a level -- so a close,
# which confirms as CLOSED and carries no open level, could never satisfy
# it. One property, two meanings, and the wrong one applied to half the
# actions.
#
# A false alarm here is worse than it sounds. This is the alert that says
# whether the book and the broker agree, and nobody investigates being told
# the worse news, so a wrong FAILED survives where a wrong OK gets caught.


def test_a_confirmed_close_is_a_success_not_a_failure():
    """The reported bug, at the property that caused it."""
    assert dealing.DealResult(status="CLOSED").ok, (
        "a confirmed close reports as a failure, so the pass mails FAILED "
        "for work that succeeded"
    )


def test_a_close_that_did_not_happen_is_still_a_failure():
    """The half that must survive the fix.

    Making closes succeed is worthless if it also silences a close that
    genuinely did not happen -- and this is the more dangerous direction,
    because a position believed shut is a position nobody is watching.
    """
    for error in (
        "POSITION STILL OPEN -- close refused: dealing is not enabled",
        "POSITION STILL OPEN -- HTTP 403 closing abc",
        "POSITION MAY STILL BE OPEN -- unconfirmed close (timeout)",
    ):
        assert not dealing.DealResult(status="CLOSED", error=error).ok, error
    # And a broker rejection keeps its own word rather than becoming CLOSED,
    # which is what makes reading the status safe at all.
    assert not dealing.DealResult(status="REJECTED").ok


def test_an_open_still_needs_a_fill_price():
    """M281, which the asymmetry must not weaken.

    A deal reference is not a fill. An open with no level is a request the
    broker has not turned into a position, and the close case must not have
    relaxed that.
    """
    assert not dealing.DealResult(status="OPEN").ok
    assert dealing.DealResult(status="OPEN", level=101.5).ok


def test_the_two_actions_normalise_success_to_different_words():
    """Why reading the status is safe, asserted rather than assumed.

    ``ok`` distinguishes an open from a close by status alone. That only
    holds because each function normalises ITS OWN success and leaves every
    other outcome as the broker said it -- so a rejection can never arrive
    wearing the other action's success word.
    """
    import inspect

    opening = inspect.getsource(dealing.place_position)
    closing = inspect.getsource(dealing.close_position)
    assert 'status="OPEN" if status.upper() in' in opening
    assert 'status="CLOSED" if status.upper() in' in closing
    # Neither normaliser may claim the other's word.
    assert '"CLOSED"' not in opening.split("status=")[-1].split("\n")[0]
    assert '"OPEN"' not in closing.split("status=")[-1].split("\n")[0]


# --------------------------------------------------------------------------- #
# M-live: a derived field, persisted, outlived the code that derived it
# --------------------------------------------------------------------------- #
# The DealResult.ok bug above did something worse than send a wrong mail. It
# wrote eight close records with `ok: false` beside `status: "CLOSED"` and an
# empty error. open_orders skipped anything not `ok`, so the record went on
# believing eight closed positions were held; reconcile found them missing
# from the account; the bridge halted and stopped opening anything. Four
# positions the engine wanted went untaken while the market was open.
#
# Correcting DealResult.ok could not fix that, because the rows were already
# on disk. The record has to survive a change in the code that wrote it.


def test_a_close_recorded_by_the_buggy_version_still_reads_as_closed():
    """The exact rows found on the deployment, byte for byte."""
    from cfdinvest.persist.orders import _confirmed

    poisoned = {"kind": "result", "action": "close", "epic": "NKE",
                "ok": False, "status": "CLOSED", "error": ""}
    assert _confirmed(poisoned), (
        "a close the broker confirmed is still read as not having happened, "
        "so the record keeps a closed position alive and halts the bridge"
    )


def test_a_close_that_failed_is_not_resurrected_as_a_success():
    """The dangerous direction, and the one that must not loosen.

    Reconciliation exists to stop trading when the account and the record
    disagree. Reading the observation must not turn a close that did not
    happen into one that did -- that would hide a real divergence.
    """
    from cfdinvest.persist.orders import _confirmed

    assert not _confirmed({"action": "close", "ok": False, "status": "CLOSED",
                           "error": "POSITION STILL OPEN -- HTTP 403 closing x"})
    assert not _confirmed({"action": "close", "ok": True, "status": "REJECTED",
                           "error": ""})


def test_an_open_still_needs_a_fill_price_in_the_record_too():
    """A deal reference is not a position, on the read side as well.

    Recording an unfilled reference as a holding puts the record ahead of
    the account, which makes reconciliation report the account as missing
    something that was never there.
    """
    from cfdinvest.persist.orders import _confirmed

    assert _confirmed({"action": "open", "status": "OPEN", "level": 155.79,
                       "error": "", "ok": True})
    assert not _confirmed({"action": "open", "status": "OPEN", "level": None,
                           "error": "", "ok": True})


def test_a_row_with_no_status_falls_back_to_the_stored_flag():
    """Absence named, not inferred.

    A row carrying no observation at all is the one case where the stored
    boolean is the only evidence there is. Distinct from a row whose
    observation merely disagrees with it.
    """
    from cfdinvest.persist.orders import _confirmed

    assert _confirmed({"action": "open", "ok": True})
    assert not _confirmed({"action": "open", "ok": False})


def test_eight_closes_clear_their_opens_end_to_end(tmp_path, monkeypatch):
    """The property, through open_orders rather than the helper.

    Eleven opens, eight of them later closed by the buggy version. What the
    app believes it holds must be the remaining three -- which is what the
    account actually held.
    """
    from cfdinvest.persist import orders as store

    path = tmp_path / "orders" / "orders.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(store, "_path", lambda config=None, account=None: path)

    held = ["AAPL", "ADBE", "AMD", "CAT", "COST", "GOOGL", "META", "MSFT",
            "NKE", "ORCL", "XOM"]
    closed = ["AAPL", "ADBE", "COST", "GOOGL", "META", "NKE", "ORCL", "XOM"]
    lines = [
        {"kind": "result", "action": "open", "epic": epic, "ok": True,
         "status": "OPEN", "level": 100.0, "error": "", "units": 1.0,
         "direction": "SELL"}
        for epic in held
    ] + [
        # As the buggy version wrote them: confirmed CLOSED, flagged not ok.
        {"kind": "result", "action": "close", "epic": epic, "ok": False,
         "status": "CLOSED", "error": ""}
        for epic in closed
    ]
    with path.open("w", encoding="utf-8") as handle:
        for line in lines:
            handle.write(json.dumps(line) + "\n")

    live = store.open_orders()

    assert sorted(live) == ["AMD", "CAT", "MSFT"], (
        f"the record believes it holds {sorted(live)}; the account held "
        "AMD, CAT and MSFT, and the difference halts the bridge"
    )
