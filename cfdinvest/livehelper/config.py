"""The limits a trade is judged against.

Every threshold here is *yours*, not the broker's. Trading 212 will happily
let you open a position that puts 80% of the account at risk; these are the
bounds you have decided to trade inside, and the helper's whole job is to
tell you when an intended trade falls outside them **before** you place it.

Defaults are the ones in the specification, scaled for a £2,000 account. They
are deliberately conservative: a helper that says SAFE too easily is worse
than no helper, because it launders a bad decision through a green label.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

__all__ = ["LiveHelperConfig", "load_live_helper_config"]


@dataclass(frozen=True, slots=True)
class LiveHelperConfig:
    """Per-trade limits, and the thresholds that raise a flag.

    Attributes
    ----------
    account_balance_gbp:
        What the account is worth *now*. Margin usage is meaningless without
        it, so it is required rather than inferred.
    used_margin_gbp:
        Margin already committed to open positions. A trade that is fine on
        an empty account can be the one that tips a loaded one over.
    max_risk_per_trade_gbp:
        The most a single trade may lose between entry and stop. This is the
        number that matters: exposure is what you hold, risk is what you can
        lose.
    max_exposure_per_instrument_gbp:
        Notional cap, per instrument.
    max_leverage_per_trade:
        Leverage on this position alone.
    max_margin_usage_pct_after_trade:
        Total margin usage once this trade is on, as a fraction. Trading 212
        force-closes on a margin *ratio* without warning, so headroom here is
        not a nicety.
    max_fx_exposure_pct:
        Share of the account exposed to a currency other than the account's.
    volatility_warning_threshold:
        Annualised volatility above which the trade is flagged. Not a block:
        volatility is why the opportunity exists.
    spread_warning_threshold:
        Spread as a fraction of the stop distance. **This is the ratio that
        matters, not the spread alone** — a 2-point spread against a 10-point
        stop is a fifth of the risk budget spent on entry.
    borderline_fraction:
        How close to a limit counts as BORDERLINE. At 0.8, using 80% of any
        limit stops being SAFE.
    """

    account_balance_gbp: float = 2_000.0
    used_margin_gbp: float = 0.0

    max_risk_per_trade_gbp: float = 100.0
    max_exposure_per_instrument_gbp: float = 2_000.0
    max_leverage_per_trade: float = 10.0
    max_margin_usage_pct_after_trade: float = 0.50
    max_fx_exposure_pct: float = 0.30

    volatility_warning_threshold: float = 0.25
    spread_warning_threshold: float = 0.20

    borderline_fraction: float = 0.80

    def __post_init__(self) -> None:
        for name in (
            "account_balance_gbp",
            "max_risk_per_trade_gbp",
            "max_exposure_per_instrument_gbp",
            "max_leverage_per_trade",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be > 0, got {getattr(self, name)}")
        if self.used_margin_gbp < 0:
            raise ValueError("used_margin_gbp must be >= 0")
        for name in (
            "max_margin_usage_pct_after_trade",
            "max_fx_exposure_pct",
            "borderline_fraction",
        ):
            value = getattr(self, name)
            if not 0 < value <= 1:
                raise ValueError(
                    f"{name} is a fraction in (0, 1], got {value}; "
                    f"did you mean {value / 100}?"
                )

    @property
    def free_margin_gbp(self) -> float:
        """What is left to commit before the usage cap bites."""
        cap = self.max_margin_usage_pct_after_trade * self.account_balance_gbp
        return max(cap - self.used_margin_gbp, 0.0)


def _env_float(name: str, fallback: float) -> float:
    """One float from the environment, ignoring anything unparseable."""
    raw = os.environ.get(name)
    if raw is None:
        return fallback
    try:
        return float(raw)
    except ValueError:
        return fallback


def load_live_helper_config(**overrides) -> LiveHelperConfig:
    """Build the config from the environment, with explicit overrides on top.

    Environment first because the account balance changes daily and editing a
    file for it invites the number going stale — which would silently make
    every margin figure wrong in the direction that flatters the trade.
    """
    settings = {
        "account_balance_gbp": _env_float("CFD_ACCOUNT_BALANCE", 2_000.0),
        "used_margin_gbp": _env_float("CFD_USED_MARGIN", 0.0),
        "max_risk_per_trade_gbp": _env_float("CFD_MAX_RISK_PER_TRADE", 100.0),
        "max_exposure_per_instrument_gbp": _env_float(
            "CFD_MAX_EXPOSURE_PER_INSTRUMENT", 2_000.0
        ),
        "max_leverage_per_trade": _env_float("CFD_MAX_LEVERAGE_PER_TRADE", 10.0),
        "max_margin_usage_pct_after_trade": _env_float(
            "CFD_MAX_MARGIN_USAGE", 0.50
        ),
        "max_fx_exposure_pct": _env_float("CFD_MAX_FX_EXPOSURE", 0.30),
    }
    settings.update(overrides)
    return LiveHelperConfig(**settings)
