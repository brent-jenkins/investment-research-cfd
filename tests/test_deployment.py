"""Deployment configuration, and keeping virtual apart from live.

Two kinds of test. The first reads the files in ``deployment/`` and checks the
handful of lines that, if they were wrong, would expose the dashboard or leak
a credential — these are configuration files nobody executes in CI, so a typo
in one is otherwise found by a stranger. The second checks that the account
separation actually separates.
"""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd
import pytest

from cfdinvest.config import AppConfig, PathConfig
from cfdinvest.env import (
    ACCOUNT_MODES,
    Environment,
    account_mode,
    load_env,
    parse_env_file,
    read_environment,
    redact,
    resolve_account,
    smtp_password,
)

DEPLOYMENT = Path("deployment")
CADDYFILE = DEPLOYMENT / "caddy" / "Caddyfile"
UNITS = sorted((DEPLOYMENT / "systemd").glob("*.service"))
TIMERS = sorted((DEPLOYMENT / "systemd").glob("*.timer"))


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Caddy
# --------------------------------------------------------------------------- #
def test_the_caddyfile_serves_the_right_name():
    assert "cfd.example.com {" in text(CADDYFILE)


def test_the_caddyfile_requires_basic_auth():
    """Without this the dashboard is a public page showing account balances."""
    assert re.search(r"basic_?auth\s*\{", text(CADDYFILE))


def test_the_caddyfile_proxies_to_streamlit_on_localhost():
    assert "reverse_proxy 127.0.0.1:8501" in text(CADDYFILE)


def test_the_caddyfile_reads_the_credential_from_the_environment():
    """A bcrypt hash is still a credential artefact. Not in the repository."""
    body = text(CADDYFILE)
    assert "{$CFD_BASIC_AUTH_USER}" in body
    assert "{$CFD_BASIC_AUTH_HASH}" in body


def test_the_caddyfile_sets_transport_security():
    body = text(CADDYFILE)
    assert "Strict-Transport-Security" in body
    assert 'X-Frame-Options "DENY"' in body


def test_the_caddyfile_rotates_its_log():
    """An unrotated access log is a slow way to fill the disk."""
    assert "roll_size" in text(CADDYFILE)


# --------------------------------------------------------------------------- #
# systemd
# --------------------------------------------------------------------------- #
def test_the_units_exist():
    assert {path.name for path in UNITS} == {
        "cfdinvest-dashboard.service",
        "cfdinvest-pipeline.service",
        "cfdinvest-monitor.service",
    }


@pytest.mark.parametrize("unit", UNITS, ids=lambda p: p.name)
def test_every_unit_reads_its_secrets_from_a_file(unit):
    """Never from the unit, and never from a command line -- a password in
    ExecStart is a password in `ps`."""
    body = text(unit)
    assert "EnvironmentFile=/etc/cfdinvest/env" in body
    assert not re.search(r"^Environment=.*(PASSWORD|SECRET|KEY)=", body, re.M)


@pytest.mark.parametrize("unit", UNITS, ids=lambda p: p.name)
def test_every_unit_runs_as_the_service_user(unit):
    body = text(unit)
    assert "User=cfdinvest" in body
    assert "User=root" not in body


@pytest.mark.parametrize("unit", UNITS, ids=lambda p: p.name)
def test_every_unit_is_confined(unit):
    body = text(unit)
    assert "ProtectSystem=strict" in body
    assert "NoNewPrivileges=true" in body


@pytest.mark.parametrize("unit", UNITS, ids=lambda p: p.name)
def test_every_unit_invokes_something_that_exists(unit):
    """The check that was missing.

    Two units shipped invoking `cfdinvest-schedule`, which pyproject never
    declared -- so on a real deployment both would have failed with "No such
    file or directory" and nothing here would have noticed. Unit files are not
    executed by the test suite, so what they invoke has to be checked against
    what is actually installed.
    """
    import re
    import tomllib

    declared = set(
        tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
        .get("project", {})
        .get("scripts", {})
    )

    for line in text(unit).splitlines():
        if not line.startswith("ExecStart="):
            continue
        command = line[len("ExecStart="):].split()[0]
        name = Path(command).name
        # Console scripts a *dependency* installs into the same venv. Listed
        # rather than pattern-matched, so adding one is a deliberate act.
        from_dependencies = {"streamlit", "python", "python3"}

        assert name in declared or name in from_dependencies, (
            f"{unit.name} runs {name!r}, which is neither a declared console "
            f"script nor a known dependency binary. Declared: "
            f"{', '.join(sorted(declared))}"
        )


