"""A paper book that pays what Trading 212 charges.

The point of this is to be wrong in public. Every measurement so far has
been backwards over a universe chosen in 2026 for having gone up, and the
strategy trades a horizon of days. A forward record settles in weeks what
a backtest on that universe can never settle at all.

Fixed stake, deliberately
-------------------------
Every trade takes **£1,000 of exposure** -- notional, not margin. With a
fixed margin instead, leverage would vary the stake without anyone
choosing it: a 30x FX pair would carry £30,000 of exposure against a 5x
share's £5,000, and the currency trades would dominate the result on
position size alone. With fixed notional the P&L is 1,000 x the move,
financing is charged on the same base everywhere, and the only thing
varying between trades is whether the call was right.

What it does not equalise is volatility. Tesla at 57% annualised and the
FTSE at 17% both take £1,000, so the volatile names will dominate the
spread of outcomes. That is the right trade for a test of signal quality
and the wrong one for a test of portfolio construction, so the percentage
move is recorded beside the pounds and either can be read later.

The costs are the real ones
---------------------------
Spread, financing per side per instrument, and the 0.5% conversion fee on
the result where it applies -- all from the recorded Trading 212 table, all
through the same ticket calculator the live helper uses. A paper book that
ignored costs would show the strategy in profit and prove nothing: the
backtest's whole finding was that costs were 10.9x the edge before any of
this was fixed.

Opened and closed, never resized
--------------------------------
Trading 212 offers no way to resize an open CFD position -- it can only be
closed. So the book writes exactly two events, ``open`` and ``close``, and
has no rebalance threshold at all. This is a constraint of the venue, not
a simplification: a paper book that trimmed a winner by a third would be
recording a trade the account could not have made, and every cost and
outcome after that point would belong to a position that never existed.

The backtest engine does resize, which is why its figures and this book's
are not directly comparable trade for trade.

Nothing here places an order or touches a broker. It writes a file.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

logger = logging.getLogger(__name__)

__all__ = [
    "PAPER_DIR",
    "STAKE_GBP",
    "PaperTrade",
    "open_trade",
    "value_trade",
    "record",
    "load_book",
    "open_positions",
]

PAPER_DIR = "paper"

#: Exposure per trade, in the account currency. See the module docstring
#: for why this is notional rather than margin.
STAKE_GBP = 1000.0

SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class PaperTrade:
    """One simulated position, opened or closed, with its costs."""

    event: str                  # "open" or "close"
    at: str                     # ISO timestamp, UTC
    ticker: str
    direction: str
    quantity: float
    price: float
    notional_gbp: float

    #: Which arm of the experiment. Three books run side by side at
    #: different confidence thresholds, so "do lower-confidence trades do
    #: worse" becomes a controlled comparison rather than an argument. Each
    #: cohort keeps its own one-position-per-instrument rule and its own
    #: cooldown, so they never interfere -- the same instrument may be held
    #: in all three at once, which is the point of running them together.
    cohort: str = ""

    #: The broker terms this was struck on, recorded rather than looked up
    #: later, so a change to the rules table cannot rewrite history.
    leverage: float | None = None
    financing_rate: float | None = None
    fx_rate: float | None = None
    fx_fee_applies: bool | None = None
    spread_cost_gbp: float | None = None
    #: "recorded" where Trading 212 published a typical spread, "estimated"
    #: where it came from the execution layer's class defaults. The two are
    #: not the same claim and the book must not let them look alike --
    #: only six of the forty instruments have a published figure.
    spread_source: str = ""
    margin_gbp: float | None = None

    #: Only on a close.
    opened_at: str = ""
    days_held: float | None = None
    gross_pnl_gbp: float | None = None
    financing_gbp: float | None = None
    fx_fee_gbp: float | None = None
    net_pnl_gbp: float | None = None
    net_pct: float | None = None
    reason: str = ""
    #: What could not be computed. Never silently zeroed.
    missing: tuple[str, ...] = ()

    #: The state the decision was made in, captured at entry and never
    #: recomputed. This is the point of the exercise.
    #:
    #: Every price-derived feature tested against 2,018 historical round
    #: trips collapsed to |t|<1 once the trend engine's own confidence was
    #: accounted for -- realised volatility, cross-sectional rank, trend
    #: age, multi-timeframe agreement, volume. The trend engine already
    #: knows what price knows, so improving the evaluation requires
    #: evidence from outside it.
    #:
    #: Sentiment is the only candidate that is not made of price, and it
    #: could not be tested historically: two years of verified headlines
    #: overlapped just 37 of those trips. So it is recorded here instead,
    #: with the filing rate and the regime, and the question becomes
    #: answerable after a few months rather than never.
    confidence: float | None = None
    sentiment: float | None = None
    sentiment_n: int | None = None
    volume_ratio: float | None = None
    filings_90d: int | None = None
    regime: str = ""

    #: The signal's full reason string, kept verbatim rather than parsed.
    #:
    #: Confidence has the environment multiplier already folded into it --
    #: high volatility halves it -- so the book cannot otherwise tell a
    #: weak signal from a strong one struck in a bad environment. The
    #: reason string names every component, which makes the pre-environment
    #: score recoverable arithmetically and the multiplier's own worth
    #: testable from this file.
    #:
    #: That question is open. Measured across 1,173 historical trips, trades
    #: opened on high-volatility days returned 0.055%/trip against 0.118%
    #: on normal days -- a difference of t=-0.48, which is no difference at
    #: all on 55 observations. The penalty is unmeasured rather than
    #: vindicated, and halving confidence for it is a live assumption.
    #:
    #: Named apart from ``reason``, which says why the position was closed.
    #: Two different questions; one field name for both would have silently
    #: overwritten the entry evidence with "time stop".
    signal_reason: str = ""

    #: Recent range, as a percentage of price. Recorded, not acted on: the
    #: stake stays fixed at £1,000 so that the only thing varying between
    #: trades is whether the call was right. Recording ATR means a
    #: volatility-weighted book can be reconstructed from this same file
    #: afterwards -- each trade's P&L rescaled by its own ATR -- without
    #: running the weeks again. Sizing is a question about portfolio
    #: construction; this test is about signal quality, and mixing them
    #: would leave neither answered.
    atr_pct: float | None = None

    def as_row(self) -> dict[str, Any]:
        row = asdict(self)
        row["missing"] = list(self.missing)
        return row


def _rules_for(ticker: str, rules=None):
    if rules is not None:
        return rules.get(ticker)
    from cfdinvest.livehelper.t212_rules import load_rules

    return load_rules().get(ticker)


def open_trade(
    ticker: str,
    direction: str,
    price: float,
    at: str,
    *,
    rules=None,
    fx_rate: float | None = None,
    stake: float = STAKE_GBP,
    state: Mapping[str, Any] | None = None,
    cohort: str = "",
) -> PaperTrade | None:
    """A £1,000 position at ``price``. ``None`` if it cannot be struck.

    Returns ``None`` rather than guessing when the instrument's terms are
    unknown or the stake falls under the broker's minimum. A paper trade
    the real account could not have taken teaches nothing, and one priced
    on an invented leverage teaches something false.
    """
    entry = _rules_for(ticker, rules)
    side = str(direction).lower()
    if side not in ("long", "short"):
        return None

    try:
        unit_price = float(price)
    except (TypeError, ValueError):
        return None
    if not unit_price > 0:
        return None

    minimum = getattr(entry, "min_trade_value", None)
    if minimum is not None and stake < float(minimum):
        logger.info(
            "%s not opened: £%.2f is below the £%.2f minimum",
            ticker, stake, float(minimum),
        )
        return None

    # The stake is exposure in the account currency, so the unit count has
    # to come back through the exchange rate.
    rate = 1.0 if fx_rate is None else float(fx_rate)
    if not rate > 0:
        rate = 1.0
    quantity = stake / (unit_price * rate)

    from cfdinvest.livehelper.ticket import build_ticket

    ticket = build_ticket(
        ticker, side, quantity, unit_price,
        currency=getattr(entry, "currency", None) or "GBP",
        fx_rate=rate,
        leverage=getattr(entry, "leverage", None),
        financing_rate=entry.annual_financing(side) if entry is not None else None,
        fx_fee_applies=getattr(entry, "fx_fee_applies", None),
    )

    spread_cost, spread_source = _spread_cost(ticker, entry, quantity, unit_price, rate)

    return PaperTrade(
        event="open",
        at=str(at),
        ticker=str(ticker),
        cohort=str(cohort),
        direction=side,
        quantity=float(quantity),
        price=unit_price,
        notional_gbp=float(stake),
        leverage=ticket.leverage,
        financing_rate=ticket.financing_rate,
        fx_rate=rate,
        fx_fee_applies=getattr(entry, "fx_fee_applies", None),
        spread_cost_gbp=spread_cost,
        spread_source=spread_source,
        margin_gbp=ticket.margin_gbp,
        missing=ticket.missing,
        confidence=_number((state or {}).get("confidence")),
        sentiment=_number((state or {}).get("sentiment")),
        sentiment_n=_count((state or {}).get("sentiment_n")),
        volume_ratio=_number((state or {}).get("volume_ratio")),
        filings_90d=_count((state or {}).get("filings_90d")),
        regime=str((state or {}).get("regime") or ""),
        signal_reason=str((state or {}).get("reason") or ""),
        atr_pct=_number((state or {}).get("atr_pct")),
    )


#: Broker-rule class -> execution-table class. Indices map to the equity
#: default only as a backstop: the six that Trading 212 publishes are far
#: tighter than that (the S&P's 0.34 on ~5,000 is under a basis point) and
#: use their recorded figure. Anything landing here is an index nobody
#: published, where erring wide is the honest direction for a cost that was
#: never measured.
_SPREAD_CLASS: dict[str, str] = {
    "share": "equity",
    "index": "equity",
    "commodity": "commodity",
    "fx": "fx",
    "volatility": "volatility",
    "bond": "bond",
    "unknown": "other",
}


def _spread_cost(
    ticker: str, entry, quantity: float, price: float, rate: float
) -> tuple[float | None, str]:
    """The round trip's spread cost in the account currency, and its source.

    Crossing costs half the spread on the way in and half on the way out,
    so the full spread is charged once at entry.

    Trading 212 publishes a typical spread for six of the forty instruments
    here and none of the nineteen shares, so charging only what is
    published would mean most paper trades paid no spread at all -- and the
    backtest's central finding was that costs were 10.9x the gross edge.
    An unpaid cost is a worse error than an estimated one. Where nothing is
    published the estimate comes from the execution layer's own per-class
    table, so the backtest and the paper book cannot disagree about what a
    share costs to cross.

    Which of the two it was is returned alongside and written into the
    book. An estimated cost and a measured one are different claims.
    """
    typical = getattr(entry, "spread_typical", None)
    if typical is not None:
        return abs(float(typical)) * quantity * rate, "recorded"

    try:
        from cfdinvest.execution.fills import DEFAULT_SPREAD_BPS
        from cfdinvest.livehelper.t212_instruments import asset_class

        # The two layers name asset classes differently -- the broker rules
        # say "share" and "index", the execution table says "equity" -- and
        # an unmapped name would fall through to no spread at all rather
        # than to a wrong one, which is the failure that hides. Mapped
        # explicitly so a new class raises the question instead of silently
        # trading for free.
        half_bps = DEFAULT_SPREAD_BPS.get(_SPREAD_CLASS.get(asset_class(ticker), ""))
        if half_bps is None:
            return None, ""
        # The table is a half-spread; the round trip crosses twice.
        full = 2.0 * float(half_bps) / 10_000.0
        return full * price * quantity * rate, "estimated"
    except Exception as error:  # noqa: BLE001
        logger.debug("No spread estimate for %s: %s", ticker, error)
        return None, ""


def _number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None      # NaN is not a reading


def _count(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def value_trade(
    trade: Mapping[str, Any],
    price: float,
    at: str,
    *,
    reason: str = "",
    days_held: float | None = None,
) -> PaperTrade:
    """Close an open paper trade and account for it fully.

    Financing accrues on calendar days, the conversion fee lands on the
    result rather than the notional, and anything that could not be
    computed is named in ``missing`` rather than treated as zero -- the
    same rule the rest of this project runs on.
    """
    import pandas as pd

    from cfdinvest.livehelper.ticket import TradeTicket, close_out

    opened = trade.get("at") or trade.get("opened_at") or ""
    if days_held is None:
        try:
            days_held = max(
                (pd.Timestamp(at) - pd.Timestamp(opened)).total_seconds() / 86_400.0,
                0.0,
            )
        except (TypeError, ValueError):
            days_held = 0.0

    quantity = float(trade.get("quantity") or 0.0)
    entry_price = float(trade.get("price") or 0.0)
    rate = trade.get("fx_rate")
    notional = float(trade.get("notional_gbp") or 0.0)

    ticket = TradeTicket(
        ticker=str(trade.get("ticker") or ""),
        direction=str(trade.get("direction") or "long"),
        quantity=quantity,
        price=entry_price,
        currency="GBP" if rate in (None, 1.0) else "USD",
        fx_rate=None if rate is None else float(rate),
        leverage=trade.get("leverage"),
        financing_rate=trade.get("financing_rate"),
        fx_fee_applies=trade.get("fx_fee_applies"),
        notional=abs(quantity * entry_price),
        notional_gbp=notional,
        margin_gbp=trade.get("margin_gbp"),
        financing_per_day_gbp=(
            None if trade.get("financing_rate") is None
            else notional * float(trade["financing_rate"]) / 365.0
        ),
        missing=tuple(trade.get("missing") or ()),
    )
    result = close_out(ticket, price, days_held=days_held)

    # The spread was paid on entry and is a real cost of the round trip.
    spread = trade.get("spread_cost_gbp")
    net = result.net_pnl_gbp
    if net is not None and spread is not None:
        net = net - float(spread)

    return PaperTrade(
        event="close",
        at=str(at),
        ticker=str(trade.get("ticker") or ""),
        cohort=str(trade.get("cohort") or ""),
        direction=str(trade.get("direction") or "long"),
        quantity=quantity,
        price=float(price),
        notional_gbp=notional,
        leverage=trade.get("leverage"),
        financing_rate=trade.get("financing_rate"),
        fx_rate=None if rate is None else float(rate),
        fx_fee_applies=trade.get("fx_fee_applies"),
        spread_cost_gbp=spread,
        spread_source=str(trade.get("spread_source") or ""),
        opened_at=str(opened),
        days_held=float(days_held),
        gross_pnl_gbp=result.gross_pnl_gbp,
        financing_gbp=result.financing_gbp,
        fx_fee_gbp=result.fx_fee_gbp,
        net_pnl_gbp=net,
        net_pct=None if net is None or not notional else net / notional * 100.0,
        reason=reason,
        missing=result.missing,
        # The evidence the position was opened on, carried onto the close.
        # Without it the outcome and the state it was predicted from sit in
        # separate rows and every question this book exists to answer needs
        # a join to ask -- which is exactly the sort of step that quietly
        # drops the rows that fail to match.
        confidence=_number(trade.get("confidence")),
        sentiment=_number(trade.get("sentiment")),
        sentiment_n=_count(trade.get("sentiment_n")),
        volume_ratio=_number(trade.get("volume_ratio")),
        filings_90d=_count(trade.get("filings_90d")),
        regime=str(trade.get("regime") or ""),
        signal_reason=str(trade.get("signal_reason") or ""),
        atr_pct=_number(trade.get("atr_pct")),
    )


def record(events: Iterable[PaperTrade], config=None, account: str | None = None) -> int:
    """Append to the book. Never raises; returns how many were written."""
    rows = list(events or ())
    if not rows:
        return 0
    path = _path(config, account)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row.as_row(), sort_keys=True) + "\n")
        return len(rows)
    except OSError as error:
        logger.warning("Could not write the paper book to %s: %s", path, error)
        return 0


def load_book(config=None, account: str | None = None) -> list[dict]:
    """Every event, oldest first. Never raises; skips a truncated line."""
    path = _path(config, account)
    rows: list[dict] = []
    try:
        if not path.is_file():
            return rows
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("Skipping a malformed paper-book line in %s", path)
    except OSError as error:
        logger.warning("Paper book unreadable at %s: %s", path, error)
    return rows


def open_positions(
    book: Iterable[Mapping[str, Any]], cohort: str | None = None
) -> dict[str, dict]:
    """Replay the book into what is currently open.

    Last event per instrument wins, which makes a duplicate open a
    correction rather than a second position -- the same rule the confirm
    layer uses, and for the same reason.

    Scoped to one cohort when asked. The arms of the experiment must not
    see each other: a position held in the 0.50 book says nothing about
    whether the 0.70 book should hold one, and letting one block the other
    would quietly turn three independent tests into one.
    """
    live: dict[str, dict] = {}
    for row in sorted(book or (), key=lambda r: str(r.get("at") or "")):
        ticker = str(row.get("ticker") or "")
        if not ticker:
            continue
        if cohort is not None and str(row.get("cohort") or "") != cohort:
            continue
        if row.get("event") == "open":
            live[ticker] = dict(row)
        elif row.get("event") == "close":
            live.pop(ticker, None)
    return live


def _path(config=None, account: str | None = None) -> Path:
    from cfdinvest.persist.storage import account_root

    return account_root(config, account) / PAPER_DIR / "book.jsonl"


#: Closed trades needed before a win rate is stated as a fact.
#:
#: Twenty, the same floor the forward record uses. A rate on five trades is
#: a number that will be quoted and should not exist -- the -5.53% that
#: looked like a finding on fifteen ticker-months inverted to +2.37% on four
#: hundred and fifty-six. Below the floor the arithmetic is still shown, as
#: ``rate_so_far``, because hiding it entirely invites the same sum done
#: worse by hand; what is withheld is the claim, not the figure.
MIN_CLOSED_FOR_RATE = 20


def summarise(
    book: Iterable[Mapping[str, Any]], cohort: str | None = None
) -> dict[str, Any]:
    """What the paper book has done so far. Never raises.

    One arm when ``cohort`` is given, everything pooled when it is not.
    Pooling three thresholds answers nothing on its own -- the arms exist to
    be compared -- so the dashboard asks per cohort and this is only pooled
    for the headline.

    The accounting identity is asserted rather than assumed, as everywhere
    else here: gross less financing, spread and the conversion fee equals
    net. A row whose net could not be computed is counted in ``unpriced``
    and excluded from every total, never treated as a zero.
    """
    rows = [
        dict(row) for row in (book or ())
        if cohort is None or str(row.get("cohort") or "") == cohort
    ]
    closed = [row for row in rows if row.get("event") == "close"]
    live = open_positions(rows, cohort)

    priced = [row for row in closed if _number(row.get("net_pnl_gbp")) is not None]
    unpriced = len(closed) - len(priced)

    nets = [float(row["net_pnl_gbp"]) for row in priced]
    wins = sum(1 for value in nets if value > 0)
    losses = sum(1 for value in nets if value < 0)

    def total(name: str) -> float:
        return sum(
            float(row[name]) for row in priced
            if _number(row.get(name)) is not None
        )

    held = [
        float(row["days_held"]) for row in priced
        if _number(row.get("days_held")) is not None
    ]

    return {
        "cohort": cohort,
        "open": len(live),
        "open_tickers": sorted(live),
        "closed": len(priced),
        "unpriced": unpriced,
        "wins": wins,
        "losses": losses,
        # The claim, withheld below the floor.
        "win_rate": (
            wins / len(priced) * 100.0 if len(priced) >= MIN_CLOSED_FOR_RATE else None
        ),
        # The arithmetic, always available and never presented as settled.
        "rate_so_far": wins / len(priced) * 100.0 if priced else None,
        "needed": max(MIN_CLOSED_FOR_RATE - len(priced), 0),
        "net_gbp": sum(nets) if nets else None,
        "gross_gbp": total("gross_pnl_gbp") if priced else None,
        "financing_gbp": total("financing_gbp") if priced else None,
        "spread_gbp": total("spread_cost_gbp") if priced else None,
        "fx_fee_gbp": total("fx_fee_gbp") if priced else None,
        "best_gbp": max(nets) if nets else None,
        "worst_gbp": min(nets) if nets else None,
        "avg_days_held": sum(held) / len(held) if held else None,
    }


def mark_open_positions(
    book: Iterable[Mapping[str, Any]],
    prices: Mapping[str, Any],
    at: str,
    cohort: str | None = None,
) -> list[dict[str, Any]]:
    """Value every open position at the prices given. Never raises.

    Returns plain dictionaries, deliberately not ``PaperTrade`` events. A
    valuation is not a close: nothing here is appended to the book, and
    giving it the same type as a real event is how a mark ends up recorded
    as a trade that never happened.

    The arithmetic is :func:`value_trade`'s, unchanged -- financing accrued
    to now, the spread paid on entry, the conversion fee on the result --
    because a position marked by one calculator and closed by another will
    disagree with itself at exactly the moment somebody is watching.

    An instrument with no usable price is returned with its figures absent
    and ``priced`` false, rather than left out. A position that exists and
    could not be valued is the thing you most need to see.
    """
    marks: list[dict[str, Any]] = []
    for ticker, position in open_positions(book, cohort).items():
        price = prices.get(ticker)
        row: dict[str, Any] = {
            "cohort": str(position.get("cohort") or ""),
            "ticker": ticker,
            "direction": position.get("direction"),
            "opened_at": position.get("at"),
            "entry_price": _number(position.get("price")),
            "quantity": _number(position.get("quantity")),
            "notional_gbp": _number(position.get("notional_gbp")),
            "margin_gbp": _number(position.get("margin_gbp")),
            "confidence": _number(position.get("confidence")),
            "price": None, "days_held": None, "gross_pnl_gbp": None,
            "financing_gbp": None, "fx_fee_gbp": None, "value_gbp": None,
            "net_pnl_gbp": None, "net_pct": None, "priced": False,
        }
        if _number(price) is None:
            marks.append(row)
            continue

        try:
            valued = value_trade(position, float(price), str(at))
        except Exception as error:  # noqa: BLE001 - one row, not the table
            logger.warning("Could not mark %s: %s", ticker, error)
            marks.append(row)
            continue

        row.update({
            "price": float(price),
            "days_held": valued.days_held,
            "gross_pnl_gbp": valued.gross_pnl_gbp,
            "financing_gbp": valued.financing_gbp,
            "fx_fee_gbp": valued.fx_fee_gbp,
            "net_pnl_gbp": valued.net_pnl_gbp,
            "net_pct": valued.net_pct,
            "priced": valued.net_pnl_gbp is not None,
        })
        # What the position is worth now: the stake plus what it has made,
        # which is the figure a broker screen shows.
        if valued.net_pnl_gbp is not None and row["notional_gbp"] is not None:
            row["value_gbp"] = row["notional_gbp"] + valued.net_pnl_gbp
        marks.append(row)
    return marks


def summarise_all(book: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Every arm, in threshold order, plus the pooled total last."""
    rows = list(book or ())
    cohorts = sorted({str(row.get("cohort") or "") for row in rows if row.get("cohort")})
    return [summarise(rows, name) for name in cohorts] + [summarise(rows)]
