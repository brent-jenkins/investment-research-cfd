"""Feeding the recorded broker terms into the backtest's cost model.

The rules table in :mod:`cfdinvest.livehelper.t212_rules` was built for the
live helper: it answers "may I place this, and what will the ticket say".
The backtest was answering a different question with different numbers -- a
flat 5% benchmark plus a 3% markup for every instrument in the universe --
and the gap between the two was not small.

Measured on the run of 2026-08-25, over its 1,692 completed round trips:

===================  ===========  ===========
                        modelled       actual
===================  ===========  ===========
long financing          £35,678      £43,924
short financing         -£4,316      £12,104
**total**              £31,361      £56,027
===================  ===========  ===========

Two separate errors. The first is a magnitude: a US share CFD costs 25.33% a
year to hold long, not 8%. The second is a **sign**. The benchmark-and-markup
model has a short *earning* 2% a year, which is what a short does when it is
funded by lending the stock out; Trading 212 charges 9.16% on the same
position. Every short in the backtest was being paid to exist. There were
451 of them.

This module does not decide anything. It reads what was recorded and shapes
it into what :class:`~cfdinvest.execution.frame.ExecutionConfig` accepts, so
that the rates the helper quotes you and the rates the backtest charges you
are the same numbers from the same file. Two copies drift, and the day they
drift the backtest says one thing and the ticket says another.

Instruments with nothing recorded are **reported, not defaulted**. The caller
gets the list back and decides; silently financing an unrecorded instrument
at the house rate would hide exactly the gap this exists to close.
"""

from __future__ import annotations

import logging
from typing import Iterable

logger = logging.getLogger(__name__)

__all__ = [
    "financing_overrides",
    "instrument_currencies",
    "fx_fee_flags",
    "unpriced",
]


def financing_overrides(
    tickers: Iterable[str] | None = None,
    table=None,
) -> dict[str, tuple[float, float]]:
    """``{ticker: (long_rate, short_rate)}`` for everything with both sides.

    Annual fractions, signed the way :class:`FinancingRates` signs them:
    positive is a charge. Trading 212 charges both sides on every instrument
    recorded so far, so every rate here is positive -- which is itself the
    correction, since the model being replaced made shorts an income.

    An instrument missing either side is left out entirely rather than
    half-filled. A position financed on one side and free on the other is not
    a conservative estimate of anything.
    """
    table = _table(table)
    wanted = _wanted(tickers, table)

    overrides: dict[str, tuple[float, float]] = {}
    for ticker in wanted:
        rules = table.get(ticker)
        long_rate = rules.annual_financing("long")
        short_rate = rules.annual_financing("short")
        if long_rate is None or short_rate is None:
            continue
        overrides[str(ticker)] = (float(long_rate), float(short_rate))
    return overrides


def instrument_currencies(
    tickers: Iterable[str] | None = None,
    table=None,
) -> dict[str, str]:
    """``{ticker: currency}`` for everything with one recorded.

    Drives whether an instrument needs an exchange rate at all. Unrecorded
    instruments are omitted, which the FX layer reads as domestic -- it
    understates the cost visibly rather than inventing a currency.
    """
    table = _table(table)
    currencies: dict[str, str] = {}
    for ticker in _wanted(tickers, table):
        currency = getattr(table.get(ticker), "currency", None)
        if currency:
            currencies[str(ticker)] = str(currency).upper()
    return currencies


def fx_fee_flags(
    tickers: Iterable[str] | None = None,
    table=None,
) -> dict[str, bool]:
    """``{ticker: whether the 0.5% conversion fee is charged}``.

    Recorded per instrument and never derived from the currency, because the
    currency does not predict it. Of the instruments recorded so far, Germany
    40, France 40, Europe 50 and Japan 225 are all quoted abroad and charge
    no fee, while USA 500 is funded in sterling and charges one.

    Only ``True``/``False`` entries appear. An instrument whose flag was
    never read is absent, and the FX layer falls back to the currency rule
    for it -- the behaviour before any of this was recorded.
    """
    table = _table(table)
    flags: dict[str, bool] = {}
    for ticker in _wanted(tickers, table):
        applies = getattr(table.get(ticker), "fx_fee_applies", None)
        if applies is not None:
            flags[str(ticker)] = bool(applies)
    return flags


def unpriced(tickers: Iterable[str], table=None) -> tuple[str, ...]:
    """Which of ``tickers`` have no usable financing pair recorded.

    The point of returning this rather than logging it: a run that financed
    part of its universe at the broker's rates and the rest at a house
    default has a cost figure that is neither, and nothing about the number
    itself would say so.
    """
    overrides = financing_overrides(tickers, table)
    return tuple(
        str(ticker) for ticker in (tickers or ()) if str(ticker) not in overrides
    )


def _table(table=None):
    if table is not None:
        return table
    from cfdinvest.livehelper.t212_rules import load_rules

    return load_rules()


def _wanted(tickers, table):
    if tickers is not None:
        return [str(ticker) for ticker in tickers]
    # RuleTable.tickers is a property; tolerate a callable so a stub table
    # in a test does not have to imitate that detail to be useful.
    listing = getattr(table, "tickers", ())
    return [str(ticker) for ticker in (listing() if callable(listing) else listing)]
