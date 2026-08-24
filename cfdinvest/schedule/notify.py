"""Email notifications over SMTP.

Message *building* is separated from message *sending*, and both are
separated from the transport. That split does real work:

* The builders are pure — given a summary, they return an
  :class:`~email.message.EmailMessage`. They can be tested by reading the
  text, with no server and no mocking.
* The transport is injectable, so the whole module is exercised offline.
  :class:`RecordingTransport` captures messages; :class:`ConsoleTransport`
  prints them.
* A send failure never propagates. An unattended run that completed
  successfully must not be reported as failed because a mail server was
  briefly unreachable — the notification is a courtesy, not the work.

**Credentials are read from the environment**, never from a config file
that could be committed. The password is redacted from every repr and log
line in this module.
"""

from __future__ import annotations

import logging
import os
import smtplib
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Any, Callable, Sequence

import pandas as pd

logger = logging.getLogger(__name__)

__all__ = [
    "SmtpConfig",
    "RecordingTransport",
    "ConsoleTransport",
    "smtp_transport",
    "build_trade_alert",
    "build_daily_summary",
    "build_error_alert",
    "send",
    "send_trade_alert",
    "send_daily_summary",
    "send_error_alert",
    "send_test_email",
]

#: A transport takes a built message and delivers it somewhere.
Transport = Callable[[EmailMessage], None]


@dataclass(frozen=True, slots=True)
class SmtpConfig:
    """Where and how to send mail.

    Read from the environment by :meth:`from_env` so credentials never sit
    in a file. ``enabled`` defaults to ``False``: a deployment that has not
    configured mail should run silently rather than raise on every bar.
    """

    host: str = "smtp.gmail.com"
    port: int = 587
    username: str | None = None
    password: str | None = field(default=None, repr=False)
    sender: str | None = None
    recipients: tuple[str, ...] = ()
    use_tls: bool = True
    timeout: float = 30.0
    enabled: bool = False
    #: Prefix on every subject line, so rules can filter on it.
    subject_prefix: str = "[cfdinvest]"

    def __repr__(self) -> str:  # pragma: no cover - defensive against leaks
        return (
            f"SmtpConfig(host={self.host!r}, port={self.port}, "
            f"username={self.username!r}, password=<redacted>, "
            f"sender={self.sender!r}, recipients={self.recipients!r}, "
            f"enabled={self.enabled})"
        )

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "SmtpConfig":
        """Build from ``CFD_SMTP_*`` environment variables.

        Recognised: ``CFD_SMTP_HOST``, ``CFD_SMTP_PORT``, ``CFD_SMTP_USER``,
        ``CFD_SMTP_PASSWORD``, ``CFD_SMTP_SENDER``,
        ``CFD_SMTP_RECIPIENTS`` (comma-separated), ``CFD_SMTP_USE_TLS``,
        ``CFD_SMTP_ENABLED``.

        Pure given ``env``; pass an explicit mapping in tests.
        """
        source = os.environ if env is None else env

        recipients = tuple(
            part.strip()
            for part in source.get("CFD_SMTP_RECIPIENTS", "").split(",")
            if part.strip()
        )
        username = source.get("CFD_SMTP_USER") or None
        enabled_flag = source.get("CFD_SMTP_ENABLED")
        enabled = (
            enabled_flag.strip().lower() in {"1", "true", "yes", "on"}
            if enabled_flag is not None
            else bool(recipients and username)
        )

        return cls(
            host=source.get("CFD_SMTP_HOST", "smtp.gmail.com"),
            port=int(source.get("CFD_SMTP_PORT", "587")),
            username=username,
            password=source.get("CFD_SMTP_PASSWORD") or None,
            sender=source.get("CFD_SMTP_SENDER") or username,
            recipients=recipients,
            use_tls=source.get("CFD_SMTP_USE_TLS", "true").strip().lower()
            in {"1", "true", "yes", "on"},
            enabled=enabled,
        )

    def validate(self) -> list[str]:
        """Return the reasons this config cannot send, if any.

        Returned rather than raised so a caller can report every problem at
        once instead of discovering them one restart at a time.
        """
        problems = []
        if not self.host:
            problems.append("no SMTP host")
        if not self.recipients:
            problems.append("no recipients (set CFD_SMTP_RECIPIENTS)")
        if not self.sender:
            problems.append("no sender (set CFD_SMTP_SENDER or CFD_SMTP_USER)")
        if self.username and not self.password:
            problems.append("username set but no password (set CFD_SMTP_PASSWORD)")
        return problems


