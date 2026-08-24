"""The live-execution helper: parsing, verdicts, sizing and the summary.

The tests that matter here are the ones about **refusing to say SAFE**. A
sizing bug produces a number that is visibly wrong; a verdict bug produces a
green label on a trade nobody checked, and that is the failure this whole
package exists to prevent.

Nothing in this suite touches the network, and nothing can: the package has
no broker client to mock.
"""

from __future__ import annotations

import math

import pytest

from cfdinvest.livehelper import cli
from cfdinvest.livehelper.config import LiveHelperConfig
from cfdinvest.livehelper.intent import TradeIntent, parse_trade_intent
from cfdinvest.livehelper.safety import (
    MarketState,
    TradeSafetyAssessment,
    evaluate_trade_safety,
)
from cfdinvest.livehelper.sizing_helper import compute_safe_sizing, round_down
from cfdinvest.livehelper.summary import build_execution_helper_summary

#: A £2,000 account with the specification's limits.
ACCOUNT = LiveHelperConfig(account_balance_gbp=2_000.0)

#: A sterling instrument, comfortably inside every limit.
CLEAN = dict(
    instrument="UKX", direction="long", quantity=0.5,
    entry_price=100.0, stop_loss=96.0, take_profit=112.0, leverage=20.0,
)


def intent(**changes) -> TradeIntent:
    return TradeIntent(**{**CLEAN, **changes})


def assess(market: MarketState | None = None, config=None, **changes):
    return evaluate_trade_safety(intent(**changes), config or ACCOUNT, market)


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def test_a_typed_line_parses():
    parsed = parse_trade_intent(
        "long SPX500 0.68 entry=4434.8 stop=4390 tp=4520 ccy=USD lev=20"
    )
    assert parsed.instrument == "SPX500"
    assert parsed.direction == "long"
    assert parsed.quantity == pytest.approx(0.68)
    assert parsed.entry_price == pytest.approx(4434.8)
    assert parsed.stop_loss == pytest.approx(4390.0)
    assert parsed.take_profit == pytest.approx(4520.0)
    assert parsed.instrument_currency == "USD"
    assert parsed.leverage == pytest.approx(20.0)


def test_the_words_people_actually_type():
    assert parse_trade_intent("buy UKX 1").direction == "long"
    assert parse_trade_intent("sell UKX 1").direction == "short"
    assert parse_trade_intent("SELL ukx 1").direction == "short"


def test_field_aliases_are_accepted():
    """A helper that refuses over a synonym is one nobody uses."""
    a = parse_trade_intent("long UKX 1 sl=90 tp=110")
    b = parse_trade_intent("long UKX 1 stop_loss=90 take_profit=110")
    assert a.stop_loss == b.stop_loss == pytest.approx(90.0)
    assert a.take_profit == b.take_profit == pytest.approx(110.0)


def test_a_mapping_parses_too():
    parsed = parse_trade_intent(
        {"ticker": "UKX", "side": "short", "qty": 2, "entry": 100}
    )
    assert parsed.instrument == "UKX"
    assert parsed.direction == "short"


def test_currency_symbols_and_commas_are_tolerated():
    parsed = parse_trade_intent("long UKX 1 entry=£1,234.50")
    assert parsed.entry_price == pytest.approx(1234.5)


def test_market_entry_is_none_not_zero():
    assert parse_trade_intent("long UKX 1 entry=market").entry_price is None


def test_an_unreadable_trade_is_refused():
    """A misparsed field silently changes the risk, so a guess is worse."""
    with pytest.raises(ValueError, match="needs"):
        parse_trade_intent("just some words")
    with pytest.raises(ValueError, match="direction"):
        parse_trade_intent({"instrument": "UKX", "direction": "sideways", "qty": 1})


def test_a_negative_quantity_is_refused():
    with pytest.raises(ValueError, match="direction carries the sign"):
        TradeIntent(instrument="UKX", direction="long", quantity=-1)


def test_an_intent_knows_its_geometry():
    long_trade = intent()
    assert long_trade.stop_distance() == pytest.approx(4.0)
    assert long_trade.target_distance() == pytest.approx(12.0)
    assert long_trade.stop_is_on_the_correct_side() is True
    assert long_trade.is_foreign is False


def test_a_short_mirrors_a_long():
    short = intent(direction="short", stop_loss=104.0, take_profit=88.0)
    assert short.stop_is_on_the_correct_side() is True
    assert short.target_is_on_the_correct_side() is True
    assert short.sign == -1


# --------------------------------------------------------------------------- #
# The verdict — SAFE, BORDERLINE, UNSAFE
# --------------------------------------------------------------------------- #
def test_a_clean_trade_is_safe():
    result = assess()
    assert result.status == "SAFE", result.reasons
    assert result.go is True
    assert result.risk_gbp == pytest.approx(2.0)
    assert result.exposure_gbp == pytest.approx(50.0)


