"""Retry helpers shared by every network-facing ingestion path.

Market-data vendors fail transiently and often: rate limits, 5xx blips,
connection resets. Rather than scatter ``try/except`` blocks through the
downloaders, they all funnel through :func:`retry_call`.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Callable, Sequence, TypeVar

from cfdinvest.config import NetworkConfig

logger = logging.getLogger(__name__)

T = TypeVar("T")


def backoff_delays(
    config: NetworkConfig,
    *,
    jitter: bool = True,
    rng: random.Random | None = None,
) -> list[float]:
    """Compute the sleep interval before each retry.

    Pure given ``rng`` -- pass a seeded :class:`random.Random` (or
    ``jitter=False``) to make tests deterministic.

    Returns
    -------
    list of float
        ``max_retries - 1`` delays in seconds, exponentially spaced and
        clamped to :attr:`NetworkConfig.backoff_cap`.

    >>> backoff_delays(NetworkConfig(max_retries=4, backoff_base=2.0), jitter=False)
    [1.0, 2.0, 4.0]
    """
    generator = rng or random
    delays = []
    for attempt in range(max(config.max_retries - 1, 0)):
        delay = min(config.backoff_base**attempt, config.backoff_cap)
        if jitter:
            # Full jitter: spreads a thundering herd of parallel workers out
            # instead of having them all retry on the same tick.
            delay = generator.uniform(0.0, delay)
        delays.append(delay)
    return delays


def retry_call(
    func: Callable[[], T],
    *,
    config: NetworkConfig,
    description: str,
    retry_on: Sequence[type[BaseException]] = (Exception,),
    sleep: Callable[[float], None] | None = None,
    rng: random.Random | None = None,
) -> T:
    """Call ``func`` with exponential backoff, re-raising the final failure.

    Parameters
    ----------
    func:
        Zero-argument callable; bind arguments with ``functools.partial`` or a
        lambda at the call site.
    config:
        Retry policy.
    description:
        Human-readable label used in log messages, e.g. ``"FRED DGS10"``.
    retry_on:
        Exception types considered transient. Anything else propagates
        immediately -- a malformed request should not be retried four times.
    sleep:
        Injected for tests. ``None`` resolves ``time.sleep`` at CALL time,
        not at import time -- binding it as a default value would capture
        the original function and silently defeat monkeypatching.
    rng:
        Injected jitter source; defaults to the module-level RNG.

    Raises
    ------
    BaseException
        The exception from the final attempt, once retries are exhausted.
    """
    delays = backoff_delays(config, rng=rng)
    attempts = len(delays) + 1
    pause = sleep if sleep is not None else time.sleep

    for attempt in range(attempts):
        try:
            return func()
        except tuple(retry_on) as exc:
            if attempt == attempts - 1:
                logger.error(
                    "%s failed after %d attempt(s): %s", description, attempts, exc
                )
                raise
            delay = delays[attempt]
            logger.warning(
                "%s failed (attempt %d/%d): %s -- retrying in %.1fs",
                description,
                attempt + 1,
                attempts,
                exc,
                delay,
            )
            pause(delay)

    raise AssertionError("unreachable")  # pragma: no cover
