"""Shared rendering: page setup, the banner, and formatting.

Seven pages need the same theme, the same banner and the same "this is not
available, and here is why" panel. Putting that here keeps the pages short
enough to read, and means the read-only contract is stated in one place
rather than seven.

Formatting helpers are pure and are tested directly; the ``render_*``
functions take the ``streamlit`` module as their first argument so that
importing this module never requires a Streamlit runtime.
"""

from __future__ import annotations

import html
from typing import Any, Iterable

import numpy as np
import pandas as pd

from cfdinvest.ui import theme
# Imported as a module, not by name: the checks are looked up at call
# time so a caller -- or a test -- can substitute them.
from cfdinvest.ui import health as health_module
from cfdinvest.ui.health import BANNER_TEXT, HealthReport, HealthThresholds
from cfdinvest.ui.sources import Source, utcnow

__all__ = [
    "PAGES",
    "DASH",
    "bootstrap",
    "read_everything",
    "page_setup",
    "render_banner",
    "render_absent",
    "render_note",
    "render_frame",
    "money",
    "number",
    "percent",
    "moment",
    "age",
    "direction_badge",
]

#: Shown wherever a value is genuinely absent. One character, used
#: consistently, so an empty cell is never mistaken for a zero.
DASH = "—"

#: The page list, for the landing page's index.
PAGES: tuple[tuple[str, str], ...] = (
    ("Status", "Lake, scheduler, market and every health check"),
    ("Signals", "What the model wants, per instrument"),
    ("Execution", "Recommended trades, fills, levels and costs"),
    ("Positions", "What is open, and what it is carrying"),
    ("P&L", "Equity curve and the cost decomposition"),
    ("Logs", "Scheduler, pipeline, ingestion, execution and errors"),
    ("Config", "Every setting the run used, read-only"),
)


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
# Reading
# --------------------------------------------------------------------------- #
def read_everything(
    st, *, now: pd.Timestamp | None = None, thresholds: HealthThresholds | None = None
) -> dict[str, Any]:
    """Read every source once, and run the health checks over them.

    Cached briefly so that clicking a widget does not re-read the lake.
    Every page calls this; the cache is what makes that cheap. The sidebar's
    refresh button clears it.
    """
    from cfdinvest.ui import sources

    @st.cache_data(ttl=30, show_spinner=False)
    def _read(_stamp: str) -> dict[str, Any]:
        config_source = sources.load_app_config()
        config = config_source.value
        universe = list(getattr(config.market, "tickers", ()) or ())

        lake = sources.load_lake_metadata(config, now=now)
        summary = sources.load_last_pipeline_summary(config)
        scheduler_status = sources.load_scheduler_status(config, universe, now=now)
        execution = sources.load_execution_frame(config)
        signals = sources.load_signal_frame(config)
        errors = sources.load_error_log(config)
        disk = sources.disk_usage(config.paths.data_root)
        market = sources.market_status(universe, now=now)

        return {
            "config_source": config_source,
            "config": config,
            "universe": universe,
            "lake": lake,
            "summary": summary,
            "scheduler": scheduler_status,
            "execution": execution,
            "signals": signals,
            "errors": errors,
            "disk": disk,
            "market": market,
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
def page_setup(st, title: str, *, icon: str = "📈") -> dict[str, Any]:
    """Configure the page, apply the theme, read the data, draw the banner.

    Returns the bundle from :func:`read_everything` so the page can get on
    with rendering.
    """
    try:
        st.set_page_config(
            page_title=f"cfdinvest — {title}", page_icon=icon, layout="wide"
        )
    except Exception:  # noqa: BLE001 - already configured by another page
        pass

    theme.apply(st)
    data = read_everything(st)

    st.markdown(
        f'<span class="cfd-readonly">read-only</span>', unsafe_allow_html=True
    )
    st.title(title)
    render_banner(st, data["health"])
    _render_sidebar(st, data)
    return data


def _render_sidebar(st, data: dict[str, Any]) -> None:
    """Freshness, health and the refresh control."""
    health: HealthReport = data["health"]
    with st.sidebar:
        st.markdown("### cfdinvest")
        st.caption("Read-only dashboard. This app never places an order.")

        state = "🔴 critical" if health.show_banner else (
            "🟠 warnings" if health.triggered else "🟢 healthy"
        )
        st.markdown(f"**System:** {state}")
        st.caption(f"Checked {moment(data['now'])}")

        summary = data["summary"]
        if summary.ok:
            st.caption(f"Run `{summary.value.get('run_id', '?')}`")

        if st.button("Refresh", width="stretch"):
            st.cache_data.clear()
            st.session_state["cfd_refresh_stamp"] = str(utcnow())
            st.rerun()


def render_banner(st, health: HealthReport) -> None:
    """The persistent critical banner, or a quiet all-clear."""
    if health.show_banner:
        items = "".join(
            f"<li>{html.escape(check.title)} — {html.escape(check.detail)}</li>"
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


def render_frame(st, frame: pd.DataFrame, *, height: int | None = None) -> None:
    """A dataframe, or a note that there is nothing in it.

    ``height`` is omitted rather than passed as ``None``: Streamlit rejects
    a null height, and every caller that does not care about height would
    otherwise have to say so.
    """
    if frame is None or frame.empty:
        render_note(st, "Nothing to show.")
        return
    if height is None:
        st.dataframe(frame, width="stretch")
    else:
        st.dataframe(frame, width="stretch", height=height)


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


def money(value: Any, currency: str = "", places: int = 2) -> str:
    """A signed amount, with the sign kept in front of the symbol."""
    if _missing(value):
        return DASH
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return str(value)
    sign = "-" if amount < 0 else ""
    return f"{sign}{currency}{abs(amount):,.{places}f}"


def percent(value: Any, places: int = 2) -> str:
    """A fraction rendered as a percentage."""
    if _missing(value):
        return DASH
    try:
        return f"{float(value) * 100:,.{places}f}%"
    except (TypeError, ValueError):
        return str(value)


def moment(value: Any) -> str:
    """A timestamp in UTC, to the minute."""
    if _missing(value):
        return DASH
    try:
        stamp = pd.Timestamp(value)
    except (TypeError, ValueError):
        return str(value)
    if stamp.tz is None:
        stamp = stamp.tz_localize("UTC")
    return f"{stamp.tz_convert('UTC'):%Y-%m-%d %H:%M} UTC"


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

    delta = utcnow(now) - stamp.tz_convert("UTC")
    seconds = delta.total_seconds()
    if seconds < 0:
        return f"in {age(utcnow(now) - delta, utcnow(now))}".replace(" ago", "")
    if seconds < 90:
        return f"{seconds:.0f} s ago"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min ago"
    if seconds < 172_800:
        return f"{seconds / 3600:.1f} h ago"
    return f"{seconds / 86_400:.1f} days ago"


def direction_badge(direction: Any) -> str:
    """A coloured dot and label for long/short/flat."""
    label = str(direction or "flat").lower()
    return theme.swatch(theme.colour_for(label), label)
