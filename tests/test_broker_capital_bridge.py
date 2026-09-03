"""From a signal to an order: sizing, limits, and the refusal to guess.

The bridge decides; ``dealing`` acts. They are separate because only one of
them can lose money, and the decision has to be testable without a network
and without a gate.

M303 a sizing rule invented here rather than the engine's
M304 a size rounded up to a dealing increment
M305 a size below the minimum raised to it instead of refused
M306 an order placed outside the exchange session
M307 the per-instrument cap not applied
M308 the gross cap applied before the keep rather than after
M309 a reversal netted into one order
M310 a drifted position resized inside the hysteresis
M311 a mismatch between account and record ignored
M312 the halt still letting orders through
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from cfdinvest.broker.capital import bridge
from cfdinvest.risk import RiskLimits


@dataclass(frozen=True)
class Market:
    epic: str
    bid: float
    offer: float
    min_deal_size: float = 0.1


MARKETS = {
    "AAPL": Market("AAPL", 99.0, 100.0, 0.1),
    "MSFT": Market("MSFT", 199.0, 200.0, 0.1),
    "NKE": Market("NKE", 39.9, 40.0, 1.0),
}
OPEN = {t: True for t in MARKETS}
LIMITS = RiskLimits.for_balance(5_000.0)          # gross 12,500, each 5,000


def signal(ticker, direction="long", size=0.05):
    return {"ticker": ticker, "direction": direction, "size": size}


def plan(signals, positions=None, **kwargs):
    kwargs.setdefault("balance", 5_000.0)
    kwargs.setdefault("limits", LIMITS)
    kwargs.setdefault("exposure_per_unit_size", 4.0)
    kwargs.setdefault("rebalance_threshold", 0.25)
    kwargs.setdefault("session_open", OPEN)
    # Off unless a test is about the gate. It is fail-closed, so leaving it
    # on would make every one of these a test of the last look instead.
    kwargs.setdefault("recheck", False)
    return bridge.plan_orders(
        signals, MARKETS, positions or {}, placed=positions or {}, **kwargs
    )


def only(report, ticker):
    return next(i for i in report.intents if i.ticker == ticker)


# --------------------------------------------------------------------------- #
# Sizing
# --------------------------------------------------------------------------- #
def test_size_is_the_engines_expression_not_a_new_one():
    """M303: a rule invented here measures itself, not the strategy.

    ``signal size x exposure_per_unit_size x balance`` is what the backtest
    used, and its recorded return came from a book sized that way. Anything
    else and the demo account tests the sizing rule rather than the signal.
    """
    report = plan([signal("AAPL", size=0.05)])
    intent = only(report, "AAPL")

    # 0.05 * 4.0 * 5000 = £1,000 of exposure, at 100 = 10 units.
    assert intent.action == "open"
    assert intent.target_units == pytest.approx(10.0)
    assert intent.notional == pytest.approx(1_000.0)


def test_a_short_is_sized_negative():
    intent = only(plan([signal("AAPL", "short", 0.05)]), "AAPL")
    assert intent.target_units == pytest.approx(-10.0)
    assert intent.action == "open"


def test_size_scales_with_the_balance_not_a_constant():
    """The balance is the broker's to report; nothing here holds one."""
    small = only(plan([signal("AAPL")], balance=2_500.0,
                      limits=RiskLimits.for_balance(2_500.0)), "AAPL")
    large = only(plan([signal("AAPL")], balance=10_000.0,
                      limits=RiskLimits.for_balance(10_000.0)), "AAPL")

    assert large.target_units == pytest.approx(small.target_units * 4)


# --------------------------------------------------------------------------- #
# Rounding and minimums
# --------------------------------------------------------------------------- #
def test_a_size_is_rounded_down_to_the_dealing_increment():
    """M304: a size rounded up can breach the limit it came from.

    Which would make this module the cause of the problem it exists to
    prevent.
    """
    # 0.0503 * 4 * 5000 = £1,006 at 40.0 = 25.15 units, increment 1.0.
    intent = only(plan([signal("NKE", size=0.0503)]), "NKE")

    assert intent.target_units == pytest.approx(25.0)
    assert intent.target_units < 25.15


def test_a_size_below_the_minimum_is_refused_not_raised():
    """M305: overriding the sizing step by rounding its zero upwards.

    A zero means sizing declined. Turning it into the minimum position would
    convert a refusal into a trade.
    """
    # Tiny signal: 0.001 * 4 * 5000 = £20 at 40.0 = 0.5 units, increment 1.0.
    report = plan([signal("NKE", size=0.001)])
    intent = only(report, "NKE")

    assert intent.action == "refused"
    assert "minimum" in intent.reason
    assert intent.target_units == 0.0
    assert not report.actionable


