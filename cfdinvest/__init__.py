"""cfdinvest - a modular multi-asset CFD backtesting engine.

Layers (built incrementally):

    config      -> typed, file/env-driven configuration
    storage     -> Parquet persistence primitives
    ingestion   -> yfinance (market) + FRED (macro) downloaders
    repository  -> unified read API over the Parquet lake
    preprocess  -> (next) alignment, returns, volatility, correlations
    signals     -> (next) trend / momentum / vol / macro-regime
    execution   -> (next) CFD fill model, financing, margin
    analytics   -> (next) performance statistics
"""

from cfdinvest.config import AppConfig, load_config
from cfdinvest.repository import DataRepository

__all__ = ["AppConfig", "load_config", "DataRepository", "__version__"]
__version__ = "0.1.0"
