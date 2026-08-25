"""Shared rendering: page setup, the banner, formatting and table styling.

Seven pages need the same theme, the same banner and the same "this is not
available, and here is why" panel. Putting that here keeps the pages short
enough to read, and means the read-only contract is stated in one place
rather than seven.

**Times are shown in the operator's local zone, not UTC.** Everything is
stored and compared in UTC — that part never changes — but a person reading
"22:30" wants to know whether that is tonight, and a UTC timestamp during
British Summer Time is an hour off the clock on the wall. The display zone is
``Europe/London`` unless ``CFD_DISPLAY_TZ`` says otherwise, and the zone
abbreviation is always printed so it cannot be misread.

Formatting helpers are pure and are tested directly; the ``render_*``
functions take the ``streamlit`` module as their first argument so that
importing this module never requires a Streamlit runtime.
"""

from __future__ import annotations

import html
import os
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import pandas as pd

from cfdinvest.ui import theme
# Imported as a module, not by name: the checks are looked up at call time so
# a caller -- or a test -- can substitute them.
from cfdinvest.ui import health as health_module
from cfdinvest.ui.health import HealthReport, HealthThresholds
from cfdinvest.ui.sources import Source, utcnow

__all__ = [
    "DASH",
    "DISPLAY_TZ",
    "CURRENCY",
    "DEFAULT_BALANCE",
    "bootstrap",
    "display_zone",
    "localise",
    "read_everything",
    "page_setup",
    "render_banner",
    "render_brand",
    "render_absent",
    "render_note",
    "render_frame",
    "render_kv",
    "render_section",
    "for_display",
    "column_config_for",
    "money",
    "number",
    "percent",
    "moment",
    "short_moment",
    "day",
    "age",
    "duration",
    "run_moment",
    "direction_badge",
    "account_balance",
    "scale_factor",
]

#: Shown wherever a value is genuinely absent. One character, used
#: consistently, so an empty cell is never mistaken for a zero.
DASH = "—"

#: Where the operator is. Times render here; nothing is *stored* in it.
DISPLAY_TZ: str = os.environ.get("CFD_DISPLAY_TZ", "Europe/London")

#: Account currency symbol, for display only.
CURRENCY: str = os.environ.get("CFD_CURRENCY_SYMBOL", "£")

#: Session key holding the operator's real account balance.
BALANCE_KEY = "cfd_account_balance"

#: Default account balance when the operator has not set one this session.
#: The virtual Trading 212 account this was built against started at £2,000.
DEFAULT_BALANCE: float = float(os.environ.get("CFD_ACCOUNT_BALANCE", "2000"))


def bootstrap() -> None:
    """Put the repository root on ``sys.path``.

    Streamlit executes each page as a script, so the import root is the
    page's own directory rather than the project. Without this, a checkout
    that has not been ``pip install``-ed cannot import ``cfdinvest`` at all.
    Idempotent, and a no-op for an installed package.
    """
    import sys
    from pathlib import Path

    root = str(Path(__file__).resolve().parents[2])
    if root not in sys.path:
        sys.path.insert(0, root)


# --------------------------------------------------------------------------- #
# Time
# --------------------------------------------------------------------------- #
def display_zone() -> ZoneInfo:
    """The zone times render in, falling back to UTC if the name is unknown."""
    try:
        return ZoneInfo(DISPLAY_TZ)
    except Exception:  # noqa: BLE001 - a bad zone name must not stop the page
        return ZoneInfo("UTC")