def test_the_readme_only_promises_commands_that_exist():
    """The deployment instructions are followed by hand, so a command that
    does not exist is a step that silently fails."""
    import tomllib

    declared = set(
        tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
        .get("project", {})
        .get("scripts", {})
    )
    body = text(DEPLOYMENT / "README.md")

    for name in re.findall(r"cfdinvest-[a-z]+", body):
        assert name in declared, f"the README promises {name!r}, which is not declared"


def test_the_dashboard_binds_localhost_only():
    """The load-bearing argument.

    Without it Streamlit binds every interface and answers on 8501 directly,
    going round Caddy and therefore round the basic auth. The proxy is only a
    boundary because the app is unreachable any other way.
    """
    body = text(DEPLOYMENT / "systemd" / "cfdinvest-dashboard.service")
    assert "--server.address 127.0.0.1" in body
    assert "0.0.0.0" not in body


def test_the_dashboard_cannot_write_to_the_lake():
    """It is read-only over the lake. If it ever needs to write, that is a bug
    in the dashboard rather than a line to add to the unit."""
    body = text(DEPLOYMENT / "systemd" / "cfdinvest-dashboard.service")
    assert "ReadOnlyPaths=/var/lib/cfdinvest" in body


def test_the_pipeline_may_write_the_lake():
    body = text(DEPLOYMENT / "systemd" / "cfdinvest-pipeline.service")
    assert "ReadWritePaths=/var/lib/cfdinvest" in body


def test_the_pipeline_cannot_hang_forever():
    """A run stuck on a vendor must not hold the timer open."""
    assert "TimeoutStartSec=" in text(
        DEPLOYMENT / "systemd" / "cfdinvest-pipeline.service"
    )


def test_a_red_monitor_is_not_a_failed_unit():
    """A red status exits 1. That is the check working.

    Without SuccessExitStatus, systemd marks the service failed every time it
    does its job, and a permanently-failed unit is one nobody looks at.
    """
    body = text(DEPLOYMENT / "systemd" / "cfdinvest-monitor.service")
    assert re.search(r"^SuccessExitStatus=.*\b1\b", body, re.M)


@pytest.mark.parametrize("timer", TIMERS, ids=lambda p: p.name)
def test_every_timer_survives_a_reboot(timer):
    """A machine asleep at the trigger should still run, not skip in silence."""
    assert "Persistent=true" in text(timer)


def test_the_pipeline_timer_is_in_utc():
    """A wall-clock schedule drifts an hour twice a year, invisibly, until a
    run lands mid-session."""
    body = text(DEPLOYMENT / "systemd" / "cfdinvest-pipeline.timer")
    assert re.search(r"^OnCalendar=.*UTC", body, re.M)


# --------------------------------------------------------------------------- #
# Nothing secret is committed
# --------------------------------------------------------------------------- #
#: What a leaked credential tends to look like in a config file: an assignment
#: whose value is present and is not a placeholder.
_ASSIGNMENT = re.compile(
    # [ \t]* rather than \s*: \s matches a newline, so an empty assignment
    # swallowed the *next* line's value and reported it as a leak.
    r"^[^#\n]*\b([A-Za-z_]*(?:PASSWORD|SECRET|TOKEN|API_KEY|HASH))[ \t]*=[ \t]*(\S*)",
    re.MULTILINE | re.IGNORECASE,
)

#: A value that is a reference rather than a credential: a Caddy placeholder,
#: a shell expansion, a documentation stand-in, or nothing at all.
_NOT_A_SECRET = re.compile(r"""^$|^["']?[{$<]|^["']{1,2}$""")


@pytest.mark.parametrize(
    "path",
    sorted(p for p in DEPLOYMENT.rglob("*") if p.is_file()),
    ids=lambda p: str(p),
)
def test_no_deployment_file_carries_a_value_for_a_secret(path):
    """Every secret-looking assignment must be empty or a placeholder."""
    for name, value in _ASSIGNMENT.findall(text(path)):
        assert _NOT_A_SECRET.match(value), (
            f"{path} appears to set {name} to a real value"
        )


def test_the_env_example_has_no_values():
    """It documents the variables. Filling one in is how one gets committed."""
    values = parse_env_file(text(DEPLOYMENT / "scripts" / "cfdinvest.env.example"))
    for name in ("CFD_SMTP_PASSWORD", "FRED_API_KEY", "CFD_SMTP_USER"):
        assert values.get(name, "") == "", f"{name} has a value in the example"


def test_the_env_example_covers_what_the_code_reads():
    values = parse_env_file(text(DEPLOYMENT / "scripts" / "cfdinvest.env.example"))
    assert {"CFD_ACCOUNT_MODE", "CFD_DATA_ROOT", "CFD_SMTP_PASSWORD"} <= set(values)


def test_the_hash_script_never_takes_a_password_as_an_argument():
    """It would sit in `ps` and in the shell history."""
    body = text(DEPLOYMENT / "scripts" / "make-basicauth-hash.sh")
    assert "read -rs password" in body
    assert "$1" not in body


# --------------------------------------------------------------------------- #
# Reading the environment
# --------------------------------------------------------------------------- #
def test_an_env_file_is_parsed():
    values = parse_env_file(
        "# a comment\n"
        "CFD_ACCOUNT_MODE=live\n"
        "export CFD_DATA_ROOT=/var/lib/cfdinvest\n"
        'CFD_SMTP_PASSWORD="a b c"\n'
        "\n"
        "not a line\n"
    )
    assert values == {
        "CFD_ACCOUNT_MODE": "live",
        "CFD_DATA_ROOT": "/var/lib/cfdinvest",
        "CFD_SMTP_PASSWORD": "a b c",
    }


def test_the_parser_does_not_expand_anything():
    """The file holds passwords. A parser that could run them is a way to."""
    values = parse_env_file("CFD_SMTP_PASSWORD=$(cat /etc/shadow)\n")
    assert values["CFD_SMTP_PASSWORD"] == "$(cat /etc/shadow)"


def test_a_missing_env_file_is_normal(tmp_path):
    """A developer machine has no /etc/cfdinvest. Refusing to start over a
    mail password would take the whole system down."""
    merged = load_env(tmp_path / "absent", env={"CFD_ACCOUNT_MODE": "live"})
    assert merged["CFD_ACCOUNT_MODE"] == "live"


def test_the_process_environment_wins_over_the_file(tmp_path):
    """systemd has usually loaded the file already, and a second read that
    disagreed with the first is a day to find."""
    path = tmp_path / "env"
    path.write_text("CFD_ACCOUNT_MODE=virtual\n", encoding="utf-8")

    merged = load_env(path, env={"CFD_ACCOUNT_MODE": "live"})
    assert merged["CFD_ACCOUNT_MODE"] == "live"


def test_override_reverses_that(tmp_path):
    path = tmp_path / "env"
    path.write_text("CFD_ACCOUNT_MODE=virtual\n", encoding="utf-8")

    merged = load_env(path, env={"CFD_ACCOUNT_MODE": "live"}, override=True)
    assert merged["CFD_ACCOUNT_MODE"] == "virtual"


def test_reading_does_not_write(tmp_path, monkeypatch):
    path = tmp_path / "env"
    path.write_text("CFD_SOMETHING_NEW=1\n", encoding="utf-8")

    monkeypatch.delenv("CFD_SOMETHING_NEW", raising=False)
    load_env(path)

    import os

    assert "CFD_SOMETHING_NEW" not in os.environ


@pytest.mark.parametrize("mode", ACCOUNT_MODES)
def test_each_account_mode_is_read(mode):
    assert account_mode({"CFD_ACCOUNT_MODE": mode}) == mode


def test_the_bare_variable_is_accepted():
    """It is what the deployment notes say; an unread variable is worse than
    an untidy one."""
    assert account_mode({"ACCOUNT_MODE": "live"}) == "live"


def test_the_prefixed_variable_wins():
    assert account_mode(
        {"CFD_ACCOUNT_MODE": "virtual", "ACCOUNT_MODE": "live"}
    ) == "virtual"


def test_the_mode_is_case_insensitive():
    assert account_mode({"CFD_ACCOUNT_MODE": "LIVE"}) == "live"


