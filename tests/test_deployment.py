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
    """Localhost, and the port the unit actually serves.

    The number is read from the unit rather than repeated here. A test that
    restates a value is a second copy of it, and the day the two drift the
    test defends the wrong one -- which is how the deploy's health check
    came to probe a port this project did not serve.
    """
    unit = text(DEPLOYMENT / "systemd" / "cfdinvest-dashboard.service")
    port = re.search(r"--server\.port\s+(\d+)", unit).group(1)
    assert f"reverse_proxy 127.0.0.1:{port}" in text(CADDYFILE)


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
#: The one unit permitted to run as root, and why.
#:
#: A deploy restarts services and may install packages; no unprivileged user
#: can do either. What makes it acceptable is that nothing on the network can
#: reach it. It is started by cfdinvest-deploy.path and by nothing else, it
#: takes no input, and it reads nothing from the request file but the fact
#: that it changed. The listener that *is* reachable runs as cfdinvest and
#: holds no privilege at all -- which is why it is a separate unit rather
#: than the single root listener the other two apps on this machine use.
PRIVILEGED_UNITS = {"cfdinvest-deploy.service"}


def test_the_units_exist():
    assert {path.name for path in UNITS} == {
        "cfdinvest-dashboard.service",
        "cfdinvest-pipeline.service",
        "cfdinvest-monitor.service",
        "cfdinvest.service",
        "cfdinvest-webhook.service",
        "cfdinvest-deploy.service",
    }


def test_only_the_deploy_unit_is_privileged_and_it_is_unreachable():
    """Root is allowed in exactly one place, and only where nothing can call it.

    The compensating condition matters more than the exemption: a root unit
    that took a network request would be a shell on the box for anyone who
    found the path.
    """
    for unit in UNITS:
        if unit.name not in PRIVILEGED_UNITS:
            continue
        body = text(unit)
        assert "User=root" in body
        # Started by the path watcher alone.
        assert "ExecStart=" in body
        assert "ListenStream" not in body and "Socket" not in body, (
            f"{unit.name} runs as root and must not listen on anything"
        )

    listener = next(u for u in UNITS if u.name == "cfdinvest-webhook.service")
    body = text(listener)
    assert "User=cfdinvest" in body, "the network-facing unit must not be root"
    assert "NoNewPrivileges=true" in body


def test_only_one_unit_may_schedule_the_pipeline():
    """Two schedulers on one lake is the failure this guards.

    cfdinvest.service supersedes the PIPELINE timer: both would ingest, on
    two different schedules, against one lake. Declaring that in a README is
    not enough -- systemd has to refuse.
    """
    body = text(DEPLOYMENT / "systemd" / "cfdinvest.service")
    conflicts = re.search(r"^Conflicts=(.*)$", body, re.M)

    assert conflicts, "cfdinvest.service must declare what it supersedes"
    assert "cfdinvest-pipeline.service" in conflicts.group(1)


def test_the_daemon_does_not_conflict_with_its_own_monitor():
    """**This asserted the opposite, and the opposite cost two stalls.**

    `cfdinvest-monitor.service` runs `monitor --alert`: it reads, it
    alerts, it ingests nothing, and it finishes in about a second. It was
    lumped into the same `Conflicts=` line as the pipeline, so enabling its
    timer would have stopped the dealer every thirty minutes -- which is
    why the timer was left disabled, and why the only check that detects
    this daemon hanging was left running INSIDE this daemon, on a daily
    snapshot at 03:05 when `market_open` is always False.

    It failed exactly as that predicts. 2026-09-09: a 45-minute stall,
    unreported. 2026-09-11: NINETEEN HOURS, through most of a trading
    session, unreported. `check_dealing_liveness` existed for the first and
    could not fire during the second.

    A monitor inside the thing it monitors is not a monitor.
    """
    body = text(DEPLOYMENT / "systemd" / "cfdinvest.service")
    conflicts = re.search(r"^Conflicts=(.*)$", body, re.M)

    assert conflicts
    assert "cfdinvest-monitor.service" not in conflicts.group(1), (
        "the daemon conflicts with its own out-of-process monitor, so the "
        "monitor timer cannot be enabled without stopping the dealer"
    )