def localise(value: Any) -> pd.Timestamp | None:
    """Parse anything into a timestamp in the display zone.

    Naive input is assumed to be UTC, because everything this app reads was
    written in UTC.
    """
    if _missing(value):
        return None
    try:
        stamp = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if stamp.tz is None:
        stamp = stamp.tz_localize("UTC")
    return stamp.tz_convert(display_zone())


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #
def read_everything(
    st, *, now: pd.Timestamp | None = None, thresholds: HealthThresholds | None = None
) -> dict[str, Any]:
    """Read every source once, and run the health checks over them.

    Cached briefly so that clicking a widget does not re-read the lake. Every
    page calls this; the cache is what makes that cheap. The sidebar's
    refresh button clears it.
    """
    from cfdinvest.ui import sources

    @st.cache_data(ttl=30, show_spinner=False)
    def _read(_stamp: str) -> dict[str, Any]:
        config_source = sources.load_app_config()
        config = config_source.value
        universe = list(getattr(config.market, "tickers", ()) or ())

        return {
            "config_source": config_source,
            "config": config,
            "universe": universe,
            "lake": sources.load_lake_metadata(config, now=now),
            "summary": sources.load_last_pipeline_summary(config),
            "scheduler": sources.load_scheduler_status(config, universe, now=now),
            "execution": sources.load_execution_frame(config),
            "signals": sources.load_signal_frame(config),
            "errors": sources.load_error_log(config),
            "disk": sources.disk_usage(config.paths.data_root),
            "market": sources.market_status(universe, now=now),
            "analytics": sources.load_run_analytics(config),
            "persisted": sources.load_persisted_run(config),
        }

    stamp = st.session_state.get("cfd_refresh_stamp", "initial")
    try:
        data = _read(stamp)
    except Exception:  # noqa: BLE001 - never let caching break the dashboard
        data = _read.__wrapped__(stamp)  # type: ignore[attr-defined]

    data["health"] = health_module.run_checks(
        config_source=data["config_source"],
        lake=data["lake"],
        scheduler_status=data["scheduler"],
        summary=data["summary"],
        execution=data["execution"],
        errors=data["errors"],
        disk=data["disk"],
        thresholds=thresholds,
        now=now,
    )
    data["now"] = utcnow(now)
    return data


# --------------------------------------------------------------------------- #
# Page frame
# --------------------------------------------------------------------------- #
def page_setup(st, title: str, *, icon: str = "📈", subtitle: str = "") -> dict:
    """Configure the page, apply the theme, read the data, draw the banner.

    Returns the bundle from :func:`read_everything` so the page can get on
    with rendering.
    """
    try:
        st.set_page_config(
            page_title=f"{title} · cfdinvest", page_icon=icon, layout="wide"
        )
    except Exception:  # noqa: BLE001 - already configured by another page
        pass

    mode = theme.apply(st)
    render_brand(st, mode)
    st.session_state["cfd_mode"] = mode

    data = read_everything(st)
    data["mode"] = mode

    heading, badge = st.columns([5, 1], vertical_alignment="bottom")
    heading.title(title)
    badge.markdown(
        '<div style="text-align:right;padding-bottom:14px;">'
        '<span class="cfd-readonly">read-only</span></div>',
        unsafe_allow_html=True,
    )
    if subtitle:
        render_note(st, subtitle)

    render_banner(st, data["health"], mode)
    _render_sidebar(st, data)
    return data


def _render_sidebar(st, data: dict) -> None:
    """Freshness, health, the account balance and the refresh control."""
    health: HealthReport = data["health"]
    scheduler = data["scheduler"].value or {}

    with st.sidebar:
        state = (
            "🔴 Critical"
            if health.show_banner
            else ("🟠 Warnings" if health.triggered else "🟢 Healthy")
        )
        render_kv(
            st,
            {
                "System": state,
                "Last completed run": age(scheduler.get("last_run"), data["now"]),
                "Next run due": short_moment(scheduler.get("next_run")),
                "Checked": short_moment(data["now"]),
                "Latest run": run_moment(
                    (data["summary"].value or {}).get("run_id")
                ),
            },
        )

        st.divider()
        account_balance(st, data)

        st.divider()
        if st.button("Refresh data", width="stretch"):
            st.cache_data.clear()
            st.session_state["cfd_refresh_stamp"] = str(utcnow())
            st.rerun()
        st.caption(
            "Read-only. This dashboard never places an order. "
            f"Times shown in {display_zone().key}."
        )


def render_brand(st, mode: str = "dark") -> None:
    """The wordmark, in Streamlit's logo slot above the navigation.

    Idempotent by construction: Streamlit keeps one logo per page and the
    last call wins, so the entry script and the page can both set it without
    the mark appearing twice.
    """
    try:
        st.logo(theme.brand_svg(mode), size="large")
    except Exception:  # noqa: BLE001 - never fail to draw over a logo
        pass


def render_banner(st, health: HealthReport, mode: str = "dark") -> None:
    """The persistent critical banner, or a quiet all-clear."""
    if health.show_banner:
        items = "".join(
            f"<li><strong>{html.escape(check.title)}</strong> — "
            f"{html.escape(check.detail)}</li>"
            for check in health.critical
        )
        st.markdown(
            f'<div class="cfd-banner">{html.escape(health_module.BANNER_TEXT)}'
            f"<ul>{items}</ul></div>",
            unsafe_allow_html=True,
        )
        return

    if health.triggered:
        titles = ", ".join(check.title for check in health.triggered)
        st.markdown(
            f'<div class="cfd-ok">Non-critical warnings: {html.escape(titles)}. '
            "See the Status page.</div>",
            unsafe_allow_html=True,
        )
        return

    st.markdown(
        '<div class="cfd-ok">All health checks passing.</div>',
        unsafe_allow_html=True,
    )