def test_an_unrecognised_mode_is_refused_not_guessed():
    """ACCOUNT_MODE=prod silently becoming live is how a demo deployment
    starts writing into the live tree."""
    with pytest.raises(ValueError, match="account mode must be one of"):
        account_mode({"CFD_ACCOUNT_MODE": "prod"})


def test_the_default_is_virtual():
    """The failure of defaulting wrong is asymmetric."""
    assert account_mode({}) == "virtual"


def test_no_variable_means_the_shared_tree_not_virtual():
    """Turning separation on by default would strand every existing artefact."""
    assert resolve_account(env={}) is None


def test_an_explicit_account_wins_over_the_environment():
    assert resolve_account("live", {"CFD_ACCOUNT_MODE": "virtual"}) == "live"


def test_an_explicit_nonsense_account_is_refused():
    with pytest.raises(ValueError, match="unknown account"):
        resolve_account("prod", {})


def test_the_smtp_password_is_read_from_either_name():
    assert smtp_password({"CFD_SMTP_PASSWORD": "a"}) == "a"
    assert smtp_password({"SMTP_PASSWORD": "b"}) == "b"
    assert smtp_password({}) is None


# --------------------------------------------------------------------------- #
# Nothing prints a secret
# --------------------------------------------------------------------------- #
def test_a_secret_is_redacted_to_a_yes_or_no():
    """Never a prefix, never a length: four characters and a length makes a
    short password guessable."""
    assert redact("CFD_SMTP_PASSWORD", "Wibble123!") == "set"
    assert redact("FRED_API_KEY", "abcdef") == "set"
    assert redact("CFD_SMTP_PASSWORD", None) == "not set"


def test_a_non_secret_is_shown():
    assert redact("CFD_ACCOUNT_MODE", "live") == "live"


def test_the_environment_repr_carries_no_secret():
    """This object ends up in a log line and on a configuration page."""
    environment = Environment(account="live", smtp_password_set=True)
    assert "set" in repr(environment)

    described = " ".join(read_environment(env={
        "CFD_SMTP_PASSWORD": "hunter2", "CFD_ACCOUNT_MODE": "live",
    }).describe().values())
    assert "hunter2" not in described


def test_a_bad_mode_does_not_stop_the_environment_being_described():
    """The page that would tell you the mode is bad must still render."""
    environment = read_environment(env={"CFD_ACCOUNT_MODE": "prod"})
    assert environment.account_mode == "virtual"


# --------------------------------------------------------------------------- #
# The trees are actually separate
# --------------------------------------------------------------------------- #
@pytest.fixture
def lake(tmp_path) -> AppConfig:
    return AppConfig(paths=PathConfig(data_root=tmp_path / "data"))


def test_the_shared_root_is_used_when_nothing_is_set(lake, monkeypatch):
    from cfdinvest.persist.storage import account_root

    monkeypatch.delenv("CFD_ACCOUNT_MODE", raising=False)
    monkeypatch.delenv("ACCOUNT_MODE", raising=False)
    assert account_root(lake) == Path(lake.paths.data_root)


@pytest.mark.parametrize("mode", ACCOUNT_MODES)
def test_the_environment_selects_the_tree(lake, monkeypatch, mode):
    from cfdinvest.persist.storage import account_root

    monkeypatch.setenv("CFD_ACCOUNT_MODE", mode)
    assert account_root(lake) == Path(lake.paths.data_root) / mode


def test_confirmations_do_not_cross_between_accounts(lake, monkeypatch):
    """The property the whole feature is for."""
    from cfdinvest.persist.confirm import (
        Confirmation,
        load_confirmations,
        write_confirmation,
    )

    monkeypatch.setenv("CFD_ACCOUNT_MODE", "virtual")
    write_confirmation(
        Confirmation.of("SPX500", "open", direction="long", quantity=1.0,
                        price=4434.8),
        lake,
    )
    assert len(load_confirmations(lake)) == 1

    monkeypatch.setenv("CFD_ACCOUNT_MODE", "live")
    assert load_confirmations(lake) == []


