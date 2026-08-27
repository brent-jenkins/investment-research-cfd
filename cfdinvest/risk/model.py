"""Portfolio risk, computed from what the pipeline already wrote.

**Nothing here drives the pipeline and nothing recomputes a position.** Every
figure comes from a persisted artefact: positions from ``positions.parquet``,
prices from the stored analysis panels, the decomposition from
``pnl.parquet``, classifications from ``regime.parquet``. If an artefact is
absent the metric is reported absent, never inferred — a dashboard that
silently reconstructs shows numbers no run ever produced.

**Two scopes, two limit sets, deliberately not merged.**
:class:`~cfdinvest.livehelper.config.LiveHelperConfig` bounds *one trade*
before you place it; :class:`RiskLimits` bounds *the book* once trades are on.
They answer different questions — "may I open this?" against "how much am I
carrying?" — and a single `max_exposure` that meant both would be wrong for
one of them.

Every function is pure, deterministic and offline.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Mapping

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

__all__ = [
    "RiskLimits",
    "RiskSnapshot",
    "compute_current_risk",
    "compute_regime_risk",
    "compute_recent_trade_risk",
    "compute_pnl_risk",
    "build_risk_snapshot",
]


@dataclass(frozen=True, slots=True)
class RiskLimits:
    """Bounds on the **book**, not on a single trade.

    Attributes
    ----------
    account_balance_gbp:
        What the account is worth now. Leverage and margin usage are
        meaningless without it, so it is required rather than inferred.
    max_total_exposure_gbp:
        Gross notional across every open position.
    max_exposure_per_instrument_gbp:
        The most any one instrument may carry — the concentration bound.
    max_effective_leverage:
        Gross exposure over balance.
    max_margin_usage_pct:
        Margin committed as a share of balance. Trading 212 force-closes on a
        margin ratio without warning, so headroom here is not a nicety.
    max_fx_exposure_pct:
        Share of the book in any one non-account currency.
    max_instrument_concentration_pct:
        Share of gross exposure in a single instrument. A book inside every
        absolute limit can still be one position.
    max_drawdown_pct:
        Drawdown past which the path matters more than the endpoint.
    """

    account_balance_gbp: float = 2_000.0
    used_margin_gbp: float = 0.0

    max_total_exposure_gbp: float = 3_000.0
    max_exposure_per_instrument_gbp: float = 1_500.0
    max_effective_leverage: float = 15.0
    max_margin_usage_pct: float = 0.50
    max_fx_exposure_pct: float = 0.30
    max_instrument_concentration_pct: float = 0.40
    max_drawdown_pct: float = 0.20

    def __post_init__(self) -> None:
        if self.account_balance_gbp <= 0:
            raise ValueError(
                f"account_balance_gbp must be > 0, got {self.account_balance_gbp}"
            )
        for name in (
            "max_margin_usage_pct",
            "max_fx_exposure_pct",
            "max_instrument_concentration_pct",
            "max_drawdown_pct",
        ):
            value = getattr(self, name)
            if not 0 < value <= 1:
                raise ValueError(
                    f"{name} is a fraction in (0, 1], got {value}; "
                    f"did you mean {value / 100}?"
                )


@dataclass(frozen=True, slots=True)
class RiskSnapshot:
    """The book's risk at one moment, and what is wrong with it."""

    as_of: pd.Timestamp | None = None
    run_id: str | None = None
    #: Which book the exposure describes: ``"confirmed"`` for trades you
    #: recorded placing, ``"paper:<arm>"`` for the forward simulation, and
    #: ``None`` when no book could be read. Never a backtest — its positions
    #: are the simulation's compounded equity and comparing them to a real
    #: account's limits produces a breach every day and means nothing.
    book: str | None = None
    exposure_metrics: dict[str, Any] = field(default_factory=dict)
    leverage_metrics: dict[str, Any] = field(default_factory=dict)
    margin_metrics: dict[str, Any] = field(default_factory=dict)
    fx_metrics: dict[str, Any] = field(default_factory=dict)
    regime_metrics: dict[str, Any] = field(default_factory=dict)
    trade_risk_metrics: dict[str, Any] = field(default_factory=dict)
    pnl_risk_metrics: dict[str, Any] = field(default_factory=dict)
    scheduler_status: dict[str, Any] = field(default_factory=dict)
    flags: tuple[str, ...] = ()
    unavailable: tuple[str, ...] = ()
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def severity(self) -> str:
        """``"critical"`` when a limit is breached, else warning or ok.

        An artefact that could not be read counts as a warning, not as
        nothing: "no limit was breached" and "no limit could be checked" are
        different statements.
        """
        if self.flags:
            return "critical"
        if self.unavailable:
            return "warning"
        return "ok"

    @property
    def ok(self) -> bool:
        return not self.flags