def test_the_deploy_enables_the_out_of_process_monitor():
    """A unit that exists but was never enabled does nothing.

    deploy.sh's own comment says exactly that, and the monitor timer was
    the counter-example sitting beside it: installed on the box, never
    enabled, never run. Asserted against the TIMERS list the deploy
    actually iterates, so documenting it elsewhere cannot satisfy this.
    """
    body = text(DEPLOYMENT / "deploy.sh")
    timers = re.search(r"^TIMERS=\((.*)\)$", body, re.M)

    assert timers, "deploy.sh must declare which timers it enables"
    assert "cfdinvest-monitor.timer" in timers.group(1), (
        "the deploy does not enable the monitor timer, so a fresh box gets "
        "no out-of-process health check at all"
    )
    assert "cfdinvest-pipeline.timer" not in timers.group(1), (
        "the pipeline timer would ingest a second time against one lake"
    )


@pytest.mark.parametrize("unit", UNITS, ids=lambda p: p.name)
def test_every_unit_reads_its_secrets_from_a_file(unit):
    """Never from the unit, and never from a command line -- a password in
    ExecStart is a password in `ps`."""
    body = text(unit)
    assert "EnvironmentFile=/etc/cfdinvest/env" in body
    assert not re.search(r"^Environment=.*(PASSWORD|SECRET|KEY)=", body, re.M)


@pytest.mark.parametrize("unit", UNITS, ids=lambda p: p.name)
def test_every_unit_runs_as_the_service_user(unit):
    if unit.name in PRIVILEGED_UNITS:
        pytest.skip(f"{unit.name} is the deploy unit; see PRIVILEGED_UNITS")
    body = text(unit)
    assert "User=cfdinvest" in body
    assert "User=root" not in body