def test_the_scheduler_state_does_not_cross(lake, monkeypatch):
    from cfdinvest.persist import scheduler_state

    monkeypatch.setenv("CFD_ACCOUNT_MODE", "virtual")
    scheduler_state.update_last_run(
        pd.Timestamp("2026-08-25 09:00", tz="UTC"), run_id="r1", config=lake
    )
    assert scheduler_state.load_scheduler_state(lake).get("last_run_id") == "r1"

    monkeypatch.setenv("CFD_ACCOUNT_MODE", "live")
    assert scheduler_state.load_scheduler_state(lake).get("last_run_id") is None


def test_monitoring_snapshots_do_not_cross(lake, monkeypatch):
    from cfdinvest.monitoring.aggregate import MonitoringSnapshot
    from cfdinvest.persist import monitoring as store

    snapshot = MonitoringSnapshot(timestamp=pd.Timestamp("2026-08-25", tz="UTC"))

    monkeypatch.setenv("CFD_ACCOUNT_MODE", "virtual")
    store.write_monitoring_snapshot(snapshot, lake)
    assert store.load_latest_monitoring_snapshot(lake) is not None

    monkeypatch.setenv("CFD_ACCOUNT_MODE", "live")
    assert store.load_latest_monitoring_snapshot(lake) is None


def test_risk_snapshots_do_not_cross(lake, monkeypatch):
    from cfdinvest.persist import risk as store
    from cfdinvest.risk import RiskLimits, build_risk_snapshot

    snapshot = build_risk_snapshot(limits=RiskLimits())

    monkeypatch.setenv("CFD_ACCOUNT_MODE", "virtual")
    store.write_risk_snapshot(snapshot, lake)
    assert store.load_latest_risk_snapshot(lake) is not None

    monkeypatch.setenv("CFD_ACCOUNT_MODE", "live")
    assert store.load_latest_risk_snapshot(lake) is None


def test_the_lake_itself_is_shared(lake, monkeypatch):
    """A deliberate departure from the spec: raw prices are the same prices
    whichever account you trade them in, and two copies could drift."""
    monkeypatch.setenv("CFD_ACCOUNT_MODE", "virtual")
    virtual = Path(lake.paths.raw_market_dir)

    monkeypatch.setenv("CFD_ACCOUNT_MODE", "live")
    assert Path(lake.paths.raw_market_dir) == virtual


# --------------------------------------------------------------------------- #
# The runner writes where it is told
# --------------------------------------------------------------------------- #
@pytest.fixture
def recorded_lake(tmp_path):
    from tests.test_analytics import TICKERS, build_lake
    from cfdinvest.backtest.runner import BacktestConfig
    from cfdinvest.repository import DataRepository
    from cfdinvest.schedule.runner import RunConfig
    from cfdinvest.schedule.runner import run_pipeline as run_scheduled

    config = build_lake(tmp_path / "data", bars=260)

    def run(**changes):
        summary = run_scheduled(
            list(TICKERS),
            RunConfig(
                backtest=BacktestConfig(interval="1d"),
                monitoring=False,
                **changes,
            ),
            repository=DataRepository(config),
            now=pd.Timestamp("2026-08-25 07:00", tz="UTC"),
        )
        assert summary.status == "ok", summary.error
        return summary

    return config, run


def test_a_run_lands_in_its_account_tree(recorded_lake):
    from cfdinvest.persist import readers

    config, run = recorded_lake
    summary = run(account="live")

    root = Path(config.paths.data_root)
    assert (root / "live" / "runs" / summary.run_id).is_dir()
    assert not (root / "runs" / summary.run_id).exists()
    assert readers.list_runs(config, "live") == [summary.run_id]
    assert readers.list_runs(config, "virtual") == []


def test_two_accounts_keep_separate_run_histories(recorded_lake):
    from cfdinvest.persist import readers

    config, run = recorded_lake
    virtual = run(account="virtual")
    live = run(account="live")

    assert readers.list_runs(config, "virtual") == [virtual.run_id]
    assert readers.list_runs(config, "live") == [live.run_id]


def test_the_scheduler_state_follows_the_run(recorded_lake):
    """Every part of one run must be filed in one tree."""
    from cfdinvest.persist import scheduler_state

    config, run = recorded_lake
    summary = run(account="live")

    assert scheduler_state.load_scheduler_state(config, "live")["last_run_id"] == (
        summary.run_id
    )
    assert scheduler_state.load_scheduler_state(config, "virtual") == {} or (
        scheduler_state.load_scheduler_state(config, "virtual").get("last_run_id")
        is None
    )


