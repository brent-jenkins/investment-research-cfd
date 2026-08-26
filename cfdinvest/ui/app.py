"""cfdinvest dashboard — the entry point.

Run it with::

    streamlit run cfdinvest/ui/app.py

**This app is read-only.** It opens files the pipeline already wrote and
renders them. It places no orders, refreshes no data, writes no config and
contacts no external service.

Navigation is declared here rather than inferred from filenames, so the
sidebar label and the page's own heading are the same string. Streamlit's
filename convention would give "05 Profit and Loss" a different name from
the "Profit & Loss" printed at the top of it, and a nav entry that does not
match the page it opens is a small, constant papercut.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Streamlit runs each page as a script, so the import root is this file's
# directory rather than the project. Must happen before any cfdinvest import.
_ROOT = str(Path(__file__).resolve().parents[2])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import streamlit as st  # noqa: E402

from cfdinvest.ui import components, theme  # noqa: E402

st.set_page_config(
    page_title=theme.APP_TITLE, page_icon="📈", layout="wide"
)
mode = theme.apply(st)

#: (file, title, icon) — the title is both the nav label and the page heading.
#:
#: The directory is `views/`, not `pages/`, and that is load-bearing.
#: Streamlit auto-discovers a directory called `pages/` and builds a sidebar
#: from it, and that automatic navigation outlived every attempt to suppress
#: it: a signed-out visitor was shown all twenty-two page names plus the
#: entry script. st.navigation takes explicit paths, so the directory does
#: not need the magic name -- and without the name there is nothing to
#: discover and nothing to suppress.
PAGES: tuple[tuple[str, str, str], ...] = (
    ("views/00_Dashboard.py", "Dashboard", "📈"),
    ("views/01_Status.py", "System status", "🩺"),
    ("views/02_Signals.py", "Signals", "🧭"),
)

def _sign_in() -> None:
    """The whole app for someone who is not signed in."""
    components.require_login(st)


# st.navigation has to run on *every* path, signed in or not.
#
# Streamlit falls back to discovering `pages/` whenever a session finishes
# without calling it -- so stopping at the sign-in screen before this point
# left the automatic navigation in charge, and an anonymous visitor was
# shown every page name plus the entry script itself. Registering a
# navigation is what tells Streamlit the pages/ directory is not in charge.
#
# Signed out, that navigation holds one hidden page: the sign-in form, and
# nothing else in the sidebar.
if not components.current_user(st):
    # No sidebar at all on the sign-in screen. The wordmark is already in
    # the middle of the page, and an empty panel beside the form reads as a
    # page that failed to load.
    components.hide_sidebar(st)
    st.navigation([st.Page(_sign_in, title="Sign in")], position="hidden").run()
    st.stop()

# The logo slot is the only place above the navigation; st.navigation draws
# the links at the very top of the sidebar regardless of call order. Drawn
# only once signed in, so it cannot keep an otherwise-empty sidebar alive.
components.render_brand(st, mode)

navigation = st.navigation(
    [
        st.Page(
            str(Path(__file__).parent / relative),
            title=title,
            icon=icon,
            default=index == 0,
        )
        for index, (relative, title, icon) in enumerate(PAGES)
    ]
)
# After the navigation, so it sits at the foot of the sidebar under the
# page list rather than above it.
components.render_sign_out(st)
navigation.run()
