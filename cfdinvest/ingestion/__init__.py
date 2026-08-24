"""Ingestion layer: vendor downloads normalised into the canonical schema.

Each ingestor is responsible for exactly three things:

1. Talking to one vendor (with retries and rate limiting).
2. Normalising the payload into :mod:`cfdinvest.schema` conventions.
3. Persisting it through :mod:`cfdinvest.storage` and returning a report.

Reading data back is *not* their job -- that belongs to
:class:`cfdinvest.repository.DataRepository`.
"""

from cfdinvest.ingestion.macro import MacroDataIngestor, normalize_fred_observations
from cfdinvest.ingestion.market import MarketDataIngestor, normalize_yfinance_frame

__all__ = [
    "MarketDataIngestor",
    "MacroDataIngestor",
    "normalize_yfinance_frame",
    "normalize_fred_observations",
]
