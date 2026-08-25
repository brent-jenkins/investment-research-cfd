"""What accounts exist, what each is for, and what governs it.

Until now there were three trees — ``virtual``, ``live``, ``simulation`` — as
a fixed tuple validated everywhere. That is enough for one demo account and
one real one, and not enough once you want a second live account for a
different strategy, or one you only trade in a particular regime.

The registry names accounts and says what each is for. It does **not** invent
a second storage layout: a registered account maps to a directory under the
same root, through the same :func:`~cfdinvest.persist.storage.account_root`,
with the same isolation the virtual/live separation already has and already
tests.

Two rules keep this safe
------------------------
**The built-in three cannot be removed or redefined.** Every existing test,
path and migration assumes they exist. A registry that could delete ``live``
would be a registry that could strand a real trading record.

**A registered name must be a plain directory name.** The name becomes a path
segment, so anything that could climb out of the data root is refused at
registration rather than at write time — by which point something has already
decided where to put a file.

Profiles reference, they do not redefine
----------------------------------------
An account's risk, sizing, exit and behavioural settings are the *existing*
config objects: :class:`~cfdinvest.risk.RiskLimits`,
:class:`~cfdinvest.livehelper.config.LiveHelperConfig`,
:class:`~cfdinvest.livehelper.refine_exit.ExitRefinementConfig`. The registry
holds overrides and hands back a configured object; it does not define a
fourth place a limit can live.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Iterable, Mapping

logger = logging.getLogger(__name__)

__all__ = [
    "ACCOUNT_TYPES",
    "BUILT_IN",
    "AccountProfile",
    "AccountRegistry",
    "known_accounts",
    "default_registry",
]

#: What an account is *for*. ``live``, ``virtual`` and ``simulation`` describe
#: what the money is; the rest describe why the account exists separately —
#: a sleeve for one strategy, one traded only in certain regimes, one kept
#: small deliberately while a habit is worked on.
ACCOUNT_TYPES: tuple[str, ...] = (
    "live", "virtual", "simulation", "strategy", "regime", "psychology",
)

#: The three that always exist. Every existing path, test and migration
#: assumes them, and a registry that could remove ``live`` could strand a real
#: record.
BUILT_IN: dict[str, str] = {
    "virtual": "virtual",
    "live": "live",
    "simulation": "simulation",
}

#: A plain directory name. The account name becomes a path segment, so this is
#: checked at registration -- refusing at write time is too late, because
#: something has already decided where a file goes.
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


@dataclass(frozen=True, slots=True)
class AccountProfile:
    """One account: what it is, what it is for, and what governs it.

    Attributes
    ----------
    tree:
        The directory under the data root. Defaults to the name. Several
        accounts may deliberately share a tree — two strategy sleeves you
        want reported together — and that is why it is a separate field.
    risk_overrides, sizing_overrides, exit_overrides:
        Field-by-field overrides on the existing config objects. Held as
        mappings rather than as instances so an unknown field can be reported
        rather than raising on load, and so the registry does not have to
        import three config modules to be read.
    """

    name: str
    type: str = "virtual"
    label: str = ""
    purpose: str = ""
    enabled: bool = True
    tree: str | None = None

    risk_overrides: dict[str, Any] = field(default_factory=dict)
    sizing_overrides: dict[str, Any] = field(default_factory=dict)
    exit_overrides: dict[str, Any] = field(default_factory=dict)

    strategy: str | None = None
    regimes: tuple[str, ...] = ()
    instruments: tuple[str, ...] = ()
    notes: str | None = None

    def __post_init__(self) -> None:
        if not _NAME.match(str(self.name or "")):
            raise ValueError(
                f"account name must be a plain lowercase directory name, got "
                f"{self.name!r}"
            )
        if self.type not in ACCOUNT_TYPES:
            raise ValueError(
                f"account type must be one of {', '.join(ACCOUNT_TYPES)}, got "
                f"{self.type!r}"
            )

        tree = self.tree or self.name
        if not _NAME.match(str(tree)):
            raise ValueError(f"account tree must be a plain name, got {tree!r}")
        object.__setattr__(self, "tree", tree)

        if not self.label:
            object.__setattr__(self, "label", self.name.replace("_", " ").title())

        for name in ("regimes", "instruments"):
            object.__setattr__(self, name, tuple(getattr(self, name) or ()))

    @property
    def built_in(self) -> bool:
        return self.name in BUILT_IN

    @property
    def is_real_money(self) -> bool:
        """Whether a mistake here costs money.

        ``strategy`` and ``regime`` sleeves are *purposes*, not funding, so
        they are not assumed real — the honest answer for those is whichever
        tree they write into, which the router decides.
        """
        return self.type == "live"

    def risk_limits(self, base=None):
        """This account's book-level limits."""
        from cfdinvest.risk import RiskLimits

        return _configured(base or RiskLimits(), self.risk_overrides, "risk")

    def sizing_config(self, base=None):
        """This account's per-trade limits."""
        from cfdinvest.livehelper import LiveHelperConfig

        return _configured(
            base or LiveHelperConfig(), self.sizing_overrides, "sizing"
        )

    def exit_config(self, base=None):
        """This account's exit-refinement settings."""
        from cfdinvest.livehelper.refine_exit import ExitRefinementConfig

        return _configured(
            base or ExitRefinementConfig(), self.exit_overrides, "exit"
        )

    def to_document(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["regimes"] = list(self.regimes)
        payload["instruments"] = list(self.instruments)
        return payload

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> "AccountProfile":
        known = set(cls.__slots__)
        payload = {k: v for k, v in dict(document).items() if k in known}
        for name in ("regimes", "instruments"):
            if payload.get(name) is not None:
                payload[name] = tuple(payload[name])
        return cls(**payload)


def _configured(base, overrides: Mapping[str, Any], what: str):
    """Apply overrides to an existing config object.

    An unknown field is dropped with a warning rather than raising. A registry
    written against a newer version of a config should not stop an older
    version starting — and the alternative, silently accepting it, would let a
    typo look like a setting.
    """
    if not overrides:
        return base

    fields = set(getattr(base, "__dataclass_fields__", {}))
    usable = {k: v for k, v in overrides.items() if k in fields}
    unknown = sorted(set(overrides) - usable.keys())
    if unknown:
        logger.warning(
            "Ignoring unknown %s override(s): %s", what, ", ".join(unknown)
        )
    try:
        return replace(base, **usable)
    except (TypeError, ValueError) as error:
        logger.warning(
            "Could not apply the %s overrides (%s); using the defaults", what, error
        )
        return base


@dataclass(frozen=True, slots=True)
class AccountRegistry:
    """Every account this deployment knows about."""

    profiles: dict[str, AccountProfile] = field(default_factory=dict)
    path: str | None = None

    def __len__(self) -> int:
        return len(self.profiles)

    def __contains__(self, name: object) -> bool:
        return str(name) in self.profiles

    def __iter__(self):
        return iter(self.profiles.values())

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self.profiles))

    @property
    def enabled(self) -> tuple[AccountProfile, ...]:
        return tuple(p for p in self.profiles.values() if p.enabled)

    @property
    def trees(self) -> tuple[str, ...]:
        """Every directory this registry can reach."""
        return tuple(sorted({p.tree for p in self.profiles.values()}))

    def get(self, name: str) -> AccountProfile | None:
        return self.profiles.get(str(name))

    def of_type(self, kind: str) -> tuple[AccountProfile, ...]:
        return tuple(p for p in self.profiles.values() if p.type == kind)

    def register(self, profile: AccountProfile) -> "AccountRegistry":
        """Add or replace one account. Returns a new registry.

        A built-in may be *described* — given a label, a purpose, tighter
        limits — but not renamed or pointed at a different tree. Repointing
        ``live`` would make every artefact written so far unreachable under
        the name that wrote it.
        """
        existing = self.profiles.get(profile.name)
        if profile.name in BUILT_IN and profile.tree != BUILT_IN[profile.name]:
            raise ValueError(
                f"{profile.name!r} is a built-in account and must keep its own "
                f"tree ({BUILT_IN[profile.name]!r}); repointing it would make "
                "everything written so far unreachable"
            )
        if existing is not None and existing.tree != profile.tree:
            logger.warning(
                "Account %r now points at tree %r instead of %r. Nothing is "
                "moved -- the old tree keeps its artefacts and this account "
                "starts empty.",
                profile.name, profile.tree, existing.tree,
            )
        return AccountRegistry(
            profiles={**self.profiles, profile.name: profile}, path=self.path
        )

    def remove(self, name: str) -> "AccountRegistry":
        """Forget one account. Built-ins cannot be forgotten."""
        if name in BUILT_IN:
            raise ValueError(
                f"{name!r} is a built-in account and cannot be removed; "
                "disable it instead"
            )
        remaining = {k: v for k, v in self.profiles.items() if k != name}
        return AccountRegistry(profiles=remaining, path=self.path)

    def describe(self) -> list[dict[str, Any]]:
        """A table for the dashboard."""
        return [
            {
                "name": profile.name,
                "label": profile.label,
                "type": profile.type,
                "tree": profile.tree,
                "enabled": profile.enabled,
                "real money": profile.is_real_money,
                "strategy": profile.strategy,
                "regimes": ", ".join(profile.regimes) or None,
                "purpose": profile.purpose,
            }
            for profile in sorted(self.profiles.values(), key=lambda p: p.name)
        ]


