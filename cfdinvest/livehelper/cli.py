"""Command line for the live-execution helper.

::

    cfdinvest-helper "long SPX500 0.68 entry=4434.8 stop=4390 ccy=USD lev=20" \\
        --fx-rate 0.79 --balance 2000

**This command cannot place a trade.** It parses what you typed, measures it
against your limits, prints a summary, and stops. There is no broker client
in this package and no Trading 212 endpoint anywhere in the project.

The exit code carries the verdict, so a wrapper script can act on it:
``0`` SAFE, ``1`` BORDERLINE, ``2`` UNSAFE. Anything that could not be parsed
exits ``3``, which is deliberately *not* one of the verdicts — a trade that
could not be read has not been judged.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import replace
from typing import Sequence

from cfdinvest.livehelper.config import LiveHelperConfig, load_live_helper_config
from cfdinvest.livehelper.intent import parse_trade_intent
from cfdinvest.livehelper.safety import MarketState, evaluate_trade_safety
from cfdinvest.livehelper.sizing_helper import compute_safe_sizing
from cfdinvest.livehelper.summary import build_execution_helper_summary

logger = logging.getLogger(__name__)

__all__ = ["EXIT_CODES", "build_parser", "main", "email_summary"]

#: The verdict, as a process exit code. ``3`` means "not judged".
EXIT_CODES: dict[str, int] = {"SAFE": 0, "BORDERLINE": 1, "UNSAFE": 2}
PARSE_FAILURE = 3


def build_parser() -> argparse.ArgumentParser:
    """The argument parser, built separately so a test can exercise it."""
    parser = argparse.ArgumentParser(
        prog="cfdinvest-helper",
        description=(
            "Check an intended Trading 212 CFD trade against your own limits. "
            "Places nothing: it reads, measures and prints."
        ),
        epilog=(
            'example: cfdinvest-helper "long SPX500 0.68 entry=4434.8 '
            'stop=4390 tp=4520 ccy=USD lev=20" --fx-rate 0.79'
        ),
    )
    parser.add_argument(
        "trade",
        help='the intended trade, e.g. "long SPX500 0.68 entry=4434.8 stop=4390"',
    )

    market = parser.add_argument_group("market state")
    market.add_argument("--last-price", type=float, help="for an at-market entry")
    market.add_argument("--spread", type=float, help="in instrument currency")
    market.add_argument(
        "--volatility", type=float, help="annualised, as a fraction (0.25 = 25%%)"
    )
    market.add_argument(
        "--fx-rate", type=float,
        help="account currency per unit of instrument currency (0.79 GBP/USD)",
    )
    market.add_argument(
        "--leverage", type=float, help="the instrument's leverage, if not in the trade"
    )
    market.add_argument(
        "--near-rollover", action="store_true",
        help="financing rolls over shortly",
    )
    market.add_argument(
        "--close-only", action="store_true",
        help="the instrument is close-only, so an opening order will be rejected",
    )

    account = parser.add_argument_group("your limits")
    account.add_argument("--balance", type=float, help="account balance")
    account.add_argument(
        "--used-margin", type=float, help="margin already committed"
    )
    account.add_argument("--max-risk", type=float, help="most one trade may lose")
    account.add_argument("--max-exposure", type=float, help="per instrument")
    account.add_argument("--max-leverage", type=float, help="effective, per trade")

    output = parser.add_argument_group("output")
    output.add_argument(
        "--email", action="store_true",
        help="also send the summary, if SMTP is configured in the environment",
    )
    output.add_argument("--quiet", action="store_true", help="suppress stdout")
    return parser


def _config(args) -> LiveHelperConfig:
    """Environment defaults, with any command-line limits on top."""
    settings = load_live_helper_config()
    overrides = {
        "account_balance_gbp": args.balance,
        "used_margin_gbp": args.used_margin,
        "max_risk_per_trade_gbp": args.max_risk,
        "max_exposure_per_instrument_gbp": args.max_exposure,
        "max_leverage_per_trade": args.max_leverage,
    }
    given = {k: v for k, v in overrides.items() if v is not None}
    return replace(settings, **given) if given else settings


def _market(args) -> MarketState:
    return MarketState(
        last_price=args.last_price,
        spread=args.spread,
        volatility=args.volatility,
        fx_rate=args.fx_rate,
        leverage=args.leverage,
        near_financing_rollover=args.near_rollover,
        close_only=args.close_only,
    )


def email_summary(instrument: str, status: str, body: str, transport=None) -> bool:
    """Send the summary, if mail is configured. Returns whether it went.

    Uses the project's existing mail path rather than a second one, so there
    is one place where credentials are read and one place where sending can
    be disabled. Returns ``False`` — quietly — when mail is not configured:
    an unconfigured mailer should not stop a helper that has already printed
    its answer.
    """
    try:
        from cfdinvest.schedule import notify

        config = notify.SmtpConfig.from_env()
        if not getattr(config, "enabled", False):
            logger.info("Mail is not configured; the summary was printed only")
            return False

        return notify.send(
            _plain_message(config, instrument, status, body), config, transport
        )
    except Exception as error:  # noqa: BLE001 - mail must not break the helper
        logger.warning("Could not send the summary: %s", error)
        return False


def _plain_message(config, instrument: str, status: str, body: str):
    """A plain-text email carrying the summary verbatim."""
    from email.message import EmailMessage

    message = EmailMessage()
    message["Subject"] = f"[CFDInvest] Live-Execution Helper — {instrument} [{status}]"
    message["From"] = config.sender or "cfdinvest@localhost"
    message["To"] = ", ".join(getattr(config, "recipients", ()) or [])
    message.set_content(body)
    return message


def main(argv: Sequence[str] | None = None) -> int:
    """Parse, assess and print. Returns the verdict as an exit code."""
    args = build_parser().parse_args(argv)

    try:
        intent = parse_trade_intent(args.trade)
    except ValueError as error:
        print(f"Could not read that trade: {error}", file=sys.stderr)
        print(
            'Try: "long SPX500 0.68 entry=4434.8 stop=4390 tp=4520 ccy=USD lev=20"',
            file=sys.stderr,
        )
        return PARSE_FAILURE

    limits = _config(args)
    market = _market(args)

    assessment = evaluate_trade_safety(intent, limits, market)
    sizing = compute_safe_sizing(intent, limits, market)
    summary = build_execution_helper_summary(
        intent, assessment, sizing, config=limits
    )

    if not args.quiet:
        print(summary)
    if args.email:
        email_summary(intent.instrument, assessment.status, summary)

    return EXIT_CODES.get(assessment.status, PARSE_FAILURE)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