@pytest.mark.parametrize("unit", UNITS, ids=lambda p: p.name)
def test_every_unit_is_confined(unit):
    if unit.name in PRIVILEGED_UNITS:
        # Confinement and "restart other services" are contradictory. The
        # protection here is unreachability, asserted separately.
        pytest.skip(f"{unit.name} is the deploy unit; see PRIVILEGED_UNITS")
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
        # `bash` and `python3.11` are the OS interpreters the deploy pair
        # runs: the listener must not depend on the project venv it exists
        # to reinstall, and deploy.sh runs before any reinstall happens.
        from_dependencies = {
            "streamlit", "python", "python3", "python3.11", "bash",
        }

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

    Without it Streamlit binds every interface and answers on its port
    directly, going round Caddy and therefore round the basic auth. The
    proxy is only a boundary because the app is unreachable any other way.

    The other Streamlit app on this box does bind 0.0.0.0, which is what
    makes this more than theoretical.
    """
    body = text(DEPLOYMENT / "systemd" / "cfdinvest-dashboard.service")
    assert "--server.address 127.0.0.1" in body
    # In the ExecStart, not in the prose explaining why it must not appear.
    execstart = "\n".join(
        line for line in body.splitlines() if not line.lstrip().startswith("#")
    )
    assert "0.0.0.0" not in execstart


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
    """A machine asleep at the trigger should still run, not skip in silence.

    Only calendar timers can miss a window. A monotonic watchdog -- one that
    fires relative to boot or to the unit's own state -- has nothing to catch
    up on, and `Persistent=` is ignored without `OnCalendar=`. Asserting it
    everywhere would mean adding a directive systemd discards, to satisfy a
    test rather than a property.
    """
    body = text(timer)
    if re.search(r"^OnCalendar=", body, re.M):
        assert "Persistent=true" in body
        return

    # A monotonic timer must instead start on its own after a reboot,
    # rather than waiting for the unit it watches to become active first.
    assert re.search(r"^OnBootSec=", body, re.M), (
        f"{timer.name} has neither a calendar nor a boot trigger, so a "
        "reboot would leave it waiting indefinitely"
    )


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
    # Compiled bytecode is not committed and is not text; importing the
    # webhook listener from a test creates some, which the scan then tried
    # to decode as UTF-8.
    sorted(
        p for p in DEPLOYMENT.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    ),
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


# --------------------------------------------------------------------------- #
# The deploy listener
# --------------------------------------------------------------------------- #
def test_the_listener_rejects_an_unsigned_or_wrongly_signed_body():
    """M88: signature verification accepts anything.

    The endpoint is reachable by anyone who finds the path, and a request
    that passes triggers a shell script as root. Verification is the only
    thing between those two facts.
    """
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path("deployment").resolve()))
    import webhook_listener as listener

    secret = "correct horse battery staple"
    body = b'{"ref": "refs/heads/main"}'
    import hashlib
    import hmac

    good = "sha256=" + hmac.new(
        secret.encode(), body, hashlib.sha256
    ).hexdigest()

    assert listener.verify_signature(secret, body, good)
    assert not listener.verify_signature(secret, body, None)
    assert not listener.verify_signature(secret, body, "")
    assert not listener.verify_signature(secret, body, "sha256=deadbeef")
    assert not listener.verify_signature("a different secret", body, good)
    # A body altered after signing must fail even with a valid-looking header.
    assert not listener.verify_signature(secret, body + b" ", good)


def test_only_a_push_to_main_deploys():
    """M89: any event deploys.

    A tag, a fork's pull request or a push to a branch must not put code on
    the server.
    """
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path("deployment").resolve()))
    import webhook_listener as listener

    assert listener.should_deploy("push", {"ref": "refs/heads/main"})
    assert not listener.should_deploy("push", {"ref": "refs/heads/feature"})
    assert not listener.should_deploy("push", {"ref": "refs/tags/v1"})
    assert not listener.should_deploy("pull_request", {"ref": "refs/heads/main"})
    assert not listener.should_deploy("ping", {"ref": "refs/heads/main"})
    assert not listener.should_deploy("push", {})


def test_the_deploy_script_restarts_every_resident_service():
    """M90: the deploy pulls and does not restart.

    This is the whole reason the script exists. Copying files while a
    daemon holds its imported modules leaves the change on disk and out of
    production, which is what happened on 2026-08-25.
    """
    from pathlib import Path

    script = Path("deployment/deploy.sh").read_text(encoding="utf-8")

    assert "git fetch" in script and "git reset --hard" in script
    for service in ("cfdinvest", "cfdinvest-dashboard"):
        assert f'systemctl restart "${{service}}"' in script or service in script, service
    assert "SERVICES=(cfdinvest cfdinvest-dashboard)" in script, (
        "both resident services must be restarted, not just one"
    )
    # And it must check what it produced rather than assume.
    assert "is-active" in script and "http_code" in script


def test_the_listener_carries_no_secret_and_the_unit_names_its_own():
    """No credential in the repository, and not the other apps' secret."""
    from pathlib import Path

    listener = Path("deployment/webhook_listener.py").read_text(encoding="utf-8")
    unit = Path("deployment/systemd/cfdinvest-webhook.service").read_text(
        encoding="utf-8"
    )

    assert "CFD_GITHUB_WEBHOOK_SECRET" in listener
    assert "os.environ" in listener
    # The other listeners on the box use GITHUB_WEBHOOK_SECRET; sharing it
    # would mean one compromise reached all three.
    assert '"GITHUB_WEBHOOK_SECRET"' not in listener
    assert "EnvironmentFile=/etc/cfdinvest/env" in unit
    # KillMode=process is what the other two listeners need because they
    # spawn their own deploy scripts. This one does not: the deploy runs in
    # its own unit, so restarting the listener cannot truncate it.
    assert "User=cfdinvest" in unit



