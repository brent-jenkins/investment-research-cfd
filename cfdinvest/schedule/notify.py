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

from cfdinvest.disclosure import DEALING_NOTICE
import logging
import os
import smtplib
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Any, Callable, Mapping, Sequence

import pandas as pd

logger = logging.getLogger(__name__)

__all__ = [
    "build_monitoring_alert",
    "send_monitoring_alert",
    "delivery_state",
    "reset_delivery_journal",
    "SmtpConfig",
    "RecordingTransport",
    "ConsoleTransport",
    "smtp_transport",
    "build_trade_alert",
    "build_daily_summary",
    "build_advisor_alert",
    "send_advisor_alert",
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

    #: Which emails are worth sending. Defaults chosen deliberately.
    #:
    #: The paper signals are the point: a selection gets placed by hand on
    #: the virtual account and compared against the simulated fills. Errors
    #: are rare and operational. Everything else is off.
    #:
    #: The run summary in particular *must* stay off by default now. It
    #: fired after every pipeline run, which was once a day when it was
    #: written and is every fifteen minutes since the engine moved to hourly
    #: bars -- twenty-six performance emails a session, which is how the two
    #: that matter end up in a folder nobody opens.
    send_paper_signals: bool = True
    send_errors: bool = True
    send_run_summary: bool = False
    send_advisor_suggestions: bool = False
    #: Prefix on every subject line, so rules can filter on it.
    subject_prefix: str = "[cfdinvest]"
    #: Which account this deployment trades. Appended to every subject, so an
    #: alert about a demo account never reads like one about real money.
    #: ``None`` when account separation is not enabled.
    account: str | None = None

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

        from cfdinvest.env import resolve_account

        try:
            account = resolve_account(env=source)
        except ValueError:
            # A bad mode must not stop mail going out; the monitoring layer
            # reports it, and an alert about the broken configuration is
            # exactly the alert you want to receive.
            account = None

        return cls(
            account=account,
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
def _compose(
    config: SmtpConfig, subject: str, body: str, html: str | None = None
) -> EmailMessage:
    """Assemble a message, plain text with an optional HTML alternative. Pure.

    The account mode goes in every subject line, at the end, where a phone
    still shows it. Two deployments send to the same inbox, and an alert about
    a demo account that reads identically to one about real money is an alert
    that will eventually be acted on as the wrong one.
    """
    message = EmailMessage()
    suffix = f" ({config.account})" if config.account else ""
    message["Subject"] = f"{config.subject_prefix} {subject}{suffix}".strip()
    message["From"] = config.sender or "cfdinvest@localhost"
    message["To"] = ", ".join(config.recipients) or "undisclosed-recipients:;"
    message.set_content(body)
    if html:
        # An alternative, never a replacement. The plain text stays the real
        # message: it is what a screen reader, a text-only client and the
        # notification preview on a phone all show, and an email whose
        # meaning lives only in its markup is an email that sometimes has no
        # meaning at all.
        message.add_alternative(html, subtype="html")
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
    lines += _confirmation_instructions(asset, row)
    return _compose(settings, subject, "\n".join(lines))


def _confirmation_instructions(asset: Any, row: Mapping[str, Any]) -> list[str]:
    """How to reply to this alert if you actually took the trade.

    The reply is parsed by :mod:`cfdinvest.confirm.reply`, which reads text
    and never fetches mail -- something else has to hand it the body. The
    lines belong in the alert because the moment you know your fill price is
    the moment you are reading this, and a later step is one that gets
    skipped.
    """
    action = str(row.get("action", "")).lower()
    verb = "CLOSE" if "close" in action else "OPEN"
    example = f"CONFIRM {verb} {asset} PRICE=<your fill>"
    if verb == "OPEN":
        example += " QTY=<your size>"
    return [
        "",
        "If you took this trade, reply with your real fill so the system "
        "stops assuming this one:",
        f"    {example}",
    ]


def build_advisor_alert(alerts, config: SmtpConfig | None = None) -> EmailMessage:
    """One email covering everything newly worth acting on.

    One message rather than one per item: five separate emails arriving
    together is how a channel gets filtered, and the ordering already puts
    the urgent thing first.

    The subject leads with what is wrong, because that is what decides
    whether it is opened now or later.
    """
    settings = config or SmtpConfig.from_env()
    rows = list(alerts or ())

    urgent = [alert for alert in rows if alert.severity == "critical"]
    warnings = [alert for alert in rows if alert.severity == "warning"]
    openings = [alert for alert in rows if alert.kind == "open"]

    if urgent:
        subject = f"Action needed: {urgent[0].headline}"
    elif warnings:
        subject = f"Watch: {warnings[0].headline}"
    else:
        subject = (
            f"{len(openings)} trade(s) to consider"
            if openings else "Nothing to act on"
        )

    lines = []
    if urgent or warnings:
        lines.append("YOUR OPEN POSITIONS")
        for alert in urgent + warnings:
            lines.append(f"  * {alert.headline}")
            if alert.detail:
                lines.append(f"      {alert.detail}")
        lines.append("")
    if openings:
        lines.append("TRADES TO CONSIDER")
        for alert in openings:
            lines.append(f"  * {alert.headline}")
            if alert.detail:
                lines.append(f"      {alert.detail}")
        lines.append("")

    lines.append(
        "Sizes are sizes, not permissions. Run anything here through the "
        "Live-Execution Helper before placing it -- that is what checks it "
        "against your limits, margin and the broker rules."
    )
    lines.append("")
    lines.append(
        "This alert is advisory: nothing here has been placed for you, and "
        "acting on it means placing it yourself."
    )
    lines.append(
        "Automated dealing runs separately, on the Capital.com demo "
        "account only, and reports its own fills in its own mail."
    )

    message = EmailMessage()
    message["Subject"] = f"[CFDInvest] {subject}"
    message["From"] = settings.sender or "cfdinvest@localhost"
    message["To"] = ", ".join(getattr(settings, "recipients", ()) or [])
    message.set_content("\n".join(lines))
    return message


def send_advisor_alert(alerts, config=None, transport=None) -> bool:
    """Send the advisor alert, if there is anything to say."""
    if not alerts:
        return False
    return send(build_advisor_alert(alerts, config), config, transport)


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


#: What each monitoring issue type is called in the email. The check names are
#: for code; these are for a person reading a phone at seven in the morning.
_MONITORING_LABELS: dict[str, str] = {
    "lake": "Data lake",
    "scheduler": "Scheduler",
    "disk": "Disk space",
    "run": "The last run",
    "confirm": "Confirmations",
    "email": "Notifications",
    "pipeline": "Pipeline output",
    "system": "The machine",
}


def build_monitoring_alert(snapshot, config: SmtpConfig | None = None) -> EmailMessage:
    """Compose the operational-health alert. Pure.

    Takes a :class:`~cfdinvest.monitoring.aggregate.MonitoringSnapshot`.

    The subject carries the worst single problem rather than a count: a
    subject line reading "7 issues" tells you nothing about whether to get out
    of bed, and an alert you cannot triage from the notification is one you
    learn to swipe away.

    Every issue prints its remedy. An alert that names a problem without a
    next step is one that gets archived.
    """
    settings = config or SmtpConfig()
    status = str(getattr(snapshot, "overall_status", "red")).upper()
    headline = getattr(snapshot, "headline", "")
    issues = tuple(getattr(snapshot, "issues", ()))

    lines = [
        f"Operational monitoring: {status}",
        "",
        headline,
        "",
        f"critical: {getattr(snapshot, 'critical_count', 0)}   "
        f"warning: {getattr(snapshot, 'warning_count', 0)}   "
        f"info: {getattr(snapshot, 'info_count', 0)}",
    ]

    for issue in issues:
        lines += [
            "",
            f"[{issue.severity.upper()}] "
            f"{_MONITORING_LABELS.get(issue.type, issue.type)}",
            f"  {issue.message}",
        ]
        if issue.remedy:
            lines.append(f"  -> {issue.remedy}")

    affected = {issue.type for issue in issues}
    clean = [
        label for kind, label in _MONITORING_LABELS.items() if kind not in affected
    ]
    if clean:
        lines += ["", f"Checked and clear: {', '.join(clean)}."]

    resources = getattr(snapshot, "system_resources", None) or {}
    measured = tuple(resources.get("measured") or ())
    readings = []
    if resources.get("free_gb") is not None:
        readings.append(f"{resources['free_gb']:,.1f} GB free")
    if resources.get("cpu_percent") is not None:
        readings.append(f"CPU {resources['cpu_percent']:.0f}%")
    if resources.get("memory_percent") is not None:
        readings.append(f"memory {resources['memory_percent']:.0f}%")
    if readings:
        lines += ["", "The machine: " + ", ".join(readings)]
    if "cpu" not in measured:
        lines.append(
            "CPU and memory were not measured (psutil is not installed). "
            "Absent is not the same as healthy."
        )

    lines += [
        "",
        DEALING_NOTICE,
    ]

    subject = f"{status} — {headline[:70]}" if headline else f"{status} — monitoring"
    return _compose(settings, subject, "\n".join(lines))


# --------------------------------------------------------------------------- #
# Sending
# --------------------------------------------------------------------------- #
class _DeliveryJournal:
    """What happened to the mail this process tried to send.

    :func:`send` swallows failures on purpose -- an unattended run that did
    its work must not be reported as broken because a mail server was
    unreachable. That is right, and it is exactly why the failure has to
    surface somewhere else. This is the somewhere else, and
    :mod:`cfdinvest.monitoring` reads it.

    In-process and bounded. The monitoring pass runs in the same process as
    the sends it is reporting on, so nothing has to survive a restart; the
    longer history lives in the monitoring snapshots.
    """

    __slots__ = ("attempts", "limit")

    def __init__(self, limit: int = 50) -> None:
        self.attempts: list[dict[str, Any]] = []
        self.limit = limit

    def record(self, subject: Any, ok: bool, error: BaseException | None = None) -> None:
        self.attempts.append(
            {
                "subject": str(subject),
                "ok": bool(ok),
                "error": f"{type(error).__name__}: {error}" if error else None,
                "at": pd.Timestamp.now(tz="UTC"),
            }
        )
        del self.attempts[: max(0, len(self.attempts) - self.limit)]

    def state(self, config: "SmtpConfig | None" = None) -> dict[str, Any]:
        failures = [a for a in self.attempts if not a["ok"]]
        return {
            "enabled": (config or SmtpConfig()).enabled,
            "attempted": len(self.attempts),
            "sent": len(self.attempts) - len(failures),
            "failed": len(failures),
            "last_error": failures[-1]["error"] if failures else None,
            "last_failed_subject": failures[-1]["subject"] if failures else None,
        }

    def clear(self) -> None:
        self.attempts.clear()


_JOURNAL = _DeliveryJournal()


def delivery_state(config: SmtpConfig | None = None) -> dict[str, Any]:
    """How the notifications this process sent actually went."""
    return _JOURNAL.state(config)


def reset_delivery_journal() -> None:
    """Forget the record. Called at the start of a run, and by tests."""
    _JOURNAL.clear()



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
        _JOURNAL.record(message["Subject"], True)
        return True
    except Exception as error:  # noqa: BLE001 - notification must never break a run
        logger.error("Failed to send notification %r: %s", message["Subject"], error)
        _JOURNAL.record(message["Subject"], False, error)
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


def send_monitoring_alert(snapshot, config=None, transport=None) -> bool:
    """Send the operational-health alert.

    Called only for a red status: an alert that arrives every day for a yellow
    becomes an alert nobody opens on the day it is red.
    """
    return send(build_monitoring_alert(snapshot, config), config, transport)


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