def _finite(value: Any) -> bool:
    try:
        return value is not None and np.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _latest_prices(analysis_frame: pd.DataFrame | None) -> pd.Series:
    """Closing price per instrument, from the stored analysis panels.

    The persisted analysis frame is long — ``timestamp, panel, series,
    value`` — so the close panel is a filter and a pivot rather than a
    recomputation. Empty when the run did not store one, which makes every
    exposure figure absent rather than guessed.
    """
    if analysis_frame is None or getattr(analysis_frame, "empty", True):
        return pd.Series(dtype="float64")
    if not {"panel", "series", "value"} <= set(analysis_frame.columns):
        return pd.Series(dtype="float64")

    closes = analysis_frame[analysis_frame["panel"] == "prices.close"]
    if closes.empty:
        return pd.Series(dtype="float64")

    ordered = closes.sort_values("timestamp")
    return ordered.groupby("series")["value"].last()


# --------------------------------------------------------------------------- #
# Exposure, leverage, margin, currency
# --------------------------------------------------------------------------- #
def compute_current_risk(
    positions_frame: pd.DataFrame | None,
    limits: RiskLimits | None = None,
    *,
    prices: pd.Series | None = None,
    analysis_frame: pd.DataFrame | None = None,
    instrument_currencies: Mapping[str, str] | None = None,
    instrument_leverage: Mapping[str, float] | None = None,
    asset_classes: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """What the book is carrying, measured against the limits.

    Exposure needs a price, and prices are not in ``positions.parquet``.
    Supply them directly, or pass the stored analysis frame to have the close
    panel read out of it. With neither, position *units* are still reported
    and everything denominated in money is reported absent.
    """
    bounds = limits or RiskLimits()
    marks = prices if prices is not None else _latest_prices(analysis_frame)
    unavailable: list[str] = []

    empty = {
        "as_of": None,
        "open_positions": 0,
        "units": {},
        "exposure_by_instrument": {},
        "exposure_by_asset_class": {},
        "gross_exposure_gbp": None,
        "net_exposure_gbp": None,
        "largest_instrument": None,
        "largest_instrument_share": None,
        "unavailable": ("positions",),
    }
    if positions_frame is None or len(positions_frame.index) == 0:
        return {**empty, "flat": True}

    frame = positions_frame.copy()
    if "timestamp" in frame.columns:
        frame = frame.set_index("timestamp")
    frame.index = pd.to_datetime(frame.index, utc=True, errors="coerce")

    as_of = frame.index.max()

    # A row with no instruments in it is a book that was read and holds
    # nothing -- which is not the same as no book at all, and reporting it as
    # "positions could not be checked" says nobody knows what is held when
    # somebody does. The paper book's raised arms are legitimately empty for
    # days at a time; every one of those days would have been a warning.
    if frame.shape[1] == 0:
        return {**empty, "as_of": as_of, "flat": True, "unavailable": ()}
    held = frame.loc[as_of]
    held = held[pd.to_numeric(held, errors="coerce").fillna(0.0) != 0.0]

    if held.empty:
        return {
            **empty,
            "as_of": as_of,
            "flat": True,
            "unavailable": (),
        }

    units = {str(k): float(v) for k, v in held.items()}

    if marks is None or len(marks) == 0:
        unavailable.append("prices")
        return {
            **empty,
            "as_of": as_of,
            "flat": False,
            "open_positions": len(units),
            "units": units,
            "unavailable": tuple(unavailable),
        }

    exposure = {}
    signed = {}
    for name, quantity in units.items():
        price = marks.get(name)
        if not _finite(price):
            unavailable.append(f"price:{name}")
            continue
        exposure[name] = abs(quantity * float(price))
        signed[name] = quantity * float(price)

    gross = float(sum(exposure.values())) if exposure else None
    net = float(sum(signed.values())) if signed else None

    classes: dict[str, float] = {}
    for name, value in exposure.items():
        classes.setdefault(_asset_class(name, asset_classes), 0.0)
        classes[_asset_class(name, asset_classes)] += value

    largest = max(exposure, key=exposure.get) if exposure else None
    share = (
        exposure[largest] / gross if largest and gross and gross > 0 else None
    )

    return {
        "as_of": as_of,
        "flat": False,
        "open_positions": len(units),
        "units": units,
        "exposure_by_instrument": exposure,
        "exposure_by_asset_class": classes,
        "gross_exposure_gbp": gross,
        "net_exposure_gbp": net,
        "largest_instrument": largest,
        "largest_instrument_share": share,
        "limit_total": bounds.max_total_exposure_gbp,
        "limit_per_instrument": bounds.max_exposure_per_instrument_gbp,
        "unavailable": tuple(dict.fromkeys(unavailable)),
    }


def _asset_class(ticker: str, mapping: Mapping[str, str] | None) -> str:
    """The asset class for an instrument, via the session calendar's map."""
    if mapping and ticker in mapping:
        return str(mapping[ticker])
    try:
        from cfdinvest.schedule.market_calendar import asset_class_of

        return asset_class_of(ticker)
    except Exception:  # noqa: BLE001
        return "other"


def compute_leverage_and_margin(
    exposure: Mapping[str, Any],
    limits: RiskLimits | None = None,
    *,
    instrument_leverage: Mapping[str, float] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Effective leverage, and margin usage where leverage is known.

    Instrument leverage is set by Trading 212 per instrument and is not
    published in machine-readable form, so margin is computable only for the
    instruments you have supplied it for. What cannot be computed is reported
    absent rather than assumed — an assumed leverage produces a margin figure
    that is confidently wrong in whichever direction the assumption leaned.
    """
    bounds = limits or RiskLimits()
    gross = exposure.get("gross_exposure_gbp")

    leverage = {
        "effective_leverage": (
            gross / bounds.account_balance_gbp if _finite(gross) else None
        ),
        "limit": bounds.max_effective_leverage,
        "account_balance_gbp": bounds.account_balance_gbp,
    }

    per_instrument = exposure.get("exposure_by_instrument") or {}
    known = instrument_leverage or {}
    margins: dict[str, float] = {}
    unknown: list[str] = []

    for name, value in per_instrument.items():
        rate = known.get(name)
        if _finite(rate) and float(rate) > 0:
            margins[name] = value / float(rate)
        else:
            unknown.append(name)

    total_margin = float(sum(margins.values())) if margins else None
    complete = bool(margins) and not unknown

    margin = {
        "margin_by_instrument": margins,
        "margin_required_gbp": total_margin,
        "used_margin_gbp": bounds.used_margin_gbp,
        "margin_usage_pct": (
            (bounds.used_margin_gbp + total_margin) / bounds.account_balance_gbp
            if _finite(total_margin)
            else None
        ),
        "limit": bounds.max_margin_usage_pct,
        "complete": complete,
        "unknown_leverage": tuple(unknown),
    }
    return leverage, margin


def compute_fx_risk(
    exposure: Mapping[str, Any],
    limits: RiskLimits | None = None,
    *,
    instrument_currencies: Mapping[str, str] | None = None,
    account_currency: str = "GBP",
) -> dict[str, Any]:
    """Exposure grouped by the currency it is quoted in.

    Anything unmapped is treated as the account currency, which is the safe
    direction: it understates foreign exposure visibly rather than inventing
    it. The mapping is reported so the understatement is checkable.
    """
    bounds = limits or RiskLimits()
    per_instrument = exposure.get("exposure_by_instrument") or {}
    mapping = dict(instrument_currencies or {})

    by_currency: dict[str, float] = {}
    for name, value in per_instrument.items():
        currency = str(mapping.get(name, account_currency)).upper()
        by_currency[currency] = by_currency.get(currency, 0.0) + value

    foreign = {
        currency: value
        for currency, value in by_currency.items()
        if currency != account_currency.upper()
    }
    balance = bounds.account_balance_gbp

    return {
        "account_currency": account_currency.upper(),
        "exposure_by_currency": by_currency,
        "foreign_exposure_gbp": float(sum(foreign.values())),
        "foreign_share_of_account": (
            float(sum(foreign.values())) / balance if balance > 0 else None
        ),
        "share_by_currency": {
            currency: value / balance for currency, value in by_currency.items()
        }
        if balance > 0
        else {},
        "limit": bounds.max_fx_exposure_pct,
        "mapped_instruments": len(
            [n for n in per_instrument if n in mapping]
        ),
        "unmapped_instruments": tuple(
            n for n in per_instrument if n not in mapping
        ),
    }


# --------------------------------------------------------------------------- #
# Regimes
# --------------------------------------------------------------------------- #
#: The classifiers a RegimeFrame persists, in the order they are shown.
REGIME_COLUMNS: tuple[str, ...] = (
    "volatility_regime",
    "trend_regime",
    "correlation_regime",
    "macro_regime",
    "combined_regime",
)

#: Regimes that mean "hold less". Not a rule — a flag on the dashboard, since
#: a crisis is also where the opportunity is.
ELEVATED_REGIMES: frozenset[str] = frozenset(
    {"crisis", "stagflation", "risk_off", "high", "breakdown", "contraction"}
)


def compute_regime_risk(regime_frame: pd.DataFrame | None) -> dict[str, Any]:
    """The latest classification from each classifier.

    Market-level: one label per bar, not per instrument. A classifier the run
    did not compute is absent rather than ``"unknown"``, because those are
    different — one means "not measured", the other means "measured and
    inconclusive", and the macro classifier genuinely returns the latter.
    """
    empty = {
        "as_of": None,
        "regimes": {},
        "elevated": (),
        "available": False,
    }
    if regime_frame is None or getattr(regime_frame, "empty", True):
        return empty

    frame = regime_frame.copy()
    if "timestamp" in frame.columns:
        frame["timestamp"] = pd.to_datetime(
            frame["timestamp"], utc=True, errors="coerce"
        )
        frame = frame.sort_values("timestamp")
        as_of = frame["timestamp"].iloc[-1]
    else:
        as_of = None

    latest = frame.iloc[-1]
    regimes = {
        column: str(latest[column])
        for column in REGIME_COLUMNS
        if column in frame.columns and pd.notna(latest[column])
    }
    elevated = tuple(
        column for column, label in regimes.items()
        if label.lower() in ELEVATED_REGIMES
    )

    return {
        "as_of": as_of,
        "regimes": regimes,
        "elevated": elevated,
        "available": bool(regimes),
        "history_bars": int(len(frame)),
    }


# --------------------------------------------------------------------------- #
# Recent trades, from the live helper's own log
# --------------------------------------------------------------------------- #
#: What each live-helper check is called on the dashboard. The helper's own
#: sentences explain one trade; these name a recurring problem across many.
CHECK_LABELS: dict[str, str] = {
    "risk": "Risk per trade over the limit",
    "exposure": "Exposure over the limit",
    "leverage": "Effective leverage over the limit",
    "margin": "Margin usage over the limit",
    "fx": "Currency exposure over the limit",
    "stop_loss": "No stop-loss set",
    "stop_side": "Stop on the wrong side of the entry",
    "target_side": "Take-profit on the wrong side of the entry",
    "fx_rate": "No exchange rate supplied",
    "price": "No price supplied",
    "close_only": "Instrument is close-only",
}


def compute_recent_trade_risk(
    assessments_frame: pd.DataFrame | None, *, recent: int = 50
) -> dict[str, Any]:
    """What the live helper has been saying about the trades you considered.

    Only populated where assessments have been recorded — the helper writes
    them with ``--record``. Absent otherwise, which is honest: no record is
    not the same as no unsafe trades.
    """
    empty = {
        "available": False,
        "assessed": 0,
        "counts": {},
        "unsafe_reasons": {},
        "average_risk_gbp": None,
        "average_effective_leverage": None,
        "margin_usage": {},
    }
    if assessments_frame is None or getattr(assessments_frame, "empty", True):
        return empty

    frame = assessments_frame.copy()
    if "timestamp" in frame.columns:
        frame["timestamp"] = pd.to_datetime(
            frame["timestamp"], utc=True, errors="coerce"
        )
        frame = frame.sort_values("timestamp").tail(recent)

    counts = (
        frame["status"].astype(str).value_counts().to_dict()
        if "status" in frame.columns
        else {}
    )

    # Grouped by which check failed, not by the sentence it printed: the
    # sentences carry amounts, so "Exposure £4,000" and "Exposure £3,000"
    # would count as two different problems rather than one recurring one.
    reasons: dict[str, int] = {}
    column = (
        "critical_checks" if "critical_checks" in frame.columns else None
    )
    if column and "status" in frame.columns:
        unsafe = frame[frame["status"].astype(str) == "UNSAFE"]
        for entry in unsafe[column]:
            for check in _as_list(entry):
                label = CHECK_LABELS.get(str(check), str(check).replace("_", " "))
                reasons[label] = reasons.get(label, 0) + 1

    def mean(column: str) -> float | None:
        if column not in frame.columns:
            return None
        values = pd.to_numeric(frame[column], errors="coerce").dropna()
        return float(values.mean()) if len(values) else None

    usage = {}
    if "margin_usage_pct_after_trade" in frame.columns:
        values = pd.to_numeric(
            frame["margin_usage_pct_after_trade"], errors="coerce"
        ).dropna()
        if len(values):
            usage = {
                "median": float(values.median()),
                "p90": float(values.quantile(0.90)),
                "max": float(values.max()),
            }

    return {
        "available": True,
        "assessed": int(len(frame)),
        "counts": {str(k): int(v) for k, v in counts.items()},
        "unsafe_reasons": dict(
            sorted(reasons.items(), key=lambda pair: -pair[1])[:5]
        ),
        "average_risk_gbp": mean("risk_gbp"),
        "average_effective_leverage": mean("leverage"),
        "margin_usage": usage,
    }


def _as_list(value: Any) -> list:
    """Coerce a stored reasons cell into a list."""
    if isinstance(value, (list, tuple, np.ndarray)):
        return list(value)
    if value in (None, "") or (isinstance(value, float) and np.isnan(value)):
        return []
    return [value]


# --------------------------------------------------------------------------- #
# Profit and loss
# --------------------------------------------------------------------------- #
def compute_pnl_risk(
    pnl_frame: pd.DataFrame | None,
    limits: RiskLimits | None = None,
    *,
    recent_bars: int = 21,
    initial_equity: float | None = None,
) -> dict[str, Any]:
    """Drawdown, and what the money came from.

    Reads the engine's own decomposition rather than reconstructing it, so
    the overnight gap is separable from the intraday move. **That split is
    the risk finding**: a strategy whose profit arrives overnight is carrying
    gap risk a stop cannot bound, and one whose losses arrive overnight is
    being taken out by moves it was never in the market for.
    """
    bounds = limits or RiskLimits()
    empty = {
        "available": False,
        "total_pnl": None,
        "gap_pnl": None,
        "intraday_pnl": None,
        "overnight_share": None,
        "recent_pnl": None,
        "max_drawdown": None,
        "current_drawdown": None,
        "bars": 0,
    }
    if pnl_frame is None or getattr(pnl_frame, "empty", True):
        return empty
    if "total" not in pnl_frame.columns:
        return empty

    frame = pnl_frame.copy()
    if "timestamp" in frame.columns:
        frame["timestamp"] = pd.to_datetime(
            frame["timestamp"], utc=True, errors="coerce"
        )
        frame = frame.sort_values("timestamp").set_index("timestamp")

    total = pd.to_numeric(frame["total"], errors="coerce").fillna(0.0)

    # The base is the equity the *run* started with, not the balance you hold
    # today. Dividing a run's profit and loss by a different account rescales
    # the drawdown fraction into something that describes neither -- a run
    # simulated at 100,000 measured against a 2,000 balance reads as a 75%
    # drawdown when the run itself never lost 3%.
    base = initial_equity if _finite(initial_equity) and initial_equity > 0 else (
        bounds.account_balance_gbp
    )
    equity = base + total.cumsum()
    peak = equity.cummax()
    drawdown = (equity - peak) / peak.where(peak > 0)

    def column_sum(name: str) -> float | None:
        if name not in frame.columns:
            return None
        return float(pd.to_numeric(frame[name], errors="coerce").fillna(0.0).sum())

    gap, intraday = column_sum("gap"), column_sum("intraday")
    market = None
    if gap is not None and intraday is not None:
        market = abs(gap) + abs(intraday)

    return {
        "available": True,
        "total_pnl": float(total.sum()),
        "gap_pnl": gap,
        "intraday_pnl": intraday,
        "overnight_share": (
            abs(gap) / market if gap is not None and market else None
        ),
        "recent_pnl": float(total.tail(recent_bars).sum()),
        "recent_bars": int(min(recent_bars, len(total))),
        "max_drawdown": float(drawdown.min()) if len(drawdown) else None,
        "current_drawdown": float(drawdown.iloc[-1]) if len(drawdown) else None,
        "limit_drawdown": bounds.max_drawdown_pct,
        "initial_equity": float(base),
        "bars": int(len(total)),
    }


# --------------------------------------------------------------------------- #
# The snapshot
# --------------------------------------------------------------------------- #
def _flags(
    exposure: dict, leverage: dict, margin: dict, fx: dict, pnl: dict,
    limits: RiskLimits,
) -> tuple[list[str], list[str]]:
    """Every breached limit, and everything that could not be checked."""
    flags: list[str] = []
    unavailable: list[str] = []

    gross = exposure.get("gross_exposure_gbp")
    if _finite(gross):
        if gross > limits.max_total_exposure_gbp:
            flags.append(
                f"Gross exposure £{gross:,.0f} is over the "
                f"£{limits.max_total_exposure_gbp:,.0f} limit."
            )
        for name, value in (exposure.get("exposure_by_instrument") or {}).items():
            if value > limits.max_exposure_per_instrument_gbp:
                flags.append(
                    f"{name} carries £{value:,.0f}, over the "
                    f"£{limits.max_exposure_per_instrument_gbp:,.0f} "
                    "per-instrument limit."
                )
        share = exposure.get("largest_instrument_share")
        if _finite(share) and share > limits.max_instrument_concentration_pct:
            flags.append(
                f"{exposure.get('largest_instrument')} is {share:.0%} of the "
                f"book, over the {limits.max_instrument_concentration_pct:.0%} "
                "concentration limit."
            )
    elif not exposure.get("flat"):
        unavailable.append("exposure")

    effective = leverage.get("effective_leverage")
    if _finite(effective):
        if effective > limits.max_effective_leverage:
            flags.append(
                f"Effective leverage {effective:,.1f}x is over the "
                f"{limits.max_effective_leverage:,.0f}x limit."
            )
    elif not exposure.get("flat"):
        unavailable.append("leverage")

    usage = margin.get("margin_usage_pct")
    if _finite(usage):
        if usage > limits.max_margin_usage_pct:
            flags.append(
                f"Margin usage {usage:.0%} is over the "
                f"{limits.max_margin_usage_pct:.0%} limit."
            )
        if not margin.get("complete"):
            unavailable.append("margin (partial)")
    elif not exposure.get("flat"):
        unavailable.append("margin")

    foreign = fx.get("foreign_share_of_account")
    if _finite(foreign) and foreign > limits.max_fx_exposure_pct:
        flags.append(
            f"Currency exposure {foreign:.0%} is over the "
            f"{limits.max_fx_exposure_pct:.0%} limit."
        )
    if fx.get("unmapped_instruments") and (exposure.get("exposure_by_instrument")):
        unavailable.append("currency map (partial)")

    current = pnl.get("current_drawdown")
    if _finite(current) and abs(current) > limits.max_drawdown_pct:
        flags.append(
            f"Current drawdown {abs(current):.0%} is past the "
            f"{limits.max_drawdown_pct:.0%} limit."
        )
    if not pnl.get("available"):
        unavailable.append("profit and loss")

    return flags, unavailable


def build_risk_snapshot(
    *,
    positions_frame=None,
    prices=None,
    book: str | None = None,
    regime_frame=None,
    pnl_frame=None,
    analysis_frame=None,
    assessments_frame=None,
    scheduler_status: Mapping[str, Any] | None = None,
    limits: RiskLimits | None = None,
    run_id: str | None = None,
    run_initial_equity: float | None = None,
    instrument_currencies: Mapping[str, str] | None = None,
    instrument_leverage: Mapping[str, float] | None = None,
    asset_classes: Mapping[str, str] | None = None,
    now: pd.Timestamp | None = None,
) -> RiskSnapshot:
    """Everything above, assembled into one snapshot.

    Pure with respect to its inputs: it reads frames and returns a value. The
    caller does the reading, so a test can assemble any combination of
    missing artefacts without a filesystem.
    """
    bounds = limits or RiskLimits()

    exposure = compute_current_risk(
        positions_frame,
        bounds,
        prices=prices,
        analysis_frame=analysis_frame,
        instrument_currencies=instrument_currencies,
        asset_classes=asset_classes,
    )
    leverage, margin = compute_leverage_and_margin(
        exposure, bounds, instrument_leverage=instrument_leverage
    )
    fx = compute_fx_risk(
        exposure, bounds, instrument_currencies=instrument_currencies
    )
    regimes = compute_regime_risk(regime_frame)
    trades = compute_recent_trade_risk(assessments_frame)
    pnl = compute_pnl_risk(pnl_frame, bounds, initial_equity=run_initial_equity)

    flags, unavailable = _flags(exposure, leverage, margin, fx, pnl, bounds)
    unavailable.extend(exposure.get("unavailable") or ())
    if not regimes.get("available"):
        unavailable.append("regimes")

    if flags:
        message = f"{len(flags)} limit breached" + ("" if len(flags) == 1 else "es")
        message = f"{len(flags)} limit{'s' if len(flags) > 1 else ''} breached."
    elif unavailable:
        message = (
            "No limit breached, but "
            f"{len(set(unavailable))} thing(s) could not be checked."
        )
    elif exposure.get("flat"):
        message = "The book is flat."
    else:
        message = "Inside every limit."

    return RiskSnapshot(
        as_of=exposure.get("as_of")
        or (pd.Timestamp(now) if now is not None else None),
        run_id=run_id,
        book=book,
        exposure_metrics=exposure,
        leverage_metrics=leverage,
        margin_metrics=margin,
        fx_metrics=fx,
        regime_metrics=regimes,
        trade_risk_metrics=trades,
        pnl_risk_metrics=pnl,
        scheduler_status=dict(scheduler_status or {}),
        flags=tuple(flags),
        unavailable=tuple(dict.fromkeys(unavailable)),
        message=message,
        details={"limits": bounds.__class__.__name__},
    )