def render_absent(st, source: Source, what: str) -> bool:
    """Explain why something is not shown. Returns ``True`` if it handled it.

    A blank panel and an absent panel look the same to a reader; only one of
    them is honest. Every page routes its empty states through here.
    """
    if source.failed:
        st.error(f"**{what} could not be read.** {source.error}")
        return True
    if not source.available:
        render_note(st, f"**{what} is not available.** {source.note or ''}")
        return True
    return False


def render_note(st, text: str) -> None:
    """A muted aside, for context that is not a warning."""
    st.markdown(f'<div class="cfd-note">{text}</div>', unsafe_allow_html=True)


def render_section(st, title: str, note: str = "") -> None:
    """A heading with an optional one-line explanation under it."""
    st.subheader(title)
    if note:
        render_note(st, note)


def render_kv(st, rows: Mapping[str, Any]) -> None:
    """Compact key/value rows, for facts that do not deserve a metric card.

    Seven metric cards in a row is a wall; seven of these is a list you can
    read down. Used where the label matters more than the number's size.
    """
    body = "".join(
        f'<div class="cfd-kv"><span class="k">{html.escape(str(key))}</span>'
        f'<span class="v">{html.escape(str(value))}</span></div>'
        for key, value in rows.items()
    )
    st.markdown(body, unsafe_allow_html=True)


# --------------------------------------------------------------------------- #
# Tables
# --------------------------------------------------------------------------- #
#: Column-name fragments and the format each implies.
_FRACTION = ("fraction", "_pct", "return", "drawdown", "share", "confidence")
_MONEY = ("pnl", "cost", "equity", "notional", "financing", "spread", "slippage",
          "fee", "value", "exposure", "balance")
_PRICE = ("price", "level", "fill", "close", "open", "high", "low", "distance")
_RATE = ("rate", "leverage", "size", "units")


def _format_for(name: str) -> str | None:
    """Which display format a column name implies."""
    lowered = str(name).lower()
    if any(token in lowered for token in _FRACTION):
        return "percent"
    if any(token in lowered for token in _MONEY):
        return "money"
    if any(token in lowered for token in _PRICE):
        return "price"
    if any(token in lowered for token in _RATE):
        return "rate"
    return None


def column_config_for(st, frame: pd.DataFrame) -> dict:
    """Per-column display rules inferred from the column names.

    Money to two decimals with a currency symbol, prices and rates to four,
    fractions as percentages, timestamps in the display zone, and every
    heading spelled out. Guessing from names is crude, but the alternative is
    every page hand-writing the same twenty rules.
    """
    config: dict[str, Any] = {}
    for name in frame.columns:
        series = frame[name]
        label = _label(name)

        if pd.api.types.is_datetime64_any_dtype(series):
            config[name] = st.column_config.DatetimeColumn(
                label, format="D MMM YYYY, HH:mm"
            )
            continue
        if pd.api.types.is_bool_dtype(series):
            config[name] = st.column_config.CheckboxColumn(label)
            continue
        if not pd.api.types.is_numeric_dtype(series):
            config[name] = st.column_config.Column(label)
            continue

        kind = _format_for(name)
        if kind == "money":
            config[name] = st.column_config.NumberColumn(
                label, format=f"{CURRENCY}%.2f"
            )
        elif kind in ("price", "rate"):
            config[name] = st.column_config.NumberColumn(label, format="%.4f")
        elif kind == "percent":
            config[name] = st.column_config.NumberColumn(label, format="percent")
        else:
            config[name] = st.column_config.NumberColumn(label)
    return config


#: Column names whose expansion is not simply "underscores to spaces".
#: Abbreviations get spelled out here rather than in seven pages.
_LABELS: dict[str, str] = {
    "asset": "Instrument",
    "ticker": "Instrument",
    "pnl": "Profit & loss",
    "realized_pnl": "Realised profit & loss",
    "unrealized_pnl": "Unrealised profit & loss",
    "total_pnl": "Total profit & loss",
    "fx_rate": "Exchange rate",
    "fx_spread": "Currency spread",
    "fx_slippage": "Currency slippage",
    "fx_conversion_fee": "Currency conversion fee",
    "fx_adjusted_fill_price": "All-in fill price",
    "fx_cost": "Currency cost",
    "fx_costs": "Currency costs",
    "take_profit_level": "Take-profit level",
    "take_profit_fill_price": "Take-profit fill",
    "take_profit_order": "Take-profit order",
    "stop_level": "Stop-loss level",
    "stop_fill_price": "Stop-loss fill",
    "stop_order": "Stop-loss order",
    "tp_distance": "Take-profit distance",
    "tp_distance_pct": "Take-profit distance",
    "stop_distance": "Stop-loss distance",
    "stop_distance_pct": "Stop-loss distance",
    "gap_through": "Gapped through",
    "breach_type": "How it was reached",
    "traded_units": "Units traded",
    "spread_cost": "Spread cost",
    "run_id": "Run",
    "duration_seconds": "Duration (seconds)",
    "is_open": "Open now",
    "is_holiday": "Holiday",
    "next_open": "Next open",
    "next_close": "Next close",
    "asset_class": "Asset class",
    "vintage_mode": "Vintage mode",
    "max_drawdown_fraction": "Maximum drawdown",
    "last_price": "Latest price",
    "entry_price": "Entry price",
    "financing_accrued": "Financing accrued",
    "scaled_units": "Units for your account",
    "scaled_notional": "Exposure for your account",
}


def _label(name: Any) -> str:
    """A human column heading. Abbreviations are spelled out."""
    key = str(name)
    if key in _LABELS:
        return _LABELS[key]
    return key.replace("_", " ").strip().capitalize()


def for_display(frame: pd.DataFrame) -> pd.DataFrame:
    """A copy with every timestamp moved into the display zone."""
    if frame is None or frame.empty:
        return frame

    shown = frame.copy()
    for name in shown.columns:
        if pd.api.types.is_datetime64_any_dtype(shown[name]):
            values = shown[name]
            if getattr(values.dtype, "tz", None) is None:
                values = values.dt.tz_localize("UTC")
            shown[name] = values.dt.tz_convert(display_zone())

    # A column holding both timestamps and strings -- a settings table, say --
    # cannot be converted to Arrow, and Streamlit recovers by guessing. Doing
    # the cast here means the table renders from a known shape rather than a
    # repaired one, and the console stops carrying a traceback per page.
    for name in shown.columns:
        if shown[name].dtype == object:
            kind = pd.api.types.infer_dtype(shown[name], skipna=True)
            if kind not in ("string", "unicode", "empty", "boolean"):
                shown[name] = shown[name].astype(str)

    if isinstance(shown.index, pd.DatetimeIndex):
        index = shown.index
        if index.tz is None:
            index = index.tz_localize("UTC")
        shown.index = index.tz_convert(display_zone())
    return shown


def render_frame(
    st,
    frame: pd.DataFrame,
    *,
    height: int | None = None,
    empty: str = "Nothing to show.",
) -> None:
    """A formatted dataframe, or a note that there is nothing in it.

    ``height`` is omitted rather than passed as ``None``: Streamlit rejects a
    null height, and every caller that does not care would otherwise have to
    say so.
    """
    if frame is None or frame.empty:
        render_note(st, empty)
        return

    shown = for_display(frame)
    options: dict[str, Any] = {"width": "stretch"}
    if height is not None:
        options["height"] = height
    try:
        options["column_config"] = column_config_for(st, shown)
    except Exception:  # noqa: BLE001 - formatting must never stop the table
        pass
    st.dataframe(shown, **options)


# --------------------------------------------------------------------------- #
# The operator's real account
# --------------------------------------------------------------------------- #
def account_balance(st, data: dict) -> float:
    """The sidebar control for the real account's balance.

    **This changes nothing about the backtest.** The simulator ran at its own
    starting equity; a real account rarely matches it, and a recommendation
    in units is meaningless at the wrong account size. This scales the
    *displayed* recommendation to the account actually being traded — a
    display transform and nothing more. No config is written and no position
    is resized anywhere but on the screen.
    """
    current = float(st.session_state.get(BALANCE_KEY, DEFAULT_BALANCE))
    balance = st.number_input(
        f"Account balance ({CURRENCY})",
        min_value=0.0,
        value=current,
        step=100.0,
        key=BALANCE_KEY,
        help=(
            "Your real Trading 212 balance. Recommended position sizes are "
            "scaled to it. Nothing is written anywhere — set "
            "CFD_ACCOUNT_BALANCE to change the default."
        ),
    )

    simulated = simulated_equity(data)
    if simulated:
        st.caption(
            f"Simulated at {money(simulated)} — sizes scaled by "
            f"×{scale_factor(data, balance):,.4g}"
        )
    else:
        st.caption("No completed run, so nothing to scale yet.")
    return float(balance)


