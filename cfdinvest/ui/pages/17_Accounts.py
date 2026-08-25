"""Accounts — what exists, what each is for, and what governs it.

The registry names accounts and maps each to a directory under the same data
root. It does not move anything: repointing an account leaves the old tree
where it is, and the new one starts empty.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parents[3])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from cfdinvest.accounts import (  # noqa: E402
    ACCOUNT_TYPES,
    AccountProfile,
    load_registry,
    write_registry,
)
from cfdinvest.ui import components  # noqa: E402

data = components.page_setup(
    st,
    "Accounts",
    icon="🗂",
    subtitle="What exists, what each account is for, and which limits govern it.",
)
config = data["config"]

try:
    registry = load_registry(config)
except Exception as error:  # noqa: BLE001 - the page must not go dark
    st.error(f"**The account registry could not be read.** {error}")
    st.stop()

# --------------------------------------------------------------------------- #
# What exists
# --------------------------------------------------------------------------- #
st.subheader("Registered accounts")

components.render_frame(st, pd.DataFrame(registry.describe()))

live = [profile for profile in registry if profile.is_real_money and profile.enabled]
if live:
    st.warning(
        f"**{len(live)} account(s) hold real money:** "
        f"{', '.join(profile.label for profile in live)}. Everything filed "
        "against these describes an account you can lose."
    )

components.render_note(
    st,
    "The three built-in accounts cannot be removed or repointed — every path "
    "and migration assumes they exist, and repointing one would make "
    "everything written so far unreachable under the name that wrote it. "
    "They can be disabled and relabelled.",
)

st.divider()

# --------------------------------------------------------------------------- #
# What governs each
# --------------------------------------------------------------------------- #
st.subheader("What governs an account")

chosen = st.selectbox("Account", options=registry.names)
profile = registry.get(chosen)

if profile is not None:
    cells = st.columns(4)
    cells[0].metric("Type", profile.type)
    cells[1].metric("Tree", profile.tree)
    cells[2].metric("Enabled", "yes" if profile.enabled else "no")
    cells[3].metric("Real money", "yes" if profile.is_real_money else "no")

    if profile.purpose:
        components.render_note(st, profile.purpose)

    limits = profile.risk_limits()
    sizing = profile.sizing_config()
    components.render_kv(
        st,
        {
            "Account balance": components.money(sizing.account_balance_gbp),
            "Risk per trade": components.money(sizing.max_risk_per_trade_gbp),
            "Exposure per instrument": components.money(
                sizing.max_exposure_per_instrument_gbp
            ),
            "Total exposure (book)": components.money(limits.max_total_exposure_gbp),
            "Effective leverage": f"{limits.max_effective_leverage:,.0f}x",
            "Margin usage": components.percent(limits.max_margin_usage_pct),
        },
    )

    overrides = {
        **{f"risk.{k}": v for k, v in profile.risk_overrides.items()},
        **{f"sizing.{k}": v for k, v in profile.sizing_overrides.items()},
        **{f"exit.{k}": v for k, v in profile.exit_overrides.items()},
    }
    if overrides:
        with st.expander("Overrides on the shared defaults"):
            components.render_kv(st, overrides)
    else:
        components.render_note(
            st,
            "No overrides — this account uses the shared defaults. Overrides "
            "are applied to the existing configuration objects rather than "
            "defining a fourth place a limit can live.",
        )

st.divider()

# --------------------------------------------------------------------------- #
# Register one
# --------------------------------------------------------------------------- #
st.subheader("Register an account")

with st.form("register_account"):
    cells = st.columns(3)
    name = cells[0].text_input("Name", value="", placeholder="momentum")
    kind = cells[1].selectbox("Type", options=ACCOUNT_TYPES, index=3)
    tree = cells[2].text_input(
        "Tree",
        value="",
        placeholder="defaults to the name",
        help="The directory under the data root. Several accounts may share "
        "one deliberately.",
    )

    purpose = st.text_input("What it is for", value="")
    cells = st.columns(3)
    strategy = cells[0].text_input("Strategy", value="", placeholder="optional")
    regimes = cells[1].text_input(
        "Regimes",
        value="",
        placeholder="risk_on, low",
        help="Comma separated. Only trades in these regimes route here.",
    )
    instruments = cells[2].text_input(
        "Instruments", value="", placeholder="SPX500, NAS100"
    )

    submitted = st.form_submit_button("Register", type="primary")

if submitted:
    if not name.strip():
        st.error("An account needs a name.")
    else:
        try:
            new_profile = AccountProfile(
                name=name.strip().lower(),
                type=kind,
                tree=tree.strip().lower() or None,
                purpose=purpose.strip(),
                strategy=strategy.strip() or None,
                regimes=tuple(
                    part.strip() for part in regimes.split(",") if part.strip()
                ),
                instruments=tuple(
                    part.strip().upper()
                    for part in instruments.split(",")
                    if part.strip()
                ),
            )
            updated = registry.register(new_profile)
        except ValueError as error:
            st.error(f"**Not registered.** {error}")
        else:
            path = write_registry(updated, config)
            if path is None:
                st.error("**Not saved.** The registry could not be written.")
            else:
                st.success(f"Registered {new_profile.label}.")
                st.caption(f"Written to `{path}`")

components.render_note(
    st,
    "Registering an account is what makes its directory legal — nothing else "
    "does, so a typo is refused rather than quietly creating a tree nobody "
    "will look in.",
)
