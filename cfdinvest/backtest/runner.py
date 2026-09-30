"""Pipeline runner: lake to equity curve in one call.

Orchestration only. Every stage is the layer that owns it, called through
its public entry point; this module adds no modelling of its own, which is
what keeps the pipeline honest — there is nowhere here to quietly adjust a
threshold because the result looked disappointing.

    repository -> AnalysisFrame -> RegimeFrame -> SignalFrame -> ExecutionFrame

The split between :func:`run_pipeline` and :func:`run_from_analysis` matters
more than it looks. Preprocessing and regime classification are causal but
expensive; execution is path-dependent and cheap. Walk-forward and parameter
sweeps therefore run the first half once and the second half many times,
which is only possible because the boundary is a function argument rather
than a step inside one big procedure.
"""

from __future__ import annotations

import dataclasses

import logging
from dataclasses import dataclass, field
from typing import Any, Sequence

import pandas as pd

from cfdinvest.execution import ExecutionConfig, run_backtest
from cfdinvest.ingestion import capital_prices
from cfdinvest.preprocessing import (
    PreprocessConfig,
    analysis_frame_from_repository,
    build_analysis_frame,
)
from cfdinvest.regimes import RegimeConfig, classify_regimes
from cfdinvest.signals import SignalConfig, generate_signals
from cfdinvest.backtest.metrics import MetricsConfig

logger = logging.getLogger(__name__)

#: Where currency pairs are read from when the decision feed does not carry
#: them. The broker quotes shares, not ``GBPUSD=X``; the pairs are vendor
#: symbols and live only under vendor intervals. Rates are forward-filled
#: onto the traded grid anyway, so the grid they arrive on does not matter.
REFERENCE_FALLBACK_INTERVAL = "1h"

