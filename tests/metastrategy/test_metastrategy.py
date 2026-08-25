"""The risk posture: how hard to push, and what it is not allowed to do.

This is the first layer whose stated purpose is to *increase* risk, so almost
every test here is about a limit on that. The posture may use headroom the
safety engine granted and can never create any; the tolerances can tighten and
never loosen; and moving forward needs every requirement clear at once while
holding back needs only one.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from cfdinvest.config import AppConfig, PathConfig
from cfdinvest.insights import build_report
from cfdinvest.livehelper import (
    LiveHelperConfig,
    MarketState,
    compute_safe_sizing,
    evaluate_trade_safety,
    parse_trade_intent,
)
from cfdinvest.livehelper.context import BiasProfile, RegimeProfile, TradingContext
from cfdinvest.livehelper.refine_exit import refine_exit
from cfdinvest.livehelper.refine_meta import refine_with_meta
from cfdinvest.livehelper.refine_sizing import refine_sizing
from cfdinvest.metastrategy import (
    FORWARD_REQUIREMENTS,
    POSTURE_SETTINGS,
    POSTURES,
    MetaStrategyProfile,
    compute_meta_strategy,
    neutral_profile,
    posture_for,
)
from cfdinvest.persist import metastrategy as store

NOW = pd.Timestamp("2026-08-25 12:00", tz="UTC")


@pytest.fixture
def lake(tmp_path) -> AppConfig:
    return AppConfig(paths=PathConfig(data_root=tmp_path / "data"))


def profitable_insights(n=200, *, mean=60.0):
    rng = np.random.default_rng(3)
    trades = pd.DataFrame(
        {
            "net_pnl": rng.normal(mean, 40, n),
            "entry_time": pd.date_range(
                "2025-01-01", periods=n, freq="D", tz="UTC"
            ),
        }
    )
    return build_report(trades, now=NOW)


def losing_insights(n=200):
    return profitable_insights(n, mean=-60.0)


CALM = RegimeProfile(
    regimes={"trend_regime": "up", "volatility_regime": "low"},
    elevated=(),
    available=True,
)
STORMY = RegimeProfile(
    regimes={"volatility_regime": "high", "correlation_regime": "breakdown"},
    elevated=("volatility_regime", "correlation_regime"),
    available=True,
)
CLEAN = BiasProfile(overconfidence=0.1, available=True)


def compute(**changes):
    fields = dict(
        insights=profitable_insights(),
        bias_profile=CLEAN,
        regime_profile=CALM,
        pnl_history={"current_drawdown": -0.01},
        account_state="virtual",
    )
    fields.update(changes)
    return compute_meta_strategy(**fields, now=NOW)


def profile_of(name) -> MetaStrategyProfile:
    return MetaStrategyProfile(posture=name, **POSTURE_SETTINGS[name])


# --------------------------------------------------------------------------- #
# The tolerances can only tighten
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", ["safety_tolerance", "margin_tolerance"])
def test_a_tolerance_above_one_is_clamped(name, caplog):
    """A tolerance that could exceed a configured limit would be a way around
    it, and the limits would be advisory."""
    with caplog.at_level("WARNING"):
        profile = neutral_profile(**{name: 2.0})

    assert getattr(profile, name) == 1.0
    assert any("clamped" in record.message for record in caplog.records)


@pytest.mark.parametrize("name", POSTURES)
def test_no_posture_ships_a_tolerance_above_one(name):
    profile = profile_of(name)
    assert profile.safety_tolerance <= 1.0
    assert profile.margin_tolerance <= 1.0


def test_a_tolerance_only_ever_reads_a_limit_down():
    """The method that makes the invariant true in practice rather than only
    in the docstring."""
    for name in POSTURES:
        profile = profile_of(name)
        assert profile.limit_for(1_000.0) <= 1_000.0
        assert profile.limit_for(1_000.0, margin=True) <= 1_000.0


def test_a_negative_tolerance_is_refused_rather_than_clamped():
    with pytest.raises(ValueError, match="must be positive"):
        neutral_profile(safety_tolerance=-0.5)


def test_an_absurd_risk_budget_is_refused():
    with pytest.raises(ValueError, match="not a posture"):
        neutral_profile(risk_budget=0.9)


def test_a_sizing_request_beyond_the_cap_is_trimmed():
    profile = neutral_profile(sizing_multiplier=10.0)
    from cfdinvest.metastrategy import MAX_SIZING_REQUEST

    assert profile.sizing_multiplier == MAX_SIZING_REQUEST


def test_an_invented_posture_is_refused():
    with pytest.raises(ValueError, match="posture must be one of"):
        MetaStrategyProfile(posture="reckless")


# --------------------------------------------------------------------------- #
# Cutting needs one reason; raising needs all of them
# --------------------------------------------------------------------------- #
def test_everything_clear_reaches_a_forward_posture():
    result = compute()
    assert result.posture in ("assertive", "opportunistic")
    assert result.details["forward_allowed"]


@pytest.mark.parametrize(
    "changes, why",
    [
        ({"pnl_history": {"current_drawdown": -0.12}}, "drawdown"),
        ({"regime_profile": STORMY}, "regime"),
        (
            {"bias_profile": BiasProfile(overconfidence=0.8, available=True)},
            "behaviour",
        ),
        ({"insights": losing_insights()}, "expectancy"),
    ],
)
def test_any_single_adverse_finding_stops_a_forward_posture(changes, why):
    """One reason is enough to hold back. That asymmetry is the whole design."""
    result = compute(**changes)

    assert result.posture in ("neutral", "conservative")
    assert not result.details["forward_allowed"]
    assert result.details["unmet_requirements"]


def test_a_high_score_is_still_gated_by_the_requirements():
    """The gate is not a modifier: a score in the assertive band still gives
    neutral when a requirement is unmet."""
    result = compute(bias_profile=BiasProfile(overconfidence=0.8, available=True))

    assert result.score >= 1.0
    assert result.posture == "neutral"


def test_the_unmet_requirements_are_named():
    result = compute(pnl_history={"current_drawdown": -0.12})
    assert any(
        "drawdown" in reason for reason in result.details["unmet_requirements"]
    )
    assert any("hold back" in note for note in result.notes)


def test_every_requirement_is_documented():
    assert len(FORWARD_REQUIREMENTS) == 4
    assert all(isinstance(text, str) and text for text in FORWARD_REQUIREMENTS)


def test_nothing_measurable_gives_neutral_not_conservative():
    """With nothing measurable, pushing and holding back are equally
    unjustified. Neutral is the only honest answer."""
    result = compute_meta_strategy(now=NOW)

    assert result.posture == "neutral"
    assert result.confidence == "low"
    assert result.sizing_multiplier == 1.0


def test_low_confidence_forbids_a_forward_posture_outright():
    result = compute(insights=None, regime_profile=None, bias_profile=None)
    assert result.confidence == "low"
    assert result.posture == "neutral"


def test_low_confidence_and_an_open_gate_cannot_coexist():
    """The coupling the engine relies on instead of a second guard.

    Every path that produces low confidence also closes the gate, because an
    unestablished expectancy is itself an unmet requirement. If someone
    loosens the expectancy gate, this is what notices.
    """
    cases = [
        {},
        {"insights": None},
        {"insights": profitable_insights(24)},
        {"insights": losing_insights()},
        {"regime_profile": None, "bias_profile": None},
        {"pnl_history": None},
    ]
    for changes in cases:
        result = compute(**changes)
        if result.confidence == "low":
            assert not result.details["forward_allowed"], changes
            assert result.posture in ("neutral", "conservative"), changes


def test_a_positive_but_unestablished_expectancy_does_not_open_the_gate():
    """Positive is not the same as established.

    A positive expectancy over a dozen trades is exactly the weak evidence
    this layer says must not justify raising. It counts toward the score and
    does not open the way to a forward posture -- without which the whole
    forward path could rest on a run.
    """
    thin = profitable_insights(24)
    assert thin.get("expectancy_trend").confidence != "confident"

    result = compute(insights=thin)
    assert result.contributions.get("expectancy", 0) > 0
    assert result.posture in ("neutral", "conservative")
    assert any("not yet established" in r
               for r in result.details["unmet_requirements"])


# --------------------------------------------------------------------------- #
# Real money counts against
# --------------------------------------------------------------------------- #
def test_a_live_account_reaches_forward_on_stronger_evidence():
    """The inputs are identical; what a mistake costs is not."""
    virtual = compute(account_state="virtual")
    live = compute(account_state="live")

    assert POSTURES.index(live.posture) <= POSTURES.index(virtual.posture)


def test_a_live_account_is_never_opportunistic():
    result = compute(account_state="live")
    assert result.posture != "opportunistic"
    if virtual := compute(account_state="virtual"):
        if virtual.posture == "opportunistic":
            assert any("real-money" in note for note in result.notes)


def test_the_account_can_be_given_in_any_shape():
    for state in ("live", {"account": "live"}, {"type": "live"}):
        assert compute(account_state=state).account == "live"


def test_the_simulation_account_is_allowed_to_be_wrong():
    result = compute(account_state="simulation")
    assert result.contributions.get("account", 0) > 0


# --------------------------------------------------------------------------- #
# Determinism and explainability
# --------------------------------------------------------------------------- #
def test_the_posture_is_deterministic():
    assert compute().posture == compute().posture
    assert compute().score == compute().score


def test_the_contributions_add_up_to_the_score():
    """Adding them up by hand is the point. A score you cannot decompose is a
    number nobody can argue with."""
    result = compute()
    assert sum(result.contributions.values()) == pytest.approx(result.score)


def test_every_posture_carries_a_reason():
    for changes in ({}, {"regime_profile": STORMY}, {"insights": None}):
        assert compute(**changes).notes


def test_the_bands_are_read_from_the_score():
    assert posture_for(5.0, forward_allowed=True) == "opportunistic"
    assert posture_for(1.2, forward_allowed=True) == "assertive"
    assert posture_for(0.0, forward_allowed=True) == "neutral"
    assert posture_for(-5.0, forward_allowed=True) == "conservative"


def test_the_gate_only_removes_forward_postures():
    """It never makes a conservative posture worse."""
    assert posture_for(5.0, forward_allowed=False) == "neutral"
    assert posture_for(-5.0, forward_allowed=False) == "conservative"


def test_behaviour_only_ever_counts_against():
    """There is no pattern whose presence argues for a larger position."""
    for severity in (0.0, 0.2, 0.6, 0.9):
        result = compute(
            bias_profile=BiasProfile(overconfidence=severity, available=True)
        )
        assert result.contributions.get("behaviour", 0) <= 0.25


# --------------------------------------------------------------------------- #
# It fills the contract the refinements already read
# --------------------------------------------------------------------------- #
def test_the_profile_fills_the_existing_plan_contract():
    from cfdinvest.livehelper.context import MetaStrategyPlan

    plan = compute().to_plan()
    assert isinstance(plan, MetaStrategyPlan)
    assert plan.available
    assert plan.target_scale == pytest.approx(compute().sizing_multiplier)


def test_the_plan_reaches_the_sizing_refinement():
    intent = parse_trade_intent("long SPX500 1.0 entry=4434.8 stop=4390 lev=20")
    roomy = LiveHelperConfig(
        account_balance_gbp=100_000.0, max_exposure_per_instrument_gbp=100_000.0
    )
    plan = profile_of("conservative").to_plan()

    result = refine_sizing(
        intent, None, None, TradingContext(plan=plan), roomy
    )
    assert result.contributions.get("meta_strategy") == pytest.approx(0.60)


# --------------------------------------------------------------------------- #
# The ceiling
# --------------------------------------------------------------------------- #
def build(config=None, text="long SPX500 1.0 entry=4434.8 stop=4390 tp=4520 lev=20"):
    """A trade, its assessment, and the two refinements."""
    limits = config or LiveHelperConfig(
        account_balance_gbp=100_000.0, max_exposure_per_instrument_gbp=2_000.0
    )
    intent = parse_trade_intent(text)
    assessment = evaluate_trade_safety(intent, limits, MarketState())
    sizing = compute_safe_sizing(intent, limits, MarketState())
    return (
        intent,
        assessment,
        sizing,
        refine_sizing(intent, assessment, sizing, TradingContext(), limits),
        refine_exit(intent, assessment, TradingContext(), limits),
        limits,
    )


def test_a_forward_posture_is_capped_at_the_safe_size():
    """The property this whole layer is bounded by."""
    _, assessment, sizing, sizing_refinement, exit_refinement, _ = build()

    result = refine_with_meta(
        sizing_refinement, exit_refinement, assessment,
        profile_of("opportunistic"), sizing_recommendation=sizing,
    )
    assert result.capped_by_safety
    assert result.sizing.adjusted_quantity <= sizing.recommended_quantity


def test_being_overruled_is_reported_rather_than_absorbed():
    """A posture applied and overruled reads very differently from one that
    never asked."""
    _, assessment, sizing, sizing_refinement, exit_refinement, _ = build()

    result = refine_with_meta(
        sizing_refinement, exit_refinement, assessment,
        profile_of("opportunistic"), sizing_recommendation=sizing,
    )
    assert result.requested_more_than_allowed
    assert any("cannot create any" in finding for finding in result.findings)


def test_a_raise_with_no_ceiling_available_says_so():
    """Silence there would read as safe."""
    intent = parse_trade_intent("long SPX500 1.0 entry=4434.8 stop=4390")
    roomy = LiveHelperConfig(
        account_balance_gbp=100_000.0, max_exposure_per_instrument_gbp=100_000.0
    )
    bare = refine_sizing(intent, None, None, TradingContext(), roomy)

    result = refine_with_meta(bare, None, None, profile_of("opportunistic"))
    assert any("unbounded until one is" in f for f in result.findings)


def test_a_conservative_posture_cuts_and_is_never_capped():
    _, assessment, sizing, sizing_refinement, exit_refinement, _ = build()

    result = refine_with_meta(
        sizing_refinement, exit_refinement, assessment,
        profile_of("conservative"), sizing_recommendation=sizing,
    )
    assert not result.capped_by_safety
    assert result.sizing.multiplier < sizing_refinement.multiplier


def test_no_profile_changes_nothing():
    _, assessment, sizing, sizing_refinement, exit_refinement, _ = build()

    result = refine_with_meta(sizing_refinement, exit_refinement, assessment, None)
    assert result.sizing is sizing_refinement
    assert not result.changed


def test_a_neutral_posture_leaves_the_multipliers_alone():
    _, assessment, sizing, sizing_refinement, exit_refinement, _ = build()

    result = refine_with_meta(
        sizing_refinement, exit_refinement, assessment, profile_of("neutral"),
        sizing_recommendation=sizing,
    )
    assert result.sizing.multiplier == pytest.approx(sizing_refinement.multiplier)


# --------------------------------------------------------------------------- #
# Exits widen rather than tighten
# --------------------------------------------------------------------------- #
def test_a_forward_posture_widens_the_stop():
    """Pushing size and tightening stops at once compounds two risks while
    reading like one decision."""
    _, assessment, sizing, sizing_refinement, exit_refinement, _ = build()

    result = refine_with_meta(
        sizing_refinement, exit_refinement, assessment,
        profile_of("opportunistic"), sizing_recommendation=sizing,
    )
    assert result.exit.exit_multiplier > exit_refinement.exit_multiplier
    assert result.exit.adjusted_stop < exit_refinement.original_stop


def test_a_conservative_posture_brings_the_stop_in():
    _, assessment, sizing, sizing_refinement, exit_refinement, _ = build()

    result = refine_with_meta(
        sizing_refinement, exit_refinement, assessment,
        profile_of("conservative"), sizing_recommendation=sizing,
    )
    assert result.exit.exit_multiplier < exit_refinement.exit_multiplier
    assert result.exit.adjusted_stop > exit_refinement.original_stop


def test_the_exit_refinement_records_the_entry_it_was_built_from():
    """Both levels are distances from it. Without it nothing downstream can
    move a level again — it has the two ends of a line and not the point they
    are measured from."""
    _, _, _, _, exit_refinement, _ = build()
    assert exit_refinement.entry_price == pytest.approx(4434.8)


# --------------------------------------------------------------------------- #
# The tolerance findings
# --------------------------------------------------------------------------- #
def test_a_tightened_tolerance_is_reported():
    _, assessment, sizing, sizing_refinement, exit_refinement, _ = build()

    result = refine_with_meta(
        sizing_refinement, exit_refinement, assessment,
        profile_of("conservative"), sizing_recommendation=sizing,
    )
    assert any("tightened safety tolerance" in f for f in result.findings)
    assert any("tightened margin tolerance" in f for f in result.findings)


def test_relaxing_a_tolerance_says_it_cannot_go_past_the_limit():
    """"Relaxed" means back toward the limit you set, never past it."""
    _, assessment, sizing, sizing_refinement, exit_refinement, _ = build()

    result = refine_with_meta(
        sizing_refinement, exit_refinement, assessment,
        profile_of("opportunistic"), sizing_recommendation=sizing,
    )
    relaxed = [f for f in result.findings if "relaxed" in f]
    assert relaxed
    assert all("cannot go past" in finding for finding in relaxed)


def test_the_assessment_is_not_rewritten():
    """A layer that edited an assessment in place would leave two versions of
    the same verdict in circulation."""
    _, assessment, sizing, sizing_refinement, exit_refinement, _ = build()
    before = assessment.status

    refine_with_meta(
        sizing_refinement, exit_refinement, assessment,
        profile_of("opportunistic"), sizing_recommendation=sizing,
    )
    assert assessment.status == before


# --------------------------------------------------------------------------- #
# The simulation journal
# --------------------------------------------------------------------------- #
def sequence(*names):
    return [
        (NOW + pd.Timedelta(days=index), profile_of(name))
        for index, name in enumerate(names)
    ]


def test_the_journal_records_each_move():
    from cfdinvest.simulate.meta import simulate_postures

    journal = simulate_postures(
        sequence("neutral", "neutral", "assertive", "conservative")
    )
    assert journal.moved == 2
    assert journal.postures["neutral"] == 2


def test_a_journal_with_no_moves_says_what_that_means():
    """With one posture throughout, the rehearsal measures a single
    multiplier."""
    from cfdinvest.simulate.meta import simulate_postures

    journal = simulate_postures(sequence("neutral", "neutral", "neutral"))
    assert journal.moved == 0
    assert any("single multiplier" in note for note in journal.notes)


def test_a_ratcheting_posture_is_called_out():
    """A posture that only moves one way is following the record rather than
    responding to it."""
    from cfdinvest.simulate.meta import simulate_postures

    journal = simulate_postures(
        sequence("conservative", "neutral", "assertive", "opportunistic")
    )
    assert journal.drifted_forward
    assert any("ratchets one way" in note for note in journal.notes)


def test_the_journal_counts_what_the_safety_engine_refused():
    from cfdinvest.simulate.meta import simulate_postures

    journal = simulate_postures(
        sequence("assertive", "assertive"), capped_flags=[True, True, False]
    )
    assert journal.capped == 2
    assert any("applied and overruled" in note for note in journal.notes)


def test_the_journal_document_is_marked_simulated():
    from cfdinvest.simulate.meta import simulate_postures

    document = simulate_postures(sequence("neutral", "assertive")).to_document()
    assert next(iter(document)) == "simulated"
    assert document["simulated"] is True


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
def test_a_profile_round_trips(lake):
    path = store.write_meta_strategy(
        compute(), lake, inputs={"drawdown": -0.01}
    )
    assert Path(path).name == "20260825-120000-metastrategy.json"

    document = store.load_latest_meta_strategy(lake)
    assert document["profile"]["posture"] in POSTURES
    assert document["inputs"] == {"drawdown": -0.01}
    assert document["notes"]


def test_the_history_shows_how_the_posture_moved(lake):
    for hour, changes in (
        (9, {}),
        (10, {"pnl_history": {"current_drawdown": -0.12}}),
    ):
        profile = compute_meta_strategy(
            insights=profitable_insights(),
            bias_profile=CLEAN,
            regime_profile=CALM,
            account_state="virtual",
            now=pd.Timestamp(f"2026-08-25 {hour}:00", tz="UTC"),
            **({"pnl_history": {"current_drawdown": -0.01}} | changes),
        )
        store.write_meta_strategy(profile, lake)

    history = store.load_meta_strategy_history(lake)
    assert len(history) == 2
    assert history["timestamp"].is_monotonic_increasing
    assert history["posture"].nunique() >= 1


def test_a_failed_write_returns_none(lake, monkeypatch):
    monkeypatch.setattr(
        store, "atomic_write_text",
        lambda *a, **k: (_ for _ in ()).throw(OSError("no space")),
    )
    assert store.write_meta_strategy(compute(), lake) is None


def test_no_profiles_is_an_empty_history(lake):
    assert store.load_latest_meta_strategy(lake) is None
    assert store.load_meta_strategy_history(lake).empty


def test_a_profile_survives_a_round_trip_through_json(lake):
    store.write_meta_strategy(compute(), lake)
    document = store.load_latest_meta_strategy(lake)

    rebuilt = MetaStrategyProfile.from_document(document["profile"])
    assert rebuilt.posture == compute().posture
    assert rebuilt.sizing_multiplier == pytest.approx(compute().sizing_multiplier)


# --------------------------------------------------------------------------- #
# Boundaries
# --------------------------------------------------------------------------- #
def test_the_layer_places_nothing_and_recomputes_nothing():
    import ast

    for module in sorted(Path("cfdinvest/metastrategy").glob("*.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        imported = {
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        } | {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        forbidden = (
            "requests", "urllib", "httpx", "cfdinvest.backtest",
            "cfdinvest.execution", "cfdinvest.schedule.runner",
        )
        assert not [
            name for name in imported
            if any(name == bad or name.startswith(bad + ".") for bad in forbidden)
        ], f"{module.name} imports something it should not"


def test_expectancy_comes_from_the_insights_layer():
    """One expectancy in the project, not two."""
    source = Path("cfdinvest/metastrategy/engine.py").read_text(encoding="utf-8")
    assert "expectancy_trend" in source
    assert "def _expectancy(" not in source
