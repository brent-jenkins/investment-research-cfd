"""Who is allowed to open the dashboard, and how they change their password.

This replaces Caddy's ``basic_auth`` for the cfdinvest site. It is worth
being clear-eyed about that trade, because it is not a straight upgrade:

**A login form is a weaker boundary than basic auth.** Basic auth is checked
by the reverse proxy, so an unauthenticated request never reaches the
application at all. This gate is checked by the application, so Streamlit's
own endpoints -- health, static assets, the websocket -- are served to anyone
who asks, and only the *rendered page* is protected. What it buys is a real
session, a sign-out, and a password you can change without editing a config
file and reloading a proxy.

That is a reasonable trade for a personal dashboard sitting behind
Cloudflare, and a bad one for anything holding other people's data. It holds
no personal data and can place no trade.

How the password is stored
--------------------------
``scrypt`` from the standard library, with a per-credential random salt and
deliberately expensive parameters. No new dependency, and nothing here is a
home-made hash.

The store lives at ``<data_root>/auth/credentials.json``, mode ``0600``. It
is **the one file the dashboard writes**, and the UI's README says so: the
read-only rule for this layer is about the pipeline's artefacts, and a
credential you cannot change without a deploy is a credential nobody changes.

Bootstrapping
-------------
The account is seeded from ``CFD_DASHBOARD_PASSWORD`` in the environment --
in deployment, ``/etc/cfdinvest/env``. There is deliberately no first-run
"choose a password" screen: an unclaimed dashboard on a public address
belongs to whoever finds it first.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)

__all__ = [
    "AUTH_DIR",
    "CREDENTIALS_FILE",
    "MIN_PASSWORD_LENGTH",
    "SESSION_KEY",
    "credentials_path",
    "hash_password",
    "verify_password",
    "load_credentials",
    "save_credentials",
    "check_login",
    "change_password",
    "bootstrap_from_environment",
    "is_configured",
    "COOKIE_NAME",
    "SESSION_DAYS",
    "issue_token",
    "verify_token",
    "session_secret",
]

AUTH_DIR = "auth"
CREDENTIALS_FILE = "credentials.json"
SCHEMA_VERSION = 1

#: Streamlit session-state key holding the signed-in username.
SESSION_KEY = "cfd_authenticated_user"

#: Short enough not to be a nuisance, long enough that the scrypt cost is
#: what an attacker has to pay rather than the search space being the weak
#: part. Length is the only rule: composition rules push people towards
#: predictable substitutions and a sticky note.
MIN_PASSWORD_LENGTH = 12

#: scrypt parameters. n is the work factor; 2**15 costs roughly 100ms and
#: 32MB per attempt here, which is unnoticeable on a login and ruinous on a
#: dictionary.
SCRYPT_N = 1 << 15
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 64
SALT_BYTES = 16


def _maxmem(n: int, r: int) -> int:
    """The memory ceiling to hand OpenSSL, derived from the parameters.

    scrypt needs ``128 * n * r`` bytes, and OpenSSL refuses with "memory
    limit exceeded" above its own 32MB default -- which n=2**15, r=8 sits
    exactly on. Derived rather than hard-coded so raising the work factor
    does not silently start failing every login.
    """
    return 128 * int(n) * int(r) * 2


def credentials_path(config=None) -> Path:
    """Where the credential store lives."""
    from cfdinvest.persist.storage import account_root

    return account_root(config, None) / AUTH_DIR / CREDENTIALS_FILE


def hash_password(password: str) -> dict[str, Any]:
    """A salted scrypt hash, with the parameters it was made under.

    The parameters are stored beside the hash rather than assumed, so raising
    the work factor later does not lock anyone out -- an old credential still
    verifies under the parameters it was created with.
    """
    import hashlib

    salt = secrets.token_bytes(SALT_BYTES)
    derived = hashlib.scrypt(
        str(password).encode("utf-8"),
        salt=salt,
        n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_DKLEN,
        maxmem=_maxmem(SCRYPT_N, SCRYPT_R),
    )
    return {
        "algorithm": "scrypt",
        "salt": salt.hex(),
        "hash": derived.hex(),
        "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P, "dklen": SCRYPT_DKLEN,
    }


def verify_password(password: str, record: Mapping[str, Any] | None) -> bool:
    """Whether the password matches. Never raises.

    Compared with :func:`hmac.compare_digest`, so the time taken does not
    depend on how much of the hash matched.
    """
    import hashlib

    if not record or record.get("algorithm") != "scrypt":
        return False
    try:
        derived = hashlib.scrypt(
            str(password).encode("utf-8"),
            salt=bytes.fromhex(str(record["salt"])),
            n=int(record["n"]), r=int(record["r"]), p=int(record["p"]),
            dklen=int(record["dklen"]),
            maxmem=_maxmem(int(record["n"]), int(record["r"])),
        )
    except Exception as error:  # noqa: BLE001 - a bad record is a failed login
        logger.warning("Could not verify a password: %s", error)
        return False
    return hmac.compare_digest(derived.hex(), str(record.get("hash", "")))


def load_credentials(config=None) -> dict[str, Any]:
    """The credential store, or an empty one. Never raises."""
    path = credentials_path(config)
    try:
        if not path.is_file():
            return {}
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        logger.warning("Credential store unreadable at %s: %s", path, error)
        return {}
    return document if isinstance(document, dict) else {}


def save_credentials(document: Mapping[str, Any], config=None) -> bool:
    """Write the store, readable only by its owner. Never raises."""
    from cfdinvest.persist.storage import atomic_write_text

    path = credentials_path(config)
    try:
        atomic_write_text(json.dumps(dict(document), indent=2), path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            # Not fatal, and worth saying: on a filesystem that will not take
            # the mode, the file is still only as private as its directory.
            logger.warning("Could not restrict permissions on %s", path)
        return True
    except Exception as error:  # noqa: BLE001
        logger.warning("Could not write the credential store: %s", error)
        return False


def is_configured(config=None) -> bool:
    """Whether any account exists yet."""
    return bool((load_credentials(config) or {}).get("users"))


def bootstrap_from_environment(config=None, *, username: str = "admin") -> bool:
    """Seed the account from ``CFD_DASHBOARD_PASSWORD`` if none exists.

    Returns whether an account was created. Does nothing when one already
    exists, so the environment variable cannot silently reset a password the
    owner has since changed -- otherwise every restart would undo it.
    """
    if is_configured(config):
        return False

    initial = os.environ.get("CFD_DASHBOARD_PASSWORD", "").strip()
    if not initial:
        logger.warning(
            "No dashboard account and CFD_DASHBOARD_PASSWORD is unset; "
            "nobody can sign in. Set it in /etc/cfdinvest/env."
        )
        return False

    document = {
        "schema_version": SCHEMA_VERSION,
        "users": {username: hash_password(initial)},
    }
    if save_credentials(document, config):
        logger.info("Created the dashboard account %r from the environment", username)
        return True
    return False


def check_login(username: str, password: str, config=None) -> bool:
    """Whether these credentials are good. Never raises.

    An unknown username is verified against a throwaway hash rather than
    returning early, so the time taken does not reveal which names exist.
    """
    users = (load_credentials(config) or {}).get("users") or {}
    record = users.get(str(username or "").strip())
    if record is None:
        verify_password(str(password), hash_password(secrets.token_hex(16)))
        return False
    return verify_password(str(password), record)


def change_password(
    username: str, current: str, new: str, confirm: str, config=None
) -> tuple[bool, str]:
    """Change a password. Returns ``(changed, message)``.

    The current password is required even though the session is already
    authenticated: a session left open on a shared screen should not be
    enough to lock the owner out of their own dashboard.
    """
    name = str(username or "").strip()
    if not check_login(name, current, config):
        return False, "That is not your current password."
    if new != confirm:
        return False, "The two new passwords do not match."
    if len(new) < MIN_PASSWORD_LENGTH:
        return False, (
            f"Use at least {MIN_PASSWORD_LENGTH} characters. Length is the "
            "only rule here, and it is the one that matters."
        )
    if new == current:
        return False, "That is the password you are already using."

    document = load_credentials(config) or {}
    users = dict(document.get("users") or {})
    users[name] = hash_password(new)
    document["users"] = users
    document.setdefault("schema_version", SCHEMA_VERSION)

    if not save_credentials(document, config):
        return False, "The new password could not be saved. It is unchanged."
    logger.info("Password changed for %r", name)
    return True, "Password changed."


# --------------------------------------------------------------------------- #
# Staying signed in across a refresh
# --------------------------------------------------------------------------- #
#: Streamlit's ``session_state`` lives for one websocket session, so a browser
#: refresh starts a new one and signs you out. That is not a policy, it is an
#: accident of where the state lives -- so the session is carried in a signed
#: cookie instead, and ``session_state`` becomes a per-run cache of it.
COOKIE_NAME = "cfd_session"

#: How long a signed-in browser stays signed in. Long enough not to be a
#: nuisance on a dashboard someone opens daily; short enough that a forgotten
#: session on an old laptop expires by itself.
SESSION_DAYS = 14


def session_secret(config=None) -> bytes:
    """The key the session cookie is signed with, created once and kept.

    Stored in the credential file so it survives a restart -- generating it
    per process would sign everybody out on every deploy, which is the exact
    failure this cookie exists to fix.
    """
    document = load_credentials(config) or {}
    existing = document.get("session_secret")
    if isinstance(existing, str) and len(existing) >= 32:
        return bytes.fromhex(existing)

    secret = secrets.token_bytes(32)
    document["session_secret"] = secret.hex()
    document.setdefault("schema_version", SCHEMA_VERSION)
    save_credentials(document, config)
    return secret


def issue_token(username: str, config=None, *, now: float | None = None) -> str:
    """A signed ``username|expiry|signature`` for the session cookie.

    The signature covers the expiry, so a browser cannot extend its own
    session by editing the cookie -- the whole value is opaque and checked,
    not trusted.
    """
    import hashlib
    import time

    name = str(username or "").strip()
    expires = int((now if now is not None else time.time()) + SESSION_DAYS * 86400)
    payload = f"{name}|{expires}"
    signature = hmac.new(
        session_secret(config), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"{payload}|{signature}"


def verify_token(token: str, config=None, *, now: float | None = None) -> str | None:
    """The username a token proves, or ``None``. Never raises.

    ``None`` covers every way a token can fail -- malformed, expired, wrong
    signature, or naming an account that no longer exists. A token for a
    deleted user must not still open the dashboard.
    """
    import hashlib
    import time

    try:
        name, expires, signature = str(token or "").rsplit("|", 2)
    except ValueError:
        return None

    expected = hmac.new(
        session_secret(config), f"{name}|{expires}".encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return None

    try:
        if int(expires) < int(now if now is not None else time.time()):
            return None
    except (TypeError, ValueError):
        return None

    users = (load_credentials(config) or {}).get("users") or {}
    return name if name in users else None
