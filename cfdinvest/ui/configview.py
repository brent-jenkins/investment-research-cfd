"""Flattening config objects for display, and spotting what is odd about them.

Three questions a config page has to answer, and none of them is "what are
the values":

* **What was overridden?** A setting that differs from its default is a
  decision somebody made, and it is the first thing to look at when a run
  behaves unexpectedly.
* **What is missing?** An unset value is not always wrong — most are
  optional — but an unset *path* or credential is why nothing works.
* **What is not recognised?** A key in the config file that no dataclass
  field matches is silently ignored by the loader. That is the failure mode
  where someone fixes a setting, redeploys, and nothing changes.

Everything here is pure and takes plain objects, so it is tested without a
Streamlit runtime.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

import pandas as pd

__all__ = [
    "DEPRECATED_FIELDS",
    "SECRET_HINTS",
    "flatten",
    "to_frame",
    "unknown_keys",
    "redact",
]

#: Fields that still load but should no longer be set, mapped to advice.
#: Empty today — the project has renamed settings but never kept an alias —
#: and present so that the first deprecation has an obvious home rather than
#: becoming a comment nobody reads.
DEPRECATED_FIELDS: dict[str, str] = {}

#: Substrings that mean a value must never be rendered. The lake config
#: carries a live FRED API key; a dashboard that prints it to a browser has
#: leaked it to every proxy log between here and the viewer.
SECRET_HINTS: tuple[str, ...] = (
    "key", "token", "secret", "password", "passwd", "credential", "auth",
)


def redact(path: str, value: Any) -> Any:
    """Mask anything whose name suggests it is a credential.

    Errs towards masking: a redacted non-secret is a minor annoyance, a
    printed secret is not recoverable.
    """
    lowered = path.lower().replace("_", "")
    if any(hint.replace("_", "") in lowered for hint in SECRET_HINTS):
        if value in (None, "", ()):
            return value
        text = str(value)
        return f"<redacted, {len(text)} chars>"
    return value


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, (str, tuple, list, dict, set)) and len(value) == 0:
        return True
    return False


def flatten(
    obj: Any,
    default: Any = None,
    *,
    prefix: str = "",
    _depth: int = 0,
) -> list[dict[str, Any]]:
    """One row per leaf setting, with its default beside it.

    Recurses into nested dataclasses, which is how every config in this
    project is built. ``default`` should be a freshly constructed instance
    of the same type; when it is ``None`` the ``overridden`` column is left
    unknown rather than guessed.
    """
    if not is_dataclass(obj) or _depth > 6:
        return []

    rows: list[dict[str, Any]] = []
    for field in fields(obj):
        value = getattr(obj, field.name, None)
        fallback = getattr(default, field.name, None) if default is not None else None
        path = f"{prefix}{field.name}"

        if is_dataclass(value):
            rows.extend(flatten(value, fallback, prefix=f"{path}.", _depth=_depth + 1))
            continue

        shown = redact(path, value)
        differs: Any = None
        if default is not None:
            try:
                differs = bool(value != fallback)
            except Exception:  # noqa: BLE001 - odd types compare oddly
                differs = None

        rows.append(
            {
                "setting": path,
                "value": _display(shown),
                "default": _display(redact(path, fallback))
                if default is not None
                else "",
                "overridden": differs,
                "missing": _is_missing(value),
                "deprecated": path in DEPRECATED_FIELDS,
                "note": DEPRECATED_FIELDS.get(path, ""),
            }
        )
    return rows


def _display(value: Any) -> str:
    """A short, readable rendering of any setting value."""
    if value is None:
        return ""
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        if not items:
            return "()"
        if len(items) > 8:
            return f"{', '.join(map(str, items[:8]))} … (+{len(items) - 8})"
        return ", ".join(map(str, items))
    if isinstance(value, Mapping):
        if not value:
            return "{}"
        pairs = [f"{k}={v}" for k, v in list(value.items())[:6]]
        extra = "" if len(value) <= 6 else f" … (+{len(value) - 6})"
        return ", ".join(pairs) + extra
    return str(value)


def to_frame(obj: Any, default: Any = None, *, prefix: str = "") -> pd.DataFrame:
    """:func:`flatten` as a DataFrame, sorted by setting name."""
    rows = flatten(obj, default, prefix=prefix)
    if not rows:
        return pd.DataFrame(
            columns=["setting", "value", "default", "overridden", "missing",
                     "deprecated", "note"]
        )
    return pd.DataFrame(rows).sort_values("setting").reset_index(drop=True)


def unknown_keys(
    payload: Mapping[str, Any] | None, obj: Any, *, prefix: str = ""
) -> list[str]:
    """Keys in a config file that no dataclass field matches.

    These are **silently ignored** by the loader, which makes them the
    quietest way for a config change to have no effect at all.
    """
    if not payload or not is_dataclass(obj):
        return []

    known = {field.name: getattr(obj, field.name, None) for field in fields(obj)}
    unknown: list[str] = []
    for key, value in payload.items():
        path = f"{prefix}{key}"
        if key not in known:
            unknown.append(path)
            continue
        nested = known[key]
        if isinstance(value, Mapping) and is_dataclass(nested):
            unknown.extend(unknown_keys(value, nested, prefix=f"{path}."))
    return sorted(unknown)