def simulated_equity(data: dict) -> float | None:
    """What the backtest started with, if the run recorded it."""
    summary = data.get("summary")
    metrics = (getattr(summary, "value", None) or {}).get("metrics") or {}
    try:
        equity = float(metrics.get("initial_equity"))
    except (TypeError, ValueError):
        return None
    return equity if equity > 0 else None


def scale_factor(data: dict, balance: float | None = None) -> float:
    """How much to shrink a simulated position to fit the real account.

    ``1.0`` when the run's starting equity is unknown — an unscaled number is
    at least honestly the simulator's, whereas a guessed factor is nobody's.
    """
    simulated = simulated_equity(data)
    if not simulated or balance is None or balance <= 0:
        return 1.0
    return float(balance) / simulated


# --------------------------------------------------------------------------- #
# Formatting — pure, and tested directly
# --------------------------------------------------------------------------- #
def _missing(value: Any) -> bool:
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def number(value: Any, places: int = 2) -> str:
    """A number, or an em dash. Never ``nan``."""
    if _missing(value):
        return DASH
    try:
        return f"{float(value):,.{places}f}"
    except (TypeError, ValueError):
        return str(value)


def money(value: Any, currency: str | None = None, places: int = 2) -> str:
    """A signed amount, with the sign kept in front of the symbol."""
    if _missing(value):
        return DASH
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return str(value)
    symbol = CURRENCY if currency is None else currency
    sign = "-" if amount < 0 else ""
    return f"{sign}{symbol}{abs(amount):,.{places}f}"


def percent(value: Any, places: int = 2) -> str:
    """A fraction rendered as a percentage."""
    if _missing(value):
        return DASH
    try:
        return f"{float(value) * 100:,.{places}f}%"
    except (TypeError, ValueError):
        return str(value)


def moment(value: Any) -> str:
    """A timestamp in the display zone: ``24 Aug 2026, 15:30 BST``."""
    stamp = localise(value)
    if stamp is None:
        return DASH if _missing(value) else str(value)
    return f"{stamp.day} {stamp:%b %Y, %H:%M %Z}"


def short_moment(value: Any) -> str:
    """A timestamp without the year: ``Mon 24 Aug, 15:30 BST``."""
    stamp = localise(value)
    if stamp is None:
        return DASH
    return f"{stamp:%a} {stamp.day} {stamp:%b, %H:%M %Z}"


def day(value: Any) -> str:
    """A date alone: ``Mon 24 Aug 2026``."""
    stamp = localise(value)
    if stamp is None:
        return DASH
    return f"{stamp:%a} {stamp.day} {stamp:%b %Y}"


def run_moment(run_id: Any) -> str:
    """A run identifier rendered as a local date and time.

    Run ids are compact UTC timestamps (``20260824T090000Z``), which is the
    right shape for a directory name and the wrong shape for a person. A
    value that does not parse is returned unchanged — some other identifier
    is still an identifier.
    """
    raw = str(run_id or "").strip()
    if not raw:
        return DASH
    try:
        stamp = pd.to_datetime(raw, format="%Y%m%dT%H%M%SZ", utc=True)
    except (ValueError, TypeError):
        return raw
    return short_moment(stamp)


def duration(seconds: Any) -> str:
    """A run length in the largest sensible unit."""
    if _missing(seconds):
        return DASH
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return str(seconds)
    if value < 90:
        return f"{value:.1f} seconds"
    if value < 5400:
        return f"{value / 60:.1f} minutes"
    return f"{value / 3600:.1f} hours"


def age(value: Any, now: pd.Timestamp | None = None) -> str:
    """How long ago something happened, in the largest sensible unit."""
    if _missing(value):
        return DASH
    try:
        stamp = pd.Timestamp(value)
    except (TypeError, ValueError):
        return DASH
    if stamp.tz is None:
        stamp = stamp.tz_localize("UTC")

    seconds = (utcnow(now) - stamp.tz_convert("UTC")).total_seconds()
    if seconds < 0:
        return f"in {_span(-seconds)}"
    return f"{_span(seconds)} ago"


def _span(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f} seconds"
    if seconds < 5400:
        return f"{seconds / 60:.0f} minutes"
    if seconds < 172_800:
        return f"{seconds / 3600:.1f} hours"
    return f"{seconds / 86_400:.1f} days"


def direction_badge(direction: Any, mode: str = "dark") -> str:
    """A coloured dot and label for long, short or flat."""
    label = str(direction or "flat").lower()
    return theme.swatch(theme.colour_for(label, mode), label.capitalize(), mode)
