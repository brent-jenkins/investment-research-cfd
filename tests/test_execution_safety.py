"""Trading 212's own limits, and checking a fill after the fact.

Two things the rest of the helper cannot do. It measures a trade against
*your* limits, which is only half the question: a trade can be inside every
one of them and still be rejected by the ticket for having three decimal
places, and Trading 212 rejects some of those silently.

The other half is after the money is on. A fill is a fact, and the only
question worth asking of it is whether it is still the trade you checked.
"""

from __future__ import annotations

import pandas as pd
import pytest

from cfdinvest.livehelper import LiveHelperConfig, MarketState, parse_trade_intent
from cfdinvest.livehelper.confirm_checks import (
    review_confirmation,
    validate_confirmation_deviation,
    validate_confirmed_margin,
    validate_confirmed_quantity,
    validate_confirmed_slippage,
)
from cfdinvest.livehelper.safety import evaluate_trade_safety
from cfdinvest.livehelper.t212_rules import (
    InstrumentRules,
    RuleTable,
    describe_coverage,
    load_rules,
    round_to_precision,
    rules_from_mapping,
    validate_close_only,
    validate_quantity,
    validate_trading_hours,
)

NOW = pd.Timestamp("2026-08-25 12:00", tz="UTC")


def spx(**changes) -> InstrumentRules:
    fields = dict(
        quantity_precision=2, min_quantity=0.1, max_quantity=100.0, leverage=20.0
    )
    fields.update(changes)
    return InstrumentRules(ticker="SPX500", **fields)


def table(**instruments) -> RuleTable:
    return RuleTable(rules={k: v for k, v in instruments.items()})


def checks(issues) -> list[str]:
    return [issue.check for issue in issues]


def severities(issues) -> list[str]:
    return [issue.severity for issue in issues]


# --------------------------------------------------------------------------- #
# The table
# --------------------------------------------------------------------------- #
def test_an_instrument_with_no_rules_is_empty_not_permissive():
    """The distinction the whole module turns on."""
    rules = InstrumentRules(ticker="NAS100")
    assert rules.empty
    assert rules.known == ()


def test_a_partial_entry_is_kept():
    """Better than none: it makes the checks it covers real."""
    rules = InstrumentRules(ticker="GLD", min_quantity=0.5)
    assert rules.known == ("min_quantity",)
    assert not rules.empty


def test_rules_are_verified_only_when_you_say_where_they_came_from():
    assert not spx().verified
    assert spx(source="T212 ticket, 2026-08-25").verified


def test_a_negative_bound_is_refused():
    with pytest.raises(ValueError, match="must be a positive number"):
        InstrumentRules(ticker="X", min_quantity=-1)


def test_a_minimum_above_the_maximum_is_refused():
    with pytest.raises(ValueError, match="above max_quantity"):
        InstrumentRules(ticker="X", min_quantity=10.0, max_quantity=1.0)


def test_an_absurd_precision_is_refused():
    with pytest.raises(ValueError, match="quantity_precision must be 0-8"):
        InstrumentRules(ticker="X", quantity_precision=20)


def test_asking_for_an_unknown_instrument_gives_an_empty_set_not_none():
    """A caller that had to check for None would eventually forget, and the
    forgetting would read as "no constraint"."""
    assert table(SPX500=spx()).get("NAS100").empty


def test_the_lookup_is_case_insensitive():
    assert not table(SPX500=spx()).get("spx500").empty


def test_a_malformed_entry_does_not_lose_the_others():
    """One bad line in a hand-written file must not downgrade everything."""
    built = rules_from_mapping(
        {
            "SPX500": {"quantity_precision": 2},
            "BROKEN": {"min_quantity": -1},
            "ALSO_BROKEN": "not a table",
        }
    )
    assert built.tickers == ("SPX500",)


def test_an_unknown_field_is_ignored_not_fatal():
    built = rules_from_mapping({"SPX500": {"min_quantity": 0.1, "invented": 1}})
    assert built.get("SPX500").min_quantity == pytest.approx(0.1)


def test_a_missing_file_is_the_normal_state(tmp_path):
    """Nobody has one until they write one."""
    built = load_rules(tmp_path / "absent.toml")
    assert len(built) == 0
    assert built.path is not None


def test_a_toml_file_round_trips(tmp_path):
    path = tmp_path / "rules.toml"
    path.write_text(
        '[SPX500]\nquantity_precision = 2\nmin_quantity = 0.1\n'
        'source = "T212 ticket"\n',
        encoding="utf-8",
    )
    built = load_rules(path)
    assert built.get("SPX500").quantity_precision == 2
    assert built.get("SPX500").verified