# --------------------------------------------------------------------------- #
# Transports
# --------------------------------------------------------------------------- #
class RecordingTransport:
    """Captures messages instead of sending them.

    The offline test double, and what ``--dry-run`` uses so a deployment can
    be checked end to end without mailing anyone.
    """

    def __init__(self) -> None:
        self.messages: list[EmailMessage] = []

    def __call__(self, message: EmailMessage) -> None:
        self.messages.append(message)

    @property
    def subjects(self) -> list[str]:
        """Subject lines captured so far."""
        return [message["Subject"] for message in self.messages]

    def body(self, index: int = -1) -> str:
        """Plain-text body of a captured message."""
        if not self.messages:
            return ""
        return self.messages[index].get_content()


class ConsoleTransport:
    """Prints messages to a stream. Useful for a first VM smoke test."""

    def __init__(self, stream=None) -> None:
        self.stream = stream

    def __call__(self, message: EmailMessage) -> None:
        import sys

        target = self.stream or sys.stdout
        target.write(f"To: {message['To']}\nSubject: {message['Subject']}\n\n")
        target.write(message.get_content())
        target.write("\n" + "-" * 60 + "\n")


def smtp_transport(config: SmtpConfig) -> Transport:
    """Build a transport that actually delivers over SMTP.

    Opens a connection per message. That is wasteful for a burst, and
    deliberate: a long-lived connection to a mail server is a thing that
    breaks quietly overnight, and reconnecting costs less than debugging
    that.
    """

    def _send(message: EmailMessage) -> None:
        with smtplib.SMTP(config.host, config.port, timeout=config.timeout) as server:
            if config.use_tls:
                server.starttls()
            if config.username and config.password:
                server.login(config.username, config.password)
            server.send_message(message)

    return _send


# --------------------------------------------------------------------------- #
# Message builders (pure)
# --------------------------------------------------------------------------- #
def _compose(config: SmtpConfig, subject: str, body: str) -> EmailMessage:
    """Assemble a plain-text message. Pure."""
    message = EmailMessage()
    message["Subject"] = f"{config.subject_prefix} {subject}".strip()
    message["From"] = config.sender or "cfdinvest@localhost"
    message["To"] = ", ".join(config.recipients) or "undisclosed-recipients:;"
    message.set_content(body)
    return message


def _format_table(frame: pd.DataFrame, columns: Sequence[str] | None = None) -> str:
    """Render a small frame as fixed-width text, or say it is empty."""
    if frame is None or frame.empty:
        return "  (none)"
    subset = frame
    if columns:
        present = [column for column in columns if column in frame.columns]
        if present:
            subset = frame[present]
    return "\n".join(f"  {line}" for line in subset.to_string().splitlines())


def build_trade_alert(trade: Any, config: SmtpConfig | None = None) -> EmailMessage:
    """Compose an alert for one executed trade.

    Pure. ``trade`` may be a mapping or a pandas Series — whatever the trade
    log yields when iterated.
    """
    settings = config or SmtpConfig()
    row = dict(trade)

    asset = row.get("asset", "?")
    action = row.get("action", "?")
    subject = f"{action} {asset}"

    lines = [f"{action.upper()} {asset}", ""]
    for key in (
        "timestamp", "units", "traded_units", "price", "fill_price",
        "notional", "spread_cost", "slippage", "financing", "realized_pnl",
        "equity", "reason",
    ):
        if key in row:
            value = row[key]
            formatted = f"{value:,.4f}" if isinstance(value, (int, float)) else value
            lines.append(f"{key:>15}: {formatted}")

    lines += ["", "Simulated fill. No order was placed with any broker."]
    return _compose(settings, subject, "\n".join(lines))