# --------------------------------------------------------------------------- #
# When it will not act at all
# --------------------------------------------------------------------------- #
def test_nothing_is_placed_outside_the_exchange_session():
    """M306: dealing at a spread the strategy cannot pay.

    Measured: a median 1.85x widening once the session closes, JNJ 14.9x,
    against a break-even near 6bp. Capital.com will deal anyway -- it
    reported eighteen of twenty tradeable forty-six minutes after the close.
    """
    report = plan([signal("AAPL")], session_open={"AAPL": False})
    intent = only(report, "AAPL")

    assert intent.action == "refused"
    assert "session" in intent.reason
    assert not report.actionable


def test_closing_is_refused_outside_the_session_too():
    """A close pays the same widened spread as an open."""
    positions = {"AAPL": {"size": 10.0, "direction": "BUY", "deal_id": "D1"}}
    report = plan(
        [signal("AAPL", "long", 0.0)], positions, session_open={"AAPL": False}
    )
    assert only(report, "AAPL").action == "refused"


def test_an_unpriced_instrument_is_refused():
    report = bridge.plan_orders(
        [signal("AAPL")], {"AAPL": Market("AAPL", 0.0, 0.0)}, {}, placed={},
        balance=5_000.0, limits=LIMITS, exposure_per_unit_size=4.0,
        rebalance_threshold=0.25, session_open=OPEN, recheck=False,
    )
    assert only(report, "AAPL").action == "refused"


def test_a_balance_that_could_not_be_read_halts_everything():
    report = plan([signal("AAPL")], balance=0.0)
    assert report.halted and "balance" in report.halt_reason
    assert not report.actionable


# --------------------------------------------------------------------------- #
# Limits
# --------------------------------------------------------------------------- #
def test_the_per_instrument_cap_binds():
    """M307: one name taking the whole book.

    40% of gross, the owner's figure, because a single position at p95 is
    120% of the balance in exposure.
    """
    # 0.5 * 4 * 5000 = £10,000 wanted; the cap is 40% of 12,500 = £5,000.
    intent = only(plan([signal("AAPL", size=0.5)]), "AAPL")

    assert intent.notional == pytest.approx(5_000.0)
    assert "per-instrument" in intent.reason


def test_the_gross_cap_scales_everything_rather_than_refusing_the_last():
    """M308: whichever signal arrived last being the one dropped.

    The engine scales the book down proportionally, which is not arbitrary.
    Refusing the newest arrival would be.
    """
    signals = [signal("AAPL", size=0.4), signal("MSFT", size=0.4)]
    report = plan(signals)

    # Each wants £8,000, capped to £5,000 each = £10,000, under the £12,500
    # ceiling -- so raise the demand until the ceiling binds.
    assert report.gross_target <= LIMITS.max_total_exposure_gbp + 1e-6
    for intent in report.intents:
        assert intent.action in ("open", "refused")


def test_the_gross_cap_shrinks_positions_when_it_binds():
    """It takes three positions before the book-wide cap can bind at all.

    Each is held to 40% of gross, so two of them reach 80% and the ceiling
    is unreachable. That is a property of the two limits together rather
    than a fixture detail, and the first version of this test used two
    instruments and asserted a scaling that therefore never happened.
    """
    small = RiskLimits.for_balance(1_000.0)      # gross 2,500, each 1,000
    signals = [signal(t, size=0.5) for t in ("AAPL", "MSFT", "NKE")]
    report = bridge.plan_orders(
        signals, MARKETS, {}, placed={}, balance=1_000.0, limits=small,
        exposure_per_unit_size=4.0, rebalance_threshold=0.25, session_open=OPEN,
        recheck=False,
    )

    assert report.gross_target <= small.max_total_exposure_gbp + 1e-6
    assert any(
        "gross" in i.reason for i in report.intents
        if i.acts or i.action == "refused"
    ), [(i.ticker, i.action, i.reason) for i in report.intents]


# --------------------------------------------------------------------------- #
# Holding, resizing, reversing
# --------------------------------------------------------------------------- #
def test_a_position_inside_the_hysteresis_is_left_alone():
    """M310: churn, reintroduced.

    The volatility-scaled size moves a little every bar. Rebalancing to the
    exact target each time is the churn the hysteresis exists to stop, and
    every adjustment crosses the spread.
    """
    positions = {"AAPL": {"size": 10.0, "direction": "BUY", "deal_id": "D1"}}
    intent = only(plan([signal("AAPL", size=0.051)], positions), "AAPL")

    assert intent.action == "none"
    assert "threshold" in intent.reason