def test_breaching_the_risk_limit_is_unsafe():
    result = assess(config=LiveHelperConfig(account_balance_gbp=2_000.0,
                                            max_risk_per_trade_gbp=1.0))
    assert result.status == "UNSAFE"
    assert any("Risk" in reason for reason in result.reasons)


def test_approaching_a_limit_is_borderline():
    """Eighty per cent of a limit stops being safe."""
    result = assess(
        config=LiveHelperConfig(account_balance_gbp=2_000.0,
                                max_risk_per_trade_gbp=2.2)
    )
    assert result.status == "BORDERLINE"
    assert result.go is False


def test_no_stop_is_unsafe_not_merely_large():
    """The loss is unbounded, so there is no number to compare."""
    result = assess(stop_loss=None)

    assert result.status == "UNSAFE"
    assert result.risk_gbp is None
    assert "stop_loss" in result.unknowns
    assert any("unbounded" in reason for reason in result.reasons)


def test_a_stop_on_the_wrong_side_is_unsafe():
    """It would trigger immediately, and it is easy to fat-finger."""
    result = assess(stop_loss=104.0)
    assert result.status == "UNSAFE"
    assert any("wrong side" in reason for reason in result.reasons)


def test_a_target_on_the_wrong_side_is_unsafe():
    result = assess(take_profit=88.0)
    assert any("wrong side" in reason for reason in result.reasons)


def test_a_stop_at_the_entry_is_refused():
    result = assess(stop_loss=100.0)
    assert result.status == "UNSAFE"
    assert any("not a stop" in reason for reason in result.reasons)


def test_an_unknown_exchange_rate_cannot_be_safe():
    """Substituting 1.0 would price a trade nobody can price."""
    result = assess(instrument_currency="USD")

    assert result.status == "UNSAFE"
    assert result.exposure_gbp is None
    assert "fx_rate" in result.unknowns


def test_a_known_exchange_rate_converts():
    result = assess(
        MarketState(fx_rate=0.80), instrument_currency="USD"
    )
    assert result.exposure_gbp == pytest.approx(40.0)
    assert result.risk_gbp == pytest.approx(1.6)


def test_an_unknown_leverage_cannot_be_safe():
    result = assess(leverage=None)

    assert result.status == "BORDERLINE"
    assert result.margin_required_gbp is None
    assert "leverage" in result.unknowns


def test_no_price_at_all_is_unsafe():
    result = assess(entry_price=None)
    assert result.status == "UNSAFE"
    assert "price" in result.unknowns


def test_a_market_order_uses_the_last_price():
    result = assess(MarketState(last_price=100.0), entry_price=None)
    assert result.exposure_gbp == pytest.approx(50.0)


def test_effective_leverage_is_the_one_size_can_change():
    """Instrument leverage is fixed by the broker; effective leverage is not."""
    result = assess(quantity=5.0)

    assert result.instrument_leverage == pytest.approx(20.0)
    assert result.leverage == pytest.approx(500.0 / 2_000.0)


def test_margin_uses_the_instruments_leverage():
    result = assess(quantity=4.0)
    assert result.exposure_gbp == pytest.approx(400.0)
    assert result.margin_required_gbp == pytest.approx(20.0)


def test_committed_margin_counts_against_the_cap():
    """A trade that is fine on an empty account can tip a loaded one over."""
    loaded = LiveHelperConfig(account_balance_gbp=2_000.0, used_margin_gbp=980.0)
    result = evaluate_trade_safety(intent(quantity=4.0), loaded)

    assert result.margin_usage_pct_after_trade == pytest.approx(0.5)
    assert result.status in ("BORDERLINE", "UNSAFE")


def test_high_volatility_is_a_flag_not_a_block():
    """It is why the opportunity exists."""
    result = assess(MarketState(volatility=0.60))

    assert result.volatility_flag is True
    assert result.status == "BORDERLINE"


def test_the_spread_is_judged_against_the_stop_distance():
    """Two points means nothing alone; against a ten-point stop it is a fifth."""
    wide = assess(MarketState(spread=2.0))     # stop distance is 4.0
    narrow = assess(MarketState(spread=0.1))

    assert wide.spread_flag is True
    assert narrow.spread_flag is False


def test_a_close_only_instrument_is_unsafe_to_open():
    result = assess(MarketState(close_only=True))
    assert result.status == "UNSAFE"
    assert any("close-only" in reason for reason in result.reasons)


def test_financing_rollover_is_a_warning():
    result = assess(MarketState(near_financing_rollover=True))
    assert result.status == "BORDERLINE"
    assert any("rolls over" in reason.lower() for reason in result.reasons)