def test_the_deploy_never_enables_a_superseded_timer():
    """M137: a second scheduler for a job that already has an owner.

    cfdinvest.service is a resident daemon owning every job there is --
    quotes, the advisor, the paper book, ingestion, the daily run, both
    snapshots and pruning. The pipeline and monitor timers predate it and
    are superseded; their unit files survive as history and the daemon
    declares Conflicts= against their services.

    A deploy that enabled them would run ingestion twice on two schedules
    against one lake. This nearly happened: a prune timer was added here on
    the belief that nothing pruned, when the daemon had owned pruning all
    along -- the artefacts it would have removed were already gone, which is
    why the reading was zero. Enabling superseded units is easy to do while
    believing you are fixing something.

    Asserted against the Conflicts= line rather than a hardcoded list, so a
    unit retired later is covered without anyone remembering to come back.
    """
    deploy = text(DEPLOYMENT / "deploy.sh")

    declared = set()
    for line in deploy.splitlines():
        if line.startswith("TIMERS="):
            declared = set(line.split("(", 1)[1].split(")", 1)[0].split())
            break
    assert declared, "deploy.sh declares no timers"

    # Every timer named must have a unit file behind it, or the deploy fails
    # on the live box rather than here.
    on_disk = {path.name for path in (DEPLOYMENT / "systemd").glob("*.timer")}
    assert declared <= on_disk, (
        f"deploy enables {sorted(declared - on_disk)} with no unit file"
    )

    body = text(DEPLOYMENT / "systemd" / "cfdinvest.service")
    conflicts = re.search(r"^Conflicts=(.*)$", body, re.M)
    assert conflicts

    superseded = {
        name.strip().replace(".service", ".timer")
        for name in conflicts.group(1).split()
        if name.strip().endswith(".service")
    }
    clash = declared & superseded
    assert not clash, f"deploy enables superseded timers: {sorted(clash)}"

    # And the daemon's own timer is the one that must be on.
    assert "cfdinvest.timer" in declared


def test_the_daemon_owns_every_scheduled_job():
    """The single owner, asserted rather than described.

    Each job the deploy does not schedule has to be scheduled by something,
    and the daemon is that something. If a job were dropped from its list
    while its timer stayed retired, nothing would run it and the symptom
    would be silence -- the hardest kind of failure to notice.
    """
    from cfdinvest.schedule import daemon

    assert "prune-runs" in daemon.JOBS, "nothing would bound the artefact store"
    assert "trade-capital" in daemon.JOBS, "nothing would reach the account"
    assert "paper-book" not in daemon.JOBS, (
        "the paper book was removed on 2026-08-30 -- it ran a different rule "
        "set from the engine, so its record measured a strategy nobody chose"
    )
    assert "ingest-daily" in daemon.JOBS, "the daily lake would go stale"
    # The only job that produces a run. ``run-daily`` was retired on
    # 2026-08-27: a US-only universe is closed at 03:05 UTC, so it skipped
    # every day and produced nothing, while this one had already been doing
    # the same work every fifteen minutes.
    assert "run-intraday" in daemon.JOBS, "nothing would produce signals"
    assert "run-daily" not in daemon.JOBS, "a job that can never do anything"

    # Pruning runs only after the snapshots that describe a run, so a run is
    # never pruned before everything about it has been written.
    due = daemon.jobs_due("2026-08-26T03:10:00Z", {})
    if "prune-runs" in due:
        assert due.index("prune-runs") > due.index("monitoring-snapshot")


