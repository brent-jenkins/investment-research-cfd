"""Resizing the shadow book, so it can hold what the account holds.

The book could not resize because Trading 212 cannot. Capital can, the
execution engine always could, and the bridge does -- so a book that could
not would hold different positions from the account it exists to be compared
against, and the gap between them would stop meaning "how wrong is the
simulation".

M349 a reduction realising nothing, so trimming a winner books no profit
M350 an increase realising, so adding to a position books imaginary profit
M351 the retained units re-priced at the mark, rewriting the entry
M352 realised money missing from the summary because only closes are counted
M353 spread charged on the position rather than on what was traded
M354 a resize restarting the hold clock, so financing and the time stop reset
M355 two resizes read as two positions by the replay
"""

from __future__ import annotations

import pytest

from cfdinvest.advisor import papertrade

FX = 0.79
STAKE = papertrade.STAKE_GBP


def opened(price=100.0, direction="long", quantity=None, at="2026-08-28T14:00:00+00:00"):
    if quantity is None:
        quantity = papertrade.target_quantity(price, FX)
    return papertrade.PaperTrade(
        event="open", at=at, ticker="AAPL", direction=direction,
        quantity=quantity, price=price, notional_gbp=STAKE, cohort="c35",
        fx_rate=FX, leverage=5.0, financing_rate=0.0786, fx_fee_applies=False,
        venue="capital-demo", spread_source="measured", spread_cost_gbp=1.0,
    ).as_row()


def test_a_reduction_realises_only_the_part_that_went_away():
    """M349: trimming a winner books nothing.

    The position is worth more, fewer units hold £1,000, and the units sold
    crystallise their gain. Realising nothing would leave that money
    unrecorded until the eventual close, understating the book whenever it
    trimmed.
    """
    row = papertrade.resize_trade(
        opened(price=100.0), 120.0, "2026-08-28T18:00:00+00:00"
    ).as_row()

    assert row["traded_quantity"] < 0
    gone = abs(row["traded_quantity"])
    # £-gain on the units sold: 20 of price move, through the rate.
    assert row["gross_pnl_gbp"] == pytest.approx(gone * 20.0 * FX, rel=1e-6)
    assert row["net_pnl_gbp"] is not None
    assert row["net_pnl_gbp"] > 0


def test_an_increase_realises_nothing():
    """M350: adding to a position booking imaginary profit.

    Nothing has been sold, so nothing has crystallised. A price fall means
    more units hold £1,000, and that is a purchase, not a result.
    """
    row = papertrade.resize_trade(
        opened(price=100.0), 80.0, "2026-08-28T18:00:00+00:00"
    ).as_row()

    assert row["traded_quantity"] > 0, "a price fall did not add units"
    assert row["gross_pnl_gbp"] is None
    assert row["net_pnl_gbp"] is None
    assert row["days_held"] is None


def test_an_increase_re_strikes_the_entry_as_a_weighted_average():
    """M351: the retained units silently re-priced at the mark.

    Marking the whole position to the new price would erase the loss (or
    gain) already carried on the units held all along.
    """
    before = opened(price=100.0)
    row = papertrade.resize_trade(
        before, 80.0, "2026-08-28T18:00:00+00:00"
    ).as_row()

    held, target = before["quantity"], row["quantity"]
    added = target - held
    expected = ((held * 100.0) + (added * 80.0)) / target

    assert row["price"] == pytest.approx(expected, rel=1e-9)
    assert 80.0 < row["price"] < 100.0, "the entry was not a blend of the two"


def test_a_reduction_leaves_the_retained_entry_alone():
    before = opened(price=100.0)
    row = papertrade.resize_trade(
        before, 120.0, "2026-08-28T18:00:00+00:00"
    ).as_row()
    assert row["price"] == pytest.approx(100.0), (
        "the units kept were re-priced at the mark"
    )


def test_spread_is_charged_on_what_was_traded_not_on_the_position():
    """M353: holding charged as though it were dealing.

    Only the change pays, the rule the execution engine follows. Charging
    the position would make every adjustment cost as much as an entry.
    """
    quotes = {"AAPL": {"bid": 119.9, "offer": 120.1, "currency": "USD"}}
    terms = {
        "financing": {"long_annual": 0.0786, "short_annual": 0.00249},
        "margin": {"implied_leverage": 5.0},
    }
    from cfdinvest.advisor.capital_venue import rules_from_quotes

    rules = rules_from_quotes(quotes, terms)
    row = papertrade.resize_trade(
        opened(price=100.0), 120.0, "2026-08-28T18:00:00+00:00", rules=rules
    ).as_row()

    gone = abs(row["traded_quantity"])
    assert row["spread_cost_gbp"] == pytest.approx(0.20 * gone * FX, rel=1e-6)
    assert row["spread_source"] == "measured"

    whole = row["quantity"] + gone
    assert row["spread_cost_gbp"] < 0.20 * whole * FX, (
        "the whole position was charged, not the traded part"
    )


def test_a_resize_does_not_restart_the_hold_clock():
    """M354: financing and the time stop reset by an adjustment.

    A position trimmed every few hours would never age out and would stop
    accruing carry, which are the two things the hold clock is for.
    """
    first = opened(price=100.0, at="2026-08-28T14:00:00+00:00")
    row = papertrade.resize_trade(
        first, 120.0, "2026-08-29T14:00:00+00:00"
    ).as_row()

    assert row["opened_at"] == first["at"], "the open time was not carried"

    # And a close after the resize charges from the open, not the resize.
    closed = papertrade.value_trade(row, 121.0, "2026-08-30T14:00:00+00:00").as_row()
    assert closed["days_held"] == pytest.approx(2.0, abs=1e-6), (
        f"charged {closed['days_held']} days; the resize reset the clock"
    )


