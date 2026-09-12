"""The :class:`AnalysisFrame`: everything a signal layer will read.

Preprocessing's single output. Ingestion writes the lake, the repository
reads it, and this module turns one :class:`~cfdinvest.repository.DataBundle`
into aligned, derived panels on a single trading clock.

The signal layer is not built yet, so :attr:`AnalysisFrame.regimes` is a
deliberately empty placeholder carrying only the calendar -- the shape the
macro-regime classifier will fill, so downstream code can already be written
against it.

Nothing here modifies ingestion or the repository; the repository is read
through its public API only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Mapping

import pandas as pd

from cfdinvest.preprocessing import alignment, correlations, returns, volatility
from cfdinvest.schema import (
    MACRO_VALUE,
    PUBLICATION_DATE,
    SERIES_ID,
    TICKER,
    TIMESTAMP,
)

logger = logging.getLogger(__name__)

__all__ = [
    "PreprocessConfig",
    "AnalysisFrame",
    "build_analysis_frame",
    "analysis_frame_from_repository",
    "regime_placeholder",
]


@dataclass(frozen=True, slots=True)
class PreprocessConfig:
    """Window lengths and policy for the preprocessing pass."""

    #: Resample rule (pandas offset alias) or ``None`` to keep native bars.
    resample_rule: str | None = None
    #: Price field used as *the* price for returns and correlations.
    price_field: str = "close"

    #: Lookback for :func:`~cfdinvest.preprocessing.returns.rolling_returns`.
    rolling_return_window: int = 21
    #: Lookback for rolling standard deviation.
    volatility_window: int = 21
    #: Lookback for realised volatility.
    realized_volatility_window: int = 21
    #: Lookback for Average True Range.
    atr_window: int = 14
    #: ATR smoothing, ``"wilder"`` or ``"sma"``.
    atr_method: str = "wilder"
    #: Lookback for the rolling correlation matrix.
    correlation_window: int = 63

    #: Annualisation factor; inferred from bar spacing when ``None``.
    periods_per_year: int | None = None

    #: Trailing window for the correlation-breakdown baseline.
    breakdown_baseline_window: int = 252
    breakdown_zscore_threshold: float = 2.0
    breakdown_level_threshold: float = 0.8
    breakdown_change_window: int = 21
    breakdown_change_threshold: float = 0.25

    #: What to do when macro data carries no publication dates:
    #: ``"error"`` (default) or ``"lag"``. See
    #: :func:`~cfdinvest.preprocessing.alignment.resolve_publication_dates`.
    missing_publication: str = "error"
    #: Assumed release lag when ``missing_publication="lag"``.
    fallback_lag_days: int = 1


def regime_placeholder(calendar: pd.DatetimeIndex) -> pd.DataFrame:
    """Return the empty regime frame the classifier will later populate.

    Carries the calendar and nothing else. Downstream code can join against
    it today and pick up real columns when the signal layer lands, without
    a shape change.
    """
    return pd.DataFrame(index=pd.DatetimeIndex(calendar, name=TIMESTAMP))


@dataclass(frozen=True, slots=True)
class AnalysisFrame:
    """Aligned market and macro data with the standard derived panels.

    Every panel shares one index -- the trading calendar -- and one column
    convention: tickers for market data, ``series_id`` for macro.

    Attributes
    ----------
    prices:
        ``{field: timestamp x ticker}`` for each OHLCV field present.
    returns:
        ``{"simple", "log", "rolling", "overnight"}`` panels. ``overnight``
        is present only when open prices were available.
    volatility:
        ``{"rolling", "realized", "atr"}``. ``atr`` needs highs and lows.
    correlations:
        ``{"rolling"}`` (MultiIndexed matrices), ``{"average"}`` (a Series
        widened to one column) and ``{"breakdown"}`` (diagnostic flags).
    macro:
        Publication-aware forward-filled ``timestamp x series_id``.
    regimes:
        Placeholder; see :func:`regime_placeholder`.
    metadata:
        Provenance: interval, windows, universe, calendar bounds.
    """

    prices: dict[str, pd.DataFrame] = field(default_factory=dict)
    returns: dict[str, pd.DataFrame] = field(default_factory=dict)
    volatility: dict[str, pd.DataFrame] = field(default_factory=dict)
    correlations: dict[str, pd.DataFrame] = field(default_factory=dict)
    macro: pd.DataFrame = field(default_factory=pd.DataFrame)
    #: The same series on a MONTHLY grid indexed by PUBLICATION date, not
    #: reindexed onto the price calendar.
    #:
    #: `macro` above is forward-filled onto the trading grid, which is right
    #: for reading a value beside a bar and wrong for measuring a CHANGE. A
    #: year-over-year on the hourly grid is a shift of 1,764 rows; on the
    #: series' own monthly index it is a shift of 12. The first needs 1,827
    #: bars of price history before it can say anything, so a six-month
    #: window reported the macro regime as `unknown` on every bar -- not
    #: because six months is too little for a day trade, but because the
    #: measurement was denominated in the wrong thing.
    #:
    #: Empty when there is no macro data, like `macro`.
    macro_monthly: pd.DataFrame = field(default_factory=pd.DataFrame)
    regimes: pd.DataFrame = field(default_factory=pd.DataFrame)
    metadata: dict[str, Any] = field(default_factory=dict)

    # -- convenience ------------------------------------------------------- #
    @property
    def calendar(self) -> pd.DatetimeIndex:
        """The single trading clock every panel shares."""
        return pd.DatetimeIndex(self.regimes.index, name=TIMESTAMP)

    @property
    def close(self) -> pd.DataFrame:
        """The primary price panel."""
        return self.prices.get("close", pd.DataFrame())

    @property
    def tickers(self) -> tuple[str, ...]:
        """Universe present in the price panels."""
        return tuple(self.close.columns)

    @property
    def series_ids(self) -> tuple[str, ...]:
        """Macro series present."""
        return tuple(self.macro.columns)

    def describe(self) -> pd.DataFrame:
        """Row counts and coverage of every panel, for a sanity check."""
        rows = []
        for group, panels in (
            ("prices", self.prices),
            ("returns", self.returns),
            ("volatility", self.volatility),
        ):
            for name, panel in panels.items():
                rows.append(
                    {
                        "panel": f"{group}.{name}",
                        "rows": len(panel),
                        "columns": panel.shape[1],
                        "populated": int(panel.notna().sum().sum()),
                    }
                )
        rows.append(
            {
                "panel": "macro",
                "rows": len(self.macro),
                "columns": self.macro.shape[1],
                "populated": int(self.macro.notna().sum().sum()),
            }
        )
        return pd.DataFrame(rows).set_index("panel")


# --------------------------------------------------------------------------- #
# Builder
# --------------------------------------------------------------------------- #
def build_analysis_frame(
    market: pd.DataFrame,
    macro_long: pd.DataFrame | None = None,
    *,
    interval: str = "1d",
    config: PreprocessConfig | None = None,
) -> AnalysisFrame:
    """Build an :class:`AnalysisFrame` from tidy market and stacked macro data.

    Pure: takes data, returns data, performs no I/O. This is the function to
    unit-test and the one a caller with in-memory frames should use.

    Parameters
    ----------
    market:
        Tidy ``(timestamp, ticker)`` OHLCV, as
        ``DataRepository.load_market()`` returns.
    macro_long:
        Stacked macro with ``publication_date``, as
        ``DataRepository.load_macro(wide=False)`` returns. ``None`` yields an
        empty macro panel.
    interval:
        Bar interval label, recorded in metadata and used to annualise when
        ``config.periods_per_year`` is unset.
    config:
        Windows and policy.

    Returns
    -------
    AnalysisFrame
    """
    settings = config or PreprocessConfig()

    panels, calendar = alignment.align_market(market, rule=settings.resample_rule)
    price_field = settings.price_field
    prices = panels.get(price_field, pd.DataFrame(index=calendar))

    annualisation = settings.periods_per_year
    if annualisation is None:
        annualisation = alignment.periods_per_year(
            calendar if len(calendar) > 1 else interval
        )

    return_panels = _build_returns(panels, prices, settings)
    volatility_panels = _build_volatility(
        panels, return_panels.get("simple"), settings, annualisation
    )
    correlation_panels = _build_correlations(return_panels.get("simple"), settings)

    macro_panel = _build_macro(macro_long, calendar, settings)
    macro_monthly = _build_macro_monthly(macro_long, settings)

    metadata = {
        "interval": interval,
        "resample_rule": settings.resample_rule,
        "price_field": price_field,
        "periods_per_year": annualisation,
        "calendar_start": calendar.min() if len(calendar) else None,
        "calendar_end": calendar.max() if len(calendar) else None,
        "bars": len(calendar),
        "tickers": tuple(prices.columns),
        "series_ids": tuple(macro_panel.columns),
        "fields": tuple(panels),
        "windows": {
            "rolling_return": settings.rolling_return_window,
            "volatility": settings.volatility_window,
            "realized_volatility": settings.realized_volatility_window,
            "atr": settings.atr_window,
            "correlation": settings.correlation_window,
        },
    }

    return AnalysisFrame(
        prices=panels,
        returns=return_panels,
        volatility=volatility_panels,
        correlations=correlation_panels,
        macro=macro_panel,
        macro_monthly=macro_monthly,
        regimes=regime_placeholder(calendar),
        metadata=metadata,
    )


def _build_returns(
    panels: Mapping[str, pd.DataFrame],
    prices: pd.DataFrame,
    settings: PreprocessConfig,
) -> dict[str, pd.DataFrame]:
    """Assemble the return panels available from the fields present."""
    if prices.empty:
        return {}

    built = {
        "simple": returns.simple_returns(prices),
        "log": returns.log_returns(prices),
        "rolling": returns.rolling_returns(prices, settings.rolling_return_window),
    }

    opens = panels.get("open")
    closes = panels.get("close")
    if opens is not None and closes is not None and not opens.empty:
        built["overnight"] = returns.overnight_returns(opens, closes)
    else:
        logger.debug("No open prices; skipping overnight returns")
    return built


def _build_volatility(
    panels: Mapping[str, pd.DataFrame],
    simple: pd.DataFrame | None,
    settings: PreprocessConfig,
    annualisation: int,
) -> dict[str, pd.DataFrame]:
    """Assemble the volatility panels available from the fields present."""
    built: dict[str, pd.DataFrame] = {}
    if simple is not None and not simple.empty:
        built["rolling"] = volatility.rolling_volatility(
            simple, settings.volatility_window, periods=annualisation
        )
        built["realized"] = volatility.realized_volatility(
            simple, settings.realized_volatility_window, periods=annualisation
        )

    high, low, close = panels.get("high"), panels.get("low"), panels.get("close")
    if high is not None and low is not None and close is not None:
        built["atr"] = volatility.average_true_range(
            high, low, close, settings.atr_window, method=settings.atr_method
        )
    else:
        logger.debug("No high/low panels; skipping ATR")
    return built


def _build_correlations(
    simple: pd.DataFrame | None, settings: PreprocessConfig
) -> dict[str, pd.DataFrame]:
    """Assemble rolling correlations and their breakdown diagnostics."""
    if simple is None or simple.empty or simple.shape[1] < 2:
        logger.debug("Fewer than two assets; skipping correlations")
        return {}

    rolling = correlations.rolling_correlation(simple, settings.correlation_window)
    average = correlations.average_pairwise_correlation(rolling)
    flags = correlations.correlation_breakdown_flags(
        average,
        baseline_window=settings.breakdown_baseline_window,
        zscore_threshold=settings.breakdown_zscore_threshold,
        level_threshold=settings.breakdown_level_threshold,
        change_window=settings.breakdown_change_window,
        change_threshold=settings.breakdown_change_threshold,
    )
    return {
        "rolling": rolling,
        "average": average.to_frame(),
        "breakdown": flags,
    }


def _build_macro_monthly(macro_long, settings) -> "pd.DataFrame":
    """The macro series on a MONTHLY grid, indexed by PUBLICATION date.

    Two things had to be right and the first attempt got both wrong.

    **Monthly, not "native".** The first version pivoted on the observation
    dates, which is the UNION of every series' dates -- and DFF, DGS10 and
    VIXCLS are daily, so the index came out daily with 13,403 rows. INDPRO's
    439 monthly values then sat scattered among them, and a twelve-row shift
    moved back twelve DAYS onto a NaN. The classifier returned `unknown` for
    every row and the fix looked like it had changed nothing.

    **Publication dates, not observation dates.** A figure for August is not
    knowable on 1 September. Indexing by when it was PUBLISHED is what makes
    carrying it forward onto later bars honest, and it is the same question
    `publication_aware_ffill` answers for the bar panel. Without it this
    would be a quiet lookahead in the one place nobody would check.

    Forward-filled across the monthly grid so a month with no publication
    carries the last known level, which is what a level-based classifier
    needs and what a reader would assume anyway.
    """
    import pandas as pd

    from cfdinvest.preprocessing import alignment

    if macro_long is None or getattr(macro_long, "empty", True):
        return pd.DataFrame()
    if SERIES_ID not in macro_long.columns or MACRO_VALUE not in macro_long.columns:
        return pd.DataFrame()

    published = alignment.resolve_publication_dates(
        macro_long,
        missing=settings.missing_publication,
        fallback_lag_days=settings.fallback_lag_days,
    )
    frame = macro_long.assign(**{PUBLICATION_DATE: published})
    frame = frame[frame[PUBLICATION_DATE].notna() & frame[MACRO_VALUE].notna()]
    if frame.empty:
        return pd.DataFrame()

    wide = frame.pivot_table(
        index=PUBLICATION_DATE, columns=SERIES_ID, values=MACRO_VALUE,
        aggfunc="last",
    ).sort_index()
    wide.index = pd.DatetimeIndex(wide.index)
    if wide.index.tz is None:
        wide.index = wide.index.tz_localize("UTC")

    monthly = wide.resample("ME").last().ffill()
    monthly.columns.name = SERIES_ID
    return monthly


def _build_macro(
    macro_long: pd.DataFrame | None,
    calendar: pd.DatetimeIndex,
    settings: PreprocessConfig,
) -> pd.DataFrame:
    """Forward-fill macro onto the trading calendar, publication-aware."""
    if macro_long is None or macro_long.empty:
        empty = pd.DataFrame(index=pd.DatetimeIndex(calendar, name=TIMESTAMP))
        empty.columns.name = SERIES_ID
        return empty

    return alignment.align_macro(
        macro_long,
        calendar,
        missing_publication=settings.missing_publication,
        fallback_lag_days=settings.fallback_lag_days,
    )


def analysis_frame_from_repository(
    repository,
    tickers=None,
    series_ids=None,
    *,
    interval: str | None = None,
    start=None,
    end=None,
    config: PreprocessConfig | None = None,
) -> AnalysisFrame:
    """Load from the lake and preprocess in one call.

    The only function in this package that performs I/O. It reads through
    :class:`~cfdinvest.repository.DataRepository`'s public API and modifies
    nothing.

    Parameters
    ----------
    repository:
        A :class:`~cfdinvest.repository.DataRepository`.
    tickers, series_ids, interval, start, end:
        Passed straight through to the repository.
    config:
        Preprocessing settings.

    Returns
    -------
    AnalysisFrame
    """
    bar_interval = interval or repository.config.market.interval
    market = repository.load_market(
        tickers, interval=bar_interval, start=start, end=end
    )
    # **Macro history is NOT cut to the price window, and `macro_native` is
    # why this now matters.**
    #
    # An earlier attempt at this was reverted on 2026-09-12 because it
    # changed nothing: `macro` is reindexed onto the price calendar, so
    # loading more history simply produced rows that were then thrown away.
    # `macro_native` keeps the series on their OWN dates, so the extra
    # history survives -- and a year-over-year on a monthly series needs
    # twelve observations regardless of how many price bars the run covers.
    #
    # Widening only BACKWARDS cannot leak anything forwards: `end` is
    # untouched and the publication-aware alignment still decides what was
    # knowable when. Twelve monthly series cost nothing beside 55
    # instruments of hourly bars.
    #
    # A full-span run already starts at the earliest data, so its panels are
    # unchanged and no recorded figure moves. Only windowed runs differ,
    # which is the case it exists for.
    macro_long = repository.load_macro(series_ids, start=None, end=end, wide=False)

    frame = build_analysis_frame(
        market, macro_long, interval=bar_interval, config=config
    )
    frame.metadata["data_root"] = str(repository.paths.data_root)
    frame.metadata["vintage_modes"] = repository.macro_vintage_modes()
    return frame