def default_registry() -> AccountRegistry:
    """The three built-ins, described.

    What a deployment has before anyone registers anything, and what it falls
    back to when the registry file cannot be read — which keeps a corrupt
    registry from making ``live`` unreachable.
    """
    return AccountRegistry(
        profiles={
            "virtual": AccountProfile(
                name="virtual", type="virtual", label="Virtual account",
                purpose="A Trading 212 demo account. Nothing here is real money.",
            ),
            "live": AccountProfile(
                name="live", type="live", label="Live account",
                purpose="Real money. Every figure describes an account you can lose.",
            ),
            "simulation": AccountProfile(
                name="simulation", type="simulation", label="Simulation",
                purpose=(
                    "Rehearsals only. Written by the simulate layer and never "
                    "by the pipeline."
                ),
            ),
        }
    )


def known_accounts(config=None) -> tuple[str, ...]:
    """Every tree this deployment may read or write.

    The built-ins plus anything registered. This is what
    :func:`~cfdinvest.persist.storage.account_root` validates against, so
    registering an account is what makes its directory legal — and nothing
    else does.
    """
    from cfdinvest.persist.storage import ACCOUNTS

    names = set(ACCOUNTS)
    try:
        from cfdinvest.persist import accounts as store

        names |= set(store.load_registry(config).trees)
    except Exception as error:  # noqa: BLE001 - an unreadable registry must
        # never make the built-in trees unreachable.
        logger.debug("No account registry: %s", error)
    return tuple(sorted(names))
