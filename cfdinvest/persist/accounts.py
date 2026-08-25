"""The account registry on disk: one document, rewritten in place.

The only store in this project that is **not** append-only, and the exception
is deliberate. Everything else here records observations — a reading, a
verdict, a fill — and those describe moments that do not come back. The
registry is *configuration*: it says what exists now, and a history of what it
used to say would be a second source of truth about which accounts are legal.

Written atomically, so a crash mid-write leaves the previous registry intact
rather than a truncated one. That matters more here than elsewhere: a corrupt
registry could make ``live`` unreachable, so reads fall back to the built-in
three rather than to nothing.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from cfdinvest.persist.storage import atomic_write_text, data_root

logger = logging.getLogger(__name__)

__all__ = [
    "REGISTRY_FILE",
    "SCHEMA_VERSION",
    "registry_path",
    "load_registry",
    "write_registry",
]

REGISTRY_FILE = "accounts.json"
SCHEMA_VERSION = 1


def registry_path(config=None) -> Path:
    """The registry document.

    At the **data root**, not under an account tree. An account registry
    filed inside one of the accounts it describes would be unreadable until
    you already knew which account to look in.
    """
    return data_root(config) / REGISTRY_FILE


def load_registry(config=None):
    """Read the registry, or the built-in three. Never raises.

    A missing file is the normal state. An unreadable one falls back to the
    built-ins with a warning rather than to nothing -- a corrupt registry must
    not be able to make ``live`` unreachable.
    """
    from cfdinvest.accounts.registry import (
        AccountProfile,
        AccountRegistry,
        default_registry,
    )

    path = registry_path(config)
    try:
        if not path.is_file():
            return default_registry()
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        logger.warning(
            "Account registry unreadable at %s (%s); falling back to the "
            "built-in accounts", path, error
        )
        return default_registry()

    if not isinstance(document, dict):
        logger.warning("Account registry at %s is not a document", path)
        return default_registry()

    registry = default_registry()
    for entry in document.get("accounts") or []:
        if not isinstance(entry, dict):
            continue
        try:
            registry = registry.register(AccountProfile.from_document(entry))
        except (TypeError, ValueError) as error:
            # One bad entry must not cost the others, and must certainly not
            # cost the built-ins.
            logger.warning(
                "Skipping account %r: %s", entry.get("name"), error
            )
    return AccountRegistry(profiles=registry.profiles, path=str(path))


def write_registry(registry, config=None) -> str | None:
    """Write the registry. Returns its path, or ``None`` if it could not.

    Never raises. A registry that failed to save is a change lost; taking the
    caller down as well would lose more.
    """
    document = {
        "schema_version": SCHEMA_VERSION,
        "accounts": [
            profile.to_document()
            for profile in sorted(registry, key=lambda p: p.name)
        ],
    }
    path = registry_path(config)
    try:
        atomic_write_text(json.dumps(document, indent=2), path)
        return str(path)
    except Exception as error:  # noqa: BLE001
        logger.warning("Could not write the account registry to %s: %s", path, error)
        return None
