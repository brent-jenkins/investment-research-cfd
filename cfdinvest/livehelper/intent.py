"""What you are about to do, before you do it.

A :class:`TradeIntent` is a *proposal*, not a record. Nothing in this package
places it, and nothing in this package can: there is no broker client here,
no HTTP call, and no automation of the Trading 212 interface. The helper
reads what you mean to type, and tells you what it implies.

**Missing values stay missing.** A trade with no stop, or a foreign
instrument with no exchange rate, cannot have its risk computed — and the
honest response is to say so, not to substitute a plausible default. A
default here would produce a confident number about a trade nobody can price,
which is worse than no number at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any, Mapping

__all__ = [
    "DIRECTIONS",
    "TradeIntent",
    "parse_trade_intent",
]

DIRECTIONS: tuple[str, ...] = ("long", "short")

#: Words a person actually types, mapped to the two directions.
_DIRECTION_WORDS: dict[str, str] = {
    "long": "long", "buy": "long", "b": "long", "l": "long",
    "short": "short", "sell": "short", "s": "short",
}

#: ``key=value`` or ``key: value``, tolerant of spaces around the separator.
#:
#: The value runs to whitespace, **not** to a comma: a comma inside a number
#: is a thousands separator, and stopping at it turned "entry=1,234.50" into
#: 1.0 — a wrong price rather than a refusal, which is the worst outcome
#: available to a parser in a safety tool.
_PAIR = re.compile(r"([A-Za-z_]+)\s*[:=]\s*([^\s;]+)")


@dataclass(frozen=True, slots=True)
class TradeIntent:
    """One intended trade.

    Attributes
    ----------
    entry_price:
        ``None`` means "at market". Every price-dependent figure then rests
        on the last price supplied in the market state, and the summary says
        which price it used.
    stop_loss:
        ``None`` means **no stop**, which makes the loss unbounded rather
        than large. That is not a number this helper will paper over.
    leverage:
        Trading 212 sets this per instrument and does not publish a machine
        list. Left ``None``, margin cannot be computed and is reported as
        unknown rather than guessed.
    """

    instrument: str
    direction: str
    quantity: float
    entry_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    account_currency: str = "GBP"
    instrument_currency: str = "GBP"
    leverage: float | None = None
    notes: str | None = None

    def __post_init__(self) -> None:
        if not str(self.instrument).strip():
            raise ValueError("instrument is required")
        if self.direction not in DIRECTIONS:
            raise ValueError(
                f"direction must be one of {DIRECTIONS}, got {self.direction!r}"
            )
        if self.quantity <= 0:
            raise ValueError(
                f"quantity must be > 0, got {self.quantity}; direction carries "
                "the sign, so a short is a positive quantity"
            )
        for name in ("entry_price", "stop_loss", "take_profit"):
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise ValueError(f"{name} must be > 0 or None, got {value}")
        if self.leverage is not None and self.leverage <= 0:
            raise ValueError(f"leverage must be > 0 or None, got {self.leverage}")

    @property
    def sign(self) -> int:
        """``+1`` for a long, ``-1`` for a short."""
        return 1 if self.direction == "long" else -1

    @property
    def is_foreign(self) -> bool:
        """Whether the instrument is quoted outside the account currency."""
        return self.instrument_currency.upper() != self.account_currency.upper()

    @property
    def has_stop(self) -> bool:
        return self.stop_loss is not None

    def stop_distance(self, price: float | None = None) -> float | None:
        """Distance from entry to stop, in instrument currency.

        ``None`` when either end is unknown. Returns a **positive** distance
        regardless of direction — the sign lives in :attr:`sign`.
        """
        entry = self.entry_price if self.entry_price is not None else price
        if entry is None or self.stop_loss is None:
            return None
        return abs(entry - self.stop_loss)

    def target_distance(self, price: float | None = None) -> float | None:
        """Distance from entry to take-profit, in instrument currency."""
        entry = self.entry_price if self.entry_price is not None else price
        if entry is None or self.take_profit is None:
            return None
        return abs(self.take_profit - entry)

    def stop_is_on_the_correct_side(self, price: float | None = None) -> bool | None:
        """Whether the stop sits *below* a long and *above* a short.

        ``None`` when it cannot be judged. A stop on the wrong side is not a
        stop — it is a resting order that fires immediately, and it is an
        easy thing to fat-finger at speed.
        """
        entry = self.entry_price if self.entry_price is not None else price
        if entry is None or self.stop_loss is None:
            return None
        return self.stop_loss < entry if self.direction == "long" else (
            self.stop_loss > entry
        )

    def target_is_on_the_correct_side(self, price: float | None = None) -> bool | None:
        """Whether the take-profit sits *above* a long and *below* a short."""
        entry = self.entry_price if self.entry_price is not None else price
        if entry is None or self.take_profit is None:
            return None
        return self.take_profit > entry if self.direction == "long" else (
            self.take_profit < entry
        )

    def resized(self, quantity: float) -> "TradeIntent":
        """A copy at a different size. Pure — the original is untouched."""
        return replace(self, quantity=float(quantity))


def _number(value: Any) -> float | None:
    """Parse a number, tolerating currency symbols, commas and blanks."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    for symbol in ("£", "$", "€", "%"):
        text = text.replace(symbol, "")
    if not text or text.lower() in {"none", "null", "market", "-", "n/a"}:
        return None
    try:
        return float(text)
    except ValueError:
        raise ValueError(f"could not read {value!r} as a number") from None