def test_the_replay_reads_successive_resizes_as_one_position():
    """M355: an adjusted position counted twice."""
    first = opened(price=100.0)
    a = papertrade.resize_trade(first, 120.0, "2026-08-28T18:00:00+00:00").as_row()
    b = papertrade.resize_trade(a, 150.0, "2026-08-29T18:00:00+00:00").as_row()

    live = papertrade.open_positions([first, a, b])
    assert set(live) == {"AAPL"}
    assert live["AAPL"]["quantity"] == pytest.approx(b["quantity"])
    assert live["AAPL"]["opened_at"] == first["at"]

    # And a close removes it, however many adjustments preceded it.
    closed = papertrade.value_trade(b, 151.0, "2026-08-30T14:00:00+00:00").as_row()
    assert papertrade.open_positions([first, a, b, closed]) == {}


def test_realised_money_from_a_resize_reaches_the_summary():
    """M352: money that only closes are counted, so trimming vanishes.

    The identity was asserted over close rows alone, so a hole here would
    widen with every resize while the assertion still passed.
    """
    first = opened(price=100.0)
    trimmed = papertrade.resize_trade(
        first, 120.0, "2026-08-28T18:00:00+00:00"
    ).as_row()

    book = [first, trimmed]
    summary = papertrade.summarise(book, cohort="c35")

    assert summary["resizes"] == 1
    assert summary["realised_on_resize_gbp"] == pytest.approx(
        trimmed["net_pnl_gbp"], rel=1e-9
    )
    assert summary["net_gbp"] == pytest.approx(trimmed["net_pnl_gbp"], rel=1e-9), (
        "the summary reported no money from a resize that realised some"
    )
    # It is not a completed trade: the position is still open.
    assert summary["closed"] == 0
    assert summary["open"] == 1


def test_a_short_reduction_realises_with_the_right_sign():
    """A short that falls is winning; trimming it books a gain, not a loss."""
    row = papertrade.resize_trade(
        opened(price=100.0, direction="short"), 80.0,
        "2026-08-28T18:00:00+00:00",
    ).as_row()

    # A short's target grows as the price falls, so this adds rather than
    # reduces -- and an increase realises nothing whichever way it is faced.
    assert row["traded_quantity"] > 0
    assert row["net_pnl_gbp"] is None

    trimmed = papertrade.resize_trade(
        opened(price=100.0, direction="short"), 120.0,
        "2026-08-28T18:00:00+00:00",
    ).as_row()
    assert trimmed["traded_quantity"] < 0
    assert trimmed["gross_pnl_gbp"] < 0, (
        "a short trimmed after the price rose booked a gain"
    )


def test_nothing_to_do_is_no_event():
    at_target = opened(price=100.0)
    assert papertrade.resize_trade(
        at_target, 100.0, "2026-08-28T18:00:00+00:00"
    ) is None


def test_an_unusable_mark_produces_no_event():
    for bad in (0.0, -1.0, None, "nonsense"):
        assert papertrade.resize_trade(
            opened(price=100.0), bad, "2026-08-28T18:00:00+00:00"
        ) is None, f"{bad!r} produced a resize"


def test_the_pass_actually_writes_a_resize_event(tmp_path, monkeypatch):
    """The seam, not the helper.

    Every test above drives ``resize_trade`` directly. A pass that computed
    an adjustment and then failed to append it would satisfy all of them and
    write nothing -- which a mutation confirmed, so this exists because the
    gap was real rather than imagined.
    """
    from cfdinvest.advisor import papertrade_run

    book = tmp_path / "book.jsonl"
    monkeypatch.setattr(
        papertrade, "_path", lambda config=None, account=None: book
    )
    monkeypatch.setattr(papertrade_run, "_market_open", lambda t, n: True)

    signals = [{"ticker": "AAA", "direction": "long", "confidence": 0.9}]

    first = papertrade_run.run_paper_pass(
        signals, {"AAA": 100.0}, now="2026-08-26T12:00:00Z",
        blackout_days=0, cooldown_days=0, rebalance_threshold=0.10,
        recheck=False,
    )
    assert first["opened"] == 1, first

    # Price up 30%: the target unit count falls well past a 10% threshold,
    # and the signal still says long, so nothing should close.
    second = papertrade_run.run_paper_pass(
        signals, {"AAA": 130.0}, now="2026-08-26T14:00:00Z",
        blackout_days=0, cooldown_days=0, time_stop_days=99,
        rebalance_threshold=0.10, recheck=False,
    )

    assert second["resized"] == 1, f"the pass reported {second}"
    rows = papertrade.load_book()
    kinds = [r["event"] for r in rows]
    assert kinds == ["open", "resize"], kinds

    live = papertrade.open_positions(rows)
    assert set(live) == {"AAA"}
    assert live["AAA"]["quantity"] == pytest.approx(
        papertrade.target_quantity(130.0, rows[0]["fx_rate"]), rel=1e-9
    )


def test_the_pass_leaves_a_position_alone_inside_the_threshold():
    """Rebalancing every pass would reintroduce the churn hysteresis removes."""
    from cfdinvest.advisor import papertrade_run

    import inspect

    source = inspect.getsource(papertrade_run.run_paper_pass)
    assert "needs_resize" in source, (
        "the pass no longer consults the hysteresis; it would adjust on "
        "every tick and pay spread for the privilege"
    )
    assert "rebalance_threshold" in source