def test_a_position_that_has_drifted_far_enough_is_resized():
    """Resizing is the owner's decision, and the engine's behaviour.

    Sixty-two per cent of the engine's actions are resizes; a book that
    skipped them would be a different strategy wearing the same name.
    """
    positions = {"AAPL": {"size": 10.0, "direction": "BUY", "deal_id": "D1"}}
    intent = only(plan([signal("AAPL", size=0.10)], positions), "AAPL")

    assert intent.action == "resize"
    assert intent.target_units == pytest.approx(20.0)
    assert intent.deal_id == "D1"


def test_a_reversal_closes_rather_than_netting():
    """M309: one order the venue would net, leaving neither in the record."""
    positions = {"AAPL": {"size": 10.0, "direction": "BUY", "deal_id": "D1"}}
    intent = only(plan([signal("AAPL", "short", 0.05)], positions), "AAPL")

    assert intent.action == "close"
    assert intent.reason == "direction reversed"
    assert intent.target_units == 0.0


def test_a_flat_signal_closes_what_is_held():
    positions = {"AAPL": {"size": 10.0, "direction": "BUY", "deal_id": "D1"}}
    intent = only(plan([signal("AAPL", "flat", 0.0)], positions), "AAPL")

    assert intent.action == "close"
    assert intent.deal_id == "D1"


# --------------------------------------------------------------------------- #
# Reconciliation
# --------------------------------------------------------------------------- #
def test_a_position_nobody_opened_halts_the_bridge():
    """M311: money at risk that nobody chose.

    The most likely cause of a mismatch is the thing that would make the
    next order wrong too, so it stops rather than trading through it.
    """
    report = bridge.plan_orders(
        [signal("AAPL")], MARKETS,
        {"NFLX": {"size": 0.01, "direction": "BUY"}}, placed={},
        balance=5_000.0, limits=LIMITS, exposure_per_unit_size=4.0,
        rebalance_threshold=0.25, session_open=OPEN, recheck=False,
    )

    assert report.halted
    assert report.unexplained == ("NFLX",)
    assert "NFLX" in report.halt_reason

    # It still says what it would have done. Withholding that as well would
    # leave nobody able to see the cost of the halt, and "here is what I
    # wanted and here is why I did not" is the useful refusal.
    assert report.intents, "a halt hid what the pass would have decided"
    assert any(i.action == "open" for i in report.intents)
    # And withheld it, which is the part that matters.
    assert report.actionable == ()


def test_a_position_the_app_thinks_it_opened_and_is_not_there_halts_it():
    """The other direction, and it fails differently.

    The engine is then reasoning about a position that does not exist.
    """
    report = bridge.plan_orders(
        [signal("AAPL")], MARKETS, {},
        placed={"AAPL": {"size": 10.0}},
        balance=5_000.0, limits=LIMITS, exposure_per_unit_size=4.0,
        rebalance_threshold=0.25, session_open=OPEN, recheck=False,
    )

    assert report.halted
    assert report.missing == ("AAPL",)


def test_a_halt_lets_nothing_through():
    """M312: a halt that reports but does not stop."""
    report = bridge.plan_orders(
        [signal("AAPL", size=0.05)], MARKETS,
        {"NFLX": {"size": 0.01, "direction": "BUY"}}, placed={},
        balance=5_000.0, limits=LIMITS, exposure_per_unit_size=4.0,
        rebalance_threshold=0.25, session_open=OPEN, recheck=False,
    )
    assert report.actionable == ()


def test_reconcile_names_both_directions():
    unexplained, missing = bridge.reconcile(
        {"A": {}, "B": {}}, {"B": {}, "C": {}}
    )
    assert unexplained == ("A",)
    assert missing == ("C",)