def parse_trade_intent(raw_input: Any) -> TradeIntent:
    """Build a :class:`TradeIntent` from a mapping or a line of text.

    Accepts either a dictionary, or a string in the shape a person would
    type::

        long SPX500 0.68 entry=4434.8 stop=4390 tp=4520 ccy=USD lev=20

    The instrument and direction may appear in any order; everything else is
    ``key=value``. Keys are forgiving — ``tp``, ``take_profit`` and
    ``takeprofit`` all work — because the alternative is a helper that
    refuses to answer over a synonym.

    Raises
    ------
    ValueError
        On anything it cannot read. A misparsed field would silently change
        the risk, so a wrong guess is worse than a refusal.
    """
    if isinstance(raw_input, TradeIntent):
        return raw_input
    if isinstance(raw_input, Mapping):
        return _from_mapping(raw_input)
    if isinstance(raw_input, str):
        return _from_mapping(_from_text(raw_input))
    raise ValueError(
        f"cannot read a trade intent from {type(raw_input).__name__}; "
        "pass a mapping or a string"
    )


#: Every spelling of a field, mapped to its canonical name.
_ALIASES: dict[str, str] = {
    "instrument": "instrument", "ticker": "instrument", "symbol": "instrument",
    "direction": "direction", "side": "direction",
    "quantity": "quantity", "qty": "quantity", "size": "quantity",
    "entry": "entry_price", "entry_price": "entry_price", "price": "entry_price",
    "stop": "stop_loss", "sl": "stop_loss", "stop_loss": "stop_loss",
    "stoploss": "stop_loss",
    "tp": "take_profit", "target": "take_profit", "take_profit": "take_profit",
    "takeprofit": "take_profit",
    "ccy": "instrument_currency", "currency": "instrument_currency",
    "instrument_currency": "instrument_currency",
    "account_currency": "account_currency", "account_ccy": "account_currency",
    "lev": "leverage", "leverage": "leverage",
    "notes": "notes", "note": "notes",
}


def _from_text(line: str) -> dict[str, Any]:
    """Pull fields out of a typed line."""
    fields: dict[str, Any] = {}
    for key, value in _PAIR.findall(line):
        canonical = _ALIASES.get(key.lower())
        if canonical:
            fields[canonical] = value

    # Whatever is left over, in order: the direction word, the instrument,
    # and a bare number for the quantity.
    remainder = _PAIR.sub(" ", line).split()
    for token in remainder:
        lowered = token.lower()
        if lowered in _DIRECTION_WORDS and "direction" not in fields:
            fields["direction"] = _DIRECTION_WORDS[lowered]
        elif "quantity" not in fields and _looks_numeric(token):
            fields["quantity"] = token
        elif "instrument" not in fields:
            fields["instrument"] = token

    return fields


def _looks_numeric(token: str) -> bool:
    try:
        float(token.replace(",", ""))
    except ValueError:
        return False
    return True


def _from_mapping(fields: Mapping[str, Any]) -> TradeIntent:
    """Normalise a mapping of possibly-aliased keys into an intent."""
    normalised: dict[str, Any] = {}
    for key, value in fields.items():
        canonical = _ALIASES.get(str(key).lower().replace(" ", "_"))
        if canonical:
            normalised[canonical] = value

    missing = [f for f in ("instrument", "direction", "quantity")
               if normalised.get(f) in (None, "")]
    if missing:
        raise ValueError(
            f"a trade intent needs {', '.join(missing)}; got "
            f"{sorted(normalised) or 'nothing usable'}"
        )

    direction = str(normalised["direction"]).strip().lower()
    if direction not in _DIRECTION_WORDS:
        raise ValueError(
            f"direction {normalised['direction']!r} is not one of "
            f"{sorted(set(_DIRECTION_WORDS))}"
        )

    return TradeIntent(
        instrument=str(normalised["instrument"]).strip().upper(),
        direction=_DIRECTION_WORDS[direction],
        quantity=_number(normalised["quantity"]),
        entry_price=_number(normalised.get("entry_price")),
        stop_loss=_number(normalised.get("stop_loss")),
        take_profit=_number(normalised.get("take_profit")),
        account_currency=str(
            normalised.get("account_currency", "GBP")
        ).strip().upper(),
        instrument_currency=str(
            normalised.get("instrument_currency", "GBP")
        ).strip().upper(),
        leverage=_number(normalised.get("leverage")),
        notes=(
            str(normalised["notes"]) if normalised.get("notes") is not None else None
        ),
    )