def test_analytics_score_the_account_they_are_asked_about(recorded_lake):
    """Scoring must follow the account, or it scores the wrong book."""
    from cfdinvest.analytics import score_run

    config, run = recorded_lake
    run(account="virtual")

    scored = score_run(config, account="virtual")
    assert scored.error is None
    assert scored.metrics

    # Nothing was recorded for live, and that must read as nothing rather
    # than as the virtual account's numbers.
    assert score_run(config, account="live").error == "no run has been recorded yet"


# --------------------------------------------------------------------------- #
# The dashboard reads the account it is showing
# --------------------------------------------------------------------------- #
def test_the_run_readers_follow_the_account(recorded_lake, monkeypatch):
    """The selector is decorative unless the readers use it.

    Every run reader goes through sources.runs_root, so this is the one place
    the account has to arrive -- and the one place it can be forgotten.
    """
    from cfdinvest.ui import sources

    config, run = recorded_lake
    summary = run(account="live")

    monkeypatch.delenv("CFD_ACCOUNT_MODE", raising=False)
    assert sources.list_runs(config, account="live").value
    assert not sources.list_runs(config, account="virtual").value
    assert not sources.list_runs(config).value

    live = sources.load_last_pipeline_summary(config, account="live")
    assert live.ok and live.value["run_id"] == summary.run_id


def test_the_dashboard_reads_the_shared_tree_when_separation_is_off(
    recorded_lake, monkeypatch
):
    from cfdinvest.ui import sources

    config, run = recorded_lake
    monkeypatch.delenv("CFD_ACCOUNT_MODE", raising=False)
    summary = run()

    assert sources.load_last_pipeline_summary(config).value["run_id"] == (
        summary.run_id
    )


def test_an_unknown_account_does_not_take_the_dashboard_down(recorded_lake):
    """The page that would tell you the account is wrong still has to render."""
    from cfdinvest.ui import sources

    config, _ = recorded_lake
    assert sources.runs_root(config, "runs", "prod") == (
        Path(config.paths.data_root) / "runs"
    )


def test_the_account_is_part_of_the_read_cache_key():
    """Keyed only on the refresh stamp, a switch hands back the other
    account's numbers under the new label."""
    import inspect

    from cfdinvest.ui import components

    source = inspect.getsource(components.read_everything)
    assert "def _read(_stamp: str, account: str | None)" in source
    assert "_read(stamp, account)" in source


# --------------------------------------------------------------------------- #
# Email says which account it is about
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("mode", ACCOUNT_MODES)
def test_the_subject_carries_the_account(mode):
    """Both deployments send to the same inbox."""
    from cfdinvest.schedule.notify import SmtpConfig, build_trade_alert

    config = SmtpConfig.from_env({"CFD_ACCOUNT_MODE": mode})
    message = build_trade_alert({"asset": "SPX500", "action": "open_long"}, config)

    assert message["Subject"].endswith(f"({mode})")


def test_the_subject_says_nothing_when_separation_is_off():
    from cfdinvest.schedule.notify import SmtpConfig, build_trade_alert

    message = build_trade_alert(
        {"asset": "SPX500", "action": "open_long"}, SmtpConfig.from_env({})
    )
    assert "(" not in message["Subject"]


def test_a_bad_mode_does_not_stop_the_mail():
    """An alert about the broken configuration is exactly the alert you want."""
    from cfdinvest.schedule.notify import SmtpConfig

    assert SmtpConfig.from_env({"CFD_ACCOUNT_MODE": "prod"}).account is None


def test_the_monitoring_alert_carries_the_account_too():
    from cfdinvest.monitoring.aggregate import MonitoringSnapshot
    from cfdinvest.schedule.notify import SmtpConfig, build_monitoring_alert

    message = build_monitoring_alert(
        MonitoringSnapshot(
            timestamp=pd.Timestamp("2026-08-25", tz="UTC"),
            overall_status="red",
            headline="0.4 GB free",
        ),
        SmtpConfig.from_env({"CFD_ACCOUNT_MODE": "live"}),
    )
    assert message["Subject"].endswith("(live)")
