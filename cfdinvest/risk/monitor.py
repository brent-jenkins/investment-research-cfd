"""The periodic risk-snapshot task.

Every N minutes: read the latest persisted positions, regimes, profit and
loss and scheduler state; compute a :class:`~cfdinvest.risk.model.RiskSnapshot`;
optionally record it. That is the whole task.

**It cannot drive the pipeline.** There is no import of the backtest engine,
the runner or the repository anywhere below, so no code path exists through
which a snapshot could trigger a run, refresh the lake or touch a position.
The only write is the snapshot file itself, which lives in its own directory
and is never read back by anything that trades.

Why a separate loop rather than a job on
:class:`~cfdinvest.schedule.scheduler.Scheduler`: that loop fires on *market
session* events — an open, a close — because that is when a run is worth
doing. Risk is worth observing on the clock instead, and most usefully when
nothing is happening: a book that drifts into a breach at 3am did so between
two session events, and a snapshot taken only at the close would date the
breach to the close.

The clock and the sleeper are injected, so a test can advance a day in a few
iterations without sleeping.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Mapping

import pandas as pd

from cfdinvest.risk.model import RiskLimits, RiskSnapshot
from cfdinvest.risk.snapshot import snapshot_from_disk

logger = logging.getLogger(__name__)

__all__ = [
    "RiskMonitorConfig",
    "RiskObservation",
    "take_risk_snapshot",
    "RiskMonitor",
]


@dataclass(frozen=True, slots=True)
class RiskMonitorConfig:
    """How often to look, and whether to keep what is seen.

    Attributes
    ----------
    interval_minutes:
        Wall-clock gap between observations. Fifteen minutes by default: a
        book of daily-bar CFD positions does not change between reads, so
        the interval is set by how soon you want to *hear* about a breach,
        not by how fast the numbers move.
    store:
        Record each snapshot. On by default — the history is most of the
        value, since the useful question is usually "when did this start".
    stop_on_critical:
        End the loop the first time a limit is breached. Off by default: the
        loop is an observer, and an observer that stops watching when things
        get interesting is the wrong way round. Available because an operator
        driving this from a shell may want exactly that.
    max_observations:
        Stop after this many. ``None`` runs until interrupted.
    """

    interval_minutes: float = 15.0
    store: bool = True
    stop_on_critical: bool = False
    max_observations: int | None = None

    def __post_init__(self) -> None:
        if self.interval_minutes <= 0:
            raise ValueError(
                f"interval_minutes must be > 0, got {self.interval_minutes}; "
                "a zero interval is a busy loop, not a monitor"
            )

    @property
    def interval_seconds(self) -> float:
        return self.interval_minutes * 60.0


@dataclass(frozen=True, slots=True)
class RiskObservation:
    """One reading, and what became of it."""

    at: pd.Timestamp
    snapshot: RiskSnapshot
    stored_path: str | None = None

    @property
    def severity(self) -> str:
        return self.snapshot.severity

    @property
    def flags(self) -> tuple[str, ...]:
        return self.snapshot.flags


def take_risk_snapshot(
    config=None,
    *,
    account: str | None = None,
    limits: RiskLimits | None = None,
    instrument_leverage: Mapping[str, float] | None = None,
    store: bool = True,
    now: pd.Timestamp | None = None,
) -> RiskObservation:
    """Read the artefacts, compute one snapshot, optionally record it.

    Returns an observation even when nothing has been persisted. A monitor
    that returns nothing when the pipeline has not run cannot report that the
    pipeline has not run, which is the state most worth reporting.
    """
    moment = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="UTC")
    if moment.tz is None:
        moment = moment.tz_localize("UTC")

    snapshot = snapshot_from_disk(
        config,
        account=account,
        limits=limits,
        instrument_leverage=instrument_leverage,
        now=moment,
    )

    path = None
    if store:
        from cfdinvest.persist import risk as risk_store

        path = risk_store.write_risk_snapshot(
            snapshot, config, account=account, now=moment
        )

    logger.info(
        "Risk snapshot at %s: %s (%s)",
        moment.isoformat(),
        snapshot.severity,
        snapshot.message,
    )
    return RiskObservation(at=moment, snapshot=snapshot, stored_path=path)


class RiskMonitor:
    """Take a snapshot every N minutes, indefinitely.

    Nothing here can start a run. The loop reads artefacts, writes snapshots
    and calls back; the pipeline is driven by
    :mod:`cfdinvest.schedule.scheduler` and by nothing else.

    Parameters
    ----------
    on_observation:
        Called with each :class:`RiskObservation`. Typically a printer or an
        alerter. Exceptions from it are caught and logged — a failed alert
        must not stop the monitoring, or one bad mail server ends the watch.
    """

    def __init__(
        self,
        config=None,
        monitor_config: RiskMonitorConfig | None = None,
        *,
        account: str | None = None,
        limits: RiskLimits | None = None,
        instrument_leverage: Mapping[str, float] | None = None,
        on_observation: Callable[[RiskObservation], Any] | None = None,
        clock: Callable[[], pd.Timestamp] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self.config = config
        self.settings = monitor_config or RiskMonitorConfig()
        self.account = account
        self.limits = limits
        self.instrument_leverage = instrument_leverage
        self.on_observation = on_observation
        self.clock = clock or (lambda: pd.Timestamp.now(tz="UTC"))
        self.sleeper = sleeper
        self.history: list[RiskObservation] = []

    def observe(self, now: pd.Timestamp | None = None) -> RiskObservation | None:
        """One reading. Returns ``None`` only if reading itself failed.

        A failure to read is contained rather than raised: the monitor's job
        is to keep watching, and an unreadable artefact at 03:00 must not end
        the watch that would have caught the breach at 04:00.
        """
        try:
            observation = take_risk_snapshot(
                self.config,
                account=self.account,
                limits=self.limits,
                instrument_leverage=self.instrument_leverage,
                store=self.settings.store,
                now=now if now is not None else self.clock(),
            )
        except Exception as error:  # noqa: BLE001 - one bad read, not the end
            logger.exception("Risk snapshot failed: %s", error)
            return None

        self.history.append(observation)
        if self.on_observation is not None:
            try:
                self.on_observation(observation)
            except Exception:  # noqa: BLE001 - a failed alert is not a failed read
                logger.exception("Risk observation handler failed")
        return observation

    def _sleep(self, seconds: float) -> None:
        if seconds <= 0:
            return
        if self.sleeper is not None:
            self.sleeper(seconds)
            return
        import time

        time.sleep(seconds)

    def run_forever(self) -> list[RiskObservation]:
        """Observe, sleep, repeat. Returns every observation taken.

        Bounded by ``max_observations``; unbounded watches return only on
        interrupt, which is caught by the caller rather than here.
        """
        taken = 0
        while (
            self.settings.max_observations is None
            or taken < self.settings.max_observations
        ):
            observation = self.observe()
            taken += 1

            if (
                observation is not None
                and self.settings.stop_on_critical
                and observation.severity == "critical"
            ):
                logger.warning(
                    "Stopping the risk monitor: %s", "; ".join(observation.flags)
                )
                break

            if (
                self.settings.max_observations is not None
                and taken >= self.settings.max_observations
            ):
                break
            self._sleep(self.settings.interval_seconds)

        return list(self.history)