def test_the_disk_is_checked_on_every_deploy():
    """The evidence that pruning is keeping up, rather than the hope.

    A prune timer that silently stopped would look exactly like one that
    was working until the disk filled. The deploy reads the free figure and
    fails on it, so the next push is the thing that notices.
    """
    deploy = (DEPLOYMENT / "deploy.sh").read_text(encoding="utf-8")
    assert "df " in deploy
    assert "/var/lib/cfdinvest" in deploy

    # And it must actually fail the deploy, not merely mention the number.
    # Asserted on the guard itself rather than on nearby text: the first
    # version of this test looked for "fail" within a few hundred
    # characters and passed happily against a version that only logged.
    lines = deploy.splitlines()
    guard = next(
        (i for i, line in enumerate(lines) if "free_pct" in line and ">=" in line),
        None,
    )
    assert guard is not None, "no threshold guard on the disk figure"

    body = "\n".join(lines[guard : guard + 4])
    assert "fail" in body, "the disk guard reports but does not fail the deploy"
    assert "ok=1" in body, "the disk guard does not set the failure code"


def test_the_dashboard_port_has_one_owner():
    """M140: three files naming the port, and two of them disagreeing.

    They did. The unit and the Caddy upstream said 8501 while the deploy's
    health check probed 8502, so the check was reporting on a port this
    project did not serve -- it returned HTTP 200 while the dashboard was
    dead, because something else was answering.

    The number matters beyond tidiness. Another Streamlit app on this box
    holds 8501 and binds 0.0.0.0, so Streamlit's default is not ours to
    take: after a VM rescale it claimed the port first and the dashboard
    entered a restart loop on "Port 8501 is not available". Pointing Caddy
    at the default would be worse than a collision -- it would serve the
    other app's pages through this site's basic auth.
    """
    unit = text(DEPLOYMENT / "systemd" / "cfdinvest-dashboard.service")
    caddy = text(CADDYFILE)
    deploy = text(DEPLOYMENT / "deploy.sh")

    in_unit = re.search(r"--server\.port\s+(\d+)", unit)
    in_caddy = re.search(r"reverse_proxy\s+127\.0\.0\.1:(\d+)", caddy)
    in_deploy = re.search(r"127\.0\.0\.1:(\d+)/", deploy)

    assert in_unit and in_caddy and in_deploy, "the port must be findable in all three"

    ports = {
        "unit": in_unit.group(1),
        "caddy": in_caddy.group(1),
        "deploy health check": in_deploy.group(1),
    }
    assert len(set(ports.values())) == 1, f"the port disagrees: {ports}"

    # And not Streamlit's default, which belongs to the other tenant.
    assert set(ports.values()) != {"8501"}, (
        "8501 is held by another app on this box, bound to every interface"
    )


def test_the_readme_matches_the_shipped_configuration():
    """M198: documentation drifting from the thing it describes.

    Every number in the README's configuration table is one somebody will
    quote without checking. Three limits in this project have already been
    found living only in a dataclass default while a file implied
    otherwise, and a README is the easiest place of all for that to happen
    unnoticed -- nothing executes it.

    Asserted against the config rather than restated, so the table cannot
    drift silently.
    """
    from cfdinvest.config import load_config

    readme = Path("README.md").read_text(encoding="utf-8")
    config = load_config()
    strategy = config.strategy

    assert f"{len(config.market.tickers)} US shares" in readme
    assert f"{strategy.entry_confidence} / {strategy.exit_confidence}" in readme
    assert f"{strategy.time_stop_days} days" in readme
    assert f"{strategy.max_leverage}\u00d7 equity" in readme
    assert f"{strategy.slippage_bps:.0f} bp" in readme

    # And the dashboard page count, in whichever way English spells it.
    #
    # This used to hardcode "Three" with the numeral as a fallback, so adding
    # a fourth page failed on the spelling rather than on the count -- the
    # test pinned how the sentence reads instead of whether it is true.
    pages = len(list(Path("cfdinvest/ui/views").glob("*.py")))
    words = {1: "One", 2: "Two", 3: "Three", 4: "Four", 5: "Five", 6: "Six"}
    spellings = {f"{pages} pages", f"{words.get(pages, pages)} pages"}
    assert any(spelling in readme for spelling in spellings), (
        f"the README does not say there are {pages} pages"
    )


