"""The Trading 212 rules table, and the eight checks that read it.

The rule that shapes every test here: **nothing is invented**. A field that
is not recorded is not defaulted, and the difference between "checked and
passed" and "no rule to check against" has to survive all the way from the
loader to the summary. Collapse it anywhere and an empty table looks like a
clean bill of health.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from cfdinvest.livehelper import (
    LiveHelperConfig,
    MarketState,
    evaluate_trade_safety,
    parse_trade_intent,
)
from cfdinvest.livehelper.t212_checks import (
    CHECKS,
    TITLES,
    check_all,
    check_close_only,
    check_financing_rate,
    check_leverage,
    check_max_quantity,
    check_min_quantity,
    check_quantity_precision,
    check_spread,
    check_trading_hours,
    coverage_of,
)
from cfdinvest.livehelper.t212_rules import (
    InstrumentRules,
    RuleTable,
    load_rules,
    rules_from_mapping,
)

NOW = pd.Timestamp("2026-08-25 12:00", tz="UTC")

#: Roomy, so a result is about the broker rules rather than about your limits.
ROOMY = LiveHelperConfig(
    account_balance_gbp=100_000.0,
    max_exposure_per_instrument_gbp=100_000.0,
    max_risk_per_trade_gbp=5_000.0,
    max_fx_exposure_pct=1.0,
)


def intent(text="long SPX500 0.68 entry=4434.8 stop=4390 tp=4520"):
    return parse_trade_intent(text)


def rules(**fields) -> InstrumentRules:
    return InstrumentRules(ticker="SPX500", **fields)


def table(**instruments) -> RuleTable:
    return RuleTable(rules=dict(instruments))


def severities(findings) -> dict[str, str]:
    return {finding.check: finding.severity for finding in findings}


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def test_a_missing_file_is_an_empty_table(tmp_path):
    """The normal state. Nobody has one until they write one."""
    built = load_rules(tmp_path / "absent.toml")
    assert len(built) == 0
    assert built.path is not None


def test_a_malformed_file_is_an_empty_table_not_an_exception(tmp_path, caplog):
    path = tmp_path / "rules.toml"
    path.write_text("this is not toml [[[", encoding="utf-8")

    with caplog.at_level("WARNING"):
        built = load_rules(path)

    assert len(built) == 0
    assert any("Could not parse" in record.message for record in caplog.records)


def test_a_file_that_is_not_a_table_of_instruments_is_refused(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text('["not", "a", "table"]', encoding="utf-8")
    assert len(load_rules(path)) == 0


def test_a_partial_instrument_loads_partially(tmp_path):
    """A partial entry makes the checks it covers real and leaves the rest
    honestly unchecked."""
    path = tmp_path / "rules.toml"
    path.write_text("[SPX500]\nmin_quantity = 0.1\n", encoding="utf-8")

    loaded = load_rules(path).get("SPX500")
    assert loaded.min_quantity == pytest.approx(0.1)
    assert loaded.quantity_precision is None
    assert loaded.known == ("min_quantity",)


def test_unknown_fields_are_ignored(caplog):
    with caplog.at_level("WARNING"):
        built = rules_from_mapping(
            {"SPX500": {"min_quantity": 0.1, "invented_later": 7}}
        )
    assert built.get("SPX500").min_quantity == pytest.approx(0.1)
    assert any("unknown field" in r.message.lower() for r in caplog.records)


def test_one_malformed_entry_does_not_cost_the_others():
    built = rules_from_mapping(
        {
            "SPX500": {"quantity_precision": 2},
            "BROKEN": {"min_quantity": -1},
            "NOT_A_TABLE": "nonsense",
        }
    )
    assert built.tickers == ("SPX500",)


def test_both_toml_and_json_are_read(tmp_path):
    toml = tmp_path / "rules.toml"
    toml.write_text("[SPX500]\nmin_quantity = 0.1\n", encoding="utf-8")
    js = tmp_path / "rules.json"
    js.write_text('{"SPX500": {"min_quantity": 0.1}}', encoding="utf-8")

    assert load_rules(toml).get("SPX500").min_quantity == pytest.approx(0.1)
    assert load_rules(js).get("SPX500").min_quantity == pytest.approx(0.1)


def test_the_lookup_is_case_insensitive():
    assert not table(SPX500=rules(min_quantity=0.1)).get("spx500").empty


def test_an_instrument_not_in_the_table_is_empty_not_permissive():
    assert table(SPX500=rules(min_quantity=0.1)).get("NAS100").empty


# --------------------------------------------------------------------------- #
# Nothing is invented
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("check_name, _title", CHECKS)
def test_every_check_reports_unchecked_on_an_empty_rule_set(check_name, _title):
    """The property the whole table exists to preserve.

    A field that is not recorded is not defaulted. There is no typical
    minimum size and no usual leverage.
    """
    findings, _ = check_all(intent(), table(SPX500=rules()), config=ROOMY)
    assert severities(findings)[check_name] == "unchecked"


def test_an_unchecked_finding_says_what_is_missing_and_how_to_fix_it():
    findings, _ = check_all(intent(), table(SPX500=rules()), config=ROOMY)
    unchecked = [f for f in findings if f.severity == "unchecked"]

    assert unchecked
    for finding in unchecked:
        assert "Not checked" in finding.message
        assert "rules table" in finding.recommendation


def test_an_instrument_absent_from_the_table_is_unverified():
    """Different from every field being blank: one says you have not written
    the instrument down, the other that you wrote it without that field."""
    _, verified = check_all(intent(), table(GLD=rules()), config=ROOMY)
    assert not verified


def test_an_instrument_present_with_no_fields_is_still_unverified():
    """An entry with nothing in it carries the same information as no entry:
    nothing about this instrument has been recorded."""
    findings, verified = check_all(intent(), table(SPX500=rules()), config=ROOMY)
    assert not verified
    assert all(f.severity == "unchecked" for f in findings)


def test_no_table_at_all_checks_nothing():
    findings, verified = check_all(intent(), None, config=ROOMY)
    assert not verified
    assert all(f.severity == "unchecked" for f in findings)


# --------------------------------------------------------------------------- #
# The eight checks
# --------------------------------------------------------------------------- #
def test_quantity_precision_is_enforced():
    """Trading 212 rejects this silently — the ticket simply does not fill."""
    bad = check_quantity_precision(
        intent("long SPX500 0.684 entry=4434.8 stop=4390"),
        rules(quantity_precision=2),
    )
    assert bad.severity == "critical"
    assert "0.68" in bad.recommendation
    assert "silently" in bad.recommendation


def test_a_quantity_that_fits_the_precision_passes():
    good = check_quantity_precision(intent(), rules(quantity_precision=2))
    assert good.severity == "pass"


def test_a_size_that_rounds_to_zero_is_caught():
    finding = check_quantity_precision(
        intent("long SPX500 0.004 entry=4434.8 stop=4390"),
        rules(quantity_precision=2),
    )
    assert finding.severity == "critical"
    assert "rounds to zero" in finding.message


def test_min_quantity_is_enforced_and_says_not_to_round_up():
    """Your risk limit says 0.04 and the instrument stops at 0.1: the trade
    cannot be taken at your risk limit, not that the size should be raised."""
    finding = check_min_quantity(
        intent("long SPX500 0.05 entry=4434.8 stop=4390"), rules(min_quantity=0.1)
    )
    assert finding.severity == "critical"
    assert "Do not round up" in finding.recommendation


def test_a_size_at_the_minimum_passes():
    assert check_min_quantity(
        intent("long SPX500 0.1 entry=4434.8 stop=4390"), rules(min_quantity=0.1)
    ).severity == "pass"


def test_max_quantity_is_enforced():
    finding = check_max_quantity(
        intent("long SPX500 500 entry=4434.8 stop=4390"), rules(max_quantity=100.0)
    )
    assert finding.severity == "critical"
    assert "100" in finding.recommendation


def test_a_size_within_the_maximum_passes():
    assert check_max_quantity(intent(), rules(max_quantity=100.0)).severity == "pass"


def test_an_understated_leverage_is_critical():
    """Assuming 20x where the platform gives 5x understates the margin this
    trade commits fourfold, and margin force-closes without warning."""
    finding = check_leverage(
        intent("long SPX500 0.68 entry=4434.8 stop=4390 lev=20"),
        rules(leverage=5.0),
    )
    assert finding.severity == "critical"
    assert "understated" in finding.recommendation


def test_an_overstated_leverage_is_only_a_warning():
    """Conservative is safe. It is still worth knowing."""
    finding = check_leverage(
        intent("long SPX500 0.68 entry=4434.8 stop=4390 lev=5"),
        rules(leverage=20.0),
    )
    assert finding.severity == "warning"
    assert "conservative" in finding.recommendation


def test_a_matching_leverage_passes():
    assert check_leverage(
        intent("long SPX500 0.68 entry=4434.8 stop=4390 lev=20"),
        rules(leverage=20.0),
    ).severity == "pass"


def test_the_recorded_leverage_is_used_when_none_was_supplied():
    finding = check_leverage(intent(), rules(leverage=20.0))
    assert finding.severity == "pass"
    assert finding.value == pytest.approx(20.0)


def test_financing_is_measured_against_what_the_trade_risks():
    """Six pounds a night is meaningless; six pounds against a forty-pound
    stop is a fifth of the risk budget every three nights."""
    heavy = check_financing_rate(
        intent("long SPX500 10 entry=4434.8 stop=4430"),
        rules(financing_rate=0.30),
        ROOMY,
    )
    assert heavy.severity == "warning"
    assert "stand still" in heavy.recommendation


def test_a_light_financing_rate_passes_and_still_states_the_cost():
    finding = check_financing_rate(
        intent(), rules(financing_rate=0.0295), ROOMY
    )
    assert finding.severity == "pass"
    assert "a night" in finding.message


def test_financing_without_a_size_says_it_was_not_put_in_context():
    finding = check_financing_rate(
        parse_trade_intent("long SPX500 1"), rules(financing_rate=0.0295), ROOMY
    )
    assert finding.severity == "pass"
    assert "not put in context" in finding.message


def test_spread_is_measured_against_the_stop_distance():
    """The ratio is what matters, and the threshold for it already exists in
    LiveHelperConfig rather than being redeclared here."""
    wide = check_spread(
        intent("long SPX500 0.68 entry=4434.8 stop=4430"),
        rules(spread_typical=3.0),
        ROOMY,
    )
    assert wide.severity == "warning"
    assert "risk budget" in wide.recommendation


def test_a_narrow_spread_passes():
    assert check_spread(intent(), rules(spread_typical=0.7), ROOMY).severity == "pass"


def test_spread_without_a_stop_says_it_was_not_put_in_context():
    finding = check_spread(
        parse_trade_intent("long SPX500 1 entry=4434.8"),
        rules(spread_typical=0.7),
        ROOMY,
    )
    assert "not put in context" in finding.message


def test_trading_hours_are_enforced_as_a_warning_not_a_block():
    """The platform is the authority; the recorded window is a note you made."""
    outside = check_trading_hours(
        intent(), rules(trading_hours="08:00-11:00"), NOW
    )
    assert outside.severity == "warning"


def test_inside_the_window_passes():
    assert check_trading_hours(
        intent(), rules(trading_hours="08:00-16:00"), NOW
    ).severity == "pass"


def test_an_unreadable_window_is_unchecked_rather_than_a_warning():
    """A fault in the table is not a fact about the trade."""
    finding = check_trading_hours(intent(), rules(trading_hours="whenever"), NOW)
    assert finding.severity == "unchecked"


def test_close_only_blocks_an_opening_order():
    """The worst shape a rejection can take: you believe you are in a position
    you are not."""
    finding = check_close_only(intent(), rules(close_only=True))
    assert finding.severity == "critical"
    assert "silent" in finding.recommendation


def test_close_only_still_allows_closing():
    assert check_close_only(
        intent(), rules(close_only=True), closing=True
    ).severity == "pass"


def test_an_instrument_explicitly_not_close_only_passes():
    assert check_close_only(intent(), rules(close_only=False)).severity == "pass"


# --------------------------------------------------------------------------- #
# Coverage
# --------------------------------------------------------------------------- #
def test_coverage_counts_what_could_be_asked():
    """Eight checks with two rules recorded is two checks and six blanks. A
    report that did not say so would look like eight passes."""
    findings, _ = check_all(
        intent(), table(SPX500=rules(quantity_precision=2, min_quantity=0.1)),
        config=ROOMY,
    )
    coverage = coverage_of(findings)

    assert coverage["total"] == len(CHECKS)
    assert coverage["checked"] == 2
    assert coverage["passed"] == 2
    assert coverage["not_checked"] == len(CHECKS) - 2


def test_coverage_names_what_is_missing_in_words():
    findings, _ = check_all(
        intent(), table(SPX500=rules(quantity_precision=2)), config=ROOMY
    )
    coverage = coverage_of(findings)

    assert "Minimum size" in coverage["missing_titles"]
    assert set(coverage["missing_titles"]) <= set(TITLES.values())


def test_full_coverage_reports_one():
    findings, _ = check_all(
        intent("long SPX500 0.68 entry=4434.8 stop=4390 lev=20"),
        table(
            SPX500=rules(
                quantity_precision=2, min_quantity=0.1, max_quantity=100.0,
                leverage=20.0, financing_rate=0.0295, spread_typical=0.7,
                trading_hours="08:00-16:00", close_only=False,
            )
        ),
        config=ROOMY, now=NOW,
    )
    coverage = coverage_of(findings)

    assert coverage["coverage"] == pytest.approx(1.0)
    assert coverage["not_checked"] == 0
    assert coverage["failed"] == 0


# --------------------------------------------------------------------------- #
# It reaches the assessment
# --------------------------------------------------------------------------- #
def assess(text=None, rule_table=None, **market):
    # A leverage is supplied by default so these tests are about the broker
    # rules rather than about the safety engine's own "leverage unknown".
    market.setdefault("leverage", 20.0)
    return evaluate_trade_safety(
        intent(text) if text else intent(),
        ROOMY,
        MarketState(**market),
        rules=rule_table,
        now=NOW,
    )


def test_the_findings_appear_in_the_assessment():
    result = assess(rule_table=table(SPX500=rules(quantity_precision=2)))
    assert {f.check for f in result.broker_findings} == {n for n, _ in CHECKS}


def test_a_failed_rule_reaches_the_verdict():
    """One verdict covers both kinds of constraint. Two separate answers would
    let you act on the reassuring one."""
    result = assess(
        "long SPX500 0.684 entry=4434.8 stop=4390",
        table(SPX500=rules(quantity_precision=2)),
    )
    assert result.status == "UNSAFE"


def test_a_passing_rule_never_moves_the_verdict():
    """Passes exist so the absence of a check is visible, not to change an
    answer."""
    result = assess(rule_table=table(SPX500=rules(quantity_precision=2)))
    assert result.status == "SAFE"


def test_passes_and_unchecked_stay_out_of_the_reasons():
    """Coverage is not a reason the verdict is what it is, and padding the
    WHY section with it would bury the actual problem."""
    result = assess(rule_table=table(SPX500=rules(quantity_precision=2)))
    joined = " ".join(result.reasons)

    assert "Not checked" not in joined
    assert "fits SPX500" not in joined


def test_an_empty_table_is_treated_as_no_table():
    """Otherwise the default lookup, which finds no file on a fresh install,
    caps every verdict at BORDERLINE — and a verdict that never changes is
    one nobody reads."""
    result = assess(rule_table=RuleTable())
    assert result.status == "SAFE"
    assert result.unknowns == ()


def test_a_populated_table_missing_this_instrument_is_an_unknown():
    """You opted into these checks and none of them could run."""
    result = assess(rule_table=table(GLD=rules(min_quantity=0.5)))
    assert result.status == "BORDERLINE"
    assert any("SPX500" in unknown for unknown in result.unknowns)


def test_an_unrecorded_field_is_not_an_unknown():
    """Otherwise every partial entry caps the verdict for ever."""
    result = assess(rule_table=table(SPX500=rules(quantity_precision=2)))
    assert result.unknowns == ()


def test_the_broker_leverage_check_does_not_collide_with_effective_leverage():
    """Two different numbers answering two different questions. Sharing a name
    made the checklist and the coverage count confuse them."""
    result = assess(
        "long SPX500 0.68 entry=4434.8 stop=4390 lev=20",
        table(SPX500=rules(leverage=5.0)),
    )
    checks = [f.check for f in result.findings]

    assert "t212_leverage" in checks
    assert len([c for c in checks if c == "t212_leverage"]) == 1


# --------------------------------------------------------------------------- #
# It reaches the checklist and the score
# --------------------------------------------------------------------------- #
def test_the_findings_reach_the_checklist():
    from cfdinvest.livehelper.refine_checklist import refine_checklist

    result = refine_checklist(
        intent(),
        assess(
            "long SPX500 0.684 entry=4434.8 stop=4390",
            table(SPX500=rules(quantity_precision=2)),
        ),
        None,
        ROOMY,
    )
    broker = next(item for item in result.items if item.key == "broker")
    assert broker.failed
    assert broker.blocking


def test_the_checklist_reports_coverage_rather_than_a_bare_pass():
    """An item that said "yes" on two of eight rules would be claiming the
    other six."""
    from cfdinvest.livehelper.refine_checklist import refine_checklist

    result = refine_checklist(
        intent(), assess(rule_table=table(SPX500=rules(quantity_precision=2))),
        None, ROOMY,
    )
    broker = next(item for item in result.items if item.key == "broker")

    assert broker.state == "pass"
    assert "of 8 rules recorded" in broker.detail
    assert "Not checked:" in broker.detail


def test_no_table_shows_the_checklist_item_as_unchecked():
    from cfdinvest.livehelper.refine_checklist import refine_checklist

    result = refine_checklist(intent(), assess(), None, ROOMY)
    broker = next(item for item in result.items if item.key == "broker")
    assert broker.state == "unchecked"


def test_the_findings_reach_the_quality_score():
    from cfdinvest.livehelper.refine_quality import refine_quality

    score = refine_quality(
        intent(), assess(rule_table=table(SPX500=rules(quantity_precision=2))),
        None, ROOMY,
    )
    component = next(c for c in score.components if c.name == "broker rules")

    assert component.measured
    # One of eight rules recorded scores an eighth, not full marks.
    assert component.score == pytest.approx(1 / len(CHECKS))


def test_a_failed_rule_scores_zero_on_that_component():
    from cfdinvest.livehelper.refine_quality import refine_quality

    score = refine_quality(
        intent("long SPX500 0.684 entry=4434.8 stop=4390"),
        assess(
            "long SPX500 0.684 entry=4434.8 stop=4390",
            table(SPX500=rules(quantity_precision=2)),
        ),
        None, ROOMY,
    )
    component = next(c for c in score.components if c.name == "broker rules")
    assert component.score == 0.0


def test_no_table_leaves_the_quality_component_unmeasured():
    """Scoring it as a pass would make an empty table look like a clean bill."""
    from cfdinvest.livehelper.refine_quality import refine_quality

    score = refine_quality(intent(), assess(), None, ROOMY)
    component = next(c for c in score.components if c.name == "broker rules")
    assert not component.measured


# --------------------------------------------------------------------------- #
# It reaches the summary and the command line
# --------------------------------------------------------------------------- #
def test_the_summary_has_its_own_rules_section():
    from cfdinvest.livehelper.summary import build_execution_helper_summary
    from cfdinvest.livehelper import compute_safe_sizing

    result = assess(rule_table=table(SPX500=rules(quantity_precision=2)))
    text = build_execution_helper_summary(
        intent(), result, compute_safe_sizing(intent(), ROOMY, MarketState()),
        config=ROOMY,
    )
    # Normalised: the block is wrapped at 68 columns, so a sentence long
    # enough to be worth asserting is a sentence split across lines.
    flowed = " ".join(text.split())

    assert "TRADING 212 RULES" in text
    assert "1 of 8 recorded" in flowed
    assert "not the same as passing" in flowed


def test_the_summary_says_nothing_when_there_is_no_table():
    from cfdinvest.livehelper.summary import build_execution_helper_summary
    from cfdinvest.livehelper import compute_safe_sizing

    text = build_execution_helper_summary(
        intent(), assess(),
        compute_safe_sizing(intent(), ROOMY, MarketState()), config=ROOMY,
    )
    assert "TRADING 212 RULES" not in text


def test_the_cli_reports_what_was_applied_and_what_was_missing(tmp_path, capsys):
    from cfdinvest.livehelper import cli

    path = tmp_path / "rules.toml"
    path.write_text(
        "[SPX500]\nquantity_precision = 2\nmin_quantity = 0.1\n", encoding="utf-8"
    )
    cli.main(
        [
            "long SPX500 0.68 entry=4434.8 stop=4390 tp=4520",
            "--balance", "100000", "--max-exposure", "100000",
            "--rules", str(path),
        ]
    )
    printed = capsys.readouterr().out

    assert "applied     2 of 8" in printed
    assert "not recorded" in printed
    assert "unverified, not passed" in printed


def test_the_cli_says_when_the_instrument_is_not_in_the_table(tmp_path, capsys):
    from cfdinvest.livehelper import cli

    path = tmp_path / "rules.toml"
    path.write_text("[GLD]\nmin_quantity = 0.5\n", encoding="utf-8")
    cli.main(
        [
            "long SPX500 0.68 entry=4434.8 stop=4390 tp=4520",
            "--balance", "100000", "--max-exposure", "100000",
            "--rules", str(path),
        ]
    )
    printed = capsys.readouterr().out
    assert "unverified, not passed" in printed


def test_the_cli_can_be_told_not_to_look_for_a_default_table(capsys):
    from cfdinvest.livehelper import cli

    cli.main(
        [
            "long SPX500 0.68 entry=4434.8 stop=4390 tp=4520",
            "--balance", "100000", "--max-exposure", "100000",
            "--no-default-rules",
        ]
    )
    printed = capsys.readouterr().out
    assert "were not checked" in printed


def test_every_cli_line_stays_within_the_block(tmp_path, capsys):
    """One long line in a 68-wide block is unreadable in a mail client that
    does not soft-wrap. Eight rule titles on one line ran to 148."""
    from cfdinvest.livehelper import cli

    path = tmp_path / "rules.toml"
    path.write_text("[SPX500]\nquantity_precision = 2\n", encoding="utf-8")
    cli.main(
        [
            "long SPX500 0.68 entry=4434.8 stop=4390 tp=4520",
            "--balance", "100000", "--max-exposure", "100000",
            "--rules", str(path),
        ]
    )
    printed = capsys.readouterr().out
    longest = max(
        (
            line
            for line in printed.splitlines()
            # A path is not wrapped -- a wrapped path cannot be copied.
            if "rules.toml" not in line
        ),
        key=len,
    )
    assert len(longest) <= 78, f"a line ran to {len(longest)}: {longest!r}"


# --------------------------------------------------------------------------- #
# The runner loads it once
# --------------------------------------------------------------------------- #
def test_the_runner_loads_the_table_once(tmp_path):
    """Loaded per run rather than per trade: a file edited mid-run would give
    two trades two different answers and nothing would say which."""
    from cfdinvest.schedule.runner import RunConfig, load_rules_once

    path = tmp_path / "rules.toml"
    path.write_text("[SPX500]\nmin_quantity = 0.1\n", encoding="utf-8")

    loaded = load_rules_once(RunConfig(rules_path=str(path)))
    assert len(loaded) == 1


def test_the_runner_survives_a_missing_table():
    from cfdinvest.schedule.runner import RunConfig, load_rules_once

    loaded = load_rules_once(RunConfig(rules_path="/nowhere/at/all.toml"))
    assert loaded is not None
    assert len(loaded) == 0


def test_every_shipped_broker_figure_names_where_it_came_from():
    """The table ships real numbers now, so each must say who measured it.

    It used to ship a template that was entirely commented out, and the test
    asserted exactly that -- the fear being an invented figure that looks
    measured. The fear is unchanged; the table is filled in, so the guard
    moves from "no values" to "no value without a provenance". An entry that
    cannot say where it came from is the thing that was always dangerous.
    """
    import tomllib

    document = tomllib.loads(
        Path("config/t212_rules.toml").read_text(encoding="utf-8")
    )
    assert document, "the shipped table must not be empty"

    for ticker, entry in document.items():
        assert entry.get("source"), (
            f"{ticker} carries broker figures with no source recorded"
        )
        assert entry.get("t212_name"), (
            f"{ticker} has no Trading 212 name, so nobody can find it to check"
        )
