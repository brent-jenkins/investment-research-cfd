"""What actually happened, as against what was recommended.

Everything upstream of this module assumes the recommendation was followed
exactly: the engine sizes a position, the analytics score that position, and
the dashboard shows it. A confirmation is where reality is admitted — the
fill you actually got, the quantity you actually took, or the fact that you
looked at the recommendation and passed.

**Append-only, one file per confirmation.** A confirmation is an observation
of a moment that does not come back, so nothing here edits or deletes. A
mistyped fill is corrected by recording a correction, not by rewriting the
original: the first record is still true — it is what you believed at the
time — and a system that quietly rewrote it could not show you that you had
been wrong.

Writes are atomic. Reads never raise.

Three things this module is careful about
-----------------------------------------
**Quantity is signed.** Negative is short. The recommendation schema carries a
direction and ``positions.parquet`` holds signed units, so an unsigned
confirmed quantity would need a direction inferred from somewhere — and the
only place to infer it from is the simulated position this confirmation
exists to override. :func:`Confirmation.of` takes ``direction`` as sugar and
applies the sign once, here.

**Not confirming is not the same as declining.** Silence means the simulated
position stands, because that is what the engine believes and nothing has
contradicted it. Declining is the explicit ``"ignore"`` action. Collapsing the
two would make an unconfirmed run look like a day of refused trades.

**A bad confirmation is refused at the door.** A confirmation overrides the
engine, so a fat-fingered fill price does not produce a suspicious number
downstream — it produces a wrong one that looks exactly as authoritative as a
right one. Validation happens on the way in.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Mapping

import pandas as pd

from cfdinvest.persist.storage import account_root, atomic_write_text

logger = logging.getLogger(__name__)

__all__ = [
    "CONFIRMATIONS_DIR",
    "SCHEMA_VERSION",
    "ACTIONS",
    "Confirmation",
    "confirmations_root",
    "write_confirmation",
    "load_confirmations",
    "load_latest_confirmation",
    "list_confirmations",
    "confirmed_positions",
    "merge_simulated_and_confirmed_positions",
    "deviations",
]

CONFIRMATIONS_DIR = "confirmations"
SCHEMA_VERSION = 1

#: ``open`` and ``close`` are one leg each; ``flip`` is a close and an open in
#: the same instruction, which Trading 212 does as two tickets but a trader
#: thinks of as one decision. ``ignore`` is the spec's "ignored trades": it
#: says a recommendation was seen and declined, which silence cannot say.
ACTIONS: tuple[str, ...] = ("open", "close", "flip", "ignore")

#: ``YYYYMMDD-HHMMSS-ACTION-TICKER.json`` — sorts in time order, and says what
#: it is without being opened.
FILENAME_FORMAT = "%Y%m%d-%H%M%S"

_TICKER = re.compile(r"^[A-Za-z0-9._\-]{1,32}$")


def _utc(value: Any) -> pd.Timestamp:
    moment = pd.Timestamp(value) if value is not None else pd.Timestamp.now(tz="UTC")
    return moment.tz_localize("UTC") if moment.tz is None else moment.tz_convert("UTC")


@dataclass(frozen=True, slots=True)
class Confirmation:
    """One thing that happened, in the account, at a moment.

    Attributes
    ----------
    quantity:
        **Signed** units. Negative is short. Zero is only valid for
        ``close`` (the position went away) and ``ignore``.
    price:
        The fill you actually got. For a ``flip`` this is the price of the
        *opening* leg; ``close_price`` carries the other one.
    exposure:
        Notional in the account currency. Computed from price and quantity
        when omitted, but accepted explicitly because the Trading 212 ticket
        shows a figure and re-deriving it invites a disagreement nobody can
        settle later.
    currency, fx_rate, leverage, financing_rate:
        The terms the position was actually opened on. A US share CFD is
        priced in dollars against a sterling account, so without the rate
        the GBP figures are unknowable rather than approximately right --
        and leverage decides the margin. Recorded because they are true of
        *that fill*: the instrument's leverage can change, and a position
        opened last month was opened on last month's terms.
    recommended_quantity, recommended_price, run_id:
        What the system had suggested, recorded alongside what happened.
        Not in the original schema, and worth having: runs are pruned
        (``keep_runs``), so a recommendation is not always recoverable when
        you later want to ask how far your fills drifted from it.
    """

    ticker: str
    action: str
    price: float | None = None
    quantity: float = 0.0
    exposure: float | None = None
    timestamp: pd.Timestamp = field(default_factory=lambda: pd.Timestamp.now(tz="UTC"))
    source: str = "manual"
    notes: str | None = None

    close_price: float | None = None
    recommended_quantity: float | None = None
    recommended_price: float | None = None
    run_id: str | None = None

    # The ticket's own terms. Additive with None defaults, so a
    # confirmation written before these existed still loads -- and None
    # means "not recorded", never a default that would let the GBP figures
    # be computed from a number nobody supplied.
    currency: str | None = None
    fx_rate: float | None = None
    leverage: float | None = None
    financing_rate: float | None = None

    def __post_init__(self) -> None:
        if not _TICKER.match(str(self.ticker or "")):
            raise ValueError(
                f"ticker must be a plain instrument code, got {self.ticker!r}"
            )
        if self.action not in ACTIONS:
            raise ValueError(
                f"action must be one of {', '.join(ACTIONS)}, got {self.action!r}"
            )

        object.__setattr__(self, "timestamp", _utc(self.timestamp))

        if self.action == "ignore":
            return

        # A price of zero is not a fill, and a negative one is not a price.
        # Either would override a good simulated position with a wrong number
        # that looks exactly as authoritative as a right one.
        for name in ("price", "close_price"):
            value = getattr(self, name)
            if value is None:
                continue
            if not (float(value) > 0):
                raise ValueError(f"{name} must be > 0, got {value}")

        if self.action in ("open", "flip"):
            if self.price is None:
                raise ValueError(f"a confirmed {self.action} needs a fill price")
            if self.quantity == 0:
                raise ValueError(
                    f"a confirmed {self.action} needs a non-zero quantity; "
                    "use action='close' to record that the position went away"
                )
        if self.action == "flip" and self.close_price is None:
            raise ValueError(
                "a confirmed flip needs close_price as well as price: it is two "
                "fills, and averaging them would lose both"
            )

        if self.exposure is None and self.price is not None:
            object.__setattr__(
                self, "exposure", abs(float(self.quantity) * float(self.price))
            )

    @classmethod
    def of(
        cls,
        ticker: str,
        action: str,
        *,
        direction: str | None = None,
        quantity: float = 0.0,
        **rest,
    ) -> "Confirmation":
        """Build one, applying ``direction`` to the sign of ``quantity``.

        The sign is applied exactly once, here, so no caller has to decide
        whether the number it is holding is already signed.
        """
        magnitude = abs(float(quantity))
        if direction is not None:
            token = str(direction).strip().lower()
            if token in ("short", "sell", "-1"):
                quantity = -magnitude
            elif token in ("long", "buy", "1"):
                quantity = magnitude
            else:
                raise ValueError(
                    f"direction must be long or short, got {direction!r}"
                )
        return cls(ticker=ticker, action=action, quantity=float(quantity), **rest)

    @property
    def direction(self) -> str | None:
        """``"long"``, ``"short"``, or ``None`` when the position is gone."""
        if self.quantity > 0:
            return "long"
        if self.quantity < 0:
            return "short"
        return None

    @property
    def slippage(self) -> float | None:
        """Fill minus recommendation, signed against the position.

        Positive is worse than recommended — you paid more to get long, or
        received less to get short — so one sign convention reads the same way
        for both directions.
        """
        if self.price is None or self.recommended_price is None:
            return None
        difference = float(self.price) - float(self.recommended_price)
        return difference if self.quantity >= 0 else -difference

    @property
    def quantity_deviation(self) -> float | None:
        """Confirmed quantity as a fraction of what was recommended.

        ``1.0`` is exactly filled, ``0.5`` is a half fill, ``0.0`` is nothing.
        """
        if self.recommended_quantity in (None, 0):
            return None
        return abs(float(self.quantity)) / abs(float(self.recommended_quantity))

    def to_document(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["timestamp"] = self.timestamp.isoformat()
        payload["schema_version"] = SCHEMA_VERSION
        payload["direction"] = self.direction
        return payload

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> "Confirmation":
        """Rebuild one from disk, ignoring fields a later version may add."""
        known = {f for f in cls.__slots__}
        payload = {k: v for k, v in dict(document).items() if k in known}
        payload.setdefault("ticker", "")
        payload.setdefault("action", "open")
        return cls(**payload)


def confirmations_root(config=None, account: str | None = None) -> Path:
    """Where confirmations live."""
    return account_root(config, account) / CONFIRMATIONS_DIR


def confirmation_filename(confirmation: Confirmation) -> str:
    return (
        f"{confirmation.timestamp.strftime(FILENAME_FORMAT)}-"
        f"{confirmation.action.upper()}-{confirmation.ticker}.json"
    )


def write_confirmation(
    confirmation: Confirmation, config=None, *, account: str | None = None
) -> str:
    """Write one confirmation. Returns its path.

    Raises rather than swallowing, unlike the rest of the persistence layer:
    a snapshot that failed to record is a lost reading, but a *confirmation*
    that failed to record leaves the system believing the recommendation was
    followed. Silence there is the one outcome worse than an error.
    """
    root = confirmations_root(config, account)
    path = root / confirmation_filename(confirmation)

    # Two confirmations for the same instrument and action in the same second
    # is unlikely and not impossible -- a scripted correction, a flip recorded
    # as two legs. Append-only means the second must not land on the first.
    if path.exists():
        stem = path.stem
        for suffix in range(2, 100):
            candidate = root / f"{stem}-{suffix}.json"
            if not candidate.exists():
                path = candidate
                break
        else:  # pragma: no cover - ninety-nine in one second is not a thing
            raise OSError(f"too many confirmations at {stem}")

    atomic_write_text(json.dumps(confirmation.to_document(), indent=2), path)
    logger.info(
        "Recorded %s %s at %s", confirmation.action, confirmation.ticker, path
    )
    return str(path)


def list_confirmations(config=None, account: str | None = None) -> list[Path]:
    """Every confirmation file, newest first."""
    root = confirmations_root(config, account)
    try:
        if not root.is_dir():
            return []
        return sorted(root.glob("*.json"), key=lambda p: p.name, reverse=True)
    except OSError as error:
        logger.warning("Confirmations unreadable at %s: %s", root, error)
        return []


def load_confirmations(
    config=None,
    account: str | None = None,
    *,
    ticker: str | None = None,
    since: pd.Timestamp | None = None,
) -> list[Confirmation]:
    """Every confirmation, oldest first. Never raises.

    A file that cannot be parsed is skipped with a warning rather than
    failing the load: one corrupt record must not hide the other forty.
    """
    found: list[Confirmation] = []
    for path in list_confirmations(config, account):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            confirmation = Confirmation.from_document(document)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
            logger.warning("Skipping unreadable confirmation %s: %s", path, error)
            continue
        if ticker is not None and confirmation.ticker != ticker:
            continue
        if since is not None and confirmation.timestamp < _utc(since):
            continue
        found.append(confirmation)

    return sorted(found, key=lambda c: c.timestamp)


def load_latest_confirmation(
    ticker: str, config=None, account: str | None = None
) -> Confirmation | None:
    """The most recent confirmation for one instrument."""
    found = load_confirmations(config, account, ticker=ticker)
    return found[-1] if found else None


def to_frame(confirmations: Iterable[Confirmation]) -> pd.DataFrame:
    """Confirmations as a table, for the dashboard and for analytics."""
    rows = [
        {
            **{
                key: value
                for key, value in c.to_document().items()
                if key != "schema_version"
            },
            "timestamp": c.timestamp,
            "slippage": c.slippage,
            "quantity_deviation": c.quantity_deviation,
        }
        for c in confirmations
    ]
    if not rows:
        return pd.DataFrame(
            columns=[
                "timestamp", "ticker", "action", "direction", "price",
                "quantity", "exposure", "slippage", "quantity_deviation",
                "source", "notes", "run_id",
            ]
        )
    frame = pd.DataFrame(rows)
    return frame.sort_values("timestamp").reset_index(drop=True)


# --------------------------------------------------------------------------- #
# What the book actually holds
# --------------------------------------------------------------------------- #
def confirmed_positions(
    confirmations: Iterable[Confirmation],
) -> dict[str, dict[str, Any]]:
    """Replay confirmations into a position per instrument.

    Last write wins, per instrument, in time order — a confirmation states
    what the position *is* after the fill, not a delta, because that is what
    the Trading 212 ticket shows you and what you can check by looking.

    ``ignore`` records a decision, not a position, so it changes nothing here.
    """
    book: dict[str, dict[str, Any]] = {}
    for confirmation in sorted(confirmations, key=lambda c: c.timestamp):
        if confirmation.action == "ignore":
            continue
        if confirmation.action == "close":
            book[confirmation.ticker] = {
                "quantity": 0.0,
                "price": confirmation.price,
                "at": confirmation.timestamp,
                "action": "close",
            }
            continue
        book[confirmation.ticker] = {
            "quantity": float(confirmation.quantity),
            "price": confirmation.price,
            "at": confirmation.timestamp,
            "action": confirmation.action,
            # The terms it was opened on, carried through so a correction
            # can be pre-filled with them rather than retyped -- and so the
            # GBP figures can be recomputed without guessing.
            "currency": confirmation.currency,
            "fx_rate": confirmation.fx_rate,
            "leverage": confirmation.leverage,
            "financing_rate": confirmation.financing_rate,
        }
    return book


def merge_simulated_and_confirmed_positions(
    simulated: pd.DataFrame | None,
    confirmed: Mapping[str, Mapping[str, Any]] | Iterable[Confirmation] | None,
    *,
    as_of: pd.Timestamp | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Overlay confirmed positions onto the simulated panel.

    Returns the merged panel and a report of what was overridden, because a
    silent override is indistinguishable from a bug: the position changes and
    nothing says why.

    Only the **latest bar** is overridden. A confirmation says what you hold
    now; it says nothing about what you held three weeks ago, and back-filling
    history from it would rewrite a simulation the engine never ran.

    An instrument with no confirmation keeps its simulated position — silence
    is not a decision, and the engine's belief stands until contradicted.
    """
    report: dict[str, Any] = {
        "overridden": {},
        "added": [],
        "unconfirmed": [],
        "as_of": None,
    }

    book = (
        dict(confirmed)
        if isinstance(confirmed, Mapping)
        else confirmed_positions(confirmed or [])
    )

    if simulated is None or getattr(simulated, "empty", True):
        if not book:
            return pd.DataFrame(), report
        moment = _utc(as_of or max(entry["at"] for entry in book.values()))
        frame = pd.DataFrame(
            [{name: float(entry["quantity"]) for name, entry in book.items()}],
            index=pd.DatetimeIndex([moment], name="timestamp"),
        )
        report["added"] = sorted(book)
        report["as_of"] = moment
        return frame, report

    frame = simulated.copy()
    if "timestamp" in frame.columns:
        frame = frame.set_index("timestamp")
    frame.index = pd.to_datetime(frame.index, utc=True, errors="coerce")

    if not book:
        report["unconfirmed"] = sorted(str(c) for c in frame.columns)
        report["as_of"] = frame.index.max()
        return frame, report

    latest = frame.index.max()
    report["as_of"] = latest

    for name, entry in book.items():
        quantity = float(entry["quantity"])
        if name not in frame.columns:
            frame[name] = 0.0
            report["added"].append(name)
        was = float(pd.to_numeric(pd.Series([frame.at[latest, name]]),
                                  errors="coerce").fillna(0.0).iloc[0])
        if was != quantity:
            report["overridden"][name] = {"simulated": was, "confirmed": quantity}
        frame.at[latest, name] = quantity

    report["added"] = sorted(report["added"])
    report["unconfirmed"] = sorted(
        str(name) for name in frame.columns if str(name) not in book
    )
    return frame, report


def deviations(confirmations: Iterable[Confirmation]) -> pd.DataFrame:
    """How far the fills drifted from what was recommended.

    Only rows where a recommendation was recorded: the rest are confirmations
    of trades the system never suggested, which are worth keeping and are not
    deviations from anything.
    """
    rows = [
        {
            "timestamp": c.timestamp,
            "ticker": c.ticker,
            "action": c.action,
            "price": c.price,
            "recommended_price": c.recommended_price,
            "slippage": c.slippage,
            "quantity": c.quantity,
            "recommended_quantity": c.recommended_quantity,
            "quantity_deviation": c.quantity_deviation,
            "partial_fill": (
                c.quantity_deviation is not None and c.quantity_deviation < 0.99
            ),
        }
        for c in sorted(confirmations, key=lambda c: c.timestamp)
        if c.recommended_price is not None or c.recommended_quantity is not None
    ]
    if not rows:
        return pd.DataFrame(
            columns=[
                "timestamp", "ticker", "action", "price", "recommended_price",
                "slippage", "quantity", "recommended_quantity",
                "quantity_deviation", "partial_fill",
            ]
        )
    return pd.DataFrame(rows)