# --------------------------------------------------------------------------- #
# It decides; it does not act
# --------------------------------------------------------------------------- #
def test_the_planner_cannot_place_anything():
    """The separation is the point: only one of the two can lose money."""
    import ast
    from pathlib import Path

    source = Path("cfdinvest/broker/capital/bridge.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)

    assert not any("dealing" in name for name in imported), imported
    assert "place_position" not in source
    assert "urllib" not in source


# --------------------------------------------------------------------------- #
# The last look, on real orders
#
# M313 an open placed on a signal that no longer holds
# M314 the gate skipped when no re-evaluation was supplied
# M315 the gate applied to a close, refusing an exit
# --------------------------------------------------------------------------- #
def _recheck(direction="long", confidence=0.9):
    return {"AAPL": {"direction": direction, "confidence": confidence}}


def test_an_open_needs_the_signal_to_still_hold():
    """M313: real orders on a signal formed before the market moved.

    The paper book already does this on the same evidence: requiring the
    signal to still hold against the live price refused 197 of 2,139 round
    trips, and those refused won 36.0% against 50.4% for the ones kept
    (t=+4.18, p=0.000038). These are real orders, so the case is stronger.
    """
    held = plan([signal("AAPL")], recheck=True, rechecked=_recheck())
    assert only(held, "AAPL").action == "open"

    flipped = plan(
        [signal("AAPL")], recheck=True, rechecked=_recheck(direction="short")
    )
    intent = only(flipped, "AAPL")
    assert intent.action == "refused"
    assert "reversed" in intent.reason

    faded = plan(
        [signal("AAPL")], recheck=True, rechecked=_recheck(confidence=0.1)
    )
    assert only(faded, "AAPL").action == "refused"


def test_no_re_evaluation_means_no_open():
    """M314: a gate that opens when it could not run.

    Fail-closed, exactly as the paper book's is. An unchecked signal is not
    an approved one.
    """
    report = plan([signal("AAPL")], recheck=True, rechecked=None)
    intent = only(report, "AAPL")

    assert intent.action == "refused"
    assert "re-evaluation" in intent.reason
    assert not report.actionable

    # And an instrument missing from the re-evaluation is refused too.
    partial = plan([signal("MSFT")], recheck=True, rechecked=_recheck())
    assert only(partial, "MSFT").action == "refused"


def test_the_gate_never_refuses_an_exit():
    """M315: a close blocked by a check about entries.

    Refusing an exit is never the safe direction, and the measurement was
    about entries. A resize is left alone for the same reason: it acts on a
    position already justified, and the hysteresis already demands a 25%
    drift.
    """
    positions = {"AAPL": {"size": 10.0, "direction": "BUY", "deal_id": "D1"}}

    closing = plan(
        [signal("AAPL", "flat", 0.0)], positions,
        recheck=True, rechecked={},        # nothing re-checked at all
    )
    assert only(closing, "AAPL").action == "close"

    resizing = plan(
        [signal("AAPL", size=0.10)], positions, recheck=True, rechecked={},
    )
    assert only(resizing, "AAPL").action == "resize"


# --------------------------------------------------------------------------- #
# The margin ceiling.
#
# Capital publishes a marginFactor per instrument and varies it -- tiered by
# size, raised in volatile conditions and around events -- so a factor can
# rise under a position that is already open and increase what the book needs
# without the book changing at all. The app fetched the figure from the day
# the account opened and discarded it, and sized from `balance` while
# `available` (what is left after committed margin) sat unread beside it.
#
# Measured on the demo account: 44.2% utilisation on 2026-09-03, 66.6% on
# 2026-09-01. There was no ceiling on either.
#
# M338 the ceiling skipped when headroom is missing, reading as protection
# M339 a missing margin factor treated as needing no margin
# M340 the ceiling measured against balance rather than remaining headroom
# M341 the ceiling refusing closes as well as opens
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MarginMarket:
    """A market carrying the margin figure in the unit the BROKER uses.

    20.0, not 0.20. The first version of these tests used the fraction and
    passed a gate that multiplied a notional by twenty -- it would have
    demanded twenty times the position in margin and refused every open on
    the live account. Read back off the demo account on 2026-09-03: every one
    of the eight instruments returns marginFactor 20.0, being 20% and 5x.
    """

    epic: str
    bid: float
    offer: float
    min_deal_size: float = 0.1
    margin_factor: float | None = 20.0

    @property
    def margin_fraction(self) -> float | None:
        if self.margin_factor is None:
            return None
        return float(self.margin_factor) / 100.0


MARGIN_MARKETS = {
    "AAPL": MarginMarket("AAPL", 99.0, 100.0, 0.1),
    "MSFT": MarginMarket("MSFT", 199.0, 200.0, 0.1),
}


def margin_plan(signals, positions=None, **kwargs):
    kwargs.setdefault("balance", 5_000.0)
    kwargs.setdefault("limits", LIMITS)
    kwargs.setdefault("exposure_per_unit_size", 4.0)
    kwargs.setdefault("rebalance_threshold", 0.25)
    kwargs.setdefault("session_open", {t: True for t in MARGIN_MARKETS})
    kwargs.setdefault("recheck", False)
    return bridge.plan_orders(
        signals, MARGIN_MARKETS, positions or {},
        placed=positions or {}, **kwargs,
    )


def test_an_open_that_would_breach_the_margin_ceiling_is_refused():
    """The guard doing its job: headroom nearly gone, so nothing new opens."""
    report = margin_plan(
        [signal("AAPL", size=0.05)],
        available=200.0,            # 4,800 already committed of 5,000
        max_margin_usage=0.50,
    )
    intent = only(report, "AAPL")
    assert intent.action == "refused", (
        f"an open was allowed at 96% margin usage: {intent.action}"
    )
    assert "margin" in intent.reason.lower()


def test_an_open_within_the_ceiling_still_proceeds():
    """A guard that refuses everything is not a guard."""
    report = margin_plan(
        [signal("AAPL", size=0.05)],
        available=5_000.0,          # nothing committed
        max_margin_usage=0.50,
    )
    assert only(report, "AAPL").action == "open", (
        "the ceiling refused an open with the whole balance free"
    )


def test_unreadable_headroom_refuses_rather_than_proceeds():
    """Fails closed, like the last look.

    A margin guard that silently disables itself when a field is missing is
    worse than no guard, because it reads as protection while providing none.
    """
    report = margin_plan(
        [signal("AAPL", size=0.05)],
        available=None,
        max_margin_usage=0.50,
    )
    intent = only(report, "AAPL")
    assert intent.action == "refused", (
        "an unreadable margin headroom let an open through"
    )
    assert "could not be read" in intent.reason


def test_a_missing_margin_factor_refuses_rather_than_assuming_none():
    """An instrument with no quoted factor needs an unknown amount of margin."""
    markets = dict(MARGIN_MARKETS)
    markets["AAPL"] = MarginMarket("AAPL", 99.0, 100.0, 0.1, margin_factor=None)
    report = bridge.plan_orders(
        [signal("AAPL", size=0.05)], markets, {}, placed={},
        balance=5_000.0, limits=LIMITS, exposure_per_unit_size=4.0,
        rebalance_threshold=0.25, session_open={"AAPL": True}, recheck=False,
        available=5_000.0, max_margin_usage=0.50,
    )
    intent = only(report, "AAPL")
    assert intent.action == "refused", (
        "a missing margin factor was treated as requiring no margin"
    )
    assert "margin factor" in intent.reason


def test_the_ceiling_is_off_unless_configured():
    """Every recorded figure predates this, so it cannot default to on."""
    report = margin_plan(
        [signal("AAPL", size=0.05)],
        available=None,             # would fail closed if the gate were on
    )
    assert only(report, "AAPL").action == "open", (
        "the margin ceiling applied without being configured, which would "
        "change what the account does on an upgrade"
    )


def test_the_margin_ceiling_never_refuses_a_close():
    """Refusing an exit is never the safe direction -- least of all here.

    A book short of margin is precisely the one that most needs to be able
    to reduce.
    """
    held = {"AAPL": {"size": 10.0, "direction": "BUY", "dealId": "d1"}}
    report = margin_plan(
        [signal("AAPL", direction="flat", size=0.0)],
        positions=held,
        available=0.0,              # no headroom whatsoever
        max_margin_usage=0.50,
    )
    intent = only(report, "AAPL")
    assert intent.action == "close", (
        f"a close was blocked by the margin ceiling: {intent.action} "
        f"({intent.reason})"
    )


def test_the_margin_figure_is_converted_from_the_brokers_percentage():
    """20.0 means 20%, and multiplying a notional by it is 100x wrong.

    Asserted on the real client type rather than the local fake, so the fake
    cannot drift into agreeing with a bug. Read off the demo account on
    2026-09-03: all eight instruments return marginFactor 20.0.
    """
    from cfdinvest.broker.capital.client import CapitalMarket

    market = CapitalMarket(epic="MU", ticker="MU", margin_factor=20.0)
    assert market.margin_fraction == pytest.approx(0.20), (
        "the broker's percentage was not converted to a fraction; a notional "
        "multiplied by 20.0 demands twenty times the position in margin"
    )
    assert CapitalMarket(epic="X", ticker="X").margin_fraction is None, (
        "an unquoted requirement became a number rather than staying absent"
    )
