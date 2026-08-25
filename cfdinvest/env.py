"""Environment: which account this is, and the secrets, read once.

Two jobs, both small and both easy to get quietly wrong.

**Which account.** ``virtual`` is a Trading 212 demo account; ``live`` is real
money. Everything downstream — runs, confirmations, analytics, risk,
monitoring — is kept in separate trees so a demo result can never be read as a
live one. The mode is read from the environment because it is a property of
the *deployment*, not of a run: the same code, the same config file and the
same command produce different trees on two machines, which is the point.

**Secrets.** Read from the environment or from a file systemd owns, never from
the repository. Nothing here logs a value, ``Environment`` redacts on repr,
and the dashboard shows whether a secret is present rather than what it is.

Reading a value never sets it. ``load_env`` returns a mapping and, by default,
will not overwrite a variable the process already has — systemd has usually
loaded the file already, and a second read silently disagreeing with the first
is the kind of thing that takes a day to find.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)

__all__ = [
    "ACCOUNT_MODES",
    "DEFAULT_ACCOUNT_MODE",
    "ACCOUNT_MODE_VARS",
    "SMTP_PASSWORD_VARS",
    "ENV_FILE",
    "Environment",
    "load_env",
    "parse_env_file",
    "account_mode",
    "resolve_account",
    "smtp_password",
    "redact",
    "read_environment",
]

#: The two accounts. ``virtual`` first, and the default, because the failure of
#: defaulting wrong is asymmetric: a live result filed under virtual is a
#: nuisance, and a virtual result read as live is a decision made on money that
#: was never at stake.
#: ``simulation`` is a tree, not a deployment mode: nothing runs "as
#: simulation", the simulate layer writes there. Kept out of ACCOUNT_MODES so
#: CFD_ACCOUNT_MODE=simulation is refused rather than quietly accepted.
ACCOUNT_MODES: tuple[str, ...] = ("virtual", "live")
DEFAULT_ACCOUNT_MODE = "virtual"

#: In order of preference. ``CFD_ACCOUNT_MODE`` matches every other variable
#: this project reads; bare ``ACCOUNT_MODE`` is accepted because it is what the
#: deployment notes say and an unread variable is worse than an untidy one.
ACCOUNT_MODE_VARS: tuple[str, ...] = ("CFD_ACCOUNT_MODE", "ACCOUNT_MODE")

#: Likewise for the mail password.
SMTP_PASSWORD_VARS: tuple[str, ...] = ("CFD_SMTP_PASSWORD", "SMTP_PASSWORD")

#: Where a deployed install keeps its environment file. Owned by root, read by
#: the service user, never in the repository.
ENV_FILE = "/etc/cfdinvest/env"

#: Anything whose name looks like this is never printed.
_SECRET_NAME = re.compile(
    r"(PASSWORD|SECRET|TOKEN|API_KEY|APIKEY|CREDENTIAL|AUTH)", re.IGNORECASE
)

_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


def redact(name: str, value: Any) -> str:
    """What a value may be shown as.

    Secrets become ``"set"`` or ``"not set"`` — never a prefix, never a length.
    A redaction that leaks four characters and a length is a redaction that
    makes a short password guessable.
    """
    if value in (None, ""):
        return "not set"
    return "set" if _SECRET_NAME.search(name) else str(value)


def parse_env_file(text: str) -> dict[str, str]:
    """Parse ``KEY=value`` lines. Pure.

    The systemd ``EnvironmentFile`` dialect, which is not shell: comments and
    blanks are skipped, ``export`` is tolerated, and one layer of matching
    quotes is stripped. Nothing is expanded and nothing is executed — a value
    containing ``$(...)`` is that literal string, because this file holds
    passwords and a parser that could run them would be a way to run them.
    """
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _LINE.match(stripped)
        if match is None:
            continue
        name, raw = match.group(1), match.group(2).strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
            raw = raw[1:-1]
        values[name] = raw
    return values


def load_env(
    path: str | Path | None = None,
    *,
    apply: bool = False,
    override: bool = False,
    env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Read the environment file, merged over the process environment.

    Returns the merged mapping and, by default, changes nothing: the caller
    gets the values to pass around explicitly. ``apply=True`` writes them into
    :data:`os.environ` for code that reads it directly.

    ``override`` is off because systemd has usually loaded this file already.
    A second read that quietly disagreed with the first would make the same
    deployment behave differently depending on which code path asked.

    A missing file is normal — a developer machine has no ``/etc/cfdinvest`` —
    and an unreadable one is logged, not raised. Refusing to start because a
    secrets file is absent would take the whole system down over a mail
    password.
    """
    base = dict(os.environ if env is None else env)
    target = Path(path) if path is not None else Path(ENV_FILE)

    from_file: dict[str, str] = {}
    try:
        if target.is_file():
            from_file = parse_env_file(target.read_text(encoding="utf-8"))
            logger.debug("Read %d variable(s) from %s", len(from_file), target)
    except OSError as error:
        logger.warning("Could not read the environment file at %s: %s", target, error)

    merged = dict(base)
    for name, value in from_file.items():
        if override or name not in merged:
            merged[name] = value

    if apply:
        for name, value in merged.items():
            if override or name not in os.environ:
                os.environ[name] = value

    return merged


