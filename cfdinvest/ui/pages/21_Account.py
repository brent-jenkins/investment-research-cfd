"""Your dashboard account: who is signed in, and changing the password.

The only page that writes anything. Everything else here opens files the
pipeline produced and renders them; this one edits the credential store, and
that is the whole reason it exists -- a password you cannot change without a
deploy is a password nobody changes.

It cannot reach the lake, the run artefacts or a position.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parents[3])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import streamlit as st  # noqa: E402

from cfdinvest.ui import auth, components  # noqa: E402

data = components.page_setup(st, "Account", icon="🔑")
user = st.session_state.get(auth.SESSION_KEY, "")

components.render_kv(
    st,
    {
        "Signed in as": user,
        "Credential store": str(auth.credentials_path()),
        "Password hashing": "scrypt, salted per credential",
    },
)

st.divider()
st.subheader("Change your password")

st.caption(
    "Your current password is asked for even though you are already signed "
    "in. A session left open on a shared screen should not be enough to "
    "lock you out of your own dashboard."
)

with st.form("cfd_change_password"):
    current = st.text_input(
        "Current password", type="password", autocomplete="current-password"
    )
    new = st.text_input(
        "New password", type="password", autocomplete="new-password"
    )
    confirm = st.text_input(
        "New password again", type="password", autocomplete="new-password"
    )
    st.caption(
        f"At least {auth.MIN_PASSWORD_LENGTH} characters. Length is the only "
        "rule: composition rules push people towards predictable "
        "substitutions and a sticky note."
    )
    submitted = st.form_submit_button("Change password")

if submitted:
    changed, message = auth.change_password(user, current, new, confirm)
    if changed:
        st.success(f"{message} It is in use from your next sign-in.")
    else:
        st.error(message)

st.divider()
components.render_note(
    st,
    "This dashboard is protected by a sign-in form rather than the "
    "reverse proxy. That gives you a session and a password you can "
    "change, and it is a **weaker boundary** than basic auth: an "
    "unauthenticated request still reaches the application, and only the "
    "page content is withheld. Reasonable for a dashboard holding no "
    "personal data that can place no trade — see `ui/auth.py`.",
)
