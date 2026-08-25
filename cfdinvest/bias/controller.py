"""Running the detectors, and filling the profile the refinements read.

The output of this layer is a
:class:`~cfdinvest.livehelper.context.BiasProfile` — the contract the
refinement layer was written against before this existed. That is deliberate:
one shape, filled here and read there, rather than a second profile that has
to be kept in step.

**A severity produced here shrinks a real position.**
:mod:`cfdinvest.livehelper.refine_sizing` cuts size when one passes 0.5, so
what this layer asserts has consequences beyond the reading of it. Which is
why an unmeasured pattern stays ``None`` all the way through — never rounded
to zero, never defaulted to "fine".

One failing detector does not stop the pass, and a crashed one is reported
rather than dropped: a pattern that vanished would read as one with no
evidence for it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

import pandas as pd

from cfdinvest.bias.model import BIASES, BiasFinding, model_detectors

logger = logging.getLogger(__name__)

__all__ = ["BiasReport", "build_bias_report", "run_bias_detection"]


@dataclass(frozen=True, slots=True)
class BiasReport:
    """Every pattern looked for, and what was found."""

    timestamp: Any
    findings: tuple[BiasFinding, ...] = ()
    run_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def asserted(self) -> tuple[BiasFinding, ...]:
        """The ones strong enough to act on, worst first."""
        return tuple(
            sorted(
                (f for f in self.findings if f.asserted),
                key=lambda f: -(f.severity or 0),
            )
        )

    @property
    def measured(self) -> tuple[BiasFinding, ...]:
        return tuple(f for f in self.findings if f.measured)

    @property
    def unmeasured(self) -> tuple[BiasFinding, ...]:
        return tuple(f for f in self.findings if not f.measured)

    @property
    def headline(self) -> str:
        """What to say first.

        Names how much could be looked at, not just what was found. "No biases
        detected" over a record too short to detect one is the most misleading
        sentence this layer could produce.
        """
        if not self.measured:
            return (
                "Nothing could be measured yet — the record is too short for "
                "any behavioural pattern to mean anything."
            )
        if not self.asserted:
            return (
                f"{len(self.measured)} of {len(self.findings)} pattern(s) could "
                "be measured, and none is pronounced enough to act on."
            )
        worst = self.asserted[0]
        return (
            f"{worst.title.lower()} stands out ({worst.severity:.0%}), from "
            f"{worst.observations} observations."
        )

    def get(self, key: str) -> BiasFinding | None:
        return next((f for f in self.findings if f.key == key), None)

    def to_profile(self):
        """As the :class:`BiasProfile` the refinement layer reads.

        Unmeasured patterns stay ``None``. Rounding them to zero would turn
        "we could not look" into "there is nothing there", and the refinements
        treat those differently on purpose.
        """
        from cfdinvest.livehelper.context import BiasProfile

        return BiasProfile(
            available=bool(self.measured),
            as_of=self.timestamp,
            **{
                finding.key: finding.severity
                for finding in self.findings
                if finding.key in BIASES
            },
        )

    def to_document(self) -> dict[str, Any]:
        from cfdinvest.persist.writers import jsonable

        return jsonable(
            {
                "timestamp": (
                    self.timestamp.isoformat()
                    if hasattr(self.timestamp, "isoformat")
                    else str(self.timestamp)
                ),
                "run_id": self.run_id,
                "headline": self.headline,
                "findings": [
                    {
                        "key": f.key,
                        "title": f.title,
                        "severity": f.severity,
                        "observations": f.observations,
                        "confidence": f.confidence,
                        "evidence": f.evidence,
                        "innocent_reading": f.innocent_reading,
                        "action": f.action,
                        "details": f.details,
                    }
                    for f in self.findings
                ],
                "details": self.details,
            }
        )


def _guarded(key: str, call: Callable[[], BiasFinding]) -> BiasFinding:
    """Run one detector, turning a crash into a reported absence."""
    try:
        return call()
    except Exception as error:  # noqa: BLE001 - one detector, not the pass
        logger.exception("Bias detector %s failed", key)
        return BiasFinding(
            key=key,
            title=BIASES.get(key, key),
            severity=None,
            confidence="insufficient",
            details={
                "reason": (
                    f"this could not be computed: {error}. That is a bug here, "
                    "not a finding about you"
                )
            },
        )


def build_bias_report(
    *,
    confirmations=None,
    trades=None,
    risk_history=None,
    assessments=None,
    run_id: str | None = None,
    now=None,
) -> BiasReport:
    """Run every detector over the frames given. Pure with respect to them."""
    moment = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="UTC")
    if moment.tz is None:
        moment = moment.tz_localize("UTC")

    findings = tuple(
        _guarded(key, call)
        for key, call in model_detectors(
            confirmations=confirmations,
            trades=trades,
            risk_history=risk_history,
            assessments=assessments,
        )
    )

    return BiasReport(
        timestamp=moment,
        findings=findings,
        run_id=run_id,
        details={
            "confirmations": 0 if confirmations is None else int(len(confirmations)),
            "trades": 0 if trades is None else int(len(trades)),
            "risk_snapshots": 0 if risk_history is None else int(len(risk_history)),
            "assessments": 0 if assessments is None else int(len(assessments)),
        },
    )


def run_bias_detection(
    config=None,
    *,
    account: str | None = None,
    run_id: str | None = None,
    store: bool = True,
    now=None,
) -> BiasReport:
    """Read the artefacts, run the detectors, optionally record the result.

    Never raises. Returns a report whether or not anything could be measured,
    because "the record is too short" is the honest answer for most of the
    first year and the one this layer should be loudest about.
    """
    from cfdinvest.persist import assessments as assessment_store
    from cfdinvest.persist import confirm, readers
    from cfdinvest.persist import risk as risk_store

    def read(call, default):
        try:
            return call()
        except Exception as error:  # noqa: BLE001
            logger.debug("Could not read: %s", error)
            return default

    chosen = run_id or read(lambda: readers.load_latest_run(config), None)

    confirmations = read(
        lambda: confirm.to_frame(confirm.load_confirmations(config, account)),
        pd.DataFrame(),
    )
    trades = read(
        lambda: readers.load_frame("trades", chosen, config, account),
        pd.DataFrame(),
    )
    risk_history = read(
        lambda: risk_store.load_risk_history(config, account), pd.DataFrame()
    )
    assessments = read(
        lambda: assessment_store.load_assessments(config, account), pd.DataFrame()
    )

    report = build_bias_report(
        confirmations=confirmations,
        trades=trades,
        risk_history=risk_history,
        assessments=assessments,
        run_id=chosen,
        now=now,
    )

    if store:
        from cfdinvest.persist import bias as bias_store

        bias_store.write_bias_report(report, config, account=account)

    logger.info("Bias detection: %s", report.headline)
    return report