def test_a_json_file_works_too(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text('{"SPX500": {"min_quantity": 0.1}}', encoding="utf-8")
    assert load_rules(path).get("SPX500").min_quantity == pytest.approx(0.1)


def test_an_unparseable_file_does_not_raise(tmp_path):
    path = tmp_path / "rules.toml"
    path.write_text("this is not toml [[[", encoding="utf-8")
    assert len(load_rules(path)) == 0


def test_coverage_is_reported_as_a_fraction_not_a_pass():
    """A table with one instrument in it protects one instrument."""
    result = describe_coverage(table(SPX500=spx()), ["SPX500", "NAS100"])
    assert result["coverage"] == pytest.approx(0.5)
    assert result["uncovered"] == ("NAS100",)


# --------------------------------------------------------------------------- #
# Quantity
# --------------------------------------------------------------------------- #
def test_a_good_quantity_passes():
    assert validate_quantity(0.68, spx()) == []


def test_too_many_decimal_places_is_caught():
    """Trading 212 rejects this silently: the ticket simply does not fill."""
    issues = validate_quantity(0.684, spx())
    assert checks(issues) == ["quantity_precision"]
    assert "0.68" in issues[0].recommendation


def test_the_suggested_size_is_rounded_down():
    """Rounding up would undo the limit that produced the number."""
    assert round_to_precision(0.689, 2) == pytest.approx(0.68)
    assert round_to_precision(0.681, 2) == pytest.approx(0.68)


def test_an_unknown_precision_rounds_to_nothing_rather_than_zero_places():
    assert round_to_precision(0.684, None) is None


def test_below_the_minimum_says_do_not_round_up():
    """The common case, and the dangerous advice.

    Your risk limit says 0.04 and the instrument will not go below 0.1. That
    means the trade cannot be taken at your risk limit, not that the size
    should be raised to meet it.
    """
    issues = validate_quantity(0.05, spx())
    assert checks(issues) == ["min_quantity"]
    assert "Do not round up" in issues[0].recommendation


def test_above_the_maximum_is_caught():
    issues = validate_quantity(500.0, spx())
    assert checks(issues) == ["max_quantity"]


def test_a_size_that_rounds_to_zero_is_caught():
    issues = validate_quantity(0.004, spx(min_quantity=0.001))
    assert "quantity_precision" in checks(issues)
    assert any("rounds to zero" in issue.message for issue in issues)


def test_no_quantity_at_all_is_refused():
    assert severities(validate_quantity(None, spx())) == ["critical"]
    assert severities(validate_quantity(0.0, spx())) == ["critical"]


def test_an_instrument_with_no_size_rules_reports_that_rather_than_passing():
    """"Not checked" and "fine" are different answers."""
    issues = validate_quantity(0.684, InstrumentRules(ticker="NAS100"))
    assert checks(issues) == ["quantity_rules"]
    assert severities(issues) == ["info"]


# --------------------------------------------------------------------------- #
# Close-only and hours
# --------------------------------------------------------------------------- #
def test_a_normal_instrument_raises_nothing():
    assert validate_close_only(spx()) == []


def test_close_only_blocks_an_opening_order():
    """The worst shape a rejection can take: you believe you are in a
    position you are not."""
    issues = validate_close_only(spx(close_only=True))
    assert severities(issues) == ["critical"]


def test_close_only_still_allows_closing():
    issues = validate_close_only(spx(close_only=True), closing=True)
    assert severities(issues) == ["info"]


def test_inside_the_trading_window_raises_nothing():
    assert validate_trading_hours(spx(trading_hours="08:00-16:00"), NOW) == []


def test_outside_the_window_is_a_warning_not_a_block():
    """The platform is the authority; the window is a note you made."""
    issues = validate_trading_hours(spx(trading_hours="08:00-11:00"), NOW)
    assert severities(issues) == ["warning"]


def test_a_window_that_wraps_midnight_works():
    """23:00-22:00 is how an index CFD trades: almost all day, short break."""
    assert validate_trading_hours(spx(trading_hours="23:00-22:00"), NOW) == []
    assert severities(
        validate_trading_hours(
            spx(trading_hours="23:00-22:00"),
            pd.Timestamp("2026-08-25 22:30", tz="UTC"),
        )
    ) == ["warning"]


def test_unreadable_hours_say_so_rather_than_blocking():
    issues = validate_trading_hours(spx(trading_hours="whenever"), NOW)
    assert severities(issues) == ["info"]


def test_no_recorded_hours_raises_nothing():
    assert validate_trading_hours(spx(), NOW) == []


# --------------------------------------------------------------------------- #
# Folded into the verdict
# --------------------------------------------------------------------------- #
def intent(text="long SPX500 0.68 entry=4434.8 stop=4390 tp=4520 lev=20"):
    return parse_trade_intent(text)


def assess(text=None, rules=None, **market):
    return evaluate_trade_safety(
        intent(text) if text else intent(),
        LiveHelperConfig(account_balance_gbp=100_000.0,
                         max_exposure_per_instrument_gbp=100_000.0,
                         max_fx_exposure_pct=1.0),
        MarketState(**market),
        rules=rules,
        now=NOW,
    )


def test_a_broker_rejection_reaches_the_verdict():
    """One verdict covers both kinds of constraint. Two separate answers
    would let you act on the reassuring one."""
    result = assess(
        "long SPX500 0.684 entry=4434.8 stop=4390 tp=4520 lev=20",
        rules=table(SPX500=spx()),
    )
    assert result.status == "UNSAFE"
    assert "t212_quantity_precision" in [f.check for f in result.findings]


def test_a_clean_trade_stays_safe_with_rules_supplied():
    assert assess(rules=table(SPX500=spx())).status == "SAFE"


def test_no_rules_table_does_not_downgrade_every_trade():
    """A verdict that is always BORDERLINE is a verdict nobody reads.

    Nobody has a rules table until they write one, so an unknown here would
    cost more than it protects.
    """
    result = assess(rules=None)
    assert result.status == "SAFE"
    assert "broker_rules" in [f.check for f in result.findings]
    assert result.unknowns == ()


def test_a_table_that_lacks_this_instrument_is_an_unknown():
    """You opted into these checks, and this one could not run."""
    result = assess(rules=table(GLD=InstrumentRules(ticker="GLD")))
    assert result.status == "BORDERLINE"
    assert any("SPX500" in unknown for unknown in result.unknowns)


def test_close_only_makes_a_trade_unsafe():
    result = assess(rules=table(SPX500=spx(close_only=True)))
    assert result.status == "UNSAFE"


# --------------------------------------------------------------------------- #
# After the fill
# --------------------------------------------------------------------------- #
def filled(**changes):
    arguments = dict(
        price=4434.8,
        quantity=0.68,
        recommended_price=4434.8,
        recommended_quantity=0.68,
        stop_loss=4390.0,
        take_profit=4520.0,
        instrument_leverage=20.0,
        config=LiveHelperConfig(
            account_balance_gbp=100_000.0,
            max_exposure_per_instrument_gbp=100_000.0,
        ),
    )
    arguments.update(changes)
    return review_confirmation(**arguments)


def test_a_fill_as_planned_says_so():
    assert filled().status == "AS PLANNED"


def test_the_status_vocabulary_is_not_the_pre_trade_one():
    """A verdict on a trade you may decline and a description of one you have
    taken are different decisions."""
    assert filled().status in ("AS PLANNED", "CHANGED", "WORSE")
    assert filled(quantity=1.5).status != "UNSAFE"


def test_taking_more_than_was_approved_is_critical():
    """Every risk figure on the dashboard understates this position."""
    result = filled(quantity=1.5)
    assert result.status == "WORSE"
    assert "221%" in result.reasons[0]


def test_a_partial_fill_is_reported_not_scolded():
    """Smaller than planned is inside every limit the larger one was."""
    result = filled(quantity=0.3)
    assert result.status == "CHANGED"
    assert "Partial fill" in result.reasons[0]


def test_rounding_to_the_ticket_is_not_a_deviation():
    assert validate_confirmed_quantity(0.68, 0.684) == []


def test_slippage_is_measured_against_the_stop_not_the_price():
    """Two points off a 4000-level index is nothing. Two points against a
    ten-point stop is a fifth of the risk budget."""
    tiny_stop = filled(price=4436.8, stop_loss=4430.8, take_profit=4450.0)
    wide_stop = filled(price=4436.8, stop_loss=4390.0)

    assert tiny_stop.slippage_share_of_stop == pytest.approx(0.5)
    assert wide_stop.slippage_share_of_stop == pytest.approx(2.0 / 44.8)
    assert tiny_stop.status == "WORSE"
    assert wide_stop.status == "AS PLANNED"


def test_slippage_reads_the_same_way_for_a_short():
    """Positive is worse than planned, both directions."""
    findings, slippage, share = validate_confirmed_slippage(
        4429.8, 4434.8, 4480.0, direction="short"
    )
    assert slippage == pytest.approx(5.0)
    assert share > 0


def test_a_better_fill_is_noted_and_not_a_problem():
    result = filled(price=4420.0)
    assert result.slippage < 0
    assert result.status != "WORSE"


def test_slippage_without_a_stop_cannot_be_put_in_context():
    findings, slippage, share = validate_confirmed_slippage(
        4440.0, 4434.8, None, direction="long"
    )
    assert share is None
    assert "only means something as a share" in findings[0].recommendation


def test_a_worse_fill_reports_the_reward_to_risk_it_left_you_with():
    """The trade you are in is not the trade you approved."""
    result = filled(price=4460.0)
    assert result.reward_risk_after_fill < 1.0
    assert any("reward-to-risk" in reason for reason in result.reasons)


def test_margin_is_absent_without_the_instruments_leverage():
    """An assumed leverage is confidently wrong in whichever direction it
    leaned, and margin force-closes without warning."""
    findings, margin, usage = validate_confirmed_margin(3_000.0, None)
    assert margin is None and usage is None
    assert findings[0].severity == "warning"


def test_margin_over_the_limit_is_critical():
    findings, margin, usage = validate_confirmed_margin(
        90_000.0, 2.0, LiveHelperConfig(account_balance_gbp=50_000.0)
    )
    assert findings[0].severity == "critical"
    assert usage > 0.5


def test_a_trade_the_system_never_suggested_is_not_a_deviation():
    result = filled(recommended_price=None, recommended_quantity=None)
    assert result.status == "CHANGED"
    assert any("not measured against anything" in r for r in result.reasons)
    assert "the recommended entry" in result.unknowns


def test_a_missing_fill_price_is_refused():
    assert filled(price=None).status == "WORSE"


def test_a_real_confirmation_can_be_reviewed():
    """The path the dashboard and the CLI both take."""
    from cfdinvest.persist.confirm import Confirmation

    confirmation = Confirmation.of(
        "SPX500", "open", direction="long", quantity=1.5, price=4460.0,
        recommended_price=4434.8, recommended_quantity=0.68,
    )
    review = validate_confirmation_deviation(
        confirmation,
        {"entry_price": 4434.8, "stop_level": 4390.0, "quantity": 0.68,
         "leverage": 20.0, "take_profit_level": 4520.0},
        LiveHelperConfig(account_balance_gbp=100_000.0),
    )
    assert review.status == "WORSE"
    assert review.quantity_deviation == pytest.approx(1.5 / 0.68)
    assert review.slippage == pytest.approx(25.2)


def test_a_review_falls_back_to_what_the_confirmation_recorded():
    """Runs get pruned, so the recommendation may be gone by now."""
    from cfdinvest.persist.confirm import Confirmation

    confirmation = Confirmation.of(
        "SPX500", "open", direction="long", quantity=0.68, price=4460.0,
        recommended_price=4434.8, recommended_quantity=0.68,
    )
    review = validate_confirmation_deviation(confirmation, None)
    assert review.slippage == pytest.approx(25.2)


# --------------------------------------------------------------------------- #
# The command line
# --------------------------------------------------------------------------- #
def test_the_helper_accepts_a_rules_table(tmp_path, capsys):
    from cfdinvest.livehelper import cli

    path = tmp_path / "rules.toml"
    path.write_text(
        "[SPX500]\nquantity_precision = 2\nmin_quantity = 0.1\n", encoding="utf-8"
    )
    code = cli.main(
        [
            "long SPX500 0.684 entry=4434.8 stop=4390 tp=4520 lev=20",
            "--balance", "100000", "--rules", str(path),
        ]
    )
    printed = capsys.readouterr().out

    assert code == 2  # UNSAFE
    assert "decimal place" in printed


def test_the_helper_still_works_without_one(capsys):
    from cfdinvest.livehelper import cli

    code = cli.main(
        ["long SPX500 0.4 entry=4434.8 stop=4390 tp=4520 lev=20",
         "--balance", "100000"]
    )
    assert code in (0, 1)
    assert "were not checked" in capsys.readouterr().out


def test_a_long_recommendation_is_wrapped(capsys):
    """One long line in a 68-wide block is unreadable in a mail client that
    does not soft-wrap."""
    from cfdinvest.livehelper import cli

    cli.main(
        ["long SPX500 0.4 entry=4434.8 stop=4390 tp=4520 lev=20",
         "--balance", "100000"]
    )
    printed = capsys.readouterr().out
    longest = max(printed.splitlines(), key=len)

    assert len(longest) <= 78, f"a line ran to {len(longest)}: {longest!r}"