def test_the_readme_never_quotes_a_return_without_its_benchmark():
    """A return on a hindsight-selected universe, quoted alone, is the single
    most misleading thing this project could publish.

    Buy-and-hold of the same twenty returns about 25% a year over the same
    window. The README must carry that wherever it carries a headline.
    """
    readme = Path("README.md").read_text(encoding="utf-8")

    assert "hindsight" in readme.lower(), "the selection bias is not disclosed"
    assert "benchmark" in readme.lower()
    assert "buy-and-hold" in readme.lower() or "buy and hold" in readme.lower()
    # And the limits section must exist rather than the caveats being scattered.
    assert "## Known limits" in readme


def test_the_readme_states_exactly_where_it_may_trade():
    """The most important sentence in the document, and it changed.

    It used to assert the README said the system had never placed a trade.
    That stopped being true on 2026-08-27, when the daemon was permitted to
    deal on a demo account, and a test pinning a false sentence is worse
    than no test -- it makes the falsehood load-bearing.

    What must hold now is narrower and harder: a first-time reader must be
    able to tell exactly where it can trade and what stops it trading
    anywhere else.
    """
    readme = Path("README.md").read_text(encoding="utf-8")
    lower = readme.lower()

    assert "never placed a trade" not in lower, (
        "the README claims the system has never traded; it deals on the "
        "demo account and must say so"
    )
    assert "demo" in lower, "the README does not say which account it deals on"
    assert "read-only" in lower, "the read-only construction is not stated"
    # The second switch is the thing between a configuration mistake and a
    # funded account, so the document has to name it.
    assert "cfd_capital_allow_live" in lower, (
        "the README does not name the switch a live account needs"
    )


def test_no_message_claims_the_system_cannot_trade():
    """M-live: the footer said the opposite of the truth for four days.

    "This system simulates trades and places no orders with any broker" was
    true when written and stopped being true on 2026-08-27, when the daemon
    was permitted to deal on the Capital.com demo account. It survived
    unchanged in four separate files -- the bias digest, the insights digest,
    the journal digest and the operational alerts -- because it was four
    copies of one sentence and nothing tied them together.

    A stale reassurance is worse than none: somebody reading an alert at
    three in the morning is entitled to know whether the thing warning them
    can place an order.
    """
    from pathlib import Path

    stale = "places no orders with any broker"
    # disclosure.py quotes the old sentence while explaining why it was
    # wrong. Striking a retraction rather than deleting it is the house rule;
    # the point is that nothing still *asserts* it.
    offenders = [
        str(path)
        for path in Path("cfdinvest").rglob("*.py")
        if path.name != "disclosure.py"
        and stale in path.read_text(encoding="utf-8", errors="ignore")
    ]
    assert not offenders, (
        f"these still claim the system cannot trade: {offenders}"
    )


def test_the_dealing_notice_has_one_owner():
    """Four copies of a claim is four chances for it to rot, and it did."""
    from pathlib import Path

    from cfdinvest.disclosure import DEALING_NOTICE, dealing_notice

    assert "demo" in DEALING_NOTICE.lower()
    assert "live" in DEALING_NOTICE.lower(), (
        "the notice does not say what would have to change for it to be live"
    )

    # Absence named: not having checked is not the same as having checked
    # and found it safe.
    assert dealing_notice(None) == DEALING_NOTICE
    assert "WARNING" in dealing_notice(True)
    assert dealing_notice(False) == DEALING_NOTICE

    # And it is imported, not restated.
    copies = [
        str(path)
        for path in Path("cfdinvest").rglob("*.py")
        if path.name != "disclosure.py"
        and DEALING_NOTICE[:40] in path.read_text(encoding="utf-8", errors="ignore")
    ]
    assert not copies, f"the notice is restated rather than imported in {copies}"