def build_daily_summary(summary: Any, config: SmtpConfig | None = None) -> EmailMessage:
    """Compose the end-of-run summary email.

    Pure. Takes a :class:`~cfdinvest.schedule.runner.RunSummary`.
    """
    settings = config or SmtpConfig()
    status = getattr(summary, "status", "unknown")
    as_of = getattr(summary, "as_of", None)

    subject = f"daily summary {as_of:%Y-%m-%d}" if as_of is not None else "daily summary"
    if status != "ok":
        subject = f"{subject} ({status})"

    metrics = getattr(summary, "metrics", {}) or {}
    headline = [
        ("total return", metrics.get("total_return")),
        ("CAGR", metrics.get("cagr")),
        ("Sharpe", metrics.get("sharpe")),
        ("max drawdown", metrics.get("max_drawdown_fraction")),
        ("round trips", metrics.get("round_trips")),
        ("win rate", metrics.get("win_rate")),
    ]

    lines = [
        getattr(summary, "headline", lambda: str(summary))(),
        "",
        f"run id      : {getattr(summary, 'run_id', '?')}",
        f"universe    : {', '.join(getattr(summary, 'universe', ()) or ()) or '(none)'}",
        f"bars        : {getattr(summary, 'bars', 0)}",
        f"duration    : {getattr(summary, 'duration_seconds', 0.0):.1f}s",
        "",
        "Backtest metrics over the loaded history",
        "---------------------------------------",
    ]
    for label, value in headline:
        if value is None:
            continue
        formatted = f"{value:,.4f}" if isinstance(value, float) else value
        lines.append(f"  {label:<14}: {formatted}")

    lines += [
        "",
        "Active signals on the latest bar",
        "--------------------------------",
        _format_table(
            getattr(summary, "latest_signals", pd.DataFrame()),
            ("direction", "size", "confidence", "reason"),
        ),
        "",
        "Trades executed on the latest bar",
        "---------------------------------",
        _format_table(
            getattr(summary, "latest_trades", pd.DataFrame()),
            ("asset", "action", "traded_units", "price", "realized_pnl"),
        ),
        "",
        "Market state",
        "------------",
        _format_table(
            getattr(summary, "market_state", pd.DataFrame()),
            ("asset_class", "is_open", "next_open", "next_close"),
        ),
        "",
        "Simulated results. No orders were placed with any broker.",
    ]
    return _compose(settings, subject, "\n".join(lines))


def build_error_alert(
    error: BaseException | str, config: SmtpConfig | None = None, *, context: str = ""
) -> EmailMessage:
    """Compose an alert for a failed run. Pure."""
    settings = config or SmtpConfig()
    description = (
        f"{type(error).__name__}: {error}"
        if isinstance(error, BaseException)
        else str(error)
    )

    lines = ["A scheduled run failed.", ""]
    if context:
        lines += [f"context: {context}", ""]
    lines += [description, "", "The scheduler will retry on its next trigger."]

    return _compose(settings, f"RUN FAILED — {description[:60]}", "\n".join(lines))


# --------------------------------------------------------------------------- #
# Sending
# --------------------------------------------------------------------------- #
def send(
    message: EmailMessage,
    config: SmtpConfig | None = None,
    transport: Transport | None = None,
) -> bool:
    """Deliver a message, returning whether it went.

    Never raises. A mail failure is logged and swallowed: an unattended run
    that did its work must not be reported as broken because a mail server
    was unreachable.
    """
    settings = config or SmtpConfig()

    if transport is None:
        if not settings.enabled:
            logger.debug("Email disabled; not sending %r", message["Subject"])
            return False
        problems = settings.validate()
        if problems:
            logger.warning("Email not configured (%s); not sending", "; ".join(problems))
            return False
        transport = smtp_transport(settings)

    try:
        transport(message)
        logger.info("Sent notification: %s", message["Subject"])
        return True
    except Exception as error:  # noqa: BLE001 - notification must never break a run
        logger.error("Failed to send notification %r: %s", message["Subject"], error)
        return False


def send_trade_alert(trade, config=None, transport=None) -> bool:
    """Build and send an alert for one executed trade."""
    return send(build_trade_alert(trade, config), config, transport)


def send_daily_summary(summary, config=None, transport=None) -> bool:
    """Build and send the end-of-run summary."""
    return send(build_daily_summary(summary, config), config, transport)


def send_error_alert(error, config=None, transport=None, *, context: str = "") -> bool:
    """Build and send a failure alert."""
    return send(build_error_alert(error, config, context=context), config, transport)


def send_test_email(config: SmtpConfig | None = None, transport=None) -> bool:
    """Send a message that proves the mail path works end to end."""
    settings = config or SmtpConfig()
    problems = settings.validate()

    body = [
        "This is a test message from cfdinvest.",
        "",
        f"host      : {settings.host}:{settings.port}",
        f"sender    : {settings.sender}",
        f"recipients: {', '.join(settings.recipients) or '(none)'}",
        f"TLS       : {settings.use_tls}",
        f"enabled   : {settings.enabled}",
        "",
        "Configuration problems: " + ("; ".join(problems) if problems else "none"),
    ]
    return send(_compose(settings, "test email", "\n".join(body)), settings, transport)
