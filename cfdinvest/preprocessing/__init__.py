"""Preprocessing: aligned, derived panels built from the raw lake.

Reads through :class:`~cfdinvest.repository.DataRepository` and writes
nothing. Every transform is a pure function on wide
``timestamp x ticker`` panels; the only stateful object is the
:class:`AnalysisFrame` that collects the results.

    alignment      -> one trading clock; publication-aware macro fill
    returns        -> simple, log, rolling, overnight
    volatility     -> rolling stdev, realised, ATR
    correlations   -> rolling matrix, average pairwise, breakdown flags
    frame          -> AnalysisFrame, the single output

The central guarantee is in :mod:`alignment`: macro values enter the feature
set on their publication date, never their observation date.
"""

from cfdinvest.preprocessing.alignment import (
    align_macro,
    align_market,
    publication_aware_ffill,
    value_available_at,
)
from cfdinvest.preprocessing.frame import (
    AnalysisFrame,
    PreprocessConfig,
    analysis_frame_from_repository,
    build_analysis_frame,
)

__all__ = [
    "AnalysisFrame",
    "PreprocessConfig",
    "build_analysis_frame",
    "analysis_frame_from_repository",
    "align_market",
    "align_macro",
    "publication_aware_ffill",
    "value_available_at",
]