def test_reward_to_risk_below_one_is_noted_not_blocked():
    result = assess(take_profit=102.0)
    assert result.reward_risk_ratio == pytest.approx(0.5)
    assert result.status == "SAFE"


def test_the_assessment_is_deterministic():
    market = MarketState(fx_rate=0.8, volatility=0.1, spread=0.2)
    first = assess(market)
    second = assess(market)
    assert first.status == second.status
    assert first.risk_gbp == second.risk_gbp


# --------------------------------------------------------------------------- #
# Sizing
# --------------------------------------------------------------------------- #
def test_rounding_is_always_down():
    """A size rounded up can breach the limit it was derived from."""
    assert round_down(0.98765, 4) == pytest.approx(0.9876)
    assert round_down(1.99999, 2) == pytest.approx(1.99)
    assert round_down(-1.0) == 0.0


def test_an_oversized_trade_is_cut():
    sizing = compute_safe_sizing(intent(quantity=100.0), ACCOUNT)

    assert sizing.reduces is True
    assert sizing.recommended_quantity < 100.0
    assert sizing.binding_constraint in sizing.ceilings


def test_a_trade_inside_every_limit_is_left_alone():
    sizing = compute_safe_sizing(intent(), ACCOUNT)

    assert sizing.recommended_quantity == pytest.approx(0.5)
    assert sizing.reduces is False


def test_the_risk_limit_binds_when_the_stop_is_wide():
    tight = LiveHelperConfig(account_balance_gbp=2_000.0, max_risk_per_trade_gbp=10.0)
    sizing = compute_safe_sizing(intent(quantity=100.0), tight)

    assert sizing.binding_constraint == "risk"
    # 10 GBP of risk over a 4-point stop is a 2.5-unit ceiling; the
    # recommendation sits at the borderline threshold below it.
    assert sizing.ceilings["risk"] == pytest.approx(2.5)
    assert sizing.recommended_quantity == pytest.approx(
        2.5 * ACCOUNT.borderline_fraction
    )


def test_the_recommended_size_actually_passes():
    """The point of the whole module: what it suggests must clear the limits."""
    sizing = compute_safe_sizing(intent(quantity=100.0), ACCOUNT)
    rechecked = evaluate_trade_safety(
        intent(quantity=sizing.recommended_quantity), ACCOUNT
    )
    assert rechecked.status == "SAFE", rechecked.reasons


def test_the_margin_limit_binds_on_a_loaded_account():
    loaded = LiveHelperConfig(account_balance_gbp=2_000.0, used_margin_gbp=950.0)
    sizing = compute_safe_sizing(intent(quantity=100.0), loaded)

    assert sizing.binding_constraint == "margin"
    assert sizing.margin_usage_pct_after_trade <= 0.5 + 1e-9


def test_currency_exposure_binds_on_a_foreign_instrument():
    sizing = compute_safe_sizing(
        intent(quantity=100.0, instrument_currency="USD"),
        ACCOUNT,
        MarketState(fx_rate=0.80),
    )
    assert sizing.binding_constraint == "fx"
    assert sizing.recommended_exposure_gbp <= 600.0 + 1e-6


def test_no_size_clears_an_impossible_limit():
    """A real answer, not a quietly tiny number."""
    impossible = LiveHelperConfig(
        account_balance_gbp=2_000.0, max_risk_per_trade_gbp=0.0001
    )
    sizing = compute_safe_sizing(intent(quantity=100.0), impossible)

    assert sizing.tradeable is False
    assert sizing.recommended_quantity == 0.0
    assert any("clears every limit" in note for note in sizing.notes)


def test_sizing_without_a_stop_says_what_it_could_not_bound():
    sizing = compute_safe_sizing(intent(quantity=100.0, stop_loss=None), ACCOUNT)

    assert "risk" not in sizing.ceilings
    assert any("No usable stop" in note for note in sizing.notes)


def test_sizing_without_an_exchange_rate_refuses():
    sizing = compute_safe_sizing(
        intent(instrument_currency="USD"), ACCOUNT, MarketState()
    )
    assert sizing.tradeable is False
    assert any("exchange rate" in note for note in sizing.notes)


def test_sizing_is_deterministic():
    first = compute_safe_sizing(intent(quantity=100.0), ACCOUNT)
    second = compute_safe_sizing(intent(quantity=100.0), ACCOUNT)
    assert first.recommended_quantity == second.recommended_quantity


# --------------------------------------------------------------------------- #
# The summary
# --------------------------------------------------------------------------- #
def summary_for(**changes) -> str:
    trade = intent(**changes)
    assessment = evaluate_trade_safety(trade, ACCOUNT)
    sizing = compute_safe_sizing(trade, ACCOUNT)
    return build_execution_helper_summary(
        trade, assessment, sizing, config=ACCOUNT
    )


