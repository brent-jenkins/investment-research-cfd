"""Command line for recording what you actually did.

::

    cfdinvest-confirm open SPX500 --price 4434.8 --quantity 0.684
    cfdinvest-confirm close NAS100 --price 18923.4
    cfdinvest-confirm flip SPX500 --close-price 4434.8 --open-price 4436.1 \\
        --quantity 0.5 --direction short
    cfdinvest-confirm ignore GLD --notes "spread too wide"
    cfdinvest-confirm list
    cfdinvest-confirm from-reply --file reply.txt

**This command places nothing.** It writes a file saying what you did, which
the rest of the system then believes in preference to its own simulation.

Every recording command prints the recommendation first and the confirmation
second, so the two are on screen together at the moment you commit to the
number. That ordering is the point of the command: the check is worthless
after the write.

Exit codes: ``0`` recorded, ``1`` refused, ``2`` bad usage.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Sequence

import pandas as pd

from cfdinvest.confirm.recommendation import recommendation_for
from cfdinvest.confirm.reply import parse_confirmation_email
from cfdinvest.persist.confirm import (
    Confirmation,
    load_confirmations,
    write_confirmation,
)

logger = logging.getLogger(__name__)

__all__ = ["build_parser", "main"]

EXIT_OK, EXIT_REFUSED, EXIT_USAGE = 0, 1, 2

DASH = "not recorded"


def build_parser() -> argparse.ArgumentParser:
    """The argument parser, built separately so a test can exercise it."""
    parser = argparse.ArgumentParser(
        prog="cfdinvest-confirm",
        description=(
            "Record a Trading 212 fill you have already made. Places nothing; "
            "it writes down what happened so the system stops assuming its "
            "own recommendation was followed."
        ),
    )
    parser.add_argument("--data-root", default=None, help="override the lake location")
    parser.add_argument("--account", default=None, help="which account this is")
    parser.add_argument(
        "--balance",
        type=float,
        default=None,
        help="account balance in GBP, to scale the recommended size",
    )
    parser.add_argument("--log-level", default="WARNING")

    commands = parser.add_subparsers(dest="command", required=True)

    def leg(name: str, help_text: str) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument("ticker")
        sub.add_argument("--notes", default=None)
        sub.add_argument(
            "--at",
            default=None,
            help="when it happened; defaults to now (ISO 8601)",
        )
        sub.add_argument(
            "--run", default=None, help="the run this answers; defaults to the latest"
        )
        return sub

    opened = leg("open", "record a position you opened")
    opened.add_argument("--price", type=float, required=True, help="the fill you got")
    opened.add_argument("--quantity", type=float, required=True)
    opened.add_argument(
        "--direction",
        choices=("long", "short"),
        default=None,
        help="defaults to the sign of --quantity, or the recommendation",
    )
    opened.add_argument("--exposure", type=float, default=None)

    closed = leg("close", "record a position you closed")
    closed.add_argument("--price", type=float, required=True)

    flipped = leg("flip", "record a close and an open in one decision")
    flipped.add_argument("--close-price", type=float, required=True)
    flipped.add_argument("--open-price", type=float, required=True)
    flipped.add_argument("--quantity", type=float, required=True)
    flipped.add_argument("--direction", choices=("long", "short"), default=None)

    ignored = leg("ignore", "record that you saw a recommendation and passed")

    listing = commands.add_parser("list", help="show what has been confirmed")
    listing.add_argument("--ticker", default=None)
    listing.add_argument("--limit", type=int, default=20)

    show = commands.add_parser(
        "show", help="print the recommendation for an instrument and stop"
    )
    show.add_argument("ticker")
    show.add_argument("--run", default=None)

    reply = commands.add_parser(
        "from-reply",
        help="read CONFIRM lines out of a saved email reply",
        description=(
            "Parses a mail body you supply. It does not fetch mail: there is "
            "no IMAP client in this project, by rule."
        ),
    )
    reply.add_argument(
        "--file", default=None, help="a file holding the body; omit to read stdin"
    )
    reply.add_argument(
        "--apply",
        action="store_true",
        help="write the confirmations; without this the parse is only shown",
    )

    return parser


def _config(args):
    from cfdinvest.config import load_config

    config = load_config()
    if args.data_root:
        from dataclasses import replace

        config = replace(
            config, paths=replace(config.paths, data_root=args.data_root)
        )
    return config


def _print_recommendation(ticker: str, config, args) -> dict:
    """Show what was suggested, before anything is written."""
    recommendation = recommendation_for(
        ticker,
        config,
        run_id=getattr(args, "run", None),
        account=args.account,
        account_balance=args.balance,
    )

    print(f"\nRecommended for {ticker} (run {recommendation['run_id'] or DASH}):")
    if not recommendation["available"]:
        print("  nothing recorded — no run has recommended this instrument")
        return recommendation

    def line(label: str, value, fmt: str = "{:,.4f}") -> None:
        shown = fmt.format(value) if value is not None else DASH
        print(f"  {label:<22} {shown}")

    if recommendation["flat"]:
        print("  the run holds no position in this instrument")
    line("direction", recommendation["direction"], "{}")
    line("quantity (simulated)", recommendation["quantity"])
    if recommendation["scale"] is not None:
        line("quantity (your size)", recommendation["scaled_quantity"])
        line("scale", recommendation["scale"], "{:.4f}")
    line("entry price", recommendation["entry_price"])
    line("stop", recommendation["stop_level"])
    line("take-profit", recommendation["take_profit_level"])
    line("exposure", recommendation["exposure"], "{:,.2f}")
    if recommendation["scale"] is not None:
        line("exposure (your size)", recommendation["scaled_exposure"], "{:,.2f}")
    line("leverage", recommendation["leverage"], "{:,.1f}")
    line("fx rate", recommendation["fx_rate"], "{:,.4f}")
    if recommendation["reason"]:
        line("reason", recommendation["reason"], "{}")
    if recommendation["absent"]:
        print(f"  not recorded by this run: {', '.join(recommendation['absent'])}")
    return recommendation


def _record(args, config, recommendation: dict) -> int:
    """Build and write the confirmation for one recording command."""
    action = args.command
    shared = dict(
        notes=args.notes,
        timestamp=args.at,
        run_id=recommendation.get("run_id"),
        recommended_price=recommendation.get("entry_price"),
        recommended_quantity=(
            recommendation.get("scaled_quantity")
            if recommendation.get("scale") is not None
            else recommendation.get("quantity")
        ),
    )

    try:
        if action == "close":
            confirmation = Confirmation(
                ticker=args.ticker, action="close", price=args.price, **shared
            )
        elif action == "ignore":
            confirmation = Confirmation(
                ticker=args.ticker, action="ignore", **shared
            )
        elif action == "flip":
            confirmation = Confirmation.of(
                args.ticker,
                "flip",
                direction=args.direction,
                quantity=args.quantity,
                price=args.open_price,
                close_price=args.close_price,
                **shared,
            )
        else:
            confirmation = Confirmation.of(
                args.ticker,
                "open",
                direction=args.direction,
                quantity=args.quantity,
                price=args.price,
                exposure=args.exposure,
                **shared,
            )
    except ValueError as error:
        print(f"\nRefused: {error}", file=sys.stderr)
        return EXIT_REFUSED

    path = write_confirmation(confirmation, config, account=args.account)

    print(f"\nConfirmed {confirmation.action} {confirmation.ticker}:")
    if confirmation.quantity:
        print(
            f"  {confirmation.direction} {abs(confirmation.quantity):,.4f} "
            f"at {confirmation.price:,.4f}"
        )
    elif confirmation.price is not None:
        print(f"  at {confirmation.price:,.4f}")
    if confirmation.exposure is not None:
        print(f"  exposure {confirmation.exposure:,.2f}")
    if confirmation.slippage is not None:
        worse = "worse than" if confirmation.slippage > 0 else "better than"
        print(
            f"  {abs(confirmation.slippage):,.4f} {worse} the recommended fill"
        )
    if confirmation.quantity_deviation is not None:
        print(f"  {confirmation.quantity_deviation:.1%} of the recommended size")
    print(f"  recorded at {path}")
    return EXIT_OK


def _command_list(args, config) -> int:
    found = load_confirmations(config, args.account, ticker=args.ticker)
    if not found:
        print("Nothing has been confirmed yet.")
        print(
            "That is not the same as no trades: unconfirmed runs are still "
            "scored on the simulated positions."
        )
        return EXIT_OK

    from cfdinvest.persist.confirm import to_frame

    frame = to_frame(found).tail(args.limit)
    columns = [
        c
        for c in (
            "timestamp", "ticker", "action", "direction", "quantity",
            "price", "exposure", "slippage", "quantity_deviation",
        )
        if c in frame.columns
    ]
    pd.set_option("display.width", 160)
    print(frame[columns].to_string(index=False))
    return EXIT_OK


def _command_from_reply(args, config) -> int:
    body = (
        open(args.file, encoding="utf-8").read()
        if args.file
        else sys.stdin.read()
    )
    commands = parse_confirmation_email(body)
    if not commands:
        print("No CONFIRM lines found.", file=sys.stderr)
        return EXIT_REFUSED

    failed = [c for c in commands if not c.ok]
    for command in commands:
        if command.ok:
            print(f"  ok      {command.line.strip()}")
        else:
            print(f"  REFUSED {command.line.strip()}\n            {command.error}")

    # All or nothing. A body with one good line and one bad one must not go
    # half through: the sender believes both were recorded.
    if failed:
        print(
            f"\n{len(failed)} line(s) could not be read; nothing was written.",
            file=sys.stderr,
        )
        return EXIT_REFUSED
    if not args.apply:
        print("\nParsed only. Pass --apply to record these.")
        return EXIT_OK

    for command in commands:
        print(f"  recorded {write_confirmation(command.confirmation, config, account=args.account)}")
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point. Returns a process exit code rather than raising."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=getattr(logging, str(args.log_level).upper(), 30))
    config = _config(args)

    try:
        if args.command == "list":
            return _command_list(args, config)
        if args.command == "from-reply":
            return _command_from_reply(args, config)

        recommendation = _print_recommendation(args.ticker, config, args)
        if args.command == "show":
            return EXIT_OK
        return _record(args, config, recommendation)
    except KeyboardInterrupt:
        print("\nInterrupted; nothing was written.", file=sys.stderr)
        return EXIT_REFUSED
    except OSError as error:
        print(f"Could not write the confirmation: {error}", file=sys.stderr)
        return EXIT_REFUSED


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
