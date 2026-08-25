"""Command line for the analytics layer.

::

    cfdinvest-analytics run-once      --tickers SPY,GLD
    cfdinvest-analytics sweep         --grid grid.json
    cfdinvest-analytics walkforward   --train 365D --test 90D
    cfdinvest-analytics montecarlo    --paths 2000

Every command reads the lake, scores what it finds, and writes only into
``<data_root>/analytics/``. Nothing here changes a pipeline run, the
scheduler, or execution behaviour, and nothing reaches the network.

Results are written under a generated run identifier — a UTC timestamp, the
same shape the pipeline uses — so repeated invocations accumulate rather than
overwrite. ``--run-id`` pins it when a caller wants to group several commands
under one label.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Sequence

import pandas as pd

from cfdinvest.analytics import backtest as backtest_module
from cfdinvest.analytics import montecarlo as montecarlo_module
from cfdinvest.analytics import store
from cfdinvest.analytics import walkforward as walkforward_module
from cfdinvest.backtest.runner import BacktestConfig
from cfdinvest.config import load_config
from cfdinvest.logging_utils import configure_logging
from cfdinvest.repository import DataRepository

logger = logging.getLogger(__name__)

__all__ = ["build_parser", "main", "generate_run_id"]

#: The run-id shape the pipeline already uses, so analytics sorts alongside it.
RUN_ID_FORMAT = "%Y%m%dT%H%M%SZ"


def generate_run_id(now: pd.Timestamp | None = None) -> str:
    """A UTC run identifier. Injectable so a test can pin it."""
    moment = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="UTC")
    if moment.tz is None:
        moment = moment.tz_localize("UTC")
    return moment.tz_convert("UTC").strftime(RUN_ID_FORMAT)


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def _common_arguments() -> argparse.ArgumentParser:
    """Options every command accepts.

    Shared through ``parents=`` rather than declared once on the top-level
    parser, so they work on **either** side of the subcommand. Argparse only
    accepts a top-level option before the subcommand, and every example of
    this tool writes them after it.
    """
    # SUPPRESS, so an option the user did not give is simply absent from the
    # namespace. Without it a subparser's default overwrites what the parent
    # already parsed, and `--tickers SPY run-once` silently loses SPY.
    common = argparse.ArgumentParser(add_help=False, argument_default=argparse.SUPPRESS)
    common.add_argument("--config", help="path to config.toml")
    common.add_argument(
        "--tickers",
        help="comma-separated universe; overrides the configured one. "
        "Comma-separated rather than nargs, because a space-separated list "
        "swallows the subcommand that follows it.",
    )
    common.add_argument("--start", help="first bar, ISO date")
    common.add_argument("--end", help="last bar, ISO date")
    common.add_argument("--interval", help="bar interval, e.g. 1d")
    common.add_argument("--run-id", dest="run_id", help="identifier to write under")
    common.add_argument("--log-level", help="DEBUG for the pipeline's own logs")
    common.add_argument(
        "--json", action="store_true", help="print the result as JSON"
    )
    return common


#: Applied after parsing, because the parser itself suppresses defaults.
COMMON_DEFAULTS: dict[str, Any] = {
    "config": None,
    "tickers": None,
    "start": None,
    "end": None,
    "interval": None,
    "run_id": None,
    "log_level": "INFO",
    "json": False,
}


def _with_defaults(args: argparse.Namespace) -> argparse.Namespace:
    """Fill in what SUPPRESS left out."""
    for name, value in COMMON_DEFAULTS.items():
        if not hasattr(args, name):
            setattr(args, name, value)
    return args


def build_parser() -> argparse.ArgumentParser:
    """The argument parser, built separately so a test can exercise it."""
    common = _common_arguments()
    parser = argparse.ArgumentParser(
        prog="cfdinvest-analytics",
        parents=[common],
        description="Score, sweep and stress-test completed pipeline runs. "
        "Read-only: nothing here trades, and nothing reaches the network.",
    )

    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser(
        "run-once", parents=[common], help="score one configuration"
    )

    scored = commands.add_parser(
        "score", parents=[common],
        help="score a run already on disk, without running the pipeline",
    )
    scored.add_argument(
        "--all", action="store_true",
        help="score every stored run and print them as one table",
    )

    sweep = commands.add_parser("sweep", parents=[common], help="run a parameter grid")
    sweep.add_argument(
        "--grid",
        required=True,
        help='JSON file or inline JSON, e.g. \'{"execution.max_leverage": [2, 5]}\'',
    )

    forward = commands.add_parser(
        "walkforward", parents=[common], help="train/test over rolling windows"
    )
    forward.add_argument("--train", default="365D", help="training length, e.g. 365D")
    forward.add_argument("--test", default="90D", help="test length, e.g. 90D")
    forward.add_argument("--step", help="stride between windows; defaults to --test")
    forward.add_argument(
        "--anchored",
        action="store_true",
        help="grow the training window from a fixed start instead of rolling it",
    )
    forward.add_argument("--grid", help="optional grid to select on, per window")
    forward.add_argument(
        "--windows", help="JSON file of explicit train/test date ranges"
    )
    forward.add_argument(
        "--objective", default="sharpe_ratio", help="metric to select on"
    )

    carlo = commands.add_parser(
        "montecarlo", parents=[common], help="resample the trade sequence"
    )
    carlo.add_argument("--paths", type=int, default=1_000, help="paths to draw")
    carlo.add_argument("--seed", type=int, default=montecarlo_module.DEFAULT_SEED)
    carlo.add_argument(
        "--shuffle",
        action="store_true",
        help="permute the actual trades instead of sampling with replacement, "
        "holding the total return fixed and varying only the ordering",
    )

    return parser


def _load_json_argument(value: str | None, what: str) -> Any:
    """Accept either a path to a JSON file or inline JSON."""
    if not value:
        return None
    candidate = Path(value)
    try:
        if candidate.is_file():
            return json.loads(candidate.read_text(encoding="utf-8"))
        return json.loads(value)
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"could not read {what}: {error}") from error


def _backtest_config(args) -> BacktestConfig:
    """Turn the shared arguments into a pipeline configuration."""
    tickers = (
        [t.strip() for t in args.tickers.split(",") if t.strip()]
        if args.tickers
        else None
    )
    return BacktestConfig(
        tickers=tickers,
        interval=args.interval,
        start=args.start,
        end=args.end,
    )


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def _command_run_once(args, app_config, repository, run_id: str) -> int:
    result = backtest_module.run_backtest(
        _backtest_config(args), repository=repository
    )
    if not result.ok:
        print(f"Run failed: {result.error}", file=sys.stderr)
        return 1

    store.save_backtest_result(app_config, run_id, result)
    _report(args, {"run_id": run_id, **result.headline,
                   "warnings": list(result.warnings)})
    return 0


def _command_score(args, app_config, repository, run_id: str) -> int:
    """Score what the pipeline already wrote. Runs nothing."""
    from cfdinvest.analytics import persisted

    if args.all:
        table = persisted.score_every_run(app_config)
        if table.empty:
            print("No runs have been recorded yet.", file=sys.stderr)
            return 1
        if args.json:
            _report(args, {"runs": table.reset_index().to_dict("records")})
        else:
            print(table.to_string())
        return 0

    result = persisted.score_run(app_config, args.run_id)
    if not result.ok:
        print(f"Could not score: {result.error}", file=sys.stderr)
        return 1

    store.save_backtest_result(app_config, result.label, result)
    _report(args, {"run_id": result.label, **result.headline,
                   "warnings": list(result.warnings)})
    return 0


def _command_sweep(args, app_config, repository, run_id: str) -> int:
    grid = _load_json_argument(args.grid, "--grid")
    if not isinstance(grid, dict) or not grid:
        print("--grid must be a JSON object of setting -> list", file=sys.stderr)
        return 2

    results = backtest_module.run_parameter_sweep(
        _backtest_config(args), grid, repository=repository
    )
    store.save_sweep(app_config, run_id, results)

    table = backtest_module.sweep_frame(results)
    if args.json:
        _report(args, {"run_id": run_id,
                       "results": table.reset_index().to_dict("records")})
    else:
        print(f"Sweep {run_id}: {len(results)} combinations")
        print(table.to_string())
    return 0 if any(r.ok for r in results) else 1


def _command_walkforward(args, app_config, repository, run_id: str) -> int:
    config = _backtest_config(args)
    windows = _windows(args, repository, config)
    if not windows:
        print(
            "no windows fit the requested period; shorten --train/--test or "
            "widen --start/--end",
            file=sys.stderr,
        )
        return 2

    results = walkforward_module.run_walkforward(
        config, windows, repository=repository,
        param_grid=_load_json_argument(args.grid, "--grid"),
        objective=args.objective,
    )
    store.save_walkforward(app_config, run_id, results)

    summary = walkforward_module.stability_summary(results)
    if args.json:
        _report(args, {"run_id": run_id, "windows": len(results),
                       "stability": summary.reset_index().to_dict("records")})
    else:
        print(f"Walk-forward {run_id}: {len(results)} windows")
        print(summary.to_string())
    return 0 if any(r.ok for r in results) else 1


def _windows(args, repository, config) -> list:
    """Explicit windows from a file, or generated ones."""
    explicit = _load_json_argument(args.windows, "--windows")
    if explicit:
        return [
            walkforward_module.DateWindow(
                number=n,
                train_start=pd.Timestamp(w["train_start"]),
                train_end=pd.Timestamp(w["train_end"]),
                test_start=pd.Timestamp(w["test_start"]),
                test_end=pd.Timestamp(w["test_end"]),
            )
            for n, w in enumerate(explicit)
        ]

    start, end = _period(args, repository, config)
    if start is None or end is None:
        return []

    builder = (
        walkforward_module.anchored_date_windows
        if args.anchored
        else walkforward_module.rolling_date_windows
    )
    return builder(start, end, train=args.train, test=args.test, step=args.step)


def _period(args, repository, config):
    """The date range to build windows over, from the lake if not given."""
    if args.start and args.end:
        return pd.Timestamp(args.start), pd.Timestamp(args.end)
    try:
        coverage = repository.coverage(config.interval)
    except Exception as error:  # noqa: BLE001
        logger.warning("Could not read coverage: %s", error)
        return None, None
    if coverage is None or coverage.empty:
        return None, None

    starts = pd.to_datetime(coverage.get("start"), utc=True, errors="coerce")
    ends = pd.to_datetime(coverage.get("end"), utc=True, errors="coerce")
    if starts is None or ends is None or starts.isna().all() or ends.isna().all():
        return None, None
    return (
        pd.Timestamp(args.start) if args.start else starts.min(),
        pd.Timestamp(args.end) if args.end else ends.max(),
    )


def _command_montecarlo(args, app_config, repository, run_id: str) -> int:
    result = backtest_module.run_backtest(
        _backtest_config(args), repository=repository
    )
    if not result.ok:
        print(f"Run failed: {result.error}", file=sys.stderr)
        return 1

    initial = float(
        (result.metrics.get("drawdown") or {}).get("peak_equity", 0.0)
    ) or float(result.equity_curve.iloc[0] if len(result.equity_curve) else 100_000.0)

    paths = montecarlo_module.resample_trades(
        result.trades,
        n_paths=args.paths,
        seed=args.seed,
        initial_equity=initial,
        replace=not args.shuffle,
    )
    if not paths:
        print("no completed trades to resample", file=sys.stderr)
        return 1

    summary = montecarlo_module.compute_montecarlo_metrics(paths)
    store.save_montecarlo(app_config, run_id, summary, paths)

    if args.json:
        _report(args, {"run_id": run_id, **summary})
    else:
        print(f"Monte Carlo {run_id}: {summary['paths']} paths, "
              f"{summary['trades_per_path']} trades each")
        print(f"  median return    {summary['median_return']:>10.2%}")
        print(f"  5th percentile   {summary['tail_loss']:>10.2%}")
        print(f"  P(loss)          {summary['probability_of_loss']:>10.2%}")
        print(f"  median drawdown  {summary['median_drawdown']:>10.2%}")
        print(f"  worst drawdown   {summary['worst_drawdown']:>10.2%}")
    return 0


def _report(args, payload: dict) -> None:
    """Print a result, as JSON when asked."""
    if args.json:
        print(json.dumps(store.jsonable(payload), indent=2))
        return
    for key, value in payload.items():
        if isinstance(value, list):
            if value:
                print(f"{key}:")
                for item in value:
                    print(f"  - {item}")
        else:
            print(f"{key}: {value}")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and dispatch. Returns a process exit code."""
    args = _with_defaults(build_parser().parse_args(argv))

    app_config = load_config(args.config) if args.config else load_config()
    # Logging is configured from the app config, with the command-line level
    # applied on top -- the config object is the single place that knows
    # whether a log file is wanted.
    configure_logging(replace(app_config.logging, level=args.log_level))
    repository = DataRepository(app_config)
    run_id = args.run_id or generate_run_id()

    commands = {
        "run-once": _command_run_once,
        "score": _command_score,
        "sweep": _command_sweep,
        "walkforward": _command_walkforward,
        "montecarlo": _command_montecarlo,
    }
    try:
        return commands[args.command](args, app_config, repository, run_id)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as error:  # noqa: BLE001 - a CLI reports, it does not dump
        logger.exception("Command %s failed", args.command)
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
