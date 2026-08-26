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
from cfdinvest.ui import auth as auth_module
from cfdinvest.ui import health as health_module
from cfdinvest.ui.health import HealthReport, HealthThresholds
from cfdinvest.ui.sources import Source, utcnow

__all__ = [
    "DASH",
    "DISPLAY_TZ",
    "CURRENCY",
    "PREFILL_KEY",
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
    "clock",
    "coverage_table",
    "render_state_card",
    "render_tiles",
    "render_crumb",
    "price",
    "confirmed_book",
    "live_prices",
    "latest_signal_rows",
    "render_regime_summary",
    "confirmed_open_count",
    "require_login",
    "hide_sidebar",
    "current_user",
    "sign_out",
    "render_sign_out",
    "state_colour",
    "style_states",
    "describe_flags",
    "plural",
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

#: Where a suggestion picked on the Overview waits for the Confirm page.
#:
#: A hand-off, not a decision: it carries the numbers so they need not be
#: retyped at the moment you are committing to a trade, which is exactly when
#: a typo is expensive. Confirm Trades still records what you *actually* did,
#: and nothing about this places an order.
PREFILL_KEY = "cfd_prefill_trade"

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

    # The account is part of the cache key, not just an argument: switching
    # from virtual to live has to re-read everything, and a cache keyed only on
    # the refresh stamp would hand back the other account's numbers under the
    # new label -- which is the exact confusion the separation exists to stop.
    account = current_account(st)

    @st.cache_data(ttl=30, show_spinner=False)
    def _read(_stamp: str, account: str | None) -> dict[str, Any]:
        config_source = sources.load_app_config()
        config = config_source.value
        universe = list(getattr(config.market, "tickers", ()) or ())

        return {
            "config_source": config_source,
            "config": config,
            "universe": universe,
            "account": account,
            "lake": sources.load_lake_metadata(config, now=now),
            "summary": sources.load_last_pipeline_summary(config, account=account),
            "scheduler": sources.load_scheduler_status(
                config, universe, now=now, account=account
            ),
            "execution": sources.load_execution_frame(config, account=account),
            "signals": sources.load_signal_frame(config, account=account),
            "errors": sources.load_error_log(config),
            "disk": sources.disk_usage(config.paths.data_root),
            "market": sources.market_status(universe, now=now),
            "analytics": sources.load_run_analytics(config, account=account),
            "persisted": sources.load_persisted_run(config),
            "paper": sources.load_paper_book(config, account=account),
        }

    stamp = st.session_state.get("cfd_refresh_stamp", "initial")
    try:
        data = _read(stamp, account)
    except Exception:  # noqa: BLE001 - never let caching break the dashboard
        data = _read.__wrapped__(stamp, account)  # type: ignore[attr-defined]

    # A Streamlit server keeps imported modules in sys.modules across reruns
    # even though it re-reads the page scripts, so a bundle cached before a
    # new key existed -- or a page newer than the module it imports -- would
    # otherwise KeyError on every page until someone restarted the process.
    # Missing keys become absent Sources, which every page already renders.
    data = {**_empty_bundle(), **data}

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
#: Every key a page may read, and what an absent one looks like. Sources
#: rather than None, because "not available, and here is why" is a state the
#: pages already know how to draw.
BUNDLE_KEYS: tuple[str, ...] = (
    "lake", "summary", "scheduler", "execution", "signals", "errors",
    "disk", "market", "analytics", "persisted", "paper",
)


def _empty_bundle() -> dict[str, Any]:
    """Defaults for every bundle key, for when one is missing."""
    from cfdinvest.config import AppConfig

    missing = Source.missing(
        "this dashboard is running against a newer page than the module it "
        "loaded; restart the Streamlit process to pick it up",
        value={},
    )
    bundle: dict[str, Any] = {key: missing for key in BUNDLE_KEYS}
    bundle["config_source"] = Source(value=AppConfig())
    bundle["config"] = AppConfig()
    bundle["universe"] = []
    bundle["account"] = None
    return bundle


def _write_cookie(st, value: str, max_age: int) -> None:
    """Set (or clear) the session cookie from the browser.

    Streamlit cannot write a response header, so this goes through a
    zero-height component. The component runs in a srcdoc iframe, which
    inherits the page's origin, so it writes a cookie for the real host --
    ``window.parent`` first, falling back to the iframe's own document.

    Best-effort by design. If a browser refuses the cookie the only cost is
    signing in again after a refresh, which is exactly where this started.
    """
    import json as _json

    import streamlit.components.v1 as components

    try:
        forwarded = st.context.headers.get("X-Forwarded-Proto", "")
    except Exception:  # noqa: BLE001 - no headers is not a reason to fail
        forwarded = ""
    # Only over HTTPS: a Secure cookie is simply dropped over plain HTTP,
    # which would silently break sign-in for anyone running this locally.
    secure = "; Secure" if forwarded == "https" else ""
    cookie = (
        f"{auth_module.COOKIE_NAME}={value}; Path=/; Max-Age={max_age}; "
        f"SameSite=Lax{secure}"
    )
    components.html(
        "<script>"
        "try { window.parent.document.cookie = " + _json.dumps(cookie) + "; }"
        "catch (e) { document.cookie = " + _json.dumps(cookie) + "; }"
        "</script>",
        height=0,
    )


def current_user(st) -> str | None:
    """Who is signed in, from the session or the cookie. Never raises.

    ``session_state`` is checked first because it is free; the cookie is what
    survives a refresh. A valid cookie repopulates the session, so the rest
    of the app can keep reading one place.
    """
    signed_in = st.session_state.get(auth_module.SESSION_KEY)
    if signed_in:
        return signed_in

    try:
        token = st.context.cookies.get(auth_module.COOKIE_NAME)
    except Exception:  # noqa: BLE001 - no cookies is not signed in
        return None

    user = auth_module.verify_token(token) if token else None
    if user:
        st.session_state[auth_module.SESSION_KEY] = user
    return user


def sign_out(st) -> None:
    """End the session here and in the browser."""
    st.session_state.pop(auth_module.SESSION_KEY, None)
    _write_cookie(st, "", 0)


def hide_sidebar(st) -> None:
    """Remove the sidebar entirely, for the sign-in screen.

    Signed out there is nothing to put in it: no navigation, and the
    wordmark is already in the middle of the page. What was left was an
    empty 360px panel beside the form, which reads as a page that failed to
    load rather than as a page with nothing to show.

    Also hides the collapse control, so there is no arrow inviting someone
    to open a panel with nothing behind it.
    """
    st.markdown(
        "<style>"
        '[data-testid="stSidebar"], [data-testid="stSidebarCollapsedControl"],'
        '[data-testid="collapsedControl"] { display: none !important; }'
        '[data-testid="stAppViewContainer"] > .main { margin-left: 0 !important; }'
        "</style>",
        unsafe_allow_html=True,
    )


def require_login(st) -> str | None:
    """Draw the sign-in screen unless already signed in. Returns the user.

    Called from :func:`page_setup`, so every page is gated by the one path
    each of them already goes through, and from the entry script before the
    navigation is built, so an anonymous visitor is not shown the list of
    pages they cannot open.

    Stops the script when not signed in. Nothing after this point renders.
    """
    user = current_user(st)
    if user:
        return user

    auth_module.bootstrap_from_environment()

    if not auth_module.is_configured():
        st.error(
            "**No dashboard account exists.** Set `CFD_DASHBOARD_PASSWORD` "
            "in `/etc/cfdinvest/env` and restart the dashboard. There is no "
            "sign-up screen on purpose: an unclaimed dashboard on a public "
            "address belongs to whoever finds it first."
        )
        st.stop()

    # The wordmark above the form, as the other CFDINVEST app does it. The
    # logo slot is not used here: it lives above the navigation, and there
    # is no navigation on this screen.
    st.markdown(
        '<div style="padding:2rem 0 1rem 0;text-align:center;">'
        f'<div style="font-size:1.8rem;font-weight:800;letter-spacing:0.12em;'
        f'color:{theme.BRAND["colour"]};">{theme.BRAND["name"]}</div>'
        f'<div style="font-size:0.85rem;letter-spacing:0.05em;margin-top:0.2rem;'
        f'color:{theme.BRAND["strapline_colour"]};">'
        f'{theme.BRAND["strapline"]}</div></div>',
        unsafe_allow_html=True,
    )

    _, middle, _ = st.columns([1, 1.2, 1])
    with middle:
        with st.form("cfd_sign_in"):
            st.subheader("Sign in")
            username = st.text_input("Username", autocomplete="username")
            password = st.text_input(
                "Password", type="password", autocomplete="current-password"
            )
            if st.form_submit_button("Sign in", width="stretch"):
                if auth_module.check_login(username, password):
                    name = username.strip()
                    st.session_state[auth_module.SESSION_KEY] = name
                    st.session_state.pop("cfd_login_failed", None)
                    _write_cookie(
                        st,
                        auth_module.issue_token(name),
                        auth_module.SESSION_DAYS * 86400,
                    )
                    st.rerun()
                else:
                    # One message for a bad name and a bad password. Telling
                    # them apart tells an attacker which names exist.
                    st.session_state["cfd_login_failed"] = True

        if st.session_state.get("cfd_login_failed"):
            st.error("Those credentials were not recognised.")

    st.stop()
    return None


def render_sign_out(st) -> None:
    """The signed-in user and a way out, at the foot of the sidebar.

    Below the navigation, as the other CFDINVEST app has it: the page list is
    what you use constantly and signing out is what you use once, so the
    thing you reach for least sits furthest down.
    """
    user = st.session_state.get(auth_module.SESSION_KEY)
    if not user:
        return
    with st.sidebar:
        st.divider()
        st.caption(f"Signed in as **{user}**")
        if st.button("Log out", width="stretch", key="cfd_sign_out"):
            sign_out(st)
            st.rerun()


def page_setup(st, title: str, *, icon: str = "📈", subtitle: str = "") -> dict:
    """Configure the page, apply the theme, read the data, draw the banner.

    Returns the bundle from :func:`read_everything` so the page can get on
    with rendering.
    """
    try:
        st.set_page_config(
            page_title=f"{title} · {theme.APP_TITLE}", page_icon=icon,
            layout="wide",
        )
    except Exception:  # noqa: BLE001 - already configured by another page
        pass

    mode = theme.apply(st)
    render_brand(st, mode)
    st.session_state["cfd_mode"] = mode

    # Before anything is read or drawn. An anonymous visitor should not be
    # able to tell from the page whether the pipeline has even run.
    require_login(st)

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
        render_account(st, data)

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


#: Where the chosen account lives between reruns.
ACCOUNT_KEY = "cfd_account"


def render_account(st, data: dict) -> None:
    """Which account is being shown, and the control to change it.

    Deliberately loud when it is live. Every number on every page changes
    meaning between the two, and a demo drawdown read as a live one is the
    kind of mistake that only looks obvious afterwards.

    The selector appears only when separation is enabled -- on a shared tree
    there is one set of artefacts and offering a choice would imply otherwise.
    """
    from cfdinvest.env import ACCOUNT_MODES, read_environment

    try:
        environment = read_environment()
    except Exception:  # noqa: BLE001 - the sidebar must not take a page down
        return

    if not environment.account_configured:
        st.caption("One account. Set CFD_ACCOUNT_MODE to separate virtual from live.")
        return

    current = st.session_state.get(ACCOUNT_KEY, environment.account or ACCOUNT_MODES[0])
    labels = {"virtual": "Virtual account", "live": "Live account"}
    chosen = st.radio(
        "Account",
        options=list(ACCOUNT_MODES),
        index=list(ACCOUNT_MODES).index(current) if current in ACCOUNT_MODES else 0,
        format_func=lambda name: labels.get(name, name),
        key=ACCOUNT_KEY,
    )

    if chosen == "live":
        st.markdown(
            '<div style="background:#C62828;color:#fff;padding:6px 10px;'
            'border-radius:6px;font-weight:700;text-align:center;">'
            "LIVE — real money</div>",
            unsafe_allow_html=True,
        )
    else:
        st.caption("Demo account. Nothing here is real money.")

    if chosen != environment.account:
        st.caption(
            f"Showing **{labels.get(chosen, chosen)}**; this deployment runs "
            f"as **{labels.get(environment.account, environment.account)}**."
        )


def current_account(st) -> str | None:
    """The account the dashboard is reading, or ``None`` for the shared tree."""
    from cfdinvest.env import resolve_account

    chosen = st.session_state.get(ACCOUNT_KEY)
    if chosen:
        return chosen
    try:
        return resolve_account()
    except ValueError:
        return None


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


def render_note(st, text: str, tone: str = "muted") -> None:
    """A muted aside, or a warning where the aside would mislead.

    ``tone="warning"`` exists for the case where a note is reporting that a
    figure could not be produced rather than that it was produced and was
    unremarkable. Rendering those alike is how "the gate is unreachable" got
    read as "nothing worth trading".
    """
    css = "cfd-note cfd-note-warning" if tone == "warning" else "cfd-note"
    st.markdown(f'<div class="{css}">{text}</div>', unsafe_allow_html=True)


def render_section(st, title: str, note: str = "") -> None:
    """A heading with an optional one-line explanation under it."""
    st.subheader(title)
    if note:
        render_note(st, note)


def price(value: Any, places: int = 4) -> str:
    """A price. More decimals than money, because FX moves in the fourth."""
    return number(value, places)


def confirmed_book(data: Mapping[str, Any] | None = None) -> dict:
    """Everything you have confirmed open, keyed by instrument. Never raises.

    The confirm layer is the only record of what you actually hold. The
    simulated book is a different thing and is not substituted here: the two
    differ exactly when it matters, and this page is about real money.
    """
    try:
        from cfdinvest.persist.confirm import confirmed_positions, load_confirmations

        config = (data or {}).get("config")
        account = (data or {}).get("account")
        book = confirmed_positions(load_confirmations(config, account=account))
        return {
            name: entry for name, entry in (book or {}).items()
            if (entry or {}).get("quantity")
        }
    except Exception as error:  # noqa: BLE001
        logger.warning("Could not read the confirmed book: %s", error)
        return {}


def live_prices(
    data: Mapping[str, Any] | None = None, tickers=None
) -> dict[str, tuple[float | None, str]]:
    """``{ticker: (price, where it came from)}``. Never raises.

    A live quote when one is fresh, the most recent close otherwise, and the
    source said out loud either way -- a page that shows a number without
    saying how old it is invites you to trade on a stale one.
    """
    from cfdinvest.livehelper.price_source import resolve_price

    config = (data or {}).get("config")
    account = (data or {}).get("account")
    wanted = [str(t) for t in (tickers or []) if t]
    resolved: dict[str, tuple[float | None, str]] = {}
    for ticker in dict.fromkeys(wanted):
        try:
            found = resolve_price(ticker, app_config=config, account=account)
            resolved[ticker] = (found.price, found.note)
        except Exception as error:  # noqa: BLE001 - one instrument, not the page
            logger.warning("Could not price %s: %s", ticker, error)
            resolved[ticker] = (None, "price unavailable")
    return resolved


def latest_signal_rows(data: Mapping[str, Any] | None = None):
    """The most recent bar's signals as plain dicts, and its timestamp.

    Returns ``([], None)`` when there are none, which is the ordinary state
    rather than a failure -- only active signals are stored, so an empty
    frame means nothing is wanted, not that something is missing.
    """
    from cfdinvest.schema import TIMESTAMP, TICKER

    source = (data or {}).get("signals")
    frame = getattr(source, "value", None)
    if frame is None or getattr(frame, "empty", True):
        return [], None

    working = frame.copy()
    if isinstance(working.index, pd.MultiIndex):
        working = working.reset_index()

    as_of = None
    if TIMESTAMP in working.columns:
        working[TIMESTAMP] = pd.to_datetime(
            working[TIMESTAMP], utc=True, errors="coerce"
        )
        as_of = working[TIMESTAMP].max()
        working = working[working[TIMESTAMP] == as_of]

    if TICKER in working.columns:
        working = working.rename(columns={TICKER: "ticker"})
    return working.to_dict("records"), as_of


#: Regime label -> (icon, severity). The severity is a *stance*, not a
#: judgement about the market: risk-off and a correlation breakdown mean
#: trade smaller, so they carry the warning colour. Nothing here says a
#: falling market is a bad one -- the strategy is happy to be short.
REGIME_MARKS: dict[str, tuple[str, str]] = {
    # Volatility
    "low": ("🟢", "pass"), "medium": ("🟡", "muted"), "high": ("🔴", "warning"),
    "extreme": ("🔴", "critical"),
    # Trend
    "uptrend": ("📈", "pass"), "downtrend": ("📉", "warning"),
    "sideways": ("➖", "muted"), "choppy": ("〰", "warning"),
    # Correlation
    "diversified": ("🟢", "pass"), "normal": ("🟡", "muted"),
    "elevated": ("🟠", "warning"), "breakdown": ("🔴", "warning"),
    "crisis": ("🔴", "critical"),
    # Macro
    "expansion": ("🟢", "pass"), "contraction": ("🟠", "warning"),
    "slowdown": ("🟡", "muted"), "recovery": ("🟢", "pass"),
    # Overall
    "risk_on": ("🟢", "pass"), "risk on": ("🟢", "pass"),
    "risk_off": ("🟠", "warning"), "risk off": ("🟠", "warning"),
    "neutral": ("🟡", "muted"),
}


def render_regime_summary(st, data: Mapping[str, Any] | None = None) -> None:
    """The current regime as a row of tiles, or a note saying there is none."""
    persisted = (data or {}).get("persisted")
    regimes = (getattr(persisted, "value", None) or {}).get("regimes")
    if regimes is None or getattr(regimes, "empty", True):
        render_note(
            st,
            "No regime classification in the latest run. The signal reasons "
            "still name the regime that produced each one.",
        )
        return

    frame = regimes.copy()
    if isinstance(frame.index, pd.MultiIndex):
        frame = frame.reset_index()
    if "timestamp" in frame.columns:
        frame["timestamp"] = pd.to_datetime(
            frame["timestamp"], utc=True, errors="coerce"
        )
        frame = frame[frame["timestamp"] == frame["timestamp"].max()]
    if frame.empty:
        render_note(st, "No regime classification on the latest bar.")
        return

    from cfdinvest.ui import explain

    row = frame.iloc[-1].to_dict()
    labels = [
        ("Volatility", "volatility_regime"), ("Trend", "trend_regime"),
        ("Correlation", "correlation_regime"), ("Macro", "macro_regime"),
        ("Overall", "combined_regime"),
    ]
    tiles = []
    for label, key in labels:
        if key not in row:
            continue
        raw = str(row.get(key, ""))
        icon, level = REGIME_MARKS.get(raw.lower(), ("", "muted"))
        text = explain.phrase(raw, {}).capitalize() or DASH
        tiles.append(
            (label, f"{icon} {text}".strip(), "", level)
        )
    if tiles:
        render_tiles(st, tiles)
    st.caption(f"As at {short_moment(row.get('timestamp'))}")


def confirmed_open_count(data: Mapping[str, Any] | None = None) -> int:
    """How many instruments you have actually confirmed open. Never raises.

    From the confirm layer, which is the only record of what you hold. Zero
    is a real answer and a common one: it means the live-quote poller has
    nothing to price, and silence from it is correct rather than a fault.
    """
    try:
        from cfdinvest.persist.confirm import confirmed_positions, load_confirmations

        config = (data or {}).get("config")
        account = (data or {}).get("account")
        book = confirmed_positions(load_confirmations(config, account=account))
        return sum(1 for entry in (book or {}).values() if (entry or {}).get("quantity"))
    except Exception as error:  # noqa: BLE001 - not knowing is not zero-with-a-crash
        logger.warning("Could not count confirmed positions: %s", error)
        return 0


def render_crumb(st, page: str) -> None:
    """The wordmark and page name above a title."""
    st.markdown(
        f'<div class="cfd-crumb"><b>{html.escape(theme.BRAND["name"])}</b> &nbsp; '
        f"{html.escape(str(page))}</div>",
        unsafe_allow_html=True,
    )


def render_tiles(st, tiles) -> None:
    """A row of status tiles: label, value, note, and a severity accent.

    ``tiles`` is a sequence of ``(label, value, note, level)``. ``level`` is
    a state word -- "pass", "warning", "critical", "unchecked" -- and it
    colours the left edge only. The value keeps the body colour so a tile
    is legible before the accent has been interpreted, and so a row of them
    does not read as a warning because one of them is.
    """
    if not tiles:
        return
    mode = theme.active_mode(st)
    columns = st.columns(len(tiles))
    for column, (label, value, note, level) in zip(columns, tiles):
        colour = state_colour(level, mode) or theme.palette_for(mode)["border"]
        text = DASH if _missing(value) else str(value)
        footer = (
            f'<span class="n">{html.escape(str(note))}</span>' if note else ""
        )
        column.markdown(
            f'<div class="cfd-tile" style="border-left-color:{colour};">'
            f'<span class="k">{html.escape(str(label))}</span>'
            f'<span class="v">{html.escape(text)}</span>{footer}</div>',
            unsafe_allow_html=True,
        )


def render_state_card(st, label: str, value: Any, note: str | None = None) -> None:
    """A metric card whose value is a state word, drawn in that state's colour.

    ``st.metric`` paints every value in the accent, so "Failed" and
    "Succeeded" reach the reader looking the same -- which is the one
    distinction a glance along that row exists to make. The box matches
    Streamlit's metric card exactly, so a row can mix the two.

    An unrecognised value keeps the body colour rather than being given one.
    """
    mode = theme.active_mode(st)
    colour = state_colour(value, mode) or theme.palette_for(mode)["text"]
    text = DASH if _missing(value) else str(value)
    footer = (
        f'<span class="n">{html.escape(str(note))}</span>' if note else ""
    )
    st.markdown(
        f'<div class="cfd-stat"><span class="k">{html.escape(str(label))}</span>'
        f'<span class="v" style="color:{colour};">{html.escape(text)}</span>'
        f"{footer}</div>",
        unsafe_allow_html=True,
    )


def render_kv(st, rows: Mapping[str, Any]) -> None:
    """Compact key/value rows, for facts that do not deserve a metric card.

    Seven metric cards in a row is a wall; seven of these is a list you can
    read down. Used where the label matters more than the number's size.
    """
    mode = theme.active_mode(st)

    def cell(value: Any) -> str:
        # A state word is worth colouring here for the same reason it is in a
        # table: "Critical" and "Healthy" should not need reading to be told
        # apart. Everything else keeps the body colour.
        text = html.escape(str(value))
        colour = state_colour(value, mode)
        if colour is None:
            return f'<span class="v">{text}</span>'
        return (
            f'<span class="v" style="color:{colour};font-weight:600;">'
            f"{text}</span>"
        )

    body = "".join(
        f'<div class="cfd-kv"><span class="k">{html.escape(str(key))}</span>'
        f"{cell(value)}</div>"
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


#: Manifest columns that are provenance rather than fact. ``params`` is the
#: exact kwargs the download was made with -- worth keeping on disk, and pure
#: noise in a table a person reads.
COVERAGE_NOISE: tuple[str, ...] = ("params",)

#: Manifest column -> what to call it on screen, in the order they read best.
COVERAGE_LABELS: tuple[tuple[str, str], ...] = (
    ("status", "Status"),
    ("rows_total", "Rows"),
    ("first_timestamp", "First bar"),
    ("last_timestamp", "Last bar"),
    ("anomalies", "Data quality flags"),
    ("vintage_mode", "Vintage mode"),
    ("fell_back", "Fell back"),
    ("initial_release_supported", "Initial release"),
    ("error", "Error"),
    ("updated_at", "Written"),
)


def describe_flags(value: Any) -> str:
    """``{'close_outside_range': 88}`` as ``close outside range: 88``.

    The manifest stores anomaly counts as a mapping, and putting that in a
    table renders Python's repr -- braces, quotes and underscores -- which
    tells a reader nothing and looks like a leak. An empty mapping is the
    common case and reads as nothing at all rather than ``{}``.
    """
    if isinstance(value, str):
        return value
    if _missing(value):
        return DASH
    if not isinstance(value, dict):
        return str(value)
    if not value:
        return DASH
    return ", ".join(
        f"{str(name).replace('_', ' ')}: {count:,}"
        for name, count in sorted(value.items())
    )


def coverage_table(frame: pd.DataFrame) -> pd.DataFrame:
    """The ingestion manifest as something a person can read.

    Raw, this frame renders dict reprs, the literal string "None" for every
    row without an error, and a params column wider than the screen. None of
    that is wrong -- it is simply the manifest's shape rather than a reader's.
    """
    if frame is None or getattr(frame, "empty", True):
        return frame

    shown = frame.copy()
    if "anomalies" in shown.columns:
        shown["anomalies"] = shown["anomalies"].map(describe_flags)
    if "error" in shown.columns:
        # "None" in every row of an Error column reads as a fault, not as
        # the absence of one.
        shown["error"] = shown["error"].map(
            lambda v: DASH if _missing(v) else str(v)
        )
    for name in ("first_timestamp", "last_timestamp"):
        if name in shown.columns:
            shown[name] = pd.to_datetime(
                shown[name], utc=True, errors="coerce"
            ).map(day)
    if "updated_at" in shown.columns:
        shown["updated_at"] = shown["updated_at"].map(clock)

    shown = shown.drop(columns=[c for c in COVERAGE_NOISE if c in shown.columns])

    ordered = [name for name, _ in COVERAGE_LABELS if name in shown.columns]
    ordered += [c for c in shown.columns if c not in ordered]
    shown = shown[ordered]
    return shown.rename(columns=dict(COVERAGE_LABELS))


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


#: Columns whose values name a state rather than measure something. Matched
#: on the display label as well as the raw name, because tables are renamed
#: before they are drawn.
STATE_COLUMNS: frozenset[str] = frozenset({
    "status", "severity", "outcome", "direction", "verdict", "result",
    "state", "level", "health", "action", "breach", "posture",
})

#: State word -> semantic colour. Anything absent is left in the body colour,
#: which is the point: inventing a colour for an unrecognised state would be
#: a claim about it.
#:
#: "Unchecked" and "unverified" sit with the warnings rather than with the
#: passes. That is this project's rule about silence, in colour: not measured
#: and measured-and-fine are different statements, and only one is green.
STATE_COLOURS: dict[str, str] = {
    "ok": "ok", "pass": "ok", "passed": "ok", "healthy": "ok",
    "succeeded": "ok", "success": "ok", "safe": "ok", "clear": "ok",
    "complete": "ok", "long": "long",
    "warning": "warning", "warnings": "warning", "warn": "warning",
    "borderline": "warning",
    "unchecked": "warning", "unverified": "warning", "stale": "warning",
    "not checked": "warning", "degraded": "warning", "partial": "warning",
    "short": "short",
    "critical": "critical", "failed": "critical", "fail": "critical",
    "error": "critical", "breach": "critical", "breached": "critical",
    "unsafe": "critical", "blocked": "critical", "rejected": "critical",
    "flat": "flat", "info": "muted", "none": "muted", "skipped": "muted",
}


def state_colour(value: Any, mode: str = "dark") -> str | None:
    """The colour a state word should be drawn in, or ``None`` to leave it."""
    key = STATE_COLOURS.get(str(value).strip().lower())
    return None if key is None else theme.colour_for(key, mode)


def _is_state_column(name: Any) -> bool:
    return str(name).strip().lower().replace(" ", "_") in STATE_COLUMNS


def style_states(frame: pd.DataFrame, mode: str = "dark"):
    """Colour the state words in a frame so a column can be scanned.

    Reading "ok" down twenty-five rows to find the one that says "failed" is
    work a colour does for free. Only recognised words are coloured, and only
    in columns that name a state -- a number is never tinted, because the
    reader would have to guess what the threshold was.

    Returns a Styler, or the frame unchanged when there is nothing to colour
    or pandas will not style it. Never raises: a table must still draw.
    """
    if frame is None or getattr(frame, "empty", True):
        return frame
    columns = [c for c in frame.columns if _is_state_column(c)]
    if not columns:
        return frame

    def paint(value: Any) -> str:
        colour = state_colour(value, mode)
        return "" if colour is None else f"color: {colour}; font-weight: 600;"

    try:
        styler = frame.style
        painter = getattr(styler, "map", None) or styler.applymap
        return painter(paint, subset=columns)
    except Exception:  # noqa: BLE001 - colour is a nicety, the table is not
        return frame


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
    st.dataframe(style_states(shown, theme.active_mode(st)), **options)


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


def clock(value: Any) -> str:
    """A timestamp as ``25/08/2026 16:42:03``, in the display zone.

    The metric cards want one fixed shape. "14 minutes ago" makes the reader
    work out when that was and goes stale while they look at it, and the long
    form ("25 Aug 2026, 16:42 BST") is wide enough that the card clipped it.
    This is the same width whatever the value, which is what a row of cards
    needs.

    Relative ages are still the right thing in prose and in tables, where the
    question really is "how long ago" -- see :func:`age`.
    """
    stamp = localise(value)
    if stamp is None:
        return DASH
    return f"{stamp:%d/%m/%Y %H:%M:%S}"


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


def plural(count: Any, noun: str, suffix: str = "s") -> str:
    """``"1 day"``, ``"2 days"`` -- a count with its noun agreeing.

    A card reading "1 days old" is the kind of small wrongness that makes a
    reader trust the rest of the number less, and it appears wherever a
    count is formatted to zero decimal places.
    """
    if _missing(count):
        return DASH
    try:
        number = float(count)
    except (TypeError, ValueError):
        return DASH
    whole = round(number)
    return f"{whole:,.0f} {noun}{'' if whole == 1 else suffix}"


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


def sentiment_readings(st, tickers) -> dict:
    """``{ticker: Sentiment}`` for the instruments shown. Never raises.

    ``st`` is passed in rather than imported, like everything else here:
    this module is imported by tests that have no Streamlit runtime, and a
    module-level ``import streamlit`` would take them all down.

    Cached for fifteen minutes because it is one network call per ticker
    and a page reruns on every click. What is cached is the dataclass and
    nothing else -- st.cache_data pickles what it stores, and a callable
    anywhere in a cached value takes the whole bundle down with it, with
    the symptom appearing on some other page entirely.

    A ticker whose feed fails is absent from the result rather than present
    and empty, so the caller can tell "not read" from "read, and quiet".
    """
    @st.cache_data(ttl=900, show_spinner=False)
    def _read(ticker: str):
        from cfdinvest.advisor.sentiment import sentiment_for

        return sentiment_for(ticker)

    readings = {}
    for ticker in dict.fromkeys(str(t) for t in (tickers or ()) if t):
        try:
            readings[ticker] = _read(ticker)
        except Exception:  # noqa: BLE001 - a news feed is not worth a blank page
            continue
    return readings


def sentiment_badge(reading) -> str:
    """A short, colourless label for a table cell.

    Colourless on purpose. Every other state column here earns its colour
    by meaning something measured against history; this one has no track
    record at all, and painting it green would lend it an authority it has
    not got.
    """
    if reading is None or getattr(reading, "score", None) is None:
        return "—"
    score = reading.score
    if score <= -0.5:
        return f"▼▼ {score:+.2f}"
    if score < -0.15:
        return f"▼ {score:+.2f}"
    if score <= 0.15:
        return f"• {score:+.2f}"
    if score < 0.5:
        return f"▲ {score:+.2f}"
    return f"▲▲ {score:+.2f}"
