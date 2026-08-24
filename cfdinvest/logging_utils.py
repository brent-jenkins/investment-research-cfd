"""Logging setup for cfdinvest entry points.

Library modules only ever call ``logging.getLogger(__name__)``; they never
configure handlers. Configuration is the job of the process that owns
``main()`` -- an example script, a CLI, or a notebook -- via
:func:`configure_logging`.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from cfdinvest.config import LoggingConfig

#: Root logger name for the package, so third-party noise stays separate.
PACKAGE_LOGGER = "cfdinvest"


def configure_logging(
    config: LoggingConfig | None = None,
    *,
    force: bool = False,
) -> logging.Logger:
    """Attach console (and optional file) handlers to the package logger.

    Idempotent: calling it twice will not duplicate handlers unless
    ``force=True``, which is useful when re-running a cell in a notebook.

    Parameters
    ----------
    config:
        Logging settings. Defaults to :class:`LoggingConfig` defaults.
    force:
        Remove existing handlers before attaching new ones.

    Returns
    -------
    logging.Logger
        The configured ``cfdinvest`` logger.
    """
    settings = config or LoggingConfig()
    logger = logging.getLogger(PACKAGE_LOGGER)

    if force:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()
    elif logger.handlers:
        logger.setLevel(settings.level.upper())
        return logger

    logger.setLevel(settings.level.upper())
    # Package logs are fully handled here; do not also bubble to the root
    # logger, which would double-print inside notebooks and pytest.
    logger.propagate = False

    formatter = logging.Formatter(settings.format)

    console = logging.StreamHandler(stream=sys.stderr)
    console.setFormatter(formatter)
    logger.addHandler(console)

    if settings.log_file:
        log_path = Path(settings.log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger
