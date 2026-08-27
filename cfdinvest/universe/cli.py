"""Running the universe screen, and reading back what it has said.

    cfdinvest-universe screen
    cfdinvest-universe screen --as-of 2026-06-30 --no-record
    cfdinvest-universe history
    cfdinvest-universe drift

The failure this closes
-----------------------
The screen existed and had no runner. Every screen this project had run was
a script somebody wrote for the occasion and then deleted, so the rule was
reproducible in principle and, in practice, only by whoever still had the
file. That is the same defect the screen was built to fix -- a selection
recorded in prose rather than executed as a rule -- one level up.

It also meant nothing was recorded. ``record_screen`` had been written and
never called: on 2026-08-27 the store held zero selections, so a comparison
against "the last screen" had to be read out of a chat log.

What it will not do
-------------------
**It never edits the configuration.** The screen ranks; a person decides.
The command prints the difference between the rule's answer and the traded
universe, records both, and stops. Changing ``market.tickers`` stays a human
act, deliberately: the ranking has never been shown to predict return, and a
rule that quietly rewrote the universe on that evidence would be acting with
more confidence than the evidence supports.

That is not caution for its own sake. Three separate measurements say the
screen is a tradability filter and nothing more: its rank does not correlate
with the following month's contribution, selecting its top N loses to
keeping everything, and its universe does not beat a random draw from the
same pool once volatility is controlled for. What it is good for is
noticing drift, which is why this records.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import sys
from typing import Sequence

logger = logging.getLogger(__name__)

__all__ = ["build_parser", "main"]

EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cfdinvest-universe",
        description=(
            "Rank the candidate pool and record the selection. Reads the lake "
            "and writes one append-only record; it never edits the universe."
        ),
    )
    parser.add_argument("--config", default=None, help="path to a TOML config file")
    parser.add_argument("--account", default=None, help="which account's store")
    parser.add_argument("--log-level", default=None, help="DEBUG, INFO, WARNING")

    commands = parser.add_subparsers(dest="command", required=True)

    screen = commands.add_parser("screen", help="rank the pool and record it")
    screen.add_argument(
        "--as-of", default=None,
        help="decision date; metrics use bars at or before it. Defaults to now.",
    )
    screen.add_argument(
        "--target", type=int, default=None,
        help="how many to select; defaults to however many are traded",
    )
    screen.add_argument(
        "--no-record", action="store_true",
        help="report without appending to the store",
    )
    screen.add_argument(
        "--interval", default="1d",
        help=(
            "bars to measure on. Daily by default and deliberately: liquidity "
            "and volatility belong to an instrument, not to the bar size it "
            "is traded on, and daily has years where hourly has two."
        ),
    )

    history = commands.add_parser("history", help="every selection ever recorded")
    history.add_argument("--limit", type=int, default=10)

    commands.add_parser(
        "drift", help="how the recorded selections have moved, and against what is traded"
    )
    return parser


def _config(args):
    from cfdinvest.config import load_config

    return load_config(args.config) if args.config else load_config()


def _pool(config) -> list[str]:
    """The candidates, or the traded universe when no pool is configured.

    An empty pool is not an error: screening the traded names against each
    other is a re-ranking, which is a smaller question than "which twenty"
    but a real one.
    """
    pool = list(getattr(config.universe, "candidates", ()) or ())
    traded = list(config.market.tickers or ())
    if not pool:
        return traded
    # The traded names are always in the pool, whatever the file says. A
    # screen that cannot see what is held cannot report what would change.
    return pool + [t for t in traded if t not in pool]


def _command_screen(args, config) -> int:
    from cfdinvest.repository import DataRepository
    from cfdinvest.universe import ScreenConfig, select_universe
    from cfdinvest.universe.screen import record_screen

    # Always stamped. With no cutoff the screen records an empty ``as_of``,
    # and a selection with no decision date cannot be ordered against the
    # others or replayed -- which is most of what recording it is for. The
    # default is now, so ``--as-of`` replays a past decision and the absence
    # of the flag still dates the record.
    import pandas as pd

    as_of = args.as_of or str(pd.Timestamp.now(tz="UTC"))

    traded = list(config.market.tickers or ())
    pool = _pool(config)
    target = args.target or getattr(config.universe, "target", None) or len(traded)
    sectors = dict(getattr(config.universe, "sectors", {}) or {})

    cap = getattr(config.universe, "max_per_sector", None)
    settings = ScreenConfig(target=int(target))
    if cap is not None:
        settings = dataclasses.replace(settings, max_per_sector=int(cap) or None)
    result = select_universe(
        DataRepository(config), pool, as_of=as_of,
        config=settings, sectors=sectors or None, interval=args.interval,
    )

    ranked = sorted(result.ranked, key=lambda c: -(c.score or 0.0))
    print(
        f"Screened {len(pool)} candidates on {args.interval} bars"
        f" as of {as_of[:19]}: "
        f"{len(ranked)} ranked, {len(result.unranked)} not."
    )
    for note in result.notes:
        print(f"  note: {note}")
    for candidate in result.unranked:
        print(f"  unranked: {candidate.ticker} — {candidate.unranked}")

    if not ranked:
        print("Nothing could be ranked; the lake has no usable bars.")
        return EXIT_FAILED

    print()
    header = (
        f"{'':2s} {'#':>3s} {'ticker':7s} {'score':>7s} {'turnover':>11s} "
        f"{'vol':>6s} {'sector':12s} held"
    )
    print(header)
    print("-" * len(header))
    for position, candidate in enumerate(ranked, 1):
        chosen = "*" if candidate.ticker in result.selected else " "
        held = "yes" if candidate.ticker in traded else ""
        turnover = (
            f"{10 ** candidate.liquidity / 1e9:10.2f}B"
            if candidate.liquidity is not None else f"{'—':>11s}"
        )
        print(
            f"{chosen:2s} {position:3d} {candidate.ticker:7s} "
            f"{candidate.score:+7.3f} {turnover} "
            f"{candidate.volatility:6.2f} "
            f"{sectors.get(candidate.ticker, '—'):12s} {held}"
        )

    # The cap, when it bound. Said loudly because it changes the answer and
    # because it was inert until a sector map existed: every screen this
    # project ran before 2026-08-27 reported "no sector map supplied, so the
    # sector cap was not applied", and the first run with one displaced
    # instruments that had always been selected before.
    if result.sector_capped:
        print()
        print(
            f"The sector cap ({settings.max_per_sector} per sector) displaced "
            f"{len(result.sector_capped)}: {', '.join(result.sector_capped)}"
        )
        print("Those ranked high enough and were held out by concentration,")
        print("not by their own numbers.")

    dropped = [t for t in traded if t not in result.selected]
    added = [t for t in result.selected if t not in traded]
    print()
    print(f"Traded universe: {len(traded)}. The rule's selection differs by "
          f"{len(dropped)}.")
    print(f"  it would drop: {', '.join(dropped) if dropped else 'nothing'}")
    print(f"  it would add : {', '.join(added) if added else 'nothing'}")

    # Said every time, not as a footnote. The ranking has never been shown to
    # predict return, and somebody reading a table of scores will infer that
    # it does unless told otherwise.
    print()
    print("The screen ranks tradability, not expected return: its rank does")
    print("not correlate with the following month's contribution, and its")
    print("selection does not beat a random draw from the same pool once")
    print("volatility is controlled for. Nothing here changes the universe.")

    if args.no_record:
        print("\nNot recorded (--no-record).")
        return EXIT_OK

    written = record_screen(result, config, args.account)
    if written:
        print("\nRecorded to universe/selections.jsonl.")
    else:
        # Never silently. A screen that thinks it recorded and did not is a
        # comparison that will quietly not exist next month.
        print("\nCould not record the selection; see the log.")
        return EXIT_FAILED
    return EXIT_OK


def _command_history(args, config) -> int:
    from cfdinvest.universe.screen import load_screens

    rows = load_screens(config, args.account)
    if not rows:
        print("No selection has been recorded yet.")
        return EXIT_OK

    print(f"{len(rows)} selection(s) recorded, newest last.")
    for row in rows[-args.limit:]:
        selected = row.get("selected") or []
        print()
        print(f"{str(row.get('as_of') or '?')[:19]}  {len(selected)} selected")
        print(f"  {', '.join(selected)}")
        for note in row.get("notes") or ():
            print(f"  note: {note}")
    return EXIT_OK


def _command_drift(args, config) -> int:
    """What has changed between recorded screens, and against what is traded."""
    from cfdinvest.universe.screen import load_screens

    rows = load_screens(config, args.account)
    traded = set(config.market.tickers or ())

    if not rows:
        print("No selection has been recorded yet, so there is no drift to show.")
        return EXIT_OK
    if len(rows) == 1:
        print("One selection recorded. Drift needs two.")

    previous: set[str] | None = None
    print(f"{'as of':21s} {'n':>3s} {'in':>3s} {'out':>4s} {'vs traded':>10s}  changes")
    print("-" * 78)
    for row in rows:
        selected = set(row.get("selected") or ())
        if previous is None:
            joined, left = set(), set()
        else:
            joined, left = selected - previous, previous - selected
        changes = " ".join(
            [f"+{t}" for t in sorted(joined)] + [f"-{t}" for t in sorted(left)]
        )
        print(
            f"{str(row.get('as_of') or '?')[:21]:21s} {len(selected):3d} "
            f"{len(joined):3d} {len(left):4d} {len(traded - selected):10d}  {changes}"
        )
        previous = selected

    if previous is not None:
        print()
        print(f"  traded but not selected: "
              f"{', '.join(sorted(traded - previous)) or 'nothing'}")
        print(f"  selected but not traded: "
              f"{', '.join(sorted(previous - traded)) or 'nothing'}")
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    from cfdinvest.logging_utils import configure_logging

    args = build_parser().parse_args(argv)
    configure_logging(args.log_level)

    try:
        config = _config(args)
    except Exception as error:  # noqa: BLE001
        print(f"Could not load the configuration: {error}", file=sys.stderr)
        return EXIT_USAGE

    handlers = {
        "screen": _command_screen,
        "history": _command_history,
        "drift": _command_drift,
    }
    try:
        return handlers[args.command](args, config)
    except KeyboardInterrupt:
        return EXIT_FAILED


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
