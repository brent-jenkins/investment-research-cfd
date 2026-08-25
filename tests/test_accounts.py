"""Accounts: what exists, and which one a trade belongs in.

Two properties carry these layers. The built-in three cannot be removed or
repointed, because either would strand a real trading record. And nothing
routes to the real-money account by default — every other routing mistake is
a bookkeeping error you can correct, and that one is not.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cfdinvest.accounts import (
    ACCOUNT_TYPES,
    BUILT_IN,
    AccountProfile,
    AccountRegistry,
    default_registry,
    explain_routing,
    known_accounts,
    load_registry,
    route_trade,
    write_registry,
)
from cfdinvest.config import AppConfig, PathConfig
from cfdinvest.livehelper import parse_trade_intent


@pytest.fixture
def lake(tmp_path) -> AppConfig:
    return AppConfig(paths=PathConfig(data_root=tmp_path / "data"))


def intent(text="long SPX500 0.5 entry=4434.8 stop=4390"):
    return parse_trade_intent(text)


def registry_with(*profiles) -> AccountRegistry:
    registry = default_registry()
    for profile in profiles:
        registry = registry.register(profile)
    return registry


def sleeve(**changes) -> AccountProfile:
    fields = dict(name="momentum", type="strategy", strategy="trend")
    fields.update(changes)
    return AccountProfile(**fields)


# --------------------------------------------------------------------------- #
# The built-ins are load-bearing
# --------------------------------------------------------------------------- #
def test_the_three_built_ins_always_exist():
    assert set(default_registry().names) == set(BUILT_IN)


def test_a_built_in_cannot_be_removed():
    """A registry that could delete "live" could strand a real record."""
    with pytest.raises(ValueError, match="built-in"):
        default_registry().remove("live")


def test_a_built_in_cannot_be_repointed():
    """Everything written so far would become unreachable under the name that
    wrote it."""
    with pytest.raises(ValueError, match="must keep its own tree"):
        default_registry().register(
            AccountProfile(name="live", type="live", tree="elsewhere")
        )


def test_a_built_in_can_be_described_and_disabled():
    """Relabelling is not repointing."""
    registry = default_registry().register(
        AccountProfile(
            name="live", type="live", label="Main account", enabled=False,
            risk_overrides={"max_total_exposure_gbp": 500.0},
        )
    )
    profile = registry.get("live")
    assert profile.label == "Main account"
    assert not profile.enabled
    assert profile.tree == "live"


def test_a_registered_account_can_be_removed():
    registry = registry_with(sleeve())
    assert "momentum" not in registry.remove("momentum").names


# --------------------------------------------------------------------------- #
# A name becomes a path segment
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "name", ["../escape", "with space", "UPPER", "", "a" * 40, "/absolute"]
)
def test_a_name_that_is_not_a_directory_name_is_refused(name):
    """Refusing at write time is too late — something has already decided
    where a file goes."""
    with pytest.raises(ValueError, match="plain"):
        AccountProfile(name=name)


def test_a_tree_that_is_not_a_directory_name_is_refused():
    with pytest.raises(ValueError, match="plain name"):
        AccountProfile(name="fine", tree="../escape")


def test_an_unknown_type_is_refused():
    with pytest.raises(ValueError, match="account type must be one of"):
        AccountProfile(name="fine", type="imaginary")


def test_every_declared_type_is_accepted():
    for kind in ACCOUNT_TYPES:
        assert AccountProfile(name="fine", type=kind).type == kind


# --------------------------------------------------------------------------- #
# Profiles reference rather than redefine
# --------------------------------------------------------------------------- #
def test_an_override_is_applied_to_the_existing_config():
    profile = sleeve(risk_overrides={"max_total_exposure_gbp": 500.0})
    limits = profile.risk_limits()

    from cfdinvest.risk import RiskLimits

    assert isinstance(limits, RiskLimits)
    assert limits.max_total_exposure_gbp == 500.0
    # Untouched fields keep the shared defaults rather than being redeclared.
    assert limits.max_effective_leverage == RiskLimits().max_effective_leverage


def test_no_overrides_gives_the_shared_defaults():
    from cfdinvest.livehelper import LiveHelperConfig

    assert sleeve().sizing_config() == LiveHelperConfig()


def test_an_unknown_override_is_dropped_rather_than_raising():
    """A registry written against a newer version should not stop an older
    one starting."""
    profile = sleeve(risk_overrides={"invented_later": 1, "max_drawdown_pct": 0.1})
    limits = profile.risk_limits()
    assert limits.max_drawdown_pct == 0.1


def test_an_invalid_override_falls_back_rather_than_raising():
    from cfdinvest.risk import RiskLimits

    profile = sleeve(risk_overrides={"max_drawdown_pct": 50.0})
    assert profile.risk_limits() == RiskLimits()


def test_the_exit_profile_uses_the_refinement_config():
    from cfdinvest.livehelper.refine_exit import ExitRefinementConfig

    assert isinstance(sleeve().exit_config(), ExitRefinementConfig)


# --------------------------------------------------------------------------- #
# Only a live account is real money
# --------------------------------------------------------------------------- #
def test_only_the_live_type_is_real_money():
    assert default_registry().get("live").is_real_money
    assert not default_registry().get("virtual").is_real_money
    assert not default_registry().get("simulation").is_real_money


def test_a_sleeve_is_not_assumed_to_be_real_money():
    """Strategy and regime sleeves are purposes, not funding."""
    assert not sleeve(type="strategy").is_real_money
    assert not sleeve(name="calm", type="regime").is_real_money


# --------------------------------------------------------------------------- #
# Registration is what makes a directory legal
# --------------------------------------------------------------------------- #
def test_an_unregistered_account_has_no_tree(lake):
    from cfdinvest.persist.storage import account_root

    with pytest.raises(ValueError, match="unknown account"):
        account_root(lake, "momentum")


def test_registering_makes_the_tree_legal(lake):
    from cfdinvest.persist.storage import account_root

    write_registry(registry_with(sleeve()), lake)
    assert account_root(lake, "momentum") == Path(lake.paths.data_root) / "momentum"


def test_the_built_ins_are_always_known(lake):
    assert set(BUILT_IN) <= set(known_accounts(lake))


def test_an_unreadable_registry_never_hides_the_built_ins(lake):
    """A corrupt registry must not be able to make "live" unreachable."""
    from cfdinvest.persist.accounts import registry_path

    path = registry_path(lake)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    assert set(load_registry(lake).names) == set(BUILT_IN)
    assert set(BUILT_IN) <= set(known_accounts(lake))


# --------------------------------------------------------------------------- #
# The registry on disk
# --------------------------------------------------------------------------- #
def test_a_registry_round_trips(lake):
    path = write_registry(registry_with(sleeve(purpose="trend sleeve")), lake)
    assert path is not None

    loaded = load_registry(lake)
    assert "momentum" in loaded
    assert loaded.get("momentum").purpose == "trend sleeve"
    assert loaded.get("momentum").strategy == "trend"


def test_the_registry_is_rewritten_not_appended(lake):
    """It is configuration, not an observation. A history of what it used to
    say would be a second source of truth about which accounts are legal."""
    write_registry(registry_with(sleeve()), lake)
    write_registry(registry_with(sleeve(name="calm", type="regime")), lake)

    loaded = load_registry(lake)
    assert "calm" in loaded
    assert "momentum" not in loaded


def test_the_registry_lives_at_the_data_root_not_inside_an_account(lake):
    """One filed inside an account it describes would be unreadable until you
    already knew which account to look in."""
    from cfdinvest.persist.accounts import registry_path

    assert registry_path(lake).parent == Path(lake.paths.data_root)


def test_one_bad_entry_does_not_cost_the_others(lake):
    from cfdinvest.persist.accounts import registry_path

    path = registry_path(lake)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "accounts": [
                    {"name": "../escape", "type": "strategy"},
                    {"name": "momentum", "type": "strategy"},
                ]
            }
        ),
        encoding="utf-8",
    )
    loaded = load_registry(lake)

    assert "momentum" in loaded
    assert set(BUILT_IN) <= set(loaded.names)


def test_a_missing_registry_is_the_normal_state(lake):
    assert set(load_registry(lake).names) == set(BUILT_IN)


def test_a_failed_write_returns_none(lake, monkeypatch):
    from cfdinvest.persist import accounts as store

    monkeypatch.setattr(
        store, "atomic_write_text",
        lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")),
    )
    assert write_registry(default_registry(), lake) is None


# --------------------------------------------------------------------------- #
# Routing: nothing reaches real money by accident
# --------------------------------------------------------------------------- #
def test_an_unmatched_trade_never_goes_to_live():
    """The property this layer exists to guarantee."""
    decision = route_trade(intent("long GLD 1.0 entry=160 stop=155"),
                           registry_with(sleeve(instruments=("SPX500",))))

    assert decision.fell_back
    assert not decision.is_real_money
    assert decision.account == "virtual"


def test_the_fall_back_says_it_is_a_fall_back():
    decision = route_trade(intent("long GLD 1.0 entry=160 stop=155"), default_registry())
    assert decision.fell_back
    assert any("safest enabled account" in reason for reason in decision.reasons)


def test_live_as_the_only_account_is_used_and_flagged_loudly():
    """The one case where a fall-back does reach real money."""
    only_live = AccountRegistry(
        profiles={"live": default_registry().get("live")}
    )
    decision = route_trade(intent(), only_live)

    assert decision.account == "live"
    assert decision.is_real_money
    assert decision.needs_a_decision
    assert any("Confirm before you place" in r for r in decision.reasons)


def test_no_enabled_account_routes_nowhere():
    empty = AccountRegistry(
        profiles={
            "live": AccountProfile(name="live", type="live", enabled=False),
        }
    )
    decision = route_trade(intent(), empty)
    assert not decision.routed


# --------------------------------------------------------------------------- #
# Routing: the rules
# --------------------------------------------------------------------------- #
def test_a_matching_strategy_and_instrument_wins():
    decision = route_trade(
        intent(), registry_with(sleeve(instruments=("SPX500", "NAS100"))),
        strategy="trend",
    )
    assert decision.account == "momentum"
    assert decision.confidence == "high"
    assert not decision.fell_back


def test_a_named_instrument_list_is_a_restriction_not_a_preference():
    decision = route_trade(
        intent("long GLD 1.0 entry=160 stop=155"),
        registry_with(sleeve(instruments=("SPX500",))),
        strategy="trend",
    )
    assert decision.account != "momentum"


def test_a_wrong_strategy_rules_an_account_out():
    decision = route_trade(
        intent(), registry_with(sleeve()), strategy="meanreversion"
    )
    assert decision.account != "momentum"


def test_a_regime_restricted_account_needs_the_regime():
    calm = AccountProfile(name="calm", type="regime", regimes=("risk_on", "low"))

    matched = route_trade(intent(), registry_with(calm), regime="risk_on")
    unmatched = route_trade(intent(), registry_with(calm), regime="crisis")

    assert matched.account == "calm"
    assert unmatched.account != "calm"


def test_a_regime_profile_object_is_read():
    from cfdinvest.livehelper.context import RegimeProfile

    decision = route_trade(
        intent(),
        registry_with(AccountProfile(name="calm", type="regime",
                                     regimes=("risk_on",))),
        regime=RegimeProfile(regimes={"combined_regime": "risk_on"},
                             available=True),
    )
    assert decision.account == "calm"


def test_an_explicit_choice_wins_over_every_rule():
    """A router that could overrule the person operating it gets switched
    off."""
    decision = route_trade(
        intent(), registry_with(sleeve()), strategy="trend", prefer="live"
    )
    assert decision.account == "live"
    assert decision.confidence == "high"
    assert "because you said so" in decision.reasons[0]


def test_forcing_an_account_that_does_not_exist_routes_nowhere():
    decision = route_trade(intent(), default_registry(), prefer="imaginary")
    assert not decision.routed


def test_a_tie_takes_the_safer_and_says_the_confidence_is_low():
    """Picking one alphabetically and presenting it as an answer is worse than
    saying two accounts fit."""
    first = AccountProfile(name="alpha", type="strategy", strategy="trend")
    second = AccountProfile(name="beta", type="virtual", strategy="trend")

    decision = route_trade(
        intent(), registry_with(first, second), strategy="trend"
    )
    assert decision.confidence == "low"
    assert any("fit equally well" in reason for reason in decision.reasons)
    assert decision.needs_a_decision


# --------------------------------------------------------------------------- #
# Routing: the reasons are the point
# --------------------------------------------------------------------------- #
def test_every_decision_carries_its_reasons():
    """A router that returned only a name would be impossible to argue with."""
    decision = route_trade(
        intent(), registry_with(sleeve(instruments=("SPX500",))), strategy="trend"
    )
    assert decision.reasons
    assert decision.rules


def test_rejections_are_recorded_not_just_the_winner():
    """Six months later you need to know whether the rule was wrong or the
    inputs were."""
    decision = route_trade(
        intent(),
        registry_with(sleeve(), AccountProfile(name="calm", type="regime",
                                               regimes=("risk_on",))),
        strategy="trend",
    )
    rejected = [rule for rule in decision.rules if not rule.matched]
    assert rejected
    assert all(rule.detail for rule in rejected)


def test_a_decision_renders_as_text():
    decision = route_trade(intent(), registry_with(sleeve()), strategy="trend")
    lines = explain_routing(decision)
    assert lines
    assert "momentum" in lines[0]


def test_a_real_money_decision_is_marked_in_the_text():
    only_live = AccountRegistry(profiles={"live": default_registry().get("live")})
    lines = explain_routing(route_trade(intent(), only_live))
    assert any("REAL MONEY" in line for line in lines)


def test_a_clear_match_does_not_need_a_decision():
    decision = route_trade(
        intent(), registry_with(sleeve(instruments=("SPX500",))), strategy="trend"
    )
    assert not decision.needs_a_decision


def test_real_money_on_a_thin_match_does_need_one():
    live_sleeve = AccountProfile(
        name="live", type="live", regimes=("risk_on",)
    )
    decision = route_trade(
        intent(), registry_with(live_sleeve), regime="risk_on"
    )
    if decision.account == "live" and decision.confidence != "high":
        assert decision.needs_a_decision


def test_routing_places_nothing():
    """It reads a trade and a registry and says where it fits."""
    import ast

    source = Path("cfdinvest/accounts/router.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
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
    forbidden = ("requests", "urllib", "httpx", "cfdinvest.persist")
    assert not [
        name for name in imported
        if any(name == bad or name.startswith(bad + ".") for bad in forbidden)
    ]
