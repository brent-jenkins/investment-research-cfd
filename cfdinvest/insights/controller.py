"""Running every insight over what is on disk, and assembling the report.

The impure half: this reads artefacts and hands frames to the pure functions
in :mod:`cfdinvest.insights.model`.

**One failing insight does not stop the pass.** Nine run; one of them raising
must not silence the other eight, and a crashed insight is reported as its own
"could not be computed" rather than vanishing — an insight that quietly
disappeared would read as one with nothing to say.

Nothing here drives the pipeline or places a trade.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

import pandas as pd

from cfdinvest.insights.model import Insight, model_insights

logger = logging.getLogger(__name__)

__all__ = ["InsightReport", "build_report", "run_insights"]


@dataclass(frozen=True, slots=True)
class InsightReport:
    """Everything the record could be asked, and what it answered."""

    timestamp: Any
    insights: tuple[Insight, ...] = ()
    run_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def usable(self) -> tuple[Insight, ...]:
        """The ones that say something you could act on."""
        return tuple(i for i in self.insights if i.usable)

    @property
    def insufficient(self) -> tuple[Insight, ...]:
        return tuple(i for i in self.insights if i.confidence == "insufficient")

    @property
    def headline(self) -> str:
        """What to put in a subject line.

        Counts what could be *said*, not what was asked. "9 insights" over a
        record too thin to support one would be the layer flattering itself.
        """
        usable = len(self.usable)
        if not usable:
            return "Nothing can be said yet — the record is too short to slice."
        return (
            f"{usable} of {len(self.insights)} insight(s) have enough record "
            "behind them."
        )

    def get(self, key: str) -> Insight | None:
        return next((i for i in self.insights if i.key == key), None)

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
                "insights": [
                    {
                        "key": item.key,
                        "title": item.title,
                        "finding": item.finding,
                        "confidence": item.confidence,
                        "trades": item.trades,
                        "caveats": list(item.caveats),
                        "buckets": [
                            {
                                "label": bucket.label,
                                "trades": bucket.trades,
                                "expectancy": bucket.expectancy,
                                "total": bucket.total,
                                "win_rate": bucket.win_rate,
                            }
                            for bucket in item.buckets
                        ],
                        "details": item.details,
                    }
                    for item in self.insights
                ],
                "details": self.details,
            }
        )


def _guarded(key: str, title: str, call: Callable[[], Insight]) -> Insight:
    """Run one insight, turning a crash into a reported absence."""
    try:
        return call()
    except Exception as error:  # noqa: BLE001 - one insight, not the pass
        logger.exception("Insight %s failed", key)
        return Insight(
            key=key,
            title=title,
            finding="",
            confidence="insufficient",
            caveats=(
                f"This could not be computed: {error}. That is a bug here, not "
                "a finding about your trading — and it means this question is "
                "currently unanswered.",
            ),
        )


def build_report(
    trades=None,
    *,
    regime_history=None,
    confirmations=None,
    risk_history=None,
    run_id: str | None = None,
    now=None,
) -> InsightReport:
    """Run every insight over the frames given. Pure with respect to them."""
    moment = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="UTC")
    if moment.tz is None:
        moment = moment.tz_localize("UTC")

    insights = tuple(
        _guarded(key, title, call)
        for key, title, call in model_insights(
            trades,
            regime_history=regime_history,
            confirmations=confirmations,
            risk_history=risk_history,
        )
    )

    return InsightReport(
        timestamp=moment,
        insights=insights,
        run_id=run_id,
        details={
            "trades_available": 0 if trades is None else int(len(trades)),
            "confirmations": 0 if confirmations is None else int(len(confirmations)),
            "risk_snapshots": 0 if risk_history is None else int(len(risk_history)),
        },
    )


def run_insights(
    config=None,
    *,
    account: str | None = None,
    run_id: str | None = None,
    store: bool = True,
    now=None,
) -> InsightReport:
    """Read the artefacts, build the report, optionally record it.

    Never raises. Returns a report whether or not there was anything to say,
    because "the record is too short" is itself the most useful thing this
    layer can tell someone new.
    """
    from cfdinvest.persist import confirm, readers
    from cfdinvest.persist import risk as risk_store

    def read(name: str, *args, default=None):
        try:
            return getattr(readers, name)(*args)
        except Exception as error:  # noqa: BLE001
            logger.debug("Could not read %s: %s", name, error)
            return default

    chosen = run_id or read("load_latest_run", config)
    trades = read(
        "load_frame", "trades", chosen, config, account, default=pd.DataFrame()
    )
    regimes = read(
        "load_regime_frame", chosen, config, account, default=pd.DataFrame()
    )

    try:
        confirmations = confirm.to_frame(confirm.load_confirmations(config, account))
    except Exception as error:  # noqa: BLE001
        logger.debug("No confirmations: %s", error)
        confirmations = pd.DataFrame()

    try:
        risk_history = risk_store.load_risk_history(config, account)
    except Exception as error:  # noqa: BLE001
        logger.debug("No risk history: %s", error)
        risk_history = pd.DataFrame()

    report = build_report(
        trades,
        regime_history=regimes,
        confirmations=confirmations,
        risk_history=risk_history,
        run_id=chosen,
        now=now,
    )

    if store:
        from cfdinvest.persist import insights as insight_store

        insight_store.write_insight_report(report, config, account=account)

    logger.info("Insights: %s", report.headline)
    return report