def _first(names: tuple[str, ...], env: Mapping[str, str] | None) -> str | None:
    source = os.environ if env is None else env
    for name in names:
        value = source.get(name)
        if value not in (None, ""):
            return str(value)
    return None


def account_mode(env: Mapping[str, str] | None = None) -> str:
    """``"virtual"`` or ``"live"``. Defaults to virtual.

    An unrecognised value is refused rather than guessed. ``ACCOUNT_MODE=prod``
    silently becoming live is how a demo deployment starts writing into the
    live tree.
    """
    raw = _first(ACCOUNT_MODE_VARS, env)
    if raw is None:
        return DEFAULT_ACCOUNT_MODE

    mode = raw.strip().lower()
    if mode not in ACCOUNT_MODES:
        raise ValueError(
            f"account mode must be one of {', '.join(ACCOUNT_MODES)}, got {raw!r}"
        )
    return mode


def account_configured(env: Mapping[str, str] | None = None) -> bool:
    """Whether an account mode was set at all, as against defaulted."""
    return _first(ACCOUNT_MODE_VARS, env) is not None


def resolve_account(
    account: str | None = None, env: Mapping[str, str] | None = None
) -> str | None:
    """Which account tree to read and write, or ``None`` for the shared root.

    The resolution order is: an explicit argument, then the environment, then
    ``None``.

    **``None`` when nothing is set, not "virtual".** Turning separation on by
    default would strand every artefact written before this existed one
    directory above where the readers now look — a lake that appears to have
    lost its history is a bad way to find out about a new feature. Setting
    ``CFD_ACCOUNT_MODE`` is how you opt in; see the deployment README for the
    one-line migration.
    """
    if account is not None:
        # An explicit account is checked against the *trees*, not against the
        # deployment modes. "simulation" is a real tree that nothing runs as:
        # CFD_ACCOUNT_MODE=simulation is still refused, and
        # account_root(config, "simulation") is still allowed. Early these
        # shared one list and the simulate layer could not write anywhere.
        from cfdinvest.persist.storage import ACCOUNTS

        if account not in ACCOUNTS:
            # Worded to match cfdinvest.persist.storage, which is the other
            # place that can raise this: one message for one mistake.
            raise ValueError(
                f"unknown account {account!r}; expected one of {ACCOUNTS}"
            )
        return account
    if not account_configured(env):
        return None
    return account_mode(env)


def smtp_password(env: Mapping[str, str] | None = None) -> str | None:
    """The mail password, from the environment. Never from the repository."""
    return _first(SMTP_PASSWORD_VARS, env)


@dataclass(frozen=True, slots=True)
class Environment:
    """What the deployment is configured as, safe to print.

    ``__repr__`` and :meth:`describe` redact every secret, because this object
    ends up in a log line and on a configuration page, and a secret that is
    safe in nine of its ten appearances is not safe.
    """

    account: str | None = None
    account_mode: str = DEFAULT_ACCOUNT_MODE
    account_configured: bool = False
    data_root: str | None = None
    smtp_password_set: bool = False
    env_file: str | None = None
    env_file_present: bool = False

    def __repr__(self) -> str:  # pragma: no cover - exercised via describe
        return (
            f"Environment(account={self.account!r}, "
            f"mode={self.account_mode!r}, "
            f"smtp_password={'set' if self.smtp_password_set else 'not set'})"
        )

    def describe(self) -> dict[str, str]:
        """A table for the Configuration page. Redacted by construction."""
        return {
            "Account mode": self.account_mode,
            "Account tree": self.account or "shared (separation not enabled)",
            "Set explicitly": "yes" if self.account_configured else "no (defaulted)",
            "Data root": self.data_root or "not set",
            "Environment file": self.env_file or "not used",
            "Environment file present": "yes" if self.env_file_present else "no",
            "SMTP password": "set" if self.smtp_password_set else "not set",
        }


def read_environment(
    path: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
    account: str | None = None,
) -> Environment:
    """Everything this module knows, in one value. Never raises on absence."""
    target = Path(path) if path is not None else Path(ENV_FILE)
    present = False
    try:
        present = target.is_file()
    except OSError:  # pragma: no cover - an unreadable parent directory
        present = False

    merged = load_env(target, env=env) if present else dict(
        os.environ if env is None else env
    )

    # A bad mode must not stop the dashboard from rendering the page that
    # would tell you the mode is bad. Both calls can raise, and early on only
    # one of them was guarded.
    try:
        mode = account_mode(merged)
    except ValueError as error:
        logger.warning("%s", error)
        mode = DEFAULT_ACCOUNT_MODE
    try:
        resolved = resolve_account(account, merged)
    except ValueError as error:
        logger.warning("%s", error)
        resolved = None

    return Environment(
        account=resolved,
        account_mode=mode,
        account_configured=account_configured(merged),
        data_root=merged.get("CFD_DATA_ROOT"),
        smtp_password_set=smtp_password(merged) is not None,
        env_file=str(target),
        env_file_present=present,
    )
