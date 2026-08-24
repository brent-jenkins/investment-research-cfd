"""End-to-end example: ingest market + macro data, then read it back.

Usage
-----
    python examples/run_ingestion.py                    # full config universe
    python examples/run_ingestion.py --tickers SPY QQQ GLD
    python examples/run_ingestion.py --interval 1h --start 2024-01-01
    python examples/run_ingestion.py --skip-macro       # no FRED key needed

A FRED API key is required for the macro leg. Put it in a ``.env`` file at the
repo root as ``FRED_API_KEY=...`` (see ``.env.example``), or pass
``--skip-macro`` to run the market leg alone.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow `python examples/run_ingestion.py` to work from a fresh clone without
# `pip install -e .` first. Library code never does this.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from cfdinvest.config import load_config  # noqa: E402
from cfdinvest.ingestion import MacroDataIngestor, MarketDataIngestor  # noqa: E402
from cfdinvest.ingestion.macro import FredError  # noqa: E402
from cfdinvest.logging_utils import configure_logging  # noqa: E402
from cfdinvest.repository import DataRepository  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line options for the example run."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default=None, help="path to a TOML config file")
    parser.add_argument("--tickers", nargs="*", default=None, help="override the universe")
    parser.add_argument("--series", nargs="*", default=None, help="override FRED series")
    parser.add_argument("--interval", default=None, help="bar size, e.g. 1d or 1h")
    parser.add_argument("--start", default=None, help="start date, YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="end date, YYYY-MM-DD")
    parser.add_argument("--data-root", default=None, help="override the lake location")
    parser.add_argument("--full-refresh", action="store_true", help="ignore stored data")
    parser.add_argument(
        "--vintage-mode",
        default=None,
        choices=["latest", "vintage", "initial_release"],
        help="macro revision mode; initial_release is point-in-time correct",
    )
    parser.add_argument(
        "--allow-mode-change",
        action="store_true",
        help="replace stored macro files when the vintage mode differs",
    )
    parser.add_argument("--skip-market", action="store_true")
    parser.add_argument("--skip-macro", action="store_true")
    parser.add_argument("--log-level", default=None, help="DEBUG, INFO, WARNING, ...")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the ingestion demo. Returns a process exit code."""
    args = parse_args(argv)

    # 1. Configuration -- everything downstream is derived from this object.
    overrides: dict[str, dict[str, object]] = {}
    if args.data_root:
        overrides.setdefault("paths", {})["data_root"] = args.data_root
    if args.log_level:
        overrides.setdefault("logging", {})["level"] = args.log_level
    if args.vintage_mode:
        overrides.setdefault("macro", {})["vintage_mode"] = args.vintage_mode

    config = load_config(args.config, overrides=overrides)
    configure_logging(config.logging)

    pd.set_option("display.width", 160)
    pd.set_option("display.max_columns", 20)

    print(f"Data lake: {config.paths.data_root.resolve()}")

    # 2. Market data (yfinance).
    if not args.skip_market:
        market_ingestor = MarketDataIngestor(config)
        market_report = market_ingestor.ingest(
            args.tickers,
            interval=args.interval,
            start=args.start,
            end=args.end,
            incremental=not args.full_refresh,
        )
        print("\n=== MARKET INGESTION ===")
        print(market_report.summary())
        print(market_report.to_frame()[["status", "rows_written", "rows_total"]])
        if market_report.failed:
            print(f"Problem symbols: {[r.symbol for r in market_report.failed]}")

        # Anomalous bars are stored unrepaired; this is how you find them.
        flagged = {r.symbol: r.anomalies for r in market_report.results if r.anomalies}
        if flagged:
            print("\nData-quality flags (bars stored as-is, NOT repaired):")
            for symbol, counts in flagged.items():
                print(f"  {symbol}: {counts}")

    # 3. Macro data (FRED). The context manager closes the HTTP session.
    if not args.skip_macro:
        try:
            with MacroDataIngestor(config) as macro_ingestor:
                macro_report = macro_ingestor.ingest(
                    args.series,
                    observation_start=args.start,
                    incremental=not args.full_refresh,
                    allow_mode_change=args.allow_mode_change,
                )
            print("\n=== MACRO INGESTION ===")
            print(macro_report.summary())
            print(f"vintage mode: {macro_report.params['vintage_mode']} "
                  f"(point-in-time: {macro_report.params['point_in_time']})")
            print(macro_report.to_frame()[["status", "rows_written", "rows_total"]])
        except FredError as exc:
            print(f"\nSkipping macro ingestion: {exc}", file=sys.stderr)

    # 4. Read it back through the unified repository.
    repo = DataRepository(config)
    interval = args.interval or config.market.interval

    print("\n=== LAKE CONTENTS ===")
    print(f"tickers: {len(repo.available_tickers(interval))}, "
          f"macro series: {len(repo.available_series())}")

    bundle = repo.load_bundle(args.tickers, args.series, interval=interval, start=args.start)

    print("\n--- market coverage ---")
    print(bundle.describe().head(10))

    # The wide panel is the shape signal maths wants: one vectorised call
    # computes returns for the entire universe.
    closes = bundle.panel("close")
    if not closes.empty:
        returns = closes.pct_change()
        print("\n--- last 3 closes ---")
        print(closes.tail(3).iloc[:, :8])
        print("\n--- annualised vol (252d, last value) ---")
        print((returns.rolling(252).std() * (252**0.5)).iloc[-1].round(4).head(10))

    if not bundle.macro.empty:
        print("\n--- macro panel (last 3 rows, gaps preserved) ---")
        print(bundle.macro.tail(3).iloc[:, :8])

        coverage = repo.macro_coverage()
        if not coverage.empty:
            print("\n--- macro provenance ---")
            print(coverage[["status", "rows_total", "vintage_mode"]])

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
