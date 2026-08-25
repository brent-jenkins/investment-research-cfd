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

# The logo slot is the only place above the navigation; st.navigation draws
# the links at the very top of the sidebar regardless of call order. Setting
# it here as well as in page_setup costs nothing -- Streamlit keeps one logo
# and the last call wins.
components.render_brand(st, mode)

#: (file, title, icon) — the title is both the nav label and the page heading.
PAGES: tuple[tuple[str, str, str], ...] = (
    ("pages/00_Overview.py", "Overview", "📈"),
    ("pages/01_Status.py", "System status", "🩺"),
    ("pages/02_Signals.py", "Signals", "🧭"),
    ("pages/03_Execution.py", "Execution", "🎯"),
    ("pages/04_Positions.py", "Positions", "📦"),
    ("pages/05_Profit_and_Loss.py", "Profit & Loss", "💷"),
    ("pages/06_Logs.py", "Logs", "📜"),
    ("pages/07_Configuration.py", "Configuration", "⚙"),
    ("pages/08_Analytics.py", "Analytics", "🔬"),
    ("pages/09_Live_Helper.py", "Live-Execution Helper", "🧮"),
    ("pages/10_Risk.py", "Risk", "⚖"),
    ("pages/11_Confirm_Trades.py", "Confirm Trades", "✍"),
    ("pages/12_Monitoring.py", "Operational Monitoring", "🚨"),
    ("pages/13_Journal.py", "Journal", "📓"),
    ("pages/14_Insights.py", "Insights", "🔎"),
    ("pages/15_Behaviour.py", "Behaviour", "🧠"),
    ("pages/16_Simulation.py", "Simulation", "🎬"),
    ("pages/17_Accounts.py", "Accounts", "🗂"),
    ("pages/18_Routing.py", "Routing", "🔀"),
    ("pages/19_T212_Rules.py", "T212 Rules", "📐"),
    ("pages/20_Meta_Strategy.py", "Meta-Strategy", "🎚"),
    ("pages/21_Account.py", "Account", "🔑"),
)

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
navigation.run()