def test_the_verdict_comes_first():
    """Anyone reading this is about to act."""
    first_lines = summary_for().splitlines()[:3]
    assert any("SAFE" in line for line in first_lines)


def test_the_summary_carries_every_key_field():
    text = summary_for()
    for expected in (
        "UKX", "long", "Quantity", "Entry", "Stop-loss", "Take-profit",
        "Risk to stop", "Exposure", "Effective leverage", "Margin required",
        "Margin used after", "SAFE SIZE",
    ):
        assert expected in text, expected


def test_an_unsafe_summary_says_so_plainly():
    text = summary_for(quantity=500.0)
    assert "UNSAFE" in text.splitlines()[1]
    assert "WHAT TO DO" in text
    assert "Reduce quantity to" in text


def test_a_missing_stop_is_shouted_not_blanked():
    assert "NONE SET" in summary_for(stop_loss=None)


def test_unknowns_are_listed():
    text = summary_for(instrument_currency="USD")
    assert "NOT CHECKED" in text
    assert "fx_rate" in text


def test_the_disclaimer_is_always_present_and_wrapped():
    text = summary_for()
    assert "decision aid, not an instruction" in text
    # Wide enough for the limit column, narrow enough that a mail client
    # which does not soft-wrap still shows every line whole.
    longest = max(text.splitlines(), key=len)
    assert len(longest) <= 78, f"a line ran to {len(longest)}: {longest!r}"


def test_specific_advice_supersedes_generic_advice():
    """One instruction, not three lines all saying trim it."""
    text = summary_for(quantity=500.0)
    actions = [
        line for line in text.splitlines()
        if line.strip().startswith("- ") and "quantity" in line.lower()
    ]
    assert len(actions) == 1
    assert "Reduce quantity to" in actions[0]


def test_the_summary_is_deterministic():
    assert summary_for() == summary_for()


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #
def test_the_verdict_is_the_exit_code(capsys):
    assert cli.main(
        ["long UKX 0.5 entry=100 stop=96 tp=112 lev=20", "--balance", "2000"]
    ) == 0
    assert "SAFE" in capsys.readouterr().out


def test_an_unsafe_trade_exits_two(capsys):
    code = cli.main(
        ["long UKX 500 entry=100 stop=96 lev=20", "--balance", "2000", "--quiet"]
    )
    assert code == cli.EXIT_CODES["UNSAFE"]


def test_an_unreadable_trade_is_not_a_verdict(capsys):
    """Three is deliberately not one of the verdicts."""
    code = cli.main(["gibberish"])
    assert code == cli.PARSE_FAILURE
    assert code not in cli.EXIT_CODES.values()
    assert "Could not read that trade" in capsys.readouterr().err


def test_command_line_limits_override_the_environment(capsys):
    cli.main(
        ["long UKX 0.5 entry=100 stop=96 lev=20",
         "--balance", "2000", "--max-risk", "1"]
    )
    assert "UNSAFE" in capsys.readouterr().out


def test_quiet_prints_nothing(capsys):
    cli.main(["long UKX 0.5 entry=100 stop=96 lev=20", "--quiet"])
    assert capsys.readouterr().out == ""


def test_mail_failure_never_breaks_the_helper(monkeypatch):
    """An unconfigured mailer must not stop a helper that already answered."""
    monkeypatch.setattr(
        "cfdinvest.schedule.notify.SmtpConfig.from_env",
        staticmethod(lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no mail"))),
    )
    assert cli.email_summary("UKX", "SAFE", "body") is False


# --------------------------------------------------------------------------- #
# The contract: this package cannot trade
# --------------------------------------------------------------------------- #
FORBIDDEN_IMPORTS = frozenset(
    {"requests", "urllib", "urllib3", "httpx", "aiohttp", "socket",
     "selenium", "playwright", "pyautogui", "webbrowser", "subprocess"}
)


def test_the_package_cannot_reach_trading212():
    """The safety claim, enforced rather than promised.

    There is no broker client here to mock, and this is what keeps it that
    way when somebody adds a module in six months.
    """
    import ast
    from pathlib import Path

    package = Path(__file__).resolve().parents[1] / "cfdinvest" / "livehelper"
    for path in package.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            for module in modules:
                assert module.split(".")[0] not in FORBIDDEN_IMPORTS, (
                    f"{path.name} imports {module}"
                )


def test_no_trading212_endpoint_appears_anywhere():
    from pathlib import Path

    package = Path(__file__).resolve().parents[1] / "cfdinvest" / "livehelper"
    for path in package.rglob("*.py"):
        source = path.read_text(encoding="utf-8").lower()
        for forbidden in ("http://", "https://", "trading212.com", "api_key"):
            assert forbidden not in source, f"{path.name} contains {forbidden!r}"
