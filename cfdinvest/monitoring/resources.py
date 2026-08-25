"""Reading the machine. The only impure part of the monitoring layer.

Kept separate so :mod:`cfdinvest.monitoring.core` stays pure and testable: a
test describes a full disk rather than having one.

**Absent readings are reported absent.** Disk space comes from the standard
library and always works. CPU and memory need ``psutil``, which is not a
dependency of this project — so on a box without it these come back ``None``
and the check says "not measured" rather than "fine". A monitoring layer that
reported an unmeasured resource as healthy would be worse than one that did
not look at all, because it would be trusted.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["read_disk_usage", "read_system_resources", "psutil_available"]

_GB = 1024.0 ** 3


def psutil_available() -> bool:
    try:
        import psutil  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def read_disk_usage(path: Path | str | None = None) -> dict[str, Any]:
    """Free and total space on the volume holding ``path``.

    Walks up to the nearest existing parent: the data root often does not
    exist yet on a fresh install, and the volume it *would* live on is the
    thing worth measuring.
    """
    target = Path(path or Path.cwd()).resolve()
    while not target.exists() and target != target.parent:
        target = target.parent

    try:
        usage = shutil.disk_usage(target)
    except OSError as error:
        logger.warning("Could not read disk usage at %s: %s", target, error)
        return {"path": str(target), "free_gb": None, "total_gb": None,
                "error": str(error)}

    return {
        "path": str(target),
        "free_gb": usage.free / _GB,
        "total_gb": usage.total / _GB,
        "used_gb": usage.used / _GB,
    }


def read_system_resources(path: Path | str | None = None) -> dict[str, Any]:
    """Disk, and CPU and memory where they can be read.

    Never raises. ``cpu_percent`` and ``memory_percent`` are ``None`` when
    psutil is absent, and ``measured`` says which of the three were taken so a
    reader can tell "healthy" from "not looked at".
    """
    resources: dict[str, Any] = {
        "cpu_percent": None,
        "memory_percent": None,
        "psutil": False,
        **read_disk_usage(path),
    }

    try:
        import psutil
    except Exception:  # noqa: BLE001 - an optional dependency, absent is normal
        resources["measured"] = tuple(
            name for name in ("disk",) if resources.get("free_gb") is not None
        )
        return resources

    try:
        # interval=None returns the load since the last call rather than
        # blocking. A monitoring pass that slept a second to measure CPU would
        # be a monitoring pass that made the thing it was measuring worse.
        resources["cpu_percent"] = float(psutil.cpu_percent(interval=None))
        resources["memory_percent"] = float(psutil.virtual_memory().percent)
        resources["psutil"] = True
    except Exception as error:  # noqa: BLE001
        logger.warning("Could not read CPU or memory: %s", error)

    resources["measured"] = tuple(
        name
        for name, value in (
            ("disk", resources.get("free_gb")),
            ("cpu", resources.get("cpu_percent")),
            ("memory", resources.get("memory_percent")),
        )
        if value is not None
    )
    return resources
