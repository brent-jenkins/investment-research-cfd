"""What accounts exist, what each is for, and what governs it.

::

    from cfdinvest.accounts import load_registry, AccountProfile

    registry = load_registry(config)
    registry.get("live").risk_limits()

Also a page in the dashboard: **Accounts**.

The registry names accounts and maps each to a directory under the same data
root, through the same ``account_root``, with the isolation the virtual/live
separation already has. It does not invent a second storage layout, and the
built-in three cannot be removed or repointed.

Profiles hold *overrides* on the existing config objects rather than defining
a fourth place a limit can live.
"""

from __future__ import annotations

from cfdinvest.accounts.registry import (
    ACCOUNT_TYPES,
    BUILT_IN,
    AccountProfile,
    AccountRegistry,
    default_registry,
    known_accounts,
)
from cfdinvest.accounts.router import (
    RoutingDecision,
    RoutingRule,
    explain_routing,
    route_trade,
)
from cfdinvest.persist.accounts import load_registry, registry_path, write_registry

__all__ = [
    "ACCOUNT_TYPES",
    "BUILT_IN",
    "AccountProfile",
    "AccountRegistry",
    "default_registry",
    "known_accounts",
    "load_registry",
    "write_registry",
    "registry_path",
    "RoutingDecision",
    "RoutingRule",
    "route_trade",
    "explain_routing",
]