__all__ = [
    "BacktestConfig", "PipelineResult", "run_pipeline", "run_from_analysis",
    "REFERENCE_FALLBACK_INTERVAL",
]


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    """Universe, date range and every layer's settings in one object."""

    # -- what to run over -------------------------------------------------- #
    tickers: Sequence[str] | None = None
    series_ids: Sequence[str] | None = None
    interval: str | None = None
    start: str | pd.Timestamp | None = None
    end: str | pd.Timestamp | None = None

    # -- per-layer settings ------------------------------------------------ #
    preprocess: PreprocessConfig = field(default_factory=PreprocessConfig)
    regime: RegimeConfig = field(default_factory=RegimeConfig)
    signal: SignalConfig = field(default_factory=SignalConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    metrics: MetricsConfig = field(default_factory=MetricsConfig)

    #: Run without regime filtering. Useful for isolating the trend engine;
    #: not the production path, and recorded in metadata so a result cannot
    #: be mistaken for a filtered one.
    use_regimes: bool = True

    #: Labelled company news, as ``signals.news.events_from_labels`` returns
    #: it. ``None`` -- the default, and every research run's -- means no
    #: news-driven trades, so no recorded figure moves because this exists.
    #: Set only on the run the dealer reads, when ``strategy.news_trading``
    #: is on (owner's decision, 2026-09-30).
    news_events: Any = None


@dataclass(frozen=True, slots=True)
class PipelineResult:
    """The four frames a pipeline run produces."""

    analysis: Any = None
    regimes: Any = None
    signals: Any = None
    execution: Any = None


def run_from_analysis(
    analysis_frame,
    config: BacktestConfig | None = None,
) -> PipelineResult:
    """Run regimes, signals and execution over an existing AnalysisFrame.

    Pure with respect to its input: the analysis frame is read, never
    mutated. This is the entry point walk-forward and tests use, because it
    skips the expensive causal stages that only need computing once.

    Returns
    -------
    PipelineResult
    """
    settings = config or BacktestConfig()

    regimes = (
        classify_regimes(analysis_frame, settings.regime)
        if settings.use_regimes
        else None
    )
    signals = generate_signals(analysis_frame, regimes, settings.signal)
    if settings.news_events is not None:
        from cfdinvest.signals.news import apply_news, news_sign_panel

        # News-driven bars written over the trend's, in the one signal step
        # every consumer shares. See `signals.news`.
        signals = apply_news(
            signals,
            news_sign_panel(settings.news_events, signals.direction.index,
                            signals.direction.columns),
            min_size=settings.signal.min_size,
        )

    # A breakout is required before a move is entered, where a trend is
    # running. Built here rather than in the execution config because it
    # needs the price panel, the direction frame and the regime -- all of
    # which exist only at this point in the pipeline.
    #
    # Measured through the engine at four lookbacks: 79.5 / 80.5 / 80.2 /
    # 80.3 percent against 73.6 for no gate, with drawdown -26.1 against
    # -30.0 and trades slightly lower. A one-point spread across an eightfold
    # range of lookback, which is insensitivity to the parameter rather than
    # a peak to fall off.
    execution_settings = settings.execution
    lookback = int(getattr(settings.execution, "breakout_lookback", 0) or 0)
    if lookback:
        from cfdinvest.signals.breakout import entry_mask, gating_frame

        # Which regime gates the confirmation is configurable rather than
        # hardwired to the trend frame -- see ExecutionConfig.breakout_regime.
        gate_frame, gate_labels = gating_frame(
            regimes, getattr(settings.execution, "breakout_regime", "trend")
        )
        mask = entry_mask(
            analysis_frame.close, signals.direction, gate_frame,
            lookback=lookback,
            **({"regimes": gate_labels} if gate_labels else {}),
        )
        if mask is not None:
            # A news trade is not a trend trade, so the breakout question
            # does not apply to it. Relaxed HERE, on the breakout mask only,
            # so the screen's membership composed below still binds.
            from cfdinvest.signals.news import exempt_news_from_gate

            mask = exempt_news_from_gate(mask, signals)
            # Combined, never replaced. ``entry_gate`` is a public
            # ExecutionConfig field and the breakout is only one of the
            # things that may shut it -- a universe screen is another. This
            # used to assign, so with breakout_lookback set (it is, at 12)
            # any caller-supplied gate was silently discarded and its arm
            # measured the baseline under another name. That is the inert-arm
            # failure this project has already paid for three times.
            #
            # AND is the right composition: both answer "may this be
            # opened", and a name has to clear every gate rather than the
            # last one to be written. Missing entries count as permitted,
            # matching the engine's own default -- "no reading" is not
            # "refused".
            mask = compose_entry_gates(
                getattr(execution_settings, "entry_gate", None), mask
            )
            execution_settings = dataclasses.replace(
                execution_settings, entry_gate=mask
            )
        else:
            logger.warning(
                "Breakout gate configured but no mask could be built; "
                "entries are ungated this run"
            )

    execution = run_backtest(signals, analysis_frame, execution_settings)

    return PipelineResult(
        analysis=analysis_frame,
        regimes=regimes,
        signals=signals,
        execution=execution,
    )


def compose_entry_gates(existing, mask):
    """Combine two "may this be opened" masks, refusing unless both permit.

    ``entry_gate`` is a public ExecutionConfig field and the breakout is only
    one of the things that may shut it -- a universe screen is another, and
    the two have to hold at once. This used to be an assignment, so with
    ``breakout_lookback`` set (it is, at 12) a caller-supplied gate was
    silently discarded and its arm measured the baseline under a different
    label. A universe sweep built that way returns an identical number at
    every size, which reads as "size does not matter" rather than as a bug.

    Missing entries count as permitted, matching the engine's own default:
    "no reading" is not "refused". Returns ``mask`` unchanged when nothing
    was supplied, so a run that configures only the breakout is untouched.
    """
    if existing is None:
        return mask
    if mask is None:
        return existing
    aligned = (
        existing.reindex(index=mask.index, columns=mask.columns)
        .fillna(True)
        .astype(bool)
    )
    return mask.astype(bool) & aligned


def attach_reference_prices(analysis, repository, tickers, *, interval=None):
    """Add reference series to the close panel, on the panel's own grid.

    The execution layer reads its exchange rates out of
    ``analysis.prices["close"]``, so a currency pair that is ingested but
    never reaches that panel is a pair the engine still cannot see. Both
    halves of that have now failed separately: the pairs were named in the
    rate map but not fetched, and then fetched but not loaded.

    They cannot simply join the universe. Currency trades nearly around the
    clock and US shares do not, so a shared hourly panel takes the union of
    two calendars, the shares go NaN outside their session, and every signal
    dies -- the mixed-session failure the universe was narrowed to escape.
    Measured rather than reasoned: loading five pairs that way took the book
    from 1,109 trades a year to zero.

    So they are **reindexed onto the traded grid** and forward-filled. The
    index is untouched, which is the point: the correlations, the regimes and
    every indicator were computed before this runs and cannot be moved by it.

    Forward-filling is right here and would not be in a price panel used for
    signals. A rate is a level that persists between observations -- the last
    quoted rate *is* the rate until the next one -- where a stale share price
    would be an invented bar.
    """
    import dataclasses as _dc

    import pandas as pd

    wanted = [str(t) for t in (tickers or ()) if str(t)]
    if not wanted or analysis is None:
        return analysis

    close = (analysis.prices or {}).get("close")
    if close is None or close.empty:
        return analysis

    missing = [t for t in wanted if t not in close.columns]
    if not missing:
        return analysis

    bar_interval = interval or repository.config.market.interval

    def _load(at):
        try:
            return repository.load_market(tickers=missing, interval=at)
        except Exception as error:  # noqa: BLE001 - no rates is not an outage
            logger.warning(
                "Could not load reference series %s at %s: %s", missing, at, error
            )
            return None

    market = _load(bar_interval)

    # The broker does not quote GBPUSD=X: the currency pairs are vendor
    # symbols and exist only under the vendor's own intervals. Moving the
    # decision feed to capital_1h therefore looked for them somewhere they
    # can never be, and every USD instrument silently converted at a rate of
    # 1.0 -- which for a GBP account holding US shares is not a relabelling,
    # it is dropping the currency move entirely.
    #
    # Falling back is sound here for the same reason the reindex below is:
    # a rate is a level that persists between observations, and it is
    # forward-filled onto the traded grid regardless of the grid it arrived
    # on. This is the third separate way these pairs have failed to reach
    # the engine, so it warns loudly rather than substituting in silence.
    if (market is None or getattr(market, "empty", True)) and capital_prices.\
            is_capital_interval(bar_interval):
        logger.warning(
            "No %s bars for reference series %s -- the broker does not quote "
            "them; falling back to %s", bar_interval, missing,
            REFERENCE_FALLBACK_INTERVAL,
        )
        market = _load(REFERENCE_FALLBACK_INTERVAL)

    if market is None or getattr(market, "empty", True):
        logger.warning(
            "No bars for reference series %s; every rate will fall back to "
            "its configured value", missing,
        )
        return analysis

    working = market.reset_index()
    stamp = "timestamp" if "timestamp" in working.columns else working.columns[0]
    name = "ticker" if "ticker" in working.columns else working.columns[1]
    wide = working.pivot_table(index=stamp, columns=name, values="close")
    wide.index = pd.to_datetime(wide.index, utc=True)

    aligned = wide.reindex(close.index.union(wide.index)).ffill().reindex(close.index)
    attached = close.join(aligned[[c for c in aligned.columns if c in missing]])

    absent = [t for t in missing if t not in attached.columns]
    if absent:
        logger.warning("Reference series still absent after loading: %s", absent)

    prices = dict(analysis.prices)
    prices["close"] = attached
    return _dc.replace(analysis, prices=prices)


def run_pipeline(repository, config: BacktestConfig | None = None) -> PipelineResult:
    """Run the whole pipeline from the Parquet lake.

    The only function in this package that performs I/O. It reads through
    :class:`~cfdinvest.repository.DataRepository`'s public API and writes
    nothing.

    Parameters
    ----------
    repository:
        A :class:`~cfdinvest.repository.DataRepository`.
    config:
        Universe, date range and per-layer settings.

    Returns
    -------
    PipelineResult
    """
    settings = config or BacktestConfig()

    analysis = analysis_frame_from_repository(
        repository,
        settings.tickers,
        settings.series_ids,
        interval=settings.interval,
        start=settings.start,
        end=settings.end,
        config=settings.preprocess,
    )

    if analysis.close.empty:
        logger.warning(
            "No market data for %s in [%s, %s]; the pipeline has nothing to run",
            settings.tickers or "the configured universe",
            settings.start,
            settings.end,
        )

    # After the panels are built, deliberately: attaching before would put
    # the pairs into the correlation and regime computations, and moving
    # those is not what a currency rate is for.
    analysis = attach_reference_prices(
        analysis, repository,
        getattr(settings.signal, "reference_tickers", ()) or (),
        interval=settings.interval,
    )

    return run_from_analysis(analysis, settings)


def pipeline_metadata(result: PipelineResult, config: BacktestConfig) -> dict:
    """Summarise what a run actually covered.

    Recorded because the alternative is inferring it from the numbers later,
    and a metric computed over a different universe or a shorter window is
    not comparable to one that was not.
    """
    analysis = result.analysis
    execution = result.execution

    metadata: dict[str, Any] = {
        "tickers": tuple(analysis.close.columns) if analysis is not None else (),
        "series_ids": analysis.metadata.get("series_ids", ()) if analysis else (),
        "interval": analysis.metadata.get("interval") if analysis else None,
        "requested_start": config.start,
        "requested_end": config.end,
        "use_regimes": config.use_regimes,
    }

    if analysis is not None and len(analysis.calendar):
        metadata["calendar_start"] = analysis.calendar.min()
        metadata["calendar_end"] = analysis.calendar.max()
        metadata["bars"] = len(analysis.calendar)

    if execution is not None:
        metadata["trades"] = len(execution.trades)
        metadata["execution_lag"] = execution.metadata.get("execution_lag")
        metadata["fill_mode"] = execution.metadata.get("fill_mode")

    if result.signals is not None:
        metadata["active_signal_bars"] = result.signals.metadata.get("active_bars")

    return metadata
