"""Volatility-adjusted position sizing, scaled to ``[0, 1]``.

A signal says *which way*; sizing says *how much*. Sizing every name equally
means the most volatile asset dominates the book's risk, so both methods here
size **inversely to volatility** — the position shrinks as the asset gets
wilder, targeting a constant risk contribution rather than a constant notional.

Two methods, because they fail differently:

``volatility_target_size``
    Targets a portfolio-level volatility. Uses realised volatility, so it is
    close-to-close and blind to intrabar movement.
``atr_risk_size``
    Sizes so that a stop placed a multiple of ATR away costs a fixed fraction
    of capital. This is the one that matters for a CFD stop-loss, because a
    stop is triggered by the low, not by the close.

:func:`blend_sizes` takes the **minimum** of the two rather than an average:
when two risk measures disagree about how dangerous something is, the
conservative one should win.

All functions are pure, vectorised, and produce no NaN in the output — an
asset with no volatility estimate sizes to zero rather than to something
undefined.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = [
    "volatility_target_size",
    "atr_risk_size",
    "blend_sizes",
    "scale_by_conviction",
    "cap_gross_exposure",
]


def _clean(size: pd.DataFrame, max_size: float) -> pd.DataFrame:
    """Clip to ``[0, max_size]`` and turn undefined sizes into zero."""
    return size.clip(lower=0.0, upper=max_size).fillna(0.0)


def volatility_target_size(
    volatility: pd.DataFrame,
    *,
    target_volatility: float = 0.15,
    max_size: float = 1.0,
    floor_volatility: float = 0.02,
) -> pd.DataFrame:
    """Size inversely to realised volatility, targeting ``target_volatility``.

    ``size = target / realised``, so an asset running at twice the target
    gets half the weight.

    Parameters
    ----------
    volatility:
        Annualised realised volatility, ``timestamp x ticker``.
    target_volatility:
        Desired annualised volatility contribution per position, e.g.
        ``0.15`` for 15%.
    max_size:
        Upper bound after scaling.
    floor_volatility:
        Volatility is floored here before dividing. Without a floor, an
        asset in an unusually quiet stretch gets an enormous size on the
        strength of a temporarily small denominator — which is exactly when
        the estimate is least trustworthy.

    Returns
    -------
    pandas.DataFrame
        Sizes in ``[0, max_size]``, zero where volatility is unknown.
    """
    if volatility is None or volatility.empty:
        return pd.DataFrame()
    if target_volatility <= 0:
        raise ValueError(f"target_volatility must be > 0, got {target_volatility}")
    if floor_volatility <= 0:
        raise ValueError(f"floor_volatility must be > 0, got {floor_volatility}")

    floored = volatility.clip(lower=floor_volatility)
    size = target_volatility / floored.where(floored > 0)
    return _clean(size, max_size)


def atr_risk_size(
    atr_percent: pd.DataFrame,
    *,
    risk_fraction: float = 0.01,
    stop_multiple: float = 2.0,
    max_size: float = 1.0,
    floor_atr_percent: float = 0.002,
) -> pd.DataFrame:
    """Size so a stop ``stop_multiple`` ATRs away risks ``risk_fraction``.

    The arithmetic: a position of size ``s`` that moves ``stop_multiple *
    atr%`` against you loses ``s * stop_multiple * atr%`` of capital. Setting
    that equal to ``risk_fraction`` gives the size below.

    Parameters
    ----------
    atr_percent:
        ATR divided by price, ``timestamp x ticker``.
    risk_fraction:
        Capital fraction at risk if the stop is hit, e.g. ``0.01`` for 1%.
    stop_multiple:
        Stop distance in ATRs.
    floor_atr_percent:
        Floor on the denominator, for the same reason as
        :func:`volatility_target_size`.

    Returns
    -------
    pandas.DataFrame
        Sizes in ``[0, max_size]``, zero where ATR is unknown.
    """
    if atr_percent is None or atr_percent.empty:
        return pd.DataFrame()
    if risk_fraction <= 0:
        raise ValueError(f"risk_fraction must be > 0, got {risk_fraction}")
    if stop_multiple <= 0:
        raise ValueError(f"stop_multiple must be > 0, got {stop_multiple}")

    floored = atr_percent.clip(lower=floor_atr_percent)
    size = risk_fraction / (stop_multiple * floored.where(floored > 0))
    return _clean(size, max_size)


def blend_sizes(*sizes: pd.DataFrame, max_size: float = 1.0) -> pd.DataFrame:
    """Combine sizing methods by taking the most conservative at each cell.

    The minimum, not the mean: when one risk measure sees danger the other
    misses, deferring to the cautious reading is the whole reason for running
    two of them.

    Empty inputs are ignored, so a lake without ATR still sizes on realised
    volatility alone.

    Returns
    -------
    pandas.DataFrame
        Element-wise minimum, clipped to ``[0, max_size]``. Empty when no
        method produced anything.
    """
    present = [frame for frame in sizes if frame is not None and not frame.empty]
    if not present:
        return pd.DataFrame()

    combined = present[0]
    for frame in present[1:]:
        aligned_left, aligned_right = combined.align(frame, join="outer")
        # fillna(max_size) on each side: a method with no opinion here must
        # not veto the other by contributing a NaN that propagates.
        combined = np.minimum(
            aligned_left.fillna(max_size), aligned_right.fillna(max_size)
        )
    return _clean(combined, max_size)


def scale_by_conviction(
    size: pd.DataFrame,
    strength: pd.DataFrame,
    *,
    min_scale: float = 0.0,
    max_size: float = 1.0,
) -> pd.DataFrame:
    """Scale sizes by the absolute trend strength.

    A marginal signal gets a marginal position. ``min_scale`` sets a floor so
    that a weak-but-valid signal is not scaled to nothing — set it to 1.0 to
    disable conviction scaling entirely.

    Returns
    -------
    pandas.DataFrame
        Sizes in ``[0, max_size]``.
    """
    if size is None or size.empty:
        return pd.DataFrame()
    if not 0.0 <= min_scale <= 1.0:
        raise ValueError(f"min_scale must be in [0, 1], got {min_scale}")
    if strength is None or strength.empty:
        return _clean(size, max_size)

    conviction = strength.abs().clip(0.0, 1.0).reindex_like(size)
    scale = conviction.fillna(0.0).clip(lower=min_scale)
    return _clean(size * scale, max_size)


def cap_gross_exposure(
    size: pd.DataFrame,
    *,
    max_gross: float = 3.0,
) -> pd.DataFrame:
    """Scale a whole row down proportionally if gross exposure exceeds a cap.

    Position-level sizing has no idea how many other positions are open. On a
    day when fifty names all signal at once, the book can be levered far
    beyond intent even though every individual size was reasonable. This
    rescales the row, preserving the *relative* sizing the volatility
    targeting produced.

    Not applied by default — it is a portfolio decision, and the caller may
    prefer to handle it at the execution layer.

    Returns
    -------
    pandas.DataFrame
        Rows whose sum exceeds ``max_gross`` scaled down; others untouched.
    """
    if size is None or size.empty:
        return pd.DataFrame()
    if max_gross <= 0:
        raise ValueError(f"max_gross must be > 0, got {max_gross}")

    gross = size.abs().sum(axis=1)
    scale = (max_gross / gross.where(gross > max_gross)).fillna(1.0)
    return size.mul(scale, axis=0)
